"""Isolated C1 benchmark; run each source/mode in a separate process."""
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tracemalloc

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--files', type=int, default=100)
parser.add_argument('--rows', type=int, default=80)
parser.add_argument('--store', action='store_true')
parser.add_argument('--trace', action='store_true')
args = parser.parse_args()
if min(args.files, args.rows) < 1: parser.error('Use positive counts.')
root, output = args.source_root.resolve(), args.output.resolve()
output.parent.mkdir(parents=True, exist_ok=True)
os.chdir(root)
sys.path[:0] = [str(root), str(root/'tests')]
from diff_engine import DiffEngine, DiffResult
from report_generator import ReportGenerator
from test_complete_export_review_fixes import BytesVCS, TableRows

work = Path(tempfile.mkdtemp(prefix='html_bench_', dir=output.parent))
os.environ['COMPARETOOL_TEMP_DIR'] = str(work/'runtime')
import logger
logger.LOG_FILE = str(work/'application.log')
store = None
if args.store:
    from html_details import HtmlDetailStore
    store = HtmlDetailStore()
old_lines = [f'OLD_{i:05d}: ' + 'abcdef0123456789' * 3 for i in range(args.rows)]
new_lines = [f'NEW_{i:05d}: ' + 'abcdef0123456789' * 3 for i in range(args.rows)]
old, new = ('\n'.join(old_lines)).encode(), ('\n'.join(new_lines)).encode()
options = dict(retain_text_contents=False)
if store is not None: options['detail_store'] = store
result = DiffResult('synthetic', 'Bench', 'fixture', 'old', 'new')
metrics = dict(files=args.files, rows=args.rows, store=args.store, trace=args.trace,
               python=sys.version, source=str(root),
               input_sha256=hashlib.sha256(old + b'\0' + new).hexdigest(),
               scope='Generation/template phases only; no VCS/export/commit/browser. Validation excluded.')
gc.collect()
if args.trace: tracemalloc.start()
try:
    started = time.perf_counter()
    for i in range(args.files):
        item = DiffEngine(BytesVCS(old, new), **options).generate_diff('old', 'new').files[0]
        item.file_path = f'src/File{i:05d}.txt'
        result.files.append(item)
    metrics['generate_seconds'] = time.perf_counter() - started
    if args.trace:
        gc.collect()
        resident, peak = tracemalloc.get_traced_memory()
        metrics.update(generation_retained_bytes=resident, generation_peak_bytes=peak)
        tracemalloc.reset_peak()
    metrics['html_string_object_bytes'] = sum(sys.getsizeof(f.side_by_side_html) for f in result.files)
    metrics['temporary_files'] = int(store is not None and store._file is not None)
    metrics['fragment_bytes'] = store.total_bytes if store is not None else 0
    started = time.perf_counter()
    report = work/'report.html'
    ReportGenerator(str(root/'templates')).generate(result, str(report))
    metrics['template_seconds'] = time.perf_counter() - started
    if args.trace:
        now, peak = tracemalloc.get_traced_memory()
        metrics.update(template_current_bytes=now, template_peak_bytes=peak,
                       template_extra_peak_bytes=max(0, peak-resident))
        tracemalloc.stop()
    metrics['report_bytes'] = report.stat().st_size
    for f in result.files:
        text = ''.join(f.html_fragment.iter_text()) if args.store else f.side_by_side_html
        rows = TableRows(text)
        assert rows.side() == list(enumerate(old_lines, 1))
        assert rows.side(True) == list(enumerate(new_lines, 1))
    assert result.summary['total_added_lines'] == args.files * args.rows
    assert result.summary['total_deleted_lines'] == args.files * args.rows
    metrics.update(output_verified=True, summary=result.summary)
finally:
    if tracemalloc.is_tracing(): tracemalloc.stop()
    if store is not None: store.close()
output.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(metrics, ensure_ascii=False), flush=True)
# Only this run's uniquely created synthetic directory is removed.
import shutil
shutil.rmtree(work)
