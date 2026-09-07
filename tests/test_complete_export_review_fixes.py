import os
import random
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from diff_engine import DiffEngine
from file_exporter import FileExporter
from path_safety import open_new_tree_file
from report_generator import ReportGenerator
from stable_diff import make_table
from vcs.base import ChangedFile, ChangeType
from vcs.git_vcs import GitVCS
from vcs.multi_version_vcs import GitMultiVersionVCS


class BytesVCS:
    project_path = "fixture"
    merge_exact_renames = False

    def __init__(self, old, new, change=ChangeType.MODIFIED):
        self.data = {"old": old, "new": new}
        self.change = change

    def get_changed_files(self, old, new):
        return [ChangedFile("file.txt", self.change, "before.txt" if self.change == ChangeType.RENAMED else "")]

    def get_file_content_raw_bytes(self, version, path):
        return self.data[version]

    get_file_content_bytes = get_file_content_raw_bytes


class TableRows(HTMLParser):
    """读取用户实际看到的六列表格，不检查实现内部列表。"""
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.rows, self.cells = [], []
        self.current = None
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cells = []
        elif tag == "td":
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.current is not None:
            self.cells.append("".join(self.current).replace("\xa0", " "))
            self.current = None
        elif tag == "tr" and len(self.cells) == 6:
            self.rows.append(self.cells)

    def side(self, new=False):
        index = 4 if new else 1
        return [(int(row[index]), row[index + 1]) for row in self.rows if row[index].isdigit()]


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        Path(".tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=".tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        env = mock.patch.dict(os.environ, {
            "COMPARETOOL_TEMP_DIR": str(self.root / "runtime"),
            "COMPARETOOL_TRANSACTION_KEY_FILE": str(self.root / "key"),
        })
        env.start()
        self.addCleanup(env.stop)

    def test_real_recursive_text_keeps_every_line_and_character(self):
        old, new = b"abcdefghijA\n" * 520, b"abcdefghijB\n" * 520
        result = DiffEngine(BytesVCS(old, new)).generate_diff("old", "new")
        file = result.files[0]
        rows = TableRows(file.side_by_side_html)
        self.assertEqual([(i + 1, "abcdefghijA") for i in range(520)], rows.side())
        self.assertEqual([(i + 1, "abcdefghijB") for i in range(520)], rows.side(True))
        self.assertEqual((520, 520), (file.deleted_lines, file.added_lines))
        self.assertIn('class="diff_chg"', file.side_by_side_html)
        path = self.root / "report.html"
        ReportGenerator().generate(result, str(path))
        self.assertIn("abcdefghij", path.read_text(encoding="utf8"))

    def test_fallback_roundtrips_unequal_replace_blocks_and_context(self):
        rng = random.Random(20260907)
        for _ in range(80):
            old = [rng.choice(["a", "a < b", "x\ty", "repeat"]) for _ in range(rng.randrange(30))]
            new = [rng.choice(["a", "a > b", "x y", "repeat"]) for _ in range(rng.randrange(30))]
            rows = TableRows(make_table(old, new))
            self.assertEqual(list(enumerate((s.expandtabs(4) for s in old), 1)), rows.side())
            self.assertEqual(list(enumerate((s.expandtabs(4) for s in new), 1)), rows.side(True))
        old = [f"line {i}" for i in range(20)]
        new = list(old)
        new[9] = "changed"
        with mock.patch("diff_engine.difflib.HtmlDiff.make_table", side_effect=RecursionError):
            file = DiffEngine(BytesVCS("\n".join(old).encode(), "\n".join(new).encode()),
                              show_full_context=False).generate_diff("old", "new").files[0]
        rows = TableRows(file.side_by_side_html)
        self.assertEqual(list(range(7, 14)), [n for n, _ in rows.side()])
        self.assertEqual(list(range(7, 14)), [n for n, _ in rows.side(True)])
        self.assertEqual("changed", dict(rows.side(True))[10])

    def test_unknown_encoding_distinguishes_bytes_and_keeps_exports(self):
        cases = [(b"caf\xe9\n", b"caf\xe8\n"),
                 (b"\x81", "\ufffd".encode()),
                 (b"\x81", "\u27e60x81\u27e7".encode())]
        for index, (old, new) in enumerate(cases):
            with self.subTest(index=index):
                vcs = BytesVCS(old, new)
                result = DiffEngine(vcs).generate_diff("old", "new")
                file = result.files[0]
                self.assertEqual((1, 1), (file.deleted_lines, file.added_lines))
                self.assertIn('class="unknown-byte"', file.side_by_side_html)
                self.assertTrue(any(f'class="diff_{kind}"' in file.side_by_side_html for kind in ("chg", "add", "sub")))
                self.assertTrue(any("编码未识别" in text for text in file.display_notes))
                output = self.root / str(index)
                FileExporter(result, vcs).export(str(output / "old"), str(output / "new"))
                self.assertEqual(old, (output / "old/file.txt").read_bytes())
                self.assertEqual(new, (output / "new/file.txt").read_bytes())
                ReportGenerator().generate(result, str(output / "report.html"))
                self.assertNotIn("\udc81", (output / "report.html").read_text(encoding="utf8"))

    def test_unknown_bytes_work_for_add_delete_rename_and_recursive_fallback(self):
        for change in (ChangeType.ADDED, ChangeType.DELETED, ChangeType.RENAMED):
            vcs = BytesVCS(b"\x81", b"\x82", change)
            file = DiffEngine(vcs).generate_diff("old", "new").files[0]
            self.assertIn('class="unknown-byte"', file.side_by_side_html)
            file.side_by_side_html.encode("utf8")
        with mock.patch("diff_engine.difflib.HtmlDiff.make_table", side_effect=RecursionError):
            file = DiffEngine(BytesVCS(b"\x81", b"\x82")).generate_diff("old", "new").files[0]
        self.assertIn("⟦0x81⟧", file.side_by_side_html)
        self.assertIn("⟦0x82⟧", file.side_by_side_html)
        file.side_by_side_html.encode("utf8")

    def test_noncolliding_literal_tilde_names_are_exportable(self):
        from diff_engine import DiffResult, FileDiff
        names = ["ABC~1/FILE~1.TXT", "other.txt", "straße/a.txt", "strasse/b.txt"]
        vcs = BytesVCS(b"", b"payload")
        result = DiffResult("fixture", "fixture", "Git", "old", "new",
                            files=[FileDiff(p, ChangeType.ADDED) for p in names])
        FileExporter(result, vcs).export(str(self.root / "old"), str(self.root / "new"))
        for name in names:
            self.assertEqual(b"payload", (self.root / "new" / name).read_bytes())

    def _require_short_names(self):
        if os.name != "nt":
            self.skipTest("需要 NTFS 8.3 名称")
        probe = self.root / "probe"
        probe.mkdir()
        long = probe / "longfilename.txt"
        long.write_bytes(b"original")
        if not (probe / "longfi~1.txt").exists():
            self.skipTest("此临时卷未启用 NTFS 8.3 名称")

    def test_actual_leaf_alias_does_not_overwrite_an_existing_file(self):
        self._require_short_names()
        root = self.root / "probe"
        with self.assertRaisesRegex(ValueError, "别名"):
            open_new_tree_file(str(root), str(root / "longfi~1.txt"))
        self.assertEqual(b"original", (root / "longfilename.txt").read_bytes())

    def test_actual_directory_alias_does_not_merge_distinct_trees(self):
        self._require_short_names()
        folder = self.root / "longdirname"
        folder.mkdir()
        if not (self.root / "longdi~1").is_dir():
            self.skipTest("未生成该目录的短文件名")
        with self.assertRaisesRegex(ValueError, "别名"):
            open_new_tree_file(str(self.root), str(self.root / "longdi~1/unexpected.txt"))
        self.assertFalse((folder / "unexpected.txt").exists())

    def test_real_git_normal_and_multi_alias_collision_preserves_previous_delivery(self):
        self._require_short_names()
        repo = self.root / "repo"
        repo.mkdir()
        def git(*args, data=None):
            p = subprocess.run([GitVCS._find_git(), *args], cwd=repo, input=data, capture_output=True)
            self.assertEqual(0, p.returncode, p.stderr.decode("utf8", "replace"))
            return p.stdout.strip()
        git("init", "-q")
        git("config", "user.name", "CompareTool Test")
        git("config", "user.email", "test@example.invalid")
        git("config", "core.autocrlf", "false")
        empty = git("mktree", data=b"")
        old = git("commit-tree", empty.decode(), "-m", "empty").decode()
        for names in [("longfilename.txt", "longfi~1.txt"),
                      ("longdirname/first.txt", "longdi~1/second.txt")]:
            git("read-tree", "--empty")
            records = []
            for name in names:
                blob = git("hash-object", "-w", "--stdin", data=name.encode())
                records.append(b"100644 " + blob + b"\t" + name.encode() + b"\0")
            git("update-index", "-z", "--index-info", data=b"".join(records))
            tree = git("write-tree")
            new = git("commit-tree", tree.decode(), "-p", old, "-m", "two files").decode()
            git("update-ref", "HEAD", new)
            vcs = GitVCS(str(repo))
            try:
                result = DiffEngine(vcs).generate_diff(old, new)
                self.assertEqual(2, len(result.files))
                output = self.root / ("output-" + new)
                for side in ("old", "new"):
                    (output / side).mkdir(parents=True)
                    (output / side / "sentinel.txt").write_bytes(b"previous delivery")
                with self.assertRaisesRegex(RuntimeError, "别名"):
                    FileExporter(result, vcs).export(str(output / "old"), str(output / "new"))
                for side in ("old", "new"):
                    self.assertEqual(["sentinel.txt"], [p.name for p in (output / side).iterdir()])
                    self.assertEqual(b"previous delivery", (output / side / "sentinel.txt").read_bytes())
                with self.assertRaisesRegex(RuntimeError, "别名"):
                    GitMultiVersionVCS(str(repo), [new])
            finally:
                vcs.cleanup()


if __name__ == "__main__":
    unittest.main()
