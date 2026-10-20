"""用真实进程验证期限、取消和残留子进程清理，不执行机器人控制。"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from primebuddy.skills import processes


@unittest.skipUnless(os.name == 'posix', 'POSIX 进程组')
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_command(self, code, **options):
        values = dict(cwd=self.root, env=dict(os.environ), runtime=self.root,
                      cancelled=threading.Event(), timeout=3, stop_grace=(.12, .12, .5))
        values.update(options)
        return processes.run_process([sys.executable, '-c', code], **values)

    def test_success_does_not_require_json_or_fixed_functions(self):
        for code, output in (("print('完成')", '完成\n'),
                             ("print('{\"hello\":true}')", '{"hello":true}\n')):
            with self.subTest(output=output):
                result = self.run_command(code, task_id='task-1', owner_id='turn-1')
                self.assertEqual((result['status'], result['returncode']), ('exited', 0))
                self.assertTrue(result['cleanup_confirmed'])
                self.assertEqual(result['stdout'], output)
                path = Path(result['record_path'])
                record = json.loads(path.read_text())
                self.assertEqual(record['owner_id'], 'turn-1')
                self.assertEqual(record['task_id'], 'task-1')
                self.assertFalse(path.stat().st_mode & 0o077)
                self.assertFalse(path.parent.stat().st_mode & 0o077)
                self.assertNotIn('command', record)
                self.assertNotIn('env', record)
                self.assertNotIn(code, path.read_text())

    def test_hard_timeout_stops_hung_command(self):
        start = time.monotonic()
        result = self.run_command('import time; time.sleep(60)', timeout=.15)
        self.assertEqual(result['status'], 'timed_out')
        self.assertTrue(result['cleanup_confirmed'])
        self.assertLess(time.monotonic() - start, 2)

    def test_escalates_when_command_ignores_interrupt_and_term(self):
        code = ('import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); '
                'signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)')
        result = self.run_command(code, timeout=.3)
        self.assertEqual(result['status'], 'timed_out')
        self.assertEqual(result['returncode'], -signal.SIGKILL)
        self.assertTrue(result['cleanup_confirmed'])

    def test_root_exit_still_cleans_live_child_in_group(self):
        code = ('import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",'
                '"import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); '
                'signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"]); '
                'print(p.pid,flush=True)')
        result = self.run_command(code)
        self.assertEqual(result['status'], 'exited')
        self.assertTrue(result['cleanup_confirmed'])
        child = int(result['stdout'].strip())
        group = json.loads(Path(result['record_path']).read_text())['pgid']
        self.assertNotIn(child, processes._group_members(group))

    def test_observed_child_in_new_session_is_cleaned_after_root_exits(self):
        child_code = '''import os
from pathlib import Path
import signal
import time
def detach(*_):
    os.setsid()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    Path('detached').touch()
signal.signal(signal.SIGINT, detach)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('ready').touch()
time.sleep(60)
'''
        code = ('import subprocess,sys,time; from pathlib import Path; '
                f'p=subprocess.Popen([sys.executable,"-c",{child_code!r}]); '
                'print(p.pid,flush=True); deadline=time.monotonic()+5\n'
                'while not Path("ready").exists() and time.monotonic()<deadline: time.sleep(.01)\n'
                'assert Path("ready").exists()')
        # 子进程在收到监督器 SIGINT 前留在原组，确保已被观察后才另建 session。
        result = self.run_command(code, timeout=10, stop_grace=(1, .25, 1))
        self.assertEqual((result['status'], result['returncode']), ('exited', 0))
        self.assertTrue(result['cleanup_confirmed'])
        child = int(result['stdout'].strip())
        self.assertTrue((self.root / 'detached').exists())
        self.assertNotIn(child, processes._process_table())
        record = json.loads(Path(result['record_path']).read_text())
        self.assertIn(child, record['observed_pids'])

    def test_second_snapshot_tracks_same_group_child_only_before_root_is_reaped(self):
        root, child = 2147483646, 2147483647
        for seen_before_reap in (True, False):
            with self.subTest(seen_before_reap=seen_before_reap):
                pipes = []
                for _ in range(2):
                    read_fd, write_fd = os.pipe()
                    os.close(write_fd)
                    pipe = os.fdopen(read_fd, 'rb')
                    self.addCleanup(pipe.close)
                    pipes.append(pipe)
                process = Mock(pid=root, returncode=None, stdout=pipes[0], stderr=pipes[1])
                alive, scans = True, 0

                def group_members(group):
                    nonlocal scans
                    self.assertEqual(group, root)
                    scans += 1
                    if scans <= 2:
                        return {root: 'root-start'}
                    return {child: 'child-start'} if alive else {}

                def process_table():
                    # 根进程在两次采样之间退出；子进程已由 PID 1 接管，但仍同组。
                    if alive and (seen_before_reap or process.returncode is not None):
                        return {child: (1, root, 'child-start')}
                    return {}

                def poll():
                    process.returncode = 0
                    return 0

                def kill_group(group, sig):
                    nonlocal alive
                    self.assertEqual((group, sig), (root, signal.SIGINT))
                    alive = False

                process.poll.side_effect = poll
                with patch.object(processes.subprocess, 'Popen', return_value=process), \
                        patch.object(processes, '_group_members', side_effect=group_members), \
                        patch.object(processes, '_process_table', side_effect=process_table), \
                        patch.object(processes.os, 'killpg', side_effect=kill_group) as killpg, \
                        patch.object(processes.os, 'kill') as kill:
                    result = self.run_command('unused')
                expected = 'exited' if seen_before_reap else 'cleanup_failed'
                self.assertEqual((result['status'], result['returncode']), (expected, 0))
                self.assertEqual(result['cleanup_confirmed'], seen_before_reap)
                record = json.loads(Path(result['record_path']).read_text())
                self.assertEqual(child in record['observed_pids'], seen_before_reap)
                self.assertEqual(killpg.call_count, int(seen_before_reap))
                kill.assert_not_called()

    def test_cancel_one_of_two_tasks_does_not_stop_the_other(self):
        cancelled = threading.Event()
        code = 'import time; print("ready",flush=True); time.sleep(.65); print("done",flush=True)'
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.run_command, code, cancelled=cancelled, task_id='first')
            second = executor.submit(self.run_command, code, task_id='second')
            time.sleep(.15)
            cancelled.set()
            first, second = first.result(timeout=3), second.result(timeout=3)
        self.assertEqual(first['status'], 'cancelled')
        self.assertEqual((second['status'], second['returncode']), ('exited', 0))
        self.assertIn('done', second['stdout'])
        self.assertNotEqual(first['execution_id'], second['execution_id'])

    def test_output_files_stay_within_total_limit(self):
        code = 'import os; [os.write(1,b"x"*65536) for _ in range(10000)]'
        result = self.run_command(code, max_output_bytes=2048)
        self.assertEqual(result['status'], 'output_limit')
        self.assertTrue(result['cleanup_confirmed'])
        stem = Path(result['record_path']).with_suffix('')
        size = sum(Path(str(stem) + '.' + name).stat().st_size for name in ('stdout', 'stderr'))
        self.assertEqual(size, 2048)

    def test_cancel_before_launch_has_no_process(self):
        cancelled = threading.Event()
        cancelled.set()
        result = self.run_command('raise AssertionError("must not run")', cancelled=cancelled)
        self.assertEqual((result['status'], result['returncode']), ('cancelled', None))
        self.assertTrue(result['cleanup_confirmed'])

    def test_unconfirmed_group_cleanup_cannot_report_success(self):
        # 模拟有一个始终存在的组成员。真实命令已退出，发送信号得到 ESRCH；
        # 再探测仍有成员时，必须保留 cleanup_failed 而非臆测完成。
        with patch.object(processes, '_group_members', return_value={2147483647: 'known'}):
            result = self.run_command("print('done')")
        self.assertEqual(result['status'], 'cleanup_failed')
        self.assertFalse(result['cleanup_confirmed'])
        self.assertEqual(result['returncode'], 0)
        record = json.loads(Path(result['record_path']).read_text())
        self.assertEqual(record['state'], 'cleanup_failed')
        self.assertEqual(record['termination_reason'], 'exited')

    def test_unbounded_deadline_is_rejected(self):
        for timeout in (float('inf'), float('nan'), 0, -1):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.run_command("print('must not run')", timeout=timeout)

    def test_probe_error_carries_confirmed_cleanup_when_retry_succeeds(self):
        original = processes._group_members
        first = True
        def probe(group):
            nonlocal first
            if first:
                first = False
                raise OSError('injected probe failure')
            return original(group)
        with patch.object(processes, '_group_members', side_effect=probe), self.assertRaises(OSError) as caught:
            self.run_command('import time; time.sleep(60)')
        self.assertIs(caught.exception.cleanup_confirmed, True)
        records = list((self.root / 'processes').glob('*.json'))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertIs(record['cleanup_confirmed'], True)
        self.assertEqual(record['state'], 'failed')

    def test_probe_error_carries_unconfirmed_cleanup_when_probe_stays_broken(self):
        with patch.object(processes, '_group_members', side_effect=OSError('injected probe failure')):
            with self.assertRaises(OSError) as caught:
                self.run_command('import time; time.sleep(60)')
        self.assertIs(caught.exception.cleanup_confirmed, False)
        records = list((self.root / 'processes').glob('*.json'))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertIs(record['cleanup_confirmed'], False)
        self.assertEqual(record['state'], 'cleanup_failed')


if __name__ == '__main__':
    unittest.main()
