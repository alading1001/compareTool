"""Real Git/SVN and six-mode staged recursive archive acceptance."""
import copy
import json
import os
from pathlib import Path
import shutil
from unittest import mock

import main
from archive_report import enrich_archive_reports
from report_generator import ReportGenerator
from vcs.base import ChangeType
from vcs.git_vcs import GitVCS
from vcs.svn_vcs import SVNVCS
from archive_workflow_fixtures import WorkflowCase, Repository, package, tree_hashes
from test_archive_report import zip_bytes, tar_bytes


class AllVCSArchiveTests(WorkflowCase):
    def repositories(self, name='base'):
        repos = []
        for kind in ('git', 'svn'):
            repo = Repository(self, kind, kind + '_' + name)
            repo.commit({'lib/app.jar': package(b'OLD_ENDPOINT'), 'plain.txt': b'old\n'})
            repo.commit({'lib/app.jar': package(b'NEW_ENDPOINT'), 'plain.txt': b'new\n'})
            repo.write({'lib/app.jar': package(b'WRONG_WORKING_COPY')})
            repos.append(repo)
        return repos

    def six_tasks(self):
        tasks = []
        for repo in self.repositories():
            tasks.append(repo.task(repo.refs[0], repo.refs[1], project_name=repo.kind))
            tasks.append(repo.task(repo.refs[1], '', multi=True, project_name=repo.kind+'_multi'))
        old, new = self.root/'old', self.root/'new'; old.mkdir(); new.mkdir()
        for directory, data in ((old, b'OLD_ENDPOINT'), (new, b'NEW_ENDPOINT')):
            (directory/'app.jar').write_bytes(package(data))
            (directory/'plain.txt').write_bytes(data)
        tasks.append(dict(vcs_type='folder', project_name='folder',
                          old_version=str(old), new_version=str(new)))
        a, b = self.root/'old.zip', self.root/'new.zip'
        a.write_bytes(zip_bytes({'app.jar': package(b'OLD_ENDPOINT')}))
        b.write_bytes(zip_bytes({'app.jar': package(b'NEW_ENDPOINT')}))
        tasks.append(dict(vcs_type='archive', project_name='archive',
                          old_version=str(a), new_version=str(b)))
        return tasks

    def test_six_modes_off_on_identity_and_zero_recursive_repository_reads(self):
        original = main.enrich_archive_reports
        observations = []
        def no_repository_reads(result, *args, **kwargs):
            self.assertIn('endpoints', kwargs)
            with mock.patch.object(GitVCS, 'export_raw_file_to_path', side_effect=AssertionError('extra Git read')), \
                    mock.patch.object(SVNVCS, 'export_raw_file_to_path', side_effect=AssertionError('extra SVN read')), \
                    mock.patch('subprocess.run', side_effect=AssertionError('recursive phase spawned a command')):
                answer = original(result, *args, **kwargs)
            observations.append(result.vcs_type)
            return answer
        for task in self.six_tasks():
            with self.subTest(mode=task['vcs_type']):
                off_app, off, a = self.generate(task, False, self.root/(task['vcs_type']+'_off'))
                self.assert_success(off_app)
                with mock.patch('main.enrich_archive_reports', no_repository_reads):
                    on_app, on, b = self.generate(task, True, self.root/(task['vcs_type']+'_on'))
                self.assert_success(on_app); self.assert_same_delivery(a, b)
                self.assertEqual(off.summary, on.summary)
                manifest = lambda r: [(f.file_path, f.old_path, f.report_type) for f in r.report_manifest_files]
                self.assertEqual(manifest(off), manifest(on))
                text = (b/'report.html').read_text(encoding='utf-8')
                self.assertIn('OLD', text); self.assertIn('NEW', text)
                self.assertNotIn('WRONG', text)
                self.assertTrue(any(f.archive_details for f in on.files))
                self.assertFalse(any(f.archive_details for f in off.files))
                self.assert_no_stages(a); self.assert_no_stages(b)
        self.assertEqual(6, len(observations))

    def test_six_mode_mixed_report_uses_each_snapshot_not_live_checkbox(self):
        tasks = [dict(t, recursive_archives=bool(i % 2))
                 for i, t in enumerate(self.six_tasks())]
        out = self.root/'mixed'; out.mkdir()
        app = self.app(); app.recursive_archives_var = mock.Mock()
        app.recursive_archives_var.get.side_effect = AssertionError('worker read Tk variable')
        captures = []
        original = ReportGenerator.generate_multi
        def capture(generator, results, path):
            captures.append(results)
            return original(generator, results, path)
        with mock.patch.object(ReportGenerator, 'generate_multi', capture):
            app._do_generate_multi([dict(t, recursive_archives=False) for t in tasks],
                str(out/'report.html'), str(out/'oldVersion'), str(out/'newVersion'), str(out))
            self.assert_success(app)
            before = self.delivery(out)
            instructions = (out/'上线操作说明.txt').read_bytes()
            app._do_generate_multi(tasks, str(out/'report.html'), str(out/'oldVersion'),
                                   str(out/'newVersion'), str(out))
            self.assert_success(app)
        self.assertEqual(before, self.delivery(out))
        self.assertEqual(instructions, (out/'上线操作说明.txt').read_bytes())
        self.assertEqual(6, len(captures[-1]))
        for index, item in enumerate(captures[-1]):
            self.assertEqual(bool(index % 2), item['diff_result'].archive_details_enabled)
        for first, second in zip(captures[0], captures[1]):
            self.assertEqual(first['diff_result'].summary, second['diff_result'].summary)
        self.assert_no_stages(out)

    def test_mixed_last_project_failure_never_publishes_partial_result(self):
        from file_exporter import FileExporter
        tasks = [dict(t, recursive_archives=True) for t in self.six_tasks()]
        out = self.root/'failed-mixed'; out.mkdir()
        app = self.app()
        app._do_generate_multi([dict(t, recursive_archives=False) for t in tasks],
            str(out/'report.html'), str(out/'oldVersion'), str(out/'newVersion'), str(out))
        self.assert_success(app); before = tree_hashes(out)
        Path(tasks[-1]['new_version']).write_bytes(zip_bytes({'app.jar': b'broken'}))
        capture = FileExporter.capture_stage_states
        captured = []
        def bind(paths, **options):
            self.assertEqual(2, len(paths))
            self.assertEqual(6, len(list(Path(paths[0]).iterdir())))
            captured.append(paths)
            return capture(paths, **options)
        with mock.patch.object(FileExporter, 'capture_stage_states', bind):
            app._do_generate_multi(tasks, str(out/'report.html'), str(out/'oldVersion'),
                                   str(out/'newVersion'), str(out))
        self.assertEqual(1, len(captured))
        self.assertFalse(app._last_task_record['success'])
        self.assertIn('archive', app._show_error.call_args.args[0])
        self.assertEqual(before, tree_hashes(out)); self.assert_no_stages(out)

    def test_git_multi_unselected_rename_keeps_old_and_new_paths(self):
        repo = Repository(self, 'git', 'multi-rename')
        repo.commit({'a.jar': package(b'OLD_RENAME')})
        first = repo.commit({'a.jar': package(b'MIDDLE_RENAME')})
        repo.cmd('mv', 'a.jar', 'b.jar'); repo.commit()
        last = repo.commit({'b.jar': package(b'NEW_RENAME')})
        app, result, out = self.generate(repo.task(first+','+last, '', multi=True))
        self.assert_success(app)
        self.assertEqual(ChangeType.RENAMED, result.files[0].change_type)
        self.assertEqual(package(b'OLD_RENAME'), (out/'oldVersion/Demo/a.jar').read_bytes())
        self.assertEqual(package(b'NEW_RENAME'), (out/'newVersion/Demo/b.jar').read_bytes())

    def test_multi_version_files_keep_distinct_endpoints_and_unselected_changes(self):
        for kind in ('git', 'svn'):
            with self.subTest(kind=kind):
                repo = Repository(self, kind, kind+'_history')
                repo.commit({name+'.jar': package((name+'1').encode()) for name in 'ABC'})
                selected1 = repo.commit({'A.jar': package(b'A2'), 'C.jar': package(b'C2')})
                repo.commit({'A.jar': package(b'A_later_unselected'), 'B.jar': package(b'B3'),
                             'C.jar': package(b'C3_retained\nold')})
                selected2 = repo.commit({'B.jar': package(b'B4'), 'C.jar': package(b'C3_retained\nC4')})
                repo.commit({name+'.jar': package(b'WRONG_LATE') for name in 'ABC'})
                task = repo.task(selected1+';\n'+selected2, '', multi=True)
                app, result, out = self.generate(task, True, self.root/(kind+'_history_out'))
                self.assert_success(app)
                self.assertEqual(task['old_version'], result.old_version)
                self.assertEqual('文件级首尾端点', result.new_version)
                expected = {'A': (b'A1', b'A2'), 'B': (b'B3', b'B4'),
                            'C': (b'C1', b'C3_retained\nC4')}
                for name, (a, b) in expected.items():
                    self.assertEqual(package(a), (out/f'oldVersion/Demo/{name}.jar').read_bytes())
                    self.assertEqual(package(b), (out/f'newVersion/Demo/{name}.jar').read_bytes())
                text = (out/'report.html').read_text(encoding='utf-8')
                self.assertNotIn('WRONG', text); self.assertIn('retained', text)

    def test_git_pinned_tag_movement_does_not_change_inspected_export(self):
        from file_exporter import FileExporter
        repo = Repository(self, 'git', 'pinned')
        first = repo.commit({'a.jar': package(b'FIRST')})
        second = repo.commit({'a.jar': package(b'SECOND')})
        late = repo.commit({'a.jar': package(b'WRONG_LATE')})
        repo.cmd('tag', 'old-test', first); repo.cmd('tag', 'new-test', second)
        prepare = FileExporter.prepare_export
        def move_then_export(exporter, *args, **kwargs):
            repo.cmd('tag', '-f', 'new-test', late)
            return prepare(exporter, *args, **kwargs)
        with mock.patch.object(FileExporter, 'prepare_export', move_then_export):
            app, result, out = self.generate(repo.task('old-test', 'new-test'))
        self.assert_success(app)
        self.assertEqual(package(b'SECOND'), (out/'newVersion/Demo/a.jar').read_bytes())
        self.assertNotIn('WRONG', (out/'report.html').read_text(encoding='utf-8'))

    def test_git_subproject_bare_and_boundary_renames(self):
        repo = Repository(self, 'git', 'scopes')
        first = repo.commit({'A/core.jar': package(b'OLD_SCOPE'),
                             'B/core.jar': package(b'OUTSIDE_OLD'),
                             'A/move.jar': package(b'OUTBOUND')})
        repo.cmd('mv', 'A/move.jar', 'B/move.jar')
        second = repo.commit({'A/core.jar': package(b'NEW_SCOPE'),
                              'B/core.jar': package(b'OUTSIDE_NEW')})
        for multi in (False, True):
            task = repo.task(second if multi else first, second, multi=multi, scope=repo.path/'A')
            app, result, out = self.generate(task, output=self.root/('sub_'+str(multi)))
            self.assert_success(app)
            self.assertEqual({'core.jar', 'move.jar'}, {f.file_path for f in result.files})
            self.assertNotIn('OUTSIDE', (out/'report.html').read_text(encoding='utf-8'))
            self.assertFalse((out/'newVersion/Demo/move.jar').exists())
            self.assertTrue((out/'oldVersion/Demo/move.jar').exists())
        bare = self.root/'bare.git'
        repo.cmd('clone', '--bare', '--quiet', str(repo.path), str(bare))
        app, result, _ = self.generate(repo.task(first, second, scope=bare),
                                       output=self.root/'bare-output')
        self.assert_success(app)
        self.assertTrue(any(f.archive_details for f in result.files))

    def test_svn_switch_and_head_advance_preserve_pinned_source(self):
        from file_exporter import FileExporter
        repo = Repository(self, 'svn', 'svn_switch')
        first = repo.commit({'trunk/a.jar': package(b'OLD_SOURCE'),
                             'other/a.jar': package(b'WRONG_SWITCH')})
        second = repo.commit({'trunk/a.jar': package(b'NEW_SOURCE')})
        prepare = FileExporter.prepare_export
        switched = []
        def switch_after_pinning(exporter, *args, **kwargs):
            if not switched:
                repo.cmd('switch', '--ignore-ancestry', repo.url+'/other', str(repo.path/'trunk'))
                repo.cmd('mkdir', repo.url+'/advanced', '-m', 'advance HEAD')
                switched.append(True)
            return prepare(exporter, *args, **kwargs)
        with mock.patch.object(FileExporter, 'prepare_export', switch_after_pinning):
            app, result, out = self.generate(repo.task(first, second, scope=repo.path/'trunk'))
        self.assert_success(app); self.assertTrue(switched)
        self.assertEqual(package(b'NEW_SOURCE'), (out/'newVersion/Demo/a.jar').read_bytes())
        self.assertNotIn('WRONG', (out/'report.html').read_text(encoding='utf-8'))

    def test_svn_root_and_nested_moves_inherit_package_endpoints(self):
        repo = Repository(self, 'svn', 'svn_moves')
        repo.commit({'P/lib/core.jar': package(b'OLD_MOVED')})
        selected1 = repo.commit({'P/lib/core.jar': package(b'MIDDLE')})
        repo.cmd('move', 'P', 'Q')
        repo.cmd('move', 'Q/lib', 'Q/renamed')
        repo.cmd('move', 'Q/renamed/core.jar', 'Q/renamed/final.jar')
        repo.commit()
        selected2 = repo.commit({'Q/renamed/final.jar': package(b'NEW_MOVED')})
        task = repo.task(selected1+','+selected2, '', multi=True, scope=repo.path/'Q')
        app, result, out = self.generate(task)
        self.assert_success(app)
        self.assertEqual(package(b'OLD_MOVED'), (out/'oldVersion/Demo/lib/core.jar').read_bytes())
        self.assertEqual(package(b'NEW_MOVED'), (out/'newVersion/Demo/renamed/final.jar').read_bytes())

    def test_git_export_conversion_valid_archive_and_invalid_archive(self):
        # A self-extracting text prefix can change EOL without changing the ZIP
        # members. This is an allowed raw != export case, not grounds to reject.
        repo = Repository(self, 'git', 'conversion')
        data = []
        for label in ('OLD', 'NEW'):
            for number in range(100):
                candidate = package((label+str(number)).encode())
                if b'\n' not in candidate:
                    data.append(b'prefix\n'+candidate); break
            else: self.fail('no newline-free ZIP fixture')
        first = repo.commit({'.gitattributes': b'*.jar text eol=crlf\n', 'a.jar': data[0]})
        second = repo.commit({'a.jar': data[1]})
        task = repo.task(first, second)
        app, result, out = self.generate(task)
        self.assert_success(app)
        self.assertEqual(data[1].replace(b'prefix\n', b'prefix\r\n'),
                         (out/'newVersion/Demo/a.jar').read_bytes())
        self.assertIsNotNone(next(f for f in result.files if f.file_path=='a.jar').archive_details)
        broken = repo.commit({'a.jar': package(b'newline\ninside')})
        app, _, _ = self.generate(repo.task(second, broken), output=self.root/'bad-conversion')
        self.assertFalse(app._last_task_record['success'])
        self.assertIn('压缩包内部比较失败', app._show_error.call_args.args[0])

    def test_git_lfs_and_filter_fail_without_downloading_or_running_filter(self):
        repo = Repository(self, 'git', 'lfs')
        first = repo.commit({'.gitattributes': b'*.jar -text\n', 'a.jar': package(b'OLD')})
        pointer = ('version https://git-lfs.github.com/spec/v1\n'
                   'oid sha256:'+'b'*64+'\nsize 999\n').encode()
        second = repo.commit({'a.jar': pointer})
        app, _, _ = self.generate(repo.task(first, second))
        self.assertFalse(app._last_task_record['success'])
        self.assertIn('LFS 指针', app._show_error.call_args.args[0])
        filtered = repo.commit({'.gitattributes': b'*.jar filter=check -text\n',
                                'a.jar': package(b'NEW')})
        marker = self.root/'filter-ran'
        repo.cmd('config', 'filter.check.smudge', 'echo unexpected > '+str(marker))
        app, _, _ = self.generate(repo.task(first, filtered), output=self.root/'filter-output')
        self.assertFalse(app._last_task_record['success']); self.assertFalse(marker.exists())

    def test_multi_root_add_and_net_zero_remain_correct(self):
        for kind in ('git', 'svn'):
            repo = Repository(self, kind, kind+'_netzero')
            data = package(b'INITIAL')
            root_commit = repo.commit({'a.jar': data})
            change = repo.commit({'a.jar': package(b'MODIFIED')})
            restore = repo.commit({'a.jar': data})
            app, result, out = self.generate(repo.task(root_commit, '', multi=True),
                                             output=self.root/(kind+'_root'))
            self.assert_success(app)
            self.assertEqual(ChangeType.ADDED, result.files[0].change_type)
            self.assertEqual({}, tree_hashes(out/'oldVersion/Demo'))
            app, result, _ = self.generate(repo.task(change+','+restore, '', multi=True),
                                           output=self.root/(kind+'_netzero_out'))
            self.assert_success(app); self.assertEqual([], result.files)

    def test_history_add_delete_rename_and_metadata_only_packages(self):
        for kind in ('git', 'svn'):
            repo = Repository(self, kind, kind+'_changes')
            first = repo.commit({n+'.jar': package(n.encode())
                                 for n in ('modify', 'delete', 'rename', 'mode', 'suffix')})
            repo.cmd('rm', 'delete.jar')
            repo.cmd('mv' if kind=='git' else 'move', 'rename.jar', 'renamed.jar')
            repo.cmd('mv' if kind=='git' else 'move', 'suffix.jar', 'suffix.bin')
            if kind == 'git': repo.cmd('update-index', '--chmod=+x', 'mode.jar')
            else: repo.cmd('propset', 'svn:executable', '*', 'mode.jar')
            second = repo.commit({'modify.jar': package(b'MODIFIED'), 'added.jar': package(b'ADDED')})
            app, result, out = self.generate(repo.task(first, second), output=self.root/(kind+'_changed_out'))
            self.assert_success(app)
            files = {f.file_path: f for f in result.files}
            self.assertEqual(ChangeType.ADDED, files['added.jar'].change_type)
            self.assertEqual(ChangeType.DELETED, files['delete.jar'].change_type)
            self.assertEqual('identical', files['renamed.jar'].archive_details['status'])
            self.assertEqual('unsupported', files['suffix.bin'].archive_details['status'])
            self.assertTrue(files['mode.jar'].metadata_changes)
            self.assertEqual('identical', files['mode.jar'].archive_details['status'])
            self.assertFalse((out/'oldVersion/Demo/added.jar').exists())
            self.assertFalse((out/'newVersion/Demo/delete.jar').exists())

    def test_partial_history_export_failure_preserves_four_outputs(self):
        for repo, cls in zip(self.repositories('failure'), (GitVCS, SVNVCS)):
            task = repo.task(repo.refs[0], repo.refs[1])
            app, _, out = self.generate(task, False, self.root/(repo.kind+'_failure_out'))
            self.assert_success(app); before = tree_hashes(out)
            def partial(_vcs, version, file_path, target_path):
                Path(target_path).write_bytes(b'partial')
                raise RuntimeError('injected historical content read failure')
            with mock.patch.object(cls, 'export_raw_file_to_path', partial):
                app, _, _ = self.generate(task, output=out)
            self.assertFalse(app._last_task_record['success'])
            self.assertEqual(before, tree_hashes(out)); self.assert_no_stages(out)
