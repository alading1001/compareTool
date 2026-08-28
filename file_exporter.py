import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from diff_engine import DiffResult
from logger import warn
from path_safety import is_link_or_junction, safe_join
from stage_ownership import (
    is_owned,
    mark_owned,
    ownership_is_abandoned,
    remove_ownership_marker,
)
from vcs.base import ChangeType


class FileExporter:
    """将变更文件导出到指定目录"""

    TRANSACTION_PREFIX = ".comparetool_transaction_"
    TRANSACTION_SUFFIX = ".json"
    TRANSACTION_KEY_ENV = "COMPARETOOL_TRANSACTION_KEY_FILE"
    TRANSACTION_HMAC_FIELD = "hmac_sha256"
    MAX_TRANSACTION_JOURNAL_BYTES = 64 * 1024
    MAX_TRANSACTION_STATES = 16
    _ORPHAN_STAGE_PATTERNS = (
        re.compile(r"^\.comparetool_stage_[A-Za-z0-9_-]{8,}$"),
        re.compile(r"^\.comparetool_report_[A-Za-z0-9_-]{8,}\.html$"),
        re.compile(r"^\.comparetool_delivery_[A-Za-z0-9_-]{8,}\.txt$"),
    )
    _MULTI_RUN_DIRECTORY_PATTERN = re.compile(
        r"^multi_run_\d{8}_\d{6}_\d{3}(?:_[0-9a-f]{8})?$"
    )

    def __init__(self, diff_result: DiffResult, vcs):
        self.diff_result = diff_result
        self.vcs = vcs

    def export(
        self,
        old_dir: str,
        new_dir: str,
        project_name: str = "",
        targets_are_staging_roots: bool = False,
    ):
        """先在同盘临时目录完整导出，全部成功后再替换目标目录。"""
        pairs = self.prepare_export(
            old_dir,
            new_dir,
            project_name=project_name,
            targets_are_staging_roots=targets_are_staging_roots,
        )
        try:
            self._replace_outputs(pairs)
        finally:
            self.cleanup_stages(pairs)

    def prepare_export(
        self,
        old_dir: str,
        new_dir: str,
        project_name: str = "",
        targets_are_staging_roots: bool = False,
    ):
        """完整写入暂存目录，返回可与报告一起提交的 (stage, target) 列表。"""
        stage_parent = ""
        if project_name:
            old_dir = self._safe_join(old_dir, project_name)
            new_dir = self._safe_join(new_dir, project_name)

        old_dir = os.path.abspath(old_dir)
        new_dir = os.path.abspath(new_dir)
        if os.path.normcase(old_dir) == os.path.normcase(new_dir):
            raise RuntimeError("新旧版本导出目录不能相同")

        if project_name and not targets_are_staging_roots:
            # 单项目的正式目标位于 oldVersion/newVersion/<项目名>，但内部
            # 暂存目录不能也落进 oldVersion/newVersion，否则强退后既会混入
            # 上线包，又无法在不扫描用户源码的前提下安全清理。把两侧随机
            # 暂存根统一放到批次根，stage 本身仍与目标同盘，可原子替换。
            stage_parent = self._transaction_root([old_dir, new_dir])
            if not stage_parent:
                raise RuntimeError("无法确定项目导出的同盘事务暂存目录")

        self._validate_export_paths(old_dir, new_dir)
        old_ver = self.diff_result.old_version
        new_ver = self.diff_result.new_version
        stage_old = ""
        stage_new = ""

        try:
            stage_old = self._make_stage_dir(old_dir, stage_parent=stage_parent)
            stage_new = self._make_stage_dir(new_dir, stage_parent=stage_parent)
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
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # 正式 VCS 使用流式接口，避免大文件在内存中形成完整 bytes 副本。
        stream_export = getattr(self.vcs, "export_file_to_path", None)
        if stream_export is not None:
            try:
                stream_export(version, rel_path, file_path)
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
                key = os.path.normcase(target).casefold()
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

    @staticmethod
    def _make_stage_dir(target_dir: str, stage_parent: str = "") -> str:
        parent = stage_parent or os.path.dirname(target_dir)
        os.makedirs(parent, exist_ok=True)
        stage_root = tempfile.mkdtemp(prefix=".comparetool_stage_", dir=parent)
        try:
            mark_owned(stage_root)
            if not stage_parent:
                return stage_root
            stage = os.path.join(stage_root, os.path.basename(target_dir))
            os.makedirs(stage)
            return stage
        except BaseException:
            shutil.rmtree(stage_root, ignore_errors=True)
            remove_ownership_marker(stage_root)
            raise

    @classmethod
    def _replace_outputs(cls, pairs):
        targets = [os.path.abspath(target) for _stage, target in pairs]
        transaction_root = cls._transaction_root(targets)
        if not transaction_root:
            raise RuntimeError("多个输出目标没有安全的共同事务目录")
        with cls._transaction_lock(transaction_root):
            return cls._replace_outputs_locked(pairs)

    @classmethod
    def _replace_outputs_locked(cls, pairs):
        """成组替换文件或目录；任一步失败时恢复全部原有输出。"""
        token = uuid.uuid4().hex
        states = []
        journal_path = ""
        try:
            target_keys = set()
            normalized_pairs = []
            for stage, target in pairs:
                stage = os.path.abspath(stage)
                target = os.path.abspath(target)
                key = os.path.normcase(target).casefold()
                if key in target_keys:
                    raise RuntimeError(f"事务中存在重复输出目标: {target}")
                if not os.path.lexists(stage):
                    raise RuntimeError(f"输出暂存项不存在: {stage}")
                if is_link_or_junction(stage):
                    raise RuntimeError(f"输出暂存项不能是符号链接或联接点: {stage}")
                if os.path.lexists(target):
                    if is_link_or_junction(target):
                        raise RuntimeError(
                            f"输出目标不能是符号链接或联接点: {target}"
                        )
                    stage_is_dir = os.path.isdir(stage)
                    target_is_dir = os.path.isdir(target)
                    if stage_is_dir != target_is_dir:
                        raise RuntimeError(
                            "输出目标类型与暂存项不一致，拒绝用文件替换目录或用目录替换文件：\n"
                            f"暂存：{stage}\n目标：{target}"
                        )
                target_keys.add(key)
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                normalized_pairs.append((stage, target))

            transaction_root = cls._transaction_root(
                [target for _stage, target in normalized_pairs]
            )
            if transaction_root:
                cls.recover_transactions(
                    transaction_root,
                    raise_on_error=True,
                    protected_stages=[stage for stage, _target in normalized_pairs],
                    acquire_locks=False,
                )

            for stage, target in normalized_pairs:
                backup = f"{target}.comparetool_backup_{token}"
                had_target = os.path.lexists(target)
                states.append({
                    "stage": stage,
                    "target": target,
                    "backup": backup,
                    "had_target": had_target,
                    "installed": False,
                })

            journal_path = cls._create_transaction_journal(states, token)

            for state in states:
                if state["had_target"]:
                    os.replace(state["target"], state["backup"])

            for state in states:
                os.replace(state["stage"], state["target"])
                state["installed"] = True
        except BaseException as original_exc:
            cls._mark_transaction(journal_path, "rollback")
            rollback_errors = []
            for state in reversed(states):
                try:
                    if state["installed"] and os.path.lexists(state["target"]):
                        cls._remove_path(state["target"])
                    if state["had_target"] and os.path.lexists(state["backup"]):
                        os.replace(state["backup"], state["target"])
                except OSError as rollback_exc:
                    rollback_errors.append(f"{state['target']}: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError(
                    "输出提交失败，且自动回滚未完全成功，请保留现场并检查备份目录：\n"
                    + "\n".join(rollback_errors)
                ) from original_exc
            cls._remove_journal(journal_path)
            raise
        else:
            cls._mark_transaction(journal_path, "commit")
            cleanup_failed = False
            for state in states:
                if state["had_target"] and os.path.lexists(state["backup"]):
                    try:
                        cls._remove_path(state["backup"])
                    except OSError as exc:
                        cleanup_failed = True
                        warn(f"输出已提交，但旧备份清理失败: {state['backup']}: {exc}")
            if not cleanup_failed:
                cls._remove_journal(journal_path)

    @classmethod
    def _create_transaction_journal(cls, states, token: str) -> str:
        """在共同输出根目录写入恢复日志；无安全公共根时仍使用当前进程回滚。"""
        targets = [state["target"] for state in states]
        root = cls._transaction_root(targets)
        if not root:
            return ""

        os.makedirs(root, exist_ok=True)
        journal_path = os.path.join(
            root, f"{cls.TRANSACTION_PREFIX}{token}{cls.TRANSACTION_SUFFIX}"
        )
        for state in states:
            owner = cls._stage_owner(state["stage"])
            if not is_owned(owner):
                mark_owned(owner)
        payload = {
            "version": 2,
            "token": token,
            "states": [
                {
                    "stage": state["stage"],
                    "target": state["target"],
                    "backup": state["backup"],
                    "had_target": state["had_target"],
                }
                for state in states
            ],
        }
        key = cls._load_transaction_key(root, create=True)
        payload = cls._signed_payload(payload, key)
        try:
            mark_owned(journal_path)
            with open(journal_path, "x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            cls._remove_journal(journal_path)
            raise
        return journal_path

    @staticmethod
    def _stage_owner(stage: str) -> str:
        stage = os.path.abspath(stage)
        parent = os.path.dirname(stage)
        return parent if os.path.basename(parent).startswith(".comparetool_stage_") else stage

    @staticmethod
    def _transaction_root(targets) -> str:
        if not targets:
            return ""
        try:
            root = os.path.commonpath([os.path.abspath(path) for path in targets])
        except ValueError:
            return ""
        if any(os.path.normcase(root) == os.path.normcase(os.path.abspath(path)) for path in targets):
            root = os.path.dirname(root)
        drive, tail = os.path.splitdrive(os.path.abspath(root))
        if not root or (drive and tail in ("\\", "/")):
            return ""
        return os.path.abspath(root)

    @staticmethod
    def _remove_journal(journal_path: str):
        if not journal_path:
            return
        for path in (journal_path, f"{journal_path}.commit", f"{journal_path}.rollback"):
            if not os.path.isfile(path):
                continue
            try:
                os.remove(path)
            except OSError as exc:
                warn(f"清理输出事务日志失败: {path}: {exc}")
        remove_ownership_marker(journal_path)

    @classmethod
    def _mark_transaction(cls, journal_path: str, decision: str):
        if not journal_path:
            return
        marker = f"{journal_path}.{decision}"
        try:
            token = cls._token_from_journal_path(journal_path)
            key = cls._load_transaction_key(os.path.dirname(journal_path), create=False)
            payload = cls._signed_payload({
                "version": 1,
                "token": token,
                "decision": decision,
            }, key)
            with open(marker, "x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return
        except (OSError, RuntimeError, ValueError) as exc:
            warn(f"写入输出事务 {decision} 标记失败: {marker}: {exc}")

    @classmethod
    def _transaction_key_path(cls) -> str:
        configured = os.environ.get(cls.TRANSACTION_KEY_ENV, "").strip()
        if configured:
            return os.path.abspath(os.path.expanduser(os.path.expandvars(configured)))
        if os.name == "nt":
            base = (
                os.environ.get("LOCALAPPDATA")
                or os.environ.get("APPDATA")
                or os.path.expanduser("~")
            )
        else:
            base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
                os.path.expanduser("~"), ".config"
            )
        return os.path.abspath(os.path.join(base, "CompareTool", "transaction_hmac.key"))

    @classmethod
    def _load_transaction_key(cls, transaction_root: str, create: bool) -> bytes:
        root = os.path.abspath(transaction_root)
        key_path = cls._transaction_key_path()
        root_real = os.path.realpath(root)
        key_real = os.path.realpath(key_path)
        try:
            inside = os.path.commonpath([root, key_path]) == root
            real_inside = os.path.commonpath([root_real, key_real]) == root_real
        except ValueError:
            inside = False
            real_inside = False
        if inside or real_inside:
            raise RuntimeError("输出事务签名私钥不能位于输出目录内")

        if create:
            parent = os.path.dirname(key_path) or "."
            os.makedirs(parent, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_BINARY", 0)
            fd = None
            created = False
            try:
                fd = os.open(key_path, flags, 0o600)
                created = True
                key = secrets.token_bytes(32)
                offset = 0
                while offset < len(key):
                    offset += os.write(fd, key[offset:])
                os.fsync(fd)
            except FileExistsError:
                pass
            except BaseException:
                if fd is not None:
                    os.close(fd)
                    fd = None
                if created:
                    try:
                        os.remove(key_path)
                    except OSError:
                        pass
                raise
            finally:
                if fd is not None:
                    os.close(fd)

        if not os.path.exists(key_path):
            raise RuntimeError("输出事务签名私钥不存在，无法安全恢复")
        if is_link_or_junction(key_path):
            raise RuntimeError("输出事务签名私钥不能是链接或联接点")
        metadata = os.lstat(key_path)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("输出事务签名私钥不是普通文件")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError("输出事务签名私钥权限过宽，必须仅当前用户可读写")
        with open(key_path, "rb") as stream:
            key = stream.read(33)
        if len(key) != 32:
            raise RuntimeError("输出事务签名私钥已损坏")
        return key

    @classmethod
    def _signed_payload(cls, payload: dict, key: bytes) -> dict:
        unsigned = dict(payload)
        unsigned.pop(cls.TRANSACTION_HMAC_FIELD, None)
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signed = dict(unsigned)
        signed[cls.TRANSACTION_HMAC_FIELD] = hmac.new(
            key, canonical, hashlib.sha256
        ).hexdigest()
        return signed

    @classmethod
    def _verify_signed_payload(cls, payload: dict, key: bytes) -> bool:
        if not isinstance(payload, dict):
            return False
        supplied = payload.get(cls.TRANSACTION_HMAC_FIELD)
        if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
            return False
        expected = cls._signed_payload(payload, key)[cls.TRANSACTION_HMAC_FIELD]
        return hmac.compare_digest(supplied, expected)

    @classmethod
    def _token_from_journal_path(cls, journal_path: str) -> str:
        name = os.path.basename(journal_path)
        match = re.fullmatch(
            re.escape(cls.TRANSACTION_PREFIX)
            + r"([0-9a-f]{32})"
            + re.escape(cls.TRANSACTION_SUFFIX),
            name,
        )
        if not match:
            raise ValueError("事务日志标识无效")
        return match.group(1)

    @classmethod
    def _read_decision_marker(
        cls, journal_path: str, token: str, decision: str, key: bytes
    ) -> bool:
        marker = f"{journal_path}.{decision}"
        if not os.path.exists(marker):
            return False
        if not os.path.isfile(marker) or os.path.getsize(marker) > 4096:
            raise RuntimeError(f"输出事务 {decision} 决策标记签名无效")
        try:
            with open(marker, encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"输出事务 {decision} 决策标记签名无效") from exc
        if (
            not cls._verify_signed_payload(payload, key)
            or payload.get("version") != 1
            or payload.get("token") != token
            or payload.get("decision") != decision
        ):
            raise RuntimeError(f"输出事务 {decision} 决策标记签名无效")
        return True

    @staticmethod
    @contextmanager
    def _transaction_lock(directory: str):
        """对一个输出批次加非阻塞进程锁，避免两个实例互相恢复/覆盖。"""
        os.makedirs(directory, exist_ok=True)
        lock_path = os.path.join(directory, ".comparetool_transaction.lock")
        stream = open(lock_path, "a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
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

    @classmethod
    def recover_transactions(
        cls,
        output_root: str,
        include_direct_children: bool = False,
        raise_on_error: bool = False,
        protected_stages=None,
        acquire_locks: bool = True,
        include_nested_multi_runs: bool = False,
    ):
        """恢复上次非正常中断的输出事务。"""
        if not output_root:
            return []
        root = os.path.abspath(output_root)
        if not os.path.isdir(root) or os.path.islink(root):
            return []
        directories = [root]
        direct_children = []
        if include_direct_children:
            try:
                direct_children = [
                    entry.path for entry in os.scandir(root)
                    if (
                        entry.is_dir(follow_symlinks=False)
                        and entry.name.casefold() not in ("oldversion", "newversion")
                        and not is_link_or_junction(entry.path)
                    )
                ]
                directories.extend(direct_children)
            except OSError as exc:
                warn(f"扫描输出事务日志失败: {root}: {exc}")
        if include_nested_multi_runs:
            # 配置输出根下可能先有批次目录，再有本次独立的 multi_run 目录。
            # 只多扫这一层且严格校验内部目录名，绝不递归普通项目/源码树。
            for child in direct_children:
                try:
                    directories.extend(
                        entry.path for entry in os.scandir(child)
                        if (
                            entry.is_dir(follow_symlinks=False)
                            and cls._MULTI_RUN_DIRECTORY_PATTERN.fullmatch(entry.name)
                            and not is_link_or_junction(entry.path)
                        )
                    )
                except OSError as exc:
                    warn(f"扫描多项目输出事务日志失败: {child}: {exc}")

        recovered = []
        failures = []
        protected = {
            os.path.normcase(os.path.abspath(path))
            for path in (protected_stages or [])
        }
        if acquire_locks:
            directories = [
                directory for directory in directories
                if cls._has_recovery_candidates(directory)
            ]
            for directory in directories:
                try:
                    with cls._transaction_lock(directory):
                        recovered.extend(cls.recover_transactions(
                            directory,
                            include_direct_children=False,
                            include_nested_multi_runs=False,
                            raise_on_error=raise_on_error,
                            protected_stages=protected_stages,
                            acquire_locks=False,
                        ))
                except RuntimeError as exc:
                    if raise_on_error:
                        raise
                    warn(str(exc))
            return recovered
        for directory in directories:
            directory_failed = False
            try:
                names = os.listdir(directory)
            except OSError as exc:
                warn(f"读取输出事务目录失败: {directory}: {exc}")
                continue
            for name in names:
                if not (
                    name.startswith(cls.TRANSACTION_PREFIX) and
                    name.endswith(cls.TRANSACTION_SUFFIX)
                ):
                    continue
                journal_path = os.path.join(directory, name)
                try:
                    cls._recover_transaction_journal(journal_path, directory)
                    recovered.append(journal_path)
                except Exception as exc:
                    warn(f"恢复输出事务失败，已保留日志: {journal_path}: {exc}")
                    failures.append(f"{journal_path}: {exc}")
                    directory_failed = True
            if not directory_failed:
                cls._cleanup_orphan_stages(directory, protected)
        if failures and raise_on_error:
            raise RuntimeError(
                "存在无法自动恢复的上次输出事务，已中止新提交：\n"
                + "\n".join(failures)
            )
        return recovered

    @classmethod
    def _has_recovery_candidates(cls, directory: str) -> bool:
        """只读识别 CompareTool 自有恢复物，避免扫描时污染普通目录。"""
        try:
            entries = os.scandir(directory)
        except OSError:
            return False
        with entries:
            for entry in entries:
                is_journal = (
                    entry.name.startswith(cls.TRANSACTION_PREFIX)
                    and entry.name.endswith(cls.TRANSACTION_SUFFIX)
                )
                is_stage = any(
                    pattern.fullmatch(entry.name)
                    for pattern in cls._ORPHAN_STAGE_PATTERNS
                )
                if (is_journal or is_stage) and is_owned(entry.path):
                    return True
        return False

    @classmethod
    def _cleanup_orphan_stages(cls, directory: str, protected=None):
        """清理尚未创建事务日志就强退留下的内部暂存物。"""
        protected = protected or set()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            warn(f"扫描遗留输出暂存物失败: {directory}: {exc}")
            return
        for entry in entries:
            if not any(pattern.fullmatch(entry.name) for pattern in cls._ORPHAN_STAGE_PATTERNS):
                continue
            if not is_owned(entry.path):
                warn(f"跳过没有 CompareTool 所有权标记的同名前缀路径: {entry.path}")
                continue
            if not ownership_is_abandoned(entry.path):
                warn(f"跳过仍由存活 CompareTool 进程持有的暂存路径: {entry.path}")
                continue
            entry_path = os.path.normcase(os.path.abspath(entry.path))
            protects_current_work = entry_path in protected
            if not protects_current_work:
                for protected_path in protected:
                    try:
                        if os.path.commonpath([entry_path, protected_path]) == entry_path:
                            protects_current_work = True
                            break
                    except ValueError:
                        continue
            if protects_current_work:
                continue
            if is_link_or_junction(entry.path):
                warn(f"跳过疑似遗留但实际为链接的暂存路径: {entry.path}")
                continue
            try:
                cls._remove_path(entry.path)
                remove_ownership_marker(entry.path)
                warn(f"已清理上次强退遗留的输出暂存物: {entry.path}")
            except OSError as exc:
                warn(f"清理遗留输出暂存物失败: {entry.path}: {exc}")

    @classmethod
    def _recover_transaction_journal(cls, journal_path: str, root: str):
        if not is_owned(journal_path):
            raise RuntimeError("事务日志缺少 CompareTool 所有权标记")
        if (
            not os.path.isfile(journal_path)
            or os.path.getsize(journal_path) > cls.MAX_TRANSACTION_JOURNAL_BYTES
        ):
            raise RuntimeError("事务日志过大或不是普通文件")
        try:
            with open(journal_path, encoding="utf-8") as stream:
                raw_payload = stream.read(cls.MAX_TRANSACTION_JOURNAL_BYTES + 1)
            payload = json.loads(raw_payload)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("事务日志无法解析") from exc

        root = os.path.abspath(root)
        key = cls._load_transaction_key(root, create=False)
        if not cls._verify_signed_payload(payload, key):
            raise RuntimeError("事务日志缺少有效 HMAC 签名，已保留现场")
        if payload.get("version") != 2 or not isinstance(payload.get("states"), list):
            raise RuntimeError("事务日志格式不支持")
        if len(payload["states"]) > cls.MAX_TRANSACTION_STATES:
            raise RuntimeError("事务日志状态过多")

        token = payload.get("token", "")
        expected_name = f"{cls.TRANSACTION_PREFIX}{token}{cls.TRANSACTION_SUFFIX}"
        if not re.fullmatch(r"[0-9a-f]{32}", str(token)) or os.path.basename(journal_path) != expected_name:
            raise RuntimeError("事务日志标识不一致")

        root_real = os.path.realpath(root)
        states = []
        for raw in payload["states"]:
            if not isinstance(raw, dict):
                raise RuntimeError("事务日志状态无效")
            stage = os.path.abspath(raw.get("stage", ""))
            target = os.path.abspath(raw.get("target", ""))
            backup = os.path.abspath(raw.get("backup", ""))
            try:
                inside = os.path.commonpath([root, stage, target, backup]) == root
                real_inside = os.path.commonpath([
                    root_real,
                    os.path.realpath(stage),
                    os.path.realpath(target),
                    os.path.realpath(backup),
                ]) == root_real
            except ValueError:
                inside = False
                real_inside = False
            stage_name = os.path.basename(stage)
            stage_parent_name = os.path.basename(os.path.dirname(stage))
            valid_stage_layout = (
                os.path.dirname(stage) == os.path.dirname(target) and
                stage_name.startswith((
                    ".comparetool_stage_",
                    ".comparetool_report_",
                    ".comparetool_delivery_",
                ))
            ) or (
                stage_parent_name.startswith(".comparetool_stage_") and
                os.path.normcase(stage_name).casefold() ==
                os.path.normcase(os.path.basename(target)).casefold()
            )
            stage_owner = (
                os.path.dirname(stage)
                if stage_parent_name.startswith(".comparetool_stage_")
                else stage
            )
            has_link = any(
                os.path.lexists(path) and is_link_or_junction(path)
                for path in (stage, target, backup)
            )
            if (
                not inside
                or not real_inside
                or not valid_stage_layout
                or not is_owned(stage_owner)
                or has_link
            ):
                raise RuntimeError("事务日志路径越界")
            if backup != os.path.abspath(f"{target}.comparetool_backup_{token}"):
                raise RuntimeError("事务备份路径无效")
            states.append({
                "stage": stage,
                "target": target,
                "backup": backup,
                "had_target": bool(raw.get("had_target")),
            })

        commit_marker = cls._read_decision_marker(
            journal_path, token, "commit", key
        )
        rollback_marker = cls._read_decision_marker(
            journal_path, token, "rollback", key
        )
        if commit_marker and rollback_marker:
            raise RuntimeError("事务决策标记冲突")
        inferred_commit = bool(states) and all(
            os.path.lexists(state["target"]) and not os.path.lexists(state["stage"])
            for state in states
        )
        committed = commit_marker or (not rollback_marker and inferred_commit)
        if committed:
            missing = [state["target"] for state in states if not os.path.lexists(state["target"])]
            if missing:
                raise RuntimeError("已提交事务缺少正式输出: " + ", ".join(missing))
            for state in states:
                if os.path.lexists(state["backup"]):
                    cls._remove_path(state["backup"])
        else:
            for state in reversed(states):
                if os.path.lexists(state["backup"]):
                    if os.path.lexists(state["target"]):
                        cls._remove_path(state["target"])
                    os.replace(state["backup"], state["target"])
                elif (
                    not state["had_target"] and
                    os.path.lexists(state["target"]) and
                    not os.path.lexists(state["stage"])
                ):
                    cls._remove_path(state["target"])
                if os.path.lexists(state["stage"]):
                    cls._remove_path(state["stage"])
        for state in states:
            stage_parent = os.path.dirname(state["stage"])
            if os.path.basename(stage_parent).startswith(".comparetool_stage_"):
                try:
                    os.rmdir(stage_parent)
                except OSError:
                    pass
                remove_ownership_marker(stage_parent)
            else:
                remove_ownership_marker(state["stage"])
        cls._remove_journal(journal_path)

    @staticmethod
    def _remove_path(path: str):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.remove(path)
