"""Buddy 与独立应用共享本机前端锁；不加载 ROS 或访问机器人。"""
import fcntl
import os
from pathlib import Path
import signal
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from primebuddy.integrations.primebot.robot_audio import AudioError, audio_session, frontend_lock


class FrontendLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lock = self.root / 'frontend.lock'
        lock_path = patch('primebuddy.integrations.primebot.robot_audio.FRONTEND_LOCK', self.lock)
        lock_path.start()
        self.addCleanup(lock_path.stop)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()

    def test_shared_lock_blocks_another_application_before_audio_initialization(self):
        descriptor = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            factory = Mock()
            with self.assertRaisesRegex(AudioError, '另一个语音应用'):
                with audio_session(self.runtime, factory):
                    self.fail('另一个应用持锁时不能进入会话')
            factory.assert_not_called()
        finally:
            os.close(descriptor)

    def test_lock_remains_held_during_cleanup_and_releases_even_if_cleanup_fails(self):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        def close():
            with self.assertRaises(AudioError):
                with frontend_lock():
                    self.fail('音频清理期间不能交还锁')
            raise RuntimeError('fixture cleanup failure')
        audio = Mock(close=Mock(side_effect=close))
        with self.assertRaisesRegex(RuntimeError, 'fixture cleanup failure'):
            with audio_session(self.runtime, lambda _: audio):
                self.assertEqual(stat.S_IMODE(self.lock.stat().st_mode), 0o600)
        self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)
        with frontend_lock():
            pass
        self.assertTrue(self.lock.is_file())

    def test_legacy_runtime_lock_still_blocks_session(self):
        with (self.runtime / 'audio.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            factory = Mock()
            with self.assertRaises(AudioError):
                with audio_session(self.runtime, factory):
                    self.fail('私有运行目录锁不能失效')
            factory.assert_not_called()
        with frontend_lock():
            pass

    def test_symlink_and_unsafe_existing_permissions_are_rejected_without_changes(self):
        target = self.root / 'target'
        target.write_text('keep')
        self.lock.symlink_to(target)
        with self.assertRaises(AudioError):
            with frontend_lock():
                self.fail('不能跟随符号链接')
        self.assertTrue(self.lock.is_symlink())
        self.assertEqual(target.read_text(), 'keep')
        self.lock.unlink()
        self.lock.write_text('keep')
        self.lock.chmod(0o644)
        with self.assertRaises(AudioError):
            with frontend_lock():
                self.fail('不能接受其他用户可读写的共享锁')
        self.assertEqual(self.lock.read_text(), 'keep')
        self.assertEqual(stat.S_IMODE(self.lock.stat().st_mode), 0o644)


if __name__ == '__main__':
    unittest.main()
