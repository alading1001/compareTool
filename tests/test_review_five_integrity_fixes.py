"""真实工作线程验证 SVN 历史、损坏配置、压缩尾部和部分交付清单。"""
import bz2
import gzip
import io
import json
import tarfile
import unittest
from urllib.parse import quote

import main
from archive_workflow_fixtures import Repository, WorkflowCase
from diff_engine import DiffResult
from report_generator import ReportGenerator
import test_review_configuration_and_report_semantics as report_tests
from vcs.archive_vcs import ArchiveVCS


def delivered(output, side):
    root = output / side
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


def tar_bytes(payload):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        item = tarfile.TarInfo('a.txt')
        item.size = len(payload)
        archive.addfile(item, io.BytesIO(payload))
    return stream.getvalue()


class SVNHistoryIntegrityTests(WorkflowCase):
    def check_delivery(self, task, output, old, new, changes):
        app, result, out = self.generate(task, False, self.root / output)
        self.assert_success(app)
        self.assertFalse(result.delivery_incomplete, result.issues)
        self.assertEqual(old, delivered(out, 'oldVersion'))
        self.assertEqual(new, delivered(out, 'newVersion'))
        self.assertEqual(changes, {(f.report_type, f.file_path, f.old_path)
                                   for f in result.report_manifest_files})
        self.assertEqual(len(changes), app._last_task_record['counts']['total_files'])
        self.assert_no_stages(out)
        return result, out

    def test_replaced_root_and_ancestor_preserve_selected_file_endpoints(self):
        for ancestor in (False, True):
            with self.subTest(ancestor=ancestor):
                repo = Repository(self, 'svn', 'replace-' + str(ancestor))
                suffix = '/project' if ancestor else ''
                source, destination = 'source' + suffix, 'destination' + suffix
                repo.commit({source + '/a.txt': b'base\n', destination + '/other.txt': b'unrelated\n'})
                first = repo.commit({source + '/a.txt': b'first\n'})
                repo.cmd('delete', 'destination')
                repo.cmd('move', 'source', 'destination')
                moved = repo.commit()
                last = repo.commit({destination + '/a.txt': b'last\n'})
                # r3 is an R directory copy, not the A used for an absent destination.
                self.assertIn('action="R"', repo.cmd('log', '--xml', '-v', '-r', moved))
                for index, (selected, payload) in enumerate((
                        (first, b'first\n'), (first + ',' + last, b'last\n'),
                        (first + ',' + moved + ',' + last, b'last\n'))):
                    task = repo.task(selected, '', multi=True, scope=repo.path / destination)
                    self.check_delivery(task, f'root-{ancestor}-{index}',
                        {'Demo/a.txt': b'base\n'}, {'Demo/a.txt': payload}, {('M', 'a.txt', '')})

    def test_replaced_root_and_ancestor_keep_child_rename_in_same_revision(self):
        for ancestor in (False, True):
            with self.subTest(ancestor=ancestor):
                repo = Repository(self, 'svn', 'rename-' + str(ancestor))
                suffix = '/project' if ancestor else ''
                source_root, target_root = 'source', 'destination'
                source, target = source_root + suffix, target_root + suffix
                repo.commit({source + '/a.txt': b'base', target + '/other.txt': b'other'})
                first = repo.commit({source + '/a.txt': b'first'})
                repo.cmd('delete', target_root)
                repo.cmd('move', source_root, target_root)
                repo.cmd('move', target + '/a.txt', target + '/renamed.txt')
                moved = repo.commit()
                last = repo.commit({target + '/renamed.txt': b'last'})
                for index, selected in enumerate((first + ',' + last, first + ',' + moved + ',' + last)):
                    task = repo.task(selected, '', multi=True, scope=repo.path / target)
                    _, out = self.check_delivery(task, f'rename-{ancestor}-{index}',
                        {'Demo/a.txt': b'base'}, {'Demo/renamed.txt': b'last'},
                        {('R', 'renamed.txt', 'a.txt')})
                    self.assertIn('[重命名旧路径] Demo/a.txt',
                        next(out.glob('*操作说明.txt')).read_text(encoding='utf-8-sig'))

    def test_unicode_whitespace_filename_keeps_literal_modify_and_rename_identity(self):
        repo = Repository(self, 'svn', 'literal')
        name, renamed = 'a.txt\u00a0', 'renamed.txt\u3000'
        base = repo.commit({'a.txt': b'decoy', name: b'before'})
        first = repo.commit({name: b'after'})
        for multi in (False, True):
            self.check_delivery(repo.task(first if multi else base, first, multi=multi),
                'literal-' + str(multi), {'Demo/' + name: b'before'},
                {'Demo/' + name: b'after'}, {('M', name, '')})
        # The Windows SVN client argv uses the system code page; URL escapes
        # preserve NBSP even when that code page cannot represent it.
        repo.cmd('move', repo.url + '/' + quote(name), repo.url + '/' + quote(renamed),
                 '-m', 'rename literal path')
        repo.cmd('update')
        last = repo.commit({renamed: b'last'})
        self.check_delivery(repo.task(first + ',' + last, '', multi=True), 'literal-rename',
            {'Demo/' + name: b'before'}, {'Demo/' + renamed: b'last'}, {('R', renamed, name)})


