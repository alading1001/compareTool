import io
import hashlib
from pathlib import Path
import stat
import tarfile
import zipfile

from archive_report import ArchiveReportBudget, _ReportArchiveVCS
from archive_workflow_fixtures import WorkflowCase
from test_archive_report import tar_bytes, zip_bytes
from vcs.archive_vcs import ArchiveVCS


class ExcludedTrailingArchiveNamesTests(WorkflowCase):
    def paths(self, suffix='.tar', extra=None):
        encode = zip_bytes if suffix == '.zip' else lambda files: tar_bytes(
            files, {'.tar': 'w', '.tar.gz': 'w:gz', '.tar.bz2': 'w:bz2'}[suffix])
        paths = []
        for side, payload in (('old', b'old\n'), ('new', b'new\n')):
            path = self.root / (side + suffix)
            files = {'shepherd/config.txt': payload,
                     'shepherd/logs/weblog/weblog.': payload}
            files.update(extra or {})
            path.write_bytes(encode(files))
            paths.append(str(path))
        return paths

    def test_desktop_excludes_reported_log_path_and_exports_real_changes(self):
        for suffix in ('.tar', '.tar.gz', '.tar.bz2', '.zip'):
            with self.subTest(suffix=suffix):
                old, new = self.paths(suffix)
                task = dict(vcs_type='archive', old_version=old, new_version=new,
                            project_name='Demo', exclude_rules='**/logs/**')
                app, result, output = self.generate(
                    task, output=self.root / ('output' + suffix))
                self.assert_success(app)
                self.assertEqual(['shepherd/config.txt'], [f.file_path for f in result.files])
                self.assertEqual(b'old\n', (output / 'oldVersion/Demo/shepherd/config.txt').read_bytes())
                self.assertEqual(b'new\n', (output / 'newVersion/Demo/shepherd/config.txt').read_bytes())
                self.assertFalse(result.delivery_incomplete)
                self.assert_no_stages(output)

    def test_without_matching_fixed_exclusion_names_still_fail(self):
        for suffix in ('.tar', '.zip'):
            paths = self.paths(suffix)
            for patterns in (None, [], ['**/Logs/**'], ['*.log']):
                with self.subTest(suffix=suffix, patterns=patterns):
                    with self.assertRaisesRegex(ValueError, '尾随空格或点'):
                        ArchiveVCS(*paths, extraction_excludes=patterns)

    def test_excluded_trailing_directories_never_create_windows_aliases(self):
        extra = {'shepherd/logs/weblog. /nested /a.': b'ignored',
                 'shepherd/logs/weblog/weblog': b'other log'}
        for suffix in ('.tar', '.zip'):
            paths = self.paths(suffix, extra)
            before = [hashlib.sha256(Path(path).read_bytes()).digest() for path in paths]
            vcs = ArchiveVCS(*paths, extraction_excludes=['**/logs/**'])
            try:
                for root in (vcs._tmp_old, vcs._tmp_new):
                    self.assertFalse(list(Path(root).rglob('weblog.')))
                    self.assertFalse(list(Path(root).rglob('nested')))
                    self.assertEqual(b'', (Path(root) / 'shepherd/logs/weblog/weblog').read_bytes())
                self.assertEqual(['shepherd/config.txt'], [f.path for f in vcs.get_changed_files()])
                with self.assertRaisesRegex(ValueError, '占位'):
                    vcs.get_file_content_bytes('new', 'shepherd/logs/weblog/weblog.')
                with self.assertRaisesRegex(ValueError, '不可变'):
                    vcs.set_exclude_patterns([])
            finally:
                vcs.cleanup()
            self.assertEqual(before, [hashlib.sha256(Path(path).read_bytes()).digest() for path in paths])

    def test_ignored_root_and_canonical_parent_spelling_share_exclusion_plan(self):
        for suffix in ('.tar', '.zip'):
            old, new = self.paths(suffix)
            vcs = ArchiveVCS(old, new, ignore_single_root=True,
                             extraction_excludes=['logs/**'])
            try:
                self.assertEqual(['config.txt'], [f.path for f in vcs.get_changed_files()])
            finally:
                vcs.cleanup()
            # 实际目录拼写来自第一个成员，排除与落盘必须使用同一拼写。
            files = {'Root/logs/first': b'log', 'root/logs/weblog.': b'log'}
            Path(old).write_bytes(zip_bytes(files) if suffix == '.zip' else tar_bytes(files))
            Path(new).write_bytes(Path(old).read_bytes())
            vcs = ArchiveVCS(old, new, extraction_excludes=['Root/logs/**'])
            try:
                self.assertEqual([], vcs.get_changed_files())
            finally:
                vcs.cleanup()

    def test_exclusion_does_not_hide_path_escape_ads_reserved_names_or_links(self):
        names = ['logs/../outside.', '/logs/file.', 'C:/logs/file.',
                 'logs/file.:stream', 'logs/NUL.', 'logs/.. /file.', 'logs/.../file.']
        for suffix in ('.tar', '.zip'):
            old, new = self.paths(suffix)
            for name in names:
                with self.subTest(suffix=suffix, name=name):
                    files = {name: b'bad'}
                    Path(new).write_bytes(zip_bytes(files) if suffix == '.zip' else tar_bytes(files))
                    with self.assertRaises(ValueError):
                        ArchiveVCS(old, new, extraction_excludes=['**'])
            if suffix == '.zip':
                with zipfile.ZipFile(new, 'w') as archive:
                    info = zipfile.ZipInfo('logs/link.')
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, b'outside')
            else:
                with tarfile.open(new, 'w') as archive:
                    info = tarfile.TarInfo('logs/link.')
                    info.type, info.linkname = tarfile.SYMTYPE, 'outside'
                    archive.addfile(info)
            with self.assertRaisesRegex(ValueError, '链接'):
                ArchiveVCS(old, new, extraction_excludes=['**'])

    def test_discarded_zip_crc_and_encryption_still_fail(self):
        old, new = self.paths('.zip')
        original = zip_bytes({'logs/weblog.': b'PAYLOAD'})
        raw = bytearray(original)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.infolist()[0]
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        raw[offset] ^= 1
        Path(new).write_bytes(raw)
        with self.assertRaises(zipfile.BadZipFile):
            ArchiveVCS(old, new, extraction_excludes=['**/logs/**'])
        raw = bytearray(original)
        raw[6] |= 1
        raw[raw.find(b'PK\x01\x02') + 8] |= 1
        Path(new).write_bytes(raw)
        with self.assertRaises(RuntimeError):
            ArchiveVCS(old, new, extraction_excludes=['**/logs/**'])

    def test_duplicate_discarded_names_verify_bodies_and_full_byte_budget(self):
        old, new = self.paths()
        for conflicting in (False, True):
            for path in (old, new):
                with tarfile.open(path, 'w') as archive:
                    for payload in (b'FIRST', b'OTHER' if conflicting else b'FIRST'):
                        info = tarfile.TarInfo('logs/weblog.')
                        info.size = len(payload)
                        archive.addfile(info, io.BytesIO(payload))
            if conflicting:
                with self.assertRaisesRegex(ValueError, '同名成员内容不一致'):
                    ArchiveVCS(old, new, extraction_excludes=['**/logs/**'])
            else:
                budget = ArchiveReportBudget()
                vcs = _ReportArchiveVCS(old, new, budget, extraction_excludes=['**/logs/**'])
                try:
                    self.assertEqual(20, budget.actual_bytes)
                    self.assertEqual(20, budget.declared_bytes)
                    self.assertEqual(4, budget.members)
                    self.assertEqual(2, vcs.duplicate_members['new']['logs/weblog.'])
                finally:
                    vcs.cleanup()

    def test_recursive_report_uses_exclusion_but_exports_original_package(self):
        old, new = self.paths('.zip')
        for path, payload in ((old, b'old\n'), (new, b'new\n')):
            package = tar_bytes({'logs/weblog.': b'ignored', 'config.txt': payload})
            Path(path).write_bytes(zip_bytes({'app.tar': package}))
        task = dict(vcs_type='archive', old_version=old, new_version=new,
                    project_name='Demo', exclude_rules='**/logs/**')
        app, result, output = self.generate(task)
        self.assert_success(app)
        self.assertFalse(result.delivery_incomplete)
        details = result.files[0].archive_details
        self.assertEqual('compared', details['status'])
        self.assertEqual(['config.txt'], [f.file_path for f in details['members']])
        for side, path in (('old', old), ('new', new)):
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.read('app.tar'),
                                 (output / (side + 'Version/Demo/app.tar')).read_bytes())

    def test_explicit_extended_directories_are_only_virtual_when_excluded(self):
        for archive_format in (tarfile.GNU_FORMAT, tarfile.PAX_FORMAT):
            old, new = self.paths()
            for path in (old, new):
                with tarfile.open(path, 'w', format=archive_format) as archive:
                    directory = 'root/logs/' + 'long-directory/' * 9 + 'weblog. /'
                    info = tarfile.TarInfo(directory)
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                    info = tarfile.TarInfo(directory + 'file. ')
                    info.size = 3
                    archive.addfile(info, io.BytesIO(b'log'))
            vcs = ArchiveVCS(old, new, ignore_single_root=True,
                             extraction_excludes=['logs/**'])
            try:
                self.assertEqual([], vcs.get_changed_files())
                self.assertEqual(['logs'], [p.name for p in Path(vcs._folder.new_dir).iterdir()])
            finally:
                vcs.cleanup()

    def test_unexcluded_child_and_conflicting_virtual_names_still_fail(self):
        for suffix in ('.tar', '.zip'):
            old, new = self.paths(suffix)
            for files, patterns in (
                    ({'logs/dir./keep.txt': b'keep'}, ['*.']),
                    ({'logs/A.': b'one', 'logs/a.': b'two'}, ['**/logs/**']),
                    ({'logs/a.': b'file', 'logs/a./b': b'child'}, ['**/logs/**'])):
                with self.subTest(suffix=suffix, names=list(files)):
                    Path(new).write_bytes(zip_bytes(files) if suffix == '.zip' else tar_bytes(files))
                    with self.assertRaises(ValueError):
                        ArchiveVCS(old, new, extraction_excludes=patterns)
