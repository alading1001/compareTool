import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Set
from urllib.parse import quote, unquote
from xml.etree import ElementTree

from path_safety import safe_join
from .base import BaseVCS, ChangedFile, ChangeType
from .folder_vcs import FolderVCS
from .git_vcs import GitVCS, GIT_NOT_FOUND_MESSAGE
from .svn_vcs import SVNVCS, SVN_NOT_FOUND_MESSAGE
from .temp_storage import create_temp_dir, remove_temp_dir
from logger import warn


def parse_multi_versions(raw: str) -> List[str]:
    """解析用户输入的多个版本号，支持逗号、分号、换行分隔。"""
    result = []
    seen = set()
    for part in re.split(r"[,;\r\n]+", raw or ""):
        token = part.strip().split()[0] if part.strip() else ""
        if token and token not in seen:
            result.append(token)
            seen.add(token)
    return result


def _remove_tree(path: str):
    if os.path.isdir(path):
        try:
            remove_temp_dir(path)
        except OSError as exc:
            warn(f"清理多版本端点临时目录失败，已保留现场: {path}: {exc}")


@dataclass(frozen=True)
class _HistoryChange:
    action: str
    path: str
    old_path: str = ""


@dataclass(eq=False)
class _LogicalFile:
    number: int
    selected: bool = False
    old_version: str = ""
    old_path: Optional[str] = None
    new_version: str = ""
    new_path: Optional[str] = None
    last_selected_step: int = 0


class _EndpointPlanner:
    """沿线性历史追踪逻辑文件，并记录每个文件自己的选中首尾端点。"""

    def __init__(self):
        self._active: Dict[str, _LogicalFile] = {}
        self._deleted: Dict[str, _LogicalFile] = {}
        self._entities: List[_LogicalFile] = []
        self._step = 0

    def _new_entity(self) -> _LogicalFile:
        entity = _LogicalFile(len(self._entities) + 1)
        self._entities.append(entity)
        return entity

    def apply(
        self,
        changes: List[_HistoryChange],
        selected: bool,
        old_version: str,
        new_version: str,
    ):
        self._step += 1
        step = self._step
        before = {entity: path for path, entity in self._active.items()}
        implicit_before: Dict[_LogicalFile, str] = {}
        touched: Set[_LogicalFile] = set()
        resolved = []

        for change in changes:
            action = change.action
            path = self._normalize_path(change.path)
            old_path = self._normalize_path(change.old_path) if change.old_path else ""

            if action == "R":
                entity = self._active.pop(old_path, None)
                if entity is None:
                    entity = self._deleted.pop(old_path, None)
                if entity is None:
                    entity = self._new_entity()
                    implicit_before[entity] = old_path
                existing = self._active.get(path)
                if existing is not None and existing is not entity:
                    raise RuntimeError(
                        "多版本历史出现无法唯一追踪的重命名目标，已中止生成：\n"
                        f"{old_path} -> {path}"
                    )
                self._active[path] = entity
                self._deleted.pop(path, None)
                touched.add(entity)
                resolved.append((change, entity, entity.selected))
                continue

            if action == "D":
                entity = self._active.pop(path, None)
                if entity is None:
                    entity = self._deleted.get(path)
                if entity is None:
                    entity = self._new_entity()
                    implicit_before[entity] = path
                self._deleted[path] = entity
                touched.add(entity)
                resolved.append((change, entity, entity.selected))
                continue

            if action == "A":
                entity = self._active.get(path)
                if entity is None:
                    # 同一路径删除后重建仍视为同一逻辑文件，以便计算最终净结果。
                    entity = self._deleted.pop(path, None) or self._new_entity()
                self._active[path] = entity
                touched.add(entity)
                resolved.append((change, entity, entity.selected))
                continue

            if action == "M":
                entity = self._active.get(path)
                if entity is None:
                    entity = self._deleted.pop(path, None)
                if entity is None:
                    entity = self._new_entity()
                    implicit_before[entity] = path
                self._active[path] = entity
                touched.add(entity)
                resolved.append((change, entity, entity.selected))
                continue

            raise RuntimeError(f"暂不支持的多版本历史变更类型 {action}: {path}")

        if not selected:
            return resolved

        after = {entity: path for path, entity in self._active.items()}
        for entity in touched:
            if not entity.selected:
                entity.selected = True
                entity.old_version = old_version
                entity.old_path = before.get(entity, implicit_before.get(entity))
            entity.new_version = new_version
            entity.new_path = after.get(entity)
            entity.last_selected_step = step
        return resolved

    @property
    def selected_entities(self) -> List[_LogicalFile]:
        return [entity for entity in self._entities if entity.selected]

    def is_deleted(self, path: str) -> bool:
        return self._normalize_path(path) in self._deleted

    def is_active(self, path: str) -> bool:
        return self._normalize_path(path) in self._active

    def has_deleted_under(self, directory: str) -> bool:
        prefix = self._normalize_path(directory).rstrip("/") + "/"
        return any(path.startswith(prefix) for path in self._deleted)

    @property
    def current_step(self) -> int:
        return self._step

    @staticmethod
    def _normalize_path(path: str) -> str:
        value = (path or "").replace("\\", "/").strip("/")
        if not value or value in (".", "..") or value.startswith("../"):
            raise RuntimeError(f"版本历史包含无效文件路径: {path}")
        return value


