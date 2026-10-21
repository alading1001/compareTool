"""生成权限展示验收报告；可选用原始 TAR 样本复核四个内容修改及导出字节。"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from diff_engine import DiffResult
from main import CompareToolApp, DEFAULT_EXCLUDE_RULES
from report_generator import ReportGenerator
from test_archive_permission_classification import package
from vcs.base import FilePermissionChange


def digest(stream):
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        value.update(block)
    return value.hexdigest()


def tar_inventory(path):
    with tarfile.open(path) as archive:
        return {m.name: (digest(archive.extractfile(m)), m.mode) for m in archive if m.isfile()}


def worker(output, old, new, *, mode='archive', excludes='', recursive=False, full=True):
    output.mkdir()
    app = CompareToolApp.__new__(CompareToolApp)
    app.root = mock.Mock()
    app.root.after.side_effect = lambda _ms, callback: callback()
    app._on_complete = mock.Mock()
    app._show_error = mock.Mock()
    app._task_log_dir = output / 'jobs'
    captured = []
    generate = ReportGenerator.generate

    def observe(generator, result, *args, **kwargs):
        rendered = generate(generator, result, *args, **kwargs)
        if output.name.startswith('mixed-'):
            # 小型合成案例随后复用到多项目报告；在任务关闭片段存储前保存正文。
            result = copy.copy(result)
            result.files = [copy.copy(file) for file in result.files]
            for file in result.files:
                if file.html_fragment:
                    file.side_by_side_html = ''.join(file.html_fragment.iter_text())
                    file.html_fragment = None
        captured.append(result)
        return rendered

    started = time.perf_counter()
    with mock.patch.object(ReportGenerator, 'generate', observe):
        app._do_generate('', mode, str(old), str(new), 'Demo', excludes.splitlines(), full,
                         True, str(output / 'report.html'), str(output / 'oldVersion'),
                         str(output / 'newVersion'), str(output), recursive_archives=recursive)
    assert not app._show_error.called, app._show_error.call_args
    assert app._last_task_record['success'], app._last_task_record
    assert len(captured) == 1
    result = captured[0]
    assert not result.issues, result.issues
    return result, time.perf_counter() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--old-archive', type=Path)
    parser.add_argument('--new-archive', type=Path)
    args = parser.parse_args()
    assert bool(args.old_archive) == bool(args.new_archive)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    runtime = output / 'runtime'
    runtime.mkdir()
    os.environ.update(TEMP=str(runtime), TMP=str(runtime), COMPARETOOL_TEMP_DIR=str(runtime))
    reports = []
    checks = []

    def record(name, result, elapsed):
        reports.append(str(output / name / 'report.html'))
        checks.append(dict(case=name, seconds=elapsed, summary=result.summary,
                           accompanied_permissions=sum(f.has_permission_change for f in result.files)))

    with mock.patch('logger.LOG_FILE', str(output / 'application.log')):
        before = {'same.txt': (b'same\n', 0o644), 'text.txt': (b'old\n', 0o644),
                  'format.txt': (b'line\n', 0o644), 'before.txt': (b'renamed\n', 0o644),
                  'removed.txt': (b'deleted\n', 0o644), 'nested/empty': (b'', 0o644)}
        after = {'same.txt': (b'same\n', 0o755), 'text.txt': (b'new\n', 0o755),
                 'format.txt': (b'line\r\n', 0o755), 'after.txt': (b'renamed\n', 0o755),
                 'added.txt': (b'added\n', 0o644), 'nested/empty': (b'', 0o640)}
        for suffix in ('.zip', '.tar'):
            old, new = output / ('old' + suffix), output / ('new' + suffix)
            package(old, before); package(new, after)
            name = 'mixed-' + suffix[1:]
            result, elapsed = worker(output / name, old, new, full=suffix == '.zip')
            assert sorted(f.report_type for f in result.files) == ['A','D','F','M','R']
            assert len(result.permission_changes) == 2
            for side, entries in (('oldVersion', before), ('newVersion', after)):
                actual = {p.relative_to(output/name/side/'Demo').as_posix(): p.read_bytes()
                          for p in (output/name/side/'Demo').rglob('*') if p.is_file()}
                assert actual == {p: b for p, (b, _) in entries.items() if p not in ('same.txt','nested/empty')}
            record(name, result, elapsed)

        generator = ReportGenerator()
        projects = []
        for index in range(2):
            item = copy.deepcopy(result)
            item.project_name = f'Project{index}'
            projects.append(dict(project_name=item.project_name, diff_result=item,
                                 vcs_type='archive', show_project_root=False))
        CompareToolApp.__new__(CompareToolApp)._check_multi_display_path_conflicts(projects)
        assert all(p['show_project_root'] for p in projects)
        multi = output / 'multi.html'
        generator.generate_multi(projects, str(multi))
        reports.append(str(multi))

        only = DiffResult('fixture', 'OnlyPermissions', 'archive', 'old', 'new',
                          permission_changes=[FilePermissionChange('nested/only.txt', '0644', '0755'),
                              FilePermissionChange('</script><script>window.__reportXss=1</script>.txt', '0640', '0644')])
        generator.generate(only, str(output / 'only-permissions.html'))
        reports.append(str(output / 'only-permissions.html'))
        empty = DiffResult('fixture', 'Empty', 'folder', 'old', 'new')
        generator.generate(empty, str(output / 'empty.html'))
        reports.append(str(output / 'empty.html'))

        old_dir, new_dir = output / 'nested-old', output / 'nested-new'
        old_dir.mkdir(); new_dir.mkdir()
        package(old_dir/'app.zip', {'inside.sh': (b'echo same\n', 0o644)})
        package(new_dir/'app.zip', {'inside.sh': (b'echo same\n', 0o755)})
        nested, elapsed = worker(output / 'nested', old_dir, new_dir, mode='folder', recursive=True)
        assert len(nested.files) == 1 and not nested.permission_changes
        assert len(nested.files[0].archive_details['permission_changes']) == 1
        assert (output/'nested/newVersion/Demo/app.zip').read_bytes() == (new_dir/'app.zip').read_bytes()
        record('nested', nested, elapsed)

        if args.old_archive:
            sources = (args.old_archive, args.new_archive)
            hashes = []
            for source in sources:
                with source.open('rb') as stream:
                    hashes.append(digest(stream))
            inventories = [tar_inventory(p) for p in sources]
            raw_changes = {p for p in inventories[0] if inventories[0][p][0] != inventories[1][p][0]}
            raw_permissions = {p for p in inventories[0] if p not in raw_changes and inventories[0][p][1] != inventories[1][p][1]}
            assert len(raw_changes) == 4 and len(raw_permissions) == 2877
            for name, excludes, recursive, full, permission_count in (
                ('actual-all', '', False, True, 2877),
                ('actual-default-recursive', DEFAULT_EXCLUDE_RULES, True, False, 2872),
            ):
                actual, elapsed = worker(output/name, *sources, excludes=excludes, recursive=recursive, full=full)
                assert {f.file_path for f in actual.files} == raw_changes
                assert all(f.report_type == 'M' for f in actual.files)
                assert len(actual.permission_changes) == permission_count
                for index, side in enumerate(('oldVersion','newVersion')):
                    files = [p for p in (output/name/side/'Demo').rglob('*') if p.is_file()]
                    assert len(files) == 4
                    for path in files:
                        with path.open('rb') as stream:
                            assert digest(stream) == inventories[index][path.relative_to(output/name/side/'Demo').as_posix()][0]
                record(name, actual, elapsed)
                print(json.dumps(checks[-1], ensure_ascii=False), flush=True)
            for source, expected in zip(sources, hashes):
                with source.open('rb') as stream:
                    assert digest(stream) == expected

    receipt = dict(status='SOURCE_VERIFIED', cases=checks, reports=reports,
                   source_archives_unchanged=bool(args.old_archive))
    (output/'verification.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(status=receipt['status'], reports=len(reports), output=str(output)), ensure_ascii=False))


if __name__ == '__main__':
    main()
