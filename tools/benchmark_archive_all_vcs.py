"""Six-mode off/on worker benchmark on disposable local Git/SVN repositories."""
import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tracemalloc
from unittest import mock

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--repeats', type=int, default=3)
args = parser.parse_args()
if args.repeats < 3: parser.error('Use at least three repetitions.')
source = args.source_root.resolve()
os.chdir(source)
sys.path[:0] = [str(source), str(source/'tests')]
from test_archive_report_all_vcs import AllVCSArchiveTests
import main

case = AllVCSArchiveTests('test_six_modes_off_on_identity_and_zero_recursive_repository_reads')
case.setUp()
records = []
original_popen, original_enrich = subprocess.Popen, main.enrich_archive_reports
active = False
counters = {}


def tracked_popen(command, *positional, **keywords):
    argv = list(command) if isinstance(command, (list, tuple)) else []
    name = Path(str(argv[0])).name.lower() if argv else ''
    content = ((name.startswith('git') and any(x in argv for x in ('show', 'cat-file')))
               or (name.startswith('svn') and 'cat' in argv))
    if content:
        counters['content_process_starts'] += 1
        counters['recursive_content_process_starts'] += int(active)
    return original_popen(command, *positional, **keywords)


def tracked_enrich(*positional, **keywords):
    global active
    active = True
    try:
        return original_enrich(*positional, **keywords)
    finally:
        active = False


try:
    tasks = case.six_tasks()
    for task in tasks:
        baseline = None
        for repeat in range(args.repeats):
            for enabled in (False, True):
                counters = dict(content_process_starts=0, recursive_content_process_starts=0)
                out = case.root/(task['vcs_type']+'_'+str(repeat)+'_'+str(enabled))
                with mock.patch('subprocess.Popen', tracked_popen), \
                        mock.patch('main.enrich_archive_reports', tracked_enrich):
                    app, result, out = case.generate(task, enabled, out)
                case.assert_success(app)
                if baseline is None:
                    baseline = (out, result.summary)
                else:
                    case.assert_same_delivery(baseline[0], out)
                    assert baseline[1] == result.summary
                assert counters['recursive_content_process_starts'] == 0
                record = app._last_task_record
                records.append(dict(mode=task['vcs_type'], recursive=enabled, repeat=repeat+1,
                    seconds=record['seconds'], phases=record['phases'], **counters))
                print(task['vcs_type'], enabled, repeat+1, round(record['seconds'], 3), flush=True)
    # Allocation tracing is a separate run, not included in timed medians.
    memory = []
    def memory_enrich(*positional, **keywords):
        tracemalloc.start()
        try:
            return original_enrich(*positional, **keywords)
        finally:
            _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
            memory.append(peak)
    with mock.patch('main.enrich_archive_reports', memory_enrich):
        app, _, _ = case.generate(tasks[0], True, case.root/'memory-only')
    case.assert_success(app)
finally:
    case.doCleanups()

# The calling terminal can retain this JSON as its benchmark log.
print('BENCHMARK_JSON=' + json.dumps({
    'repeats': args.repeats,
    'runs': records,
    'separate_recursive_peak_python_bytes': memory,
    'scope': 'Small local fixtures. Worker time includes export and final commit; '
             'excludes app startup, browser and fixture creation. Memory tracing '
             'is a separate run. Process counts are not logical object reads.'
}, ensure_ascii=False), flush=True)
