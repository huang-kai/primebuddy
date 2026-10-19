"""型号识别、部署位置、管理后端与独立音频任务的语音调度。"""
import contextlib
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from primebuddy.config.storage import SetupError
from primebuddy.core.engine import Context, execute, validate_plan
from primebuddy.integrations.primebot import device, native_agent
from primebuddy.integrations.primebot.robot_audio import RobotAudio, AudioError


class DeviceTests(unittest.TestCase):
    def test_identity_selects_t1_q1_on_either_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for series, kind in [('T1', device.T1Voice), ('Q1', device.Q1Voice)]:
                for soc in ('0', '1'):
                    for name, value in dict(series=series, sku=series + '-test', soc_index=soc).items():
                        (root / name).write_text(value)
                    identity = device.detect_device(root)
                    with patch.object(device, 'detect_device', return_value=identity):
                        facade = device.create_voice()
                    self.assertIsInstance(facade, kind)
                    self.assertEqual(facade.stream_id, 0)

    def test_missing_or_unknown_identity_never_defaults_to_t1(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(SetupError):
                device.detect_device(root)
            for name, value in dict(series='unknown', sku='Q1', soc_index='0').items():
                (root / name).write_text(value)
            with self.assertRaises(SetupError):
                device.detect_device(root)

    def test_local_audio_does_not_start_ssh_or_copy_files(self):
        robot = RobotAudio.__new__(RobotAudio)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audio.wav'
            path.write_bytes(b'fixture')
            with patch.object(device, 'audio_host', return_value='local'), \
                    patch('primebuddy.integrations.primebot.robot_audio.audio_host', return_value='local'), \
                    patch('primebuddy.integrations.primebot.robot_audio.subprocess.run') as run:
                self.assertEqual(device.host_command(Path(directory), 'true'), ['bash', '-c', 'true'])
                with robot.prepared_audio(path) as target:
                    self.assertEqual(target, path.resolve())
                run.assert_not_called()

    def test_explicit_audio_host_is_not_forced_by_controller(self):
        with patch.dict('os.environ', {'PRIMEBOT_AUDIO_HOST': 'local'}):
            with patch.object(device, 'detect_device', side_effect=AssertionError('must not infer topology')):
                self.assertEqual(device.audio_host(), 'local')
        with patch.dict('os.environ', {'PRIMEBOT_AUDIO_HOST': 'run@robot.example'}):
            self.assertEqual(device.audio_host(), 'run@robot.example')

    def test_mode_rpc_prefers_each_models_installed_sdk_environment(self):
        for series, preferred in [('T1', 'app_proxy'), ('Q1', 'housekeeper')]:
            with patch.object(device, 'detect_device', return_value=device.Device(series, series, '0')), \
                    patch.object(device, 'audio_host', return_value='local'):
                command = device.host_command(Path('/tmp'), 'python3 -c pass', ros=True)[-1]
                other = 'housekeeper' if preferred == 'app_proxy' else 'app_proxy'
                self.assertLess(command.index(preferred), command.index(other))


class AgentBackendTests(unittest.TestCase):
    def test_yamo_is_selected_by_availability(self):
        for code, stdout, expected in [(0, b'/usr/local/bin/yamo\n', 'yamo'), (1, b'', 'http')]:
            with patch.object(native_agent, 'host_command', return_value=['probe']), \
                    patch.object(native_agent.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, stdout)):
                self.assertEqual(native_agent.agent_backend(Path('/tmp')), expected)

    def test_transport_failure_cannot_be_treated_as_yamo_absence(self):
        with patch.object(native_agent, 'host_command', return_value=['probe']), \
                patch.object(native_agent.subprocess, 'run', return_value=subprocess.CompletedProcess([], 255, b'')):
            with self.assertRaises(AudioError):
                native_agent.agent_backend(Path('/tmp'))

    def test_failed_yamo_write_never_falls_back_to_http(self):
        with patch.object(native_agent, 'host_command', side_effect=lambda runtime, command: [command]) as command, \
                patch.object(native_agent.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as run:
            with self.assertRaises(AudioError):
                native_agent.agent_rpc(Path('/tmp'), 'stop', 'yamo')
            command.assert_called_once_with(Path('/tmp'), 'yamo em stop-app agent')
            run.assert_called_once()


class SpeakerSchedulingTests(unittest.TestCase):
    def registry(self, music, light):
        registry = Mock()
        registry.skills = {name: {'actions': {'run': {'resources': resources}}}
                           for name, resources in [('music', ['speaker.music']), ('light', ['lights'])]}
        modules = {}
        for name, callback in [('music', music), ('light', light)]:
            modules[name] = Mock()
            modules[name].run.side_effect = callback
            modules[name].describe.return_value = name
        registry.load.side_effect = modules.__getitem__
        return registry

    def plan(self):
        return {'reply': '', 'tasks': [dict(id=name, skill=name, action='run', args={}, after=[])
                                       for name in ('music', 'light')]}

    def test_other_result_is_deferred_until_music_really_finishes(self):
        music_started, light_done = threading.Event(), threading.Event()
        playing = threading.Event()
        spoken = []
        def music(*args):
            playing.set()
            music_started.set()
            if not light_done.wait(2):
                raise RuntimeError('fixture stalled')
            playing.clear()
            return {'status': 'success', 'message': 'music ended'}
        def light(*args):
            if not music_started.wait(2):
                raise RuntimeError('fixture stalled')
            light_done.set()
            return {'status': 'success', 'message': 'light finished'}
        def announce(message):
            self.assertFalse(playing.is_set(), '框架不能在独立 skill 播音乐时插入播报')
            spoken.append(message)
        results = execute(self.plan(), self.registry(music, light), Context(simulate=False), announce)
        self.assertEqual(results['music']['status'], 'success')
        self.assertIn('light finished', spoken)

    def test_speaker_subtypes_are_the_same_physical_resource(self):
        registry = self.registry(lambda *a: None, lambda *a: None)
        registry.skills['light']['actions']['run']['resources'] = ['speaker.voice']
        with self.assertRaisesRegex(ValueError, '同一资源'):
            validate_plan(self.plan(), registry)


class SkillShutdownTests(unittest.TestCase):
    def test_unused_module_is_not_imported_during_shutdown(self):
        from primebuddy.skills.runtime import SkillRuntime
        runtime = SkillRuntime({'entrypoint': 'not_loaded.py'})
        with patch('primebuddy.skills.manager.load_entry', side_effect=AssertionError('must not load')):
            runtime.close()

    def test_all_loaded_skills_are_closed_even_when_one_fails(self):
        from primebuddy.skills.registry import Registry
        registry = Registry.__new__(Registry)
        first, second = Mock(), Mock()
        first.close.side_effect = RuntimeError('cleanup failed')
        registry.modules = {'first': first, 'second': second}
        with self.assertRaises(SetupError):
            registry.close()
        second.close.assert_called_once()
