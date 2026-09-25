"""Task-scoped, streaming access to Git blobs through one cat-file process.

No filters or shell are involved. Each response is consumed completely before
another request is sent; malformed/short responses invalidate the process.
"""

import io
import re
import subprocess
import threading

from .temp_storage import open_temp_file


class MissingGitObject(FileNotFoundError):
    pass


class GitBatchReader:
    CHUNK_SIZE = 1024 * 1024

    def __init__(self, git, cwd):
        self.git = git
        self.cwd = cwd
        self._process = None
        self._stderr = None
        self._lock = threading.Lock()

    @staticmethod
    def supports(expression):
        # Legacy one-shot reads preserve paths not representable by --batch.
        return not any(char in expression for char in ("\n", "\r", "\x00"))

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        self._close_unlocked()
        self._stderr = open_temp_file(prefix="comparetool_git_batch_")
        try:
            self._process = subprocess.Popen(
                [self.git, "cat-file", "--batch"], cwd=self.cwd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._stderr, bufsize=64 * 1024,
            )
        except BaseException:
            self._stderr.close()
            self._stderr = None
            raise

    def read_bytes(self, expression):
        target = io.BytesIO()
        self.copy_to(expression, target)
        return target.getvalue()

    def copy_to(self, expression, target):
        """Write exactly one blob to target; never buffer a whole export."""
        if not self.supports(expression):
            raise ValueError("Object expression requires a one-shot Git read")
        request = expression.encode("utf-8", "surrogateescape") + b"\n"
        with self._lock:
            self._start()
            process = self._process
            try:
                process.stdin.write(request)
                process.stdin.flush()
                header = process.stdout.readline(len(request) + 256)
                if header == request[:-1] + b" missing\n":
                    raise MissingGitObject(f"Git object not found: {expression}")
                fields = header.rstrip(b"\n").split(b" ")
                if (not header.endswith(b"\n") or len(fields) != 3
                        or not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[0])
                        or fields[1] != b"blob" or not fields[2].isdigit()):
                    raise RuntimeError(f"Invalid Git batch blob response: {header!r}")
                size = remaining = int(fields[2])
                while remaining:
                    chunk = process.stdout.read(min(remaining, self.CHUNK_SIZE))
                    if not chunk:
                        raise RuntimeError("Git batch returned incomplete blob content")
                    written = target.write(chunk)
                    if written is not None and written != len(chunk):
                        raise OSError("Incomplete write while exporting Git blob")
                    remaining -= len(chunk)
                if process.stdout.read(1) != b"\n":
                    raise RuntimeError("Invalid Git batch blob terminator")
                return size
            except MissingGitObject:
                raise
            except BaseException:
                self._close_unlocked(abort=True)
                raise

    def _close_unlocked(self, abort=False):
        process, self._process = self._process, None
        try:
            if process is not None:
                if abort and process.poll() is None:
                    process.terminate()
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        finally:
            if process is not None and process.stdout is not None:
                process.stdout.close()
            if self._stderr is not None:
                self._stderr.close()
                self._stderr = None

    def close(self):
        with self._lock:
            self._close_unlocked()
