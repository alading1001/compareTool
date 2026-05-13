import os
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

        # **/ 替换为占位符（可选目录前缀，包括根目录）
        pattern = pattern.replace('**/', '\x00')
        # 将剩余的 ** 替换为占位符
        pattern = pattern.replace('**', '\x01')
        # 转义正则特殊字符
        regex = re.escape(pattern)
        # \x00 表示 **/ → 可选目录前缀
        regex = regex.replace('\x00', '(.*/)?')
        # \x01 表示 ** → 任意字符（用于 target/** 尾部的 **）
        regex = regex.replace('\x01', '.*')
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

    def get_file_content_bytes(self, version: str, file_path: str) -> bytes:
        """获取指定版本的文件原始字节。失败返回 None，文件为空返回 b"""""
        content = self.get_file_content(version, file_path)
        return content.encode("utf-8") if content else None

    def get_file_content_bytes_working(self, file_path: str) -> bytes:
        """从工作目录读取文件原始字节。失败返回 None，文件为空返回 b"""""
        full_path = os.path.join(self.project_path, file_path)
        if not os.path.exists(full_path) or os.path.isdir(full_path):
            return None
        with open(full_path, "rb") as f:
            return f.read()

    @staticmethod
    def _is_text_bytes(data: bytes) -> bool:
        """不含 null 字节的数据视为文本文件"""
        return b'\x00' not in data

    @staticmethod
    def _apply_crlf(data: bytes) -> bytes:
        """将单独的 LF 转为 CRLF，模拟 Windows checkout 换行符转换"""
        return re.sub(rb'(?<!\r)\n', b'\r\n', data)

    @abstractmethod
    def get_versions(self) -> List[str]:
        """获取可用的版本列表（tags/branches/revisions）"""
        ...

    @abstractmethod
    def check_version_exists(self, version: str) -> bool:
        """检查版本是否存在"""
        ...
