"""语音会话、原厂监听、名称确认与识别协议。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from primebuddy.integrations.primebot.native_voice import NativeVoiceInput
import contextlib
import copy
from pathlib import Path
from primebuddy.core.engine import Context, execute, CleanupUnconfirmed
from primebuddy.skills.registry import Registry
from primebuddy.integrations.primebot.robot_audio import RobotAudio, AudioError
import json
from unittest.mock import Mock, patch
import urllib.error
from primebuddy.providers.qianwen.asr import request_payload, recognize, MODEL, NEW_MODEL, AsrError
from primebuddy.core.grounding import confirmation_for, confirmation_answer, vocabulary
from primebuddy.providers.errors import ProviderNetworkError, AsrNetworkError, TtsNetworkError
from primebuddy.voice.speech_cache import NETWORK_ERROR_PROMPT


class VoiceTests(unittest.TestCase):
    def microphone(self):
        clock = SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=1))
        node = SimpleNamespace(get_clock=lambda: clock)
        return NativeVoiceInput(None, SimpleNamespace(node=node))

    def test_listening_window_is_thirty_seconds(self):
        mic = self.microphone()
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=100):
            mic.listen(0)
        self.assertEqual(mic.deadline, 130)
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=111):
            mic.check_health()
        self.assertEqual(mic.phase, 'listening')

    def test_empty_recognition_resumes_without_extending_or_reopening_deadline(self):
        mic = self.microphone()
        mic.conversation_state = 1
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=100):
            mic.listen(0)
        mic.idle(0)
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=110):
            mic.listen(0, reset_deadline=False)
        self.assertEqual((mic.phase, mic.deadline), ('listening', 130))
        mic.idle(0)
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=131):
            mic.listen(0, reset_deadline=False)
        self.assertEqual(mic.phase, 'muted')
        self.assertEqual(mic.events.get_nowait()[1], 'timeout')
        self.assertTrue(mic.wait_for_new_listening)
        self.assertEqual(mic.deadline, 130)

    def test_empty_recognition_keeps_new_speech_queued_during_asr(self):
        mic = self.microphone()
        mic.conversation_state = 1
        mic.listen(0)
        mic.idle(0)
        mic.robot.node.get_clock = lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=100))
        mic.listen(0, reset_deadline=False)
        # ASR等待期间说的新句子属于原监听窗，不能把开窗时间后移而丢掉。
        for state in (1, 3):
            mic.on_audio(self.audio(state))
        self.assertEqual(mic.events.get_nowait()[1], 'utterance')

    def test_interaction_reset_discards_old_events_audio_and_completion_callbacks(self):
        mic = self.microphone()
        mic.listen(0)
        mic.events.put((0, 'utterance', b'old'))
        mic.interrupts.put(b'old interrupt')
        mic.buffer = bytearray(b'incomplete')
        self.assertEqual(mic.reset_interaction(0), 1)
        self.assertEqual(mic.phase, 'muted')
        self.assertIsNone(mic.buffer)
        self.assertTrue(mic.events.empty())
        self.assertTrue(mic.interrupts.empty())
        mic.listen(0)
        self.assertEqual(mic.phase, 'muted')
        mic.listen(1)
        self.assertEqual(mic.phase, 'listening')

    def test_exclusive_suspends_audio_health_and_reopen_callbacks_until_release(self):
        mic = self.microphone()
        mic.listen(0)
        mic.events.put((0, 'utterance', b'old'))
        generation = mic.suspend_interaction(0)
        mic.ready.set()
        mic.robot.node.count_publishers = Mock(return_value=0)
        for state in (0, 3, 1):
            mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=state)))
            # 无需解析独占期间的消息，即使内容格式错误也不影响宿主。
            mic.on_audio(object())
            mic.check_health()
            mic.listen(generation)
            mic.wait_for_listening(generation)
            mic.monitor_interrupt(generation)
            mic.idle(generation)
        self.assertEqual(mic.phase, 'exclusive')
        self.assertIsNone(mic.error)
        self.assertIsNone(mic.buffer)
        self.assertTrue(mic.events.empty())
        self.assertTrue(mic.interrupts.empty())
        mic.robot.node.count_publishers.assert_not_called()
        generation = mic.reset_interaction(generation)
        mic.listen(generation)
        for state in (1, 3):
            mic.on_audio(self.audio(state))
        self.assertEqual(mic.events.get_nowait()[:2], (2, 'utterance'))

    def test_native_listening_transition_opens_one_window(self):
        mic = self.microphone()
        mic.wait_for_listening(0)
        mic.conversation_state = 0
        msg = SimpleNamespace(main_state=SimpleNamespace(value=1))
        mic.on_state(msg)
        mic.on_state(msg)
        self.assertEqual(mic.events.qsize(), 1)
        self.assertEqual(mic.events.get()[1], 'native_listening')

    def test_first_native_listening_snapshot_opens_one_window(self):
        mic = self.microphone()
        mic.wait_for_listening(0)
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
        self.assertEqual(mic.phase, 'listening')
        self.assertEqual(mic.events.get_nowait()[1], 'native_listening')
        deadline = mic.deadline
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
        self.assertTrue(mic.events.empty())
        self.assertEqual(mic.deadline, deadline)

    def test_waiting_follows_already_known_native_listening_once(self):
        mic = self.microphone()
        mic.conversation_state = 1
        mic.wait_for_listening(0)
        self.assertEqual(mic.phase, 'listening')
        self.assertEqual(mic.events.get_nowait()[1], 'native_listening')
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
        self.assertTrue(mic.events.empty())

    def test_timeout_waits_until_native_listening_opens_again(self):
        mic = self.microphone()
        mic.conversation_state = 1
        mic.listen(0)
        mic.timeout()
        self.assertEqual(mic.events.get_nowait()[1], 'timeout')
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=0)))
        mic.wait_for_listening(0)
        self.assertEqual(mic.phase, 'waiting')
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=0)))
        self.assertTrue(mic.events.empty())
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
        self.assertEqual(mic.phase, 'listening')
        self.assertEqual(mic.events.get_nowait()[1], 'native_listening')

    def test_timeout_does_not_reopen_from_cached_or_repeated_listening(self):
        mic = self.microphone()
        mic.conversation_state = 1
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=100):
            mic.listen(0)
        mic.timeout()
        self.assertEqual(mic.events.get_nowait()[1], 'timeout')
        mic.wait_for_listening(0)
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
        self.assertEqual(mic.phase, 'waiting')
        self.assertTrue(mic.events.empty())
        self.assertEqual(mic.deadline, 130)
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=0)))
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=140):
            mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
        self.assertEqual(mic.events.get_nowait()[1], 'native_listening')
        self.assertEqual(mic.deadline, 170)
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
        self.assertTrue(mic.events.empty())
        self.assertEqual(mic.deadline, 170)
        mic.timeout()
        self.assertEqual(mic.events.get_nowait()[1], 'timeout')
        # 明确的业务播放结束仍能直接开窗，不依赖原厂再次转换状态。
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=200):
            mic.listen(0)
        self.assertEqual(mic.phase, 'listening')
        self.assertEqual(mic.deadline, 230)

    def audio(self, state, pcm=b'\0' * 6400):
        return SimpleNamespace(stream_id=0, audio_vad_state=SimpleNamespace(value=state),
            header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=2)), audio_data=pcm)

    def test_playback_state_and_complete_vad_do_not_open_or_cancel(self):
        mic = self.microphone()
        mic.idle(0)
        mic.conversation_state = 3
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
        mic.on_audio(self.audio(1))
        self.assertTrue(mic.events.empty())
        self.assertTrue(mic.interrupts.empty())
        mic.on_audio(self.audio(3))
        self.assertTrue(mic.interrupts.empty())
        self.assertTrue(mic.events.empty())
        self.assertEqual(mic.phase, 'muted')
        self.assertEqual(mic.generation, 0)

    def test_known_native_speaking_rejects_audio_without_extending_window(self):
        mic = self.microphone()
        mic.conversation_state = 3
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=100):
            mic.listen(0)
        for state in (1, 2, 3):
            mic.on_audio(self.audio(state))
        self.assertTrue(mic.events.empty())
        self.assertIsNone(mic.buffer)
        self.assertEqual(mic.phase, 'listening')
        self.assertEqual(mic.deadline, 130)
        self.assertEqual(mic.generation, 0)
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
        for state in (1, 3):
            mic.on_audio(self.audio(state))
        self.assertEqual(mic.events.get_nowait(), (0, 'utterance', b'\0' * 12800))
        self.assertTrue(mic.events.empty())
        self.assertEqual(mic.deadline, 130)

    def test_native_speaking_discards_started_segment_until_new_begin(self):
        mic = self.microphone()
        mic.conversation_state = 1
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=100):
            mic.listen(0)
        mic.on_audio(self.audio(1))
        self.assertIsNotNone(mic.buffer)
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=3)))
        self.assertIsNone(mic.buffer)
        mic.on_audio(self.audio(2))
        mic.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
        mic.on_audio(self.audio(3))
        self.assertTrue(mic.events.empty())
        self.assertEqual(mic.phase, 'listening')
        self.assertEqual(mic.generation, 0)
        self.assertEqual(mic.deadline, 130)
        for state in (1, 3):
            mic.on_audio(self.audio(state))
        self.assertEqual(mic.events.get_nowait(), (0, 'utterance', b'\0' * 12800))
        self.assertTrue(mic.events.empty())
        self.assertEqual(mic.deadline, 130)





    def test_interrupt_keeps_followup_segment_and_rejects_old_listen_callback(self):
        mic = self.microphone()
        mic.monitor_interrupt(0)
        generation = mic.begin_interrupt(0)
        mic.on_audio(self.audio(1))
        mic.listen(0)  # 旧播放的完成回调不能清空新一轮已经开始的音频。
        mic.stop_interrupt(0)
        self.assertIsNotNone(mic.buffer)
        self.assertEqual(mic.phase, 'interrupt')
        mic.resume_after_interrupt(generation)
        mic.on_audio(self.audio(3))
        self.assertEqual(mic.events.get_nowait(), (1, 'utterance', b'\0' * 12800))

    def test_interrupt_keeps_complete_followup_until_cleanup_confirmed(self):
        mic = self.microphone()
        mic.monitor_interrupt(0)
        generation = mic.begin_interrupt(0)
        for state in (1, 3):
            mic.on_audio(self.audio(state))
        self.assertTrue(mic.events.empty())
        mic.resume_after_interrupt(generation)
        self.assertEqual(mic.events.get_nowait()[1], 'utterance')

    def test_incomplete_interrupt_does_not_timeout_the_normal_conversation(self):
        mic = self.microphone()
        mic.monitor_interrupt(0)
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=100):
            mic.on_audio(self.audio(1))
        with patch('primebuddy.integrations.primebot.native_voice.time.monotonic', return_value=104):
            mic.check_health()
        self.assertIsNone(mic.buffer)
        self.assertTrue(mic.events.empty())
        self.assertEqual(mic.phase, 'interrupt')





    def test_no_frontend_audio_is_not_reported_as_user_silence(self):
        mic = self.microphone()
        mic.listen(0)
        mic.timeout()
        _, kind, reason = mic.events.get_nowait()
        self.assertEqual(kind, 'timeout')
        self.assertIn('未收到原厂音频', reason)
        self.assertNotIn('元仔', reason)
        self.assertNotIn('Q仔', reason)

    def test_wrong_stream_and_stale_audio_are_distinguished(self):
        mic = self.microphone()
        msg = SimpleNamespace(stream_id=2, audio_vad_state=SimpleNamespace(value=1))
        mic.listen(0)
        mic.on_audio(msg)
        mic.timeout()
        self.assertIn('其他音频流', mic.events.get_nowait()[2])
        mic.listen(0)
        mic.opened_at_ns = 100
        msg.stream_id = 0
        msg.header = SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=1))
        mic.on_audio(msg)
        mic.timeout()
        self.assertIn('跨板时钟', mic.events.get_nowait()[2])

    def test_incomplete_segment_is_not_reported_as_no_audio(self):
        mic = self.microphone()
        mic.listen(0)
        mic.on_audio(SimpleNamespace(stream_id=0, audio_vad_state=SimpleNamespace(value=3),
            header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=2)), audio_data=b'\0'*6400))
        mic.timeout()
        self.assertIn('未收到完整 VAD', mic.events.get_nowait()[2])

    def test_new_window_resets_packet_diagnostics(self):
        mic = self.microphone()
        mic.window_packets, mic.window_stream_packets, mic.window_stale_packets = 10, 9, 8
        mic.listen(0)
        self.assertEqual((mic.window_packets, mic.window_stream_packets, mic.window_stale_packets), (0, 0, 0))


class VoiceRegistry(Registry):
    """固定语音契约，不加载真实技能、账号配置或设备接口。"""
    def __init__(self):
        light_fields = dict(power={'type': 'boolean'}, scope={'type': 'string'},
                            room={'type': 'string'}, device={'type': 'string'})
        self.skills = {
            'yeelight': {'name': 'yeelight', 'description': '测试灯控', 'actions': {
                'set_power': {'parameters': light_fields, 'resources': ['lights']}}},
            'diangetai': {'name': 'diangetai', 'description': '测试音乐', 'actions': {
                'play': {'parameters': {'artist': {'type': 'string'}, 'song': {'type': 'string'}},
                         'resources': ['speaker']}}},
        }
        self.modules = {}
        self.check_resources = Mock()
        self.prepare_voice = Mock()
        for name in self.skills:
            self.modules[name] = SimpleNamespace(
                validate=Mock(), preflight=Mock(), run=Mock(),
                describe=(lambda action, args: '正在' + ('打开' if args['power'] else '关闭')
                          + (args['device'] or args['room'] or '全屋灯')) if name == 'yeelight'
                         else lambda action, args: '正在播放' + (args['song'] or args['artist']),
                confirmation=Mock(side_effect=lambda action, args, text, snapshot:
                                  '打开天际线灯' if text == '打开天气限灯' else None))

    def read_file(self, name, relative='SKILL.md'):
        return {'instructions': '语音测试技能', 'actions': self.skills[name]['actions']}


class DialogueTests(unittest.TestCase):
    def test_acknowledgement_precedes_write_and_verified_result_follows(self):
        registry = VoiceRegistry()
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
        with patch.object(robot, 'prepared_audio', return_value=contextlib.nullcontext('remote.wav')),\
                patch.object(robot, 'play_file', side_effect=AudioError('failed')):
            with self.assertRaises(AudioError):
                robot.play('question.wav', on_finished=lambda: opened.append(True))
        self.assertEqual(opened, [])

    def dialogue(self, inputs, proposals, result=None, bridge=None, agent_messages=None, tool='yeelight',
                 executor=None, trace=None, prepare_error=None, ros_callbacks=None,
                 robot_series='T1', asr_vocabularies=None, on_wait=None,
                 on_asr=None, on_plan=None, on_prepare=None, cancel_check=None, registry=None, simulate=True,
                 on_play=None, ensure_audio_idle=None):
        import tempfile
        from primebuddy.voice.session import serve
        registry = registry or VoiceRegistry()
        trace = [] if trace is None else trace
        def prepare_voice(context):
            trace.append(('prepare_voice', context.runtime.is_dir()))
            if prepare_error:
                raise prepare_error
        registry.prepare_voice.side_effect = prepare_voice
        node = SimpleNamespace(get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=1)))
        checking_playback = [False]
        def spin(seconds=.05, **kwargs):
            if checking_playback[0]:
                trace.append(('playback_checkpoint_wait', seconds))
            if ros_callbacks:
                ros_callbacks.pop(0)()
        robot = SimpleNamespace(node=node, spin=spin,
            check=lambda: dict(play_service=True, stop_service=True, soc0_ssh_connected=True))
        answers = iter(inputs)
        def play(text, on_finished=None):
            trace.append(('tts', text))
            if on_play:
                on_play(text)
            if on_finished:
                on_finished()
        robot.play = play
        def audio_output(runtime, checkpoint):
            def checked_play(text, **kwargs):
                checkpoint()
                if kwargs.get('checkpoint'):
                    checking_playback[0] = True
                    try:
                        kwargs['checkpoint']()
                    finally:
                        checking_playback[0] = False
                return play(text, on_finished=kwargs.get('on_finished'))
            return SimpleNamespace(play=checked_play, close=lambda: None,
                                   ensure_idle=ensure_audio_idle or (lambda: None))
        microphones = []
        class Microphone(NativeVoiceInput):
            def start(self):
                microphones.append(self)
                self.ready.set()
            def check_health(self):
                pass
            def reset_interaction(self, generation):
                generation = super().reset_interaction(generation)
                trace.append(('interaction_generation', generation))
                return generation
            def wait_for_listening(self, generation):
                super().wait_for_listening(generation)
                trace.append(('waiting', self.phase))
                if on_wait:
                    on_wait(self, trace)
                self.on_state(SimpleNamespace(main_state=SimpleNamespace(value=0)))
                self.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
            def listen(self, generation, **kwargs):
                trace.append(('listen', generation))
                super().listen(generation, **kwargs)
                # 在最后一句提示播放完成时立即送来回答；没有 wake/state 事件。
                for state in (1, 3):
                    self.on_audio(SimpleNamespace(stream_id=0, audio_vad_state=SimpleNamespace(value=state),
                        header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=2)),
                        audio_data=b'\0' * 6400))
        def asr(*args, **kwargs):
            if asr_vocabularies is not None:
                asr_vocabularies.append(kwargs.get('vocabulary', []))
            text = next(answers)
            trace.append(('asr', text))
            if isinstance(text, BaseException):
                raise text
            if on_asr:
                on_asr()
            return text
        def prepare(text):
            if on_prepare:
                on_prepare(text)
            return text
        def run(*args):
            trace.append(('execute', args[1]))
            return result or {'status': 'success', 'message': '天际线灯已打开。'}
        snapshot = {'yeelight': {'status': 'success', 'rooms': ['客厅'],
                     'devices': [{'name': '天际线灯', 'room': '客厅'}]}}
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            patches = {
                'primebuddy.integrations.primebot.device.detect_device': lambda: SimpleNamespace(
                    series=robot_series, sku='T1-2X' if robot_series == 'T1' else 'Q1-1X', soc_index='1'),
                'primebuddy.integrations.primebot.native_voice.NativeVoiceInput': Microphone,
                'primebuddy.voice.audio.AudioOutput': audio_output,
                'primebuddy.integrations.primebot.native_agent.voice_only_agent': lambda _: contextlib.nullcontext(),
                'primebuddy.integrations.primebot.robot_audio.audio_session': lambda _: contextlib.nullcontext(robot),
                'primebuddy.integrations.primebot.robot_audio.connect_soc0': lambda _: None,
                'primebuddy.integrations.primebot.robot_audio.check_cancel': cancel_check or (lambda: None),
                'primebuddy.integrations.primebot.speech_audio.native_preflight': lambda: None,
                'primebuddy.integrations.primebot.speech_audio.encode_asr_wav': lambda *a, **k: b'wav',
                'primebuddy.config.models.load_model': lambda *args: {'provider': 'qianwen', 'model': 'test-only'},
                'primebuddy.providers.transcribe_wav': asr,
                'primebuddy.config.runtime.private_runtime': lambda: Path(directory),
                'primebuddy.voice.session.SpeechCache': lambda _: SimpleNamespace(prepare=prepare),
            }
            for name, value in patches.items():
                stack.enter_context(patch(name, value))
            if executor is not None:
                stack.enter_context(patch('primebuddy.voice.session.execute',
                    side_effect=lambda *args, **kwargs: executor(*args, microphone=microphones[0],
                                                                  trace=trace, **kwargs)))
            if bridge:
                stack.enter_context(patch('primebuddy.integrations.primebot.sdk_bridge.SdkBridge', return_value=bridge))
            stack.enter_context(patch.object(registry, 'planning_context', return_value=snapshot))
            stack.enter_context(patch.object(registry.load(tool), 'run', side_effect=run))
            plans = iter(proposals)
            def propose(*args, **kwargs):
                trace.append(('planning_history', copy.deepcopy(args[3])))
                if on_plan:
                    on_plan()
                proposal = next(plans)
                if isinstance(proposal, BaseException):
                    raise proposal
                return proposal
            if agent_messages is None:
                planner = stack.enter_context(patch('primebuddy.voice.session.plan', side_effect=propose))
            else:
                messages = iter(agent_messages)
                planner = stack.enter_context(patch('primebuddy.core.planner.chat', side_effect=lambda *args: next(messages)))
            serve(registry, Context(simulate=simulate), sdk_bridge='namespaced' if bridge else 'off')
            return trace, planner.call_args_list

    def test_playback_checkpoints_do_not_wait_on_the_host_executor(self):
        trace, _ = self.dialogue(['你好', '退出语音助手'], [{'reply': '你好。', 'tasks': []}])
        # 启动/退出没有结束回调，普通回答有结束回调；两条播放分支均不得延迟媒体反馈。
        self.assertEqual([value for kind, value in trace if kind == 'playback_checkpoint_wait'], [0, 0, 0])

    def test_successful_exclusive_application_asks_once_after_resource_verification(self):
        for reason in ('inactivity', 'exit'):
            registry = VoiceRegistry()
            registry.skills['diangetai']['actions']['play']['interaction'] = 'exclusive'
            proposal = {'reply': '', 'tasks': [{'id': 'game', 'skill': 'diangetai', 'action': 'play',
                        'args': {'artist': '', 'song': '交互应用测试'}, 'after': []}]}
            trace = []
            registry.load('diangetai')._verify = Mock(side_effect=lambda context: trace.append(('verified', True)))
            message = '成语接龙因两分钟无回应结束。' if reason == 'inactivity' else '成语接龙已结束。'
            with self.subTest(reason=reason), patch('primebuddy.voice.session.continue_plan') as continuation:
                self.dialogue(['开始游戏', '退出语音助手'], [proposal], registry=registry, trace=trace,
                    tool='diangetai', simulate=False,
                    result={'status': 'success', 'message': message, 'reason': reason, 'resources_released': True})
                spoken = [event[1] for event in trace if event[0] == 'tts']
                self.assertNotIn(message, spoken)
                self.assertEqual(spoken.count('接下来，你还想让我帮你做些什么呢？'), 1)
                self.assertEqual(len(spoken), 4)  # 宿主启动、开始任务、交接提问、显式退出 Buddy。
                self.assertIn('交互应用测试', spoken[1])
                self.assertLess(trace.index(('verified', True)), trace.index(('interaction_generation', 2)))
                self.assertLess(trace.index(('interaction_generation', 2)), trace.index(('tts', '接下来，你还想让我帮你做些什么呢？')))
                self.assertLess(trace.index(('tts', '接下来，你还想让我帮你做些什么呢？')), trace.index(('listen', 2)))
                registry.load('diangetai')._verify.assert_called_once()
                continuation.assert_not_called()
                self.assertEqual([event[1] for event in trace if event[0] == 'asr'], ['开始游戏', '退出语音助手'])

    def test_unsuccessful_exclusive_application_keeps_result_and_retry_prompt(self):
        for status in ('failed', 'clarification_required', 'auth_required'):
            registry = VoiceRegistry()
            registry.skills['diangetai']['actions']['play']['interaction'] = 'exclusive'
            registry.load('diangetai')._verify = Mock()
            proposal = {'reply': '', 'tasks': [{'id': 'game', 'skill': 'diangetai', 'action': 'play',
                        'args': {'artist': '', 'song': '交互应用测试'}, 'after': []}]}
            with self.subTest(status=status):
                trace, _ = self.dialogue(['开始游戏', '退出语音助手'], [proposal], registry=registry,
                    tool='diangetai', simulate=False,
                    result={'status': status, 'message': '应用尚未完成，请检查配置。', 'resources_released': True})
                spoken = [event[1] for event in trace if event[0] == 'tts']
                self.assertIn('应用尚未完成，请检查配置。', spoken)
                self.assertIn('接下来，你还想让我帮你做些什么呢？', spoken)
                registry.load('diangetai')._verify.assert_called_once()

    def test_exclusive_application_keeps_host_muted_then_accepts_only_a_new_request(self):
        registry = VoiceRegistry()
        registry.skills['diangetai']['actions']['play']['interaction'] = 'exclusive'
        bridge = Mock()
        bridge.topic, bridge.service_name = '/primebuddy/asr_result', '/primebuddy/PlayTts'
        proposal = {'reply': '', 'tasks': [{'id': 'game', 'skill': 'diangetai', 'action': 'play',
                    'args': {'artist': '', 'song': '交互应用测试'}, 'after': []}]}
        calls = []
        def executor(proposal, registry, context, announce, checkpoint, microphone, trace, cleanup_checkpoint):
            calls.append(proposal['tasks'][0]['id'])
            if calls[-1] != 'game':
                return {'light': {'status': 'success', 'message': '灯已打开'}}
            self.assertTrue(bridge.busy)
            self.assertEqual((microphone.phase, microphone.generation), ('exclusive', 1))
            for native_state in (0, 3, 1):
                microphone.on_state(SimpleNamespace(main_state=SimpleNamespace(value=native_state)))
                for state in (1, 3):
                    microphone.on_audio(SimpleNamespace(stream_id=0, audio_vad_state=SimpleNamespace(value=state),
                        header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=2)), audio_data=b'\0' * 6400))
                checkpoint()
            self.assertTrue(microphone.events.empty())
            self.assertEqual([event[1] for event in trace if event[0] == 'asr'], ['开始游戏'])
            microphone.events.put((1, 'utterance', b'late game answer'))
            microphone.interrupts.put(b'late interrupt')
            microphone.buffer = bytearray(b'late partial answer')
            def verify(context):
                self.assertEqual((microphone.phase, microphone.generation), ('exclusive', 1))
                self.assertFalse(microphone.events.empty())
                trace.append(('resources_released', True))
            registry.load('diangetai')._verify = Mock(side_effect=verify)
            return {'game': {'status': 'success', 'message': '游戏已结束'}}
        from primebuddy.core.planner import continue_plan
        with patch('primebuddy.voice.session.continue_plan', wraps=continue_plan) as continuation:
            trace, planned = self.dialogue(['开始游戏', '打开天际线灯', '退出语音助手'],
                [proposal, self.proposal()], executor=executor, registry=registry, bridge=bridge, simulate=False)
        self.assertEqual(calls, ['game', 'light'])
        self.assertEqual(len(planned), 2)
        self.assertEqual(continuation.call_count, 1)
        self.assertEqual(continuation.call_args.args[0]['tasks'][0]['skill'], 'yeelight')
        self.assertLess(trace.index(('resources_released', True)), trace.index(('listen', 2)))
        self.assertEqual([event[1] for event in trace if event[0] == 'asr'],
                         ['开始游戏', '打开天际线灯', '退出语音助手'])
        registry.load('diangetai')._verify.assert_called_once()

    def test_exclusive_cleanup_failure_keeps_host_alive_until_explicit_shutdown(self):
        for failure in ('execute', 'status'):
            registry = VoiceRegistry()
            registry.skills['diangetai']['actions']['play']['interaction'] = 'exclusive'
            registry.load('diangetai')._verify = Mock(side_effect=CleanupUnconfirmed('尚未释放'))
            proposal = {'reply': '', 'tasks': [{'id': 'game', 'skill': 'diangetai', 'action': 'play',
                        'args': {'artist': '', 'song': '交互应用测试'}, 'after': []}]}
            trace, microphones = [], []
            def executor(*args, microphone, **kwargs):
                microphones.append(microphone)
                if failure == 'execute':
                    raise CleanupUnconfirmed('尚未释放')
                return {'game': {'status': 'success', 'message': '整局结束'}}
            def shutdown_after_retry():
                if registry.load('diangetai')._verify.call_count >= 2:
                    self.assertFalse(microphones[0].closed.is_set())
                    self.assertEqual(microphones[0].phase, 'exclusive')
                    raise KeyboardInterrupt()
            with self.subTest(failure=failure), self.assertRaises(KeyboardInterrupt), \
                    patch('primebuddy.voice.session.INTERACTION_RECOVERY_INTERVAL', 0):
                self.dialogue(['开始游戏'], [proposal], executor=executor, registry=registry,
                              trace=trace, simulate=False, cancel_check=shutdown_after_retry)
            self.assertEqual(microphones[0].generation, 1)
            self.assertNotIn(('tts', '接下来，你还想让我帮你做些什么呢？'), trace)
            self.assertNotIn(('listen', 2), trace)

    def test_exclusive_cleanup_retry_recovers_then_accepts_a_new_skill(self):
        for failure in ('execute', 'status'):
            registry = VoiceRegistry()
            registry.skills['diangetai']['actions']['play']['interaction'] = 'exclusive'
            proposal = {'reply': '', 'tasks': [{'id': 'game', 'skill': 'diangetai', 'action': 'play',
                        'args': {'artist': '', 'song': '交互应用测试'}, 'after': []}]}
            trace, microphones, attempts = [], [], []
            def verify(context, *, recover=False):
                self.assertEqual(microphones[0].phase, 'exclusive')
                self.assertEqual([v for k, v in trace if k == 'asr'], ['开始游戏'])
                attempts.append(recover)
                if len(attempts) == 1:
                    raise CleanupUnconfirmed('清理暂未完成')
                trace.append(('recovered', True))
            registry.load('diangetai')._verify = Mock(side_effect=verify)
            def executor(plan, registry, context, announce, microphone, **kwargs):
                if plan['tasks'][0]['id'] != 'game':
                    trace.append(('new_skill', True))
                    return {'light': {'status': 'success', 'message': '灯已打开'}}
                microphones.append(microphone)
                microphone.events.put((microphone.generation, 'utterance', b'old game answer'))
                if failure == 'execute':
                    context.cancelled.set()
                    raise CleanupUnconfirmed('游戏异常退出，设备清理未确认')
                return {'game': {'status': 'success', 'message': '整局结束'}}
            with self.subTest(failure=failure), patch('primebuddy.voice.session.INTERACTION_RECOVERY_INTERVAL', 0):
                self.dialogue(['开始游戏', '打开天际线灯', '退出语音助手'], [proposal, self.proposal()],
                              executor=executor, registry=registry, trace=trace, simulate=False)
            self.assertEqual(attempts, [failure == 'execute', True])
            self.assertLess(trace.index(('recovered', True)), trace.index(('listen', 2)))
            self.assertLess(trace.index(('listen', 2)), trace.index(('new_skill', True)))
            self.assertEqual([v for k, v in trace if k == 'asr'], ['开始游戏', '打开天际线灯', '退出语音助手'])

    def test_exclusive_failure_verifies_release_before_new_generation_and_error_prompt(self):
        registry = VoiceRegistry()
        registry.skills['diangetai']['actions']['play']['interaction'] = 'exclusive'
        trace = []
        registry.load('diangetai')._verify = Mock(side_effect=lambda context: trace.append(('verified', True)))
        proposal = {'reply': '', 'tasks': [{'id': 'game', 'skill': 'diangetai', 'action': 'play',
                    'args': {'artist': '', 'song': '交互应用测试'}, 'after': []}]}
        def executor(*args, **kwargs):
            raise RuntimeError('game failure after cleanup')
        self.dialogue(['开始游戏', '退出语音助手'], [proposal], executor=executor, registry=registry,
                      trace=trace, simulate=False)
        self.assertLess(trace.index(('verified', True)), trace.index(('listen', 2)))
        self.assertEqual(sum(event == ('verified', True) for event in trace), 1)

    def test_asr_network_failure_uses_offline_notice_and_suppresses_repeats(self):
        trace, calls = self.dialogue([AsrNetworkError(), AsrNetworkError(), '退出语音助手'], [])
        self.assertEqual([event[1] for event in trace if event[0] == 'tts'],
                         ['PrimeBuddy 已启动，你想做什么？', NETWORK_ERROR_PROMPT, '语音助手已退出。'])
        self.assertEqual(calls, [])

    def test_empty_asr_does_not_speak_publish_or_call_planner(self):
        bridge = Mock()
        bridge.topic, bridge.service_name = '/primebuddy/asr_result', '/primebuddy/PlayTts'
        trace, calls = self.dialogue(['', '  ', '你好', '退出语音助手'],
                                    [{'reply': '你好。', 'tasks': []}], bridge=bridge)
        self.assertEqual([event[1] for event in trace if event[0] == 'tts'],
                         ['PrimeBuddy 已启动，你想做什么？', '你好。', '语音助手已退出。'])
        self.assertEqual([call.args[0] for call in calls], ['你好'])
        self.assertEqual([call.args[0] for call in bridge.publish_asr.call_args_list],
                         ['你好', '退出语音助手'])

    def test_empty_asr_preserves_pending_confirmation(self):
        trace, calls = self.dialogue(['打开天气限灯', '', '确认', '退出语音助手'], [self.proposal()])
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(event[0] == 'execute' for event in trace), 1)
        self.assertFalse(any(event[0] == 'tts' and event[1].startswith('本轮未能完整完成')
                             for event in trace))

    def test_llm_network_failure_uses_notice_and_accepts_next_turn(self):
        trace, calls = self.dialogue(['你好', '退出语音助手'], [ProviderNetworkError()])
        self.assertEqual(sum(event == ('tts', NETWORK_ERROR_PROMPT) for event in trace), 1)
        self.assertEqual(len(calls), 1)

    def test_business_tts_network_failure_uses_offline_notice(self):
        def prepare(text):
            if text == '你好。':
                raise TtsNetworkError()
        trace, _ = self.dialogue(['你好', '退出语音助手'], [{'reply': '你好。', 'tasks': []}], on_prepare=prepare)
        self.assertEqual([event[1] for event in trace if event[0] == 'tts'],
                         ['PrimeBuddy 已启动，你想做什么？', NETWORK_ERROR_PROMPT, '语音助手已退出。'])

    def test_skill_acknowledgement_network_failure_announces_after_task_cleanup(self):
        def prepare(text):
            if text.startswith(('好的，', '收到，', '明白，')):
                raise TtsNetworkError()
        trace, _ = self.dialogue(['播放小星星', '退出语音助手'], [self.proposal()], on_prepare=prepare)
        self.assertEqual(sum(event == ('tts', NETWORK_ERROR_PROMPT) for event in trace), 1)
        self.assertFalse(any(event[0] == 'execute' for event in trace))

    def test_interrupted_exclusive_acknowledgement_recovers_and_accepts_retry(self):
        registry = VoiceRegistry()
        registry.skills['diangetai']['actions']['play']['interaction'] = 'exclusive'
        trace = []
        registry.load('diangetai')._verify = Mock(
            side_effect=lambda context: trace.append(('verified_cancelled', context.cancelled.is_set())))
        proposal = {'reply': '', 'tasks': [{'id': 'game', 'skill': 'diangetai', 'action': 'play',
                    'args': {'artist': '', 'song': '交互应用测试'}, 'after': []}]}
        interrupted = []
        def play(text):
            if text.startswith(('好的，', '收到，', '明白，')) and not interrupted:
                interrupted.append(text)
                raise AudioError('原厂音频提前停止或播放失败。')
        self.dialogue(['开始游戏', '开始游戏', '退出语音助手'], [proposal, proposal],
                      registry=registry, trace=trace, tool='diangetai', simulate=False, on_play=play)
        error_prompt = next(event for event in trace if event[0] == 'tts' and event[1].startswith('本轮未能完整完成'))
        self.assertLess(trace.index(('verified_cancelled', True)), trace.index(error_prompt))
        self.assertLess(trace.index(error_prompt), trace.index(('listen', 2)))
        self.assertEqual(sum(event[0] == 'execute' for event in trace), 1)
        self.assertIn(('verified_cancelled', False), trace)
        self.assertIn(('tts', '接下来，你还想让我帮你做些什么呢？'), trace)
        self.assertIn(('tts', '语音助手已退出。'), trace)

    def test_interrupted_acknowledgement_with_unreleased_resources_does_not_resume(self):
        registry = VoiceRegistry()
        trace, blocked = [], []
        def check_resources(context, resources):
            if context.cancelled.is_set():
                blocked.append(context)
                raise CleanupUnconfirmed('扬声器尚未释放')
        registry.check_resources.side_effect = check_resources
        def play(text):
            if text.startswith(('好的，', '收到，', '明白，')):
                raise AudioError('原厂音频提前停止或播放失败。')
        with self.assertRaisesRegex(CleanupUnconfirmed, '扬声器尚未释放'):
            self.dialogue(['播放小星星'], [self.proposal()], registry=registry,
                          trace=trace, on_play=play, simulate=False)
        self.assertEqual(len(blocked), 1)
        self.assertFalse(any(event[0] == 'tts' and event[1].startswith('本轮未能完整完成') for event in trace))
        self.assertFalse(any(event[0] == 'execute' for event in trace))

    def test_startup_tts_network_failure_uses_notice_then_waits_for_native(self):
        def prepare(text):
            if text == 'PrimeBuddy 已启动，你想做什么？':
                raise TtsNetworkError()
        trace, _ = self.dialogue(['退出语音助手'], [], on_prepare=prepare)
        self.assertLess(trace.index(('tts', NETWORK_ERROR_PROMPT)), trace.index(('waiting', 'waiting')))

    def test_sdk_tts_network_failure_uses_offline_notice_and_keeps_session(self):
        bridge = Mock()
        bridge.topic, bridge.service_name = '/primebuddy/asr_result', '/primebuddy/PlayTts'
        errors = [TtsNetworkError()]
        def drain(*_):
            if errors:
                raise errors.pop()
        bridge.drain.side_effect = drain
        trace, _ = self.dialogue(['退出语音助手'], [], bridge=bridge)
        self.assertEqual(sum(event == ('tts', NETWORK_ERROR_PROMPT) for event in trace), 1)
        self.assertIn(('tts', '语音助手已退出。'), trace)

    def test_generic_error_tts_network_failure_also_uses_offline_notice(self):
        def prepare(text):
            if text.startswith('本轮未能完整完成'):
                raise TtsNetworkError()
        trace, _ = self.dialogue(['你好', '退出语音助手'], [ValueError('invalid')], on_prepare=prepare)
        self.assertEqual(sum(event == ('tts', NETWORK_ERROR_PROMPT) for event in trace), 1)

    def test_non_network_provider_error_retains_generic_result(self):
        trace, _ = self.dialogue([AsrError('ASR HTTP 401'), '退出语音助手'], [])
        self.assertFalse(any(event == ('tts', NETWORK_ERROR_PROMPT) for event in trace))
        self.assertTrue(any(event[0] == 'tts' and event[1].startswith('本轮未能完整完成') for event in trace))

    def test_skill_network_result_stops_cloud_return_and_only_announces_once(self):
        result = {'status': 'failed', 'message': NETWORK_ERROR_PROMPT, 'error_code': 'network_unavailable'}
        with patch('primebuddy.voice.session.continue_plan') as continuation:
            trace, _ = self.dialogue(['播放小星星', '退出语音助手'], [self.proposal()], result=result)
        continuation.assert_not_called()
        self.assertEqual(sum(event == ('tts', NETWORK_ERROR_PROMPT) for event in trace), 1)
        self.assertNotIn(('tts', '接下来，你还想让我帮你做些什么呢？'), trace)

    def test_failed_offline_notice_waits_for_native_and_accepts_next_turn(self):
        prepared = []
        def prepare(text):
            prepared.append(text)
            if text == NETWORK_ERROR_PROMPT:
                raise AudioError('本地提示音无法播放')
        trace, _ = self.dialogue([AsrNetworkError(), '退出语音助手'], [], on_prepare=prepare)
        self.assertEqual(prepared.count(NETWORK_ERROR_PROMPT), 1)
        self.assertEqual(sum(event[0] == 'waiting' for event in trace), 2)
        self.assertIn(('tts', '语音助手已退出。'), trace)

    def test_failed_recovery_prompt_keeps_host_available(self):
        def play(text):
            if text.startswith(('好的，', '收到，', '明白，', '本轮未能完整完成')):
                raise AudioError('原厂音频被打断')
        trace, _ = self.dialogue(['播放小星星', '你好', '退出语音助手'],
                                [self.proposal(), {'reply': '你好。', 'tasks': []}], on_play=play)
        self.assertEqual(sum(event[0] == 'waiting' for event in trace), 2)
        self.assertIn(('tts', '你好。'), trace)
        self.assertIn(('tts', '语音助手已退出。'), trace)

    def test_failed_recovery_prompt_with_unconfirmed_audio_does_not_resume(self):
        def play(text):
            if text.startswith('本轮未能完整完成'):
                raise AudioError('停止未确认')
        idle = Mock(side_effect=[None, CleanupUnconfirmed('停止未确认')])
        trace = []
        with self.assertRaises(CleanupUnconfirmed):
            self.dialogue(['你好'], [ValueError('invalid')], trace=trace,
                          on_play=play, ensure_audio_idle=idle)
        self.assertEqual(trace.count(('waiting', 'waiting')), 1)

    def test_global_exit_during_recovery_prompt_still_exits(self):
        stopped = []
        def play(text):
            if text.startswith('本轮未能完整完成'):
                stopped.append(True)
                raise AudioError('收到退出信号')
        def check():
            if stopped:
                raise AudioError('收到退出信号')
        trace = []
        with self.assertRaisesRegex(AudioError, '退出信号'):
            self.dialogue(['你好'], [ValueError('invalid')], trace=trace, on_play=play, cancel_check=check)
        self.assertEqual(trace.count(('waiting', 'waiting')), 1)

    def test_exit_signal_received_during_asr_prevents_llm_and_more_tts(self):
        import threading
        stopped = threading.Event()
        prepared, trace = [], []
        def check():
            if stopped.is_set():
                raise AudioError('收到退出信号')
        with self.assertRaisesRegex(AudioError, '退出信号'):
            self.dialogue(['你好'], [], trace=trace, on_asr=stopped.set,
                          on_prepare=prepared.append, cancel_check=check)
        self.assertFalse(any(event[0] == 'planning_history' for event in trace))
        self.assertEqual(prepared, ['PrimeBuddy 已启动，你想做什么？'])

    def test_exit_signal_after_llm_prevents_cloud_tts_preparation(self):
        import threading
        stopped = threading.Event()
        prepared = []
        def check():
            if stopped.is_set():
                raise AudioError('收到退出信号')
        with self.assertRaisesRegex(AudioError, '退出信号'):
            self.dialogue(['你好'], [{'reply': '你好，请说。', 'tasks': []}], on_plan=stopped.set,
                          on_prepare=prepared.append, cancel_check=check)
        self.assertEqual(prepared, ['PrimeBuddy 已启动，你想做什么？'])

    def test_exit_signal_before_llm_entry_prevents_planning(self):
        import threading
        stopped = threading.Event()
        trace = []
        bridge = Mock()
        bridge.topic, bridge.service_name = '/primebuddy/asr_result', '/primebuddy/PlayTts'
        bridge.publish_asr.side_effect = lambda _: stopped.set()
        def check():
            if stopped.is_set():
                raise AudioError('收到退出信号')
        with self.assertRaisesRegex(AudioError, '退出信号'):
            self.dialogue(['你好'], [], bridge=bridge, trace=trace, cancel_check=check)
        self.assertFalse(any(event[0] == 'planning_history' for event in trace))

    def test_startup_resource_check_precedes_any_tts(self):
        trace, _ = self.dialogue(['退出语音助手'], [])
        self.assertLess(trace.index(('prepare_voice', True)),
                        trace.index(('tts', 'PrimeBuddy 已启动，你想做什么？')))

    def test_startup_unreleased_resource_blocks_all_tts(self):
        trace = []
        with self.assertRaises(CleanupUnconfirmed):
            self.dialogue([], [], trace=trace, prepare_error=CleanupUnconfirmed('旧播放未确认停止'))
        self.assertEqual(trace, [('prepare_voice', True)])

    def test_startup_waits_for_native_listening_without_self_opening(self):
        trace = []
        def waiting(microphone, events):
            microphone.on_state(SimpleNamespace(main_state=SimpleNamespace(value=0)))
            self.assertEqual(microphone.phase, 'waiting')
            self.assertFalse(any(event[0] in ('listen', 'asr') for event in events))
            for state in (1, 3):
                microphone.on_audio(SimpleNamespace(stream_id=0, audio_vad_state=SimpleNamespace(value=state),
                    header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=2)), audio_data=b'\0' * 6400))
            self.assertTrue(microphone.events.empty())
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.dialogue([], [], trace=trace, on_wait=waiting)
        self.assertIn(('tts', 'PrimeBuddy 已启动，你想做什么？'), trace)
        self.assertFalse(any(event[0] in ('listen', 'asr') for event in trace))

    def test_cleanup_checkpoint_pumps_ros_without_asr_tts_or_new_tasks(self):
        callbacks = []
        trace = []
        def executor(proposal, registry, context, announce, checkpoint, microphone, trace, cleanup_checkpoint):
            context.cancelled.set()
            def packet(state):
                def callback():
                    self.assertTrue(context.cancelled.is_set())
                    self.assertEqual(microphone.phase, 'muted')
                    microphone.on_audio(SimpleNamespace(stream_id=0, audio_vad_state=SimpleNamespace(value=state),
                        header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=2)), audio_data=b'\0' * 6400))
                    trace.append(('cleanup_vad', state))
                return callback
            callbacks.extend([packet(1), packet(3)])
            trace.append(('cleanup', 'start'))
            cleanup_checkpoint()
            cleanup_checkpoint()
            self.assertTrue(microphone.events.empty())
            self.assertTrue(microphone.interrupts.empty())
            trace.append(('cleanup', 'complete'))
            raise CleanupUnconfirmed('设备状态未确认')
        with self.assertRaises(CleanupUnconfirmed):
            self.dialogue(['播放小星星'], [self.proposal()], executor=executor,
                          trace=trace, ros_callbacks=callbacks)
        self.assertEqual([event for event in trace if event[0] == 'cleanup_vad'],
                         [('cleanup_vad', 1), ('cleanup_vad', 3)])
        self.assertEqual([event for event in trace if event[0] == 'tts'],
                         [('tts', 'PrimeBuddy 已启动，你想做什么？')])
        self.assertEqual([event for event in trace if event[0] == 'asr'], [('asr', '播放小星星')])

    def test_ordinary_vad_text_does_not_cancel_active_task(self):
        def executor(proposal, registry, context, announce, checkpoint, microphone, trace, cleanup_checkpoint):
            microphone.conversation_state = 3
            microphone.on_state(SimpleNamespace(main_state=SimpleNamespace(value=1)))
            for state in (1, 3):
                microphone.on_audio(SimpleNamespace(stream_id=0, audio_vad_state=SimpleNamespace(value=state),
                    header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=2)), audio_data=b'\0' * 6400))
            checkpoint()
            trace.append(('continued', context.cancelled.is_set()))
            return {}
        trace, calls = self.dialogue(['播放小星星', '退出语音助手'], [self.proposal()],
                                     executor=executor)
        self.assertIn(('continued', False), trace)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(event == ('tts', '接下来，你还想让我帮你做些什么呢？') for event in trace), 1)

    def test_independent_music_completion_reopens_two_rounds_without_wake(self):
        proposal = {'reply': '', 'tasks': [{'id': 'music', 'skill': 'diangetai', 'action': 'play',
                    'args': {'artist': '', 'song': '小星星'}, 'after': []}]}
        trace, calls = self.dialogue(['播放小星星', '再播放一首', '退出语音助手'], [proposal, proposal],
                    result={'status': 'success', 'message': '歌曲播放完毕。'}, tool='diangetai')
        self.assertEqual(len([event for event in trace if event[0] == 'execute']), 2)
        self.assertEqual([event[1] for event in trace if event[0] == 'asr'],
                         ['播放小星星', '再播放一首', '退出语音助手'])
        self.assertEqual(len([event for event in trace if event == ('tts', '接下来，你还想让我帮你做些什么呢？')]), 2)

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

    def test_business_text_and_quoted_content_reach_planner_unchanged(self):
        texts = ['元仔元仔，打开天际线灯', 'Q仔Q仔，播放音乐', '播放歌曲《你好。Q仔Q仔》',
                 '他说“你好。元仔元仔”。', '他在想听谁？袁仔，袁仔。']
        for text in texts:
            with self.subTest(text=text):
                bridge = Mock()
                bridge.topic, bridge.service_name = '/primebuddy/asr_result', '/primebuddy/PlayTts'
                trace, calls = self.dialogue([text, '退出语音助手'],
                    [{'reply': '请继续。', 'tasks': []}], bridge=bridge)
                self.assertEqual(calls[0].args[0], text)
                self.assertEqual(bridge.publish_asr.call_args_list[0].args[0], text)

    def test_business_text_does_not_clear_clarification_history(self):
        text = '袁仔，袁仔，打开天际线灯'
        trace, calls = self.dialogue(['打开灯', text, '退出语音助手'],
            [{'reply': '要打开哪盏灯？', 'tasks': []}, self.proposal()])
        self.assertEqual(calls[1].args[0], text)
        history = [value for key, value in trace if key == 'planning_history'][1]
        self.assertEqual(history[0]['content'], '打开灯')
        self.assertIn('要打开哪盏灯', history[1]['content'])

    def test_asr_vocabulary_has_only_skill_supplied_business_words(self):
        words = []
        self.dialogue(['你好', '退出语音助手'], [{'reply': '你好。', 'tasks': []}],
                      asr_vocabularies=words, robot_series='Q1')
        self.assertEqual(words, [[], []])
        self.assertEqual(vocabulary({'business': {'status': 'success', 'vocabulary': ['餐桌灯']}}), ['餐桌灯'])

    def test_unverified_result_never_announces_success(self):
        trace, _ = self.dialogue(['打开天际线灯', '退出语音助手'], [self.proposal()],
            {'status': 'failed', 'message': '灯控请求已发送，但目标状态尚未确认，请检查灯具。'})
        self.assertFalse(any(k == 'tts' and '已打开' in v for k, v in trace))

    def agent_messages(self, final='还想做什么？'):
        def tool(name, args, identifier):
            return {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
        return [tool('read_skill_file', {'skill': 'yeelight', 'path': 'SKILL.md'}, 'read'),
                tool('get_skill_context', {'skill': 'yeelight'}, 'context'),
                tool('execute_tasks', {'tasks': self.proposal()['tasks']}, 'execute'),
                {'role': 'assistant', 'content': final}]

    def test_agent_query_confirmation_and_actual_feedback_share_voice_session(self):
        trace, calls = self.dialogue(['打开天气限灯', '确认', '退出语音助手'], [], agent_messages=self.agent_messages())
        execute_at = next(i for i, item in enumerate(trace) if item[0] == 'execute')
        self.assertLess(trace.index(('asr', '确认')), execute_at)
        self.assertEqual(len(calls), 4)
        self.assertIn(('tts', '天际线灯已打开。'), trace[execute_at + 1:])
        self.assertEqual(calls[-1].args[0][-1]['role'], 'tool')

    def test_agent_summary_cannot_override_unverified_business_feedback(self):
        trace, _ = self.dialogue(['打开天际线灯', '退出语音助手'], [],
            {'status': 'failed', 'message': '灯的实际状态尚未确认。'},
            agent_messages=self.agent_messages(final='天际线灯已打开。'))
        self.assertIn(('tts', '灯的实际状态尚未确认。'), trace)
        self.assertFalse(any(k == 'tts' and '已打开' in v for k, v in trace))


CONFIG = {'endpoint': 'https://example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions',
          'api_key': 'test-only', 'model': MODEL}


class AsrTests(unittest.TestCase):
    def test_new_model_uses_native_endpoint_and_real_name_vocabulary(self):
        url, body = request_payload('data:audio/wav;base64,AA==', CONFIG, NEW_MODEL,
                                    ['餐桌灯', '天际线灯', '天际线灯', '长' * 16])
        self.assertTrue(url.endswith('/api/v1/services/aigc/multimodal-generation/generation'))
        self.assertEqual(body['parameters']['vocabulary'], {'餐桌灯': 4, '天际线灯': 4})
        self.assertEqual(body['input']['messages'][-1]['content'][0]['type'], 'input_audio')
        self.assertLessEqual(len(body['input']['messages'][0]['content'][0]['text']), 400)

    def test_old_model_keeps_compatible_contract(self):
        url, body = request_payload('audio', CONFIG, MODEL, ['天际线灯'])
        self.assertEqual(url, CONFIG['endpoint'])
        self.assertEqual(body['model'], MODEL)
        self.assertNotIn('parameters', body)

    def test_new_response_text_is_parsed(self):
        response = Mock()
        response.read.return_value = json.dumps({'output': {'text': '关闭天际线灯。'}})
        with patch('primebuddy.providers.qianwen.asr.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value = response
            self.assertEqual(recognize('audio', CONFIG, NEW_MODEL), '关闭天际线灯。')

    def test_model_permission_failure_does_not_fallback_or_retry(self):
        with patch('primebuddy.providers.qianwen.asr.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError('url', 403, 'blocked', {}, None)
            with self.assertRaises(AsrError):
                recognize('audio', CONFIG, NEW_MODEL)
            opener.return_value.open.assert_called_once()


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.registry = VoiceRegistry()
        self.guard = self.registry.load('yeelight').confirmation
        self.guard.side_effect = None
        self.guard.return_value = None
        self.snapshot = {'yeelight': {'status': 'success', 'rooms': ['客厅'], 'vocabulary': ['客厅', '天际线灯'],
                                     'devices': [{'name': '天际线灯', 'room': '客厅'}]}}
        self.plan = {'reply': '', 'tasks': [{'id': 'light', 'skill': 'yeelight', 'action': 'set_power',
            'args': {'power': False, 'scope': 'device', 'room': '', 'device': '天际线灯'}, 'after': []}]}

    def test_exact_name_does_not_need_extra_confirmation(self):
        self.assertIsNone(confirmation_for(self.plan, self.registry, '关闭天际线灯', self.snapshot))
        self.guard.assert_called_once_with('set_power', self.plan['tasks'][0]['args'],
                                           '关闭天际线灯', self.snapshot['yeelight'])

    def test_phonetic_candidate_requires_confirmation(self):
        self.guard.return_value = '关闭天际线灯'
        question = confirmation_for(self.plan, self.registry, '关闭天气限灯', self.snapshot)
        self.assertIn('关闭天际线灯', question)
        self.assertIn('确认', question)

    def test_skill_target_validation_failure_is_not_silently_confirmed(self):
        for reason in ('目标不存在', '目标重名'):
            with self.subTest(reason=reason):
                self.guard.side_effect = ValueError(reason)
                with self.assertRaisesRegex(ValueError, reason):
                    confirmation_for(self.plan, self.registry, '关闭天际线灯', self.snapshot)

    def test_full_plan_is_named_in_confirmation(self):
        self.guard.return_value = '关闭天际线灯'
        self.plan['tasks'].append({'id': 'music', 'skill': 'diangetai', 'action': 'play',
                                  'args': {'artist': '周杰伦', 'song': ''}, 'after': []})
        question = confirmation_for(self.plan, self.registry, '关闭天气限灯并播放周杰伦', self.snapshot)
        self.assertIn('周杰伦', question)

    def test_cli_preview_target_is_kept_even_when_adapter_description_is_generic(self):
        self.guard.return_value = '请确认：关闭客厅的天际线灯？'
        self.registry.load('yeelight').describe = Mock(return_value='正在控制灯光')
        self.plan['tasks'].append({'id': 'music', 'skill': 'diangetai', 'action': 'play',
                                  'args': {'artist': '周杰伦', 'song': ''}, 'after': []})
        question = confirmation_for(self.plan, self.registry, '关闭天气限灯并播放周杰伦', self.snapshot)
        self.assertIn('关闭客厅的天际线灯', question)
        self.assertIn('播放周杰伦', question)
        self.assertEqual(question.count('请确认'), 1)
        self.registry.load('yeelight').describe.assert_not_called()

    def test_no_confirmation_does_not_run_description_queries(self):
        description = self.registry.load('yeelight').describe = Mock()
        self.assertIsNone(confirmation_for(self.plan, self.registry, '关闭天际线灯', self.snapshot))
        description.assert_not_called()

    def test_confirmation_accepts_only_explicit_answers(self):
        self.assertIs(confirmation_answer('确认。'), True)
        self.assertIs(confirmation_answer('不是'), False)
        self.assertIsNone(confirmation_answer('是的，但是换成厨房灯'))

    def test_hotwords_come_from_actual_inventory(self):
        self.assertIn('天际线灯', vocabulary(self.snapshot))
        self.assertNotIn('天气限灯', vocabulary(self.snapshot))
