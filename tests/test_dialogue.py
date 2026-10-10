"""执行反馈顺序与播报结束边界的回归测试，不连接机器人。"""
import contextlib
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from primebuddy.engine import Context, execute
from primebuddy.registry import Registry
from primebuddy.voice_support.native_voice import NativeVoiceInput
from primebuddy.voice_support.robot_audio import RobotAudio, AudioError

ROOT = Path(__file__).resolve().parents[1]


class DialogueTests(unittest.TestCase):
    def test_acknowledgement_precedes_write_and_verified_result_follows(self):
        registry = Registry(ROOT.parent / 'primebuddy-skills')
        events = []
        proposal = {'reply': '', 'tasks': [{'id': 'light', 'skill': 'yeelight', 'action': 'set_power',
            'args': {'power': True, 'scope': 'device', 'room': '', 'device': '餐桌灯'}, 'after': []}]}
        def run(*args):
            events.append('write-and-verify')
            return {'status': 'success', 'message': '餐桌灯已打开。'}
        with patch.object(registry.load('yeelight'), 'run', side_effect=run):
            execute(proposal, registry, Context(), events.append)
        self.assertTrue(events[0].startswith(('好的，', '收到，', '明白，')))
        self.assertIn('正在打开餐桌灯', events[0])
        self.assertEqual(events[1:], ['write-and-verify', '餐桌灯已打开。'])

    def test_immediate_answer_during_audio_cleanup_needs_no_wake(self):
        node = SimpleNamespace(get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=1)))
        mic = NativeVoiceInput(None, SimpleNamespace(node=node))
        robot = object.__new__(RobotAudio)
        @contextlib.contextmanager
        def prepared(_):
            yield 'remote.wav'
            # 用户在听到提问结束后立即回答；此时旧实现仍在清理远端文件。
            for state in (1, 3):
                mic.on_audio(SimpleNamespace(stream_id=0, audio_vad_state=SimpleNamespace(value=state),
                    header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=2)),
                    audio_data=b'\0' * 6400))
        with patch.object(robot, 'prepared_audio', prepared), patch.object(robot, 'play_file', return_value={}):
            robot.play('question.wav', on_finished=lambda: mic.listen(0))
        self.assertEqual(mic.events.get_nowait()[1], 'utterance')
        self.assertIsNone(mic.conversation_state)  # 没有收到任何唤醒事件。

    def test_failed_playback_does_not_open_answer_window(self):
        robot = object.__new__(RobotAudio)
        opened = []
        with patch.object(robot, 'prepared_audio', return_value=contextlib.nullcontext('remote.wav')), \
                patch.object(robot, 'play_file', side_effect=AudioError('failed')):
            with self.assertRaises(AudioError):
                robot.play('question.wav', on_finished=lambda: opened.append(True))
        self.assertEqual(opened, [])

    def dialogue(self, inputs, proposals, result=None, bridge=None):
        import tempfile
        from primebuddy.voice import serve
        registry = Registry(ROOT.parent / 'primebuddy-skills')
        trace = []
        node = SimpleNamespace(get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=1)))
        robot = SimpleNamespace(node=node, spin=lambda: None,
            check=lambda: dict(play_service=True, stop_service=True, soc0_ssh_connected=True))
        answers = iter(inputs)
        def play(text, on_finished=None):
            trace.append(('tts', text))
            if on_finished:
                on_finished()
        robot.play = play
        class Microphone(NativeVoiceInput):
            def start(self):
                self.ready.set()
            def check_health(self):
                pass
            def listen(self, generation):
                super().listen(generation)
                # 在最后一句提示播放完成时立即送来回答；没有 wake/state 事件。
                for state in (1, 3):
                    self.on_audio(SimpleNamespace(stream_id=0, audio_vad_state=SimpleNamespace(value=state),
                        header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=2)),
                        audio_data=b'\0' * 6400))
        def asr(*args):
            text = next(answers)
            trace.append(('asr', text))
            return text
        def run(*args):
            trace.append(('execute', args[1]))
            return result or {'status': 'success', 'message': '天际线灯已打开。'}
        snapshot = {'yeelight': {'status': 'success', 'rooms': ['客厅'],
                     'devices': [{'name': '天际线灯', 'room': '客厅'}]}}
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            patches = {
                'primebuddy.voice_support.native_voice.NativeVoiceInput': Microphone,
                'primebuddy.voice_support.native_agent.voice_only_agent': lambda _: contextlib.nullcontext(),
                'primebuddy.voice_support.robot_audio.audio_session': lambda _: contextlib.nullcontext(robot),
                'primebuddy.voice_support.robot_audio.connect_soc0': lambda _: None,
                'primebuddy.voice_support.robot_audio.check_cancel': lambda: None,
                'primebuddy.voice_support.speech_audio.native_preflight': lambda: None,
                'primebuddy.voice_support.speech_audio.encode_asr_wav': lambda *a, **k: b'wav',
                'primebuddy.voice_support.qwen_asr.load_config': lambda: {},
                'primebuddy.voice_support.qwen_asr.transcribe_wav': asr,
                'primebuddy.voice_support.runtime.private_runtime': lambda: Path(directory),
                'primebuddy.voice.SpeechCache': lambda _: SimpleNamespace(prepare=lambda text: text),
            }
            for name, value in patches.items():
                stack.enter_context(patch(name, value))
            if bridge:
                stack.enter_context(patch('primebuddy.sdk_bridge.SdkBridge', return_value=bridge))
            stack.enter_context(patch.object(registry, 'planning_context', return_value=snapshot))
            stack.enter_context(patch.object(registry.load('yeelight'), 'run', side_effect=run))
            plans = iter(proposals)
            def propose(*args):
                trace.append(('planning_history', copy.deepcopy(args[3])))
                return next(plans)
            planner = stack.enter_context(patch('primebuddy.voice.plan', side_effect=propose))
            serve(registry, Context(), sdk_bridge='namespaced' if bridge else 'off')
            return trace, planner.call_args_list

    def test_sdk_bridge_receives_asr_and_closes_when_dialogue_exits(self):
        from unittest.mock import Mock
        bridge = Mock()
        bridge.topic, bridge.service_name = '/primebuddy/asr_result', '/primebuddy/PlayTts'
        self.dialogue(['你好', '退出语音助手'], [{'reply': '你好。', 'tasks': []}], bridge=bridge)
        self.assertEqual([call.args[0] for call in bridge.publish_asr.call_args_list], ['你好', '退出语音助手'])
        bridge.start.assert_called_once()
        bridge.close.assert_called_once()
        self.assertTrue(bridge.drain.called)

    def test_sdk_start_failure_still_closes_bridge(self):
        from unittest.mock import Mock
        bridge = Mock()
        bridge.start.side_effect = RuntimeError('endpoint conflict')
        with self.assertRaisesRegex(RuntimeError, 'endpoint conflict'):
            self.dialogue([], [], bridge=bridge)
        bridge.close.assert_called_once()

    def proposal(self):
        return {'reply': '', 'tasks': [{'id': 'light', 'skill': 'yeelight', 'action': 'set_power',
            'args': {'power': True, 'scope': 'device', 'room': '', 'device': '天际线灯'}, 'after': []}]}

    def test_confirmation_and_execution_without_second_wake_or_second_llm(self):
        for answer in ('是的', '好的', '嗯'):
            with self.subTest(answer=answer):
                trace, calls = self.dialogue(['打开天气限灯', answer, '退出语音助手'], [self.proposal()])
                self.assertEqual(len(calls), 1)
                execute_at = next(i for i, item in enumerate(trace) if item[0] == 'execute')
                self.assertLess(trace.index(('asr', answer)), execute_at)
                self.assertTrue(any(k == 'tts' and '请确认：打开天际线灯' in v for k, v in trace[:execute_at]))
                self.assertIn(('tts', '天际线灯已打开。'), trace[execute_at + 1:])

    def test_cancel_does_not_execute_pending_task(self):
        trace, calls = self.dialogue(['打开天气限灯', '取消', '退出语音助手'], [self.proposal()])
        self.assertFalse(any(k == 'execute' for k, _ in trace))
        self.assertEqual(len(calls), 1)

    def test_clarification_remembers_previous_question_without_wake(self):
        trace, calls = self.dialogue(['打开灯', '天际线灯', '退出语音助手'],
            [{'reply': '要打开哪盏灯？', 'tasks': []}, self.proposal()])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].args[0], '天际线灯')
        history = [v for k, v in trace if k == 'planning_history'][1]
        self.assertEqual(history[0]['content'], '打开灯')
        self.assertIn('要打开哪盏灯', history[1]['content'])
        self.assertIn(('tts', '要打开哪盏灯？'), trace)
        self.assertEqual(sum(k == 'execute' for k, _ in trace), 1)

    def test_unverified_result_never_announces_success(self):
        trace, _ = self.dialogue(['打开天际线灯', '退出语音助手'], [self.proposal()],
            {'status': 'failed', 'message': '灯控请求已发送，但目标状态尚未确认，请检查灯具。'})
        self.assertFalse(any(k == 'tts' and '已打开' in v for k, v in trace))
