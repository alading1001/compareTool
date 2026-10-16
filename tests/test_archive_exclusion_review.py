"""Independent regressions for excluded directory streams and GNU sparse TARs."""
import bz2
import gzip
import hashlib
import io
from pathlib import Path
import tarfile
import zipfile

from archive_report import ArchiveReportBudget, _ReportArchiveVCS
from archive_workflow_fixtures import WorkflowCase
from vcs.archive_vcs import ArchiveVCS


def sparse_tar(payload=b'abcd', *, sparse_name='sparse.dat', logs=None):
    info = tarfile.TarInfo(sparse_name)
    info.type, info.size = tarfile.GNUTYPE_SPARSE, len(payload)
    header = bytearray(info.tobuf(format=tarfile.GNU_FORMAT))
    header[386:398] = tarfile.itn(0, 12)
    header[398:410] = tarfile.itn(len(payload), 12)
    header[482] = 0
    header[483:495] = tarfile.itn(8192, 12)
    header[148:156] = b'        '
    header[148:156] = ('%06o\0 ' % sum(header)).encode('ascii')
    tail = io.BytesIO()
    with tarfile.open(fileobj=tail, mode='w') as archive:
        for name, data in (logs if logs is not None else [('logs/weblog.', b'log')] * 2):
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return header + payload + b'\0' * (-len(payload) % 512) + tail.getvalue()


def pax_sparse_tar(name, payload=b'abcd', *, extra=()):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w', format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo('physical-header-name')
        info.size = len(payload)
        info.pax_headers = {'GNU.sparse.map': '0,' + str(len(payload)),
                            'GNU.sparse.name': name, 'GNU.sparse.realsize': '8192'}
        archive.addfile(info, io.BytesIO(payload))
        for path, data in extra:
            item = tarfile.TarInfo(path)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return stream.getvalue()


