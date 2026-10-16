"""Old GNU sparse extended maps, native logical bytes and worker delivery."""
import bz2
import gzip
import io
import tarfile
from unittest import mock

from archive_workflow_fixtures import WorkflowCase
from archive_report import ArchiveReportBudget, _ReportArchiveVCS
from vcs.archive_vcs import ArchiveVCS


def extended_sparse(payload=b'abcd', count=27):
    offsets = [index * 256 for index in range(count)]
    info = tarfile.TarInfo('sparse.dat')
    info.type, info.size = tarfile.GNUTYPE_SPARSE, count * len(payload)
    header = bytearray(info.tobuf(format=tarfile.GNU_FORMAT))
    for index, offset in enumerate(offsets[:4]):
        position = 386 + index * 24
        header[position:position + 12] = tarfile.itn(offset, 12)
        header[position + 12:position + 24] = tarfile.itn(len(payload), 12)
    header[482] = int(count > 4)
    header[483:495] = tarfile.itn(8192, 12)
    header[148:156] = b'        '
    header[148:156] = ('%06o\0 ' % sum(header)).encode('ascii')
    blocks = []
    for start in range(4, count, 21):
        block = bytearray(512)
        for index, offset in enumerate(offsets[start:start + 21]):
            block[index * 24:index * 24 + 12] = tarfile.itn(offset, 12)
            block[index * 24 + 12:index * 24 + 24] = tarfile.itn(len(payload), 12)
        block[504] = int(start + 21 < count)
        blocks.append(bytes(block))
    tail = io.BytesIO()
    with tarfile.open(fileobj=tail, mode='w') as archive:
        for name, data in (('keep.txt', b'kept'), ('logs/skip.txt', b'excluded')):
            member = tarfile.TarInfo(name); member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    body = payload * count
    expected = bytearray(8192)
    for offset in offsets:
        expected[offset:offset + len(payload)] = payload
    return bytes(header) + b''.join(blocks) + body + bytes(-len(body) % 512) + tail.getvalue(), bytes(expected)


class GnuSparseExtensionTests(WorkflowCase):
    def pair(self, old, new, suffix='.tar'):
        paths = self.root / ('old' + suffix), self.root / ('new' + suffix)
        for path, data in zip(paths, (old, new)):
            path.write_bytes(data)
        return tuple(map(str, paths))

    def test_chained_maps_logical_budgets_and_delivery(self):
        for count in (5, 27):
            old, old_bytes = extended_sparse(count=count)
            new, new_bytes = extended_sparse(b'wxyz', count)
            with tarfile.open(fileobj=io.BytesIO(new)) as archive:
                self.assertEqual(new_bytes, archive.extractfile('sparse.dat').read())
                self.assertEqual(b'kept', archive.extractfile('keep.txt').read())
            for index, (suffix, encode) in enumerate((('.tar', bytes), ('.tgz', gzip.compress),
                                                       ('.tar.bz2', bz2.compress))):
                paths = self.pair(encode(old), encode(new), suffix)
                budget = ArchiveReportBudget()
                vcs = _ReportArchiveVCS(*paths, budget, extraction_excludes=['logs/**'])
                try:
                    self.assertEqual(old_bytes, vcs.get_file_content_bytes('old', 'sparse.dat'))
                    self.assertEqual(new_bytes, vcs.get_file_content_bytes('new', 'sparse.dat'))
                    self.assertEqual((6, 16408, 16408),
                                     (budget.members, budget.declared_bytes, budget.actual_bytes))
                finally:
                    vcs.cleanup()
                task = dict(vcs_type='archive', old_version=paths[0], new_version=paths[1],
                            project_name='Demo', exclude_rules='logs/**')
                app, result, out = self.generate(task, output=self.root / f'out-{count}-{index}')
                self.assert_success(app)
                self.assertFalse(result.delivery_incomplete)
                self.assertEqual(['sparse.dat'], [f.file_path for f in result.files])
                self.assertEqual(old_bytes, (out / 'oldVersion/Demo/sparse.dat').read_bytes())
                self.assertEqual(new_bytes, (out / 'newVersion/Demo/sparse.dat').read_bytes())
                instructions = next(out.glob('*操作说明.txt')).read_text(encoding='utf-8')
                self.assertIn('删除文件：0 个', instructions)
                self.assertIn('新增和修改文件不在此重复列出，请直接以 newVersion 目录为准', instructions)
                self.assertIn('sparse.dat', (out / 'report.html').read_text(encoding='utf-8'))
                self.assertEqual({'sparse.dat'}, {p.relative_to(out / 'newVersion/Demo').as_posix()
                                                for p in (out / 'newVersion/Demo').rglob('*') if p.is_file()})

    def test_invalid_and_truncated_maps_fail_even_when_excluded(self):
        original, _ = extended_sparse(count=5)
        for damage in ('overlap', 'overflow', 'length', 'truncated'):
            raw = bytearray(original)
            if damage == 'truncated':
                raw = raw[:700]
            elif damage == 'length':
                raw[524:536] = tarfile.itn(3, 12)
            else:
                raw[512:524] = tarfile.itn(0 if damage == 'overlap' else 9000, 12)
            paths = self.pair(original, raw)
            with self.subTest(damage=damage), self.assertRaisesRegex(ValueError, 'sparse|提前结束'):
                ArchiveVCS(*paths, extraction_excludes=['**'])

    def test_limits_use_logical_size_and_count_map_metadata(self):
        data, _ = extended_sparse()
        for limits in ({'MAX_SINGLE_MEMBER_BYTES': 1024}, {'MAX_TAR_METADATA_BYTES': 512},
                       {'MAX_TAR_METADATA_RECORDS': 1}):
            with self.subTest(limits=limits), mock.patch.multiple(ArchiveVCS, **limits):
                with self.assertRaises(ValueError):
                    ArchiveVCS(*self.pair(data, data), extraction_excludes=['**'])
