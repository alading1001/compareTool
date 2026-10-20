"""界面实际选中路径、原生 Git 检出字节和失败清理的交付回归。"""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest import mock

import main
from archive_workflow_fixtures import Repository, WorkflowCase
from file_exporter import FileExporter


class InputPathTests(WorkflowCase):
    @contextmanager
    def window(self, config):
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        with ExitStack() as stack:
            for name, value in (('CONFIG_DIR', str(self.root)), ('CONFIG_FILE', str(config_path)),
                                ('_CONFIG_LOAD_FAILURE', None)):
                stack.enter_context(mock.patch.object(main, name, value))
            app = main.CompareToolApp()
            app.root.withdraw()
            app._on_complete = mock.Mock()
            app._show_error = mock.Mock()
            app.project_name_var.set('Demo')
            app._project_name_manual = True
            app.output_batch_var.set('')
            app._replace_exclude_text('')
            try:
                yield app, config_path
            finally:
                for token in app.root.tk.call('after', 'info'):
                    app.root.tk.call('after', 'cancel', token)
                app.root.destroy()

    @contextmanager
    def synchronous_generation(self):
        def thread(*, target, args, kwargs, daemon):
            return SimpleNamespace(start=lambda: target(*args, **kwargs))
        # 只替换 main 的线程入口，subprocess 的真实管道读取线程仍正常工作。
        with mock.patch('main.threading', SimpleNamespace(Thread=thread)), \
                mock.patch('main.messagebox.askyesno', return_value=True), \
                mock.patch('main.messagebox.showwarning') as warning:
            yield warning

    def adjacent_repositories(self):
        intended = Repository(self, 'git', 'intended')
        intended.path.rename(intended.base / 'work\u2003')
        intended.path = intended.base / 'work\u2003'
        wrong = Repository(self, 'git', 'adjacent')
        wrong.path.rename(intended.base / 'work')
        wrong.path = intended.base / 'work'
        for repo, prefix in ((intended, b'INTENDED_'), (wrong, b'WRONG_')):
            repo.commit({'a.txt': prefix + b'OLD\n'})
            repo.commit({'a.txt': prefix + b'NEW\n'})
        return intended, wrong

    def test_real_gui_keeps_selected_repository_output_and_saved_paths(self):
        intended, _wrong = self.adjacent_repositories()
        output = self.root / 'output\u2003'
        with self.window(dict(vcs_type='git', project_path=str(intended.path),
                              output_dir=str(output))) as (app, config_path):
            app.old_version_var.set('HEAD~1'); app.new_version_var.set('HEAD')
            with self.synchronous_generation() as warning:
                app._generate()
                app.root.update()
            warning.assert_not_called()
            self.assert_success(app)
            self.assertEqual('completed', app._last_task_record['status'])
            self.assertEqual(b'INTENDED_OLD\n', (output/'oldVersion/Demo/a.txt').read_bytes())
            self.assertEqual(b'INTENDED_NEW\n', (output/'newVersion/Demo/a.txt').read_bytes())
            saved = json.loads(config_path.read_text('utf-8'))
            self.assertEqual(str(intended.path), saved['project_path'])
            self.assertEqual(str(output), saved['output_dir'])
            self.assertEqual(str(intended.path), saved['recent_projects'][0]['project_path'])
            task = app._task_from_current_form()
            self.assertEqual(str(intended.path), task['project_path'])
            self.assertEqual(str(intended.path), app._normalize_loaded_multi_tasks([task])[0]['project_path'])
        with self.window(saved) as (restored, _path):
            self.assertEqual(str(intended.path), restored.dir_entry.get())
            self.assertEqual(str(output), restored.output_dir_var.get())

    def test_real_gui_rejects_tracked_output_before_any_clear(self):
        intended, _wrong = self.adjacent_repositories()
        output = intended.path / 'output'
        protected = output / 'Demo_diff.html'
        output.mkdir(); protected.write_bytes(b'TRACKED_SOURCE_MUST_SURVIVE')
        intended.commit()
        with self.window(dict(vcs_type='git', project_path=str(intended.path),
                              output_dir=str(output))) as (app, _path):
            app.old_version_var.set('HEAD~1'); app.new_version_var.set('HEAD')
            with self.synchronous_generation() as warning, \
                    mock.patch.object(FileExporter, '_clear_outputs') as clear:
                app._generate()
            clear.assert_not_called()
            self.assertIn('已跟踪', warning.call_args.args[1])
        self.assertEqual(b'TRACKED_SOURCE_MUST_SURVIVE', protected.read_bytes())

    def test_folder_paths_survive_generation_task_save_and_window_restore(self):
        old, new, output = (self.root / name for name in ('old\u2003', 'new\u3000', 'out\u2003'))
        old.mkdir(); new.mkdir()
        (old/'a.txt').write_bytes(b'old\n'); (new/'a.txt').write_bytes(b'new\n')
        config = dict(vcs_type='folder', output_dir=str(output), last_comparison_paths={
            'folder': dict(old_version=str(old), new_version=str(new))})
        with self.window(config) as (app, config_path):
            task = app._task_from_current_form()
            self.assertEqual((str(old), str(new)), (task['old_version'], task['new_version']))
            restored = app._normalize_loaded_multi_tasks([task])[0]
            self.assertEqual((str(old), str(new)), (restored['old_version'], restored['new_version']))
            with self.synchronous_generation() as warning:
                app._generate()
                app.root.update()
            warning.assert_not_called(); self.assert_success(app)
            self.assertEqual(b'new\n', (output/'newVersion/Demo/a.txt').read_bytes())
            saved = json.loads(config_path.read_text('utf-8'))
            self.assertEqual(config['last_comparison_paths'], saved['last_comparison_paths'])
        with self.window(saved) as (app, _path):
            self.assertEqual(str(old), app.old_version_var.get())
            self.assertEqual(str(new), app.new_version_var.get())


