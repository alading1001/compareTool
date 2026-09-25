"""Repeatable folder-scan benchmark using only uniquely owned test data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--files', type=int, default=300)
parser.add_argument('--changed', type=int, default=30)
parser.add_argument('--repeats', type=int, default=3)
parser.add_argument('--output', type=Path)
args = parser.parse_args()
if args.files < 1 or not 0 <= args.changed <= args.files or args.repeats < 1:
    parser.error('Require files >= 1, 0 <= changed <= files, repeats >= 1')
source = args.source_root.resolve()
sys.path.insert(0, str(source))
from vcs.folder_vcs import FolderVCS

scratch = source / '.tmp'
scratch.mkdir(exist_ok=True)
runs = []
content_digests = []
with tempfile.TemporaryDirectory(prefix='folder_bench_', dir=scratch) as owned:
    fixture = Path(owned)
    old, new = fixture / 'old', fixture / 'new'
    old.mkdir(); new.mkdir()
    os.environ['COMPARETOOL_TEMP_DIR'] = str(fixture / 'runtime')
    expected = {}
    for i in range(args.files):
        rel = f'd{i % 30:02d}/File{i:04d}.java'
        a, b = old / rel, new / rel
        a.parent.mkdir(exist_ok=True); b.parent.mkdir(exist_ok=True)
        payload = ''.join(f'line {j:03d} file {i:04d} value\n' for j in range(80)).encode()
        updated = payload.replace(b'line 040', b'line XXX', 1) if i < args.changed else payload
        a.write_bytes(payload); b.write_bytes(updated)
        if i < args.changed:
            expected[rel] = (payload, updated)
    expected_items = sorted((path, 'M') for path in expected)
    for _ in range(args.repeats):
        vcs = FolderVCS(str(old), str(new))
        try:
            start = time.perf_counter()
            items = vcs.get_changed_files('old', 'new')
            runs.append(time.perf_counter() - start)
            if sorted((item.path, item.change_type.value) for item in items) != expected_items:
                raise AssertionError('Changed-file list does not match fixture')
            digest = hashlib.sha256()
            for rel, pair in sorted(expected.items()):
                for version, wanted in zip(('old', 'new'), pair):
                    actual = vcs.get_file_content_bytes(version, rel)
                    if actual != wanted:
                        raise AssertionError(f'Wrong snapshot bytes: {version}/{rel}')
                    digest.update(version.encode() + b'\0' + rel.encode() + b'\0')
                    digest.update(actual)
            content_digests.append(digest.hexdigest())
        finally:
            vcs.cleanup()
result = dict(source_root=str(source), files_each=args.files, changed=args.changed,
              runs=runs, median_seconds=statistics.median(runs),
              snapshot_bytes_verified=True, snapshot_digest=content_digests[0],
              note='Scan, byte comparison, changed-file snapshots and final source verification only')
assert len(set(content_digests)) == 1
encoded = json.dumps(result, ensure_ascii=True, indent=2)
if args.output:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding='utf-8')
print(encoded)
