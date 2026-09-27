"""Deterministic renderer/glob benchmark. Run baseline/candidate in separate processes."""
import argparse, hashlib, html, json, re, statistics, sys, time
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--repeats',type=int,default=3)
a=parser.parse_args()
if a.repeats<1:parser.error('repeats must be positive')
root=a.source_root.resolve();sys.path[:0]=[str(root),str(root/'tests')]
from stable_diff import _changed_pair, _line_groups
from diff_engine import DiffEngine
from vcs.base import BaseVCS
from test_complete_export_review_fixes import BytesVCS, TableRows
import main

def measure(fn,verify):
    values=[]
    for _ in range(a.repeats):
        start=time.perf_counter();value=fn();values.append(time.perf_counter()-start)
        verify(value)
    return dict(seconds=values,median_seconds=statistics.median(values),output_verified=True)

def require(condition):
    if not condition:raise AssertionError('benchmark content mismatch')

result=dict(source=str(root),python=sys.version,repeats=a.repeats,
            scope='In-memory matching/rendering/glob only. Verification outside timed region; no export/transaction/browser.')
result['long_line']=[];result['interior_repeated_lines']=[]
for n in (2000,4000,8000):
    old,new='ab'*(n//2)+'X','ab'*(n//2)+'Y'
    record=measure(lambda:_changed_pair(old,new),lambda pair:require(
        re.sub('<[^>]+>','',pair[0])==old and re.sub('<[^>]+>','',pair[1])==new))
    result['long_line'].append(dict(n=n,**record))
    left=['old-start']*64+['repeat']*n+['old-end']*64
    right=['new-start']*64+['repeat']*n+['new-end']*64
    def verify(groups):
        i=k=0
        for group in groups:
            for tag,x,y,u,v in group:
                require((i,k)==(x,u))
                if tag=='equal':require(left[x:y]==right[u:v])
                i,k=y,v
        require((i,k)==(len(left),len(right)))
    record=measure(lambda:list(_line_groups(left,right,False,3)),verify)
    result['interior_repeated_lines'].append(dict(n=n,**record))
    print('matched',n,flush=True)
result['engine_routes']=[]
for count in (1,5,64):
    left=['ab'*1000+'X']+['old-row']*(count-1)
    right=['ab'*1000+'Y']+['new-row']*(count-1)
    vcs=BytesVCS(('\n'.join(left)).encode(),('\n'.join(right)).encode())
    def verify(file):
        table=TableRows(file.side_by_side_html)
        require(table.side()==list(enumerate(left,1)))
        require(table.side(True)==list(enumerate(right,1)))
        require((file.added_lines,file.deleted_lines)==(count,count))
    record=measure(lambda:DiffEngine(vcs).generate_diff('old','new').files[0],verify)
    result['engine_routes'].append(dict(lines=count,**record))
patterns=[p.strip() for p in main.DEFAULT_EXCLUDE_RULES.splitlines() if p.strip()]
paths=[f'src/mod{i%127}/File{i}.java' for i in range(20000)]+[f'target/classes/Type{i}.class' for i in range(1000)]
paths+=['root/file.py','nested/build/data.bin','.git/objects/a','target/one/x','root/target/x','logs/app.log']
expected=[any(BaseVCS._match_glob_pattern(p,g,True) for g in patterns) for p in paths]
result['glob']=measure(lambda:[any(BaseVCS._match_glob_pattern(p,g,True) for g in patterns) for p in paths],lambda x:require(x==expected))
result['glob']['decision_sha256']=hashlib.sha256(bytes(expected)).hexdigest()
result['input_sha256']=hashlib.sha256(json.dumps([patterns,paths],ensure_ascii=False).encode()).hexdigest()
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print('RESULT',a.output,flush=True)
