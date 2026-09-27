"""Fixed raw reuse: real repository request counts and cache failure boundaries."""
import hashlib
import os
from pathlib import Path
import subprocess
from unittest import mock
import unittest
from archive_workflow_fixtures import WorkflowCase, Repository
from vcs.base import BaseVCS
from vcs.svn_vcs import SVNVCS
from vcs.raw_cache import RawEndpointCache

class EndpointReuseTests(WorkflowCase):
    def test_svn_six_files_normal_and_multi_read_each_endpoint_once(self):
        repo=Repository(self,'svn','svn')
        old=repo.commit({f'{i}.txt':b'OLD\n' for i in range(6)})
        new=repo.commit({f'{i}.txt':b'NEW\n' for i in range(6)})
        for multi in (False,True):
            calls=[];original=subprocess.Popen
            def capture(args,*a,**kw):
                calls.append(list(args));return original(args,*a,**kw)
            task=repo.task(new if multi else old,new,multi=multi)
            with mock.patch('subprocess.Popen',side_effect=capture), mock.patch.object(BaseVCS,'_file_contains_null',side_effect=AssertionError('no-EOL NUL scan')):
                app,result,out=self.generate(task,False,self.root/str(multi))
            self.assert_success(app)
            self.assertEqual(12,sum('cat' in c for c in calls),calls)
            self.assertEqual(12,sum('proplist' in c for c in calls),calls)
            for i in range(6):
                self.assertEqual(b'OLD\n',(out/'oldVersion/Demo'/f'{i}.txt').read_bytes())
                self.assertEqual(b'NEW\n',(out/'newVersion/Demo'/f'{i}.txt').read_bytes())
            self.assertFalse(list((self.root/'runtime').glob('comparetool_svn_raw_*')))

    def test_cache_publication_partial_failure_and_repeat_consumption(self):
        cache=RawEndpointCache();self.addCleanup(cache.close)
        def broken(writer):writer.write(b'part');raise OSError('interrupted')
        with self.assertRaises(OSError):cache.acquire('key',broken)
        self.assertIsNone(cache.get('key'))
        entry=cache.acquire('key',lambda w:w.write(b'complete'))
        self.assertEqual(b'complete',cache.read_bytes(entry))
        self.assertEqual(b'complete',cache.read_bytes(entry))
        target=self.root/'copy';cache.copy_to(entry,str(target))
        self.assertEqual(b'complete',target.read_bytes())
        self.assertEqual((8,hashlib.sha256(b'complete').hexdigest()),cache.signature(entry))
        owned=Path(cache.root);cache.close();cache.close();self.assertFalse(owned.exists())

    def test_cache_same_size_time_tamper_replace_and_delete_are_not_hits(self):
        for attack in ('inplace','replace','delete'):
            cache=RawEndpointCache();self.addCleanup(cache.close)
            entry=cache.acquire('key',lambda w:w.write(b'AAAA'))
            original=os.stat(entry.path)
            if attack=='inplace':Path(entry.path).write_bytes(b'BBBB')
            elif attack=='replace':
                alternate=Path(cache.root)/'replacement';alternate.write_bytes(b'AAAA');os.replace(alternate,entry.path)
            else:Path(entry.path).unlink()
            if attack!='delete':os.utime(entry.path,ns=(original.st_atime_ns,original.st_mtime_ns))
            with self.assertRaises((OSError,RuntimeError)):cache.read_bytes(entry)

    def test_cache_checks_bytes_in_the_existing_consumption_stream(self):
        cache=RawEndpointCache();self.addCleanup(cache.close)
        entry=cache.acquire('key',lambda w:w.write(b'AAAA'))
        # Keep expected file identity deliberately equal: content digest must
        # still reject an in-place overwrite with restored timestamps.
        Path(entry.path).write_bytes(b'BBBB')
        from dataclasses import replace
        from path_safety import regular_file_path_identity
        changed=replace(entry,identity=regular_file_path_identity(entry.path))
        with self.assertRaisesRegex(RuntimeError,'内容已变化'):cache.read_bytes(changed)

    def test_svn_derive_eol_does_not_modify_raw(self):
        vcs=SVNVCS.__new__(SVNVCS)
        raw=self.root/'raw';target=self.root/'target'
        for style,expected in [('',b'a\r\nb\rc\n'),('lf',b'a\nb\nc\n'),('crlf',b'a\r\nb\r\nc\r\n'),('cr',b'a\rb\rc\r')]:
            data=b'a\r\nb\rc\n';raw.write_bytes(data)
            # Each matrix row represents a different fixed task policy.
            vcs._eol_cache={}
            vcs._property_cache={('2','a'):{'svn:eol-style':style}}
            vcs.derive_export_from_raw('2','a',str(raw),str(target))
            self.assertEqual(data,raw.read_bytes());self.assertEqual(expected,target.read_bytes())
        with self.assertRaisesRegex(RuntimeError,'同一文件'):
            vcs.derive_export_from_raw('2','a',str(raw),str(raw))

    def test_git_multi_raw_logical_requests_are_not_duplicated(self):
        from vcs.git_batch import GitBatchReader
        repo=Repository(self,'git','git')
        repo.commit({f'{i}.txt':b'old\n' for i in range(3)})
        new=repo.commit({f'{i}.txt':b'new\n' for i in range(3)})
        requests=[];original=GitBatchReader.copy_to
        def count(reader,expression,target):
            requests.append(expression);return original(reader,expression,target)
        with mock.patch.object(GitBatchReader,'copy_to',count):
            app,result,out=self.generate(repo.task(new,'',multi=True),False)
        self.assert_success(app)
        self.assertEqual(6,len(requests),requests)
        self.assertEqual(6,len(set(requests)))

if __name__=='__main__':unittest.main()
