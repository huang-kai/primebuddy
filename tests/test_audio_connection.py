"""跨板连接残留的回归测试；用真实 Unix socket，不连接机器人。"""
import contextlib
import io
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from primebuddy.voice_support.robot_audio import AudioError, connect_soc0


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pb-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.path = self.runtime / 'soc0.sock'
        self.jetson = patch('primebuddy.voice_support.robot_audio.require_jetson')
        self.jetson.start()
        self.addCleanup(self.jetson.stop)

    def connect(self, codes, start=None):
        def run(args, **kwargs):
            if '-Nf' in args and start:
                start()
            return subprocess.CompletedProcess(args, next(values))
        values = iter(codes)
        with patch('primebuddy.voice_support.robot_audio.subprocess.run', side_effect=run) as call:
            connect_soc0(self.runtime)
        return call

    def test_healthy_master_reused(self):
        self.assertEqual(self.connect([0]).call_count, 1)

    def test_stale_socket_removed_before_reconnect(self):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(self.path))
        def start():
            self.assertFalse(self.path.exists(), '旧 socket 会导致 SSH 禁用复用')
        self.assertEqual(self.connect([255, 0, 0], start).call_count, 3)

    def test_successful_ssh_exit_does_not_prove_master_ready(self):
        with self.assertRaisesRegex(AudioError, '复用连接'):
            self.connect([255, 0, 255])

    def test_live_socket_is_not_deleted_when_master_check_fails(self):
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(self.path))
            sock.listen(1)
            with self.assertRaises(AudioError):
                self.connect([255])
            self.assertTrue(self.path.exists())

    def test_regular_file_and_symlink_are_preserved(self):
        self.path.write_text('keep')
        with self.assertRaises(AudioError):
            self.connect([255])
        self.assertEqual(self.path.read_text(), 'keep')
        self.path.unlink()
        target = self.runtime / 'target'
        target.write_text('keep')
        self.path.symlink_to(target)
        with self.assertRaises(AudioError):
            self.connect([255])
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), 'keep')

    def test_authentication_failure_has_actionable_message(self):
        with patch('primebuddy.voice_support.robot_audio.subprocess.run', side_effect=[
                subprocess.CompletedProcess([], 255), subprocess.CalledProcessError(255, 'ssh')]):
            with self.assertRaisesRegex(AudioError, 'SSH 登录失败'):
                connect_soc0(self.runtime)

    def test_cli_shows_safe_audio_error(self):
        from primebuddy.__main__ import main
        output = io.StringIO()
        with patch('sys.argv', ['primebuddy']), \
                patch('primebuddy.__main__.run', side_effect=AudioError('跨板 SSH 未连接')), \
                contextlib.redirect_stderr(output):
            self.assertEqual(main(), 1)
        self.assertIn('跨板 SSH 未连接', output.getvalue())

    def test_failed_preflight_names_component_and_does_not_take_over_agent(self):
        from primebuddy.voice import serve
        registry, context, robot = Mock(), Mock(), Mock()
        robot.check.return_value = dict(play_service=True, stop_service=False, soc0_ssh_connected=False)
        registry.planning_context.return_value = {'skills': []}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('primebuddy.voice_support.runtime.private_runtime', return_value=self.runtime))
            for name in ('connect_soc0',):
                stack.enter_context(patch('primebuddy.voice_support.robot_audio.' + name))
            stack.enter_context(patch('primebuddy.voice_support.speech_audio.native_preflight'))
            stack.enter_context(patch('primebuddy.voice_support.qwen_asr.load_config'))
            stack.enter_context(patch('primebuddy.voice.vocabulary', return_value=[]))
            stack.enter_context(patch('primebuddy.voice_support.robot_audio.audio_session',
                                      return_value=contextlib.nullcontext(robot)))
            agent = stack.enter_context(patch('primebuddy.voice_support.native_agent.voice_only_agent'))
            with self.assertRaisesRegex(AudioError, '停止播放服务.*跨板 SSH'):
                serve(registry, context)
            agent.assert_not_called()
