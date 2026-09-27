"""Real temporary storage, streaming templates and worker rollback contracts."""
from dataclasses import replace
import os
import re
from pathlib import Path
from unittest import mock

from archive_workflow_fixtures import WorkflowCase, tree_hashes, package
from archive_report import enrich_archive_reports
from diff_engine import DiffEngine
from html_details import HtmlDetailStore
from report_generator import ReportGenerator
from test_complete_export_review_fixes import BytesVCS, TableRows
from vcs.folder_vcs import FolderVCS


class HtmlDetailTests(WorkflowCase):
    def store(self):
        result = HtmlDetailStore(avoid_paths=[self.root/'inputs'])
        self.addCleanup(result.close)
        return result

    def test_moved_repeated_block_keeps_common_text_aligned_in_both_engine_routes(self):
        import diff_engine
        common = [f'unchanged_unique_{i:04d}' for i in range(2000)]
        old = ['old-start'] * 64 + common + ['repeat'] * 32 + ['old-end'] * 64
        new = ['new-start'] * 64 + ['repeat'] * 32 + common + ['new-end'] * 64
        for left, right in ((old, new), (new, old)):
            for full in (False, True):
                rendered = []
                for stored in (False, True):
                    with self.subTest(full=full, stored=stored, reverse=left is new):
                        method = 'iter_stable_diff_table' if stored else 'make_stable_diff_table'
                        with mock.patch.object(diff_engine, method, wraps=getattr(diff_engine, method)) as call:
                            result = DiffEngine(BytesVCS('\n'.join(left).encode(), '\n'.join(right).encode()),
                                show_full_context=full, detail_store=self.store() if stored else None
                            ).generate_diff('old', 'new')
                        self.assertTrue(call.called)
                        file = result.files[0]
                        self.assertEqual((160, 160), (file.added_lines, file.deleted_lines))
                        text = ''.join(file.html_fragment.iter_text()) if stored else file.side_by_side_html
                        rows = TableRows(text)
                        rendered.append(rows.rows)
                        for side, lines in ((False, left), (True, right)):
                            visible = rows.side(side)
                            if full:
                                self.assertEqual(list(enumerate(lines, 1)), visible)
                            else:
                                self.assertTrue(set((n, value) for n, value in enumerate(lines, 1)
                                    if not value.startswith('unchanged_unique_')).issubset(visible))
                                self.assertTrue(all(lines[n-1] == value for n, value in visible))
                        common_rows = [r for r in rows.rows if r[2].startswith('unchanged_unique_')]
                        self.assertEqual(2000 if full else 6, len(common_rows))
                        self.assertTrue(all(r[2] == r[5] for r in common_rows))
                        # Equal text must also be unhighlighted in the emitted HTML.
                        self.assertFalse(any('diff_chg' in row or 'diff_add' in row or 'diff_sub' in row
                            for row in re.findall(r'<tr>.*?</tr>', text) if 'unchanged_unique_' in row))
                self.assertEqual(*rendered)

    def test_unicode_boundaries_offsets_interleaving_and_empty_fragments(self):
        store = self.store()
        store.CHUNK_BYTES = 7
        values = ['中🙂é<&>' * 100, '', 'other\n' * 25]
        refs = [store.add_fragment(s) for s in values]
        self.assertEqual([0, len(values[0].encode()), len(values[0].encode())],
                         [r.offset for r in refs])
        for _ in range(2):
            self.assertEqual(values, [''.join(r.iter_text()) for r in refs])
        a, b = refs[0].iter_text(), refs[2].iter_text()
        first, second = next(a), next(b)
        self.assertEqual(values[0], first + ''.join(a))
        self.assertEqual(values[2], second + ''.join(b))
        self.assertEqual(3, store.fragments)
        self.assertLessEqual(len(list(Path(store.root).iterdir())), 1)
        directory = store.root
        store.close(); store.close()
        self.assertFalse(Path(directory).exists())
        with self.assertRaises(RuntimeError): list(refs[0].iter_text())

    def test_completed_references_only_short_write_and_missing_data(self):
        store = self.store()
        def interrupted():
            yield 'partial'
            raise OSError('injected writer failure')
        with self.assertRaises(OSError): store.add_chunks(interrupted())
        self.assertEqual(0, store.fragments)
        with self.assertRaises(RuntimeError): store.add_fragment('later')
        other = self.store(); ref = other.add_fragment('complete')
        proxy = mock.Mock(wraps=other._file)
        proxy.write.return_value = 1
        other._file = proxy
        with self.assertRaisesRegex(OSError, '写入不完整'): other.add_fragment('more')
        with self.assertRaises(RuntimeError): list(ref.iter_text())

    def test_tampering_cross_task_and_truncation_fail(self):
        for attack in ('replace-content', 'truncate'):
            store = self.store(); ref = store.add_fragment('original')
            store._file.seek(0)
            if attack == 'truncate': store._file.truncate(1)
            else: store._file.write(b'changed!')
            store._file.flush()
            with self.assertRaises(OSError): list(ref.iter_text())
        a, b = self.store(), self.store()
        ref = a.add_fragment('abc')
        b.add_fragment('def')
        with self.assertRaises(ValueError): list(b.iter_fragment(ref))
        with self.assertRaises(ValueError): list(a.iter_fragment(replace(ref, length=999)))

    def test_public_default_and_stored_content_render_identically(self):
        for full in (True, False):
            for before, after in [(b'old\n', b'new\n'), (b'', b'added\n'),
                                  (b'a\xff\n', b'b\xfe\n')]:
                store = self.store()
                normal = DiffEngine(BytesVCS(before, after), show_full_context=full).generate_diff('old', 'new')
                stored = DiffEngine(BytesVCS(before, after), show_full_context=full,
                                    detail_store=store, retain_text_contents=False).generate_diff('old', 'new')
                self.assertEqual(normal.summary, stored.summary)
                self.assertIsNone(normal.files[0].html_fragment)
                self.assertEqual('', stored.files[0].side_by_side_html)
                text = ''.join(stored.files[0].html_fragment.iter_text())
                for side in (False, True):
                    self.assertEqual(TableRows(normal.files[0].side_by_side_html).side(side),
                                     TableRows(text).side(side))
                out = self.root/('render-' + str(store.fragments) + '.html')
                ReportGenerator().generate(stored, str(out))
                html = out.read_text(encoding='utf-8')
                self.assertIn(text, html)
                self.assertNotIn(store.root, html)
                # A second traversal must not see an exhausted generator.
                ReportGenerator().generate(stored, str(out))
                self.assertIn(text, out.read_text(encoding='utf-8'))

    def folder_task(self):
        a, b = self.root/'before', self.root/'after'
        a.mkdir(); b.mkdir()
        for folder, word in ((a, b'OLD'), (b, b'NEW')):
            (folder/'a.txt').write_bytes(word + b'\n')
            (folder/'a.jar').write_bytes(package(word))
        return dict(vcs_type='folder', project_name='Demo',
                    old_version=str(a), new_version=str(b), recursive_archives=True)

    def test_archive_members_survive_their_vcs_cleanup(self):
        task = self.folder_task(); store = self.store()
        vcs = FolderVCS(task['old_version'], task['new_version'])
        try:
            result = DiffEngine(vcs, detail_store=store).generate_diff('old', 'new')
            enrich_archive_reports(result, vcs, detail_store=store)
        finally: vcs.cleanup()
        report = self.root/'after-cleanup.html'
        ReportGenerator().generate(result, str(report))
        self.assertIn('OLD', report.read_text(encoding='utf-8'))
        self.assertIn('NEW', report.read_text(encoding='utf-8'))
        self.assertGreaterEqual(store.fragments, 3)
        self.assertTrue(all(f.side_by_side_html == '' for f in result.files))

    def test_worker_fragment_write_and_read_failure_preserve_outputs(self):
        task = self.folder_task()
        out = self.root/'delivery'
        app, _, out = self.generate(task, True, out)
        self.assert_success(app)
        original = tree_hashes(out)
        def partial_read(_store, _ref):
            yield '<p>partial</p>'
            raise OSError('injected fragment read failure')
        for method, failure in [('add_chunks', OSError('injected fragment write failure')),
                                ('iter_fragment', partial_read)]:
            with mock.patch.object(HtmlDetailStore, method, side_effect=failure if isinstance(failure, Exception) else None,
                                   **({'autospec': True} if callable(failure) else {})) as patched:
                if callable(failure): patched.side_effect = failure
                app, _, _ = self.generate(task, True, out)
            self.assertFalse(app._last_task_record['success'])
            self.assertIn('injected fragment', app._show_error.call_args.args[0])
            self.assertEqual(original, tree_hashes(out))
            self.assert_no_stages(out)
            self.assertFalse(list((self.root/'runtime').glob('comparetool_html_*')))

    def test_mixed_task_store_lifetime_and_cleanup(self):
        task = self.folder_task()
        tasks = [dict(task, project_name=n) for n in ('One', 'Two')]
        out = self.root/'multi'; out.mkdir()
        app = self.app(); seen = []
        original_generate = ReportGenerator.generate_multi
        def generate(generator, projects, destination):
            stores = [f.html_fragment.store for p in projects
                      for f in p['diff_result'].files]
            self.assertEqual(1, len({id(s) for s in stores}))
            self.assertFalse(stores[0]._closed)
            seen.append(stores[0])
            return original_generate(generator, projects, destination)
        with mock.patch.object(ReportGenerator, 'generate_multi', generate):
            app._do_generate_multi(tasks, str(out/'report.html'), str(out/'oldVersion'),
                                   str(out/'newVersion'), str(out))
        self.assert_success(app)
        self.assertTrue(seen[0]._closed)
        self.assertFalse(list((self.root/'runtime').glob('comparetool_html_*')))
        before = tree_hashes(out)
        with mock.patch.object(HtmlDetailStore, 'iter_fragment', side_effect=OSError('multi read failure')):
            app._do_generate_multi(tasks, str(out/'report.html'), str(out/'oldVersion'),
                                   str(out/'newVersion'), str(out))
        self.assertFalse(app._last_task_record['success'])
        self.assertEqual(before, tree_hashes(out))
        self.assert_no_stages(out)
        self.assertFalse(list((self.root/'runtime').glob('comparetool_html_*')))

    def test_stable_renderer_streams_rows_with_notes_and_unknown_characters(self):
        import diff_engine
        from stable_diff import iter_table, make_table
        old = [f'old {i} 中\u2028 literal ⟦U+2028⟧' for i in range(80)]
        new = [f'new {i} 中\u2029 literal ⟦U+2029⟧' for i in range(80)]
        chunks = list(iter_table(old, new))
        self.assertGreater(len(chunks), 80)
        for side in (False, True):
            self.assertEqual(TableRows(make_table(old, new)).side(side),
                             TableRows(''.join(chunks)).side(side))
        before = ('\n'.join(old)).encode() + b'\nunknown:\xff'
        after = ('\n'.join(new)).encode() + b'\nunknown:\xfe'
        for full in (False, True):
            normal = DiffEngine(BytesVCS(before, after), show_full_context=full).generate_diff('old', 'new')
            store = self.store()
            with mock.patch.object(diff_engine, 'make_stable_diff_table',
                                   side_effect=AssertionError('whole-table buffering')):
                result = DiffEngine(BytesVCS(before, after), detail_store=store,
                                    show_full_context=full).generate_diff('old', 'new')
            text = ''.join(result.files[0].html_fragment.iter_text())
            self.assertIn('unknown-byte', text)
            self.assertIn('special-separator', text)
            self.assertIn('文件说明', text)
            self.assertEqual(normal.summary, result.summary)
            for side in (False, True):
                self.assertEqual(TableRows(normal.files[0].side_by_side_html).side(side),
                                 TableRows(text).side(side))