class MultiTaskRuleRecoveryTests(WorkflowCase):
    def folder_task(self, name='Healthy', rules='**/logs/**\n**/.git/**'):
        old, new = self.root / 'before', self.root / 'after'
        for root in (old, new):
            for path in ('src/a.txt', 'logs/trace.txt', '.git/config'):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(root.name.encode())
        return dict(vcs_type='folder', project_name=name, project_path='',
                    old_version=str(old), new_version=str(new), exclude_rules=rules)

    def run_saved_tasks(self, tasks, name):
        config = dict(vcs_type='folder', multi_tasks=tasks, output_dir=str(self.root / name))
        with report_tests.ProjectExclusionRecoveryTests.configured_window(self, config) as (app, path, warning):
            normalized = list(app._multi_tasks)
            self.assertTrue(app._save_current_config())
            self.assertEqual(normalized, json.loads(path.read_text(encoding='utf-8'))['multi_tasks'])
            warnings = [str(call.args[0]) for call in warning.call_args_list]
        out = self.root / name
        out.mkdir()
        worker = self.app()
        worker._do_generate_multi(normalized, str(out / 'report.html'), str(out / 'oldVersion'),
                                  str(out / 'newVersion'), str(out))
        self.assert_success(worker)
        self.assert_no_stages(out)
        return normalized, warnings, out

    def test_invalid_task_rules_are_warned_and_skipped_before_save_and_export(self):
        good = self.folder_task()
        tasks = [dict(good, exclude_rules=value) for value in (None, [], {}, 42, True)] + [good]
        normalized, warnings, out = self.run_saved_tasks(tasks, 'invalid')
        self.assertEqual([good['project_name']], [t['project_name'] for t in normalized])
        self.assertEqual(5, sum('排除规则不是字符串' in line for line in warnings))
        self.assertEqual({'Healthy/src/a.txt': b'after'}, delivered(out, 'newVersion'))

    def test_legacy_missing_rules_use_defaults_but_explicit_empty_stays_empty(self):
        legacy = self.folder_task('Legacy')
        del legacy['exclude_rules']
        empty = self.folder_task('Empty', '')
        normalized, warnings, out = self.run_saved_tasks([legacy, empty], 'legacy')
        self.assertEqual(main.DEFAULT_EXCLUDE_RULES, normalized[0]['exclude_rules'])
        self.assertEqual('', normalized[1]['exclude_rules'])
        self.assertTrue(any('未保存排除规则' in line for line in warnings))
        self.assertEqual({'Legacy/src/a.txt': b'after', 'Empty/src/a.txt': b'after',
                         'Empty/logs/trace.txt': b'after', 'Empty/.git/config': b'after'},
                         delivered(out, 'newVersion'))


