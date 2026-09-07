import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from diff_engine import DiffEngine
from file_exporter import FileExporter
from report_generator import ReportGenerator
from vcs.base import ChangedFile, ChangeType
from vcs.git_vcs import GitVCS
from vcs.multi_version_vcs import GitMultiVersionVCS, SVNMultiVersionVCS
from vcs.svn_vcs import SVNVCS


class PassthroughExportTests(unittest.TestCase):
    def setUp(self):
        os.makedirs('.tmp', exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        env = mock.patch.dict(os.environ, {
            'COMPARETOOL_TEMP_DIR': str(self.root / 'runtime'),
            'COMPARETOOL_TRANSACTION_KEY_FILE': str(self.root / 'signing.key'),
        })
        env.start()
        self.addCleanup(env.stop)

    def run_command(self, args, cwd):
        result = subprocess.run(args, cwd=cwd, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr.decode('utf-8', 'replace'))
        return result.stdout

    def git_repo(self):
        git = GitVCS._find_git()
        repo = self.root / 'git'
        repo.mkdir()
        self.git = lambda *args: self.run_command([git, *args], repo)
        self.git('init', '-q')
        self.git('config', 'user.name', 'CompareTool Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'core.autocrlf', 'false')
        (repo / '.gitattributes').write_text('*.txt filter=Passthrough\n', encoding='utf-8')
        (repo / 'plain.txt').write_bytes(b'old\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        old = self.git('rev-parse', 'HEAD').decode().strip()
        (repo / 'plain.txt').write_bytes(b'new\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'new')
        new = self.git('rev-parse', 'HEAD').decode().strip()
        return repo, old, new

    def assert_export(self, vcs, old, new, output, old_bytes, new_bytes):
        result = DiffEngine(vcs).generate_diff(old, new)
        self.assertEqual(['plain.txt'], [f.file_path for f in result.files])
        self.assertEqual(old_bytes, vcs.get_file_content_bytes(old, 'plain.txt'))
        self.assertEqual(new_bytes, vcs.get_file_content_bytes(new, 'plain.txt'))
        FileExporter(result, vcs).export(str(output / 'old'), str(output / 'new'))
        self.assertEqual(old_bytes, (output / 'old' / 'plain.txt').read_bytes())
        self.assertEqual(new_bytes, (output / 'new' / 'plain.txt').read_bytes())

    def test_missing_and_clean_only_git_filters_export_normal_and_multi(self):
        repo, old, new = self.git_repo()
        # 另一个大小写不同的驱动不应影响 Passthrough。
        self.git('config', 'filter.passthrough.smudge', 'must-not-run')
        for clean_only in (False, True):
            with self.subTest(clean_only=clean_only):
                if clean_only:
                    # 配置发生在提交之后，测试只读检出，不执行 clean 命令。
                    self.git('config', 'filter.Passthrough.clean', 'must-not-run')
                    self.git('config', 'filter.Passthrough.required', 'false')
                self.assertEqual(b'new\n', self.git('cat-file', '--filters', f'{new}:plain.txt'))
                normal = GitVCS(str(repo))
                self.assert_export(normal, old, new, self.root / 'normal', b'old\n', b'new\n')
                self.assertEqual('Passthrough', normal._get_checkout_attributes(new, 'plain.txt')['filter'])
                multi = GitMultiVersionVCS(str(repo), [new])
                try:
                    self.assert_export(
                        multi, multi.old_version_label, multi.new_version_label,
                        self.root / 'multi', b'old\n', b'new\n',
                    )
                finally:
                    multi.cleanup()

    def test_git_checkout_programs_and_required_filters_still_fail(self):
        repo, old, new = self.git_repo()
        for setting, value in (('smudge', 'cat'), ('process', 'must-not-run'), ('required', 'true')):
            with self.subTest(setting=setting):
                self.git('config', f'filter.Passthrough.{setting}', value)
                normal = GitVCS(str(repo))
                normal.get_changed_files(old, new)
                with self.assertRaisesRegex(RuntimeError, 'filter=Passthrough'):
                    normal.get_file_content_bytes(new, 'plain.txt')
                with self.assertRaisesRegex(RuntimeError, 'filter=Passthrough'):
                    normal.export_file_to_path(new, 'plain.txt', str(self.root / 'rejected.txt'))
                self.assertFalse((self.root / 'rejected.txt').exists())
                with self.assertRaisesRegex(RuntimeError, 'filter=Passthrough'):
                    GitMultiVersionVCS(str(repo), [new])
                self.git('config', '--unset', f'filter.Passthrough.{setting}')

    def test_git_filter_snapshot_rejects_configuration_change(self):
        repo, old, new = self.git_repo()
        vcs = GitVCS(str(repo))
        with mock.patch.object(vcs, '_read_filter_checkout_config', side_effect=[
            {'smudge': b'', 'process': b'', 'required': False},
            {'smudge': b'cat', 'process': b'', 'required': False},
        ]):
            with self.assertRaisesRegex(RuntimeError, '快照期间发生变化'):
                vcs.get_changed_files(old, new)

    def test_git_invalid_required_config_is_not_treated_as_disabled(self):
        repo, old, new = self.git_repo()
        self.git('config', 'filter.Passthrough.required', 'not-a-boolean')
        with self.assertRaisesRegex(RuntimeError, 'filter 配置失败'):
            GitVCS(str(repo)).get_changed_files(old, new)

    @unittest.skipUnless(shutil.which('svn') and shutil.which('svnadmin'), '需要 svn 和 svnadmin')
    def test_empty_svn_keywords_export_normal_and_multi(self):
        svn = shutil.which('svn')
        for index, keywords in enumerate(('', ' \t\n')):
            with self.subTest(keywords=repr(keywords)):
                repo = self.root / f'svn-{index}'
                wc = self.root / f'wc-{index}'
                self.run_command([shutil.which('svnadmin'), 'create', str(repo)], self.root)
                self.run_command([svn, 'checkout', repo.as_uri(), str(wc)], self.root)
                path = wc / 'plain.txt'
                path.write_bytes(b'old $Id$\n')
                self.run_command([svn, 'add', str(path)], wc)
                self.run_command([svn, 'propset', 'svn:keywords', keywords, str(path)], wc)
                self.run_command([svn, 'commit', '-m', 'base'], wc)
                path.write_bytes(b'new $Id$\n')
                self.run_command([svn, 'commit', '-m', 'new'], wc)
                self.assert_export(
                    SVNVCS(str(wc)), '1', '2', self.root / 'svn-normal',
                    b'old $Id$\n', b'new $Id$\n',
                )
                multi = SVNMultiVersionVCS(str(wc), ['2'])
                try:
                    self.assert_export(
                        multi, multi.old_version_label, multi.new_version_label,
                        self.root / 'svn-multi', b'old $Id$\n', b'new $Id$\n',
                    )
                finally:
                    multi.cleanup()
                self.run_command([svn, 'propset', 'svn:keywords', 'Id', str(path)], wc)
                self.run_command([svn, 'commit', '-m', 'enable keyword'], wc)
                with self.assertRaisesRegex(RuntimeError, 'svn:keywords'):
                    SVNVCS(str(wc)).get_changed_files('2', '3')
                with self.assertRaisesRegex(RuntimeError, 'svn:keywords'):
                    SVNMultiVersionVCS(str(wc), ['3'])


class SpecialSeparatorTests(unittest.TestCase):
    @staticmethod
    def diff(old, new, change_type=ChangeType.MODIFIED, show_full=True):
        old_path = 'previous.txt' if change_type == ChangeType.RENAMED else ''
        getter = lambda version, path: old if version == 'old' else new
        vcs = SimpleNamespace(
            project_path='demo', merge_exact_renames=False,
            get_changed_files=lambda *_: [ChangedFile('plain.txt', change_type, old_path)],
            get_file_content_raw_bytes=getter, get_file_content_bytes=getter,
        )
        return DiffEngine(vcs, show_full_context=show_full).generate_diff('old', 'new'), vcs

    def test_separators_are_visible_and_counted_in_all_change_types(self):
        for separator in ('\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
            content = f'alpha{separator}beta\n'.encode('utf-8')
            for change_type, old, new, counts in (
                (ChangeType.MODIFIED, content, b'alpha\nbeta\n', (2, 1)),
                (ChangeType.RENAMED, content, b'alpha\nbeta\n', (2, 1)),
                (ChangeType.ADDED, b'', content, (1, 0)),
                (ChangeType.DELETED, content, b'', (0, 1)),
            ):
                for show_full in (False, True):
                    with self.subTest(separator=repr(separator), kind=change_type, full=show_full):
                        result, _ = self.diff(old, new, change_type, show_full)
                        f = result.files[0]
                        self.assertFalse(f.format_only)
                        self.assertEqual(counts, (f.added_lines, f.deleted_lines))
                        self.assertNotIn('No Differences Found', f.side_by_side_html)
                        self.assertIn(f'⟦U+{ord(separator):04X}⟧', f.side_by_side_html)
                        self.assertTrue(any(f'class="{css}"' in f.side_by_side_html for css in ('diff_add', 'diff_sub', 'diff_chg')))

    def test_literal_marker_does_not_hide_a_real_separator_change(self):
        result, _ = self.diff('alpha\u2028beta'.encode(), 'alpha⟦U+2028⟧beta'.encode(), show_full=False)
        f = result.files[0]
        self.assertEqual((1, 1), (f.added_lines, f.deleted_lines))
        self.assertNotIn('No Differences Found', f.side_by_side_html)
        self.assertIn('class="diff_', f.side_by_side_html)

    def test_report_marks_separators_without_changing_export_bytes(self):
        old = 'alpha\u2028beta\n'.encode()
        new = b'alpha\nbeta\n'
        result, vcs = self.diff(old, new, show_full=False)
        os.makedirs('.tmp', exist_ok=True)
        with tempfile.TemporaryDirectory(dir='.tmp') as directory:
            root = Path(directory).resolve()
            with mock.patch.dict(os.environ, {'COMPARETOOL_TRANSACTION_KEY_FILE': str(root / 'key')}):
                ReportGenerator().generate(result, str(root / 'report.html'))
                FileExporter(result, vcs).export(str(root / 'old'), str(root / 'new'))
            self.assertIn('⟦U+2028⟧', (root / 'report.html').read_text(encoding='utf-8'))
            self.assertEqual(old, (root / 'old' / 'plain.txt').read_bytes())
            self.assertEqual(new, (root / 'new' / 'plain.txt').read_bytes())

    def test_cr_lf_and_trailing_empty_lines_keep_normal_line_counts(self):
        result, _ = self.diff(b'', b'a\rb\r\nc\n\n', ChangeType.ADDED)
        self.assertEqual(4, result.files[0].added_lines)


class FilterNameParsingTests(unittest.TestCase):
    def test_undecodable_filter_name_cannot_query_a_different_driver(self):
        with self.assertRaisesRegex(RuntimeError, '准确解析 Git filter 名称'):
            GitVCS._parse_check_attr_records(b'plain.txt\0filter\0driver-\xff\0')


if __name__ == '__main__':
    unittest.main()