class ArchiveExclusionReviewTests(WorkflowCase):
    def pair(self, suffix, old, new):
        paths = self.root / ('old' + suffix), self.root / ('new' + suffix)
        for path, data in zip(paths, (old, new)):
            path.write_bytes(data)
        return tuple(map(str, paths))

    def directory_zip(self, name, payload=b'', *, legacy=False):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_STORED) as archive:
            info = zipfile.ZipInfo(name)
            if legacy:
                info.create_system, info.external_attr = 3, 0xFFFF0010
            archive.writestr(info, payload)
        return stream.getvalue()

    def test_nonempty_zip_directory_is_rejected_even_when_excluded(self):
        for name in ('logs/weblog./', 'logs/weblog/'):
            for corrupt in (False, True):
                with self.subTest(name=name, corrupt=corrupt):
                    raw = bytearray(self.directory_zip(name, b'PAYLOAD'))
                    if corrupt:
                        raw[30 + len(name.encode())] ^= 1
                    paths = self.pair('.zip', self.directory_zip('empty/'), raw)
                    with self.assertRaisesRegex(ValueError, '目录.*正文|CRC'):
                        ArchiveVCS(*paths, extraction_excludes=['**/logs/**'])

    def test_all_empty_zip_directories_validate_local_headers_and_crc(self):
        for name, patterns, legacy in (('logs/weblog./', ['**/logs/**'], False),
                                       ('regular/', None, False),
                                       ('legacy/', None, True)):
            original = self.directory_zip(name, legacy=legacy)
            for damage in ('local_name', 'crc', 'encrypted', 'local_encrypted'):
                with self.subTest(name=name, damage=damage):
                    raw = bytearray(original)
                    central = raw.index(b'PK\x01\x02')
                    if damage == 'local_name':
                        raw[30] ^= 1
                    elif damage == 'crc':
                        raw[central + 16] ^= 1
                    elif damage == 'encrypted':
                        raw[6] |= 1
                        raw[central + 8] |= 1
                    else:
                        raw[6] |= 1
                    paths = self.pair('.zip', self.directory_zip('empty/'), raw)
                    with self.assertRaises((ValueError, zipfile.BadZipFile, RuntimeError)):
                        ArchiveVCS(*paths, extraction_excludes=patterns)

    def test_sparse_tar_keeps_logical_bytes_and_counts_discarded_duplicates(self):
        for suffix, encode in (('.tar', bytes), ('.tar.gz', gzip.compress),
                               ('.tar.bz2', bz2.compress)):
            with self.subTest(suffix=suffix):
                paths = self.pair(suffix, encode(sparse_tar()), encode(sparse_tar(b'wxyz')))
                before = [hashlib.sha256(Path(p).read_bytes()).digest() for p in paths]
                budget = ArchiveReportBudget()
                vcs = _ReportArchiveVCS(*paths, budget, extraction_excludes=['**/logs/**'])
                try:
                    self.assertEqual(b'abcd' + b'\0' * 8188,
                                     vcs.get_file_content_bytes('old', 'sparse.dat'))
                    self.assertEqual(b'wxyz' + b'\0' * 8188,
                                     vcs.get_file_content_bytes('new', 'sparse.dat'))
                    self.assertEqual(['sparse.dat'], [f.path for f in vcs.get_changed_files()])
                    self.assertEqual((6, 16396, 16396),
                                     (budget.members, budget.declared_bytes, budget.actual_bytes))
                    self.assertEqual(2, vcs.duplicate_members['new']['logs/weblog.'])
                    self.assertFalse((Path(vcs._tmp_new) / 'logs/weblog.').exists())
                    with self.assertRaisesRegex(ValueError, '占位'):
                        vcs.get_file_content_bytes('new', 'logs/weblog.')
                    with self.assertRaisesRegex(ValueError, '占位'):
                        vcs.export_file_to_path('new', 'logs/weblog.', str(self.root / 'leaked'))
                    with self.assertRaisesRegex(ValueError, '不可变'):
                        vcs.set_exclude_patterns([])
                finally:
                    vcs.cleanup()
                self.assertEqual(before, [hashlib.sha256(Path(p).read_bytes()).digest() for p in paths])
                with self.assertRaisesRegex(ValueError, '安全限制'):
                    _ReportArchiveVCS(*paths, ArchiveReportBudget(max_bytes=16395),
                                      extraction_excludes=['**/logs/**'])

    def test_sparse_tar_virtual_names_do_not_relax_strict_or_conflict_checks(self):
        for logs in ([('logs/../outside.', b'x')], [('logs/NUL.', b'x')],
                     [('logs/a.', b'one'), ('logs/a.', b'two')],
                     [('logs/A.', b'one'), ('logs/a.', b'two')]):
            with self.subTest(logs=logs):
                data = sparse_tar(logs=logs)
                paths = self.pair('.tar', data, data)
                with self.assertRaises(ValueError):
                    ArchiveVCS(*paths, extraction_excludes=['**/logs/**'])
        data = sparse_tar()
        paths = self.pair('.tar', data, data)
        for patterns in (None, [], ['**/Logs/**']):
            with self.subTest(patterns=patterns), self.assertRaisesRegex(ValueError, '尾随'):
                ArchiveVCS(*paths, extraction_excludes=patterns)

    def test_sparse_tar_uses_actual_root_for_relative_exclusions(self):
        data = sparse_tar(sparse_name='root/sparse.dat', logs=[('root/logs/weblog.', b'log')])
        paths = self.pair('.tar', data, data)
        vcs = ArchiveVCS(*paths, ignore_single_root=True, extraction_excludes=['logs/**'])
        try:
            self.assertEqual('root', vcs.new_root_prefix)
            self.assertEqual(b'abcd' + b'\0' * 8188,
                             vcs.get_file_content_bytes('new', 'sparse.dat'))
        finally:
            vcs.cleanup()
        for name in ('root./sparse.dat', '../sparse.dat'):
            bad = sparse_tar(sparse_name=name, logs=[])
            with self.subTest(name=name), self.assertRaises(ValueError):
                ArchiveVCS(*self.pair('.tar', bad, bad), ignore_single_root=True,
                           extraction_excludes=['logs/**'])

    def test_pax_sparse_real_name_preserves_strict_and_fixed_endpoints(self):
        for patterns in (None, ['**/logs/**']):
            extra = [('logs/weblog.', b'log')] if patterns else []
            data = pax_sparse_tar('root/real.dat', extra=extra)
            paths = self.pair('.tar', data, data)
            with self.subTest(patterns=patterns):
                vcs = ArchiveVCS(*paths, extraction_excludes=patterns)
                try:
                    self.assertEqual(b'abcd' + b'\0' * 8188,
                                     vcs.get_file_content_bytes('new', 'root/real.dat'))
                    self.assertFalse((Path(vcs._tmp_new) / 'physical-header-name').exists())
                finally:
                    vcs.cleanup()
        # The sparse file itself can be an excluded trailing-name member;
        # logical bytes still count even though no physical placeholder exists.
        for data in (sparse_tar(sparse_name='logs/sparse.dat.',
                                logs=[('kept.txt', b'kept')]),
                     pax_sparse_tar('logs/sparse.dat.', extra=[('kept.txt', b'kept')])):
            paths = self.pair('.tar', data, data)
            budget = ArchiveReportBudget()
            vcs = _ReportArchiveVCS(*paths, budget, extraction_excludes=['**/logs/**'])
            try:
                self.assertEqual((4, 16392, 16392),
                                 (budget.members, budget.declared_bytes, budget.actual_bytes))
                self.assertEqual(b'kept', vcs.get_file_content_bytes('new', 'kept.txt'))
                with self.assertRaisesRegex(ValueError, '占位'):
                    vcs.get_file_content_bytes('new', 'logs/sparse.dat.')
            finally:
                vcs.cleanup()
        data = pax_sparse_tar('actual-root/real.dat',
                              extra=[('actual-root/logs/weblog.', b'log')])
        vcs = ArchiveVCS(*self.pair('.tar', data, data), ignore_single_root=True,
                         extraction_excludes=['logs/**'])
        try:
            self.assertEqual('actual-root', vcs.new_root_prefix)
            self.assertEqual(b'abcd' + b'\0' * 8188,
                             vcs.get_file_content_bytes('new', 'real.dat'))
        finally:
            vcs.cleanup()
