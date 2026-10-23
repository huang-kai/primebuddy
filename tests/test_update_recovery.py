"""真实本地进程验证升级互斥、入口保留及缺失源码时的离线恢复。"""
import contextlib
import fcntl
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from primebuddy.skills import manager
from primebuddy.tools import update
from primebuddy.tools.install import stage_launcher


PAUSED_RUNNER = '''import importlib.util
from pathlib import Path
import sys
import time
home = Path(sys.argv[1])
pause_target = sys.argv[2]
spec = importlib.util.spec_from_file_location('transaction', home / '.update/runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
original = runner.move
def pause(source, target):
    original(source, target)
    if str(target.relative_to(home)) == pause_target:
        (home / 'paused').touch()
        deadline = time.monotonic() + 10
        while not (home / 'resume').exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('test handshake timed out')
            time.sleep(.01)
runner.move = pause
sys.argv = [str(home / '.update/runner.py'), str(home)]
raise SystemExit(runner.main())
'''


class UpdateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.stage = self.home / 'stage'
        for root, value in ((self.home, 'old'), (self.stage, 'new')):
            for name in ('src/code.py', 'skills/demo/main.py'):
                path = root / name
                path.parent.mkdir(parents=True)
                path.write_text(value)
        self.paths = ['src', 'skills/demo']

    def stop_process(self, process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def paused_runner(self, target, **kwargs):
        process = subprocess.Popen([sys.executable, '-c', PAUSED_RUNNER, str(self.home), target],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
        self.addCleanup(self.stop_process, process)
        deadline = time.monotonic() + 5
        while not (self.home / 'paused').exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((self.home / 'paused').exists(), '升级执行器未到达中断点')
        return process

    def recovery_command(self):
        return [sys.executable, str(self.home / '.update/runner.py'), '--recover', str(self.home)]

    def assert_original_code(self):
        self.assertEqual((self.home / 'src/code.py').read_text(), 'old')
        self.assertEqual((self.home / 'skills/demo/main.py').read_text(), 'old')
        self.assertFalse((self.home / '.update/transaction.json').exists())

    def test_runner_keeps_parent_locks_and_rejects_concurrent_recovery(self):
        locks = [(self.home / name).open('a') for name in ('install.lock', 'audio.lock')]
        for lock in locks:
            self.addCleanup(lock.close)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def run_with_parent_closed(command, **kwargs):
            process = self.paused_runner('.update/old/src', **kwargs)
            # 父进程退出会关闭这些描述符；独立执行器必须继续保有两把锁。
            for lock in locks:
                lock.close()
            for name in ('install.lock', 'audio.lock'):
                with (self.home / name).open('a') as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            journal = (self.home / '.update/transaction.json').read_bytes()
            with subprocess.Popen(self.recovery_command(), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as recovery:
                recovery.communicate(timeout=5)
                self.assertNotEqual(recovery.returncode, 0)
            self.assertEqual((self.home / '.update/transaction.json').read_bytes(), journal)
            (self.home / 'resume').touch()
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            return subprocess.CompletedProcess(command, process.returncode)

        with patch.object(update.subprocess, 'run', side_effect=run_with_parent_closed), \
                contextlib.redirect_stdout(io.StringIO()):
            update.commit_update(self.home, self.stage, self.paths,
                                 lock_fds=tuple(lock.fileno() for lock in locks))
        self.assertEqual((self.home / 'src/code.py').read_text(), 'new')
        self.assertEqual((self.home / 'skills/demo/main.py').read_text(), 'new')
        self.assertFalse((self.home / '.update/transaction.json').exists())

    def test_recovery_is_exclusive_even_without_parent_install_lock(self):
        update.prepare_transaction(self.home, self.stage, self.paths)
        process = self.paused_runner('.update/old/src')
        journal = (self.home / '.update/transaction.json').read_bytes()
        result = subprocess.run(self.recovery_command(), capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.home / '.update/transaction.json').read_bytes(), journal)
        process.kill()
        process.communicate(timeout=5)
        result = subprocess.run(self.recovery_command(), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_original_code()

    def test_runner_keeps_installation_lock_after_parent_context_exits(self):
        update.prepare_transaction(self.home, self.stage, self.paths)
        with patch.object(manager, 'get_data_directory', return_value=self.home):
            with self.assertRaisesRegex(RuntimeError, 'parent interrupted'):
                with manager.installation_lock() as lock:
                    process = self.paused_runner('.update/old/src', pass_fds=(lock.fileno(),))
                    raise RuntimeError('parent interrupted')
            with (self.home / 'install.lock').open('a') as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            process.kill()
            process.communicate(timeout=5)
            # 最后一个继承描述符关闭后，正常安装仍能获取并释放锁。
            with manager.installation_lock():
                pass
            with manager.installation_lock():
                pass

    def test_launcher_stays_present_after_backup_and_before_replacement(self):
        launcher = self.home / 'bin/primebuddy'
        launcher.parent.mkdir()
        previous = b'#!/bin/bash\nexit 79\n'
        launcher.write_bytes(previous)
        launcher.chmod(0o755)
        names = self.paths + stage_launcher(self.stage, self.home)
        update.prepare_transaction(self.home, self.stage, names)
        process = self.paused_runner('.update/old/bin/primebuddy')
        self.assertEqual(launcher.read_bytes(), previous)
        process.kill()
        process.communicate(timeout=5)
        result = subprocess.run(self.recovery_command(), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(launcher.read_bytes(), previous)
        self.assert_original_code()

    def test_bootstrap_launcher_recovers_after_source_disappears(self):
        launcher = self.home / 'bin/primebuddy'
        launcher.parent.mkdir()
        previous = b'#!/bin/bash\nexit 79\n'
        launcher.write_bytes(previous)
        launcher.chmod(0o755)
        names = self.paths + stage_launcher(self.stage, self.home)
        update.prepare_transaction(self.home, self.stage, names)
        process = self.paused_runner('.update/old/src')
        self.assertFalse((self.home / 'src').exists())
        self.assertNotEqual(launcher.read_bytes(), previous)
        process.kill()
        process.communicate(timeout=5)
        # home 中没有 main.py 或 scripts/robot_env.sh；恢复不能依赖应用或 ROS。
        result = subprocess.run([str(launcher), 'update', '--recover'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(launcher.read_bytes(), previous)
        self.assert_original_code()

    def test_interrupted_rollback_keeps_launcher_for_another_recovery(self):
        from primebuddy.tools import update_transaction as runner
        launcher = self.home / 'bin/primebuddy'
        launcher.parent.mkdir()
        previous = b'#!/bin/bash\nexit 79\n'
        launcher.write_bytes(previous)
        launcher.chmod(0o755)
        names = self.paths + stage_launcher(self.stage, self.home)
        update.prepare_transaction(self.home, self.stage, names)
        journal = self.home / '.update/transaction.json'
        data = json.loads(journal.read_text())
        self.assertEqual(data['entries'][0]['path'], 'bin/primebuddy')
        backup = self.home / '.update/old/bin/primebuddy'
        runner.backup_file(launcher, backup)
        runner.move(self.home / '.update/new/bin/primebuddy', launcher)
        data['entries'][0]['state'] = 'installed'
        runner.save(journal, data)
        original = runner.move

        def interrupted(source, target):
            if source == backup:
                self.assertTrue(launcher.is_file(), '恢复入口不能在回退期间消失')
                raise OSError('injected interruption before launcher restore')
            original(source, target)

        with patch.object(runner, 'move', side_effect=interrupted), self.assertRaises(OSError):
            runner.recover(self.home)
        result = subprocess.run([str(launcher), 'update', '--recover'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(launcher.read_bytes(), previous)
        self.assert_original_code()


if __name__ == '__main__':
    unittest.main()
