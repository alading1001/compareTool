"""Keep complete reports while avoiding repeated-edge and archive buffering regressions."""
import unittest

from diff_engine import DiffEngine, DiffResult, FileDiff
from report_generator import ReportGenerator
from stable_diff import make_table
from vcs.base import ChangeType
from test_complete_export_review_fixes import BytesVCS, TableRows


class RepeatedBoundaryTests(unittest.TestCase):
    def test_repeated_edges_preserve_every_line_and_original_numbers(self):
        prefix = ["same prefix"] * 200
        suffix = ["same suffix"] * 250
        old = prefix + ["old <value>\tA"] * 64 + suffix
        new = prefix + ["new &value\tB"] * 97 + suffix
        for full in (True, False):
            with self.subTest(full=full):
                result = DiffEngine(BytesVCS("\n".join(old).encode(),
                    "\n".join(new).encode()), show_full_context=full).generate_diff("old", "new")
                file = result.files[0]
                rows = TableRows(file.side_by_side_html)
                old_rows = [(i, s.expandtabs(4)) for i, s in enumerate(old, 1)]
                new_rows = [(i, s.expandtabs(4)) for i, s in enumerate(new, 1)]
                self.assertEqual(old_rows if full else old_rows[197:267], rows.side())
                self.assertEqual(new_rows if full else new_rows[197:300], rows.side(True))
                self.assertEqual((64, 97), (file.deleted_lines, file.added_lines))

    def test_trimmed_empty_sides_and_context_boundaries(self):
        cases = [([], []), (["same"] * 10, ["same"] * 10),
                 ([], ["new"]), (["old"], []),
                 (["same"] * 10, ["same"] * 5 + ["insert"] + ["same"] * 5),
                 (["same"] * 5 + ["delete"] + ["same"] * 5, ["same"] * 10)]
        for old, new in cases:
            with self.subTest(old=old, new=new):
                rows = TableRows(make_table(old, new))
                self.assertEqual(list(enumerate(old, 1)), rows.side())
                self.assertEqual(list(enumerate(new, 1)), rows.side(True))
                context = make_table(old, new, context=True)
                if old == new:
                    self.assertIn("没有内容差异", context)
                else:
                    self.assertIn("diff_", context)


class ArchiveStreamingTests(unittest.TestCase):
    def test_single_and_multi_reports_stream_complete_nested_details(self):
        leaves = [FileDiff(f"member_{i}.txt", ChangeType.MODIFIED,
                    side_by_side_html=f"<div>payload-{i:03d}:" + "x" * 32768 + "</div>")
                  for i in range(64)]

        def wrap(files, name):
            inner = DiffResult("input", "Demo", "folder", "old", "new", files=files)
            return FileDiff(name, ChangeType.MODIFIED, archive_details=dict(
                status="compared", members=files, counts=inner.summary, filtered=False))

        nested = wrap([wrap(leaves, "inner<&>.jar")], "outer.tar")
        result = DiffResult("input", "Demo", "folder", "old", "new", files=[nested],
                            archive_details_enabled=True)

        class Capture(ReportGenerator):
            def _dump_limited(self, stream, path):
                stream.enable_buffering(64)  # Same batching as the production writer.
                self.chunks = list(stream)

        for multi in (False, True):
            with self.subTest(multi=multi):
                generator = Capture()
                if multi:
                    generator.generate_multi([dict(project_name="Demo", vcs_type="folder",
                        show_project_root=True, diff_result=result)], "unused")
                else:
                    generator.generate(result, "unused")
                total = sum(len(chunk) for chunk in generator.chunks)
                # A macro materializes the whole package as one multi-MB chunk.
                self.assertLess(max(map(len, generator.chunks)), total // 4)
                html = "".join(generator.chunks)
                for i in range(64):
                    self.assertEqual(1, html.count(f"<div>payload-{i:03d}:" + "x" * 32768 + "</div>"))
                self.assertIn("inner&lt;&amp;&gt;.jar", html)
                self.assertEqual(65, html.count('<template class="archive-member-template">'))


if __name__ == "__main__":
    unittest.main()
