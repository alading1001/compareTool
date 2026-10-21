"""归档内容与权限分别交付，覆盖真实工作线程和递归报告。"""
import hashlib
import io
import stat
import tarfile
import zipfile

from archive_workflow_fixtures import WorkflowCase, tree_hashes
from diff_engine import DiffEngine, DiffResult
from report_generator import ReportGenerator
from vcs.archive_vcs import ArchiveVCS
from vcs.base import FilePermissionChange


def package(path, entries, unix=True):
    if path.suffix == '.zip':
        with zipfile.ZipFile(path, 'w') as archive:
            for name, (body, mode) in entries.items():
                item = zipfile.ZipInfo(name)
                item.create_system = 3 if unix else 0
                item.external_attr = (stat.S_IFREG | mode) << 16 if unix else 0x20
                archive.writestr(item, body)
    else:
        with tarfile.open(path, 'w') as archive:
            for name, (body, mode) in entries.items():
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(body), mode
                archive.addfile(item, io.BytesIO(body))


class ArchivePermissionClassificationTests(WorkflowCase):
    def task(self, old, new, **options):
        return dict(vcs_type='archive', project_name='Demo', project_path='',
                    old_version=str(old), new_version=str(new), exclude_rules='', **options)

    def test_real_workflow_keeps_only_content_or_path_changes_in_delivery(self):
        for suffix in ('.zip', '.tar'):
            with self.subTest(suffix=suffix):
                old, new = self.root/('old'+suffix), self.root/('new'+suffix)
                before = {'same.txt': b'unchanged\n', 'same.jar': b'\x00same', 'empty': b'',
                          'text.txt': b'old\n', 'binary.class': b'\x00a',
                          'format.txt': b'line\n', 'removed': b'deleted', 'before.bin': b'\x00rename'}
                after = dict(before, **{'text.txt': b'new\n', 'binary.class': b'\x00b',
                                        'format.txt': b'line\r\n', 'added': b''})
                del after['removed']
                after['after.bin'] = after.pop('before.bin')
                package(old, {name:(body,0o664) for name,body in before.items()})
                package(new, {name:(body,0o640) for name,body in after.items()})
                hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (old,new)]
                app, result, out = self.generate(self.task(old,new), False, self.root/suffix[1:])
                self.assert_success(app)
                self.assertEqual({'text.txt':'M', 'binary.class':'M', 'format.txt':'F',
                    'removed':'D', 'added':'A', 'after.bin':'R'}, {f.file_path:f.report_type for f in result.files})
                self.assertEqual(2,result.summary['modified_files'])
                self.assertEqual({'same.txt','same.jar','empty'}, {p.path for p in result.permission_changes})
                for side, entries in (('oldVersion',before),('newVersion',after)):
                    expected = {name:body for name,body in entries.items() if name not in ('same.txt','same.jar','empty')}
                    self.assertEqual(set('Demo/'+name for name in expected),set(tree_hashes(out/side)))
                    for name,body in expected.items():
                        self.assertEqual(body,(out/side/'Demo'/name).read_bytes())
                report = (out/'report.html').read_text('utf-8')
                self.assertIn('仅权限变化（内容相同）：3 个',report)
                self.assertIn('Demo/same.txt：0664 → 0640',report)
                self.assertNotIn('data-path="same.txt"',report)
                instructions = next(out.glob('*操作说明.txt')).read_text('utf-8-sig')
                self.assertIn('[文件权限] Demo/same.txt：0664 → 0640',instructions)
                self.assertEqual(hashes,[hashlib.sha256(p.read_bytes()).hexdigest() for p in (old,new)])

    def test_permission_only_project_still_has_report_and_instructions(self):
        old,new = self.root/'old.zip',self.root/'new.zip'
        package(old,{'run.sh':(b'echo same\n',0o644)})
        package(new,{'run.sh':(b'echo same\n',0o755)})
        app,result,out = self.generate(self.task(old,new),False)
        self.assert_success(app)
        self.assertEqual([],result.files)
        self.assertEqual(0,result.summary['total_files'])
        self.assertEqual({},tree_hashes(out/'newVersion'))
        instructions = next(out.glob('*操作说明.txt')).read_text('utf-8-sig')
        self.assertIn('Demo/run.sh：0644 → 0755',instructions)
        self.assertIn('无需复制内容',instructions)

    def test_unknown_zip_permissions_do_not_invent_a_change(self):
        for changed in (False,True):
            old,new = self.root/'old.zip',self.root/'new.zip'
            package(old,{'a.txt':(b'old',0o644)},unix=False)
            package(new,{'a.txt':(b'new' if changed else b'old',0o644)})
            vcs = ArchiveVCS(str(old),str(new))
            try:
                result = DiffEngine(vcs).generate_diff('old','new')
                self.assertEqual(['M'] if changed else [],[f.report_type for f in result.files])
                self.assertEqual([],result.permission_changes)
            finally:
                vcs.cleanup()

    def test_root_and_exclusions_apply_to_permission_records(self):
        for suffix in ('.zip','.tar'):
            old,new = self.root/('old'+suffix),self.root/('new'+suffix)
            package(old,{'v1/keep':(b'keep',0o644),'v1/skip':(b'skip',0o644)})
            package(new,{'v2/keep':(b'keep',0o755),'v2/skip':(b'skip',0o755)})
            vcs = ArchiveVCS(str(old),str(new),ignore_single_root=True,extraction_excludes=['skip'])
            try:
                for _ in range(2):
                    result = DiffEngine(vcs).generate_diff('old','new')
                    self.assertEqual([],result.files)
                    self.assertEqual(['keep'],[p.path for p in result.permission_changes])
            finally:
                vcs.cleanup()

    def test_nested_permission_changes_do_not_enter_parent_delivery(self):
        old_dir,new_dir = self.root/'old',self.root/'new'
        old_dir.mkdir(); new_dir.mkdir()
        package(old_dir/'app.zip',{'run.sh':(b'echo same\n',0o644)})
        package(new_dir/'app.zip',{'run.sh':(b'echo same\n',0o755)})
        app,result,out = self.generate(dict(vcs_type='folder',project_name='Demo',project_path='',
            old_version=str(old_dir),new_version=str(new_dir),exclude_rules=''),True)
        self.assert_success(app)
        self.assertEqual(['M'],[f.report_type for f in result.files])
        detail = result.files[0].archive_details
        self.assertEqual([],detail['members'])
        self.assertEqual(0,detail['counts']['modified_files'])
        self.assertEqual(['run.sh'],[p.path for p in detail['permission_changes']])
        self.assertEqual([],result.permission_changes)
        self.assertIn('run.sh：0644 → 0755',(out/'report.html').read_text('utf-8'))
        self.assertNotIn('run.sh',next(out.glob('*操作说明.txt')).read_text('utf-8-sig'))
        self.assertEqual((new_dir/'app.zip').read_bytes(),(out/'newVersion/Demo/app.zip').read_bytes())

    def test_single_multi_and_basic_reports_escape_permission_paths(self):
        result = DiffResult('project','Demo','ArchiveVCS','old','new',
            permission_changes=[FilePermissionChange('</script><b>&.txt','0644','0755')])
        single,multi = self.root/'single.html',self.root/'multi.html'
        generator = ReportGenerator()
        generator.generate(result,str(single))
        generator.generate_multi([dict(project_name='Demo',vcs_type='archive',diff_result=result,
            show_project_root=True,old_version='old',new_version='new')],str(multi))
        for path in (single,multi):
            report = path.read_text('utf-8')
            self.assertIn('&lt;/script&gt;&lt;b&gt;&amp;.txt',report)
            self.assertNotIn('</script><b>',report)
            self.assertIn('\\u003c/script\\u003e',report)
        basic = ''.join(generator._basic_chunks([dict(diff_result=result)]))
        self.assertIn('仅权限变化',basic)
        self.assertIn('&lt;/script&gt;',basic)
