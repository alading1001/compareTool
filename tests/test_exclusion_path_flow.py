from pathlib import Path
from unittest import mock

from archive_workflow_fixtures import WorkflowCase, Repository
from report_generator import ReportGenerator
from test_archive_report import zip_bytes
from vcs.base import BaseVCS, ChangeType
from vcs.folder_vcs import FolderVCS
from vcs.multi_version_vcs import SVNMultiVersionVCS, _SVNPathChange, _EndpointPlanner


class ExclusionPathFlowTests(WorkflowCase):
    def six_tasks(self):
        def files(value):
            return {'keep.txt': value,
                    'logs/blocked.txt': b'EXCLUDED_OUTER_BODY_' + value,
                    'lib/app.jar': zip_bytes({'config/value.txt': value,
                                              'logs/weblog.': b'EXCLUDED_INNER_BODY_' + value})}
        tasks = []
        for kind in ('git', 'svn'):
            repo = Repository(self, kind, kind + '-six')
            first = repo.commit(files(b'old\n'))
            last = repo.commit(files(b'new\n'))
            tasks.append(repo.task(first, last, project_name=kind))
            tasks.append(repo.task(last, '', multi=True, project_name=kind + '_multi'))
        folders = []
        archives = []
        for side, payload in (('old', b'old\n'), ('new', b'new\n')):
            folder = self.root / side
            for name, data in files(payload).items():
                target = folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            folders.append(str(folder))
            archive = self.root / (side + '.zip')
            archive.write_bytes(zip_bytes(dict(files(payload), **{'logs/weblog.': b'ignored'})))
            archives.append(str(archive))
        for kind, paths in (('folder', folders), ('archive', archives)):
            tasks.append(dict(vcs_type=kind, project_name=kind, project_path='',
                              old_version=paths[0], new_version=paths[1]))
        return [dict(task, exclude_rules='**/logs/**', recursive_archives=True) for task in tasks]

    def assert_excluded_result(self, result, expected):
        self.assertFalse(result.issues)
        self.assertEqual(set(expected), {f.file_path for f in result.files})
        self.assertEqual(set(expected), {f.file_path for f in result.report_manifest_files})
        archive = next(f for f in result.files if f.file_path == 'lib/app.jar')
        self.assertEqual('compared', archive.archive_details['status'])
        self.assertEqual(['config/value.txt'],
                         [f.file_path for f in archive.archive_details['members']])

    def test_six_modes_and_mixed_report_use_each_exclusion_snapshot(self):
        tasks = self.six_tasks()
        for task in tasks:
            app, result, output = self.generate(task, output=self.root / (task['vcs_type'] + '-single'))
            self.assert_success(app)
            self.assert_excluded_result(result, ['keep.txt', 'lib/app.jar'])
            for side in ('old', 'new'):
                self.assertFalse(list((output / (side + 'Version')).rglob('logs')))
            self.assertNotIn('logs/', next(output.glob('*操作说明.txt')).read_text(encoding='utf-8'))
            text = (output / 'report.html').read_text(encoding='utf-8')
            self.assertNotIn('EXCLUDED_OUTER_BODY', text)
            self.assertNotIn('EXCLUDED_INNER_BODY', text)
        # 部分项目再排除 keep.txt，验证混合任务使用各自保存的规则。
        tasks = [dict(task, exclude_rules=task['exclude_rules'] + ('\nkeep.txt' if i % 2 else ''))
                 for i, task in enumerate(tasks)]
        output = self.root / 'mixed'
        output.mkdir()
        captures = []
        original = ReportGenerator.generate_multi
        def capture(generator, results, path):
            captures.extend(results)
            return original(generator, results, path)
        app = self.app()
        with mock.patch.object(ReportGenerator, 'generate_multi', capture):
            app._do_generate_multi(tasks, str(output / 'report.html'),
                                   str(output / 'oldVersion'), str(output / 'newVersion'), str(output))
        self.assert_success(app)
        self.assertEqual(6, len(captures))
        for i, item in enumerate(captures):
            self.assert_excluded_result(item['diff_result'],
                                        ['lib/app.jar'] if i % 2 else ['keep.txt', 'lib/app.jar'])
        self.assertFalse(list(output.rglob('logs')))
        self.assertNotIn('logs/', (output / '上线操作说明.txt').read_text(encoding='utf-8'))
        self.assert_no_stages(output)

    def test_recursive_exclusion_covers_descendants_without_combining_rules(self):
        vcs = FolderVCS(str(self.root), str(self.root), snapshot=False)
        for patterns, path, expected in (
                (['**/logs/**'], 'logs/weblog/sub', True),
                (['**/logs/**'], 'app/logs/weblog', True),
                (['logs/**'], 'app/logs/weblog', False),
                (['app/*/**'], 'app/logs/weblog', True),
                (['logs/*', 'logs/*/*'], 'logs', False),
                (['logs/*'], 'logs/weblog', False)):
            with self.subTest(patterns=patterns, path=path):
                vcs.set_exclude_patterns(patterns)
                self.assertEqual(expected, vcs._is_excluded_tree(path))

    def test_fixed_folder_does_not_read_excluded_contents(self):
        old, new = self.root / 'old', self.root / 'new'
        for directory, payload in ((old, b'old\n'), (new, b'new\n')):
            (directory / 'logs').mkdir(parents=True)
            (directory / 'logs/blocked.txt').write_bytes(payload)
            (directory / 'keep.txt').write_bytes(payload)
        vcs = FolderVCS(str(old), str(new), snapshot=False)
        vcs.set_exclude_patterns(['**/logs/**'])
        original = FolderVCS._same_file_content
        def compare(a, b):
            if '/logs/' in Path(a).as_posix():
                raise AssertionError('已排除正文不应参与比较')
            return original(a, b)
        with mock.patch.object(FolderVCS, '_same_file_content', side_effect=compare):
            self.assertEqual(['keep.txt'], [f.path for f in vcs.get_changed_files()])

    def test_snapshot_does_not_query_excluded_directory_identity(self):
        old, new = self.root / 'old', self.root / 'new'
        for directory, payload in ((old, b'old\n'), (new, b'new\n')):
            (directory / 'logs').mkdir(parents=True)
            (directory / 'logs/ignored').write_bytes(payload)
            (directory / 'keep.txt').write_bytes(payload)
        vcs = FolderVCS(str(old), str(new))
        self.addCleanup(vcs.cleanup)
        vcs.set_exclude_patterns(['**/logs/**'])
        original = vcs._directory_identity
        def identity(path):
            if Path(path).name == 'logs':
                raise AssertionError('不应打开已剪枝目录')
            return original(path)
        with mock.patch.object(vcs, '_directory_identity', side_effect=identity):
            self.assertEqual(['keep.txt'], [f.path for f in vcs.get_changed_files()])

    def svn_delegate(self, patterns):
        vcs = SVNMultiVersionVCS.__new__(SVNMultiVersionVCS)
        vcs._tmp_root = ''
        vcs._folder = vcs._raw_folder = None
        BaseVCS.__init__(vcs, str(self.root))
        vcs.set_exclude_patterns(patterns)
        vcs._get_svn_properties = mock.Mock(return_value={})
        vcs._svn_node_exists = mock.Mock(return_value=True)
        return vcs

    def test_excluded_svn_directory_is_not_expanded(self):
        vcs = self.svn_delegate(['**/logs/**'])
        vcs._list_svn_files = mock.Mock(side_effect=AssertionError('已排除目录不应查询成员'))
        raw = [_SVNPathChange('D', 'dir', 'logs', None, None),
               _SVNPathChange('M', 'file', 'keep.txt', None, None)]
        changes = vcs._expand_svn_changes(raw, 3, _EndpointPlanner(), selected=True)
        self.assertEqual([('M', 'keep.txt', '')],
                         [(c.action, c.path, c.old_path) for c in changes])

    def test_excluded_svn_copy_does_not_compete_for_rename_identity(self):
        vcs = self.svn_delegate(['**/logs/**'])
        listing = {'app': ['app/a.txt'], 'release': ['release/a.txt'],
                   'logs/copy': ['logs/copy/a.txt']}
        vcs._list_svn_files = mock.Mock(side_effect=lambda name, revision: listing[name])
        raw = [_SVNPathChange('D', 'dir', 'app', None, None),
               _SVNPathChange('A', 'dir', 'release', 'app', 2),
               _SVNPathChange('A', 'dir', 'logs/copy', 'app', 2)]
        changes = vcs._expand_svn_changes(raw, 3, _EndpointPlanner(), selected=True)
        self.assertEqual([('R', 'release/a.txt', 'app/a.txt')],
                         [(c.action, c.path, c.old_path) for c in changes])

    def test_svn_list_filters_files_without_over_pruning_partial_globs(self):
        vcs = self.svn_delegate(['logs/*'])
        vcs._svn_file_url = mock.Mock(return_value='file:///test/logs@3')
        vcs._run_bytes = mock.Mock(return_value=(
            b'<lists><list><entry kind="file"><name>a.txt</name></entry>'
            b'<entry kind="file"><name>nested/b.txt</name></entry></list></lists>'))
        self.assertEqual(['logs/nested/b.txt'], vcs._list_svn_files('logs', 3))
        vcs.set_exclude_patterns(['**/logs/**'])
        vcs._run_bytes.reset_mock()
        self.assertEqual([], vcs._list_svn_files('logs', 3))
        vcs._run_bytes.assert_not_called()

    def test_git_and_svn_history_crossing_exclusion_becomes_delete_and_add(self):
        for kind in ('git', 'svn'):
            with self.subTest(kind=kind):
                repo = Repository(self, kind, kind)
                repo.commit({'app/a.txt': b'kept payload\n', 'logs/anchor': b'ignored'})
                repo.cmd('mv', 'app/a.txt', 'logs/a.txt')
                first = repo.commit()
                repo.cmd('mv', 'logs/a.txt', 'app/b.txt')
                last = repo.commit()
                task = repo.task(', '.join((first, last)), '', multi=True)
                task['exclude_rules'] = '**/logs/**'
                app, result, output = self.generate(task, output=self.root / (kind + '-output'))
                self.assert_success(app)
                self.assertFalse(result.delivery_incomplete)
                self.assertEqual({'app/a.txt': ChangeType.DELETED, 'app/b.txt': ChangeType.ADDED},
                                 {f.file_path: f.change_type for f in result.files})
                self.assertEqual(b'kept payload\n', (output / 'oldVersion/Demo/app/a.txt').read_bytes())
                self.assertEqual(b'kept payload\n', (output / 'newVersion/Demo/app/b.txt').read_bytes())
                self.assertFalse(list(output.rglob('logs')))

    def test_svn_file_directory_replacements_filter_each_real_side(self):
        for old_directory in (True, False):
            repo = Repository(self, 'svn', 'replacement-' + str(old_directory))
            baseline = {'foo/a.txt': b'old\n'} if old_directory else {'foo': b'old\n'}
            baseline['discard/anchor'] = b'ignored'
            first = repo.commit(baseline)
            repo.cmd('move', 'foo', 'discard/old')
            last = repo.commit({'foo': b'new\n'} if old_directory else {'foo/a.txt': b'new\n'})
            patterns = 'foo\ndiscard/**' if old_directory else 'foo/**\ndiscard/**'
            expected = {'foo/a.txt' if old_directory else 'foo': ChangeType.DELETED}
            for multi in (False, True):
                with self.subTest(old_directory=old_directory, multi=multi):
                    task = repo.task(last if multi else first, last, multi=multi)
                    task['exclude_rules'] = patterns
                    app, result, output = self.generate(
                        task, output=self.root / f'replacement-output-{old_directory}-{multi}')
                    self.assert_success(app)
                    self.assertFalse(result.delivery_incomplete)
                    self.assertEqual(expected, {f.file_path: f.change_type for f in result.files})
                    self.assertEqual([], result.required_directory_deletions)
