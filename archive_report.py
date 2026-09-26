"""Report-only recursive archive inspection; never adds delivery entries."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
import zipfile

from path_safety import open_regular_file_no_links
from task_progress import measured_phase, progress
from vcs.archive_vcs import ArchiveVCS
from vcs.folder_vcs import FolderVCS
from vcs.temp_storage import create_temp_dir, remove_temp_dir

ARCHIVE_SUFFIXES = ('.zip', '.jar', '.war', '.ear', '.aar', '.tar',
                    '.tar.gz', '.tgz', '.tar.bz2', '.tbz2')


def is_report_archive(path):
    return str(path or '').lower().endswith(ARCHIVE_SUFFIXES)


@dataclass
class ArchiveReportBudget:
    """Shared across every nested package (and every project in one report)."""
    max_depth: int = 8
    max_members: int = 100_000
    max_bytes: int = 10 * 1024**3
    members: int = 0
    declared_bytes: int = 0
    actual_bytes: int = 0

    def reserve(self, count, size):
        if self.members + count > self.max_members:
            raise ValueError('包内递归比较累计成员数超过安全限制，已中止')
        if self.declared_bytes + size > self.max_bytes:
            raise ValueError('包内递归比较累计展开字节超过安全限制，已中止')
        self.members += count
        self.declared_bytes += size

    def consume(self, size):
        if self.actual_bytes + size > self.max_bytes:
            raise ValueError('包内递归比较实际展开字节超过安全限制，已中止')
        self.actual_bytes += size


class _CountedWriter:
    def __init__(self, stream, budget):
        self._stream, self._budget = stream, budget

    def write(self, data):
        self._budget.consume(len(data))
        return self._stream.write(data)

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._stream.__exit__(*exc)


class _ReportArchiveVCS(ArchiveVCS):
    """Keep the existing validator/extractor, adding a shared expansion budget."""
    def __init__(self, old, new, budget):
        self._report_budget = budget
        self._reserved_sources = {}
        super().__init__(old, new, ignore_single_root=False)

    def _validate_archive_limits(self, path, members, check_member_ratio=True):
        size = super()._validate_archive_limits(path, members, check_member_ratio)
        signature = (len(members), size)
        # ZIP preflight and extraction both validate the same locked source.
        key = os.path.abspath(path)
        if key not in self._reserved_sources:
            self._report_budget.reserve(*signature)
            self._reserved_sources[key] = signature
        elif self._reserved_sources[key] != signature:
            raise ValueError('包内比较源信息在预检后变化，已中止')
        return size

    def _open_archive_member_target(self, dest, target, cache):
        path, stream = super()._open_archive_member_target(dest, target, cache)
        return path, _CountedWriter(stream, self._report_budget)


@contextmanager
def _empty_archive():
    directory = create_temp_dir(prefix='comparetool_report_empty_')
    try:
        path = os.path.join(directory, 'empty.zip')
        with zipfile.ZipFile(path, 'w'):
            pass
        yield path
    finally:
        remove_temp_dir(directory)


def _endpoint_path(vcs, version, path):
    # Reuse already-fixed temporary endpoints, not live working-copy contents.
    if isinstance(vcs, ArchiveVCS):
        version = vcs._to_folder_ver(version)
        vcs = vcs._folder
    if not isinstance(vcs, FolderVCS):
        raise ValueError('包内报告首版只支持文件夹和压缩包比较')
    folder = vcs._resolve_version_dir(version)
    return vcs._resolve_file_path(folder, path)


def _same_archive_bytes(old, new):
    with open_regular_file_no_links(old, deny_writes=True) as a, \
            open_regular_file_no_links(new, deny_writes=True) as b:
        if os.fstat(a.fileno()).st_size != os.fstat(b.fileno()).st_size:
            return False
        while True:
            left, right = a.read(1024**2), b.read(1024**2)
            if left != right:
                return False
            if not left:
                return True


class _Inspector:
    def __init__(self, show_full_context, patterns, budget):
        self.show_full_context = show_full_context
        self.patterns = list(patterns or [])
        self.budget = budget

    def attach(self, result, vcs, depth=1, chain=()):
        from vcs.base import ChangeType
        for file in result.files:
            old_name = file.old_path or file.file_path
            if not (is_report_archive(file.file_path) or is_report_archive(old_name)):
                continue
            old = None if file.change_type == ChangeType.ADDED else (
                _endpoint_path(vcs, result.old_version, old_name))
            new = None if file.change_type == ChangeType.DELETED else (
                _endpoint_path(vcs, result.new_version, file.file_path))
            names = chain + (file.file_path,)
            if ((old and not is_report_archive(old_name)) or
                    (new and not is_report_archive(file.file_path))):
                file.archive_details = dict(status='unsupported', members=[],
                    note='一侧文件格式不支持内部展开，仅保留原始文件差异。')
                continue
            file.archive_details = self.inspect(old, new, depth, names)

    def inspect(self, old, new, depth, names):
        if old and new and _same_archive_bytes(old, new):
            return dict(status='identical', members=[],
                        note='压缩包原始字节相同，未继续展开；重命名或属性变化见上方。')
        if depth > self.budget.max_depth:
            raise RuntimeError('包内递归深度超过安全限制: ' + ' → '.join(names))
        try:
            if old is None or new is None:
                with _empty_archive() as empty:
                    return self.compare(old or empty, new or empty, depth, names)
            return self.compare(old, new, depth, names)
        except Exception as exc:
            raise RuntimeError('压缩包内部比较失败 [' + ' → '.join(names) +
                               ']: ' + str(exc)) from exc

    def compare(self, old, new, depth, names):
        from diff_engine import DiffEngine
        vcs = _ReportArchiveVCS(old, new, self.budget)
        try:
            vcs.set_exclude_patterns(self.patterns)
            result = DiffEngine(vcs, show_full_context=self.show_full_context).generate_diff('old', 'new')
            self.attach(result, vcs, depth + 1, names)
            for member in result.files:
                # Only the rendered details are retained; these are not exports.
                member.old_content = member.new_content = ''
            return dict(status='compared', members=result.files,
                        counts=result.summary, filtered=bool(self.patterns))
        finally:
            vcs.cleanup()


@measured_phase('archive.report', '递归分析变化压缩包（仅报告）')
def enrich_archive_reports(result, vcs, *, show_full_context=True,
                           exclude_patterns=(), budget=None):
    """Attach details without changing result.files, summaries, or delivery paths."""
    if not isinstance(vcs, (FolderVCS, ArchiveVCS)):
        raise ValueError('包内报告首版只支持文件夹和压缩包比较')
    inspector = _Inspector(show_full_context, exclude_patterns,
                           budget if budget is not None else ArchiveReportBudget())
    inspector.attach(result, vcs)
    result.archive_details_enabled = True
    return result
