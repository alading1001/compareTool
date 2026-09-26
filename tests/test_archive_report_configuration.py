"""Six-mode option persistence and actual GUI worker scheduling."""
from contextlib import ExitStack
import json
import time
from unittest import mock

import main
from main import CompareToolApp, SUPPORTED_VCS_TYPES
from archive_workflow_fixtures import WorkflowCase, Repository, package


class ArchiveConfigurationTests(WorkflowCase):
    def test_six_type_legacy_boolean_and_invalid_schema(self):
        app = self.app()
        for kind in SUPPORTED_VCS_TYPES:
            task = dict(vcs_type=kind, project_name='Demo', project_path=str(self.root),
                        old_version='1', new_version='2')
            self.assertFalse(app._normalize_loaded_multi_tasks([task])[0]['recursive_archives'])
            for value, expected in ((True, True), (False, False), ('true', True),
                                    ('false', False), ('yes', True), ('0', False)):
                normalized = app._normalize_loaded_multi_tasks([dict(task, recursive_archives=value)])
                self.assertEqual(expected, normalized[0]['recursive_archives'])
        self.assertEqual([], app._normalize_loaded_multi_tasks([dict(task, vcs_type='invalid')]))
        self.assertEqual([], app._normalize_loaded_multi_tasks([None, {}, 'bad']))

    def isolated_gui(self, stack, configuration):
        stack.enter_context(mock.patch('main.CONFIG_DIR', str(self.root)))
        stack.enter_context(mock.patch('main.CONFIG_FILE', str(self.root/'config.json')))
        stack.enter_context(mock.patch('main._CONFIG_LOAD_FAILURE', None))
        stack.enter_context(mock.patch('main._load_config', return_value=configuration))
        stack.enter_context(mock.patch('main.messagebox.askyesno', return_value=False))
        stack.enter_context(mock.patch('main.messagebox.showerror'))
        app = CompareToolApp(); app.root.withdraw()
        def dispose():
            for token in app.root.tk.call('after', 'info'):
                app.root.after_cancel(token)
            app.root.destroy()
        stack.callback(dispose)
        return app

    def test_all_checkboxes_restart_and_recent_source_settings(self):
        a, b = self.root/'A', self.root/'B'; a.mkdir(); b.mkdir()
        config = dict(output_dir=str(self.root/'output'), vcs_type='git')
        with ExitStack() as stack:
            app = self.isolated_gui(stack, config)
            for kind in SUPPORTED_VCS_TYPES:
                app.vcs_var.set(kind)
                self.assertFalse(app.recursive_archives_check.instate(['disabled']))
                app.recursive_archives_var.set(True)
                self.assertTrue(app._recursive_archives())
            app.vcs_var.set('git')
            for path, enabled in ((a, True), (b, False)):
                app.dir_entry.delete(0, 'end'); app.dir_entry.insert(0, str(path))
                app._on_project_path_changed(); app._refresh_output_paths_now()
                app.recursive_archives_var.set(enabled)
                app.project_name_var.set(path.name)
                app._save_current_project_settings_for_current_key()
                app._remember_recent_project()
            chosen = next(k for k, value in app._recent_project_value_map.items()
                          if value['project_path'] == str(a))
            app.old_version_var.set('stale'); app.new_version_var.set('stale')
            app.project_name_var.set(chosen); app._on_recent_project_selected()
            self.assertTrue(app.recursive_archives_var.get())
            self.assertEqual('', app.old_version_var.get())
            self.assertEqual('', app.new_version_var.get())
            self.assertTrue(app._save_current_config())
            saved = json.loads((self.root/'config.json').read_text(encoding='utf-8'))
        with ExitStack() as stack:
            restarted = self.isolated_gui(stack, saved)
            restarted._refresh_output_paths_now()
            self.assertTrue(restarted.recursive_archives_var.get())
            restarted.vcs_var.set('git_multi')
            self.assertEqual('文件级首尾端点', restarted.new_version_var.get())

    def test_real_git_gui_starts_thread_and_preserves_option_snapshot(self):
        repo = Repository(self, 'git', 'gui')
        old = repo.commit({'a.jar': package(b'GUI_OLD')})
        new = repo.commit({'a.jar': package(b'GUI_NEW')})
        with ExitStack() as stack:
            app = self.isolated_gui(stack, dict(vcs_type='git', project_path=str(repo.path),
                                                output_dir=str(self.root/'gui-output')))
            app.old_version_var.set(old); app.new_version_var.set(new)
            app.project_name_var.set('Demo'); app._refresh_output_paths_now()
            app.exclude_text.delete('1.0', 'end'); app.recursive_archives_var.set(True)
            app._confirm_output_batch = lambda *args: True
            app._generate()
            app.recursive_archives_var.set(False)  # Worker must use its snapshot.
            def poll():
                if not app._generating:
                    app.root.quit()
                else:
                    app.root.after(20, poll)
            app.root.after(20, poll)
            deadline = app.root.after(90000, app.root.quit)
            app.root.mainloop()  # Real Tcl event loop permits worker after().
            app.root.after_cancel(deadline)
            self.assertFalse(app._generating, 'GUI worker did not finish')
            self.assertTrue(app._last_task_record['success'], app._last_task_record)
            text = __import__('pathlib').Path(app.report_path_var.get()).read_text(encoding='utf-8')
            self.assertIn('包内审查明细', text)
            self.assertIn('GUI_', text)