class _MultiVersionFolderDelegate(BaseVCS):
    """多版本文件端点公共实现，报告与导出统一读取端点快照。"""

    # 文件身份已经由历史规划器判定，不能再按内容启发式合并删除和新增。
    merge_exact_renames = False

    def __init__(self, source_project_path: str, selected_versions: List[str], prefix: str):
        if not selected_versions:
            raise ValueError("请选择至少一个版本")
        self.source_project_path = source_project_path
        self.selected_versions = selected_versions
        self.selected_versions_label = ", ".join(selected_versions)
        self.old_version_label = self.selected_versions_label
        self.new_version_label = "文件级首尾端点"
        self._tmp_root = create_temp_dir(prefix=prefix)
        self._old_dir = os.path.join(self._tmp_root, "old")
        self._new_dir = os.path.join(self._tmp_root, "new")
        self._old_raw_dir = os.path.join(self._tmp_root, "old_raw")
        self._new_raw_dir = os.path.join(self._tmp_root, "new_raw")
        self._folder: Optional[FolderVCS] = None
        self._raw_folder: Optional[FolderVCS] = None
        self._planned_files: List[ChangedFile] = []
        super().__init__(self._new_dir)

    def _finish_plan(self, entities: List[_LogicalFile], content_getter, raw_content_getter):
        os.makedirs(self._old_dir, exist_ok=True)
        os.makedirs(self._new_dir, exist_ok=True)
        os.makedirs(self._old_raw_dir, exist_ok=True)
        os.makedirs(self._new_raw_dir, exist_ok=True)
        old_targets: Dict[str, str] = {}
        new_targets: Dict[str, str] = {}
        old_raw_targets: Dict[str, str] = {}
        new_raw_targets: Dict[str, str] = {}

        ordered = sorted(
            entities,
            key=lambda item: ((item.new_path or item.old_path or "").casefold(), item.number),
        )
        for entity in ordered:
            old_data = self._read_endpoint(
                content_getter, entity.old_version, entity.old_path, "旧版本"
            )
            new_data = self._read_endpoint(
                content_getter, entity.new_version, entity.new_path, "新版本"
            )
            old_raw = self._read_endpoint(
                raw_content_getter, entity.old_version, entity.old_path, "旧版本原始字节"
            )
            new_raw = self._read_endpoint(
                raw_content_getter, entity.new_version, entity.new_path, "新版本原始字节"
            )
            metadata = self._compare_endpoint_metadata(
                entity.old_version,
                entity.old_path,
                entity.new_version,
                entity.new_path,
            )
            metadata_changes = list(metadata.get("changes", []))
            metadata_kwargs = {
                "metadata_changes": metadata_changes,
                "old_executable": metadata.get("old_executable"),
                "new_executable": metadata.get("new_executable"),
                "old_mode": metadata.get("old_mode", ""),
                "new_mode": metadata.get("new_mode", ""),
            }

            if entity.old_path is None and entity.new_path is None:
                continue
            if (
                entity.old_path is not None
                and entity.new_path is not None
                and entity.old_path == entity.new_path
                and old_data == new_data
                and old_raw == new_raw
                and not metadata_changes
            ):
                continue

            if entity.old_path is None:
                self._write_endpoint(self._new_dir, entity.new_path, new_data, new_targets)
                self._write_endpoint(
                    self._new_raw_dir, entity.new_path, new_raw, new_raw_targets
                )
                self._planned_files.append(ChangedFile(
                    entity.new_path, ChangeType.ADDED, **metadata_kwargs
                ))
            elif entity.new_path is None:
                self._write_endpoint(self._old_dir, entity.old_path, old_data, old_targets)
                self._write_endpoint(
                    self._old_raw_dir, entity.old_path, old_raw, old_raw_targets
                )
                self._planned_files.append(ChangedFile(
                    entity.old_path, ChangeType.DELETED, **metadata_kwargs
                ))
            elif entity.old_path != entity.new_path:
                self._write_endpoint(self._old_dir, entity.old_path, old_data, old_targets)
                self._write_endpoint(self._new_dir, entity.new_path, new_data, new_targets)
                self._write_endpoint(
                    self._old_raw_dir, entity.old_path, old_raw, old_raw_targets
                )
                self._write_endpoint(
                    self._new_raw_dir, entity.new_path, new_raw, new_raw_targets
                )
                self._planned_files.append(ChangedFile(
                    entity.new_path,
                    ChangeType.RENAMED,
                    old_path=entity.old_path,
                    **metadata_kwargs,
                ))
            else:
                self._write_endpoint(self._old_dir, entity.old_path, old_data, old_targets)
                self._write_endpoint(self._new_dir, entity.new_path, new_data, new_targets)
                self._write_endpoint(
                    self._old_raw_dir, entity.old_path, old_raw, old_raw_targets
                )
                self._write_endpoint(
                    self._new_raw_dir, entity.new_path, new_raw, new_raw_targets
                )
                self._planned_files.append(ChangedFile(
                    entity.new_path, ChangeType.MODIFIED, **metadata_kwargs
                ))

        self._folder = FolderVCS(self._old_dir, self._new_dir, snapshot=False)
        self._raw_folder = FolderVCS(
            self._old_raw_dir, self._new_raw_dir, snapshot=False
        )

    @staticmethod
    def _read_endpoint(content_getter, version: str, path: Optional[str], label: str):
        if path is None:
            return None
        data = content_getter(version, path)
        if data is None:
            raise RuntimeError(f"无法读取{label}文件端点，已中止生成: {path}@{version}")
        return data

    def _compare_endpoint_metadata(
            self, old_version, old_path, new_version, new_path) -> dict:
        return {}

    @staticmethod
    def _write_endpoint(base_dir: str, path: str, data: bytes, targets: Dict[str, str]):
        key = os.path.normcase(path.replace("\\", "/")).casefold()
        if key in targets:
            raise RuntimeError(
                "不同文件端点会写入同一 Windows 路径，已中止生成：\n"
                f"{targets[key]}\n{path}"
            )
        targets[key] = path
        try:
            target = safe_join(base_dir, path, label="多版本端点路径")
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(data)

    def set_exclude_patterns(self, patterns: List[str]):
        super().set_exclude_patterns(patterns)
        if self._folder:
            self._folder.set_exclude_patterns(patterns)
        if self._raw_folder:
            self._raw_folder.set_exclude_patterns(patterns)

    def _to_folder_ver(self, version: str) -> str:
        # GUI/旧配置中的多选版本可能用换行、逗号或分号保存，不能依赖
        # selected_versions_label 的格式来识别 old。多版本模式的新端点标签是固定的，
        # 因此只显式识别 new，其余调用方传入的选中版本表达式都应读取 old。
        if version in ("new", self.new_version_label):
            return "new"
        return "old"

    def get_changed_files(self, old_version: str = "", new_version: str = "") -> List[ChangedFile]:
        return self._filter_files(list(self._planned_files))

    def get_file_content(self, version: str, file_path: str) -> str:
        return self._folder.get_file_content(self._to_folder_ver(version), file_path)

    def get_file_content_bytes(self, version: str, file_path: str) -> bytes:
        return self._folder.get_file_content_bytes(self._to_folder_ver(version), file_path)

    def get_file_content_raw_bytes(self, version: str, file_path: str) -> bytes:
        return self._raw_folder.get_file_content_raw_bytes(
            self._to_folder_ver(version), file_path
        )

    def get_file_content_working(self, file_path: str) -> str:
        return self._folder.get_file_content_working(file_path)

    def get_file_content_bytes_working(self, file_path: str) -> bytes:
        return self._folder.get_file_content_bytes_working(file_path)

    def get_versions(self) -> List[str]:
        return []

    def check_version_exists(self, version: str) -> bool:
        return True

    def cleanup(self):
        _remove_tree(self._tmp_root)

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass


