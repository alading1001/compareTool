"""正常归档超过旧规模阈值时仍生成完整结果，显式策略单独验收。"""
import hashlib
from html import unescape
import io
import os
from pathlib import Path
import re
import tarfile
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

from archive_report import ArchiveReportBudget, enrich_archive_reports
from diff_engine import DiffEngine
from file_exporter import FileExporter
from report_generator import ReportGenerator
from vcs.archive_vcs import ArchiveVCS
from vcs.folder_vcs import FolderVCS


def package(files, compression=zipfile.ZIP_STORED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=compression) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


class ArchiveUnlimitedDefaultsTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / '.tmp' / 'unlimited-tests'
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temporary_root)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {
            'COMPARETOOL_TEMP_DIR': str(self.root / 'runtime'),
        })
        env.start()
        self.addCleanup(env.stop)

    def source(self, old, new):
        a, b = self.root / 'old', self.root / 'new'
        a.mkdir()
        b.mkdir()
        (a / 'release.zip').write_bytes(old)
        (b / 'release.zip').write_bytes(new)
        vcs = FolderVCS(str(a), str(b))
        self.addCleanup(vcs.cleanup)
        return vcs, DiffEngine(vcs).generate_diff('old', 'new')

    def test_more_than_100000_members_reaches_report_and_export(self):
        # 重复空目录在 ZIP 中合法，能实测真实成员数而不制造十万个磁盘文件。
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(buffer, 'w') as archive:
                for _ in range(100_001):
                    archive.writestr('empty/', b'')
                archive.writestr('last.txt', b'complete-last-member')
        new = buffer.getvalue()
        vcs, result = self.source(package({}), new)
        budget = ArchiveReportBudget()
        enrich_archive_reports(result, vcs, budget=budget)
        self.assertEqual(100_002, budget.members)
        details = result.files[0].archive_details
        self.assertEqual(['last.txt'], [f.file_path for f in details['members']])
        report = self.root / 'report.html'
        ReportGenerator().generate(result, str(report))
        self.assertIn('complete-last-member', report.read_text(encoding='utf-8'))
        FileExporter(result, vcs).export(str(self.root / 'export-old'), str(self.root / 'export-new'))
        self.assertEqual(hashlib.sha256(new).digest(),
                         hashlib.sha256((self.root / 'export-new/release.zip').read_bytes()).digest())

    def test_more_than_eight_layers_keeps_last_member(self):
        def nested(value):
            data = package({'last.txt': value})
            for _ in range(9):
                data = package({'inner.jar': data})
            return data
        vcs, result = self.source(nested(b'old-complete'), nested(b'new-complete'))
        enrich_archive_reports(result, vcs)
        item = result.files[0]
        depth = 0
        while item.archive_details:
            depth += 1
            item = item.archive_details['members'][0]
        self.assertEqual(10, depth)
        self.assertEqual('last.txt', item.file_path)
        visible = unescape(re.sub(r'<[^>]*>', '', item.side_by_side_html))
        self.assertIn('new-complete', visible)

    def test_high_compression_zip_and_tar_are_fully_read(self):
        payload = b'\x00' * (8 * 1024**2)
        zipped = package({'body.bin': payload}, zipfile.ZIP_DEFLATED)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            item = tarfile.TarInfo('body.bin')
            item.size = len(payload)
            archive.addfile(item, io.BytesIO(payload))
        empty_tar = io.BytesIO()
        with tarfile.open(fileobj=empty_tar, mode='w:gz'):
            pass
        for suffix, data in (('.zip', zipped), ('.tar.gz', buffer.getvalue())):
            with self.subTest(suffix=suffix):
                self.assertGreater(len(payload) / len(data), 1000)
                old, new = self.root / ('old' + suffix), self.root / ('new' + suffix)
                old.write_bytes(package({}) if suffix == '.zip' else empty_tar.getvalue())
                new.write_bytes(data)
                vcs = ArchiveVCS(str(old), str(new))
                try:
                    self.assertEqual(payload, (Path(vcs._tmp_new) / 'body.bin').read_bytes())
                finally:
                    vcs.cleanup()

    def test_large_declared_sizes_have_no_default_budget(self):
        # 声明预检不要求创建超大测试文件；实际长度/CRC 仍由读取链路核对。
        size = 3 * 1024**3
        members = [(f'{i}.bin', size, 1, False) for i in range(4)]
        self.assertEqual(12 * 1024**3,
                         ArchiveVCS._validate_archive_limits('large.zip', members))
        budget = ArchiveReportBudget()
        budget.reserve(100_001, 12 * 1024**3)
        budget.consume(12 * 1024**3)
        self.assertEqual(budget.declared_bytes, budget.actual_bytes)

    def test_explicit_policies_still_reject_overflow(self):
        members = [('body.bin', 5, 1, False)]
        for key, limit in (('MAX_ARCHIVE_MEMBERS', 0), ('MAX_SINGLE_MEMBER_BYTES', 4),
                           ('MAX_TOTAL_UNCOMPRESSED_BYTES', 4), ('MAX_COMPRESSION_RATIO', 4)):
            with self.subTest(policy=key), mock.patch.object(ArchiveVCS, key, limit):
                with self.assertRaises(ValueError):
                    ArchiveVCS._validate_archive_limits('limited.zip', members)


if __name__ == '__main__':
    unittest.main()
