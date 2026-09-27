"""Local-only repositories and actual worker entry points for archive acceptance."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from main import CompareToolApp
from report_generator import ReportGenerator
from test_archive_report import zip_bytes, tar_bytes


def tree_hashes(root):
    result = {}
    for path in sorted(Path(root).rglob('*')):
        # 批次互斥锁不是交付文件；其有效性由输出锁专项测试覆盖。
        if path.name == '.comparetool_output.lock' and path.parent == Path(root):
            continue
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as source:
                for block in iter(lambda: source.read(1024**2), b''):
                    digest.update(block)
            result[path.relative_to(root).as_posix()] = digest.hexdigest()
    return result


def package(value):
    return zip_bytes({'config/value.txt': value, 'fixed.txt': b'fixed'})


class Repository:
    def __init__(self, case, kind, name):
        self.case, self.kind = case, kind
        self.base = case.root/name
        self.base.mkdir()
        self.path = self.base/'work'
        self.executable = shutil.which(kind)
        if not self.executable or (kind == 'svn' and not shutil.which('svnadmin')):
            case.skipTest('需要本地 ' + kind + ' 命令行工具')
        self.refs = []
        if kind == 'git':
            self.path.mkdir()
            self.cmd('init', '--quiet', '--template=')
            for key, value in (('user.name', 'Archive tests'),
                               ('user.email', 'archive@example.invalid'),
                               ('core.autocrlf', 'false')):
                self.cmd('config', key, value)
        else:
            self.store = self.base/'repository'
            subprocess.run([shutil.which('svnadmin'), 'create', str(self.store)],
                           check=True, capture_output=True)
            self.url = self.store.as_uri()
            self.cmd('checkout', self.url, str(self.path), cwd=self.base)

    def cmd(self, *args, cwd=None):
        command = [self.executable]
        if self.kind == 'svn':
            command += ['--non-interactive', '--config-dir', str(self.base/'svn-config')]
        result = subprocess.run(command + list(args), cwd=cwd or self.path,
                                capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', 'replace'))
        return result.stdout.decode('utf-8', 'replace').strip()

    def write(self, files):
        for name, data in files.items():
            target = self.path/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def commit(self, files=None):
        self.write(files or {})
        if self.kind == 'git':
            self.cmd('add', '--all')
            self.cmd('commit', '--quiet', '-m', 'fixture')
            version = self.cmd('rev-parse', 'HEAD')
        else:
            self.cmd('add', '--force', '--no-ignore', '.')
            self.cmd('commit', '-m', 'fixture')
            self.cmd('update')
            version = self.cmd('info', '--show-item', 'revision')
        self.refs.append(version)
        return version

    def task(self, old, new, *, multi=False, project_name='Demo', scope=None):
        return dict(vcs_type=self.kind + ('_multi' if multi else ''),
                    project_path=str(scope or self.path), old_version=old,
                    new_version='文件级首尾端点' if multi else new,
                    project_name=project_name, exclude_rules='',
                    show_full_context=True, show_project_root=True)


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        work = Path('.tmp').resolve(); work.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='archive_workflow_', dir=work)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = self.root/'gitconfig'; config.write_text('', encoding='utf-8')
        env = mock.patch.dict(os.environ, {
            'COMPARETOOL_TEMP_DIR': str(self.root/'runtime'),
            'COMPARETOOL_TRANSACTION_KEY_FILE': str(self.root/'key'),
            'GIT_CONFIG_GLOBAL': str(config), 'GIT_CONFIG_NOSYSTEM': '1',
        })
        env.start(); self.addCleanup(env.stop)
        log = mock.patch('logger.LOG_FILE', str(self.root/'application.log'))
        log.start(); self.addCleanup(log.stop)

    def app(self):
        app = CompareToolApp.__new__(CompareToolApp)
        app.root = mock.Mock()
        app.root.after.side_effect = lambda _ms, callback: callback()
        app._on_complete = mock.Mock(); app._on_multi_complete = mock.Mock()
        app._show_error = mock.Mock(); app._task_log_dir = self.root/'jobs'
        return app

    def generate(self, task, enabled=True, output=None):
        app = self.app(); captured = []
        out = output or self.root/('on' if enabled else 'off')
        out.mkdir(parents=True, exist_ok=True)
        original = ReportGenerator.generate
        def observe(generator, result, *args, **kwargs):
            captured.append(result)
            return original(generator, result, *args, **kwargs)
        with mock.patch.object(ReportGenerator, 'generate', observe):
            app._do_generate(task.get('project_path', ''), task['vcs_type'],
                task['old_version'], task['new_version'], task['project_name'],
                task.get('exclude_rules', '').splitlines(),
                task.get('show_full_context', True), task.get('show_project_root', True),
                str(out/'report.html'), str(out/'oldVersion'), str(out/'newVersion'),
                str(out), ignore_archive_root=task.get('ignore_archive_root', False),
                recursive_archives=enabled)
        return app, captured[0] if captured else None, out

    def assert_success(self, app):
        message = (app._show_error.call_args.args[0]
                   if app._show_error.called else app._last_task_record)
        self.assertTrue(app._last_task_record['success'], message)

    def delivery(self, out):
        return {side: tree_hashes(out/side) for side in ('oldVersion', 'newVersion')}

    def assert_same_delivery(self, a, b):
        self.assertEqual(self.delivery(a), self.delivery(b))
        instructions = lambda p: {x.name: x.read_bytes() for x in p.glob('*操作说明.txt')}
        self.assertEqual(instructions(a), instructions(b))

    def assert_no_stages(self, out):
        self.assertFalse(list(out.glob('.comparetool_stage_*')))
        self.assertFalse(list(out.glob('.comparetool_transaction_*.json')))
