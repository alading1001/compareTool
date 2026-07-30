import ntpath
import os
import shutil
import tempfile
import uuid
from diff_engine import DiffResult
from vcs.base import ChangeType


class FileExporter:
    """将变更文件导出到指定目录"""

    def __init__(self, diff_result: DiffResult, vcs):
        self.diff_result = diff_result
        self.vcs = vcs

    def export(self, old_dir: str, new_dir: str, project_name: str = ""):
        """先在同盘临时目录完整导出，全部成功后再替换目标目录。"""
        if project_name:
            old_dir = self._safe_join(old_dir, project_name)
            new_dir = self._safe_join(new_dir, project_name)

        old_dir = os.path.abspath(old_dir)
        new_dir = os.path.abspath(new_dir)
        if os.path.normcase(old_dir) == os.path.normcase(new_dir):
            raise RuntimeError("新旧版本导出目录不能相同")

        self._validate_export_paths(old_dir, new_dir)
        old_ver = self.diff_result.old_version
        new_ver = self.diff_result.new_version
        stage_old = self._make_stage_dir(old_dir)
        stage_new = self._make_stage_dir(new_dir)

        try:
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
            self._replace_outputs([
                (stage_old, old_dir),
                (stage_new, new_dir),
            ])
        finally:
            for stage in (stage_old, stage_new):
                if os.path.isdir(stage):
                    shutil.rmtree(stage, ignore_errors=True)

    def _write_file(self, base_dir: str, rel_path: str, version: str, text_content: str):
        file_path = self._safe_join(base_dir, rel_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # 所有正式 VCS 都支持原始字节读取；失败必须中止，不能静默漏包。
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
        """拼接受控相对路径，拒绝盘符、绝对路径和父目录越界。"""
        raw = (rel_path or "").replace("\\", "/")
        drive, _ = ntpath.splitdrive(raw)
        parts = [part for part in raw.split("/") if part not in ("", ".")]
        if (
            not raw or
            not parts or
            "\x00" in raw or
            drive or
            raw.startswith("/") or
            any(part == ".." for part in parts)
        ):
            raise RuntimeError(f"导出路径不安全: {rel_path}")

        root = os.path.abspath(base_dir)
        target = os.path.abspath(os.path.join(root, *parts))
        try:
            inside_root = os.path.commonpath([root, target]) == root
        except ValueError:
            inside_root = False
        if not inside_root:
            raise RuntimeError(f"导出路径越界: {rel_path}")
        return target

    @staticmethod
    def _make_stage_dir(target_dir: str) -> str:
        parent = os.path.dirname(target_dir)
        os.makedirs(parent, exist_ok=True)
        return tempfile.mkdtemp(prefix=".comparetool_stage_", dir=parent)

    @classmethod
    def _replace_outputs(cls, pairs):
        """成组替换文件或目录；任一步失败时恢复全部原有输出。"""
        token = uuid.uuid4().hex
        states = []
        try:
            for stage, target in pairs:
                backup = f"{target}.comparetool_backup_{token}"
                had_target = os.path.lexists(target)
                if had_target:
                    os.replace(target, backup)
                states.append({
                    "stage": stage,
                    "target": target,
                    "backup": backup,
                    "had_target": had_target,
                    "installed": False,
                })

            for state in states:
                os.replace(state["stage"], state["target"])
                state["installed"] = True
        except Exception:
            for state in reversed(states):
                if state["installed"] and os.path.lexists(state["target"]):
                    cls._remove_path(state["target"])
                if state["had_target"] and os.path.lexists(state["backup"]):
                    os.replace(state["backup"], state["target"])
            raise
        else:
            for state in states:
                if state["had_target"] and os.path.lexists(state["backup"]):
                    cls._remove_path(state["backup"])

    @staticmethod
    def _remove_path(path: str):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.remove(path)
