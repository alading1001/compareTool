"""Compatibility and observable work counts for the first optimization batch."""
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock
from vcs.base import BaseVCS
from vcs.svn_vcs import SVNVCS
from diff_engine import DiffEngine
from test_complete_export_review_fixes import BytesVCS, TableRows
from archive_workflow_fixtures import WorkflowCase, Repository

class SimpleOptimizations(WorkflowCase):
    def test_glob_cache_does_not_depend_on_setter_or_path(self):
        vcs = SVNVCS.__new__(SVNVCS)
        BaseVCS._compile_glob.cache_clear()
        for pattern, expected in [('*.class',True), ('*.java',False), ('a/*',True), ('a/*/*',False)]:
            vcs.exclude_patterns=[pattern]
            for _ in range(20): self.assertEqual(expected, vcs._is_excluded('a/F.class'))
        self.assertEqual(4, BaseVCS._compile_glob.cache_info().misses)
        self.assertTrue(BaseVCS._match_glob_pattern('a/b/x', 'x', True))
        self.assertFalse(BaseVCS._match_glob_pattern('a/b/x', 'x', False))
        vcs.exclude_patterns=['foo/*','foo/*/*']
        self.assertFalse(vcs._is_excluded_tree('foo'))
        vcs.exclude_patterns=['foo/**']
        self.assertTrue(vcs._is_excluded_tree('foo'))

    def test_svn_known_no_eol_skips_scan_and_direct_binary_remains_compatible(self):
        vcs = SVNVCS.__new__(SVNVCS)
        target = self.root/'writer'
        vcs.export_raw_file_to_path = lambda version,path,out: Path(out).write_bytes(b'raw\n')
        vcs._property_cache={('2','a'): {}}
        with mock.patch.object(vcs,'_file_contains_null', side_effect=AssertionError('unnecessary NUL scan')):
            vcs.export_file_to_path('r2','a',str(target))
        self.assertEqual(b'raw\n',target.read_bytes())
        vcs._property_cache={}
        vcs.export_raw_file_to_path = lambda version,path,out: Path(out).write_bytes(b'\x00raw')
        with mock.patch.object(vcs,'_get_eol_style', side_effect=AssertionError('new attribute dependency')):
            vcs.export_file_to_path('2','a',str(target))
        self.assertEqual(b'\x00raw',target.read_bytes())

    def test_fixed_property_provider_rejects_identity_and_url_mismatch(self):
        vcs = SVNVCS.__new__(SVNVCS)
        vcs._source_identity_pinned=True
        vcs._pinned_repo_uuid='uuid'; vcs._pinned_repo_root_url='file:///repository'
        vcs._pinned_project_url='file:///repository/trunk'; vcs._pinned_peg_revision='10'
        vcs._file_url=lambda rev,path: 'file:///repository/trunk/'+path+'@'+rev
        identity=vcs._property_source_identity()
        for bad_identity, url in [(('other',)+identity[1:],vcs._file_url('2','a')), (identity,'file:///wrong/a@2')]:
            vcs.use_fixed_property_provider(lambda v,p: (bad_identity,url,{}))
            with self.assertRaisesRegex(RuntimeError,'不一致'): vcs._get_properties('r2','a')
        values={'svn:eol-style':'LF'}
        vcs.use_fixed_property_provider(lambda v,p: (identity,vcs._file_url(v,p),values))
        result=vcs._get_properties('r2','a'); result.clear(); values.clear()
        self.assertEqual({'svn:eol-style':'LF'},vcs._get_properties('2','a'))

    def test_svn_multi_shares_successful_file_properties(self):
        repo=Repository(self,'svn','svn')
        repo.commit({f'{i}.txt':b'old\n' for i in range(3)})
        new=repo.commit({f'{i}.txt':b'new\n' for i in range(3)})
        calls=[]; original=subprocess.run
        def observed(args,*a,**kw):
            if 'proplist' in args: calls.append(list(args))
            return original(args,*a,**kw)
        with mock.patch('subprocess.run',side_effect=observed):
            app,result,out=self.generate(repo.task(new,'',multi=True),False)
        self.assert_success(app)
        self.assertEqual(6,len(calls), calls)
        self.assertEqual(3,result.summary['total_files'])
        self.assertTrue(all((out/'newVersion/Demo'/f'{i}.txt').read_bytes()==b'new\n' for i in range(3)))

    def test_public_text_retention_and_explicit_internal_mode(self):
        values=[]
        for retain in (True,False):
            result=DiffEngine(BytesVCS(b'old\n',b'new\n'),retain_text_contents=retain).generate_diff('old','new')
            file=result.files[0]
            self.assertEqual('old\n' if retain else '',file.old_content)
            self.assertEqual('new\n' if retain else '',file.new_content)
            values.append((result.summary,TableRows(file.side_by_side_html).side(),TableRows(file.side_by_side_html).side(True)))
        self.assertEqual(values[0],values[1])
        self.assertEqual('old\n',DiffEngine(BytesVCS(b'old\n',b'new\n')).generate_diff('old','new').files[0].old_content)

if __name__ == '__main__': unittest.main()
