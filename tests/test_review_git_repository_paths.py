"""真实 Git 路径保真、源码保护及错误仓库隔离回归。"""
import os
import subprocess
from html.parser import HTMLParser
from unittest import mock

from archive_workflow_fixtures import Repository, WorkflowCase
from file_exporter import FileExporter
from main import CompareToolApp
from vcs.git_vcs import GitVCS


class GitRepositoryPathTests(WorkflowCase):
    def repository(self, name, directory_name='work'):
        repo = Repository(self, 'git', name)
        target = repo.base / directory_name
        if target != repo.path:
            repo.path.rename(target)
            repo.path = target
        return repo

    def assert_delivery(self, repo, output, *, scope=None):
        for multi in (False, True):
            task = repo.task('HEAD' if multi else 'HEAD~1', 'HEAD',
                             multi=multi, scope=scope)
            app, result, out = self.generate(
                task, False, output / ('multi' if multi else 'normal'))
            self.assert_success(app)
            self.assertEqual('completed', app._last_task_record['status'], result.issues)
            self.assertFalse(result.issues)
            self.assertEqual([('M', 'a.txt', '')], [
                (file.report_type, file.file_path, file.old_path) for file in result.files])
            for side, expected in (('oldVersion', b'INTENDED_OLD\r\n'),
                                   ('newVersion', b'INTENDED_NEW\r\n')):
                files = {path.relative_to(out / side).as_posix(): path.read_bytes()
                         for path in (out / side).rglob('*') if path.is_file()}
                self.assertEqual({'Demo/a.txt': expected}, files)
            visible = []

            class Text(HTMLParser):
                def handle_data(self, data):
                    visible.append(data)

            Text().feed((out / 'report.html').read_text('utf-8'))
            self.assertIn('INTENDED_NEW', ''.join(visible))
            self.assertNotIn('WRONG_REPOSITORY', ''.join(visible))
            self.assert_no_stages(out)

    def commit_endpoints(self, repo, prefix=''):
        repo.commit({'.gitattributes': b'*.txt text eol=crlf\n',
                     prefix + 'a.txt': b'INTENDED_OLD\n'})
        repo.commit({prefix + 'a.txt': b'INTENDED_NEW\n'})

    def test_trailing_unicode_space_cannot_bypass_tracked_output_protection(self):
        repo = self.repository('trailing-space', 'work\u2003')
        protected = {
            'app/Demo_diff.html': b'TRACKED_REPORT_SOURCE\n',
            'app/oldVersion/Demo/keep.txt': b'TRACKED_OLD_SOURCE\n',
            'app/newVersion/Demo/keep.txt': b'TRACKED_NEW_SOURCE\n',
        }
        old = repo.commit(dict(protected, **{'app/a.txt': b'old\n'}))
        new = repo.commit({'app/a.txt': b'new\n'})
        scope = repo.path / 'app'
        self.assertEqual(str(scope), str(scope).strip())
        report, old_export, new_export = (
            scope / 'Demo_diff.html', scope / 'oldVersion', scope / 'newVersion')
        for multi in (False, True):
            task = repo.task(new if multi else old, new, multi=multi, scope=scope)
            for target in (report, old_export / 'Demo', new_export / 'Demo'):
                with self.subTest(multi=multi, target=target.name):
                    with self.assertRaisesRegex(ValueError, '已跟踪'):
                        CompareToolApp._validate_repository_output_targets(
                            str(scope), task['vcs_type'], [str(target)])
            for multi_report in (False, True):
                app = self.app()
                with mock.patch.object(FileExporter, '_clear_outputs',
                        side_effect=AssertionError('must not clear tracked source')) as clear:
                    if multi_report:
                        app._do_generate_multi([task], str(report), str(old_export),
                                               str(new_export), str(scope))
                    else:
                        app._do_generate(str(scope), task['vcs_type'], task['old_version'],
                            task['new_version'], 'Demo', [], True, True, str(report),
                            str(old_export), str(new_export), str(scope))
                clear.assert_not_called()
                self.assertEqual('failed', app._last_task_record['status'])
                self.assertIn('已跟踪', app._show_error.call_args.args[0])
        self.assertEqual(protected, {path: (repo.path / path).read_bytes() for path in protected})

    def test_unicode_line_separators_never_select_adjacent_repository(self):
        for index, separator in enumerate(('\u0085', '\u2028', '\u2029')):
            with self.subTest(separator=repr(separator)):
                repo = self.repository('separator-' + str(index), 'line' + separator + 'suffix')
                self.commit_endpoints(repo)
                wrong = self.repository('wrong-' + str(index))
                wrong.path.rename(repo.base / 'line')
                wrong.path = repo.base / 'line'
                wrong.commit({'suffixb.txt': b'WRONG_REPOSITORY_OLD\n'})
                wrong.commit({'suffixb.txt': b'WRONG_REPOSITORY_NEW\n'})
                self.assert_delivery(repo, self.root / ('output-' + str(index)))

    def test_unicode_project_prefix_keeps_scope_and_checkout_bytes(self):
        repo = self.repository('project-prefix')
        prefix = 'sub\u2028project/'
        self.commit_endpoints(repo, prefix)
        self.assert_delivery(repo, self.root / 'scope-output', scope=repo.path / prefix)

    def test_bare_unicode_root_keeps_endpoints_and_metadata_protection(self):
        repo = self.repository('bare-source')
        self.commit_endpoints(repo)
        bare = self.root / 'bare\u2028repository\u2003'
        repo.cmd('clone', '--bare', '--quiet', str(repo.path), str(bare))
        for mode in ('git', 'git_multi'):
            with self.assertRaisesRegex(ValueError, '元数据'):
                CompareToolApp._validate_repository_output_targets(
                    str(bare), mode, [str(bare / 'Demo_diff.html')])
        self.assert_delivery(repo, self.root / 'bare-output', scope=bare)

    def test_trailing_space_allows_external_and_untracked_outputs(self):
        repo = self.repository('allowed-output', 'work\u2003')
        self.commit_endpoints(repo)
        self.assert_delivery(repo, self.root / 'external-output')
        self.assert_delivery(repo, repo.path / 'untracked-output')

    def test_crlf_command_records_preserve_root_and_empty_project_prefix(self):
        repo = self.repository('crlf-records')
        self.commit_endpoints(repo)
        original = subprocess.run

        def crlf_records(command, *args, **kwargs):
            result = original(command, *args, **kwargs)
            if ('rev-parse' in command and any(option in command for option in (
                    '--show-toplevel', '--absolute-git-dir', '--git-common-dir'))):
                result.stdout = result.stdout.replace(b'\n', b'\r\n')
            return result

        with mock.patch('main.subprocess.run', side_effect=crlf_records):
            self.assert_delivery(repo, self.root / 'crlf-output')

    def test_malformed_root_records_stop_before_clear_or_scope_cache(self):
        repo = self.repository('malformed')
        self.commit_endpoints(repo)
        original = subprocess.run
        for payload in (b'relative\n', b'\n', os.fsencode(repo.path) + b'\nextra\n',
                        os.fsencode(repo.path), os.fsencode(self.root) + b'\n\n',
                        os.fsencode(repo.path) + b'\n/other-project/\n'):
            with self.subTest(payload=payload):
                def malformed(command, *args, **kwargs):
                    if '--show-toplevel' in command:
                        return subprocess.CompletedProcess(command, 0, payload, b'')
                    return original(command, *args, **kwargs)

                app = self.app()
                out = self.root / 'malformed-output'
                with mock.patch('main.subprocess.run', side_effect=malformed), \
                        mock.patch.object(FileExporter, '_clear_outputs') as clear:
                    app._do_generate(str(repo.path), 'git', 'HEAD~1', 'HEAD', 'Demo',
                        [], True, True, str(out / 'report.html'), str(out / 'oldVersion'),
                        str(out / 'newVersion'), str(out))
                clear.assert_not_called()
                self.assertEqual('failed', app._last_task_record['status'])
                vcs = GitVCS(str(repo.path))
                try:
                    with mock.patch('vcs.git_vcs.subprocess.run', side_effect=malformed):
                        with self.assertRaises(RuntimeError):
                            vcs._git_cwd()
                    self.assertFalse(hasattr(vcs, '_repository_root'))
                finally:
                    vcs.cleanup()
