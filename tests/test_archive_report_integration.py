import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from archive_report import ArchiveReportBudget
from main import CompareToolApp
from report_generator import ReportGenerator
import test_archive_report as fixtures
from test_archive_report import zip_bytes


class ArchiveIntegrationTests(unittest.TestCase):
    setUp = fixtures.ArchiveReportTests.setUp
    source = fixtures.ArchiveReportTests.source
    enrich = fixtures.ArchiveReportTests.enrich

    def app(self):
        app = CompareToolApp.__new__(CompareToolApp)
        app.root = mock.Mock()
        app._task_log_dir = self.root/'jobs'
        return app

    def task(self, enabled=True):
        return dict(project_name='Demo', vcs_type='folder', project_path='',
            old_version=str(self.old), new_version=str(self.new), exclude_rules='',
            show_full_context=True, show_project_root=True, recursive_archives=enabled)

    def test_old_config_defaults_off_and_git_can_enable(self):
        app = self.app()
        task = self.task(); task.pop('recursive_archives')
        self.assertFalse(app._normalize_loaded_multi_tasks([task])[0]['recursive_archives'])
        task.update(vcs_type='git', project_path=str(self.new), recursive_archives=True)
        self.assertTrue(app._normalize_loaded_multi_tasks([task])[0]['recursive_archives'])

    def test_display_option_roundtrip_and_default(self):
        app = self.app()
        app.show_project_root_var = mock.Mock(); app.show_full_context_var = mock.Mock()
        app.ignore_archive_root_var = mock.Mock(); app.recursive_archives_var = mock.Mock()
        app._apply_display_options({})
        app.recursive_archives_var.set.assert_called_with(False)
        app._apply_display_options({'recursive_archives': True})
        app.recursive_archives_var.set.assert_called_with(True)
        app.recursive_archives_var.get.return_value = True
        self.assertTrue(app._current_display_options()['recursive_archives'])
        app.vcs_var = mock.Mock(); app.vcs_var.get.return_value = 'folder'
        self.assertTrue(app._recursive_archives())
        app.vcs_var.get.return_value = 'svn'
        self.assertTrue(app._recursive_archives())

    def test_multi_task_uses_own_flag_and_shares_budget(self):
        self.source({'a.zip': zip_bytes({'a.txt': b'old'})},
                    {'a.zip': zip_bytes({'a.txt': b'new'})})
        app = self.app()
        tasks = [dict(self.task(flag), project_name='Demo' + str(index))
                 for index, flag in enumerate((False, True, True))]
        out = self.root/'multi'; out.mkdir()
        captured = []
        generate = ReportGenerator.generate_multi
        def observe(generator, results, path):
            captured.extend(results)
            return generate(generator, results, path)
        with mock.patch('main.ArchiveReportBudget', side_effect=lambda: ArchiveReportBudget(max_members=3)), \
                mock.patch.object(ReportGenerator, 'generate_multi', observe):
            app._do_generate_multi(tasks[:2], str(out/'multi.html'),
                str(out/'old'), str(out/'new'), str(out))
            self.assertTrue(app._last_task_record['success'])
            self.assertIsNone(captured[0]['diff_result'].files[0].archive_details)
            self.assertIsNotNone(captured[1]['diff_result'].files[0].archive_details)
            before = (out/'multi.html').read_bytes()
            app._do_generate_multi(tasks, str(out/'multi.html'),
                str(out/'old'), str(out/'new'), str(out))
            self.assertFalse(app._last_task_record['success'])
            self.assertFalse((out/'multi.html').exists())
        html = before.decode('utf-8')
        self.assertIn('archive-member-template', html)
        self.assertIn('不单独交付', html)

    def test_main_export_toggle_preserves_bytes_and_instructions(self):
        a = zip_bytes({'removed.txt': b'old', 'keep.xml': b'old'})
        b = zip_bytes({'added.txt': b'new', 'keep.xml': b'new'})
        self.source({'a.zip': a}, {'a.zip': b})
        app = self.app(); out = self.root/'out'; out.mkdir()
        original = None
        for enabled in (False, True):
            app._do_generate('', 'folder', str(self.old), str(self.new), 'Demo', [],
                True, True, str(out/'report.html'), str(out/'oldVersion'),
                str(out/'newVersion'), str(out), recursive_archives=enabled)
            self.assertTrue(app._last_task_record['success'], app._last_task_record)
            self.assertEqual(1, app._last_task_record['counts']['total_files'])
            files = [p for p in out.rglob('*') if p.is_file() and p.suffix != '.html'
                     and not p.name.startswith('.comparetool')]
            data = {p.relative_to(out).as_posix(): p.read_bytes() for p in files}
            if original is None: original = data
            else: self.assertEqual(original, data)
            html = (out/'report.html').read_text(encoding='utf-8')
            self.assertEqual(enabled, 'removed.txt' in html)
        self.assertEqual(b, (out/'newVersion/Demo/a.zip').read_bytes())
        instructions = next(out.glob('*_上线操作说明.txt')).read_text(encoding='utf-8')
        self.assertNotIn('removed.txt', instructions)

    def test_nested_failure_does_not_restore_previous_delivery(self):
        self.source({'a.zip': zip_bytes({'a.txt': b'old'})},
                    {'a.zip': zip_bytes({'a.txt': b'new'})})
        app = self.app(); out = self.root/'out'; out.mkdir()
        def run():
            app._do_generate('', 'folder', str(self.old), str(self.new), 'Demo', [],
                True, True, str(out/'report.html'), str(out/'oldVersion'),
                str(out/'newVersion'), str(out), recursive_archives=True)
        run(); self.assertTrue(app._last_task_record['success'])
        before = (out/'report.html').read_bytes(), (out/'newVersion/Demo/a.zip').read_bytes()
        (self.new/'a.zip').write_bytes(b'broken archive')
        run(); self.assertFalse(app._last_task_record['success'])
        self.assertFalse((out/'report.html').exists())
        self.assertFalse((out/'newVersion/Demo').exists())

    def test_html_source_text_is_escaped(self):
        vcs, result = self.source({}, {'a.zip': zip_bytes({'a.txt': b'<script>bad()</script>'})})
        self.enrich(vcs, result)
        ReportGenerator().generate(result, str(self.root/'r.html'))
        html = (self.root/'r.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>bad()</script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_unchanged_top_archive_keeps_no_detail(self):
        data = zip_bytes({'a.txt': b'unchanged'})
        vcs, result = self.source({'a.zip': data}, {'a.zip': data})
        with mock.patch('archive_report._Inspector.inspect', side_effect=AssertionError('opened')):
            self.enrich(vcs, result)
        self.assertFalse(result.files)


    def test_gui_checkbox_snapshot_and_task_roundtrip(self):
        self.source({"a.zip": zip_bytes({"a.txt": b"old"})},
                    {"a.zip": zip_bytes({"a.txt": b"new"})})
        with mock.patch("main._load_config", return_value={}), \
                mock.patch("main._CONFIG_LOAD_FAILURE", None), \
                mock.patch("main.CONFIG_DIR", str(self.root)), \
                mock.patch("main.CONFIG_FILE", str(self.root/"config.json")):
            app = CompareToolApp()
            try:
                app.root.withdraw(); app.vcs_var.set("folder")
                app.old_version_var.set(str(self.old)); app.new_version_var.set(str(self.new))
                app._do_update_output_paths(); app.recursive_archives_var.set(True)
                app._add_or_update_multi_task()
                self.assertTrue(app._multi_tasks[0]["recursive_archives"])
                app.recursive_archives_var.set(False)
                app.multi_task_tree.selection_set("0"); app._edit_multi_task()
                self.assertTrue(app.recursive_archives_var.get()); app._cancel_edit_task()
                app.output_dir_var.set(str(self.root/"out")); app._do_update_output_paths()
                with mock.patch("main.messagebox.askyesno", return_value=True), mock.patch("main.threading.Thread") as thread:
                    app._generate()
                    self.assertTrue(thread.call_args.kwargs["kwargs"]["recursive_archives"])
                app._set_generating(False); app.vcs_var.set("git")
                self.assertFalse(app.recursive_archives_check.instate(["disabled"]))
            finally:
                app.root.destroy()


if __name__ == "__main__":
    unittest.main()
