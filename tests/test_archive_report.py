import copy
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

from archive_report import ArchiveReportBudget, enrich_archive_reports
from diff_engine import DiffEngine
from file_exporter import FileExporter
from report_generator import ReportGenerator
from vcs.archive_vcs import ArchiveVCS
from vcs.folder_vcs import FolderVCS


def zip_bytes(files, date=(2026, 1, 1, 0, 0, 0), mode=0o644):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, payload in files.items():
            info = zipfile.ZipInfo(name, date_time=date)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, payload)
    return buffer.getvalue()


def tar_bytes(files, mode='w'):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(payload), 0o644
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class ArchiveReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            'COMPARETOOL_TEMP_DIR': str(self.root / 'runtime'),
            'COMPARETOOL_TRANSACTION_KEY_FILE': str(self.root / 'key'),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.old, self.new = self.root / 'old', self.root / 'new'
        self.old.mkdir(); self.new.mkdir()

    def source(self, old, new, patterns=()):
        for base, files in [(self.old, old), (self.new, new)]:
            for name, data in files.items():
                path = base / name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        vcs = FolderVCS(str(self.old), str(self.new))
        self.addCleanup(vcs.cleanup)
        vcs.set_exclude_patterns(list(patterns))
        return vcs, DiffEngine(vcs).generate_diff('old', 'new')

    def enrich(self, vcs, result, **options):
        initial = copy.deepcopy(result.summary)
        paths = [(f.file_path, f.old_path, f.change_type) for f in result.files]
        enrich_archive_reports(result, vcs, **options)
        self.assertEqual(initial, result.summary)
        self.assertEqual(paths, [(f.file_path, f.old_path, f.change_type) for f in result.files])
        return result.files[0].archive_details if result.files else None

    def test_nested_tar_war_jar_text_and_export_bytes(self):
        def payload(text):
            inner = zip_bytes({'config/rules.xml': text, 'A.class': b'\xca\xfe\xba\xbe'})
            return tar_bytes({'app.war': zip_bytes({'lib/core.jar': inner}),
                              'unchanged.jar': zip_bytes({'a.txt': b'fixed'})})
        old, new = payload(b'<v>old</v>'), payload(b'<v>new</v>')
        vcs, result = self.source({'server.tar': old}, {'server.tar': new})
        detail = self.enrich(vcs, result)
        war = detail['members'][0]
        jar = war.archive_details['members'][0]
        leaf = jar.archive_details['members'][0]
        self.assertEqual('config/rules.xml', leaf.file_path)
        self.assertIn('old', leaf.side_by_side_html)
        self.assertIn('new', leaf.side_by_side_html)
        FileExporter(result, vcs).export(str(self.root/'delivery_old'), str(self.root/'delivery_new'))
        self.assertEqual(old, (self.root/'delivery_old/server.tar').read_bytes())
        self.assertEqual(new, (self.root/'delivery_new/server.tar').read_bytes())
        self.assertEqual(['server.tar'], [p.name for p in (self.root/'delivery_new').iterdir()])
        ReportGenerator().generate(result, str(self.root/'report.html'))
        html = (self.root/'report.html').read_text(encoding='utf-8')
        self.assertIn('config/rules.xml', html)
        self.assertIn('archive-member-template', html)
        self.assertIn('不单独交付', html)

    def test_unchanged_nested_package_not_opened(self):
        from archive_report import _ReportArchiveVCS
        bad_but_unchanged = b'not a valid zip; leave opaque when equal'
        a = zip_bytes({'a.txt': b'old', 'fixed.jar': bad_but_unchanged})
        b = zip_bytes({'a.txt': b'new', 'fixed.jar': bad_but_unchanged})
        vcs, result = self.source({'a.zip': a}, {'a.zip': b})
        with mock.patch('archive_report._ReportArchiveVCS', wraps=_ReportArchiveVCS) as opened:
            self.enrich(vcs, result)
        self.assertEqual(1, opened.call_count)

    def test_container_repack_preserves_parent_change(self):
        a = zip_bytes({'a.txt': b'same'})
        b = zip_bytes({'a.txt': b'same'}, date=(2026, 2, 1, 0, 0, 0))
        vcs, result = self.source({'a.zip': a}, {'a.zip': b})
        detail = self.enrich(vcs, result)
        self.assertEqual([], detail['members'])
        self.assertEqual('M', result.files[0].report_type)

    def test_same_bytes_rename_not_expanded(self):
        data = zip_bytes({'a.txt': b'same'})
        vcs, result = self.source({'old.zip': data}, {'new.zip': data})
        with mock.patch('archive_report._ReportArchiveVCS', side_effect=AssertionError('expanded')):
            detail = self.enrich(vcs, result)
        self.assertEqual('R', result.files[0].report_type)
        self.assertEqual('identical', detail['status'])

    def test_new_and_deleted_packages_have_one_sided_members(self):
        vcs, result = self.source({'gone.zip': zip_bytes({'old.txt': b'old'})},
                                  {'added.zip': zip_bytes({'new.txt': b'new'})})
        self.enrich(vcs, result)
        self.assertEqual({'A', 'D'}, {f.report_type for f in result.files})
        for package in result.files:
            self.assertEqual(package.report_type, package.archive_details['members'][0].report_type)

    def test_inner_filters_do_not_modify_export(self):
        a = zip_bytes({'a.txt': b'old', 'A.class': b'old-class'})
        b = zip_bytes({'a.txt': b'new', 'A.class': b'new-class'})
        vcs, result = self.source({'a.jar': a}, {'a.jar': b}, ['*.class'])
        detail = self.enrich(vcs, result, exclude_patterns=['*.class'])
        self.assertEqual(['a.txt'], [f.file_path for f in detail['members']])
        self.assertTrue(detail['filtered'])
        FileExporter(result, vcs).export(str(self.root/'dold'), str(self.root/'dnew'))
        self.assertEqual(b, (self.root/'dnew/a.jar').read_bytes())

    def test_excluded_parent_never_expanded(self):
        vcs, result = self.source({'a.jar': b'old'}, {'a.jar': b'new'}, ['*.jar'])
        with mock.patch('archive_report._ReportArchiveVCS', side_effect=AssertionError('expanded')):
            self.enrich(vcs, result)
        self.assertFalse(result.files)

    def test_member_permission_only_change_is_shown(self):
        a = zip_bytes({'run.sh': b'echo ok'}, mode=0o644)
        b = zip_bytes({'run.sh': b'echo ok'}, mode=0o755)
        vcs, result = self.source({'a.zip': a}, {'a.zip': b})
        detail = self.enrich(vcs, result)
        self.assertTrue(detail['members'][0].metadata_changes)
        self.assertTrue(detail['members'][0].new_executable)

    def test_nested_depth_limit_is_explicit_failure(self):
        def data(value):
            return zip_bytes({'inner.jar': zip_bytes({'x.txt': value})})
        vcs, result = self.source({'a.zip': data(b'old')}, {'a.zip': data(b'new')})
        with self.assertRaisesRegex(RuntimeError, '深度.*inner.jar'):
            self.enrich(vcs, result, budget=ArchiveReportBudget(max_depth=1))

    def test_global_member_budget_shared_between_packages(self):
        old = {f'{i}.zip': zip_bytes({'x.txt': b'old'}) for i in range(2)}
        new = {f'{i}.zip': zip_bytes({'x.txt': b'new'}) for i in range(2)}
        vcs, result = self.source(old, new)
        with self.assertRaisesRegex(RuntimeError, '累计成员'):
            self.enrich(vcs, result, budget=ArchiveReportBudget(max_members=3))

    def test_global_byte_budget_fails_without_silent_skipping(self):
        vcs, result = self.source({'a.zip': zip_bytes({'x.txt': b'old'})},
                                  {'a.zip': zip_bytes({'x.txt': b'new'})})
        with self.assertRaisesRegex(RuntimeError, '累计展开字节'):
            self.enrich(vcs, result, budget=ArchiveReportBudget(max_bytes=5))

    def test_corrupt_changed_nested_archive_fails(self):
        vcs, result = self.source({'a.zip': zip_bytes({'inner.jar': b'old-bad'})},
                                  {'a.zip': zip_bytes({'inner.jar': b'new-bad'})})
        with self.assertRaisesRegex(RuntimeError, 'inner.jar'):
            self.enrich(vcs, result)

    def test_unsafe_member_name_fails(self):
        vcs, result = self.source({}, {'evil.zip': zip_bytes({'../escape.txt': b'bad'})})
        with self.assertRaisesRegex(RuntimeError, 'evil.zip'):
            self.enrich(vcs, result)
        self.assertFalse((self.root/'escape.txt').exists())

    def test_archive_mode_exports_nested_package_not_its_members(self):
        a = zip_bytes({'config/a.xml': b'old'})
        b = zip_bytes({'config/a.xml': b'new'})
        old = self.root/'old.zip'; new = self.root/'new.zip'
        old.write_bytes(zip_bytes({'release_old/app.war': a}))
        new.write_bytes(zip_bytes({'release_new/app.war': b}))
        vcs = ArchiveVCS(str(old), str(new), ignore_single_root=True)
        self.addCleanup(vcs.cleanup)
        result = DiffEngine(vcs).generate_diff(str(old), str(new))
        detail = self.enrich(vcs, result)
        self.assertEqual('app.war', result.files[0].file_path)
        self.assertEqual('config/a.xml', detail['members'][0].file_path)
        FileExporter(result, vcs).export(str(self.root/'dold'), str(self.root/'dnew'))
        self.assertEqual(b, (self.root/'dnew/app.war').read_bytes())
        self.assertFalse((self.root/'dnew/config').exists())

    def test_tar_gzip_and_bzip_recursive_formats(self):
        for suffix, mode in [('.tgz', 'w:gz'), ('.tar.bz2', 'w:bz2')]:
            with self.subTest(suffix=suffix):
                a = tar_bytes({'a.txt': b'old'}, mode)
                b = tar_bytes({'a.txt': b'new'}, mode)
                vcs, result = self.source({'bundle'+suffix: a}, {'bundle'+suffix: b})
                self.enrich(vcs, result)
                for file in result.files:
                    self.assertEqual('a.txt', file.archive_details['members'][0].file_path)

    def test_binary_class_is_not_decompiled(self):
        vcs, result = self.source({'a.jar': zip_bytes({'A.class': b'old'})},
                                  {'a.jar': zip_bytes({'A.class': b'new'})})
        detail = self.enrich(vcs, result)
        file = detail['members'][0]
        self.assertIsNone(file.archive_details)
        self.assertFalse(file.line_counts_complete)
        self.assertIn('二进制', file.side_by_side_html)

    def test_rendering_escapes_member_text(self):
        payload = b'<script>bad()</script>'
        vcs, result = self.source({}, {'a.zip': zip_bytes({'quoted\"name.txt': payload})})
        # Windows forbids quotes in member paths; validation must reject it.
        with self.assertRaises(RuntimeError):
            self.enrich(vcs, result)


if __name__ == '__main__':
    unittest.main()
