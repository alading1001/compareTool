import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import List


class ChangeType(Enum):
    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"
    RENAMED = "R"


@dataclass
class ChangedFile:
    path: str
    change_type: ChangeType
    old_path: str = ""  # for renamed files


class BaseVCS(ABC):
    """VCS抽象基类，定义统一接口"""

    def __init__(self, project_path: str):
        self.project_path = project_path
        self.exclude_patterns: List[str] = []

    def set_exclude_patterns(self, patterns: List[str]):
        """设置排除规则列表，支持 glob 模式：
           *.class     - 排除所有 .class 文件
           target/**   - 排除 target 目录下所有内容
           **/test/**  - 排除任意路径下的 test 目录
        """
        self.exclude_patterns = [p.strip() for p in patterns if p.strip()]

    def _is_excluded(self, file_path: str) -> bool:
        """判断文件路径是否匹配任一排除规则"""
        file_path = file_path.replace('\\', '/')
        for pattern in self.exclude_patterns:
            if self._match_glob(file_path, pattern):
                return True
        return False

    def _match_glob(self, path: str, pattern: str) -> bool:
        """将 glob 模式转为正则匹配"""
        pattern = pattern.replace('\\', '/')

        # 不含 / 的简单模式（如 *.class）匹配任意目录深度
        if '/' not in pattern and '**' not in pattern:
            pattern = '**/' + pattern

        # 将 ** 替换为占位符
        pattern = pattern.replace('**', '\x00')
        # 转义正则特殊字符
        regex = re.escape(pattern)
        # ** 匹配任意层级目录
        regex = regex.replace('\x00', '.*')
        # 单个 * 匹配单级目录内的任意字符（不含 /）
        regex = regex.replace(r'\*', '[^/]*')

        return re.match('^' + regex + '$', path) is not None

    def _filter_files(self, files: List[ChangedFile]) -> List[ChangedFile]:
        """过滤掉匹配排除规则的文件"""
        if not self.exclude_patterns:
            return files
        return [f for f in files if not self._is_excluded(f.path)]

    @abstractmethod
    def get_changed_files(self, old_version: str, new_version: str) -> List[ChangedFile]:
        """获取两个版本之间的变更文件列表"""
        ...

    @abstractmethod
    def get_file_content(self, version: str, file_path: str) -> str:
        """获取指定版本的文件内容"""
        ...

    @abstractmethod
    def get_file_content_working(self, file_path: str) -> str:
        """从工作目录读取文件内容（代表新版本）"""
        ...

    @abstractmethod
    def get_versions(self) -> List[str]:
        """获取可用的版本列表（tags/branches/revisions）"""
        ...

    @abstractmethod
    def check_version_exists(self, version: str) -> bool:
        """检查版本是否存在"""
        ...