class ExternalAttributePathTests(WorkflowCase):
    def test_external_attribute_names_keep_exact_native_export_bytes(self):
        for kind, suffix in (('space', '\u2003'), ('line-separator', '\u2028')):
            with self.subTest(kind=kind):
                repo = Repository(self, 'git', kind)
                old = repo.commit({'a.txt': b'OLD\n'})
                new = repo.commit({'a.txt': b'NEW\n'})
                attributes = self.root / (kind + '_attributes' + suffix)
                attributes.write_bytes(b'*.txt text eol=crlf\n')
                self.root.joinpath(kind + '_attributes').write_bytes(b'*.txt text eol=lf\n')
                repo.cmd('config', 'core.attributesfile', str(attributes))
                with self.subTest(attributes=attributes.name):
                    expected = {version: subprocess.run([repo.executable, 'cat-file', '--filters',
                        '--path=a.txt', version + ':a.txt'], cwd=repo.path, capture_output=True,
                        check=True).stdout for version in (old, new)}
                    self.assertEqual(b'NEW\r\n', expected[new])
                    for multi in (False, True):
                        app, result, out = self.generate(repo.task(new if multi else old, new, multi=multi),
                            False, self.root / (kind + '-output-' + str(multi)))
                        self.assert_success(app); self.assertFalse(result.issues)
                        self.assertEqual(expected[old], (out/'oldVersion/Demo/a.txt').read_bytes())
                        self.assertEqual(expected[new], (out/'newVersion/Demo/a.txt').read_bytes())
                        self.assert_no_stages(out)


