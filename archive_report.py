"""Report-only recursive archive inspection; never adds delivery entries."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
import re
import zipfile
from typing import Optional

from path_safety import open_regular_file_no_links
from task_progress import measured_phase, progress
from vcs.archive_vcs import ArchiveVCS
from vcs.folder_vcs import FolderVCS
from vcs.base import comparison_versions
from vcs.temp_storage import create_temp_dir, remove_temp_dir

ARCHIVE_SUFFIXES = ('.zip', '.jar', '.war', '.ear', '.aar', '.tar',
                    '.tar.gz', '.tgz', '.tar.bz2', '.tbz2')


def is_report_archive(path):
    return str(path or '').lower().endswith(ARCHIVE_SUFFIXES)


def has_archive_candidates(result):
    from vcs.base import ChangeType
    return any(is_report_archive(file.file_path) or (
        file.change_type == ChangeType.RENAMED and is_report_archive(file.old_path)
    ) for file in result.files)


def _is_lfs_pointer(path):
    # The LFS specification requires a complete UTF-8 pointer smaller than
    # 1024 bytes. Never read an entire archive just to diagnose a pointer.
    with open_regular_file_no_links(path, deny_writes=True) as source:
        if os.fstat(source.fileno()).st_size >= 1024:
            return False
        data = source.read(1024)
    if not data or not data.endswith(b'\n') or b'\r' in data:
        return False
    try:
        lines = data.decode('utf-8').split('\n')[:-1]
    except UnicodeDecodeError:
        return False
    pairs = [line.split(' ', 1) for line in lines]
    if any(len(pair) != 2 or not re.fullmatch(r'[a-z0-9.-]+', pair[0])
           or not pair[1] for pair in pairs):
        return False
    keys = [pair[0] for pair in pairs]
    values = dict(pairs)
    return (keys[0] == 'version' and len(keys) == len(values)
            and keys[1:] == sorted(keys[1:])
            and values.get('version') in ('https://git-lfs.github.com/spec/v1',
                                          'https://hawser.github.com/spec/v1')
            and re.fullmatch(r'sha256:[0-9a-f]{64}', values.get('oid', '')) is not None
            and re.fullmatch(r'0|[1-9][0-9]*', values.get('size', '')) is not None)


@dataclass
class ArchiveReportBudget:
    """全报告共享计数；默认不限规模，显式策略才限制资源用量。"""
    max_depth: Optional[int] = None
    max_members: Optional[int] = None
    max_bytes: Optional[int] = None
    members: int = 0
    declared_bytes: int = 0
    actual_bytes: int = 0

    def reserve(self, count, size):
        if self.max_members is not None and self.members + count > self.max_members:
            raise ValueError('包内递归比较累计成员数超过安全限制，已中止')
        if self.max_bytes is not None and self.declared_bytes + size > self.max_bytes:
            raise ValueError('包内递归比较累计展开字节超过安全限制，已中止')
        self.members += count
        self.declared_bytes += size

    def consume(self, size):
        if self.max_bytes is not None and self.actual_bytes + size > self.max_bytes:
            raise ValueError('包内递归比较实际展开字节超过安全限制，已中止')
        self.actual_bytes += size


class _ReportArchiveVCS(ArchiveVCS):
    """Keep the existing validator/extractor, adding a shared expansion budget."""
    def __init__(self, old, new, budget, *, extraction_excludes=None):
        self._report_budget = budget
        self._reserved_sources = {}
        super().__init__(old, new, ignore_single_root=False, extraction_excludes=extraction_excludes)

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

    def _consume_archive_bytes(self, size):
        # Includes discarded bodies exactly once; creating empty placeholders
        # itself consumes no expanded-content budget.
        self._report_budget.consume(size)


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
        raise ValueError('此来源没有可直接借用的快照，请显式传入暂存端点')
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


def iter_archive_events(detail):
    """按深度优先顺序逐项输出模板事件，不递归或拼接整棵 HTML。"""
    pending = [('section', detail)]
    while pending:
        kind, value = pending.pop()
        if kind == 'members':
            member = next(value, None)
            if member is None:
                continue
            pending.append(('members', value))
            pending.append(('member_end', None))
            if member.archive_details:
                pending.append(('section', member.archive_details))
            yield 'member', None, member
        elif kind == 'section':
            pending.append(('section_end', None))
            if value.get('status') == 'compared':
                pending.append(('members', iter(value.get('members', ()))))
            yield kind, value, None
        else:
            yield kind, None, None


class _Inspector:
    def __init__(self, show_full_context, patterns, budget, detail_store=None, best_effort=False):
        self.show_full_context = show_full_context
        self.patterns = list(patterns or [])
        self.budget = budget
        self.detail_store = detail_store
        self.best_effort = best_effort
        self.issues = []

    def attach(self, result, vcs=None, depth=1, chain=(), *, endpoints=None):
        # 每个生成器保存本层 VCS/空包的上下文，在子层完成后才释放。
        # 子层异常逐层 throw 回父生成器，沿用严格/尽量输出的既有语义。
        pending = [self._attach(result, vcs, depth, chain, endpoints=endpoints)]
        failure = None
        try:
            while pending:
                try:
                    child = (next(pending[-1]) if failure is None
                             else pending[-1].throw(failure))
                except StopIteration:
                    pending.pop()
                    failure = None
                except Exception as exc:
                    pending.pop()
                    failure = exc
                else:
                    failure = None
                    pending.append(self._attach(*child))
            if failure is not None:
                raise failure
        finally:
            cleanup_error = None
            for iterator in reversed(pending):
                try:
                    iterator.close()
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
            if cleanup_error is not None:
                raise cleanup_error

    def _attach(self, result, vcs=None, depth=1, chain=(), *, endpoints=None):
        from vcs.base import ChangeType
        if endpoints is None:
            old_version, new_version = comparison_versions(vcs, result.old_version, result.new_version)
        for file in result.files:
            old_name = (file.old_path or file.file_path) if file.change_type == ChangeType.RENAMED else file.file_path
            if not (is_report_archive(file.file_path) or is_report_archive(old_name)):
                continue
            try:
                old = None if file.change_type == ChangeType.ADDED else (
                    endpoints.path_for('old', old_name) if endpoints is not None
                    else _endpoint_path(vcs, old_version, old_name))
                new = None if file.change_type == ChangeType.DELETED else (
                    endpoints.path_for('new', file.file_path) if endpoints is not None
                    else _endpoint_path(vcs, new_version, file.file_path))
            except (OSError, ValueError, RuntimeError) as exc:
                raise RuntimeError(f'项目 [{result.project_name}] 包内端点读取失败: {exc}') from exc
            names = chain + (file.file_path,)
            if ((old and not is_report_archive(old_name)) or
                    (new and not is_report_archive(file.file_path))):
                file.archive_details = dict(status='unsupported', members=[],
                    note='一侧文件格式不支持内部展开，仅保留原始文件差异。')
                continue
            try:
                with self.inspect(old, new, depth, names) as (detail, child):
                    if child is not None:
                        yield child
                    file.archive_details = detail
            except Exception as exc:
                if self.best_effort:
                    note = '包内分析未完成，原始压缩包已保留：' + str(exc)
                    file.archive_details = dict(status='error', members=[], note=note)
                    self.issues.append(dict(stage='包内分析', path=' → '.join(names),
                                            message=note, delivery=False))
                    continue
                raise RuntimeError(
                    f'项目 [{result.project_name}] 旧侧 [{old_name if old else "不存在"}] / '
                    f'新侧 [{file.file_path if new else "不存在"}]: {exc}'
                ) from exc

    @contextmanager
    def inspect(self, old, new, depth, names):
        if old and new and _same_archive_bytes(old, new):
            yield (dict(status='identical', members=[],
                        note='压缩包原始字节相同，未继续展开；重命名或属性变化见上方。'), None)
            return
        if self.budget.max_depth is not None and depth > self.budget.max_depth:
            raise RuntimeError('包内递归深度超过安全限制: ' + ' → '.join(names))
        try:
            for side, path in (('旧', old), ('新', new)):
                if path is not None and _is_lfs_pointer(path):
                    raise ValueError(f'{side}侧待交付文件是 Git LFS 指针，不是实际压缩包；本功能不下载 LFS 对象')
            if old is None or new is None:
                with _empty_archive() as empty:
                    with self.compare(old or empty, new or empty, depth, names) as prepared:
                        yield prepared
            else:
                with self.compare(old, new, depth, names) as prepared:
                    yield prepared
        except Exception as exc:
            raise RuntimeError('压缩包内部比较失败 [' + ' → '.join(names) +
                               ']: ' + str(exc)) from exc

    @contextmanager
    def compare(self, old, new, depth, names):
        from diff_engine import DiffEngine
        vcs = _ReportArchiveVCS(old, new, self.budget, extraction_excludes=self.patterns or None)
        try:
            vcs.set_exclude_patterns(self.patterns)
            result = DiffEngine(vcs, show_full_context=self.show_full_context, retain_text_contents=False, detail_store=self.detail_store).generate_diff('old', 'new')
            detail = dict(status='compared', members=result.files,
                          counts=result.summary, filtered=bool(self.patterns),
                          duplicate_members=vcs.duplicate_members)
            yield detail, (result, vcs, depth + 1, names)
            for member in result.files:
                # Only the rendered details are retained; these are not exports.
                member.old_content = member.new_content = ''
        finally:
            vcs.cleanup()


@measured_phase('archive.report', '递归分析变化压缩包（仅报告）')
def enrich_archive_reports(result, vcs=None, *, endpoints=None, show_full_context=True,
                           exclude_patterns=(), budget=None, detail_store=None, best_effort=False):
    """Attach details without changing result.files, summaries, or delivery paths."""
    if endpoints is None and not isinstance(vcs, (FolderVCS, ArchiveVCS)):
        raise ValueError('此来源没有可直接借用的快照，请显式传入暂存端点')
    inspector = _Inspector(show_full_context, exclude_patterns,
                           budget if budget is not None else ArchiveReportBudget(),
                           detail_store=detail_store, best_effort=best_effort)
    inspector.attach(result, vcs, endpoints=endpoints)
    result.issues.extend(inspector.issues)
    result.archive_details_enabled = True
    return result
