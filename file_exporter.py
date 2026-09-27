from task_progress import measured_phase, progress, advance
import hashlib
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from diff_engine import DiffResult
from logger import warn
from path_safety import (
    ensure_no_link_components,
    is_link_or_junction,
    metadata_is_link_or_junction,
    open_regular_file_no_links,
    open_new_tree_file,
    regular_file_handle_identity,
    regular_file_path_identity,
    windows_path_key,
    safe_join,
)
from stage_ownership import mark_owned, remove_ownership_marker
from vcs.base import ChangeType


class FileExporter:
    """确认覆盖后删除旧输出，生成完整新结果；不保留备份或回滚日志。"""

    def __init__(self, diff_result: DiffResult, vcs):
        self.diff_result = diff_result
        self.vcs = vcs
        self._directory_entries = {}

    def export(
        self,
        old_dir: str,
        new_dir: str,
        project_name: str = "",
        targets_are_staging_roots: bool = False,
    ):
        targets = [self._safe_join(p, project_name) if project_name else p
                   for p in (old_dir, new_dir)]
        trusted_root = self._output_root(targets)

        def write():
            pairs = self.prepare_export(
                old_dir, new_dir, project_name=project_name,
                targets_are_staging_roots=targets_are_staging_roots,
                trusted_root=trusted_root,
            )
            try:
                self._replace_outputs(pairs, trusted_root=trusted_root)
            finally:
                self.cleanup_stages(pairs)

        if targets_are_staging_roots:
            # 多项目外层任务已持锁；内部暂存只允许写入尚不存在的项目。
            self._validate_output_targets(targets, trusted_root)
            if any(os.path.lexists(p) for p in targets):
                raise RuntimeError("多项目暂存中出现重复项目输出")
            write()
        else:
            with self.output_session(targets, trusted_root=trusted_root,
                                     directory_targets=targets):
                write()

    @measured_phase('output.export', '导出新旧变更文件')
    def prepare_export(
        self,
        old_dir: str,
        new_dir: str,
        project_name: str = "",
        targets_are_staging_roots: bool = False,
        trusted_root: str = "",
    ):
        """完整写入暂存目录，返回可与报告一起提交的 (stage, target) 列表。"""
        if project_name:
            old_dir = self._safe_join(old_dir, project_name)
            new_dir = self._safe_join(new_dir, project_name)

        old_dir = os.path.abspath(old_dir)
        new_dir = os.path.abspath(new_dir)
        if os.path.normcase(old_dir) == os.path.normcase(new_dir):
            raise RuntimeError("新旧版本导出目录不能相同")

        # 暂存物始终直接放在生成开始时确定的可信输出根。正式目标可能位于
        # batch/multi_run 等更深目录；若这些子目录在耗时生成期间被替换为
        # junction，暂存源码仍不会跟随写出可信根，最终提交校验会安全拒绝。
        trusted_root = os.path.abspath(
            trusted_root or self._output_root([old_dir, new_dir])
        )
        if not trusted_root:
            raise RuntimeError("无法确定项目导出的同盘暂存目录")

        self._validate_export_paths(old_dir, new_dir)
        old_ver = self.diff_result.old_version
        new_ver = self.diff_result.new_version
        stage_old = ""
        stage_new = ""

        try:
            stage_old = self._make_stage_dir(
                old_dir, stage_parent=trusted_root, trusted_root=trusted_root
            )
            stage_new = self._make_stage_dir(
                new_dir, stage_parent=trusted_root, trusted_root=trusted_root
            )
            progress(0, sum(1 if f.change_type in (ChangeType.ADDED, ChangeType.DELETED) else 2
                            for f in self.diff_result.files))
            for file_diff in self.diff_result.files:
                if file_diff.change_type == ChangeType.DELETED:
                    self._write_file(stage_old, file_diff.file_path, old_ver, file_diff.old_content)
                elif file_diff.change_type == ChangeType.ADDED:
                    self._write_file(stage_new, file_diff.file_path, new_ver, file_diff.new_content)
                elif file_diff.change_type == ChangeType.RENAMED:
                    old_path = file_diff.old_path or file_diff.file_path
                    self._write_file(stage_old, old_path, old_ver, file_diff.old_content)
                    self._write_file(stage_new, file_diff.file_path, new_ver, file_diff.new_content)
                else:
                    self._write_file(stage_old, file_diff.file_path, old_ver, file_diff.old_content)
                    self._write_file(stage_new, file_diff.file_path, new_ver, file_diff.new_content)
            return [
                (stage_old, old_dir),
                (stage_new, new_dir),
            ]
        except BaseException:
            for stage in (stage_old, stage_new):
                if stage:
                    self._cleanup_stage(stage)
            raise

    @classmethod
    def cleanup_stages(cls, pairs):
        for stage, _target in pairs:
            cls._cleanup_stage(stage)

    @classmethod
    def _cleanup_stage(cls, stage: str):
        if not stage:
            return
        parent = os.path.dirname(os.path.abspath(stage))
        owner = parent if os.path.basename(parent).startswith(".comparetool_stage_") else stage
        if os.path.lexists(stage):
            try:
                cls._remove_path(stage)
            except OSError as exc:
                warn(f"清理导出暂存目录失败: {stage}: {exc}")
                return
        if os.path.basename(parent).startswith(".comparetool_stage_"):
            try:
                os.rmdir(parent)
            except FileNotFoundError:
                pass
            except OSError as exc:
                warn(f"清理导出暂存根失败: {parent}: {exc}")
        if os.path.lexists(owner):
            return
        remove_ownership_marker(owner)

    def _write_file(self, base_dir: str, rel_path: str, version: str, text_content: str):
        file_path = self._safe_join(base_dir, rel_path)
        try:
            file_path, reserved = open_new_tree_file(
                base_dir, file_path, self._directory_entries, "导出"
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        # 在调用允许覆盖目标的旧接口之前，先以独占方式占有这个新路径。
        # 若路径其实是已有长名的 8.3 别名，绝不能进入 writer 或异常删除分支。
        reserved.close()

        # 正式 VCS 使用流式接口，避免大文件在内存中形成完整 bytes 副本。
        stream_export = getattr(self.vcs, "export_file_to_path", None)
        if stream_export is not None:
            try:
                stream_export(version, rel_path, file_path)
                advance()
                return
            except Exception:
                try:
                    os.remove(file_path)
                except OSError:
                    pass
                raise

        # 兼容测试桩和旧扩展；正式内置 VCS 均不会走到这里。
        raw = self.vcs.get_file_content_bytes(version, rel_path)
        if raw is None:
            raise RuntimeError(f"无法读取版本 {version} 中的文件，已中止导出: {rel_path}")
        with open(file_path, "wb") as f:
            f.write(raw)
        advance()

    def _validate_export_paths(self, old_dir: str, new_dir: str):
        old_paths = []
        new_paths = []
        for file_diff in self.diff_result.files:
            if file_diff.change_type == ChangeType.DELETED:
                old_paths.append(file_diff.file_path)
            elif file_diff.change_type == ChangeType.ADDED:
                new_paths.append(file_diff.file_path)
            elif file_diff.change_type == ChangeType.RENAMED:
                old_paths.append(file_diff.old_path or file_diff.file_path)
                new_paths.append(file_diff.file_path)
            else:
                old_paths.append(file_diff.file_path)
                new_paths.append(file_diff.file_path)

        for label, base_dir, paths in (
            ("旧版本", old_dir, old_paths),
            ("新版本", new_dir, new_paths),
        ):
            seen = {}
            for rel_path in paths:
                target = self._safe_join(base_dir, rel_path)
                key = windows_path_key(target)
                if key in seen:
                    raise RuntimeError(
                        f"{label}导出存在 Windows 路径冲突，无法同时保存: "
                        f"{seen[key]} / {rel_path}"
                    )
                seen[key] = rel_path

    @staticmethod
    def _safe_join(base_dir: str, rel_path: str) -> str:
        try:
            return safe_join(base_dir, rel_path, label="导出路径")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc

    @classmethod
    def _make_stage_dir(
        cls,
        target_dir: str,
        stage_parent: str = "",
        trusted_root: str = "",
    ) -> str:
        parent = stage_parent or os.path.dirname(target_dir)
        anchor = cls._validate_trusted_paths(
            trusted_root or parent,
            [parent, target_dir],
            "输出暂存路径",
        )
        os.makedirs(parent, exist_ok=True)
        cls._validate_trusted_paths(anchor, [parent], "输出暂存目录")
        stage_root = tempfile.mkdtemp(prefix=".comparetool_stage_", dir=parent)
        try:
            cls._validate_trusted_paths(
                anchor, [stage_root], "输出暂存目录"
            )
            mark_owned(stage_root)
            if not stage_parent:
                return stage_root
            stage = os.path.join(stage_root, os.path.basename(target_dir))
            os.makedirs(stage)
            cls._validate_trusted_paths(
                anchor, [stage_root, stage], "输出暂存目录"
            )
            return stage
        except BaseException:
            shutil.rmtree(stage_root, ignore_errors=True)
            remove_ownership_marker(stage_root)
            raise

    @classmethod
    def _make_stage_file(
        cls,
        target_path: str,
        trusted_root: str,
        prefix: str,
        suffix: str,
    ) -> str:
        """在可信输出根直接创建空暂存文件，内容写入前完成路径复核。"""
        target_path = os.path.abspath(target_path)
        anchor = cls._validate_trusted_paths(
            trusted_root,
            [target_path],
            "输出暂存目标",
        )
        os.makedirs(anchor, exist_ok=True)
        cls._validate_trusted_paths(anchor, [anchor], "可信输出根")
        fd, stage_path = tempfile.mkstemp(
            prefix=prefix,
            suffix=suffix,
            dir=anchor,
        )
        os.close(fd)
        try:
            cls._validate_trusted_paths(
                anchor, [stage_path], "输出暂存文件"
            )
            mark_owned(stage_path)
            return stage_path
        except BaseException:
            try:
                os.remove(stage_path)
            except OSError:
                pass
            remove_ownership_marker(stage_path)
            raise

    @classmethod
    def _validate_output_targets(cls, targets, trusted_root):
        """只接受可信根下彼此独立的明确目标，包括真实 Windows 别名检查。"""
        targets = [os.path.abspath(p) for p in targets]
        root = cls._validate_trusted_paths(trusted_root, targets, "输出目标")
        root_key = windows_path_key(os.path.realpath(root))
        keys = []
        for target in targets:
            key = windows_path_key(os.path.realpath(target))
            if key == root_key:
                raise RuntimeError("不能清空整个输出根目录")
            for previous in keys:
                if (key == previous or key.startswith(previous + "\\")
                        or previous.startswith(key + "\\")):
                    raise RuntimeError("输出目标重复或互相包含")
            keys.append(key)
            current = root
            for component in os.path.relpath(target, root).split(os.sep):
                candidate = os.path.join(current, component)
                if not os.path.lexists(candidate):
                    break
                with os.scandir(current) as entries:
                    literal_exists = any(windows_path_key(e.name) == windows_path_key(component)
                                         for e in entries)
                if not literal_exists:
                    raise RuntimeError("输出目标与实际 Windows 名称别名冲突: " + candidate)
                current = candidate
        return targets

    @classmethod
    @contextmanager
    def output_session(cls, targets, *, trusted_root, directory_targets=()):
        """持锁删除已确认的旧结果，再允许调用方生成；异常不恢复旧结果。"""
        targets = cls._validate_output_targets(targets, trusted_root)
        if not targets:
            raise RuntimeError("缺少输出目标")
        lock_root = cls._output_root(targets)
        with cls._output_lock(lock_root, trusted_root=trusted_root):
            yield cls._clear_outputs(targets, trusted_root, directory_targets)

    @classmethod
    @measured_phase('output.clear', '删除上次项目输出')
    def _clear_outputs(cls, targets, trusted_root, directory_targets):
        cls._validate_output_targets(targets, trusted_root)
        directories = {cls._target_state_key(p) for p in directory_targets}
        existing = []
        # 完成所有目标的范围和类型检查后才开始删除，不读旧文件正文。
        for target in targets:
            if not os.path.lexists(target):
                continue
            metadata = os.lstat(target)
            is_directory = cls._target_state_key(target) in directories
            if not (stat.S_ISDIR(metadata.st_mode) if is_directory
                    else stat.S_ISREG(metadata.st_mode)):
                raise RuntimeError("输出目标类型不符，未开始删除: " + target)
            existing.append(target)
        for target in existing:
            cls._validate_trusted_paths(trusted_root, [target], "旧输出删除路径")
            try:
                cls._remove_path(target)
            except OSError as exc:
                raise RuntimeError(
                    "无法删除上次输出，请关闭占用文件的程序后重试。"
                    "已删除的旧结果不会恢复：\n" + target + "\n" + str(exc)
                ) from exc
        return len(existing)

    @classmethod
    @measured_phase('output.commit', '发布本次完整输出')
    def _replace_outputs(cls, pairs, trusted_root="", expected_stage_states=None):
        """调用方已持锁并清空目标；只安装新结果，不备份或恢复旧结果。"""
        pairs = [(os.path.abspath(s), os.path.abspath(t)) for s, t in pairs]
        targets = [t for _s, t in pairs]
        trusted_root = trusted_root or cls._output_root([p for pair in pairs for p in pair])
        cls._validate_output_targets(targets, trusted_root)
        stage_keys = {cls._target_state_key(s) for s, _t in pairs}
        if len(stage_keys) != len(pairs):
            raise RuntimeError("输出暂存项重复")
        if expected_stage_states and not set(expected_stage_states).issubset(stage_keys):
            raise RuntimeError("缺少已绑定的包内分析暂存项")
        stage_identities = {}
        for stage, target in pairs:
            cls._validate_trusted_paths(trusted_root, [stage, target], "输出发布路径")
            metadata = os.lstat(stage)
            if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                raise RuntimeError("输出暂存项不是普通文件或目录: " + stage)
            if any(os.path.commonpath([stage, t]) in (stage, t) for t in targets):
                raise RuntimeError("输出暂存项与正式目标重叠")
            if os.path.lexists(target):
                raise RuntimeError("生成期间输出目标被重新创建，请检查后重试: " + target)
            expected = (expected_stage_states or {}).get(cls._target_state_key(stage))
            if expected is not None:
                cls._assert_identity(stage, expected, "包内分析待交付内容")
            stage_identities[stage] = (metadata.st_dev, metadata.st_ino)

        installed = []
        try:
            for stage, target in pairs:
                cls._validate_trusted_paths(trusted_root, [stage, target], "输出发布路径")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                cls._validate_trusted_paths(trusted_root, [target], "输出发布路径")
                if os.path.lexists(target):
                    raise RuntimeError("生成期间输出目标被重新创建，请检查后重试: " + target)
                # Windows rename 拒绝覆盖已有目标，不把并发创建的文件当作旧输出删除。
                os.rename(stage, target)
                installed.append((target, stage_identities[stage]))
                expected = (expected_stage_states or {}).get(cls._target_state_key(stage))
                if expected is not None:
                    cls._assert_identity(target, expected, "包内分析已发布内容")
        except BaseException:
            # 只清理本次已安装的新结果；旧结果早已按覆盖确认删除。
            for target, identity in reversed(installed):
                try:
                    cls._validate_trusted_paths(trusted_root, [target], "失败输出清理路径")
                    metadata = os.lstat(target)
                    if (metadata.st_dev, metadata.st_ino) != identity:
                        warn("本次输出已被外部替换，保留该路径: " + target)
                        continue
                    cls._remove_path(target)
                except FileNotFoundError:
                    pass
                except (OSError, RuntimeError) as exc:
                    warn(f"清理本次未完成输出失败: {target}: {exc}")
            raise

    @classmethod
    @measured_phase('archive.stage_bind', '固定包内分析的待交付内容')
    def capture_stage_states(cls, stages, *, trusted_root):
        """One pre-analysis scan; caller owns stages and passes this to commit."""
        paths = [os.path.abspath(path) for path in stages]
        cls._validate_trusted_paths(trusted_root, paths, "包内分析暂存路径")
        result = {}
        for path in paths:
            key = cls._target_state_key(path)
            if key in result:
                raise RuntimeError("包内分析暂存根重复: " + path)
            identity = cls._tree_identity(path)
            if identity.get("kind") != "dir":
                raise RuntimeError("包内分析需要完整的源码暂存目录: " + path)
            result[key] = identity
        return result

    @staticmethod
    def _target_state_key(path: str) -> str:
        return windows_path_key(os.path.abspath(path))

    @classmethod
    def _validate_trusted_paths(cls, trusted_root: str, paths, label: str):
        if not trusted_root:
            raise RuntimeError("无法确定可信输出根")
        root = os.path.abspath(trusted_root)
        try:
            ensure_no_link_components(root, root, "可信输出根")
            for path in paths:
                ensure_no_link_components(root, os.path.abspath(path), label)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return root

    @classmethod
    def _tree_identity(cls, path: str) -> dict:
        """绑定文件系统对象及其树内元数据，不跟随任何链接。"""
        path = os.path.abspath(path)
        if not os.path.lexists(path):
            return {"kind": "missing"}
        if is_link_or_junction(path):
            raise RuntimeError(f"待交付内容不能是符号链接或联接点: {path}")

        digest = hashlib.sha256()

        def add_record(relative: str, kind: str, metadata):
            record = (
                relative,
                kind,
                int(getattr(metadata, "st_dev", 0)),
                int(getattr(metadata, "st_ino", 0)),
                int(metadata.st_size),
                int(getattr(
                    metadata,
                    "st_mtime_ns",
                    int(metadata.st_mtime * 1_000_000_000),
                )),
            )
            digest.update(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8", errors="surrogatepass")
            )
            digest.update(b"\n")

        def visit(current: str, relative: str):
            try:
                metadata = os.lstat(current)
            except OSError as exc:
                raise RuntimeError(f"无法读取待交付内容身份: {current}: {exc}") from exc
            # Same-scan metadata only; later handle/path and transaction checks
            # remain independent observations.
            is_redirect = metadata_is_link_or_junction(metadata)
            if os.name == "nt" and not hasattr(metadata, "st_reparse_tag"):
                is_redirect = is_redirect or is_link_or_junction(current)
            if is_redirect:
                raise RuntimeError(
                    f"待交付内容树包含符号链接或联接点: {current}"
                )
            if stat.S_ISREG(metadata.st_mode):
                add_record(relative, "file", metadata)
                try:
                    with open_regular_file_no_links(current) as stream:
                        opened_identity = regular_file_handle_identity(stream)
                        digest.update(b"content\x00")
                        while True:
                            chunk = stream.read(1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                        digest.update(b"\x00end-content\n")
                        closed_identity = regular_file_handle_identity(stream)
                except (OSError, RuntimeError) as exc:
                    raise RuntimeError(
                        f"无法读取待交付内容内容身份: {current}: {exc}"
                    ) from exc
                if opened_identity != closed_identity:
                    raise RuntimeError(
                        f"待交付内容在计算内容身份期间发生变化: {current}"
                    )
                try:
                    final_identity = regular_file_path_identity(current)
                except (OSError, RuntimeError) as exc:
                    raise RuntimeError(
                        f"待交付内容在计算内容身份后发生变化: {current}: {exc}"
                    ) from exc
                if final_identity != opened_identity:
                    raise RuntimeError(
                        f"待交付内容在计算内容身份期间被替换: {current}"
                    )
                return
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(f"待交付内容树包含非普通文件: {current}")
            add_record(relative, "dir", metadata)
            try:
                entries = sorted(
                    os.scandir(current),
                    key=lambda item: (item.name.casefold(), item.name),
                )
            except OSError as exc:
                raise RuntimeError(f"无法遍历待交付内容树: {current}: {exc}") from exc
            for entry in entries:
                child_relative = (
                    entry.name if relative == "." else f"{relative}/{entry.name}"
                )
                visit(entry.path, child_relative)

        visit(path, ".")
        root_metadata = os.lstat(path)
        root_kind = "dir" if stat.S_ISDIR(root_metadata.st_mode) else "file"
        return {
            "kind": root_kind,
            "dev": int(getattr(root_metadata, "st_dev", 0)),
            "ino": int(getattr(root_metadata, "st_ino", 0)),
            "digest": digest.hexdigest(),
        }

    @classmethod
    def _assert_identity(cls, path: str, expected: dict, label: str):
        current = cls._tree_identity(path)
        if current != expected:
            raise RuntimeError(
                f"{label}的文件系统身份或内容元数据已变化（含文件内容摘要），"
                f"已停止自动处理: {path}"
            )

    @staticmethod
    def _output_root(targets) -> str:
        if not targets:
            return ""
        try:
            root = os.path.commonpath([os.path.abspath(path) for path in targets])
        except ValueError:
            return ""
        if any(os.path.normcase(root) == os.path.normcase(os.path.abspath(path)) for path in targets):
            root = os.path.dirname(root)
        if not root:
            return ""
        return os.path.abspath(root)

    @classmethod
    @contextmanager
    def _output_lock(cls, directory: str, trusted_root: str = ""):
        """生成全程持有输出批次锁；进程退出自动释放，不需要恢复记录。"""
        directory = os.path.abspath(directory)
        anchor = cls._validate_trusted_paths(
            trusted_root or directory, [directory], "输出锁目录"
        )
        os.makedirs(directory, exist_ok=True)
        cls._validate_trusted_paths(anchor, [directory], "输出锁目录")
        lock_path = os.path.join(directory, ".comparetool_output.lock")
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = None
        try:
            fd = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                if os.write(fd, b"0") != 1:
                    raise OSError("无法完整初始化输出锁")
                os.fsync(fd)
            except BaseException:
                os.close(fd)
                fd = None
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
                raise
        except FileExistsError:
            if is_link_or_junction(lock_path):
                raise RuntimeError(f"输出锁不能是链接或联接点: {lock_path}")
            before = os.lstat(lock_path)
            if (
                not stat.S_ISREG(before.st_mode)
                or int(getattr(before, "st_nlink", 1)) != 1
                or before.st_size != 1
            ):
                raise RuntimeError(f"输出锁文件身份无效: {lock_path}")
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(lock_path, flags | nofollow)
        try:
            stream = os.fdopen(fd, "r+b", buffering=0)
            fd = None
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
        try:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or int(getattr(metadata, "st_nlink", 1)) != 1
                or metadata.st_size != 1
            ):
                raise RuntimeError(f"输出锁文件身份无效: {lock_path}")
            handle_identity = regular_file_handle_identity(stream)
            path_identity = regular_file_path_identity(lock_path)
            if handle_identity != path_identity:
                raise RuntimeError(f"输出锁文件在打开期间被替换: {lock_path}")
            cls._validate_trusted_paths(anchor, [lock_path], "输出锁路径")
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, IOError) as exc:
                raise RuntimeError(
                    f"输出目录正在被另一个 CompareTool 实例使用: {directory}"
                ) from exc
            try:
                yield
            finally:
                stream.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            stream.close()

    @staticmethod
    def _remove_path(path: str):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.remove(path)
