# Ordered extraction and immutable body-discard plans use real archive I/O.
import io
import os
from pathlib import Path
import stat
import tarfile
import zipfile
from unittest import mock

from archive_workflow_fixtures import WorkflowCase
from test_archive_report import zip_bytes, tar_bytes
from vcs.archive_vcs import ArchiveVCS
from archive_report import ArchiveReportBudget, _ReportArchiveVCS
from diff_engine import DiffEngine
from file_exporter import FileExporter


class ArchiveStreamingTests(WorkflowCase):
    def archives(self, old, new, suffix='.zip'):
        mode = {'.tar': 'w', '.tar.gz': 'w:gz', '.tgz': 'w:gz',
                '.tar.bz2': 'w:bz2', '.tbz2': 'w:bz2'}
        encode = (lambda files: tar_bytes(files, mode[suffix])) if suffix in mode else zip_bytes
        a, b = self.root/('old'+suffix), self.root/('new'+suffix)
        a.write_bytes(encode(old)); b.write_bytes(encode(new))
        return str(a), str(b)

    def result(self, paths, patterns, sparse=False, root=False):
        vcs = ArchiveVCS(*paths, ignore_single_root=root,
                         extraction_excludes=patterns if sparse else None)
        self.addCleanup(vcs.cleanup)
        vcs.set_exclude_patterns(patterns)
        result = DiffEngine(vcs).generate_diff('old', 'new')
        return vcs, result

    def identity(self, result):
        return (result.summary, result.required_directory_deletions,
                [(f.file_path, f.old_path, f.change_type, f.old_mode,
                  f.new_mode, f.metadata_changes) for f in result.files])

    def test_tar_formats_consume_in_order_without_getmembers(self):
        for suffix in ('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2'):
            with self.subTest(suffix=suffix):
                files = {'nested/'+('x'*140)+'.txt': b'payload\n', 'empty': b''}
                paths = self.archives(files, files, suffix)
                with mock.patch.object(tarfile.TarFile, 'getmembers',
                                       side_effect=AssertionError('extra full scan')):
                    vcs = ArchiveVCS(*paths)
                    try:
                        for name, data in files.items():
                            self.assertEqual(data, vcs.get_file_content_raw_bytes('old', name))
                        self.assertEqual([], vcs.get_changed_files())
                    finally: vcs.cleanup()

    def test_tar_gnu_long_names_permissions_and_preflight_order_binding(self):
        paths=[]
        for side, text in [('old',b'old'),('new',b'new')]:
            path=self.root/(side+'.tar.gz'); paths.append(str(path))
            with tarfile.open(path,'w:gz',format=tarfile.GNU_FORMAT) as tf:
                item=tarfile.TarInfo('long/'+('x'*150)+'.sh')
                item.size=len(text); item.mode=0o755
                tf.addfile(item,io.BytesIO(text))
        vcs,result=self.result(paths,[])
        self.assertEqual('0755',result.files[0].new_mode)
        original=ArchiveVCS._get_tar_plan
        def wrong(obj,source,dest):
            size,members=original(obj,source,dest)
            return size,tuple(m._replace(name='different.txt') for m in members)
        with mock.patch.object(ArchiveVCS,'_get_tar_plan',wrong):
            with self.assertRaisesRegex(ValueError,'预检不一致'):
                ArchiveVCS(*paths)

    def test_discard_keeps_names_and_all_visible_exports_identical(self):
        for suffix in ('.zip','.tar','.tar.gz','.tar.bz2'):
            paths=self.archives({'ignored/A.class':b'old'*1000,'keep.txt':b'old'},
                                {'ignored/A.class':b'new'*1000,'keep.txt':b'new'},suffix)
            full,a=self.result(paths,['*.class'])
            sparse,b=self.result(paths,['*.class'],True)
            self.assertEqual(self.identity(a),self.identity(b))
            self.assertEqual(b'',(Path(sparse._tmp_new)/'ignored/A.class').read_bytes())
            self.assertEqual(b'new'*1000,(Path(full._tmp_new)/'ignored/A.class').read_bytes())
            output=self.root/suffix.replace('.','_');output.mkdir()
            for label,vcs,result in [('full',full,a),('sparse',sparse,b)]:
                FileExporter(result,vcs).export(str(output/label/'old'),str(output/label/'new'))
            from archive_workflow_fixtures import tree_hashes
            self.assertEqual(tree_hashes(output/'full'),tree_hashes(output/'sparse'))

    def test_root_prefix_directory_replacement_and_one_sided_rename(self):
        cases=[({'classes/ignored.class':b'old'}, {'classes':b'file'},['classes/**'],False),
               ({'before.class':b'moved'}, {'after.txt':b'moved'},['*.class'],False),
               ({'release-old/classes/A.class':b'a','release-old/conf/a.txt':b'old'},
                {'release-new/classes/A.class':b'b','release-new/conf/a.txt':b'new'},['classes/**'],True),
               ({'Foo/A.class':b'a','foo/keep.txt':b'old'},
                {'Foo/A.class':b'b','foo/keep.txt':b'new'},['Foo/*.class'],False)]
        for old,new,patterns,root in cases:
            paths=self.archives(old,new)
            _,a=self.result(paths,patterns,False,root)
            _,b=self.result(paths,patterns,True,root)
            self.assertEqual(self.identity(a),self.identity(b))

    def test_excluded_permissions_do_not_reenter_and_invalid_root_is_not_filtered(self):
        a,b=self.root/'a.zip',self.root/'b.zip'
        a.write_bytes(zip_bytes({'hidden.class':b'old'},mode=0o644))
        b.write_bytes(zip_bytes({'hidden.class':b'new'},mode=0o755))
        _,r=self.result((str(a),str(b)),['*.class'],True)
        self.assertEqual([],r.files)
        paths=self.archives({'root/a.txt':b'a','top.class':b'bad'},
                            {'root/a.txt':b'b','top.class':b'bad'})
        with self.assertRaisesRegex(ValueError,'单一文件夹'):
            ArchiveVCS(*paths,ignore_single_root=True,extraction_excludes=['*.class'])

    def test_discarded_zip_crc_encryption_and_unsafe_names_still_fail(self):
        raw=bytearray(zip_bytes({'hidden.class':b'PAYLOAD'}))
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            info=z.infolist()[0]
            offset=info.header_offset+30+len(info.filename.encode())+len(info.extra)
        raw[offset]^=1
        old,new=self.root/'a.zip',self.root/'b.zip'
        old.write_bytes(zip_bytes({}));new.write_bytes(raw)
        with self.assertRaises((ValueError,zipfile.BadZipFile,RuntimeError)):
            ArchiveVCS(str(old),str(new),extraction_excludes=['*.class'])
        for name in ('../hidden.class','C:/hidden.class','hidden.class:stream'):
            new.write_bytes(zip_bytes({name:b'bad'}))
            with self.assertRaises(ValueError):
                ArchiveVCS(str(old),str(new),extraction_excludes=['*'])
        raw=bytearray(zip_bytes({'hidden.class':b'PAYLOAD'}))
        raw[6]|=1; central=raw.find(b'PK\x01\x02'); raw[central+8]|=1
        new.write_bytes(raw)
        with self.assertRaises((ValueError,RuntimeError)):
            ArchiveVCS(str(old),str(new),extraction_excludes=['*.class'])

    def test_discarded_bytes_count_once_in_shared_report_budget(self):
        paths=self.archives({'hidden.class':b'A'*200,'keep':b'B'*5},
                            {'hidden.class':b'C'*300,'keep':b'D'*7})
        budget=ArchiveReportBudget(max_bytes=600)
        vcs=_ReportArchiveVCS(*paths,budget,extraction_excludes=['*.class'])
        self.addCleanup(vcs.cleanup)
        self.assertEqual(512,budget.actual_bytes)
        self.assertEqual(512,budget.declared_bytes)
        self.assertEqual(4,budget.members)
        with self.assertRaises(ValueError):
            _ReportArchiveVCS(*paths,ArchiveReportBudget(max_bytes=400),
                              extraction_excludes=['*.class'])

    def test_immutable_internal_mode_and_public_dynamic_setter(self):
        paths=self.archives({'a.class':b'old'},{'a.class':b'new'})
        full,_=self.result(paths,['*.class'])
        full.set_exclude_patterns([])
        self.assertEqual(1,len(full.get_changed_files()))
        sparse,_=self.result(paths,['*.class'],True)
        with self.assertRaisesRegex(ValueError,'不可变'):sparse.set_exclude_patterns([])
        with self.assertRaisesRegex(ValueError,'占位'):sparse.get_file_content_bytes('new','a.class')
        sparse.exclude_patterns=[]
        with self.assertRaisesRegex(ValueError,'已被改变'):sparse.get_changed_files()

    def test_sparse_space_decision_does_not_require_full_body_storage(self):
        payload=b'x'*(2*1024*1024)
        paths=self.archives({'hidden.class':payload,'keep':b'A'},
                            {'hidden.class':payload,'keep':b'B'})
        import shutil
        original=shutil.disk_usage(self.root)
        limited=type(original)(original.total,original.total-2500000,2500000)
        with mock.patch('shutil.disk_usage',return_value=limited):
            with self.assertRaisesRegex((OSError,RuntimeError),'空间|space'):
                ArchiveVCS(*paths)
            vcs=ArchiveVCS(*paths,extraction_excludes=['*.class'])
            self.addCleanup(vcs.cleanup)
            self.assertEqual(b'B',vcs.get_file_content_bytes('new','keep'))

    def test_real_alias_collision_is_not_hidden_by_discard(self):
        if os.name!='nt':self.skipTest('Requires Windows 8.3 names')
        probe=self.root/'probe';probe.mkdir()
        (probe/'longfilename.txt').write_bytes(b'x')
        if not (probe/'longfi~1.txt').exists():self.skipTest('8.3 aliases disabled on test volume')
        paths=self.archives({}, {'longfilename.txt':b'discard','longfi~1.txt':b'kept'})
        for patterns in (None,['longfilename.txt']):
            with self.assertRaisesRegex(ValueError,'别名|碰撞|冲突'):
                ArchiveVCS(*paths,extraction_excludes=patterns)
        paths=self.archives({}, {'ABC~1.TXT':b'kept'})
        vcs=ArchiveVCS(*paths,extraction_excludes=['*.class']);self.addCleanup(vcs.cleanup)
        self.assertEqual(b'kept',vcs.get_file_content_bytes('new','ABC~1.TXT'))

    def test_legacy_gnu_sparse_keeps_expanded_bytes_in_compatibility_path(self):
        info=tarfile.TarInfo('sparse.dat');info.type=tarfile.GNUTYPE_SPARSE;info.size=4
        header=bytearray(info.tobuf(format=tarfile.GNU_FORMAT))
        header[386:398]=b'00000000000\0';header[398:410]=b'00000000004\0'
        header[482]=0;header[483:495]=b'00000020000\0'
        header[148:156]=b'        ';header[148:156]=('%06o\0 ' % sum(header)).encode('ascii')
        path=self.root/'sparse.tar';path.write_bytes(header+b'abcd'+b'\0'*508+b'\0'*1024)
        for patterns in (None,['*.class']):
            vcs=ArchiveVCS(str(path),str(path),extraction_excludes=patterns)
            try:self.assertEqual(b'abcd'+b'\0'*8188,vcs.get_file_content_bytes('old','sparse.dat'))
            finally:vcs.cleanup()