class CompressedTarIntegrityTests(WorkflowCase):
    def payloads(self):
        valid = gzip.compress(tar_bytes(b'after'))
        return {'valid': valid,
                'crc': valid[:-8] + bytes([valid[-8] ^ 1]) + valid[-7:],
                'size': valid[:-4] + bytes([valid[-4] ^ 1]) + valid[-3:],
                'truncated': valid[:-8]}

    def test_gzip_trailer_is_checked_on_both_sides_even_when_every_member_is_excluded(self):
        good = gzip.compress(tar_bytes(b'before'))
        for name, payload in self.payloads().items():
            if name == 'valid':
                continue
            with self.assertRaises((gzip.BadGzipFile, EOFError)):
                gzip.decompress(payload)
            for side in ('old', 'new'):
                for patterns in (None, ['**']):
                    with self.subTest(damage=name, side=side, patterns=patterns):
                        old, new = self.root / 'old.tgz', self.root / 'new.tgz'
                        old.write_bytes(payload if side == 'old' else good)
                        new.write_bytes(payload if side == 'new' else good)
                        with self.assertRaises((gzip.BadGzipFile, EOFError, ValueError)):
                            ArchiveVCS(str(old), str(new), extraction_excludes=patterns)
        self.assertFalse(list((self.root / 'runtime').glob('cmp_*')))

    def test_valid_compressed_streams_keep_padding_and_member_bytes(self):
        for index, (suffix, encode) in enumerate((('.tar', bytes), ('.tgz', gzip.compress),
                                                  ('.tbz2', bz2.compress))):
            old, new = self.root / ('old' + suffix), self.root / ('new' + suffix)
            # Extra TAR record padding is legal and must not become another member.
            old.write_bytes(encode(tar_bytes(b'before') + b'\0' * (2 * 1024**2)))
            new.write_bytes(encode(tar_bytes(b'after') + b'\0' * (2 * 1024**2)))
            task = dict(project_name='Demo', vcs_type='archive', project_path='',
                        old_version=str(old), new_version=str(new), exclude_rules='')
            app, result, out = self.generate(task, False, self.root / str(index))
            self.assert_success(app)
            self.assertFalse(result.delivery_incomplete, result.issues)
            self.assertEqual({'Demo/a.txt': b'before'}, delivered(out, 'oldVersion'))
            self.assertEqual({'Demo/a.txt': b'after'}, delivered(out, 'newVersion'))

    def test_corrupt_top_level_is_incomplete_and_nested_archive_is_preserved_with_notice(self):
        for name, payload in self.payloads().items():
            if name == 'valid':
                continue
            with self.subTest(damage=name):
                old, new = self.root / (name + '-old'), self.root / (name + '-new')
                old.mkdir(); new.mkdir()
                before = gzip.compress(tar_bytes(b'before'))
                (old / 'package.tar.gz').write_bytes(before)
                (new / 'package.tar.gz').write_bytes(payload)
                task = dict(project_name='Demo', vcs_type='archive', project_path='',
                    old_version=str(old / 'package.tar.gz'), new_version=str(new / 'package.tar.gz'),
                    exclude_rules='')
                app, result, out = self.generate(task, False, self.root / (name + '-direct'))
                self.assertEqual('partial', app._last_task_record['status'])
                self.assertTrue(result.delivery_incomplete)
                self.assertTrue(result.issues)
                self.assertFalse(result.files)
                self.assertFalse(delivered(out, 'newVersion'))
                task.update(vcs_type='folder', old_version=str(old), new_version=str(new))
                app, result, out = self.generate(task, True, self.root / (name + '-nested'))
                self.assert_success(app)
                self.assertFalse(result.delivery_incomplete, result.issues)
                self.assertEqual('error', result.files[0].archive_details['status'])
                self.assertIn('包内分析未完成', (out / 'report.html').read_text(encoding='utf-8'))
                self.assertEqual({'Demo/package.tar.gz': before}, delivered(out, 'oldVersion'))
                self.assertEqual({'Demo/package.tar.gz': payload}, delivered(out, 'newVersion'))
                self.assert_no_stages(out)


