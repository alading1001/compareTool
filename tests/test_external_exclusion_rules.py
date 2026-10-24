"""外部规则的真实窗口、六模式交付、跨批次更新与失败保护。"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
from pathlib import Path
import shutil
from unittest import mock
import unittest
import zipfile

import main
from exclusion_rules import freeze_tasks, manual_snapshot, read_rule_file
from report_generator import ReportGenerator
from archive_workflow_fixtures import Repository, WorkflowCase


class ExternalRulesTests(WorkflowCase):
    def file_source(self, text="*.log\n", *, follow=True, encoding="utf-8-sig", name="paichu.txt"):
        path = self.root / name
        path.write_bytes(text.encode(encoding))
        return dict(kind="file", path=str(path), encoding=encoding, follow=follow)

    def folder_task(self, name="Demo"):
        old, new = self.root / name / "old", self.root / name / "new"
        for directory, prefix in ((old, b"old"), (new, b"new")):
            for path in ("src/a.txt", "logs/a.log", "WEB-INF/A.class", "bin/start.sh"):
                target = directory / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(prefix + path.encode())
        return dict(vcs_type="folder", project_name=name, project_path="", old_version=str(old),
                    new_version=str(new), exclude_rules="", show_project_root=True)

    @contextmanager
    def window(self, config):
        path = self.root / "config.json"
        if config is not None:
            path.write_text(json.dumps(config), encoding="utf-8")
        with ExitStack() as stack:
            for key, value in (("CONFIG_DIR", str(self.root)), ("CONFIG_FILE", str(path)), ("_CONFIG_LOAD_FAILURE", None)):
                stack.enter_context(mock.patch.object(main, key, value))
            stack.enter_context(mock.patch("main.messagebox.showerror"))
            app = main.CompareToolApp()
            app.root.withdraw()
            try:
                yield app
            finally:
                app._end_task_progress()
                for token in app.root.tk.call("after", "info"):
                    app.root.after_cancel(token)
                app.root.destroy()

    def test_file_encoding_comments_line_numbers_and_legacy_literal_hash(self):
        for encoding in ("utf-8-sig", "gb18030"):
            source = self.file_source("# 日志\n\n*.log\n目录/**\n", encoding=encoding)
            snapshot = read_rule_file(source)
            self.assertEqual(((3, "*.log"),), snapshot.matches("deep/a.log"))
            self.assertEqual(((4, "目录/**"),), snapshot.matches("目录/a.txt"))
            self.assertFalse(snapshot.matches("deep/A.LOG"))
            self.assertEqual(hashlib.sha256(Path(source['path']).read_bytes()).hexdigest(), snapshot.sha256)
        self.assertEqual(((1, "#file"),), manual_snapshot("#file").matches("#file"))
        with self.assertRaises(ValueError):
            snapshot.matches("D:/project/a.log")
        Path(source['path']).write_bytes(b"\x00")
        with self.assertRaisesRegex(ValueError, "NUL"):
            read_rule_file(source)

    def test_batch_reads_shared_file_once_freezes_before_first_project_and_keeps_saved_tasks(self):
        source = self.file_source()
        tasks = [dict(project_name=name, exclude_source=source, exclude_rules="old") for name in ("A", "B")]
        with mock.patch("exclusion_rules.read_rule_file", wraps=read_rule_file) as read:
            frozen = freeze_tasks(tasks)
            self.assertEqual(1, read.call_count)
        Path(source['path']).write_text("*.txt", encoding="utf-8")
        self.assertEqual(["*.log", "*.log"], [t["exclude_rules"] for t in freeze_tasks(frozen)])
        self.assertEqual(["old", "old"], [t["exclude_rules"] for t in tasks])
        self.assertEqual("*.txt", freeze_tasks(tasks)[0]["exclude_rules"])
        Path(source['path']).unlink()
        with mock.patch('exclusion_rules.read_rule_file', wraps=read_rule_file) as read:
            failed = freeze_tasks(tasks, best_effort=True)
        self.assertEqual(1, read.call_count)
        self.assertTrue(all(task.get('_exclude_error') for task in failed))
        self.assertTrue(all(task['exclude_snapshot'] == {} for task in failed))

    def test_fixed_snapshot_survives_missing_file_and_follow_failure_is_partial_without_fallback(self):
        task = self.folder_task()
        source = self.file_source(follow=False)
        task.update(exclude_source=source, exclude_snapshot=read_rule_file(source).to_dict())
        Path(source['path']).unlink()
        frozen = freeze_tasks([task])[0]
        self.assertEqual("*.log", frozen["exclude_rules"])
        source["follow"] = True
        with self.assertRaisesRegex(ValueError, '排除规则无效'):
            freeze_tasks([task])
        output = self.root / "protected-output"
        output.mkdir()
        report = output / "report.html"
        report.write_bytes(b"previous report")
        app = self.app()
        independent = self.folder_task('Independent')
        app._do_generate_multi([task, independent], str(report), str(output / "oldVersion"), str(output / "newVersion"), str(output))
        self.assert_success(app)
        html = report.read_text('utf-8')
        self.assertIn('排除规则', html)
        self.assertIn('无法读取', html)
        self.assertFalse((output / 'newVersion/Demo/src/a.txt').exists())
        self.assertTrue((output / 'newVersion/Independent/src/a.txt').exists())

    def test_window_reuses_file_across_release_paths_and_preserves_binding_on_restart(self):
        task = self.folder_task()
        source = self.file_source("# 只排日志\n*.log\n")
        config = dict(vcs_type="folder", output_dir=str(self.root / "output"),
                      last_comparison_paths={"folder": {k: task[k] for k in ("old_version", "new_version")}})
        with self.window(config) as app:
            app._link_rule_file(source["path"])
            self.assertEqual("disabled", app.exclude_text.cget("state"))
            self.assertEqual(("*.log",), app._current_rule_snapshot().patterns)
            app._save_current_config()
            release = self.root / "release_next"
            release.mkdir()
            app.new_version_var.set(str(release))
            app._refresh_output_paths_now()
            # 新路径不猜项目身份；通过最近文件一次选择复用。
            app.exclude_file_var.set(source["path"])
            app._reuse_rule_file()
            app.project_name_var.set("Demo")
            saved = app._task_from_current_form()
            self.assertEqual(source["path"], saved["exclude_source"]["path"])
            self.assertTrue(saved["exclude_source"]["follow"])
            self.assertEqual("*.log", saved["exclude_rules"])
            self.assertTrue(app._save_current_config())
        with self.window(None) as app:
            self.assertEqual("file", app.exclude_source_var.get())
            self.assertEqual(source["path"], app.exclude_file_var.get())
            self.assertEqual(1, len(app._recent_exclude_files))
            app.exclude_test_var.set("logs/a.log")
            with mock.patch("exclusion_ui.messagebox.showinfo") as show:
                app._test_rule_path()
            self.assertIn("第 2 行", show.call_args.args[1])
            self.assertIn("结果：排除", show.call_args.args[1])
            app.exclude_template_var.set("部署包比较")
            app._apply_rule_template()
            self.assertFalse(app._current_rule_snapshot().matches("WEB-INF/A.class"))
            self.assertFalse(app._current_rule_snapshot().matches("bin/start.sh"))

    def test_single_gui_freezes_current_file_then_uses_same_bytes_for_report_and_export(self):
        task = self.folder_task()
        source = self.file_source()
        output = self.root / "single"
        config = dict(vcs_type="folder", output_dir=str(output),
                      last_comparison_paths={"folder": {k: task[k] for k in ("old_version", "new_version")}})
        with self.window(config) as app:
            app.project_name_var.set("Demo")
            app._link_rule_file(source["path"])
            # 预览后修改文件；点击生成必须读取最新内容。
            Path(source['path']).write_text("*.txt", encoding="utf-8")
            dispatch = []
            class Thread:
                def __init__(self, *, target, args, kwargs, daemon):
                    dispatch.append((target, args, kwargs))
                def start(self):
                    pass
            with mock.patch.object(app, "_confirm_output_batch", return_value=True), mock.patch('main.threading.Thread', Thread):
                app._generate()
            self.assertEqual(1, len(dispatch))
            _target, args, kwargs = dispatch[0]
            self.assertEqual(["*.txt"], args[5])
            Path(source['path']).write_text("*", encoding="utf-8")
            worker = self.app()
            worker._do_generate(*args, **kwargs)
            self.assert_success(worker)
            new_root = Path(args[10]) / "Demo"
            self.assertFalse((new_root / "src/a.txt").exists())
            self.assertEqual(b"newWEB-INF/A.class", (new_root / "WEB-INF/A.class").read_bytes())
            html = Path(args[8]).read_text("utf-8")
            self.assertIn(kwargs['exclude_snapshot']['sha256'], html)
            self.assertIn("*.txt", html)
            self.assertNotIn("<pre style=\"white-space:pre-wrap;overflow-wrap:anywhere\">*</pre>", html)

    def test_multirun_uses_one_snapshot_across_projects_and_next_run_refreshes(self):
        source = self.file_source()
        tasks = [self.folder_task(name) for name in ("One", "Two")]
        for task in tasks:
            task["exclude_source"] = source
        for run, rule, missing in ((1, "*.log", "logs/a.log"), (2, "*.txt", "src/a.txt")):
            Path(source['path']).write_text(rule, encoding="utf-8")
            output = self.root / f"multi{run}"
            output.mkdir()
            app = self.app()
            original = app._prepare_task_result
            def prepare(task, *args, **kwargs):
                Path(source['path']).write_text("*", encoding="utf-8")
                return original(task, *args, **kwargs)
            with mock.patch.object(app, "_prepare_task_result", prepare):
                app._do_generate_multi(tasks, str(output / "report.html"), str(output / "oldVersion"),
                                       str(output / "newVersion"), str(output))
            self.assert_success(app)
            for task in tasks:
                new = output / "newVersion" / task['project_name']
                self.assertFalse((new / missing).exists())
                self.assertTrue((new / "WEB-INF/A.class").exists())
            html = (output / 'report.html').read_text('utf-8')
            self.assertIn(hashlib.sha256(rule.encode()).hexdigest(), html)

    def test_external_file_cannot_be_overwritten_as_a_report_target(self):
        task = self.folder_task()
        output = self.root / "output"
        output.mkdir()
        path = output / "report.html"
        path.write_bytes(b"*.log")
        source = dict(kind="file", path=str(path), encoding="utf-8-sig", follow=True)
        snapshot = read_rule_file(source)
        worker = self.app()
        worker._do_generate('', 'folder', task['old_version'], task['new_version'], 'Demo',
                            list(snapshot.patterns), True, True, str(path), str(output/'oldVersion'),
                            str(output/'newVersion'), str(output), exclude_snapshot=snapshot.to_dict())
        self.assertTrue(worker._show_error.called)
        self.assertEqual(b"*.log", path.read_bytes())

    def test_legacy_task_normalization_stays_manual_and_bad_binding_does_not_disable_filtering(self):
        app = self.app()
        task = self.folder_task()
        task['exclude_rules'] = '*.log'
        normalized = app._normalize_loaded_multi_tasks([task])
        self.assertEqual({'kind': 'manual'}, normalized[0]['exclude_source'])
        for source in (None, {'kind': 'unknown'}, {'kind': 'file', 'path': '', 'follow': False}):
            with self.subTest(source=source), mock.patch('main.warn') as warning:
                self.assertEqual([], app._normalize_loaded_multi_tasks([dict(task, exclude_source=source)]))
                self.assertTrue(warning.called)

    def test_missing_file_or_wrong_encoding_blocks_gui_instead_of_using_saved_preview(self):
        task = self.folder_task()
        source = self.file_source()
        config = dict(vcs_type="folder", output_dir=str(self.root / 'output'),
                      last_comparison_paths={'folder': {k: task[k] for k in ('old_version', 'new_version')}})
        with self.window(config) as app:
            app._link_rule_file(source['path'])
            app.project_name_var.set('Demo')
            for invalid in ('missing', 'encoding'):
                if invalid == 'missing':
                    Path(source['path']).unlink()
                else:
                    Path(source['path']).write_bytes(b'\xff\xfe')
                with mock.patch('main.threading.Thread') as thread, mock.patch('main.messagebox.showerror') as error:
                    app._generate()
                thread.assert_not_called()
                self.assertIn('排除规则', error.call_args.args[0])

    def test_saved_fixed_task_edit_and_path_test_keep_snapshot_when_file_is_missing(self):
        task = self.folder_task()
        source = self.file_source()
        config = dict(vcs_type='folder', last_comparison_paths={'folder': {
            k: task[k] for k in ('old_version', 'new_version')}})
        with self.window(config) as app:
            app.project_name_var.set('Demo')
            app._link_rule_file(source['path'])
            app.exclude_follow_var.set(False)
            app._multi_tasks = [app._task_from_current_form()]
            self.assertTrue(app._save_current_config())
        Path(source['path']).unlink()
        with self.window(None) as app:
            app.multi_task_tree.selection_set('0')
            app._edit_multi_task()
            app.exclude_test_var.set('logs/a.log')
            with mock.patch('exclusion_ui.messagebox.showinfo') as show:
                app._test_rule_path()
            self.assertIn('结果：排除', show.call_args.args[1])
            updated = app._task_from_current_form()
            self.assertFalse(updated['exclude_source']['follow'])
            self.assertEqual('*.log', updated['exclude_rules'])
            self.assertEqual('*.log', freeze_tasks([updated])[0]['exclude_rules'])

    def test_corrupt_project_binding_blocks_generation_until_explicitly_reselected(self):
        task = self.folder_task()
        key = main.CompareToolApp._normalize_project_path(task['new_version'])
        config = dict(vcs_type='folder', last_comparison_paths={'folder': {
            k: task[k] for k in ('old_version','new_version')}},
            project_exclude_rules={key: '*.log'}, project_exclude_sources={key: None})
        with mock.patch('main.warn'), mock.patch('exclusion_ui.warn'), self.window(config) as app:
            self.assertEqual('invalid', app.exclude_source_var.get())
            with self.assertRaisesRegex(ValueError, '来源损坏'):
                app._current_rule_snapshot()
            app.exclude_template_var.set('空白规则')
            app._apply_rule_template()
            self.assertEqual((), app._current_rule_snapshot().patterns)

    def test_corrupt_source_map_cannot_turn_into_manual_defaults_or_destroy_saved_rules(self):
        task = self.folder_task()
        key = main.CompareToolApp._normalize_project_path(task['new_version'])
        for invalid in (None, [], 'damaged'):
            config = dict(vcs_type='folder', last_comparison_paths={'folder': {
                k: task[k] for k in ('old_version', 'new_version')}},
                project_exclude_rules={key: '*.log'}, project_exclude_sources=invalid)
            with self.subTest(value=invalid), mock.patch('exclusion_ui.warn'), self.window(config) as app:
                self.assertEqual('invalid', app.exclude_source_var.get())
                with self.assertRaises(ValueError):
                    app._current_rule_snapshot()
                self.assertTrue(app._save_current_config())
                saved = json.loads((self.root/'config.json').read_text('utf-8'))
                self.assertEqual('*.log', saved['project_exclude_rules'][key])
                self.assertEqual('invalid', saved['project_exclude_sources'][key]['kind'])

    def test_recursive_report_uses_frozen_external_rules_and_exports_original_archive(self):
        import io
        task = self.folder_task()
        source = self.file_source()
        payloads = {}
        for key, side in (('old_version', b'old'), ('new_version', b'new')):
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as archive:
                archive.writestr('keep.txt', side)
                archive.writestr('skip.log', side)
            payloads[key] = data.getvalue()
            (Path(task[key])/'app.jar').write_bytes(data.getvalue())
        frozen = freeze_tasks([dict(task, exclude_source=source)])[0]
        Path(source['path']).write_text('*', encoding='utf-8')
        app = self.app()
        out = self.root/'recursive'
        out.mkdir()
        app._do_generate('', 'folder', task['old_version'], task['new_version'], 'Demo',
                         frozen['exclude_rules'].splitlines(), True, True, str(out/'report.html'),
                         str(out/'oldVersion'), str(out/'newVersion'), str(out), recursive_archives=True,
                         exclude_snapshot=frozen['exclude_snapshot'])
        self.assert_success(app)
        self.assertEqual(payloads['new_version'], (out/'newVersion/Demo/app.jar').read_bytes())
        html = (out/'report.html').read_text('utf-8')
        self.assertIn('keep.txt', html)
        self.assertNotIn('skip.log', html)

    def test_report_snapshot_remains_visible_and_escaped_in_normal_and_fallback_reports(self):
        from diff_engine import DiffResult
        snapshot = manual_snapshot('<script>alert(1)</script>\n*.log')
        result = DiffResult('', 'Demo', 'folder', 'old', 'new', exclude_snapshot=snapshot.to_dict())
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                output = self.root / ('fallback.html' if fallback else 'normal.html')
                generator = ReportGenerator(str(self.root/'missing-templates') if fallback else None, best_effort=True)
                generator.generate(result, str(output))
                html = output.read_text('utf-8')
                self.assertIn(snapshot.sha256, html)
                self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
                self.assertNotIn('<script>alert(1)</script>', html)

    def test_external_rules_feed_all_six_vcs_modes(self):
        source = self.file_source("*.log\n")
        tasks = [self.folder_task()]
        archives = []
        for side, prefix in (("old", b"old"), ("new", b"new")):
            path = self.root / (side + '.zip')
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('keep.txt', prefix)
                archive.writestr('skip.log', prefix)
            archives.append(str(path))
        tasks.append(dict(vcs_type='archive', project_path='', project_name='Archive',
                          old_version=archives[0], new_version=archives[1], exclude_rules=''))
        for kind in ('git', 'svn'):
            if not shutil.which(kind) or (kind == 'svn' and not shutil.which('svnadmin')):
                self.skipTest(kind + ' unavailable')
            repo = Repository(self, kind, kind)
            old = repo.commit({'keep.txt': b'old', 'skip.log': b'old'})
            new = repo.commit({'keep.txt': b'new', 'skip.log': b'new'})
            tasks.extend((repo.task(old, new, project_name=kind),
                          repo.task(new, '', multi=True, project_name=kind + '_multi')))
        for index, original in enumerate(tasks):
            with self.subTest(mode=original['vcs_type']):
                task = freeze_tasks([dict(original, exclude_source=source)])[0]
                worker = self.app()
                out = self.root / f'six{index}'
                out.mkdir()
                worker._do_generate(task['project_path'], task['vcs_type'], task['old_version'],
                    task['new_version'], task['project_name'], task['exclude_rules'].splitlines(), True, True,
                    str(out/'report.html'), str(out/'oldVersion'), str(out/'newVersion'), str(out),
                    exclude_snapshot=task['exclude_snapshot'])
                self.assert_success(worker)
                exported = [p for p in (out/'newVersion').rglob('*') if p.is_file()]
                self.assertTrue(exported)
                self.assertFalse(any(p.suffix == '.log' for p in exported))


if __name__ == '__main__':
    unittest.main()
