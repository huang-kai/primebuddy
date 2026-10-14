"""语音会话、唤醒文字、名称确认与识别协议。"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from primebuddy.voice.session import wake_command
from primebuddy.integrations.t1.native_voice import NativeVoiceInput
import contextlib
import copy
from pathlib import Path
from primebuddy.core.engine import Context, execute
from primebuddy.skills.registry import Registry
from primebuddy.integrations.t1.robot_audio import RobotAudio, AudioError
import json
from unittest.mock import Mock, patch
import urllib.error
from primebuddy.providers.qianwen.asr import request_payload, recognize, MODEL, NEW_MODEL, AsrError
from primebuddy.core.grounding import confirmation_for, confirmation_answer, vocabulary


class VoiceTests(unittest.TestCase):
    def microphone(self):
        clock = SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=1))
        node = SimpleNamespace(get_clock=lambda: clock)
        return NativeVoiceInput(None, SimpleNamespace(node=node))

    def test_listening_window_is_thirty_seconds(self):
        mic = self.microphone()
        with patch('primebuddy.integrations.t1.native_voice.time.monotonic', return_value=100):
            mic.listen(0)
        self.assertEqual(mic.deadline, 130)
        with patch('primebuddy.integrations.t1.native_voice.time.monotonic', return_value=111):
            mic.check_health()
        self.assertEqual(mic.phase, 'listening')

    def test_wake_transition_emits_one_event(self):
        mic = self.microphone()
        mic.wait_for_wake(0)
        mic.conversation_state = 0
        msg = SimpleNamespace(main_state=SimpleNamespace(value=1))
        mic.on_state(msg)
        mic.on_state(msg)
        self.assertEqual(mic.events.qsize(), 1)
        self.assertEqual(mic.events.get()[1], 'wake')

    def test_wake_filter_keeps_real_commands(self):
        for text in ('元仔，元仔。', '源崽源崽', '元仔', '源崽。'):
            self.assertEqual(wake_command(text), (True, ''))
        for text in ('元仔，关闭餐厅灯。', '人在，人在。'):
            self.assertEqual(wake_command(text), (False, text))

    def test_observed_repeated_wake_homophones_skip_planner(self):
        for text in ('冤哉冤哉。', '原在，原在。', '袁仔，袁仔。', '元仔袁仔'):
            self.assertEqual(wake_command(text), (True, ''))
        self.assertEqual(wake_command('原在'), (False, '原在'))
        self.assertEqual(wake_command('原在原在，打开天际线灯。'), (True, '打开天际线灯'))

    def test_wake_marks_new_instruction_boundary(self):
        self.assertEqual(wake_command('元仔元仔播放周杰伦的歌'), (True, '播放周杰伦的歌'))
        self.assertEqual(wake_command('他在想听谁？袁仔，袁仔。'), (True, ''))
        self.assertEqual(wake_command('元仔元仔。旧话。元仔元仔，播放晴天'), (True, '播放晴天'))

    def test_song_names_and_single_homophones_are_not_wake_controls(self):
        for text in ('袁仔', '播放袁仔的歌', '播放歌曲《元仔元仔》', '播放原在原在',
                     '播放袁仔袁仔的歌', '播放歌曲《元仔元仔。晴天》',
                     '播放歌曲《你好。元仔元仔》', '他说“你好。元仔元仔”。'):
            self.assertEqual(wake_command(text), (False, text))

    def test_no_frontend_audio_is_not_reported_as_user_silence(self):
        mic = self.microphone()
        mic.listen(0)
        mic.timeout()
        _, kind, reason = mic.events.get_nowait()
        self.assertEqual(kind, 'timeout')
        self.assertIn('未收到原厂音频', reason)

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
        with patch.object(robot, 'prepared_audio', return_value=contextlib.nullcontext('remote.wav')),\
                patch.object(robot, 'play_file', side_effect=AudioError('failed')):
            with self.assertRaises(AudioError):
                robot.play('question.wav', on_finished=lambda: opened.append(True))
        self.assertEqual(opened, [])

    def dialogue(self, inputs, proposals, result=None, bridge=None, agent_messages=None):
        import tempfile
        from primebuddy.voice.session import serve
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
        def asr(*args, **kwargs):
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
                'primebuddy.integrations.t1.native_voice.NativeVoiceInput': Microphone,
                'primebuddy.voice.audio.AudioOutput': lambda *_: SimpleNamespace(play=lambda text, **kw: play(text, on_finished=kw.get('on_finished')), close=lambda: None),
                'primebuddy.integrations.t1.native_agent.voice_only_agent': lambda _: contextlib.nullcontext(),
                'primebuddy.integrations.t1.robot_audio.audio_session': lambda _: contextlib.nullcontext(robot),
                'primebuddy.integrations.t1.robot_audio.connect_soc0': lambda _: None,
                'primebuddy.integrations.t1.robot_audio.check_cancel': lambda: None,
                'primebuddy.integrations.t1.speech_audio.native_preflight': lambda: None,
                'primebuddy.integrations.t1.speech_audio.encode_asr_wav': lambda *a, **k: b'wav',
                'primebuddy.config.models.load_model': lambda *args: {'provider': 'qianwen', 'model': 'test-only'},
                'primebuddy.providers.transcribe_wav': asr,
                'primebuddy.config.runtime.private_runtime': lambda: Path(directory),
                'primebuddy.voice.session.SpeechCache': lambda _: SimpleNamespace(prepare=lambda text: text),
            }
            for name, value in patches.items():
                stack.enter_context(patch(name, value))
            if bridge:
                stack.enter_context(patch('primebuddy.integrations.t1.sdk_bridge.SdkBridge', return_value=bridge))
            stack.enter_context(patch.object(registry, 'planning_context', return_value=snapshot))
            stack.enter_context(patch.object(registry.load('yeelight'), 'run', side_effect=run))
            plans = iter(proposals)
            def propose(*args, **kwargs):
                trace.append(('planning_history', copy.deepcopy(args[3])))
                return next(plans)
            if agent_messages is None:
                planner = stack.enter_context(patch('primebuddy.voice.session.plan', side_effect=propose))
            else:
                messages = iter(agent_messages)
                planner = stack.enter_context(patch('primebuddy.core.planner.chat', side_effect=lambda *args: next(messages)))
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

    def test_logged_wake_homophone_skips_planner_and_clears_clarification(self):
        from unittest.mock import Mock
        bridge = Mock()
        bridge.topic, bridge.service_name = '/primebuddy/asr_result', '/primebuddy/PlayTts'
        trace, calls = self.dialogue(
            ['打开灯', '他在想听谁？袁仔，袁仔。', '打开天际线灯', '退出语音助手'],
            [{'reply': '要打开哪盏灯？', 'tasks': []}, self.proposal()], bridge=bridge)
        self.assertEqual([call.args[0] for call in calls], ['打开灯', '打开天际线灯'])
        self.assertEqual([v for k, v in trace if k == 'planning_history'][1], [])
        self.assertNotIn('他在想听谁？袁仔，袁仔。',
                         [call.args[0] for call in bridge.publish_asr.call_args_list])
        self.assertEqual(sum(k == 'execute' for k, _ in trace), 1)

    def test_new_wake_discards_old_confirmation(self):
        trace, calls = self.dialogue(
            ['打开天气限灯', '元仔元仔', '确认', '退出语音助手'],
            [self.proposal(), {'reply': '请说要做什么。', 'tasks': []}])
        self.assertFalse(any(k == 'execute' for k, _ in trace))
        self.assertEqual([call.args[0] for call in calls], ['打开天气限灯', '确认'])
        self.assertEqual([v for k, v in trace if k == 'planning_history'][1], [])

    def test_wake_and_instruction_in_one_segment_keeps_only_new_instruction(self):
        from unittest.mock import Mock
        bridge = Mock()
        bridge.topic, bridge.service_name = '/primebuddy/asr_result', '/primebuddy/PlayTts'
        trace, calls = self.dialogue(
            ['打开灯', '袁仔，袁仔，打开天际线灯', '退出语音助手'],
            [{'reply': '要打开哪盏灯？', 'tasks': []}, self.proposal()], bridge=bridge)
        self.assertEqual(calls[1].args[0], '打开天际线灯')
        self.assertEqual([v for k, v in trace if k == 'planning_history'][1], [])
        self.assertEqual(bridge.publish_asr.call_args_list[1].args[0], '打开天际线灯')
        self.assertEqual(sum(k == 'execute' for k, _ in trace), 1)

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
                                    ['元仔', '天际线灯', '天际线灯', '长' * 16])
        self.assertTrue(url.endswith('/api/v1/services/aigc/multimodal-generation/generation'))
        self.assertEqual(body['parameters']['vocabulary'], {'元仔': 4, '天际线灯': 4})
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
        self.registry = Registry(Path(__file__).resolve().parents[2] / 'primebuddy-skills')
        self.snapshot = {'yeelight': {'status': 'success', 'rooms': ['客厅'], 'vocabulary': ['客厅', '天际线灯'],
                                     'devices': [{'name': '天际线灯', 'room': '客厅'}]}}
        self.plan = {'reply': '', 'tasks': [{'id': 'light', 'skill': 'yeelight', 'action': 'set_power',
            'args': {'power': False, 'scope': 'device', 'room': '', 'device': '天际线灯'}, 'after': []}]}

    def test_exact_name_does_not_need_extra_confirmation(self):
        self.assertIsNone(confirmation_for(self.plan, self.registry, '关闭天际线灯', self.snapshot))

    def test_phonetic_candidate_requires_confirmation(self):
        question = confirmation_for(self.plan, self.registry, '关闭天气限灯', self.snapshot)
        self.assertIn('关闭天际线灯', question)
        self.assertIn('确认', question)

    def test_unknown_candidate_and_duplicate_names_are_blocked(self):
        self.plan['tasks'][0]['args']['device'] = '不存在的灯'
        with self.assertRaises(ValueError):
            confirmation_for(self.plan, self.registry, '关闭不存在的灯', self.snapshot)
        self.plan['tasks'][0]['args']['device'] = '天际线灯'
        self.snapshot['yeelight']['devices'].append({'name': '天际线灯', 'room': '厨房'})
        with self.assertRaises(ValueError):
            confirmation_for(self.plan, self.registry, '关闭天际线灯', self.snapshot)

    def test_full_plan_is_named_in_confirmation(self):
        self.plan['tasks'].append({'id': 'music', 'skill': 'diangetai', 'action': 'play',
                                  'args': {'artist': '周杰伦', 'song': ''}, 'after': []})
        question = confirmation_for(self.plan, self.registry, '关闭天气限灯并播放周杰伦', self.snapshot)
        self.assertIn('周杰伦', question)

    def test_confirmation_accepts_only_explicit_answers(self):
        self.assertIs(confirmation_answer('确认。'), True)
        self.assertIs(confirmation_answer('不是'), False)
        self.assertIsNone(confirmation_answer('是的，但是换成厨房灯'))

    def test_hotwords_come_from_actual_inventory(self):
        self.assertIn('天际线灯', vocabulary(self.snapshot))
        self.assertNotIn('天气限灯', vocabulary(self.snapshot))