class GitMultiVersionVCS(_MultiVersionFolderDelegate):
    """Git 多版本：按当前分支第一父历史生成每个文件自己的首尾端点。"""

    def __init__(self, project_path: str, selected_versions: List[str]):
        self._git_exe = GitVCS._find_git()
        self._content_vcs = GitVCS(project_path)
        self._content_vcs._git = self._git_exe
        self._git_mode_cache = {}
        super().__init__(project_path, selected_versions, "comparetool_git_multi_")
        try:
            self._prepare()
        except Exception:
            self.cleanup()
            raise

    @staticmethod
    def _run(args: list, cwd: str) -> bytes:
        try:
            result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=600)
        except FileNotFoundError:
            raise RuntimeError(GIT_NOT_FOUND_MESSAGE)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            stdout = result.stdout.decode("utf-8", errors="replace")
            raise RuntimeError(f"Git命令失败: {' '.join(args)}\n{stderr or stdout}")
        return result.stdout

    def _git_bytes(self, *args: str) -> bytes:
        return self._run([self._git_exe] + list(args), self.source_project_path)

    def _git(self, *args: str) -> str:
        return self._git_bytes(*args).decode("utf-8", errors="replace").strip()

    @staticmethod
    def get_recent_versions(project_path: str, limit: int = 100) -> List[str]:
        git = GitVCS._find_git()
        try:
            result = subprocess.run(
                [git, "log", "--first-parent", f"-{limit}", "--format=%h%x09%p%x09%s"],
                cwd=project_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except FileNotFoundError:
            raise RuntimeError(GIT_NOT_FOUND_MESSAGE)
        if result.returncode != 0:
            raise RuntimeError(f"Git命令失败: git log\n{result.stderr}")
        versions = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            short_hash = parts[0].strip()
            parents = parts[1].split() if len(parts) > 1 else []
            subject = parts[2].strip() if len(parts) > 2 else ""
            marker = " [merge]" if len(parents) > 1 else ""
            versions.append(f"{short_hash}{marker} {subject}".strip())
        return versions

    def _prepare(self):
        history = self._git("rev-list", "--first-parent", "HEAD").splitlines()
        if not history:
            raise RuntimeError("当前 Git 分支没有可用提交")
        history_index = {commit: index for index, commit in enumerate(history)}

        resolved = []
        seen = set()
        for raw in self.selected_versions:
            commit = self._git("rev-parse", "--verify", f"{raw}^{{commit}}")
            if commit not in history_index:
                raise RuntimeError(f"提交不在当前分支第一父历史中: {raw}")
            if commit not in seen:
                resolved.append(commit)
                seen.add(commit)

        earliest_index = max(history_index[commit] for commit in resolved)
        latest_index = min(history_index[commit] for commit in resolved)
        linear_history = list(reversed(history[latest_index:earliest_index + 1]))
        selected = set(resolved)
        planner = _EndpointPlanner()
        ambiguous_candidate_sets = []
        pending_deletes = []
        self._git_rename_candidate_cache = {}
        for commit in linear_history:
            parent = self._first_parent(commit)
            changes, ambiguous_path_pairs = self._changes_for_commit(commit, parent)
            resolved_changes = planner.apply(
                changes, commit in selected, parent, commit
            )
            if ambiguous_path_pairs:
                sources = {}
                targets = {}
                for change, entity, was_selected in resolved_changes:
                    if change.action == "D":
                        sources[change.path] = (entity, was_selected)
                    elif change.action == "A":
                        targets[change.path] = entity
                    elif change.action == "R":
                        sources[change.old_path] = (entity, was_selected)
                        targets[change.path] = entity
                    elif change.action == "M":
                        targets[change.path] = entity
                candidates = []
                for source_path, target_path in ambiguous_path_pairs:
                    if source_path in sources and target_path in targets:
                        source_entity, was_selected = sources[source_path]
                        candidates.append((
                            source_path,
                            source_entity,
                            was_selected,
                            target_path,
                            targets[target_path],
                        ))
                if candidates:
                    ambiguous_candidate_sets.append(
                        (commit, planner.current_step, False, candidates)
                    )

            resolved_deletes = []
            resolved_targets = []
            resolved_add_paths = set()
            for change, entity, was_selected in resolved_changes:
                if change.action == "D":
                    resolved_deletes.append(
                        (change.path, entity, was_selected, parent)
                    )
                elif change.action == "A":
                    resolved_targets.append((change.path, entity))
                    resolved_add_paths.add(change.path)
                elif change.action in ("R", "M"):
                    resolved_targets.append((change.path, entity))

            # Git 不保存“跨提交移动”元数据。若文件先在一个未选提交删除、
            # 后在另一个提交以相同或高相似内容新增，直接当两个实体会让
            # newVersion 留下旧路径。这里保留竞争候选，只有候选两侧后来都
            # 关联选中变更时才 fail closed，普通无关删除/新增不受影响。
            for target_path, target_entity in resolved_targets:
                candidates = []
                for (
                    source_path,
                    source_entity,
                    source_was_selected,
                    source_version,
                ) in pending_deletes:
                    if self._git_rename_candidate(
                        source_version, source_path, commit, target_path
                    ):
                        candidates.append((
                            source_path,
                            source_entity,
                            source_was_selected,
                            target_path,
                            target_entity,
                        ))
                if candidates:
                    ambiguous_candidate_sets.append(
                        (commit, planner.current_step, True, candidates)
                    )

            if resolved_add_paths:
                pending_deletes = [
                    item
                    for item in pending_deletes
                    if item[0] not in resolved_add_paths
                ]
            pending_deletes.extend(resolved_deletes)

        suspicious = []
        for commit, event_step, include_same_step, candidates in ambiguous_candidate_sets:
            ambiguous_pairs = [
                (source_path, target_path)
                for (
                    source_path,
                    source_entity,
                    was_selected,
                    target_path,
                    target_entity,
                ) in candidates
                if (
                    was_selected
                    and (
                        target_entity.last_selected_step > event_step
                        or (
                            include_same_step
                            and target_entity.last_selected_step == event_step
                        )
                    )
                    and source_entity is not target_entity
                )
            ]
            if ambiguous_pairs:
                pair_lines = "\n".join(
                    f"  {source_path} -> {target_path}"
                    for source_path, target_path in ambiguous_pairs
                )
                suspicious.append(
                    f"提交 {commit}\n{pair_lines}"
                )
        if suspicious:
            raise RuntimeError(
                "Git 检测到无法唯一确认的删除/新增/重命名候选，且候选两侧都"
                "关联选中变更，无法排除彻底改写或同内容文件造成的错误配对。"
                "本次未生成报告和源码包。\n"
                + "\n".join(suspicious)
            )

        self._finish_plan(
            planner.selected_entities,
            self._read_git_endpoint,
            self._read_git_raw_endpoint,
        )

    def _first_parent(self, commit: str) -> str:
        # rev-list 会把浅克隆边界伪装成根提交；直接读取 commit 对象才能看到
        # 真实 parent。父对象缺失时必须失败，不能把整棵树误报为新增。
        payload = self._git_bytes("cat-file", "-p", commit).decode(
            "utf-8", errors="replace"
        )
        parent = ""
        for line in payload.splitlines():
            if line.startswith("parent "):
                parent = line.split(" ", 1)[1].strip()
                break
            if not line:
                break
        if not parent:
            return ""
        try:
            self._git("cat-file", "-e", f"{parent}^{{commit}}")
        except RuntimeError as exc:
            raise RuntimeError(
                f"Git 仓库缺少提交 {commit} 的第一父对象 {parent}，"
                "可能是浅克隆边界；请补全历史后重试。"
            ) from exc
        return parent

    def _changes_for_commit(self, commit: str, parent: str):
        reliable = self._diff_changes(commit, parent, "50%")
        permissive = self._diff_changes(commit, parent, "1%")
        reliable_renames = {(item.old_path, item.path) for item in reliable if item.action == "R"}
        low_similarity_pairs = {
            (item.old_path, item.path)
            for item in permissive
            if item.action == "R"
            and (item.old_path, item.path) not in reliable_renames
        }
        deletes = [item for item in reliable if item.action == "D"]
        adds = [item for item in reliable if item.action == "A"]
        renames = [item for item in reliable if item.action == "R"]
        modifications = [item for item in reliable if item.action == "M"]
        ambiguous_pairs = {
            (deleted.path, added.path)
            for deleted in deletes
            for added in adds
        } | low_similarity_pairs

        # 低阈值只负责补充“可能是同一身份”的候选，不能在这里全局失败。
        # 下面的规划器会继续解析 source/target 对应的逻辑文件，只有候选身份
        # 真正跨越选中端点时才 fail closed；完全无关的中间文件不得阻断生成。

        # Git 的 rename 是快照相似度推断，不保存真实移动元数据。额外
        # source/target 若被 Git 自己判为 rename，也可能是全局匹配选错的
        # 竞争候选。必须复用 Git 原生 score，不能用另一套文本相似算法。
        for deleted in deletes:
            for renamed in renames:
                if self._git_rename_candidate(
                    parent, deleted.path, commit, renamed.path
                ):
                    ambiguous_pairs.add((deleted.path, renamed.path))
        for renamed in renames:
            for added in adds:
                if self._git_rename_candidate(
                    parent, renamed.old_path, commit, added.path
                ):
                    ambiguous_pairs.add((renamed.old_path, added.path))
        for deleted in deletes:
            for modified in modifications:
                if self._git_rename_candidate(
                    parent, deleted.path, commit, modified.path
                ):
                    ambiguous_pairs.add((deleted.path, modified.path))
        for renamed in renames:
            for modified in modifications:
                if self._git_rename_candidate(
                    parent, renamed.old_path, commit, modified.path
                ):
                    ambiguous_pairs.add((renamed.old_path, modified.path))
        for source in renames:
            for target in renames:
                if source is target:
                    continue
                # 多个 rename 的匹配是 Git 根据相似度全局分配的；Git 不保存真实
                # 移动元数据。只要身份需要跨该 commit 延续，就必须把所有交叉
                # 配对视为候选，避免同内容或高相似文件按 basename 错配。
                ambiguous_pairs.add((source.old_path, target.path))
        return reliable, sorted(ambiguous_pairs)

    def _git_rename_candidate(
        self,
        old_version: str,
        old_path: str,
        new_version: str,
        new_path: str,
    ) -> bool:
        """让 Git 在只含 source/target blob 的临时目录上重算原生 score。"""
        key = (old_version, old_path, new_version, new_path)
        cached = self._git_rename_candidate_cache.get(key)
        if cached is not None:
            return cached
        old_blob = self._content_vcs.get_file_content_raw_bytes(
            old_version, old_path
        )
        new_blob = self._content_vcs.get_file_content_raw_bytes(
            new_version, new_path
        )
        if old_blob is None or new_blob is None:
            raise RuntimeError(
                "无法读取 Git 重命名候选 blob："
                f"{old_path}@{old_version} -> {new_path}@{new_version}"
            )

        candidate_root = tempfile.mkdtemp(
            prefix="rename_candidate_", dir=self._tmp_root
        )
        try:
            old_dir = os.path.join(candidate_root, "old")
            new_dir = os.path.join(candidate_root, "new")
            os.makedirs(old_dir)
            os.makedirs(new_dir)
            with open(os.path.join(old_dir, "source"), "wb") as stream:
                stream.write(old_blob)
            with open(os.path.join(new_dir, "target"), "wb") as stream:
                stream.write(new_blob)
            result = subprocess.run(
                [
                    self._git_exe,
                    "diff",
                    "--no-index",
                    "--name-status",
                    "-z",
                    "--find-renames=50%",
                    "--",
                    old_dir,
                    new_dir,
                ],
                cwd=self.source_project_path,
                capture_output=True,
                timeout=600,
            )
            if result.returncode not in (0, 1):
                raise RuntimeError(
                    "Git 重命名候选评分失败:\n"
                    + result.stderr.decode("utf-8", errors="replace")
                )
            fields = result.stdout.split(b"\x00")
            matched = bool(fields and fields[0].startswith(b"R"))
        finally:
            shutil.rmtree(candidate_root, ignore_errors=True)
        self._git_rename_candidate_cache[key] = matched
        return matched

    def _diff_changes(self, commit: str, parent: str, threshold: str) -> List[_HistoryChange]:
        if parent:
            args = (
                "diff", "--name-status", "-z", f"--find-renames={threshold}",
                parent, commit, "--",
            )
        else:
            args = (
                "diff-tree", "--root", "--no-commit-id", "--name-status", "-z",
                "-r", f"--find-renames={threshold}", commit, "--",
            )
        return self._parse_git_changes(self._git_bytes(*args))

    @staticmethod
    def _parse_git_changes(data: bytes) -> List[_HistoryChange]:
        fields = data.split(b"\x00")
        if fields and fields[-1] == b"":
            fields.pop()
        changes = []
        index = 0
        while index < len(fields):
            status = fields[index].decode("ascii", errors="replace")
            index += 1
            if index >= len(fields):
                raise RuntimeError("无法解析 Git 多版本变更记录")
            if status.startswith(("R", "C")):
                if index + 1 >= len(fields):
                    raise RuntimeError("无法解析 Git 多版本重命名/复制记录")
                old_path = fields[index].decode("utf-8", errors="surrogateescape")
                new_path = fields[index + 1].decode("utf-8", errors="surrogateescape")
                index += 2
                if status.startswith("R"):
                    changes.append(_HistoryChange("R", new_path, old_path))
                else:
                    changes.append(_HistoryChange("A", new_path))
                continue

            path = fields[index].decode("utf-8", errors="surrogateescape")
            index += 1
            if status == "T":
                # 同一路径的类型变化仍属于同一文件身份。这里只追踪历史，最终
                # 是否可导出由 _validate_git_endpoint_mode 对选中端点判定；这样
                # 无关文件在中间发生类型变化不会阻断整个需求包。
                changes.append(_HistoryChange("M", path))
                continue
            if status not in ("A", "M", "D"):
                raise RuntimeError(f"暂不支持的 Git 多版本变更类型 {status}: {path}")
            changes.append(_HistoryChange(status, path))
        return changes

    def _read_git_endpoint(self, version: str, path: str) -> Optional[bytes]:
        self._validate_git_endpoint_mode(version, path)
        return self._content_vcs.get_file_content_bytes(version, path)

    def _read_git_raw_endpoint(self, version: str, path: str) -> Optional[bytes]:
        self._validate_git_endpoint_mode(version, path)
        return self._content_vcs.get_file_content_raw_bytes(version, path)

    def _validate_git_endpoint_mode(self, version: str, path: str):
        cache_key = (str(version), path)
        if cache_key in self._git_mode_cache:
            return self._git_mode_cache[cache_key]
        mode_line = self._git_bytes("ls-tree", "-z", version, "--", path)
        if not mode_line:
            return ""
        header = mode_line.split(b"\t", 1)[0].decode("ascii", errors="replace")
        mode = header.split(" ", 1)[0]
        if mode not in ("100644", "100755"):
            raise RuntimeError(
                f"Git 多版本端点不是普通文件，已中止生成: {path}@{version} (mode={mode})"
            )
        self._git_mode_cache[cache_key] = mode
        return mode

    def _compare_endpoint_metadata(
            self, old_version, old_path, new_version, new_path) -> dict:
        old_mode = (
            self._validate_git_endpoint_mode(old_version, old_path)
            if old_path is not None else ""
        )
        new_mode = (
            self._validate_git_endpoint_mode(new_version, new_path)
            if new_path is not None else ""
        )
        return {
            "changes": GitVCS._mode_metadata(
                old_mode or "000000", new_mode or "000000"
            ),
            "old_executable": (
                GitVCS._mode_executable(old_mode) if old_mode else None
            ),
            "new_executable": (
                GitVCS._mode_executable(new_mode) if new_mode else None
            ),
            "old_mode": old_mode,
            "new_mode": new_mode,
        }


@dataclass(frozen=True)
class _SVNPathChange:
    action: str
    kind: str
    path: str
    copyfrom_path: str = ""
    copyfrom_rev: int = 0
    copyfrom_historical: bool = False
    props_modified: bool = False
    project_root_transition: bool = False


class SVNMultiVersionVCS(_MultiVersionFolderDelegate):
    """SVN 多版本：沿 revision 历史生成每个文件自己的首尾端点。"""

    @staticmethod
    def get_recent_versions(project_path: str, svn_path: str = "", limit: int = 100) -> List[str]:
        svn = svn_path or SVNVCS._find_svn()
        try:
            info = subprocess.run(
                [svn, "info", "--non-interactive", "--show-item", "url"],
                cwd=project_path,
                capture_output=True,
                timeout=60,
            )
        except FileNotFoundError:
            raise RuntimeError(SVN_NOT_FOUND_MESSAGE)
        if info.returncode != 0:
            raise RuntimeError(
                "SVN命令失败: info\n" + SVNMultiVersionVCS._decode(info.stderr or info.stdout)
            )
        url = SVNMultiVersionVCS._decode(info.stdout).strip()
        try:
            result = subprocess.run(
                [svn, "log", "-r", "HEAD:1", "-l", str(limit), "--non-interactive", f"{url}@HEAD"],
                capture_output=True,
                timeout=60,
            )
        except FileNotFoundError:
            raise RuntimeError(SVN_NOT_FOUND_MESSAGE)
        if result.returncode != 0:
            raise RuntimeError(
                "SVN命令失败: log\n" + SVNMultiVersionVCS._decode(result.stderr or result.stdout)
            )
        return SVNMultiVersionVCS._parse_log_versions(SVNMultiVersionVCS._decode(result.stdout))

    @staticmethod
    def _parse_log_versions(output: str) -> List[str]:
        revisions = []
        entries = output.split("------------------------------------------------------------------------")
        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            lines = entry.split("\n")
            match = re.match(r"^r(\d+) \|", lines[0].strip())
            if not match:
                continue
            rev = f"r{match.group(1)}"
            msg_parts = []
            in_paths = False
            for line in lines[1:]:
                value = line.strip()
                if not value:
                    in_paths = False
                    continue
                if value.startswith("Changed paths:"):
                    in_paths = True
                    continue
                if not in_paths:
                    msg_parts.append(value)
            message = " ".join(msg_parts)
            suffix = "..." if len(message) > 60 else ""
            revisions.append(f"{rev} {message[:60]}{suffix}".strip())
        return revisions

    def __init__(self, project_path: str, selected_versions: List[str], svn_path: str = ""):
        self._svn = svn_path or SVNVCS._find_svn()
        super().__init__(project_path, selected_versions, "comparetool_svn_multi_")
        try:
            self._prepare()
        except Exception:
            self.cleanup()
            raise

    @staticmethod
    def _decode(data: bytes) -> str:
        for encoding in ("utf-8", "gbk"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                pass
        return data.decode("utf-8", errors="replace")

    def _run(self, args: List[str]) -> str:
        try:
            result = subprocess.run(
                [self._svn] + args,
                cwd=self.source_project_path,
                capture_output=True,
                timeout=600,
            )
        except FileNotFoundError:
            raise RuntimeError(SVN_NOT_FOUND_MESSAGE)
        if result.returncode != 0:
            raise RuntimeError(
                f"SVN命令失败: {' '.join(args)}\n" + self._decode(result.stderr or result.stdout)
            )
        return self._decode(result.stdout)

    def _run_bytes(self, args: List[str]) -> bytes:
        try:
            result = subprocess.run(
                [self._svn] + args,
                cwd=self.source_project_path,
                capture_output=True,
                timeout=600,
            )
        except FileNotFoundError:
            raise RuntimeError(SVN_NOT_FOUND_MESSAGE)
        if result.returncode != 0:
            raise RuntimeError(
                f"SVN命令失败: {' '.join(args)}\n"
                + self._decode(result.stderr or result.stdout)
            )
        return result.stdout

    def _prepare(self):
        revisions = self._parse_revisions()
        selected = set(revisions)
        first_revision = min(revisions)
        last_revision = max(revisions)

        self._project_url = self._run(
            ["info", "--non-interactive", "--show-item", "url"]
        ).strip()
        self._repo_root_url = self._run(
            ["info", "--non-interactive", "--show-item", "repos-root-url"]
        ).strip()
        relative_url = self._run(
            ["info", "--non-interactive", "--show-item", "relative-url"]
        ).strip()
        if not self._project_url or not self._repo_root_url or not relative_url.startswith("^"):
            raise RuntimeError("无法获取 SVN 项目 URL、仓库根 URL 或仓库相对路径")
        self._project_repo_path = "/" + unquote(relative_url[1:]).strip("/")
        self._project_root_transitions = []
        self._svn_raw_cache = {}
        self._svn_eol_cache = {}
        self._svn_property_cache = {}

        self._content_vcs = SVNVCS(self.source_project_path, self._svn)
        self._content_vcs._cached_repo_url = self._project_url
        self._content_vcs._eol_cache = {}

        output = self._run([
            "log", "--xml", "-v", "--non-interactive",
            # 需要看到最后选中版之后的项目根移动，才能把 HEAD URL 映射回
            # 选中 revision；这些后续记录只用于路径映射，不参与端点规划。
            "-r", f"{first_revision}:HEAD", f"{self._project_url}@HEAD",
        ])
        history = self._parse_svn_history(output)
        planner = _EndpointPlanner()
        for revision in sorted(history):
            if revision > last_revision:
                continue
            is_selected = revision in selected
            changes = self._expand_svn_changes(
                history[revision], revision, planner, selected=is_selected
            )
            planner.apply(changes, is_selected, str(revision - 1), str(revision))

        self._finish_plan(
            planner.selected_entities,
            self._read_svn_endpoint,
            self._read_svn_raw_endpoint,
        )

    def _parse_revisions(self) -> List[int]:
        revisions = []
        seen = set()
        for raw in self.selected_versions:
            token = raw.strip().split()[0].lstrip("rR")
            if not token.isdigit() or int(token) <= 0:
                raise RuntimeError(f"SVN revision 格式不正确: {raw}")
            value = int(token)
            if value not in seen:
                revisions.append(value)
                seen.add(value)
        return revisions

    def _parse_svn_history(self, output: str) -> Dict[int, List[_SVNPathChange]]:
        try:
            root = ElementTree.fromstring(output)
        except ElementTree.ParseError as exc:
            raise RuntimeError(f"无法解析 SVN 多版本历史: {exc}") from exc
        history: Dict[int, List[_SVNPathChange]] = {}
        entries = []
        for entry in root.findall("logentry"):
            revision_text = entry.get("revision", "")
            if not revision_text.isdigit():
                continue
            entries.append((int(revision_text), entry))

        current_prefix = self._project_repo_path.rstrip("/")
        transitions = []
        for revision, entry in sorted(entries, key=lambda item: item[0], reverse=True):
            root_copy_candidates = []
            entry_nodes = list(entry.findall("./paths/path"))
            for node in entry_nodes:
                absolute_path = self._normalize_repo_path((node.text or "").strip())
                copyfrom_absolute = self._normalize_repo_path(
                    node.get("copyfrom-path", "")
                )
                if (
                    (node.get("action") or "").upper() == "A"
                    and (node.get("kind") or "").lower() == "dir"
                    and copyfrom_absolute
                    and (
                        absolute_path == current_prefix
                        or current_prefix.startswith(absolute_path.rstrip("/") + "/")
                    )
                ):
                    suffix = current_prefix[len(absolute_path.rstrip("/")):]
                    historical_prefix = copyfrom_absolute.rstrip("/") + suffix
                    root_copy_candidates.append(
                        (
                            len(absolute_path),
                            historical_prefix,
                            copyfrom_absolute.rstrip("/"),
                            int(node.get("copyfrom-rev", "") or 0),
                            (node.get("prop-mods") or "").lower() == "true",
                            absolute_path.rstrip("/") == current_prefix,
                        )
                    )
            root_candidate = max(
                root_copy_candidates,
                key=lambda item: item[0],
                default=(0, "", "", 0, False, False),
            )
            root_copyfrom = root_candidate[1]
            root_source = root_candidate[2]
            root_transition_is_move = bool(root_source) and any(
                (node.get("action") or "").upper() == "D"
                and (node.get("kind") or "").lower() == "dir"
                and self._normalize_repo_path((node.text or "").strip()).rstrip("/")
                == root_source
                for node in entry_nodes
            )
            items = []
            if root_copyfrom:
                # 即使移动的是项目祖先，当前项目根也发生了有效的历史前缀
                # 切换。保留一个不参与文件展开的根标记，供选中 revision
                # 检查根 svn:externals 和同 revision 的根属性变化。
                items.append(_SVNPathChange(
                    action="A",
                    kind="dir",
                    path="",
                    copyfrom_rev=root_candidate[3] or revision - 1,
                    props_modified=(root_candidate[4] and root_candidate[5]),
                    project_root_transition=True,
                ))
            for node in entry_nodes:
                absolute_path = self._normalize_repo_path((node.text or "").strip())
                rel_path = self._repo_to_project_path(absolute_path, current_prefix)
                if rel_path is None:
                    continue
                action = (node.get("action") or "").upper()
                kind = (node.get("kind") or "").lower()
                if rel_path == "" and root_copyfrom and action == "A" and kind == "dir":
                    # 项目根自身移动不应把所有内部文件误报为新增；它只改变
                    # 更早 revision 的项目仓库前缀。
                    continue
                copyfrom_absolute = self._normalize_repo_path(
                    node.get("copyfrom-path", "")
                )
                copyfrom = self._repo_to_project_path(
                    copyfrom_absolute, current_prefix
                )
                copyfrom_historical = False
                if copyfrom is None and root_copyfrom:
                    copyfrom = self._repo_to_project_path(
                        copyfrom_absolute, root_copyfrom
                    )
                    copyfrom_historical = (
                        copyfrom is not None and root_transition_is_move
                    )
                copyfrom_rev_text = node.get("copyfrom-rev", "")
                items.append(_SVNPathChange(
                    action=action,
                    kind=kind,
                    path=rel_path,
                    copyfrom_path=copyfrom or "",
                    copyfrom_rev=(
                        int(copyfrom_rev_text) if copyfrom_rev_text.isdigit() else 0
                    ),
                    copyfrom_historical=copyfrom_historical,
                    props_modified=(
                        (node.get("prop-mods") or "").lower() == "true"
                    ),
                ))
            history[revision] = items
            if root_copyfrom:
                transitions.append((revision, current_prefix, root_copyfrom))
                current_prefix = root_copyfrom
        self._project_root_transitions = transitions
        return history

    @staticmethod
    def _normalize_repo_path(repo_path: str) -> str:
        if not repo_path:
            return ""
        # svn log --xml 返回仓库中的字面路径；其中的 %HH 不代表 URL 转义，
        # 解码会把合法文件名改成另一个身份。只有 svn info 的 relative-url
        # 输出需要在其专用解析处 unquote。
        return "/" + repo_path.replace("\\", "/").strip("/")

    def _repo_to_project_path(
        self, repo_path: str, project_prefix: str = ""
    ) -> Optional[str]:
        if not repo_path:
            return None
        normalized = self._normalize_repo_path(repo_path).rstrip("/")
        prefix = (project_prefix or self._project_repo_path).rstrip("/")
        if normalized == prefix:
            return ""
        if not normalized.startswith(prefix + "/"):
            return None
        return normalized[len(prefix) + 1:]

    def _expand_svn_changes(
        self,
        raw_changes: List[_SVNPathChange],
        revision: int,
        planner: _EndpointPlanner,
        selected: bool = False,
    ) -> List[_HistoryChange]:
        files = [item for item in raw_changes if item.kind != "dir"]
        directories = [item for item in raw_changes if item.kind == "dir"]
        changes: List[_HistoryChange] = []

        def predecessor_endpoint(item: _SVNPathChange):
            """返回 R 节点真实的替换前端点；新目标可能在 r-1 尚不存在。"""
            if item.action != "R":
                return item.path, revision - 1
            if self._svn_node_exists(item.path, revision - 1):
                return item.path, revision - 1
            if item.copyfrom_path:
                return item.copyfrom_path, item.copyfrom_rev or revision - 1
            # 没有 copyfrom 时仍让 kind/property 查询按既有路径 fail closed。
            return item.path, revision - 1

        normalized_files = []
        for item in files:
            old_path, old_revision = predecessor_endpoint(item)
            if item.action == "R" and self._svn_node_kind(old_path, old_revision) == "dir":
                changes.extend(
                    _HistoryChange("D", path)
                    for path in self._list_svn_files(old_path, old_revision)
                )
                normalized_files.append(_SVNPathChange(
                    "A", item.kind, item.path, item.copyfrom_path,
                    item.copyfrom_rev, item.copyfrom_historical,
                    item.props_modified, item.project_root_transition,
                ))
                if selected:
                    required = set(getattr(self, "required_directory_deletions", []))
                    required.add(item.path)
                    self.required_directory_deletions = sorted(required)
            else:
                normalized_files.append(item)
        files = normalized_files
        normalized_directories = []
        for item in directories:
            old_path, old_revision = predecessor_endpoint(item)
            if item.action == "R" and self._svn_node_kind(old_path, old_revision) == "file":
                changes.append(_HistoryChange("D", item.path))
                normalized_directories.append(_SVNPathChange(
                    "A", item.kind, item.path, item.copyfrom_path,
                    item.copyfrom_rev, item.copyfrom_historical,
                    item.props_modified, item.project_root_transition,
                ))
            else:
                normalized_directories.append(item)
        directories = normalized_directories
        if selected:
            for item in directories:
                if item.project_root_transition:
                    old_path, old_revision = "", item.copyfrom_rev or revision - 1
                elif item.action == "D" and not self._svn_node_exists(
                    item.path, revision - 1
                ):
                    # 同 revision 目录 copy/move 产生后又删除的临时路径在
                    # r-1 没有真实端点；其最终目标目录仍会单独校验属性。
                    old_path, old_revision = "", 0
                elif item.action in ("D", "M", "R"):
                    old_path, old_revision = predecessor_endpoint(item)
                else:
                    old_path, old_revision = "", 0
                old_props = self._get_svn_properties(
                    str(old_revision), old_path
                ) if old_revision else {}
                new_props = (
                    self._get_svn_properties(str(revision), item.path)
                    if item.action in ("A", "M", "R") else {}
                )
                if "svn:externals" in old_props or "svn:externals" in new_props:
                    raise RuntimeError(
                        "SVN 多版本目录启用了 svn:externals，文件级交付无法保真，"
                        f"已中止生成: {item.path}@{revision}"
                    )
        if selected and any(
            item.action == "M" or item.props_modified for item in directories
        ):
            paths = ", ".join(
                sorted(
                    item.path or "<项目根>" for item in directories
                    if item.action == "M" or item.props_modified
                )
            )
            raise RuntimeError(
                "SVN 多版本选中 revision 包含目录属性变化，"
                f"当前文件级交付无法保真，已中止生成: {paths}"
            )
        # 根历史标记只服务于属性/externals 校验与前缀映射，不能被当成
        # 普通新增目录展开，否则会把整个项目误报为新增。
        directories = [
            item for item in directories if not item.project_root_transition
        ]
        if selected:
            added_file_paths = {
                item.path for item in files if item.action in ("A", "R")
            }
            deleted_directory_paths = {
                item.path for item in directories if item.action in ("D", "R")
            }
            required = set(getattr(self, "required_directory_deletions", []))
            required.update(
                directory for directory in deleted_directory_paths
                if any(
                    new_path.casefold() == directory.casefold()
                    or directory.casefold().startswith(
                        new_path.rstrip("/").casefold() + "/"
                    )
                    for new_path in added_file_paths
                )
            )
            self.required_directory_deletions = sorted(required)

        deleted_files = {item.path: item for item in files if item.action == "D"}
        deleted_dirs = {item.path: item for item in directories if item.action == "D"}
        node_exists_cache = {}

        def node_exists(path: str, at_revision: int) -> bool:
            key = (path, at_revision)
            if key not in node_exists_cache:
                node_exists_cache[key] = self._svn_node_exists(path, at_revision)
            return node_exists_cache[key]

        def covered_by_deleted_dir(path: str) -> bool:
            return any(
                path == directory
                or path.startswith(directory.rstrip("/") + "/")
                for directory in deleted_dirs
            )

        directory_moves = []
        for item in directories:
            if item.action != "A" or not item.copyfrom_path:
                continue
            source_missing_before_copy = (
                planner.has_deleted_under(item.copyfrom_path)
                and not node_exists(item.copyfrom_path, revision - 1)
            )
            if covered_by_deleted_dir(item.copyfrom_path) or source_missing_before_copy:
                directory_moves.append(item)

        explicit_move_candidates = {}
        explicit_move_contexts = {}
        copy_pair_actions = {}
        inherited_directory_targets = set()
        ordinary_directory_copy_targets = set()
        file_changes_by_path = {}
        for file_change in files:
            file_changes_by_path.setdefault(file_change.path, []).append(file_change)
        for item in files:
            if item.action not in ("A", "R") or not item.copyfrom_path:
                continue
            matching_moves = []
            for directory_move in directory_moves:
                old_prefix = directory_move.copyfrom_path.rstrip("/") + "/"
                new_prefix = directory_move.path.rstrip("/") + "/"
                if item.copyfrom_path.startswith(old_prefix):
                    matching_moves.append((
                        len(old_prefix), old_prefix, new_prefix
                    ))
            if not matching_moves:
                continue

            # 嵌套目录在同一 revision 连续移动时，一个子文件 copyfrom 可能
            # 同时落入外层和内层映射。必须用最具体的目录移动解释它，否则
            # 外层已消失的自然后缀会把内层普通 copy 误判成显式改名。
            _, old_prefix, new_prefix = max(matching_moves)
            suffix = item.copyfrom_path[len(old_prefix):]
            inherited_target = new_prefix + suffix
            if item.path == inherited_target:
                # 目录 copy 本身已经继承了这个后缀，交给目录展开统一生成
                # 身份链，避免重复追加 A/R。
                inherited_directory_targets.add(item.path)
            elif node_exists(inherited_target, revision) and not any(
                candidate is not item
                and candidate.action in ("A", "R")
                and candidate.copyfrom_path != item.copyfrom_path
                for candidate in file_changes_by_path.get(inherited_target, [])
            ):
                # 原后缀在目标目录仍然存在，当前记录只是从它复制出一个新
                # 文件，不是把原文件重命名到 item.path。
                ordinary_directory_copy_targets.add(item.path)
            else:
                explicit_move_candidates.setdefault(item.copyfrom_path, set()).add(
                    item.path
                )
                explicit_move_contexts[item.copyfrom_path] = (old_prefix, new_prefix)
                copy_pair_actions[(item.copyfrom_path, item.path)] = item.action

        explicit_directory_moves = {}
        external_explicit_moves = {}
        resolved_explicit_moves = {}
        for source_path, target_paths in explicit_move_candidates.items():
            if len(target_paths) == 1:
                target_path = next(iter(target_paths))
                resolved_explicit_moves[source_path] = target_path
                _, new_prefix = explicit_move_contexts[source_path]
                if target_path.startswith(new_prefix):
                    explicit_directory_moves[source_path] = target_path
                else:
                    external_explicit_moves[source_path] = (
                        target_path,
                        copy_pair_actions[(source_path, target_path)],
                    )
            else:
                # SVN copyfrom 允许一个源分叉到多个目标，但没有“哪一个才是
                # rename”的元数据。部署净结果可以无猜测地表达为 D + 多个 A，
                # 因而降级为独立复制，不应为无关历史或当前选中 revision 失败。
                ordinary_directory_copy_targets.update(target_paths)

        explicit_sources = set(resolved_explicit_moves)
        explicit_targets = set(resolved_explicit_moves.values())
        consumed_deleted_files = set()
        moved_file_sources = set()
        moved_file_targets = set()
        moved_target_prefixes = {
            item.path.rstrip("/") for item in directory_moves
        }
        for item in files:
            if item.path in inherited_directory_targets:
                continue
            if item.path in explicit_targets or (
                item.action == "D" and item.path in explicit_sources
            ):
                continue
            if (
                item.action == "D"
                and any(
                    item.path == prefix
                    or item.path.startswith(prefix.rstrip("/") + "/")
                    for prefix in moved_target_prefixes
                )
                and not node_exists(item.path, revision - 1)
            ):
                # 同 revision 内由目录 copy 临时产生、随后又被子 move 删除的
                # 路径没有真实 revision-1 端点，其身份由 copyfrom 展开负责。
                continue
            if item.action == "A" and item.path in ordinary_directory_copy_targets:
                changes.append(_HistoryChange("A", item.path))
            elif item.action in ("A", "R") and item.copyfrom_path and (
                item.copyfrom_path in deleted_files
                or covered_by_deleted_dir(item.copyfrom_path)
                or planner.is_deleted(item.copyfrom_path)
                or (
                    item.copyfrom_historical
                    and planner.is_active(item.copyfrom_path)
                )
            ):
                if item.action == "R":
                    changes.append(_HistoryChange("D", item.path))
                changes.append(_HistoryChange("R", item.path, item.copyfrom_path))
                consumed_deleted_files.add(item.copyfrom_path)
                moved_file_sources.add(item.copyfrom_path)
                moved_file_targets.add(item.path)
            elif item.action == "A":
                changes.append(_HistoryChange("A", item.path))
            elif item.action == "D":
                changes.append(_HistoryChange("D", item.path))
            elif item.action in ("M", "R"):
                changes.append(_HistoryChange("M", item.path))
            else:
                raise RuntimeError(
                    f"暂不支持的 SVN 多版本变更类型 {item.action}: {item.path}"
                )
        for source_path, (target_path, action) in external_explicit_moves.items():
            if action == "R":
                changes.append(_HistoryChange("D", target_path))
            changes.append(_HistoryChange("R", target_path, source_path))
            consumed_deleted_files.add(source_path)
            moved_file_sources.add(source_path)
            moved_file_targets.add(target_path)
        if consumed_deleted_files:
            changes = [
                item for item in changes
                if not (item.action == "D" and item.path in consumed_deleted_files)
            ]

        for item in directories:
            if item.action != "A":
                continue
            if item in directory_moves:
                nested_source_prefixes = [
                    other.copyfrom_path.rstrip("/")
                    for other in directory_moves
                    if other is not item
                    and other.copyfrom_path.startswith(
                        item.copyfrom_path.rstrip("/") + "/"
                    )
                ]
                nested_target_prefixes = [
                    other.path.rstrip("/")
                    for other in directory_moves
                    if other is not item
                    and other.path.startswith(item.path.rstrip("/") + "/")
                ]
                consumed_old_prefixes = nested_source_prefixes + [
                    source
                    for source in moved_file_sources
                    if source.startswith(item.copyfrom_path.rstrip("/") + "/")
                ]
                consumed_new_prefixes = nested_target_prefixes + [
                    target
                    for target in moved_file_targets
                    if target.startswith(item.path.rstrip("/") + "/")
                ]
                explicit = {
                    old_path: new_path
                    for old_path, new_path in explicit_directory_moves.items()
                    if old_path.startswith(item.copyfrom_path.rstrip("/") + "/")
                    and new_path.startswith(item.path.rstrip("/") + "/")
                    and not any(
                        old_path == prefix
                        or old_path.startswith(prefix.rstrip("/") + "/")
                        for prefix in consumed_old_prefixes
                    )
                    and not any(
                        new_path == prefix
                        or new_path.startswith(prefix.rstrip("/") + "/")
                        for prefix in consumed_new_prefixes
                    )
                }
                changes.extend(self._expand_directory_move(
                    item.copyfrom_path,
                    item.path,
                    item.copyfrom_rev or revision - 1,
                    revision,
                    explicit,
                    consumed_old_prefixes,
                    consumed_new_prefixes,
                ))
            else:
                changes.extend(
                    _HistoryChange("A", path)
                    for path in self._list_svn_files(item.path, revision)
                )

        moved_source_prefixes = {
            item.copyfrom_path.rstrip("/") for item in directory_moves
        } | moved_file_sources
        for item in directories:
            if item.action == "D":
                if not node_exists(item.path, revision - 1):
                    # 与文件级临时 D 相同，本 revision 才出现的中间目录不能
                    # 再按 revision-1 的真实仓库路径展开。
                    continue
                # 删除祖先目录可能只是一次或多次 copy+delete move 的外壳；
                # 已移动的源文件/子树不能再次展开为独立删除，但祖先下其它
                # 实际被删文件仍必须保留。
                changes.extend(
                    _HistoryChange("D", path)
                    for path in self._list_svn_files(item.path, revision - 1)
                    if not any(
                        path == source
                        or path.startswith(source.rstrip("/") + "/")
                        for source in moved_source_prefixes
                    )
                )
            elif item.action == "R":
                changes.extend(self._expand_directory_replacement(item.path, revision))
            elif item.action not in ("A", "D", "M", "R"):
                raise RuntimeError(
                    f"暂不支持的 SVN 目录变更类型 {item.action}: {item.path}"
                )

        unique = []
        seen = set()
        # 替换目标必须先移除旧身份，再把 copyfrom 源连接到该路径；普通
        # source D 先发生也没关系，R 会从 planner._deleted 中恢复同一实体。
        priority = {"D": 0, "R": 1, "A": 2, "M": 3}
        for item in sorted(changes, key=lambda value: (priority[value.action], value.path, value.old_path)):
            key = (item.action, item.path, item.old_path)
            if key not in seen:
                unique.append(item)
                seen.add(key)
        return unique

    def _expand_directory_move(
        self,
        old_dir: str,
        new_dir: str,
        source_revision: int,
        target_revision: int,
        explicit_moves: Dict[str, str],
        excluded_old_prefixes=(),
        excluded_new_prefixes=(),
    ) -> List[_HistoryChange]:
        old_files = self._directory_suffix_map(old_dir, source_revision)
        new_files = self._directory_suffix_map(new_dir, target_revision)
        if excluded_old_prefixes:
            old_files = {
                suffix: path
                for suffix, path in old_files.items()
                if not any(
                    path == prefix
                    or path.startswith(prefix.rstrip("/") + "/")
                    for prefix in excluded_old_prefixes
                )
            }
        if excluded_new_prefixes:
            new_files = {
                suffix: path
                for suffix, path in new_files.items()
                if not any(
                    path == prefix
                    or path.startswith(prefix.rstrip("/") + "/")
                    for prefix in excluded_new_prefixes
                )
            }
        result = []
        used_old = set()
        used_new = set()
        old_path_set = set(old_files.values())
        new_path_set = set(new_files.values())
        for old_path, new_path in sorted(explicit_moves.items()):
            if old_path not in old_path_set or new_path not in new_path_set:
                raise RuntimeError(
                    "SVN 目录移动中的显式文件 copyfrom 无法在端点确认：\n"
                    f"{old_path} -> {new_path}"
                )
            result.append(_HistoryChange("R", new_path, old_path))
            used_old.add(old_path)
            used_new.add(new_path)
        for suffix in sorted(set(old_files) | set(new_files)):
            old_path = old_files.get(suffix)
            new_path = new_files.get(suffix)
            if old_path in used_old:
                if new_path and new_path not in used_new:
                    result.append(_HistoryChange("A", new_path))
                continue
            if new_path in used_new:
                if old_path:
                    result.append(_HistoryChange("D", old_path))
                continue
            if old_path and new_path:
                result.append(_HistoryChange("R", new_path, old_path))
            elif old_path:
                result.append(_HistoryChange("D", old_path))
            else:
                result.append(_HistoryChange("A", new_path))
        return result

    def _expand_directory_replacement(
        self, directory: str, revision: int
    ) -> List[_HistoryChange]:
        old_files = set(self._list_svn_files(directory, revision - 1))
        new_files = set(self._list_svn_files(directory, revision))
        result = [_HistoryChange("M", path) for path in sorted(old_files & new_files)]
        result.extend(_HistoryChange("D", path) for path in sorted(old_files - new_files))
        result.extend(_HistoryChange("A", path) for path in sorted(new_files - old_files))
        return result

    def _directory_suffix_map(self, directory: str, revision: int) -> Dict[str, str]:
        prefix = directory.rstrip("/")
        result = {}
        for path in self._list_svn_files(directory, revision):
            suffix = path[len(prefix):].lstrip("/") if prefix else path
            result[suffix] = path
        return result

    def _list_svn_files(self, directory: str, revision: int) -> List[str]:
        relative = directory.replace("\\", "/").strip("/")
        url = self._svn_file_url(str(revision), relative)
        output = self._run([
            "list", "--xml", "-R", "--non-interactive", "-r", str(revision),
            url,
        ])
        try:
            root = ElementTree.fromstring(output)
        except ElementTree.ParseError as exc:
            raise RuntimeError(f"无法解析 SVN 目录文件列表: {exc}") from exc
        files = []
        for entry in root.findall(".//entry"):
            if entry.get("kind") != "file":
                continue
            name = (entry.findtext("name") or "").replace("\\", "/").strip("/")
            if not name:
                continue
            files.append(f"{relative}/{name}" if relative else name)
        return files

    def _svn_node_exists(self, path: str, revision: int) -> bool:
        try:
            result = subprocess.run(
                [
                    self._svn,
                    "info",
                    "--non-interactive",
                    "-r",
                    str(revision),
                    self._svn_file_url(str(revision), path),
                ],
                cwd=self.source_project_path,
                capture_output=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(
                f"无法确认 SVN 节点是否存在，已中止生成: {path}@{revision}\n{exc}"
            ) from exc
        if result.returncode == 0:
            return True
        stderr = self._decode(result.stderr or result.stdout)
        if re.search(r"(?:E160013|W160013|E200009|W200017|E155010)", stderr):
            return False
        raise RuntimeError(
            f"确认 SVN 节点是否存在时失败，已中止生成: {path}@{revision}\n{stderr}"
        )

    def _svn_node_kind(self, path: str, revision: int) -> str:
        cache = getattr(self, "_svn_kind_cache", None)
        if cache is None:
            self._svn_kind_cache = {}
            cache = self._svn_kind_cache
        key = (path, revision)
        if key in cache:
            return cache[key]
        try:
            result = subprocess.run(
                [
                    self._svn, "info", "--non-interactive", "--show-item", "kind",
                    "-r", str(revision), self._svn_file_url(str(revision), path),
                ],
                cwd=self.source_project_path,
                capture_output=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(
                f"无法确认 SVN 替换前节点类型: {path}@{revision}\n{exc}"
            ) from exc
        kind = self._decode(result.stdout).strip().lower() if result.returncode == 0 else ""
        if kind not in ("file", "dir"):
            raise RuntimeError(
                f"无法确认 SVN 替换前节点类型: {path}@{revision}\n"
                + self._decode(result.stderr or result.stdout)
            )
        cache[key] = kind
        return kind

    def _read_svn_endpoint(self, version: str, path: str) -> Optional[bytes]:
        data = self._read_svn_raw_endpoint(version, path)
        if data is None or not self._content_vcs._is_text_bytes(data):
            return data
        style = self._get_svn_eol_style(version, path).strip().lower()
        if style == "crlf" or (style == "native" and os.linesep == "\r\n"):
            data = self._content_vcs._apply_crlf(self._content_vcs._normalize_lf(data))
        elif style == "cr" or (style == "native" and os.linesep == "\r"):
            data = self._content_vcs._normalize_lf(data).replace(b"\n", b"\r")
        elif style == "lf":
            data = self._content_vcs._normalize_lf(data)
        return data

    def _read_svn_raw_endpoint(self, version: str, path: str) -> Optional[bytes]:
        cache_key = (str(version), path)
        if cache_key in self._svn_raw_cache:
            return self._svn_raw_cache[cache_key]
        self._validate_svn_regular_endpoint(version, path)
        try:
            data = self._run_bytes(["cat", self._svn_file_url(version, path)])
        except RuntimeError:
            data = None
        self._svn_raw_cache[cache_key] = data
        return data

    def _get_svn_eol_style(self, version: str, path: str) -> str:
        cache_key = (str(version), path)
        if cache_key in self._svn_eol_cache:
            return self._svn_eol_cache[cache_key]
        style = self._validate_svn_regular_endpoint(version, path).get(
            "svn:eol-style", ""
        )
        self._svn_eol_cache[cache_key] = style
        return style

    def _get_svn_properties(self, version: str, path: str):
        cache_key = (str(version), path)
        if cache_key in self._svn_property_cache:
            return self._svn_property_cache[cache_key]
        rev = str(version).lstrip("rR")
        try:
            result = subprocess.run(
                [
                    self._svn,
                    "proplist",
                    "--xml",
                    "-v",
                    "--non-interactive",
                    "-r",
                    rev,
                    self._svn_file_url(version, path),
                ],
                cwd=self.source_project_path,
                capture_output=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(
                f"无法检查 SVN 多版本端点属性: {path}@{version}\n{exc}"
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"无法检查 SVN 多版本端点属性: {path}@{version}\n"
                + self._decode(result.stderr or result.stdout)
            )
        try:
            root = ElementTree.fromstring(self._decode(result.stdout))
        except ElementTree.ParseError as exc:
            raise RuntimeError(f"无法解析 SVN 端点属性: {path}@{version}") from exc
        properties = {
            node.get("name", ""): (
                f"{node.get('encoding')}:{node.text or ''}"
                if node.get("encoding") else (node.text or "")
            )
            for node in root.findall(".//property")
            if node.get("name")
        }
        self._svn_property_cache[cache_key] = properties
        return properties

    def _validate_svn_regular_endpoint(self, version: str, path: str):
        properties = self._get_svn_properties(version, path)
        if "svn:special" in properties:
            raise RuntimeError(
                f"SVN 多版本端点是 svn:special（符号链接等特殊节点），"
                f"已中止生成: {path}@{version}"
            )
        if "svn:keywords" in properties:
            raise RuntimeError(
                "SVN 多版本端点启用了 svn:keywords，svn cat 不能可靠复现"
                f"工作副本展开字节，已中止生成: {path}@{version}"
            )
        return properties

    def _compare_endpoint_metadata(
            self, old_version, old_path, new_version, new_path) -> dict:
        old_props = (
            self._validate_svn_regular_endpoint(old_version, old_path)
            if old_path is not None else {}
        )
        new_props = (
            self._validate_svn_regular_endpoint(new_version, new_path)
            if new_path is not None else {}
        )
        changed = {
            name for name in set(old_props) | set(new_props)
            if old_props.get(name) != new_props.get(name)
        }
        supported = {"svn:eol-style", "svn:executable"}
        unsupported = sorted(changed - supported)
        if unsupported:
            raise RuntimeError(
                "SVN 多版本文件属性发生变化，但普通文件交付无法保真，已中止生成: "
                f"{new_path or old_path}\n属性: {', '.join(unsupported)}"
            )
        details = []
        if "svn:eol-style" in changed:
            details.append(
                "SVN 换行属性："
                f"{old_props.get('svn:eol-style', '未设置')} → "
                f"{new_props.get('svn:eol-style', '未设置')}"
            )
        if "svn:executable" in changed:
            details.append(
                "SVN 可执行属性："
                f"{'已设置' if 'svn:executable' in old_props else '未设置'} → "
                f"{'已设置' if 'svn:executable' in new_props else '未设置'}"
            )
        return {
            "changes": details,
            "old_executable": (
                "svn:executable" in old_props if old_path is not None else None
            ),
            "new_executable": (
                "svn:executable" in new_props if new_path is not None else None
            ),
        }

    def _svn_file_url(self, version: str, path: str) -> str:
        revision = int(str(version).lstrip("rR"))
        project_prefix = self._project_repo_path_at(revision).rstrip("/")
        relative = path.replace("\\", "/").strip("/")
        repo_path = project_prefix + ("/" + relative if relative else "")
        return f"{self._repo_root_url.rstrip('/')}{quote(repo_path, safe='/')}@{revision}"

    def _project_repo_path_at(self, revision: int) -> str:
        prefix = self._project_repo_path.rstrip("/")
        for transition_revision, new_prefix, old_prefix in sorted(
            self._project_root_transitions, reverse=True
        ):
            if prefix != new_prefix and revision >= transition_revision:
                continue
            if revision < transition_revision:
                prefix = old_prefix
        return prefix
