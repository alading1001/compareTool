# Native Git filters for tiny probes only; not the raw blob data channel.
import os
import re
import subprocess
import threading
from .temp_storage import open_temp_file


class ProbeBatchUnavailable(RuntimeError):
    pass


class GitProbeBatch:
    def __init__(self, command, env, allowed_probes):
        self.command = list(command)
        self.env = dict(env)
        self.allowed = dict(allowed_probes)
        self.process = None
        self.stderr = None
        self.failed = False
        self.lock = threading.Lock()

    @staticmethod
    def supports(path):
        return bool(path) and not path[0].isspace() and not any(c in path for c in ('\n','\r','\0'))

    def _start(self):
        if self.failed:
            raise RuntimeError('Git 检出探针通道已经失效')
        if self.process is not None:
            if self.process.poll() is not None:
                raise RuntimeError('Git 检出探针进程提前结束')
            return
        self.stderr = open_temp_file(prefix='comparetool_git_probe_')
        try:
            self.process = subprocess.Popen(self.command, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr)
        except BaseException:
            self.stderr.close(); self.stderr = None
            raise

    def _diagnostic(self):
        if self.stderr is None: return ''
        self.stderr.seek(0)
        return self.stderr.read().decode('utf-8','replace')

    def read(self, oid, path):
        if oid not in self.allowed or not self.supports(path):
            raise ValueError('Git 批量检出只接收本任务微小探针及可表达路径')
        with self.lock:
            try:
                self._start()
                p = self.process
                p.stdin.write((oid+' '+path+'\n').encode('utf-8','surrogateescape'))
                p.stdin.flush()
                header = p.stdout.readline(256)
                if not header:
                    try: code = p.wait(timeout=2)
                    except subprocess.TimeoutExpired: code = None
                    if code == 129:
                        raise ProbeBatchUnavailable(self._diagnostic())
                fields = header.rstrip(b'\n').split(b' ')
                if (not header.endswith(b'\n') or len(fields)!=3
                        or fields[0] != oid.encode('ascii') or fields[1]!=b'blob'
                        or not fields[2].isdigit()):
                    raise RuntimeError('Git 检出探针响应无效: '+repr(header)+' '+self._diagnostic())
                raw = self.allowed[oid]
                converted = re.sub(rb"(?<!\r)\n", b"\r\n", raw)
                # Git 2.53 batch --filters prints RAW objectsize in its header,
                # even when the converted body is longer. These two tiny probes
                # have known LF counts and only two allowed representations.
                # Never apply this protocol to arbitrary user content.
                if int(fields[2]) not in (len(raw), len(converted)):
                    raise RuntimeError('Git 检出探针声明长度无效')
                parts = []
                for _ in range(raw.count(b"\n")):
                    part = p.stdout.readline(max(len(raw),len(converted))+1)
                    if not part.endswith(b"\n"):
                        raise RuntimeError('Git 检出探针内容不完整: '+self._diagnostic())
                    parts.append(part)
                body = b"".join(parts)
                if body not in (raw, converted) or p.stdout.read(1) != b"\n":
                    raise RuntimeError('Git 检出探针内容或结束符无效: '+self._diagnostic())
                if os.fstat(self.stderr.fileno()).st_size:
                    raise RuntimeError('Git 原生检出失败: '+self._diagnostic())
                return body
            except BaseException:
                self.failed = True
                self._close(abort=True)
                raise

    def _close(self, abort=False):
        p,self.process=self.process,None
        try:
            if p is not None:
                if abort and p.poll() is None: p.terminate()
                if p.stdin is not None:
                    try:p.stdin.close()
                    except OSError:pass
                try:p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill();p.wait()
        finally:
            if p is not None and p.stdout is not None:p.stdout.close()
            if self.stderr is not None:self.stderr.close();self.stderr=None

    def close(self):
        with self.lock:self._close()
