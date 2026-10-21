"""Restart real GUI windows against saved project-name configuration."""
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
from unittest import mock

import main
from archive_workflow_fixtures import WorkflowCase


class RecentProjectNamePersistenceTests(WorkflowCase):
    @contextmanager
    def window(self, config=None):
        config_path = self.root / 'config.json'
        if config is not None:
            config_path.write_text(json.dumps(config), encoding='utf-8')
        with ExitStack() as stack:
            stack.enter_context(mock.patch('main.CONFIG_DIR', str(self.root)))
            stack.enter_context(mock.patch('main.CONFIG_FILE', str(config_path)))
            stack.enter_context(mock.patch('main._CONFIG_LOAD_FAILURE', None))
            stack.enter_context(mock.patch('main.messagebox.showerror'))
            app = main.CompareToolApp()
            app.root.withdraw()
            try:
                yield app
            finally:
                for token in app.root.tk.call('after', 'info'):
                    app.root.after_cancel(token)
                app.root.destroy()

    def saved_config(self):
        return json.loads((self.root / 'config.json').read_text('utf-8'))

    def test_custom_name_survives_restart_refresh_and_resave_in_all_repository_modes(self):
        for kind in ('git', 'git_multi', 'svn', 'svn_multi'):
            with self.subTest(mode=kind):
                project = self.root / kind / 'backend'
                project.mkdir(parents=True)
                custom_name = kind + '_业务交付项目'
                config = dict(vcs_type=kind, project_path=str(project),
                              output_dir=str(self.root / 'output'))
                with self.window(config) as app:
                    app.project_name_var.set(custom_name)
                    app._project_name_manual = True
                    self.assertTrue(app._save_current_config())
                self.assertEqual(custom_name, self.saved_config()['recent_projects'][0]['project_name'])

                for restart in range(2):
                    with self.subTest(restart=restart), self.window() as app:
                        # A pending output refresh must also preserve the restored name.
                        app._refresh_output_paths_now()
                        self.assertEqual(custom_name, app.project_name_var.get())
                        self.assertEqual(custom_name, app._recent_projects[0]['project_name'])
                        choices = list(app.project_name_entry['values'])
                        self.assertEqual(1, len(choices))
                        self.assertEqual(custom_name,
                                         app._recent_project_value_map[choices[0]]['project_name'])
                        self.assertEqual(custom_name + '_diff.html',
                                         Path(app.report_path_var.get()).name)
                        self.assertTrue(app._save_current_config())
                    self.assertEqual(custom_name,
                                     self.saved_config()['recent_projects'][0]['project_name'])

    def test_startup_matches_family_and_normalized_project_path(self):
        project, other = self.root / 'backend', self.root / 'other'
        project.mkdir()
        other.mkdir()
        entries = [
            dict(vcs_family='svn', project_path=str(project), project_name='SVN项目'),
            dict(vcs_family='git', project_path=str(other), project_name='其他Git项目'),
            dict(vcs_family='git', project_path=str(project) + '/.', project_name='当前Git项目'),
        ]
        with self.window(dict(vcs_type='git_multi', project_path=str(project),
                              recent_projects=entries)) as app:
            app._refresh_output_paths_now()
            self.assertEqual('当前Git项目', app.project_name_var.get())
            actual = {(item['vcs_family'], app._normalize_project_path(item['project_path'])):
                      item['project_name'] for item in app._recent_projects}
            expected = {(item['vcs_family'], app._normalize_project_path(item['project_path'])):
                        item['project_name'] for item in entries}
            self.assertEqual(expected, actual)

    def test_legacy_configuration_without_recent_projects_keeps_directory_default(self):
        project = self.root / 'backend'
        project.mkdir()
        for kind in ('git', 'git_multi', 'svn', 'svn_multi'):
            with self.subTest(mode=kind), self.window(
                    dict(vcs_type=kind, project_path=str(project))) as app:
                app._refresh_output_paths_now()
                self.assertEqual('backend', app.project_name_var.get())
                self.assertEqual('backend', app._recent_projects[0]['project_name'])
                self.assertEqual(app._vcs_family(kind), app._recent_projects[0]['vcs_family'])
                self.assertTrue(app._save_current_config())


if __name__ == '__main__':
    import unittest
    unittest.main()
