# Tiny native filter channel and grouped mode queries retain export semantics.
import io
import subprocess
import tempfile
from unittest import mock
from archive_workflow_fixtures import WorkflowCase, Repository
from vcs.git_probe_batch import GitProbeBatch
from vcs.multi_version_vcs import GitMultiVersionVCS


class GitProbeBatchTests(WorkflowCase):
    def test_probe_failure_after_eviction_closes_all_task_processes(self):
        repo = Repository(self, 'git', 'failed-probe')
        repo.commit({f'f{i}.txt': f'old-{i}\n'.encode() for i in range(6)})
        refs = [repo.commit({f'f{i}.txt': f'new-{i}\n'.encode()}) for i in range(6)]
        processes = []
        original = GitProbeBatch.read
        def fail_after_third_endpoint(channel, oid, path):
            result = original(channel, oid, path)
            if channel.process not in processes:
                processes.append(channel.process)
            if len(processes) == 3:
                raise RuntimeError('injected probe failure after eviction')
            return result
        with mock.patch.object(GitProbeBatch, 'read', fail_after_third_endpoint):
            with self.assertRaisesRegex(RuntimeError, 'injected probe failure'):
                GitMultiVersionVCS(str(repo.path), refs)
        self.assertEqual(3, len(processes))
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertFalse(list((self.root/'runtime').glob('comparetool_git_probe_*')))

    def test_many_endpoints_recycle_processes_and_keep_native_conversion(self):
        for count in (24, 48):
            repo = Repository(self, 'git', 'many-' + str(count))
            repo.commit({**{f'f{i:02d}.txt': f'original-{i}\n'.encode() for i in range(count)},
                         '.gitattributes': b'*.txt text eol=lf\n'})
            refs = []
            for i in range(count):
                refs.append(repo.commit({f'f{i:02d}.txt': f'updated-{i}\n'.encode(),
                    '.gitattributes': b'*.txt text eol=' + (b'crlf\n' if i % 2 == 0 else b'lf\n')}))
            processes = []; peak = 0; requests = 0
            original = GitProbeBatch.read
            def observe(channel, oid, path):
                nonlocal peak, requests
                value = original(channel, oid, path)
                requests += 1
                if channel.process not in processes:
                    processes.append(channel.process)
                peak = max(peak, sum(p.poll() is None for p in processes))
                return value
            vcs = None
            try:
                with mock.patch.object(GitProbeBatch, 'read', observe):
                    vcs = GitMultiVersionVCS(str(repo.path), refs, exclude_patterns=['.gitattributes'])
                self.assertEqual(count, len(vcs.get_changed_files()))
                for i in range(count):
                    path = f'f{i:02d}.txt'
                    before, after = f'original-{i}\n'.encode(), f'updated-{i}\n'.encode()
                    self.assertEqual(before, vcs.get_file_content_raw_bytes('old', path))
                    self.assertEqual(after, vcs.get_file_content_raw_bytes('new', path))
                    self.assertEqual(before.replace(b'\n', b'\r\n') if i % 2 else before,
                                     vcs.get_file_content_bytes('old', path))
                    self.assertEqual(after.replace(b'\n', b'\r\n') if i % 2 == 0 else after,
                                     vcs.get_file_content_bytes('new', path))
                self.assertEqual(count * 3, requests)
                self.assertLessEqual(len(processes), count * 2)
                self.assertLessEqual(peak, 2)
                self.assertLessEqual(sum(p.poll() is None for p in processes), 2)
            finally:
                if vcs is not None:
                    vcs.cleanup()
            self.assertTrue(all(p.poll() is not None for p in processes))
            self.assertFalse(list((self.root/'runtime').glob('comparetool_git_probe_*')))

    def test_real_twelve_file_probes_reuse_two_fixed_endpoint_processes(self):
        for multi in (False,True):
            repo=Repository(self,'git','repo'+str(multi))
            old=repo.commit({f'{i}.txt':b'OLD\n' for i in range(12)})
            new=repo.commit({f'{i}.txt':b'NEW\n' for i in range(12)})
            calls=[];start=subprocess.Popen
            def record(args,*a,**kw):calls.append(list(args));return start(args,*a,**kw)
            with mock.patch('subprocess.Popen',record):
                app,result,out=self.generate(repo.task(new if multi else old,new,multi=multi),False,self.root/('out'+str(multi)))
            self.assert_success(app)
            filters=[c for c in calls if '--filters' in c]
            self.assertEqual(2,len(filters),filters)
            self.assertTrue(all('--batch' in c for c in filters))
            modes=[c for c in calls if 'ls-tree' in c and '--literal-pathspecs' in c]
            if multi:self.assertEqual(2,len(modes),modes)
            for i in range(12):
                self.assertEqual(b'OLD\n',(out/'oldVersion/Demo'/f'{i}.txt').read_bytes())
                self.assertEqual(b'NEW\n',(out/'newVersion/Demo'/f'{i}.txt').read_bytes())
            self.assertFalse(list((self.root/'runtime').glob('comparetool_git_probe_*')))

    def test_protocol_rejects_wrong_objects_types_sizes_frames_and_stderr(self):
        oid='a'*40;body=b'Probe\n'
        frames=[b'b'*40+b' blob 6\n'+body+b'\n',
                oid.encode()+b' tree 6\n'+body+b'\n',
                oid.encode()+b' blob -1\n',oid.encode()+b' blob 9999\n',
                oid.encode()+b' blob 6\nshort',oid.encode()+b' blob 6\n'+body+b'X',
                b'X'*257,oid.encode()+b' missing\n']
        for frame in frames:
            with self.subTest(frame=frame[:65]):
                channel=GitProbeBatch([],{}, {oid:body})
                p=mock.Mock();p.poll.return_value=None
                p.stdin=io.BytesIO();p.stdout=io.BytesIO(frame)
                channel.process=p;channel.stderr=tempfile.TemporaryFile()
                with self.assertRaises(RuntimeError):channel.read(oid,'a.txt')
                self.assertTrue(channel.failed);self.assertIsNone(channel.process)
                with self.assertRaises(RuntimeError):channel.read(oid,'a.txt')
                channel.close()
        channel=GitProbeBatch([],{}, {oid:body})
        p=mock.Mock();p.poll.return_value=None
        p.stdin=io.BytesIO();p.stdout=io.BytesIO(oid.encode()+b' blob 6\n'+body+b'\n')
        channel.process=p;channel.stderr=tempfile.TemporaryFile()
        channel.stderr.write(b'native warning');channel.stderr.flush()
        with self.assertRaisesRegex(RuntimeError,'native warning'):channel.read(oid,'a.txt')

    def test_no_large_user_content_or_ambiguous_path_enters_probe_protocol(self):
        channel=GitProbeBatch([],{}, {'a'*40:b'Probe\n'})
        for path in (' leading.txt','\tleading.txt','a\nb.txt','a\rb.txt','a\0b.txt'):
            self.assertFalse(channel.supports(path))
            with self.assertRaises(ValueError):channel.read('a'*40,path)
        with self.assertRaises(ValueError):channel.read('b'*40,'a.txt')
        self.assertTrue(channel.supports('directory/space name.txt'))
        self.assertIsNone(channel.process)
