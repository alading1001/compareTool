"""Borrow already-exported archive endpoints; never own or modify their trees."""
import os
import stat

from path_safety import (
    safe_join, ensure_no_link_components, windows_path_key,
    open_regular_file_no_links, regular_file_handle_identity,
    regular_file_path_identity,
)


class StagedArchiveEndpoints:
    """Explicit side selection, independent of VCS display/version labels."""

    def __init__(self, old_root, new_root, *, trusted_root):
        if not trusted_root or not old_root or not new_root:
            raise ValueError('包内分析需要明确的可信根和新旧暂存根')
        self.trusted_root = os.path.abspath(trusted_root)
        self.roots = {'old': os.path.abspath(old_root),
                      'new': os.path.abspath(new_root)}
        for root in self.roots.values():
            self._validate_root(root)
        common = os.path.commonpath(list(self.roots.values()))
        if any(windows_path_key(common) == windows_path_key(root)
               for root in self.roots.values()):
            raise ValueError('包内分析的新旧暂存根不能相同或互相包含')

    def _validate_root(self, root):
        ensure_no_link_components(self.trusted_root, root, '包内分析暂存根')
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise ValueError('包内分析暂存根不是目录: ' + root)
        anchor_real, root_real = (os.path.realpath(p)
                                  for p in (self.trusted_root, root))
        if os.path.commonpath([anchor_real, root_real]) != anchor_real:
            raise ValueError('包内分析暂存根越出可信范围: ' + root)

    @classmethod
    def from_export_pairs(cls, pairs, old_target, new_target, *, trusted_root):
        """Resolve actual returned stages by target, never by wrapper names."""
        expected = [windows_path_key(os.path.abspath(p))
                    for p in (old_target, new_target)]
        mapped = {}
        for stage, target in pairs:
            key = windows_path_key(os.path.abspath(target))
            if key not in expected or key in mapped:
                raise ValueError('包内分析收到不匹配或重复的源码暂存目标')
            mapped[key] = stage
        if len(mapped) != 2 or expected[0] == expected[1]:
            raise ValueError('包内分析缺少完整的新旧源码暂存对')
        return cls(mapped[expected[0]], mapped[expected[1]],
                   trusted_root=trusted_root)

    def path_for(self, side, relative_path):
        if side not in ('old', 'new'):
            raise ValueError('包内分析侧别必须为 old 或 new，不能使用版本显示标签')
        root = self.roots[side]
        try:
            self._validate_root(root)
            path = safe_join(root, relative_path, label='包内分析端点路径')
            self._validate_path(root, path)
            with open_regular_file_no_links(path, deny_writes=True) as stream:
                before = regular_file_handle_identity(stream)
                self._validate_path(root, path)
                if regular_file_path_identity(path) != before:
                    raise RuntimeError('包内分析端点在打开期间被替换')
            return path
        except (OSError, ValueError, RuntimeError) as exc:
            label = '旧' if side == 'old' else '新'
            raise RuntimeError(f'无法读取{label}侧待交付压缩包 [{relative_path}]: {exc}') from exc

    def _validate_path(self, root, path):
        # Inspect original components BEFORE resolving them; resolving first
        # would hide an inserted junction. Repeat for every access (no cache).
        ensure_no_link_components(self.trusted_root, root, '包内分析暂存根')
        ensure_no_link_components(root, path, '包内分析端点')
        root_real, target_real = os.path.realpath(root), os.path.realpath(path)
        if os.path.commonpath([root_real, target_real]) != root_real:
            raise ValueError('包内分析端点越出暂存根: ' + path)
