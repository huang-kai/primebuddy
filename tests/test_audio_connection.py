"""跨板连接、互斥与空闲音频边界。"""
import contextlib
import io
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from primebuddy.integrations.primebot.robot_audio import AudioError, connect_soc0
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import wave
from primebuddy.integrations.primebot.robot_audio import AudioError, RobotAudio


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pb-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.path = self.runtime / 'soc0.sock'
        self.jetson = patch('primebuddy.integrations.primebot.robot_audio.require_robot')
        host = patch('primebuddy.integrations.primebot.robot_audio.audio_host', return_value='run@10.1.1.100')
        host.start()
        self.addCleanup(host.stop)
        self.jetson.start()
        self.addCleanup(self.jetson.stop)

    def connect(self, codes, start=None):
        def run(args, **kwargs):
            if '-Nf' in args and start:
                start()
            return subprocess.CompletedProcess(args, next(values))
        values = iter(codes)
        with patch('primebuddy.integrations.primebot.robot_audio.subprocess.run', side_effect=run) as call:
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
        with patch('primebuddy.integrations.primebot.robot_audio.subprocess.run', side_effect=[
                subprocess.CompletedProcess([], 255), subprocess.CalledProcessError(255, 'ssh')]):
            with self.assertRaisesRegex(AudioError, 'SSH 登录失败'):
                connect_soc0(self.runtime)

    def test_cli_shows_safe_audio_error(self):
        from primebuddy.cli.commands import main
        output = io.StringIO()
        with patch('sys.argv', ['primebuddy']),\
                patch('primebuddy.cli.commands.dispatch_command', side_effect=AudioError('跨板 SSH 未连接')),\
                contextlib.redirect_stderr(output):
            self.assertEqual(main(), 1)
        self.assertIn('跨板 SSH 未连接', output.getvalue())

    def test_failed_preflight_names_component_and_does_not_take_over_agent(self):
        from primebuddy.voice.session import serve
        registry, context, robot = Mock(), Mock(), Mock()
        robot.check.return_value = dict(play_service=True, stop_service=False, soc0_ssh_connected=False)
        registry.planning_context.return_value = {'skills': []}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('primebuddy.config.runtime.private_runtime', return_value=self.runtime))
            for name in ('connect_soc0',):
                stack.enter_context(patch('primebuddy.integrations.primebot.robot_audio.' + name))
            stack.enter_context(patch('primebuddy.integrations.primebot.speech_audio.native_preflight'))
            stack.enter_context(patch('primebuddy.config.models.load_model', return_value={
                'provider': 'qianwen', 'model': 'test-only', 'endpoint': 'https://example.invalid', 'voice': 'test-only'}))
            stack.enter_context(patch('primebuddy.voice.session.vocabulary', return_value=[]))
            stack.enter_context(patch('primebuddy.integrations.primebot.robot_audio.audio_session',
                                      return_value=contextlib.nullcontext(robot)))
            stack.enter_context(patch('primebuddy.integrations.primebot.device.detect_device',
                return_value=SimpleNamespace(series='T1', sku='T1-2X', soc_index='1')))
            agent = stack.enter_context(patch('primebuddy.integrations.primebot.native_agent.voice_only_agent'))
            with self.assertRaisesRegex(AudioError, '停止播放服务.*跨板 SSH'):
                serve(registry, context)
            agent.assert_not_called()

    def test_native_preflight_checks_each_selected_model_and_vendor_message_fields(self):
        import sys
        from primebuddy.integrations.primebot.speech_audio import native_preflight
        from primebuddy.providers.errors import AsrError
        fields = {'header', 'stream_id', 'audio_vad_state', 'audio_data'}
        audio = SimpleNamespace(get_fields_and_field_types=lambda: fields)
        state = SimpleNamespace(get_fields_and_field_types=lambda: {'main_state'})
        with patch('primebuddy.integrations.primebot.speech_audio.require_robot'), \
                patch('primebuddy.integrations.primebot.speech_audio.load_model', side_effect=lambda role: {'provider': role, 'model': 'test-only'}) as load, \
                patch.dict(sys.modules, {'aimdk_msgs.msg': SimpleNamespace(ProcessedAudioOutput=audio, AgentConversationState=state)}):
            self.assertEqual(native_preflight()['cloud'], {
                'asr': 'asr:test-only', 'llm': 'llm:test-only', 'tts': 'tts:test-only'})
            self.assertEqual([call.args[0] for call in load.call_args_list], ['asr', 'llm', 'tts'])
            fields.remove('audio_data')
            with self.assertRaises(AsrError):
                native_preflight()


class AudioIdleTests(unittest.TestCase):
    def start(self, finish_at, cancel=False):
        robot = object.__new__(RobotAudio)
        robot.node = Mock()
        robot.play_client = Mock()
        robot.call = Mock()
        robot.playback_events = []
        now = [0.0]

        def spin():
            now[0] += .25
            state = 2 if finish_at is not None and now[0] >= finish_at else 1
            robot.playback_events.append((now[0], 'vendor', state))

        def checkpoint():
            if cancel and now[0] >= 2:
                raise InterruptedError('cancelled')

        robot.spin = spin
        self.robot, self.now = robot, now
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'test.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\0' * 48000)
            with patch.dict('sys.modules', {'aimdk_msgs.srv': SimpleNamespace(
                    PlayAudioFile=SimpleNamespace(Request=Mock))}),\
                    patch('primebuddy.integrations.primebot.robot_audio.time.monotonic', side_effect=lambda: now[0]):
                robot.start_file(path, Path('/tmp/test.wav'), checkpoint)

    def test_waits_for_vendor_sound_to_finish_before_submitting(self):
        self.start(finish_at=3)
        self.assertGreaterEqual(self.now[0], 3)
        self.robot.call.assert_called_once()

    def test_busy_timeout_never_submits_or_stops_other_audio(self):
        with self.assertRaisesRegex(AudioError, '其他音频正在播放'):
            self.start(finish_at=None)
        self.assertGreaterEqual(self.now[0], 8)
        self.assertLess(self.now[0], 9)
        self.robot.call.assert_not_called()

    def test_wait_can_be_cancelled_without_submitting(self):
        with self.assertRaises(InterruptedError):
            self.start(finish_at=None, cancel=True)
        self.robot.call.assert_not_called()