class StageCleanupIdentityTests(WorkflowCase):
    def test_single_and_multi_failed_publish_keep_foreign_replacement(self):
        old, new = self.root/'old', self.root/'new'
        old.mkdir(); new.mkdir()
        (old/'a.txt').write_bytes(b'old'); (new/'a.txt').write_bytes(b'new')
        task = dict(vcs_type='folder', project_path='', project_name='Demo',
                    old_version=str(old), new_version=str(new), exclude_rules='')
        original_rename = os.rename
        for multi in (False, True):
            with self.subTest(multi=multi):
                out = self.root / ('multi' if multi else 'single'); out.mkdir()
                report = str(out/'report.html')
                foreign = self.root / ('external-' + str(multi))
                foreign.write_bytes(b'EXTERNAL_REPLACEMENT_MUST_SURVIVE')
                identity = foreign.stat().st_ino
                stages = []
                def rename(source, target):
                    if target == report:
                        stages.append(source)
                        error = PermissionError('synthetic sharing violation'); error.winerror = 32
                        raise error
                    return original_rename(source, target)
                def replace(_delay):
                    os.replace(foreign, stages[-1])
                app = self.app()
                with mock.patch('file_exporter.os.rename', side_effect=rename), \
                        mock.patch('file_exporter.time.sleep', side_effect=replace):
                    if multi:
                        app._do_generate_multi([task], report, str(out/'oldVersion'),
                                               str(out/'newVersion'), str(out))
                    else:
                        app._do_generate('', 'folder', str(old), str(new), 'Demo', [],
                            True, True, report, str(out/'oldVersion'), str(out/'newVersion'), str(out))
                self.assertEqual('failed', app._last_task_record['status'])
                self.assertIn('暂存项已被替换', app._show_error.call_args.args[0])
                preserved = Path(stages[-1])
                self.assertEqual(identity, preserved.stat().st_ino)
                self.assertEqual(b'EXTERNAL_REPLACEMENT_MUST_SURVIVE', preserved.read_bytes())
                self.assertFalse(Path(report).exists())
                self.assertEqual(b'old', (old/'a.txt').read_bytes())
                self.assertEqual(b'new', (new/'a.txt').read_bytes())

    def test_replaced_child_or_parent_is_preserved_and_unowned_names_never_authorize_cleanup(self):
        out = self.root/'output'; out.mkdir()
        stage = Path(FileExporter._make_stage_dir(str(out/'target'),
                     stage_parent=str(out), trusted_root=str(out)))
        stage.rename(self.root/'original-child')
        stage.mkdir(); (stage/'foreign.txt').write_bytes(b'child must survive')
        FileExporter._cleanup_stage(str(stage))
        self.assertEqual(b'child must survive', (stage/'foreign.txt').read_bytes())
        stage_file = Path(FileExporter._make_stage_file(str(out/'report.html'), str(out),
                                                       '.comparetool_report_', '.html'))
        out.rename(self.root/'original-output')
        out.mkdir(); foreign = out/stage_file.name; foreign.write_bytes(b'parent must survive')
        FileExporter._cleanup_stage(str(stage_file))
        self.assertEqual(b'parent must survive', foreign.read_bytes())
        unowned = out/'.comparetool_stage_unowned'; unowned.mkdir()
        (unowned/'keep.txt').write_bytes(b'unowned must survive')
        FileExporter._cleanup_stage(str(unowned))
        self.assertEqual(b'unowned must survive', (unowned/'keep.txt').read_bytes())

    def test_successful_atomic_report_and_directory_cleanup_leave_only_delivery(self):
        old, new = self.root/'before', self.root/'after'
        old.mkdir(); new.mkdir()
        (new/'a.txt').write_bytes(b'NORMAL_COMPLETE_REPORT\n')
        task = dict(vcs_type='folder', project_path='', project_name='Demo',
                    old_version=str(old), new_version=str(new), exclude_rules='')
        app, result, out = self.generate(task, False)
        self.assert_success(app); self.assertFalse(result.issues)
        self.assertIn('NORMAL_COMPLETE_REPORT', (out/'report.html').read_text('utf-8'))
        self.assertEqual(['.comparetool_output.lock'], sorted(p.name for p in out.glob('.comparetool*')))
