import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from diff_engine import DiffEngine
from stable_diff import prefer_stable_diff
from vcs.git_batch import GitBatchReader, MissingGitObject
from vcs.git_vcs import GitVCS
from test_complete_export_review_fixes import BytesVCS, TableRows


class StableRoutingTests(unittest.TestCase):
    def test_bulk_replace_avoids_recursive_renderer(self):
        old, new = b"abcdefghijA\n" * 240, b"abcdefghijB\n" * 240
        with mock.patch("diff_engine.difflib.HtmlDiff.make_table",
                        side_effect=AssertionError("Legacy renderer must not run")):
            file = DiffEngine(BytesVCS(old, new)).generate_diff("old", "new").files[0]
        rows = TableRows(file.side_by_side_html)
        self.assertEqual([(i + 1, "abcdefghijA") for i in range(240)], rows.side())
        self.assertEqual([(i + 1, "abcdefghijB") for i in range(240)], rows.side(True))
        self.assertEqual((240, 240), (file.deleted_lines, file.added_lines))

    def test_small_edits_and_equal_files_keep_legacy_alignment(self):
        old = [f"line {i}" for i in range(500)]
        self.assertFalse(prefer_stable_diff(old, old))
        new = list(old)
        new[5], new[490] = "first change", "last change"
        self.assertFalse(prefer_stable_diff(old, new))
        self.assertFalse(prefer_stable_diff(["A"] * 63, ["B"] * 63))
        self.assertTrue(prefer_stable_diff(["A"] * 64, ["B"] * 64))

    def test_bulk_context_keeps_boundary_lines(self):
        prefix = [f"prefix {i}" for i in range(10)]
        suffix = [f"suffix {i}" for i in range(10)]
        old = prefix + ["abcdefghijA"] * 80 + suffix
        new = prefix + ["abcdefghijB"] * 80 + suffix
        vcs = BytesVCS("\n".join(old).encode(), "\n".join(new).encode())
        file = DiffEngine(vcs, show_full_context=False).generate_diff("old", "new").files[0]
        rows = TableRows(file.side_by_side_html)
        self.assertEqual(list(range(8, 94)), [i for i, _ in rows.side()])
        self.assertEqual(list(enumerate(old, 1))[7:93], rows.side())
        self.assertEqual(list(enumerate(new, 1))[7:93], rows.side(True))

    def test_unequal_bulk_replace_roundtrips_every_line(self):
        old, new = b"old<&>\tA\n" * 80, b"new<&>\tB\n" * 97
        file = DiffEngine(BytesVCS(old, new)).generate_diff("old", "new").files[0]
        rows = TableRows(file.side_by_side_html)
        self.assertEqual([(i + 1, "old<&>\tA".expandtabs(4)) for i in range(80)], rows.side())
        self.assertEqual([(i + 1, "new<&>\tB".expandtabs(4)) for i in range(97)], rows.side(True))


