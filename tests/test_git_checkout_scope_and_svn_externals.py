import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from diff_engine import DiffEngine
from file_exporter import FileExporter
from vcs.git_checkout import GitCheckoutSnapshot
from vcs.git_vcs import GitVCS
from vcs.multi_version_vcs import GitMultiVersionVCS, SVNMultiVersionVCS
from vcs.svn_vcs import SVNVCS


class CheckoutScopeTests(unittest.TestCase):
    def setUp(self):
        Path('.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        patch = mock.patch.dict(os.environ, {
            'COMPARETOOL_TEMP_DIR': str(self.root / 'runtime'),
            'COMPARETOOL_TRANSACTION_KEY_FILE': str(self.root / 'key'),
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git_exe = GitVCS._find_git()
        self.git('init', '-q')
        self.git('config', 'user.name', 'CompareTool Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'core.autocrlf', 'false')
        self.blobs = {}

    def run_command(self, args, cwd=None, data=None):
        result = subprocess.run(args, cwd=cwd or self.repo, input=data, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    def git(self, *args, data=None):
        return self.run_command([self.git_exe, *args], data=data)

    def commit(self, files, parent=None):
        self.git('read-tree', '--empty')
        records = []
        for name, data in files.items():
            if data not in self.blobs:
                self.blobs[data] = self.git('hash-object', '-w', '--stdin', data=data).strip()
            records.append(b'100644 ' + self.blobs[data] + b'\t' + name.encode() + b'\0')
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.git('update-index', '-z', '--index-info', data=b''.join(records))
        tree = self.git('write-tree').decode().strip()
        args = ['commit-tree', tree, '-m', 'fixture']
        if parent:
            args.extend(['-p', parent])
        oid = self.git(*args).decode().strip()
        self.git('update-ref', 'HEAD', oid)
        return oid

    def normal(self, path=None):
        vcs = GitVCS(str(path or self.repo))
        self.addCleanup(vcs.cleanup)
        return vcs

    def multi(self, versions, path=None):
        vcs = GitMultiVersionVCS(str(path or self.repo), versions)
        self.addCleanup(vcs.cleanup)
        return vcs

    def exported(self, vcs, old, new, name):
        diff = DiffEngine(vcs).generate_diff(old, new)
        output = self.root / name
        FileExporter(diff, vcs).export(str(output / 'old'), str(output / 'new'))
        return diff, {side: {p.relative_to(output / side).as_posix(): p.read_bytes()
                            for p in (output / side).rglob('*') if p.is_file()}
                      for side in ('old', 'new')}

    def test_native_eol_matrix_matches_bytes_and_streaming_exports(self):
        samples = {
            'lf': b'alpha\nbeta\n', 'mixed': b'alpha\r\nbeta\n',
            'cr': b'alpha\rbeta\n', 'binary': bytes(range(1, 9)) + b'\n',
            'nul': b'alpha\x00beta\n', 'eof': b'alpha\n\x1a',
            'dense': b'a' * 128 + b'\x01\n', 'sparse': b'a' * 127 + b'\x01\n',
        }
        attrs = ['', 'text=auto', 'text', '-text', 'eol=crlf', 'text=auto eol=crlf',
                 'text eol=lf', 'text=set', 'text=unset', 'text=Auto']
        rules = '\n'.join(f'{i}-*.dat {attr}' for i, attr in enumerate(attrs)).encode() + b'\n'
        new_files = {f'{i}-{label}.dat': data for i in range(len(attrs)) for label, data in samples.items()}
        old_files = {name: b'old\n' for name in new_files}
        old_files['.gitattributes'] = new_files['.gitattributes'] = rules
        old = self.commit(old_files)
        new = self.commit(new_files, old)
        self.git('config', 'core.autocrlf', 'true')
        normal = self.normal()
        diff, actual = self.exported(normal, old, new, 'matrix')
        self.assertEqual(80, len(diff.files))
        for name in new_files:
            if name == '.gitattributes':
                continue
            with self.subTest(path=name):
                native = self.git('cat-file', '--filters', f'{new}:{name}')
                self.assertEqual(native, actual['new'][name])
                self.assertEqual(native, normal.get_file_content_bytes(new, name))

    def test_native_config_defaults_and_boolean_forms(self):
        old = self.commit({'.gitattributes': b'*.txt text\n', 'a.txt': b'old\n'})
        new = self.commit({'.gitattributes': b'*.txt text\n', 'a.txt': b'new\n'}, old)
        for autocrlf, eol in [('false', 'native'), ('input', 'crlf'), ('yes', 'lf'), ('false', 'lf')]:
            with self.subTest(autocrlf=autocrlf, eol=eol):
                self.git('config', 'core.autocrlf', autocrlf)
                self.git('config', 'core.eol', eol)
                vcs = self.normal()
                vcs.get_changed_files(old, new)
                self.assertEqual(self.git('cat-file', '--filters', f'{new}:a.txt'),
                                 vcs.get_file_content_bytes(new, 'a.txt'))

    def test_streaming_auto_text_uses_entire_content_and_chunk_boundaries(self):
        first = b'x' * (1024 * 1024 - 1)
        for payload in (first + b'\r\nlast\n', first + b'\x00last\n',
                        first + b'\x01last\n', first + b'\n\x1a'):
            with self.subTest(tail=payload[-10:]):
                old = self.commit({'large.dat': b'old\n'})
                new = self.commit({'large.dat': payload}, old)
                self.git('config', 'core.autocrlf', 'true')
                vcs = self.normal()
                vcs.get_changed_files(old, new)
                target = self.root / 'large.dat'
                with mock.patch.object(vcs, '_apply_crlf', side_effect=AssertionError('must stream')):
                    vcs.export_file_to_path(new, 'large.dat', str(target))
                self.assertEqual(self.git('cat-file', '--filters', f'{new}:large.dat'), target.read_bytes())

    def test_literal_filter_names_fail_but_actual_states_still_export(self):
        rules = b'\n'.join([b'literal-' + d + b'.txt filter=' + d for d in (b'unset', b'unspecified', b'set')])
        rules += b'\ndisabled.txt -filter\nunspecified.txt !filter\nboolean.txt filter\n'
        files = {name: b'old\n' for name in ('literal-unset.txt', 'literal-unspecified.txt', 'literal-set.txt',
                                            'disabled.txt', 'unspecified.txt', 'boolean.txt', 'absent.txt')}
        files['.gitattributes'] = rules
        old = self.commit(files)
        files = {name: b'new\n' if name.endswith('.txt') else data for name, data in files.items()}
        new = self.commit(files, old)
        marker = self.root / 'filter-executed'
        for driver in ('unset', 'unspecified', 'set'):
            self.git('config', f'filter.{driver}.smudge', f'echo executed > "{marker.as_posix()}"; cat')
        vcs = self.normal()
        vcs.get_changed_files(old, new)
        for name in files:
            if not name.endswith('.txt'):
                continue
            with self.subTest(name=name):
                if name.startswith('literal-'):
                    with self.assertRaisesRegex(RuntimeError, 'filter'):
                        vcs.get_file_content_bytes(new, name)
                    target = self.root / 'blocked.txt'
                    with self.assertRaisesRegex(RuntimeError, 'filter'):
                        vcs.export_file_to_path(new, name, str(target))
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(b'new\n', vcs.get_file_content_bytes(new, name))
        self.assertFalse(marker.exists())
        with self.assertRaisesRegex(RuntimeError, 'filter'):
            self.multi([new])
        self.assertFalse(marker.exists())

    def test_subproject_scope_renames_and_attributes_normal_and_multi(self):
        attrs = b'*.txt -text\napp/*.txt text eol=crlf\n'
        old_files = {'.gitattributes': attrs, 'app/exit.txt': b'exit\n',
                     'incoming.txt': b'enter\n', 'app/before.txt': b'rename\n',
                     'other/out.txt': b'other-old\n', 'app/value.txt': b'value-old\n'}
        old = self.commit(old_files)
        new = self.commit({'.gitattributes': attrs, 'outside.txt': b'exit\n',
                           'app/entered.txt': b'enter\n', 'app/after.txt': b'rename\n',
                           'other/out.txt': b'other-new\n', 'app/value.txt': b'value-new\n'}, old)
        for mode in ('normal', 'multi'):
            vcs = self.normal(self.repo / 'app') if mode == 'normal' else self.multi([new], self.repo / 'app')
            ov, nv = (old, new) if mode == 'normal' else (vcs.old_version_label, vcs.new_version_label)
            diff, actual = self.exported(vcs, ov, nv, mode)
            self.assertEqual({'exit.txt': b'exit\r\n', 'before.txt': b'rename\r\n', 'value.txt': b'value-old\r\n'}, actual['old'])
            self.assertEqual({'entered.txt': b'enter\r\n', 'after.txt': b'rename\r\n', 'value.txt': b'value-new\r\n'}, actual['new'])
            self.assertEqual([('before.txt', 'after.txt')],
                             [(f.old_path, f.file_path) for f in diff.files if f.change_type.value == 'R'])

    def test_subproject_excludes_and_unselected_moves_apply_before_identity(self):
        old = self.commit({'app/a.txt': b'a\n', 'app/skip/old.txt': b'skip\n', 'other/out.txt': b'other\n'})
        first = self.commit({'app/a.txt': b'selected first\n', 'app/skip/old.txt': b'skip\n', 'other/out.txt': b'other\n'}, old)
        middle = self.commit({'app/renamed.txt': b'selected first\n', 'app/skip/new.txt': b'skip\n', 'other/out.txt': b'other\n'}, first)
        last = self.commit({'app/renamed.txt': b'selected last\n', 'app/skip/new.txt': b'skip-new\n', 'other/out.txt': b'other-new\n'}, middle)
        vcs = GitMultiVersionVCS(str(self.repo / 'app'), [first, last], exclude_patterns=['skip/**'])
        self.addCleanup(vcs.cleanup)
        diff, actual = self.exported(vcs, vcs.old_version_label, vcs.new_version_label, 'selected')
        self.assertEqual({'a.txt': b'a\n'}, actual['old'])
        self.assertEqual({'renamed.txt': b'selected last\n'}, actual['new'])
        self.assertEqual('R', diff.files[0].change_type.value)

    def test_frozen_external_attributes_and_config_are_reused(self):
        old = self.commit({'app/a.txt': b'old\n'})
        new = self.commit({'app/a.txt': b'new\n'}, old)
        info = self.repo / '.git' / 'info' / 'attributes'
        info.write_bytes(b'app/*.txt text eol=crlf\n')
        vcs = self.normal(self.repo / 'app')
        vcs.get_changed_files(old, new)
        info.write_bytes(b'app/*.txt -text\n')
        self.git('config', 'core.autocrlf', 'input')
        self.assertEqual(b'new\r\n', vcs.get_file_content_bytes(new, 'a.txt'))
        target = self.root / 'frozen.txt'
        vcs.export_file_to_path(new, 'a.txt', str(target))
        self.assertEqual(b'new\r\n', target.read_bytes())

    def test_bare_repository_still_exports(self):
        old = self.commit({'a.txt': b'old\n'})
        new = self.commit({'a.txt': b'new\n'}, old)
        bare = self.root / 'bare.git'
        self.git('clone', '--bare', str(self.repo), str(bare))
        for mode in ('normal', 'multi'):
            vcs = self.normal(bare) if mode == 'normal' else self.multi([new], bare)
            ov, nv = (old, new) if mode == 'normal' else (vcs.old_version_label, vcs.new_version_label)
            _, actual = self.exported(vcs, ov, nv, 'bare-' + mode)
            native = self.run_command([self.git_exe, 'cat-file', '--filters', f'{new}:a.txt'], cwd=bare)
            self.assertEqual({'a.txt': native}, actual['new'])

    def test_sha256_repository_uses_matching_probe_object_format(self):
        self.repo = self.root / 'sha256'
        self.repo.mkdir()
        self.git('init', '-q', '--object-format=sha256')
        self.git('config', 'user.name', 'CompareTool Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'core.autocrlf', 'true')
        old = self.commit({'a.txt': b'old\n'})
        new = self.commit({'a.txt': b'new\n'}, old)
        self.assertEqual(64, len(new))
        vcs = self.normal()
        _, actual = self.exported(vcs, old, new, 'sha-output')
        self.assertEqual({'a.txt': b'new\r\n'}, actual['new'])

    def test_unicode_subproject_and_literal_pathspec_modes(self):
        project = '组件[一]'
        old = self.commit({f'{project}/[file].txt': b'old\n', 'other/file.txt': b'outside\n'})
        new = self.commit({f'{project}/[file].txt': b'new\n', 'other/file.txt': b'outside-new\n'}, old)
        for mode in ('normal', 'multi'):
            vcs = self.normal(self.repo / project) if mode == 'normal' else self.multi([new], self.repo / project)
            ov, nv = (old, new) if mode == 'normal' else (vcs.old_version_label, vcs.new_version_label)
            _, actual = self.exported(vcs, ov, nv, 'unicode-' + mode)
            self.assertEqual({'[file].txt': b'new\n'}, actual['new'])

    def test_global_attribute_macro_bom_and_info_precedence(self):
        old = self.commit({'.gitattributes': b'*.txt delivery\n', 'a.txt': b'old\n'})
        new = self.commit({'.gitattributes': b'*.txt delivery\n', 'a.txt': b'new\n'}, old)
        global_attrs = self.root / 'global.attrs'
        global_attrs.write_bytes(b'\xef\xbb\xbf[attr]delivery text eol=crlf -filter\n')
        self.git('config', 'core.attributesfile', str(global_attrs))
        self.git('config', 'filter.unset.smudge', 'must-not-execute')
        vcs = self.normal()
        vcs.get_changed_files(old, new)
        self.assertEqual(b'new\r\n', vcs.get_file_content_bytes(new, 'a.txt'))
        (self.repo / '.git' / 'info' / 'attributes').write_bytes(b'*.txt eol=lf\n')
        other = self.normal()
        other.get_changed_files(old, new)
        self.assertEqual(b'new\n', other.get_file_content_bytes(new, 'a.txt'))

    def test_attribute_copy_is_bound_to_snapshot_even_if_source_is_restored(self):
        old = self.commit({'a.txt': b'old\n'})
        new = self.commit({'a.txt': b'new\n'}, old)
        info = self.repo / '.git' / 'info' / 'attributes'
        original = b'*.txt text eol=crlf\n'
        info.write_bytes(original)
        copy = GitCheckoutSnapshot._copy_attribute_file

        def racing_copy(path, target):
            if path and Path(path) == info:
                info.write_bytes(b'*.txt -text\n')
                try:
                    return copy(path, target)
                finally:
                    info.write_bytes(original)
            return copy(path, target)

        vcs = self.normal()
        with mock.patch.object(GitCheckoutSnapshot, '_copy_attribute_file', side_effect=racing_copy):
            with self.assertRaisesRegex(RuntimeError, '属性文件复制期间发生变化'):
                vcs.get_changed_files(old, new)
        self.assertEqual(original, info.read_bytes())
        self.assertEqual([], list((self.root / 'runtime').glob('comparetool_git_checkout_*')))

    @unittest.skipUnless(shutil.which('svn') and shutil.which('svnadmin'), '需要 SVN')
    def test_svn_empty_and_comment_externals_export_but_real_definitions_fail(self):
        repo, wc = self.root / 'svn', self.root / 'wc'
        self.run_command(['svnadmin', 'create', str(repo)])
        self.run_command(['svn', 'checkout', repo.as_uri(), str(wc)])
        (wc / 'a.txt').write_bytes(b'base\n')
        self.run_command(['svn', 'add', 'a.txt'], cwd=wc)
        self.run_command(['svn', 'commit', '-m', 'base'], cwd=wc)
        for revision, value in enumerate(('', ' \t\n', '# disabled\n  # another\n', '^/external ext'), start=2):
            self.run_command(['svn', 'update'], cwd=wc)
            self.run_command(['svn', 'propset', 'svn:externals', value, '.'], cwd=wc)
            payload = f'revision {revision}\n'.encode()
            (wc / 'a.txt').write_bytes(payload)
            self.run_command(['svn', 'commit', '-m', 'properties'], cwd=wc)
            if revision == 5:
                with self.assertRaisesRegex(RuntimeError, 'svn:externals'):
                    SVNVCS(str(wc)).get_changed_files('4', '5')
                with self.assertRaisesRegex(RuntimeError, 'svn:externals'):
                    SVNMultiVersionVCS(str(wc), ['5'])
                continue
            normal = SVNVCS(str(wc))
            _, actual = self.exported(normal, str(revision - 1), str(revision), f'svn-normal-{revision}')
            self.assertEqual(payload, actual['new']['a.txt'])
            multi = SVNMultiVersionVCS(str(wc), [str(revision)])
            self.addCleanup(multi.cleanup)
            _, actual = self.exported(multi, multi.old_version_label, multi.new_version_label, f'svn-multi-{revision}')
            self.assertEqual(payload, actual['new']['a.txt'])


if __name__ == '__main__':
    unittest.main()
