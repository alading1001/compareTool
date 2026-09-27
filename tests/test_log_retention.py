import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import logger
from task_progress import TaskObserver


class LogRetentionTests(unittest.TestCase):
    def setUp(self):
        Path('.tmp').mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir='.tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def record(self, index, completed=False, pid=None, corrupt=False):
        base = self.root / f'task_20200101_000000_{index:012x}'
        payload = dict(schema='comparetool.task.v1', event='task_start')
        if pid is not None:
            payload['pid'] = pid
        base.with_suffix('.jsonl').write_text('{"unfinished":' if corrupt else json.dumps(payload) + '\n', encoding='utf-8')
        if completed:
            base.with_suffix('.json').write_text(json.dumps(dict(schema='comparetool.task.v1')), encoding='utf-8')
        return base

    def test_finished_interrupted_and_partial_logs_share_history_limit(self):
        bases = [self.record(i, completed=i % 2 == 0, corrupt=i % 3 == 0) for i in range(9)]
        with mock.patch.object(TaskObserver, 'MAX_HISTORY', 3):
            TaskObserver._prune(self.root)
        self.assertEqual([False] * 6 + [True] * 3,
                         [b.with_suffix('.jsonl').exists() for b in bases])
        self.assertFalse(bases[0].with_suffix('.json').exists())

    def test_live_process_and_current_observer_are_not_pruned(self):
        running = self.record(0, pid=123456)
        abandoned = self.record(1, pid=123457)
        newest = self.record(2, completed=True)
        observer = TaskObserver('folder', log_dir=self.root)
        self.addCleanup(lambda: observer.stream and observer.stream.close())
        self.addCleanup(TaskObserver._active_logs.discard, os.path.abspath(observer.log_path))
        with mock.patch('task_progress.pid_is_alive', side_effect=lambda p: p == 123456), \
                mock.patch.object(TaskObserver, 'MAX_HISTORY', 1):
            TaskObserver._prune(self.root)
        self.assertTrue(running.with_suffix('.jsonl').exists())
        self.assertFalse(abandoned.with_suffix('.jsonl').exists())
        self.assertTrue(newest.with_suffix('.jsonl').exists())
        self.assertTrue(Path(observer.log_path).exists())

    def test_starting_task_prunes_old_aborted_logs_without_waiting_for_success(self):
        bases = [self.record(i) for i in range(6)]
        with mock.patch.object(TaskObserver, 'MAX_HISTORY', 2):
            observer = TaskObserver('folder', log_dir=self.root)
            self.assertEqual(2, sum(b.with_suffix('.jsonl').exists() for b in bases))
            observer.failure = 'RuntimeError'
            observer.completion = '_show_error', ('failed',)
            result = observer.finish()
        self.assertFalse(result['success'])
        self.assertEqual(2, len(list(self.root.glob('*.jsonl'))))

    def test_user_files_and_links_are_preserved(self):
        personal = self.root / 'notes.txt'
        personal.write_bytes(b'personal')
        external = self.root / 'task_20200101_000000_000000000000.json'
        external.write_text('{"schema":"another-tool"}', encoding='utf-8')
        self.record(1)
        with mock.patch.object(TaskObserver, 'MAX_HISTORY', 1):
            TaskObserver._prune(self.root)
        self.assertEqual(b'personal', personal.read_bytes())
        self.assertEqual('{"schema":"another-tool"}', external.read_text())

    def test_event_and_summary_sizes_are_bounded_even_for_oversized_details(self):
        observer = TaskObserver('folder', log_dir=self.root)
        for _ in range(30):
            observer.record('detail', text='中' * 30000)
        observer.phases['oversized'] = 'x' * (TaskObserver.MAX_SUMMARY_BYTES * 2)
        observer.completion = '_on_complete', ('report', {})
        result = observer.finish()
        event = Path(result['log_path'])
        summary = event.with_suffix('.json')
        self.assertLessEqual(event.stat().st_size, TaskObserver.MAX_EVENT_BYTES + TaskObserver.MAX_SUMMARY_BYTES)
        self.assertLessEqual(summary.stat().st_size, TaskObserver.MAX_SUMMARY_BYTES)
        self.assertTrue(json.loads(summary.read_text(encoding='utf-8'))['details_truncated'])
        self.assertIn('task_end', event.read_text(encoding='utf-8').splitlines()[-1])

    def test_regular_log_rotates_before_writing_and_caps_huge_messages(self):
        path = self.root / 'compareTool.log'
        with mock.patch.object(logger, 'LOG_FILE', str(path)):
            for _ in range(20):
                logger.error('超长错误' * 100000)
        files = [path, Path(str(path) + '.bak')]
        self.assertTrue(all(p.is_file() for p in files))
        self.assertTrue(all(p.stat().st_size <= logger._MAX_SIZE for p in files))
        self.assertTrue(all('日志过长，已截断' in p.read_text(encoding='utf-8') for p in files))
        self.assertEqual(2, len(list(self.root.iterdir())))

    def test_legacy_oversized_log_is_bounded_when_rotated(self):
        path = self.root / 'compareTool.log'
        path.write_bytes('旧记录'.encode('utf-8') * logger._MAX_SIZE)
        with mock.patch.object(logger, 'LOG_FILE', str(path)):
            logger.warn('current')
        self.assertLessEqual(Path(str(path) + '.bak').stat().st_size, logger._MAX_SIZE)
        Path(str(path) + '.bak').read_text(encoding='utf-8')
        self.assertIn('current', path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
