# Tiny native filter channel and grouped mode queries retain export semantics.
import io
import subprocess
import tempfile
from unittest import mock
from archive_workflow_fixtures import WorkflowCase, Repository
from vcs.git_probe_batch import GitProbeBatch


class GitProbeBatchTests(WorkflowCase):
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
