import argparse, copy, sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--source-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
p.add_argument('--store',action='store_true')
a=p.parse_args();root=a.source_root.resolve();a.output.mkdir(parents=True,exist_ok=True)
sys.path[:0]=[str(root),str(root/'tests')]
from diff_engine import DiffEngine,DiffResult,FileDiff
from report_generator import ReportGenerator
from vcs.base import ChangeType
from test_complete_export_review_fixes import BytesVCS
store = None
if a.store:
    from html_details import HtmlDetailStore
    store = HtmlDetailStore()

def text(name,full):
    old=[f'line {i:02d} '+('0123456789'*180) for i in range(30)]
    new=list(old);new[4]='NEW_A '+new[4];new[24]='NEW_B '+new[24]
    result=DiffEngine(BytesVCS(('\n'.join(old)).encode(),('\n'.join(new)).encode()),show_full_context=full, **({'detail_store':store, 'retain_text_contents':False} if store else {})).generate_diff('old','new')
    file=result.files[0];file.file_path=name;return file

def wrap(name,members):
    inner=DiffResult('fixture','Demo','folder','old','new',files=members)
    return FileDiff(name,ChangeType.MODIFIED,archive_details=dict(status='compared',members=members,counts=inner.summary,filtered=False))

def result(full):
    leaves=[text('config/value<&>.txt',full),text('config/other.txt',full),text('note </script><script>window.__reportXss=1</script>.txt',full)]
    parent=wrap('server.tar',[wrap('app.war',[wrap('lib/core.jar',leaves)])])
    extra=[FileDiff(f'dir{i%30:02d}/sub{i%3}/file{i:04d}.txt',
                    ChangeType.ADDED if i%2 else ChangeType.DELETED,
                    side_by_side_html='<p>Small complete fixture detail</p>') for i in range(600)]
    if store is not None:
        for file in extra:
            file.html_fragment = store.add_fragment(file.side_by_side_html)
            file.side_by_side_html = ''
    return DiffResult('fixture','Demo','folder','old','new',files=[text('plain.txt',full),parent,*extra],archive_details_enabled=True)

try:
    generator=ReportGenerator(str(root/'templates'))
    for full in (True,False):
        generator.generate(result(full),str(a.output/('single-full.html' if full else 'single-context.html')))
    items=[]
    for i in range(2):
        r=result(True);r.project_name=f'Demo{i}'
        items.append(dict(project_name=r.project_name,vcs_type='folder',show_project_root=True,diff_result=r))
    generator.generate_multi(items,str(a.output/'multi.html'))
    print(a.output)
finally:
    if store is not None: store.close()
