import gzip
import io
import os
from pathlib import Path
import unittest
from unittest import mock

from archive_formats import detect_archive_stream
from archive_report import ArchiveReportBudget, has_archive_candidates, enrich_archive_reports
from diff_engine import DiffEngine
from file_exporter import FileExporter
from vcs.archive_vcs import ArchiveVCS
import test_archive_report as fixtures
from test_archive_report import zip_bytes, tar_bytes
from archive_workflow_fixtures import WorkflowCase, Repository


class ArchiveContentTests(unittest.TestCase):
    setUp = fixtures.ArchiveReportTests.setUp
    source = fixtures.ArchiveReportTests.source
    enrich = fixtures.ArchiveReportTests.enrich

    def test_detection_preserves_position_and_ignores_suffix(self):
        for data, expected in ((zip_bytes({}), 'zip'), (tar_bytes({}), 'tar'),
                               (gzip.compress(b'data'), 'gzip'), (b'plain text', None),
                               (b'7z\xbc\xaf\x27\x1cdata', '7z'), (b'Rar!\x1a\x07data', 'rar')):
            with self.subTest(kind=expected):
                source = io.BytesIO(data); source.seek(2)
                self.assertEqual(expected, detect_archive_stream(source))
                self.assertEqual(2, source.tell())

    def test_top_level_content_detection_for_all_supported_containers(self):
        for index, pack in enumerate((zip_bytes, tar_bytes,
                lambda files: tar_bytes(files, 'w:gz'),
                lambda files: tar_bytes(files, 'w:bz2'))):
            with self.subTest(format=index):
                old, new = self.root/f'old{index}.txt', self.root/f'new{index}.txt'
                old.write_bytes(pack({'data.txt': b'old'}))
                new.write_bytes(pack({'data.txt': b'new'}))
                vcs = ArchiveVCS(str(old), str(new)); self.addCleanup(vcs.cleanup)
                result = DiffEngine(vcs).generate_diff('old', 'new')
                self.assertEqual(['data.txt'], [f.file_path for f in result.files])
                self.assertIn('new', result.files[0].side_by_side_html)

    def test_top_level_plain_text_with_archive_suffix_is_rejected(self):
        old, new = self.root/'old.tar.gz', self.root/'new.tar.gz'
        old.write_text('old'); new.write_text('new')
        with self.assertRaisesRegex(ValueError, '不是有效'):
            ArchiveVCS(str(old), str(new))

    def test_recursive_hidden_archives_and_disguised_text(self):
        def pack(value):
            return zip_bytes({'hidden': zip_bytes({'leaf.txt': value}),
                              'aaa.tar.gz': value, 'raw.zip': b'\0' + value,
                              'data.dat.gz': gzip.compress(value, mtime=0)})
        vcs, result = self.source({'outer.bin': pack(b'old')}, {'outer.bin': pack(b'new')})
        self.assertTrue(has_archive_candidates(result, vcs=vcs))
        detail = self.enrich(vcs, result)
        files = {f.file_path: f for f in detail['members']}
        self.assertIsNone(files['aaa.tar.gz'].archive_details)
        self.assertIn('old', files['aaa.tar.gz'].side_by_side_html)
        self.assertIn('new', files['aaa.tar.gz'].side_by_side_html)
        self.assertFalse(files['raw.zip'].line_counts_complete)
        self.assertIsNone(files['raw.zip'].archive_details)
        self.assertEqual('leaf.txt', files['hidden'].archive_details['members'][0].file_path)
        self.assertEqual('data.dat', files['data.dat.gz'].archive_details['members'][0].file_path)
        FileExporter(result, vcs).export(str(self.root/'out_old'), str(self.root/'out_new'))
        self.assertEqual(pack(b'new'), (self.root/'out_new/outer.bin').read_bytes())

    def test_gzip_streams_gbk_empty_and_concatenated_members(self):
        payload = '1@|@中文数据@|@\r\n'.encode('gbk')
        for index, data in enumerate((payload, b'', payload * 200000)):
            with self.subTest(index=index):
                old, new = self.root/f'o{index}', self.root/f'n{index}'
                old.mkdir(); new.mkdir()
                a, b = old/'table.dat.gz', new/'table.dat.gz'
                a.write_bytes(gzip.compress(b'old'))
                b.write_bytes(gzip.compress(data[:len(data)//2]) + gzip.compress(data[len(data)//2:]))
                vcs = ArchiveVCS(str(a), str(b)); self.addCleanup(vcs.cleanup)
                target = self.root/f'payload{index}.dat'
                vcs.export_file_to_path('new', 'table.dat', str(target))
                self.assertEqual(data, target.read_bytes())

    def test_gzip_corruption_and_exclusion_still_validate_full_stream(self):
        data = bytearray(gzip.compress(b'new' * 1000)); data[-8] ^= 1
        old, new = self.root/'old.dat.gz', self.root/'new.dat.gz'
        old.write_bytes(gzip.compress(b'old')); new.write_bytes(data)
        for patterns in (None, ['**']):
            with self.subTest(patterns=patterns), self.assertRaises((ValueError, OSError, EOFError)):
                ArchiveVCS(str(old), str(new), extraction_excludes=patterns)
        self.assertFalse(list((self.root/'runtime').glob('cmp_*')))

    def test_gzip_shared_budget_and_excluded_bytes(self):
        old = gzip.compress(b'old'); new = gzip.compress(b'new')
        vcs, result = self.source({'table.dat.gz': old}, {'table.dat.gz': new})
        budget = ArchiveReportBudget()
        detail = self.enrich(vcs, result, budget=budget, exclude_patterns=['*.dat'])
        self.assertEqual([], detail['members'])
        self.assertEqual((2, 6, 6), (budget.members, budget.declared_bytes, budget.actual_bytes))
        with self.assertRaisesRegex(RuntimeError, '累计展开'):
            self.enrich(vcs, result, budget=ArchiveReportBudget(max_bytes=5))

    def test_false_suffix_does_not_create_archive_candidate(self):
        vcs, result = self.source({'aaa.tar.gz': b'old'}, {'aaa.tar.gz': b'new'})
        self.assertFalse(has_archive_candidates(result, vcs=vcs))
        self.enrich(vcs, result)
        self.assertIsNone(result.files[0].archive_details)
        self.assertIn('new', result.files[0].side_by_side_html)

    def test_stage_binding_occurs_once_before_first_real_archive(self):
        vcs, result = self.source({'hidden': zip_bytes({'nested': zip_bytes({'x': b'old'})}),
                                   'aaa.tar.gz': b'old'},
                                  {'hidden': zip_bytes({'nested': zip_bytes({'x': b'new'})}),
                                   'aaa.tar.gz': b'new'})
        bind = mock.Mock()
        enrich_archive_reports(result, vcs, before_archive_analysis=bind)
        bind.assert_called_once_with()
        result.files = [f for f in result.files if f.file_path == 'aaa.tar.gz']
        bind.reset_mock()
        enrich_archive_reports(result, vcs, before_archive_analysis=bind)
        bind.assert_not_called()

    def test_unsupported_real_format_and_damaged_real_zip_are_not_text(self):
        vcs, result = self.source({'7z.data': b'7z\xbc\xaf\x27\x1cold',
                                   'bad.dat': b'PK\x03\x04old'},
                                  {'7z.data': b'7z\xbc\xaf\x27\x1cnew',
                                   'bad.dat': b'PK\x03\x04new'})
        enrich_archive_reports(result, vcs, best_effort=True)
        files = {f.file_path: f for f in result.files}
        self.assertEqual('unsupported', files['7z.data'].archive_details['status'])
        self.assertEqual('error', files['bad.dat'].archive_details['status'])
        self.assertFalse(result.delivery_incomplete)


class ContentDetectionWorkflowTests(WorkflowCase):
    def test_six_modes_content_detection_preserves_delivery_and_staging(self):
        def content(value):
            return zip_bytes({'aaa.tar.gz': value,
                              'table.dat.gz': gzip.compress(value, mtime=0),
                              'hidden': zip_bytes({'data.txt': value})})
        tasks = []
        for kind in ('git', 'svn'):
            repo = Repository(self, kind, kind+'_content')
            first = repo.commit({'bundle.data': content(b'OLD_CONTENT')})
            last = repo.commit({'bundle.data': content(b'NEW_CONTENT')})
            tasks.extend((repo.task(first, last, project_name=kind),
                          repo.task(last, '', multi=True, project_name=kind+'_multi')))
            repo.write({'bundle.data': content(b'WRONG_WORKING_COPY')})
        old, new = self.root/'old', self.root/'new'; old.mkdir(); new.mkdir()
        (old/'bundle.data').write_bytes(content(b'OLD_CONTENT'))
        (new/'bundle.data').write_bytes(content(b'NEW_CONTENT'))
        tasks.append(dict(vcs_type='folder', project_name='folder', old_version=str(old), new_version=str(new)))
        a, b = self.root/'old.unknown', self.root/'new.unknown'
        a.write_bytes(zip_bytes({'bundle.data': content(b'OLD_CONTENT')}))
        b.write_bytes(zip_bytes({'bundle.data': content(b'NEW_CONTENT')}))
        tasks.append(dict(vcs_type='archive', project_name='archive', old_version=str(a), new_version=str(b)))
        for task in tasks:
            with self.subTest(mode=task['vcs_type']):
                off_app, off, off_dir = self.generate(task, False, self.root/(task['project_name']+'_off'))
                self.assert_success(off_app)
                with mock.patch.object(FileExporter, 'capture_stage_states', wraps=FileExporter.capture_stage_states) as bind:
                    on_app, on, on_dir = self.generate(task, True, self.root/(task['project_name']+'_on'))
                self.assert_success(on_app); self.assertTrue(bind.called)
                self.assert_same_delivery(off_dir, on_dir)
                self.assertEqual(off.summary, on.summary)
                text = (on_dir/'report.html').read_text(encoding='utf-8')
                self.assertIn('OLD_CONTENT', text); self.assertIn('NEW_CONTENT', text)
                self.assertNotIn('WRONG_WORKING_COPY', text)
                self.assert_no_stages(on_dir)
        # 混合报告同样必须对无归档后缀的真实容器绑定暂存基线。
        out = self.root/'mixed'; out.mkdir()
        app = self.app()
        app._do_generate_multi([dict(task, recursive_archives=True) for task in tasks],
            str(out/'report.html'), str(out/'oldVersion'), str(out/'newVersion'), str(out))
        self.assert_success(app)
        self.assertIn('table.dat', (out/'report.html').read_text(encoding='utf-8'))
        self.assert_no_stages(out)
