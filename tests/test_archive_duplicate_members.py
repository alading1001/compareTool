"""同名重复成员仅在完整正文和文件属性一致时合并。"""
import hashlib
import io
import os
from pathlib import Path
import stat
import tarfile
import warnings
import zipfile
from unittest import mock

from archive_report import ArchiveReportBudget, _ReportArchiveVCS
from archive_workflow_fixtures import WorkflowCase, Repository
from diff_engine import DiffEngine
from vcs.archive_vcs import ArchiveVCS


def archive_bytes(entries, suffix='.zip'):
    stream = io.BytesIO()
    if suffix in ('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2'):
        mode = ('w:gz' if suffix in ('.tar.gz', '.tgz') else
                'w:bz2' if suffix in ('.tar.bz2', '.tbz2') else 'w')
        with tarfile.open(fileobj=stream, mode=mode) as archive:
            for name, data, mode in entries:
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                archive.addfile(info, io.BytesIO(data))
    else:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(stream, 'w') as archive:
                for name, data, mode in entries:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | mode) << 16
                    archive.writestr(info, data)
    return stream.getvalue()


class ArchiveDuplicateMembersTests(WorkflowCase):
    def paths(self, old_entries, new_entries=None, suffix='.zip'):
        a, b = self.root / ('old' + suffix), self.root / ('new' + suffix)
        a.write_bytes(archive_bytes(old_entries, suffix))
        b.write_bytes(archive_bytes(old_entries if new_entries is None else new_entries, suffix))
        return str(a), str(b)

    def test_all_formats_merge_identical_members_once(self):
        for suffix in ('.zip', '.jar', '.war', '.ear', '.aar', '.tar',
                       '.tar.gz', '.tgz', '.tar.bz2', '.tbz2'):
            with self.subTest(suffix=suffix):
                entries = [('root/a.sh', b'echo ok\n', 0o755), ('root/empty', b'', 0o644),
                           ('root/a.sh', b'echo ok\n', 0o755), ('root/empty', b'', 0o644),
                           ('root/a.sh', b'echo ok\n', 0o755)]
                vcs = ArchiveVCS(*self.paths(entries, suffix=suffix), ignore_single_root=True)
                try:
                    self.assertEqual(b'echo ok\n', vcs.get_file_content_bytes('old', 'a.sh'))
                    self.assertEqual(b'', vcs.get_file_content_bytes('new', 'empty'))
                    self.assertEqual([], vcs.get_changed_files())
                    self.assertEqual({'root/a.sh': 3, 'root/empty': 2}, vcs.duplicate_members['old'])
                    self.assertEqual('0755', vcs._old_metadata['a.sh']['mode'])
                    self.assertIn('同名重复', DiffEngine(vcs).generate_diff('old', 'new').comparison_note)
                finally:
                    vcs.cleanup()

    def test_same_size_different_content_fails_and_cleans_temp(self):
        for suffix in ('.zip', '.tar', '.tar.gz', '.tar.bz2'):
            with self.subTest(suffix=suffix):
                paths = self.paths([], [('a.bin', b'FIRST', 0o644), ('a.bin', b'OTHER', 0o644)], suffix)
                with self.assertRaisesRegex(ValueError, '同名成员内容不一致'):
                    ArchiveVCS(*paths)
                self.assertFalse(list((self.root / 'runtime').rglob('cmp_new_*')))
                self.assertFalse(list((self.root / 'runtime').rglob('cmp_old_*')))

    def test_repeated_member_keeps_actual_parent_spelling_and_sparse_plan(self):
        entries = [('Root/keep.txt', b'fixed', 0o644),
                   ('root/a.sh', b'echo ok', 0o755), ('root/a.sh', b'echo ok', 0o755)]
        for suffix in ('.zip', '.tar'):
            for patterns in (None, ['*.txt']):
                with self.subTest(suffix=suffix, patterns=patterns):
                    vcs = ArchiveVCS(*self.paths(entries, suffix=suffix), extraction_excludes=patterns)
                    try:
                        self.assertEqual({'Root/keep.txt', 'Root/a.sh'}, set(vcs._new_metadata))
                        self.assertEqual('0755', vcs._new_metadata['Root/a.sh']['mode'])
                        self.assertEqual(b'echo ok', vcs.get_file_content_bytes('new', 'Root/a.sh'))
                        self.assertEqual([], vcs.get_changed_files())
                    finally:
                        vcs.cleanup()

    def test_permissions_must_match_for_identical_content(self):
        for suffix in ('.zip', '.tar'):
            with self.subTest(suffix=suffix):
                paths = self.paths([], [('a.sh', b'same', 0o644), ('a.sh', b'same', 0o755)], suffix)
                with self.assertRaisesRegex(ValueError, '文件属性不一致'):
                    ArchiveVCS(*paths)

    def test_tar_owner_difference_is_not_silently_merged(self):
        a, b = self.paths([], suffix='.tar')
        with tarfile.open(b, 'w') as archive:
            for uid in (0, 1000):
                info = tarfile.TarInfo('a.txt')
                info.size, info.uid = 4, uid
                archive.addfile(info, io.BytesIO(b'same'))
        with self.assertRaisesRegex(ValueError, '文件属性不一致'):
            ArchiveVCS(a, b)

    def test_path_aliases_and_unsafe_names_are_not_duplicate_exceptions(self):
        for first, second in (('A.txt', 'a.txt'), ('dir/a.txt', 'dir/./a.txt'),
                              ('../a.txt', '../a.txt'), ('a.txt:stream', 'a.txt:stream')):
            with self.subTest(names=(first, second)):
                paths = self.paths([], [(first, b'same', 0o644), (second, b'same', 0o644)])
                with self.assertRaises(ValueError):
                    ArchiveVCS(*paths)

    def test_second_zip_copy_crc_is_checked_even_when_excluded(self):
        entries = [('a.class', b'PAYLOAD', 0o644)] * 2
        raw = bytearray(archive_bytes(entries))
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entry = archive.infolist()[1]
        offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
        raw[offset] ^= 1
        a, b = self.paths([])
        Path(b).write_bytes(raw)
        for patterns in (None, ['*.class']):
            with self.subTest(patterns=patterns), self.assertRaises(zipfile.BadZipFile):
                ArchiveVCS(a, b, extraction_excludes=patterns)

    def test_excluded_duplicates_still_compare_all_content_without_writing_it(self):
        for suffix in ('.zip', '.tar.gz'):
            with self.subTest(suffix=suffix):
                entries = [('a.class', b'PAYLOAD', 0o644)] * 3
                paths = self.paths(entries, suffix=suffix)
                budget = ArchiveReportBudget()
                vcs = _ReportArchiveVCS(*paths, budget, extraction_excludes=['*.class'])
                try:
                    self.assertEqual(b'', (Path(vcs._tmp_new) / 'a.class').read_bytes())
                    self.assertEqual(6, budget.members)
                    self.assertEqual(42, budget.declared_bytes)
                    self.assertEqual(42, budget.actual_bytes)
                    self.assertEqual(3, vcs.duplicate_members['new']['a.class'])
                    with self.assertRaisesRegex(ValueError, '占位'):
                        vcs.get_file_content_bytes('new', 'a.class')
                finally:
                    vcs.cleanup()

    def test_exclusion_does_not_hide_conflicting_duplicates(self):
        for suffix in ('.zip', '.tar'):
            with self.subTest(suffix=suffix):
                paths = self.paths([], [('a.class', b'AAAA', 0o644), ('a.class', b'BBBB', 0o644)], suffix)
                with self.assertRaisesRegex(ValueError, '同名成员内容不一致'):
                    ArchiveVCS(*paths, extraction_excludes=['*.class'])

    def test_duplicate_entries_cannot_bypass_shared_limits(self):
        paths = self.paths([('a.txt', b'1234', 0o644)] * 2)
        for budget in (ArchiveReportBudget(max_members=3), ArchiveReportBudget(max_bytes=15)):
            with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, '安全限制'):
                _ReportArchiveVCS(*paths, budget)

    def test_only_duplicate_candidates_are_hashed_and_written_once(self):
        for copies in (1, 3):
            with self.subTest(copies=copies):
                paths = self.paths([('a.bin', b'x' * (1024**2 + 9), 0o644)] * copies +
                                   [('unique.txt', b'one', 0o644)])
                dest = self.root / ('extract' + str(copies))
                dest.mkdir()
                instance = ArchiveVCS.__new__(ArchiveVCS)
                with mock.patch('vcs.archive_vcs.hashlib.sha256', wraps=hashlib.sha256) as digest, \
                        mock.patch.object(instance, '_open_archive_member_target',
                                          wraps=instance._open_archive_member_target) as create:
                    instance._extract_zip(paths[0], str(dest))
                self.assertEqual(copies if copies > 1 else 0, digest.call_count)
                self.assertEqual(2, create.call_count)
                self.assertEqual(b'x' * (1024**2 + 9), (dest / 'a.bin').read_bytes())

    def test_legacy_sparse_tar_duplicates_compare_logical_bytes(self):
        info = tarfile.TarInfo('sparse.dat')
        info.type, info.size = tarfile.GNUTYPE_SPARSE, 4
        header = bytearray(info.tobuf(format=tarfile.GNU_FORMAT))
        header[386:398] = b'00000000000\0'
        header[398:410] = b'00000000004\0'
        header[482] = 0
        header[483:495] = b'00000020000\0'
        header[148:156] = b'        '
        header[148:156] = ('%06o\0 ' % sum(header)).encode('ascii')
        path = self.root / 'sparse.tar'
        path.write_bytes((header + b'abcd' + b'\0' * 508) * 2 + b'\0' * 1024)
        vcs = ArchiveVCS(str(path), str(path))
        try:
            self.assertEqual(b'abcd' + b'\0' * 8188, vcs.get_file_content_bytes('old', 'sparse.dat'))
            self.assertEqual(2, vcs.duplicate_members['old']['sparse.dat'])
        finally:
            vcs.cleanup()

    def test_recursive_git_report_preserves_delivery_and_shows_duplicate_counts(self):
        repo = Repository(self, 'git', 'repository')
        def package(value, copies):
            return archive_bytes([('lib/shared.jar', archive_bytes([('fixed', b'fixed', 0o644)]), 0o644)] * copies +
                                 [('config.txt', value, 0o644)])
        old_payload, new_payload = package(b'OLD_VALUE\n', 2), package(b'NEW_VALUE\n', 3)
        old = repo.commit({'app.jar': old_payload})
        new = repo.commit({'app.jar': new_payload})
        task = repo.task(old, new)
        off, plain, a = self.generate(task, enabled=False)
        self.assert_success(off)
        on, detailed, b = self.generate(task, enabled=True)
        self.assert_success(on)
        self.assertEqual(plain.summary, detailed.summary)
        self.assert_same_delivery(a, b)
        self.assertEqual(old_payload, (b / 'oldVersion/Demo/app.jar').read_bytes())
        self.assertEqual(new_payload, (b / 'newVersion/Demo/app.jar').read_bytes())
        detail = detailed.files[0].archive_details
        self.assertEqual({'old': {'lib/shared.jar': 2}, 'new': {'lib/shared.jar': 3}}, detail['duplicate_members'])
        self.assertEqual(['config.txt'], [item.file_path for item in detail['members']])
        text = (b / 'report.html').read_text(encoding='utf-8')
        self.assertIn('同名重复成员', text)
        self.assertIn('lib/shared.jar（2 份）', text)
        self.assertIn('lib/shared.jar（3 份）', text)
        self.assert_no_stages(b)
        multi = self.root / 'multi'
        multi.mkdir()
        app = self.app()
        app._do_generate_multi([dict(task, recursive_archives=True)],
            str(multi / 'report.html'), str(multi / 'oldVersion'),
            str(multi / 'newVersion'), str(multi))
        self.assert_success(app)
        self.assertIn('lib/shared.jar（3 份）', (multi / 'report.html').read_text(encoding='utf-8'))
        self.assertEqual(self.delivery(b), self.delivery(multi))
        self.assert_no_stages(multi)

    def test_duplicate_note_names_are_html_escaped(self):
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        template = Environment(loader=FileSystemLoader('templates'),
                               autoescape=select_autoescape(['html'])).get_template('archive_details.html')
        detail = dict(status='compared', members=[], filtered=False,
                      counts=dict(total_files=0, added_files=0, modified_files=0,
                                  format_changed_files=0, deleted_files=0, renamed_files=0),
                      duplicate_members={'new': {'<img src=x onerror=alert(1)>': 2}})
        html = template.render(detail=detail)
        self.assertNotIn('<img', html)
        self.assertIn('&lt;img', html)
