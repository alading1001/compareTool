"""固定属性/配置的 Git 原生检出；隔离配置以确保不运行外部 filter。"""

import hashlib
import os
import stat
import subprocess

from .temp_storage import create_temp_dir, remove_temp_dir
from .git_probe_batch import GitProbeBatch, ProbeBatchUnavailable


class GitCheckoutSnapshot:
    _PLAIN_PROBE = b"CompareTool\n"
    _BINARY_PROBE = b"CompareTool\x00\r\n\rprobe\n"

    def __init__(self, vcs, config):
        self.root = ""
        self._probe_batches = {}
        self._probe_batch_unavailable = False
        self.git = vcs._git
        self.timeout = vcs.COMMAND_TIMEOUT
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")
        }
        self.env.update({
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
        })
        self.sources = self._attribute_paths(vcs)
        before = self._source_state(self.sources)
        try:
            self.root = create_temp_dir("comparetool_git_checkout_")
            object_format = vcs._run_bytes(["rev-parse", "--show-object-format"]).decode("ascii").strip()
            self._run(["init", "--bare", "--template=", f"--object-format={object_format}", self.root])
            objects = vcs._run_bytes([
                "rev-parse", "--path-format=absolute", "--git-path", "objects"
            ]).decode("utf-8", "surrogateescape").strip()
            # 只读共享对象库；cat-file 不向源仓库写入对象、索引或配置。
            alternate = os.path.join(self.root, "objects", "info", "alternates")
            with open(alternate, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(objects.replace("\\", "/") + "\n")
            global_attrs = os.path.join(self.root, "global_attributes")
            with open(global_attrs, "wb") as target:
                for index, path in enumerate(self.sources[:2]):
                    if self._copy_attribute_file(path, target) != before[index][1]:
                        raise RuntimeError("Git 属性文件复制期间发生变化，已中止生成")
                    target.write(b"\n")
            os.makedirs(os.path.join(self.root, "info"), exist_ok=True)
            with open(os.path.join(self.root, "info", "attributes"), "wb") as target:
                if self._copy_attribute_file(self.sources[2], target) != before[2][1]:
                    raise RuntimeError("Git 属性文件复制期间发生变化，已中止生成")
            self.args = [
                f"--git-dir={self.root}", "-c", f"core.attributesfile={global_attrs}"
            ]
            for name, value in config.items():
                if value:
                    self.args.extend(["-c", f"{name}={value}"])
            self.probes = []
            for payload in (self._PLAIN_PROBE, self._BINARY_PROBE):
                oid = self._run([*self.args, "hash-object", "-w", "--stdin"], data=payload).decode("ascii").strip()
                self.probes.append(oid)
                own_object = os.path.join(self.root, "objects", oid[:2], oid[2:])
                if os.path.isfile(own_object):
                    # Git 新建的只读探针对象属于本次临时仓库，允许正常清理。
                    os.chmod(own_object, stat.S_IREAD | stat.S_IWRITE)
            self.modes = {}
            if before != self._source_state(self._attribute_paths(vcs)):
                raise RuntimeError("Git 属性文件在任务快照期间发生变化，已中止生成")
            self.source_state = before
        except BaseException:
            self.cleanup()
            raise

    @staticmethod
    def _attribute_paths(vcs):
        paths = []
        for name in ("GIT_ATTR_SYSTEM", "GIT_ATTR_GLOBAL"):
            no_system = os.environ.get("GIT_ATTR_NOSYSTEM", "").lower()
            if name == "GIT_ATTR_SYSTEM" and no_system not in ("", "0", "false", "no", "off"):
                paths.append("")
                continue
            raw = vcs._run_bytes(["var", name]).decode("utf-8", "surrogateescape").strip()
            paths.append(os.path.abspath(os.path.join(vcs._git_cwd(), raw)) if raw else "")
        raw = vcs._run_bytes([
            "rev-parse", "--path-format=absolute", "--git-path", "info/attributes"
        ]).decode("utf-8", "surrogateescape").strip()
        paths.append(raw)
        return paths

    @staticmethod
    def _copy_attribute_file(path, target):
        if not path:
            return
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as source:
                first = True
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    if first and chunk.startswith(b"\xef\xbb\xbf"):
                        chunk = chunk[3:]
                    first = False
                    target.write(chunk)
            return digest.digest()
        except FileNotFoundError:
            pass

    @staticmethod
    def _source_state(paths):
        result = []
        for path in paths:
            digest = hashlib.sha256()
            try:
                if not path:
                    raise FileNotFoundError
                with open(path, "rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                result.append((path, digest.digest()))
            except FileNotFoundError:
                result.append((path, None))
        return result

    def verify_source(self, vcs):
        if self.source_state != self._source_state(self._attribute_paths(vcs)):
            raise RuntimeError("Git 属性文件在任务快照期间发生变化，已中止生成")

    def _run(self, args, *, endpoint=None, data=None):
        env = dict(self.env)
        if endpoint:
            env["GIT_ATTR_SOURCE"] = endpoint
        result = subprocess.run(
            [self.git, *args], env=env, input=data, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=self.timeout,
        )
        if result.returncode or (endpoint and result.stderr):
            raise RuntimeError(
                "Git 原生检出失败；无法可靠导出的 filter 或内容已中止处理：\n"
                + result.stderr.decode("utf-8", "replace")
            )
        return result.stdout

    def conversion_mode(self, endpoint, repository_path, filters):
        """仅让 Git 转换微小探针；大文件始终由原始导出和分块转换处理。"""
        key = (endpoint, repository_path)
        if key in self.modes:
            return self.modes[key]
        args = list(self.args)
        for driver, config in filters.items():
            if config["smudge"] or config["process"] or config["required"]:
                # 不复制转换命令。真正引用该驱动时，Git 因缺少必需程序失败；
                # -filter / !filter / 裸 filter 的状态由 Git 自己识别。
                args.extend(["-c", f"filter.{driver}.required=true"])
        plain = self._probe(args, endpoint, repository_path, self.probes[0])
        if plain == self._PLAIN_PROBE:
            mode = "none"
        elif plain == self._PLAIN_PROBE.replace(b"\n", b"\r\n"):
            binary = self._probe(args, endpoint, repository_path, self.probes[1])
            if binary == self._BINARY_PROBE:
                mode = "auto"
            elif binary == b"CompareTool\x00\r\n\rprobe\r\n":
                mode = "text"
            else:
                raise RuntimeError("Git 检出规则不属于可可靠流式处理的换行转换")
        else:
            raise RuntimeError("Git 检出规则不属于可可靠流式处理的换行转换")
        self.modes[key] = mode
        return mode

    def _probe(self, args, endpoint, path, oid):
        if (self.timeout is not None or self._probe_batch_unavailable
                or not GitProbeBatch.supports(path)):
            return self._run([*args, "cat-file", "--filters", f"--path={path}", oid],
                             endpoint=endpoint)
        key = (endpoint, tuple(args))
        channel = self._probe_batches.get(key)
        if channel is None:
            env = dict(self.env)
            env["GIT_ATTR_SOURCE"] = endpoint
            channel = GitProbeBatch([self.git, *args, "cat-file", "--batch", "--filters"],
                                    env, dict(zip(self.probes, (self._PLAIN_PROBE, self._BINARY_PROBE))))
            self._probe_batches[key] = channel
        try:
            return channel.read(oid, path)
        except ProbeBatchUnavailable:
            # Only a rejected capability at startup uses the legacy command.
            # Bad frames, content errors and failed filters never fall back.
            self._probe_batches.pop(key, None)
            self._probe_batch_unavailable = True
            return self._run([*args, "cat-file", "--filters", f"--path={path}", oid],
                             endpoint=endpoint)
        except BaseException:
            self._probe_batches.pop(key, None)
            channel.close()
            raise

    @staticmethod
    def auto_text_uses_crlf(chunks):
        """分块实现 Git convert.c 的自动文本统计，不按样本截断猜测。"""
        printable_bytes = bytes([8, 9, 12, 27, *range(32, 127), *range(128, 256)])
        ignored = printable_bytes + b"\n"
        printable = nonprintable = 0
        last = b""
        has_lf = False
        for chunk in chunks:
            if b"\r" in chunk or b"\x00" in chunk:
                return False
            controls = len(chunk.translate(None, ignored))
            lf = chunk.count(b"\n")
            printable += len(chunk) - lf - controls
            nonprintable += controls
            has_lf = has_lf or lf > 0
            if chunk:
                last = chunk[-1:]
        if last == b"\x1a":
            nonprintable -= 1
        return has_lf and (printable >> 7) >= nonprintable

    def cleanup(self):
        for channel in getattr(self, "_probe_batches", {}).values():
            channel.close()
        self._probe_batches = {}
        if self.root:
            remove_temp_dir(self.root)
            self.root = ""