@unittest.skipUnless(report_tests.NODE, 'Node.js is required to execute generated summary scripts')
class PartialDeliverySummaryTests(WorkflowCase):
    def test_multi_summary_retains_failed_project_and_successful_manifest(self):
        old, new = self.root / 'old', self.root / 'new'
        old.mkdir(); new.mkdir()
        (old / 'good.txt').write_bytes(b'before'); (new / 'good.txt').write_bytes(b'after')
        tasks = [dict(project_name='Healthy', vcs_type='folder', project_path='',
                      old_version=str(old), new_version=str(new), exclude_rules=''),
                 dict(project_name='Missing', vcs_type='git', project_path=str(self.root / 'missing'),
                      old_version='HEAD~1', new_version='HEAD', exclude_rules='')]
        out = self.root / 'multi'; out.mkdir()
        app = self.app()
        app._do_generate_multi(tasks, str(out / 'report.html'), str(out / 'oldVersion'),
                                str(out / 'newVersion'), str(out))
        self.assertEqual('partial', app._last_task_record['status'])
        summary = report_tests.summary_text((out / 'report.html').read_text(encoding='utf-8'))
        for text in ('交付未完成', '共 1 项未完成', 'Missing/<项目>', '项目读取', 'Healthy/good.txt'):
            self.assertIn(text, summary)
        self.assertEqual({'Healthy/good.txt': b'after'}, delivered(out, 'newVersion'))

    def test_single_summary_retains_rejected_file_without_adding_it_to_manifest(self):
        repo = Repository(self, 'git', 'single')
        first = repo.commit({'safe.txt': b'before'})
        repo.write({'safe.txt': b'after'})
        repo.cmd('add', 'safe.txt')
        blob = repo.cmd('hash-object', '-w', '--stdin')
        repo.cmd('update-index', '--add', '--cacheinfo', '120000,' + blob + ',link')
        repo.cmd('commit', '-m', 'change with unsupported endpoint')
        last = repo.cmd('rev-parse', 'HEAD')
        app, result, out = self.generate(repo.task(first, last), False)
        self.assertEqual('partial', app._last_task_record['status'])
        summary = report_tests.summary_text((out / 'report.html').read_text(encoding='utf-8'))
        for text in ('交付未完成', '共 1 项未完成', 'Demo/link', '文件类型', 'Demo/safe.txt'):
            self.assertIn(text, summary)
        self.assertEqual(['safe.txt'], [f.file_path for f in result.report_manifest_files])
        self.assertEqual({'Demo/safe.txt': b'after'}, delivered(out, 'newVersion'))

    def test_summary_escapes_issue_text_and_does_not_warn_for_informational_issues(self):
        message = 'reason </script><script>throw new Error("injection")</script>\n下一行'
        result = DiffResult('test', 'Demo', 'folder', 'old', 'new')
        result.add_issue('说明', 'note', message)
        for multi in (False, True):
            output = self.root / (str(multi) + '.html')
            def render():
                if multi:
                    ReportGenerator().generate_multi([dict(project_name='Demo', show_project_root=False,
                        diff_result=result, vcs_type='folder')], str(output))
                else:
                    ReportGenerator().generate(result, str(output), show_project_root=False)
                return report_tests.summary_text(output.read_text(encoding='utf-8'))
            self.assertNotIn('交付未完成', render())
            result.add_issue('读取', 'new.txt', message, delivery=True, old_path='old.txt')
            summary = render()
            for text in (message, 'Demo/new.txt', 'old.txt', '交付未完成'):
                self.assertIn(text, summary)
            result.issues.pop()