@unittest.skipUnless(shutil.which("git"), "Git CLI is required")
class GitBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Path(".tmp").mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="batch_tests_", dir=".tmp")
        cls.root = Path(cls.temporary.name).resolve()
        cls.repo = cls.root / "repo"
        cls.repo.mkdir()
        cls.git = shutil.which("git")
        cls.run_git("init", "--quiet", "--template=")
        cls.small = b"zero\x00one\r\ntwo\n\xff"
        cls.large = (b"0123456789abcdef\x00\n" * 130000) + b"tail"
        cls.small_oid = cls.run_git("hash-object", "-w", "--stdin", data=cls.small).strip().decode()
        cls.large_oid = cls.run_git("hash-object", "-w", "--stdin", data=cls.large).strip().decode()
        cls.empty_oid = cls.run_git("hash-object", "-w", "--stdin", data=b"").strip().decode()
        cls.tree_oid = cls.run_git("mktree", data=b"").strip().decode()

    @classmethod
    def run_git(cls, *args, data=None):
        return subprocess.run([cls.git, *args], cwd=cls.repo, input=data,
                              capture_output=True, check=True, timeout=30).stdout

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        env = mock.patch.dict(os.environ, {"COMPARETOOL_TEMP_DIR": str(self.root / "runtime")})
        env.start()
        self.addCleanup(env.stop)
        self.reader = GitBatchReader(self.git, str(self.repo))
        self.addCleanup(self.reader.close)

    def test_empty_binary_repeated_reads_use_one_process(self):
        self.assertEqual(b"", self.reader.read_bytes(self.empty_oid))
        process = self.reader._process
        for _ in range(4):
            self.assertEqual(self.small, self.reader.read_bytes(self.small_oid))
        self.assertIs(process, self.reader._process)
        self.reader.close()
        self.assertIsNotNone(process.poll())

    def test_large_export_is_chunked_and_byte_exact(self):
        class Sink:
            def __init__(self):
                self.digest, self.max_chunk = hashlib.sha256(), 0
            def write(self, data):
                self.max_chunk = max(self.max_chunk, len(data))
                self.digest.update(data)
                return len(data)
        sink = Sink()
        self.assertEqual(len(self.large), self.reader.copy_to(self.large_oid, sink))
        self.assertEqual(hashlib.sha256(self.large).digest(), sink.digest.digest())
        self.assertLessEqual(sink.max_chunk, self.reader.CHUNK_SIZE)

    def test_missing_object_does_not_desynchronize_next_read(self):
        with self.assertRaises(MissingGitObject):
            self.reader.read_bytes("f" * 40)
        process = self.reader._process
        self.assertEqual(self.small, self.reader.read_bytes(self.small_oid))
        self.assertIs(process, self.reader._process)

    def test_non_blob_aborts_process_then_can_restart(self):
        with self.assertRaises(RuntimeError):
            self.reader.read_bytes(self.tree_oid)
        self.assertIsNone(self.reader._process)
        self.assertEqual(self.small, self.reader.read_bytes(self.small_oid))

    def test_failed_target_write_invalidates_channel(self):
        class BrokenSink:
            def write(self, _data):
                raise FileNotFoundError("test target disappeared")
        with self.assertRaises(FileNotFoundError):
            self.reader.copy_to(self.large_oid, BrokenSink())
        self.assertIsNone(self.reader._process)
        self.assertEqual(self.small, self.reader.read_bytes(self.small_oid))

    def test_concurrent_requests_are_serialized(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            outputs = list(pool.map(self.reader.read_bytes, [self.small_oid] * 12))
        self.assertEqual([self.small] * 12, outputs)

    def test_line_protocol_injection_is_rejected(self):
        for suffix in ("\nother", "\rother", "\x00other"):
            expression = self.small_oid + suffix
            self.assertFalse(GitBatchReader.supports(expression))
            with self.assertRaises(ValueError):
                self.reader.read_bytes(expression)
        self.assertIsNone(self.reader._process)

    def test_short_content_and_bad_terminator_are_not_accepted(self):
        for payload in (b"abc", b"abcd!"):
            process = mock.Mock()
            process.stdin = io.BytesIO()
            process.stdout = io.BytesIO(self.small_oid.encode() + b" blob 4\n" + payload)
            process.poll.return_value = None
            process.wait.return_value = 0
            self.reader._process = process
            with mock.patch.object(self.reader, "_start"):
                with self.assertRaises(RuntimeError):
                    self.reader.read_bytes(self.small_oid)
            self.assertIsNone(self.reader._process)
            process.terminate.assert_called_once()


@unittest.skipUnless(shutil.which("git"), "Git CLI is required")
class GitBatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        Path(".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="batch_integration_", dir=".tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        env = mock.patch.dict(os.environ, {"COMPARETOOL_TEMP_DIR": str(self.root / "runtime")})
        env.start()
        self.addCleanup(env.stop)
        self.git("init", "--quiet", "--template=")
        self.git("config", "user.name", "CompareTool test")
        self.git("config", "user.email", "comparetool-test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.name = "space \u4e2d\u6587.txt"
        (self.repo / self.name).write_bytes(b"old\n")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "old")
        self.old = self.git("rev-parse", "HEAD").strip().decode()
        (self.repo / self.name).write_bytes(b"new\n")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "new")
        self.new = self.git("rev-parse", "HEAD").strip().decode()
        self.vcs = GitVCS(str(self.repo))
        self.addCleanup(self.vcs.cleanup)

    def git(self, *args):
        return subprocess.run([shutil.which("git"), *args], cwd=self.repo,
                              capture_output=True, check=True, timeout=30).stdout

    def test_reads_and_exports_share_channel_and_pinned_versions(self):
        self.vcs.get_changed_files(self.old, "HEAD")
        self.assertEqual(b"old\n", self.vcs.get_file_content_raw_bytes(self.old, self.name))
        process = self.vcs._batch_reader._process
        self.assertEqual(b"new\n", self.vcs.get_file_content_raw_bytes("HEAD", self.name))
        self.git("update-ref", "HEAD", self.old)
        self.assertEqual(b"new\n", self.vcs.get_file_content_raw_bytes("HEAD", self.name))
        target = self.root / "exported.txt"
        self.vcs.export_raw_file_to_path("HEAD", self.name, str(target))
        self.assertEqual(b"new\n", target.read_bytes())
        self.assertIs(process, self.vcs._batch_reader._process)
        self.vcs.cleanup()
        self.assertIsNotNone(process.poll())

    def test_explicit_command_timeout_keeps_one_shot_path(self):
        self.vcs.COMMAND_TIMEOUT = 10
        with mock.patch.object(self.vcs, "_get_batch_reader",
                               side_effect=AssertionError("Batch should not run")):
            self.assertEqual(b"new\n", self.vcs.get_file_content_raw_bytes(self.new, self.name))
            target = self.root / "one_shot.txt"
            self.vcs.export_raw_file_to_path(self.new, self.name, str(target))
        self.assertEqual(b"new\n", target.read_bytes())


if __name__ == "__main__":
    unittest.main()
