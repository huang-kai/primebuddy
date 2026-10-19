"""T1 会话接管恢复与 SDK 兼容入口。"""
import contextlib
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from primebuddy.integrations.primebot import native_agent as agent
from primebuddy.integrations.primebot.robot_audio import AudioError, audio_session
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from primebuddy.integrations.primebot.sdk_bridge import ENDPOINTS, SdkBridge, sdk_types
from primebuddy.config.storage import SetupError
from primebuddy.integrations.primebot.native_voice import NativeVoiceInput
from primebuddy.providers.errors import TtsNetworkError


class AgentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.record = self.runtime / 'agent_mode.json'
        self.mode = 'only_voice'
        self.running = True
        self.calls = []
        self.fail_restart = False
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, impl in [('agent_backend', lambda _: 'http'), ('agent_target', lambda: {'series': 'T1', 'sku': 'T1-2X', 'host': 'local'}), ('agent_rpc', self.rpc),
                           ('agent_mode', self.read_write), ('restart_agent', self.restart),
                           ('confirm_agent', self.confirm)]:
            self.stack.enter_context(patch.object(agent, name, side_effect=impl))
        self.stack.enter_context(patch.object(agent, 'progress'))

    def save(self, previous='normal', **changes):
        data = dict(previous_running=True, previous_mode=previous,
                    purpose='music_session', backend='http', target={'series': 'T1', 'sku': 'T1-2X', 'host': 'local'})
        data.update(changes)
        self.record.write_text(json.dumps(data))
        return self.record.read_bytes()

    def rpc(self, runtime, operation, backend):
        self.calls.append(('rpc', operation))
        if operation == 'start':
            self.assertTrue(self.record.exists())
            self.running = True
        return {'status': 'Running(123)' if self.running else 'Idel(-1)',
                'pid': 123 if self.running else -1}

    def confirm(self, runtime, running, backend):
        self.assertEqual(self.running, running)

    def read_write(self, runtime, value=None):
        if value is not None:
            self.assertTrue(self.record.exists(), '改模式前必须保留恢复目标')
            self.calls.append(('mode', value))
            self.mode = value
        return self.mode

    def restart(self, runtime, backend):
        self.assertTrue(self.record.exists(), '重启中断时仍须能恢复')
        self.calls.append(('restart', self.mode))
        if self.fail_restart:
            raise AudioError('连接中断')

    def test_interrupted_session_takes_over_directly_and_preserves_original(self):
        original = self.save()
        with agent.voice_only_agent(self.runtime):
            self.assertEqual(self.mode, 'only_voice')
            self.assertEqual(self.record.read_bytes(), original)
            self.assertEqual([c for c in self.calls if c[0] == 'mode'],
                             [('mode', 'only_voice')])
            self.assertEqual([c for c in self.calls if c[0] == 'restart'],
                             [('restart', 'only_voice')])
        self.assertEqual(self.mode, 'normal')
        self.assertFalse(self.record.exists())

    def test_stopped_agent_with_record_is_started_before_takeover(self):
        self.save()
        self.running = False
        with agent.voice_only_agent(self.runtime):
            self.assertIn(('rpc', 'start'), self.calls)

    def test_original_only_voice_is_preserved(self):
        self.save(previous='only_voice')
        with agent.voice_only_agent(self.runtime):
            pass
        self.assertEqual(self.mode, 'only_voice')
        self.assertFalse(self.record.exists())

    def test_repeated_restart_failure_preserves_exact_record_and_can_retry(self):
        original = self.save()
        self.fail_restart = True
        with self.assertRaisesRegex(AudioError, '恢复.*记录.*保留'):
            with agent.voice_only_agent(self.runtime):
                self.fail('重启失败不应进入业务')
        self.assertEqual(self.record.read_bytes(), original)
        self.fail_restart = False
        with agent.voice_only_agent(self.runtime):
            self.assertEqual(self.mode, 'only_voice')
        self.assertFalse(self.record.exists())

    def test_mode_readback_mismatch_preserves_record(self):
        original = self.save()
        with patch.object(agent, 'agent_mode', return_value='only_voice'):
            with self.assertRaisesRegex(AudioError, '恢复'):
                with agent.voice_only_agent(self.runtime):
                    pass
        self.assertEqual(self.record.read_bytes(), original)

    def test_takeover_readback_mismatch_does_not_enter_business(self):
        self.save()
        with patch.object(agent, 'agent_mode', return_value='normal'):
            with self.assertRaisesRegex(AudioError, '未确认 only_voice'):
                with agent.voice_only_agent(self.runtime):
                    self.fail('接管模式未确认时不能进入业务')
        self.assertFalse(self.record.exists(), '已经确认回到原模式')

    def test_reentry_after_partial_restore_still_skips_normal_mode_write(self):
        self.save()
        self.mode = 'normal'
        with agent.voice_only_agent(self.runtime):
            self.assertEqual([c for c in self.calls if c[0] == 'mode'], [('mode', 'only_voice')])

    def test_another_live_session_blocks_reentry_before_agent_changes(self):
        original = self.save()
        with (self.runtime / 'audio.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(AudioError, '已有.*任务运行'):
                with audio_session(self.runtime):
                    with agent.voice_only_agent(self.runtime):
                        self.fail('已有会话时不能接管')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record.read_bytes(), original)

    def test_invalid_records_never_change_agent(self):
        for changes in ({'previous_mode': 'unknown'}, {'previous_running': False},
                        {'purpose': 'unknown'}, {'backend': 'aima'}, {'target': {'series': 'Q1'}}):
            with self.subTest(changes=changes):
                original = self.save(**changes)
                with self.assertRaises(AudioError):
                    with agent.voice_only_agent(self.runtime):
                        self.fail('非法记录不能接管')
                self.assertEqual(self.record.read_bytes(), original)
                self.assertEqual(self.calls, [])
        for content in ('broken json', '[]'):
            self.record.write_text(content)
            with self.assertRaises(AudioError):
                with agent.voice_only_agent(self.runtime):
                    self.fail('损坏记录不能接管')
            self.assertEqual(self.record.read_text(), content)

    def test_legacy_probe_record_is_not_deleted_or_interpreted(self):
        legacy = self.runtime / 'agent_voice_probe.json'
        legacy.write_text('{}')
        with self.assertRaisesRegex(AudioError, 'agent_voice_probe.json'):
            with agent.voice_only_agent(self.runtime):
                self.fail('其他程序记录不能套用当前恢复协议')
        self.assertTrue(legacy.exists())
        self.assertEqual(self.calls, [])

    def test_normal_start_and_business_exception_restore_mode(self):
        self.mode = 'normal'
        with self.assertRaisesRegex(ValueError, 'business'):
            with agent.voice_only_agent(self.runtime):
                raise ValueError('business')
        self.assertEqual(self.mode, 'normal')
        self.assertFalse(self.record.exists())

    def test_failed_exit_cleanup_is_recovered_on_next_start(self):
        self.mode = 'normal'
        with self.assertRaises(AudioError):
            with agent.voice_only_agent(self.runtime):
                self.fail_restart = True
        self.assertTrue(self.record.exists())
        self.fail_restart = False
        with agent.voice_only_agent(self.runtime):
            self.assertEqual(json.loads(self.record.read_text())['previous_mode'], 'normal')
        self.assertFalse(self.record.exists())


SDK = Path(__file__).resolve().parents[2] / 'sdk_t1-v1.0.0.0/aimdk_msgs'


def message_class(name, definitions, cache):
    if name in cache:
        return cache[name]
    fields, constants = {}, {}
    for line in definitions[name].splitlines():
        declaration = line.split('#', 1)[0].strip()
        if not declaration:
            continue
        kind, field = declaration.split(maxsplit=1)
        if '=' in field:
            key, value = field.split('=', 1)
            constants[key.strip()] = int(value.strip(), 0)
            continue
        if kind == 'string':
            factory = str
        elif kind == 'bool':
            factory = bool
        elif kind.startswith('float'):
            factory = float
        elif kind.startswith(('uint', 'int')):
            factory = int
        elif kind == 'builtin_interfaces/Time':
            factory = lambda: NS(sec=0, nanosec=0)
        else:
            factory = message_class(kind, definitions, cache)
        fields[field] = factory
    def init(self):
        for key, factory in fields.items():
            setattr(self, key, factory())
    result = type(name, (), dict(constants, __slots__=tuple(fields), __init__=init))
    cache[name] = result
    return result


@unittest.skipUnless(SDK.is_dir(), '需要工作区附带的 SDK 定义，测试不依赖 ROS 安装')
class BridgeTests(unittest.TestCase):
    def setUp(self):
        definitions = {p.stem: p.read_text() for p in SDK.rglob('*.msg')}
        request, response = (SDK / 'interaction/srv/PlayTts.srv').read_text().split('\n---\n')
        definitions.update(Request=request, Response=response)
        cache = {}
        self.asr = message_class('AsrResult', definitions, cache)
        self.state = message_class('CommonState', definitions, cache)
        self.tts = NS(Request=message_class('Request', definitions, cache),
                      Response=message_class('Response', definitions, cache))
        modules = {
            'aimdk_msgs': NS(),
            'aimdk_msgs.msg': NS(AsrResult=self.asr, CommonState=self.state),
            'aimdk_msgs.srv': NS(PlayTts=self.tts),
            'rclpy': NS(),
            'rclpy.qos': NS(QoSProfile=lambda **kw: NS(**kw),
                ReliabilityPolicy=NS(RELIABLE='reliable'), DurabilityPolicy=NS(VOLATILE='volatile')),
        }
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.node = Mock()
        self.node.get_name.return_value = 'primebuddy_test'
        self.node.get_namespace.return_value = '/'
        self.node.get_publishers_info_by_topic.return_value = []
        self.node.get_node_names_and_namespaces.return_value = [('primebuddy_test', '/')]
        self.node.get_clock.return_value.now.return_value = NS(to_msg=lambda: NS(sec=123, nanosec=0), nanoseconds=123)
        self.bridge = SdkBridge(self.node, 'namespaced')
        self.addCleanup(self.bridge.close)

    def request(self, text='你好'):
        request = self.tts.Request()
        request.tts_req.text, request.tts_req.domain = text, 'test'
        request.tts_req.trace_id = 'test-trace'
        request.tts_req.priority_level.value = 6
        request.tts_req.is_interrupted = True
        return request

    def submit(self, request=None):
        return self.bridge.on_tts(request or self.request(), self.tts.Response())

    def test_installed_types_match_supplied_sdk_fields(self):
        self.assertEqual(sdk_types(), (self.asr, self.tts, self.state))
        with patch.dict(sys.modules, {'aimdk_msgs.msg': NS(AsrResult=lambda: NS(), CommonState=self.state)}):
            with self.assertRaisesRegex(SetupError, '字段不匹配'):
                sdk_types()

    def test_asr_typed_final_unique_id_sequence_and_no_fake_confidence(self):
        self.bridge.start()
        self.bridge.publish_asr('')
        self.bridge.publish_asr('打开灯')
        self.bridge.publish_asr('播放音乐')
        messages = [call.args[0] for call in self.bridge.publisher.publish.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(isinstance(m, self.asr) and m.is_final for m in messages))
        self.assertEqual(messages[0].text, '打开灯')
        self.assertEqual([m.header.sequence for m in messages], [0, 1])
        self.assertNotEqual(messages[0].event_id, messages[1].event_id)
        self.assertEqual(messages[0].header.stamp.sec, 123)
        self.assertEqual(messages[0].confidence, 0.0)
        self.assertEqual(messages[0].language, '')
        self.assertEqual(self.node.create_publisher.call_args.args[2].durability, 'volatile')

    def test_accepted_is_not_completed_and_only_one_request_can_wait(self):
        self.bridge.busy = False
        response = self.submit()
        self.assertTrue(response.tts_resp.is_success)
        self.assertEqual(response.header.header.code, 0)
        self.assertEqual(response.header.status.value, self.state.SUCCESS)
        self.assertIn('不代表播放完成', response.header.message)
        self.assertEqual(response.tts_resp.trace_id, 'test-trace')
        self.assertEqual(response.tts_resp.estimated_duration, 0)
        self.assertFalse(self.submit().tts_resp.is_success)

    def test_busy_invalid_and_unsupported_priority_requests_are_rejected(self):
        self.assertFalse(self.submit().tts_resp.is_success)
        self.bridge.busy = False
        for text, level, weight in [('', 6, 0), ('字' * 101, 6, 0), ('你好', 8, 0), ('你好', 6, 100)]:
            request = self.request(text)
            request.tts_req.priority_level.value = level
            request.tts_req.priority_weight = weight
            response = self.submit(request)
            self.assertFalse(response.tts_resp.is_success)
            self.assertNotEqual(response.header.header.code, 0)
            self.assertEqual(response.header.status.value, self.state.FAILURE)
            self.assertNotEqual(response.header.status.reason, 0)
            self.assertTrue(response.tts_resp.error_message)
            self.assertIsNone(self.bridge.pending)
        self.bridge.available = lambda: False
        self.assertFalse(self.submit().tts_resp.is_success)

    def test_both_modes_refuse_existing_servers_and_asr_publishers(self):
        for mode, (topic, service) in ENDPOINTS.items():
            bridge = SdkBridge(self.node, mode)
            self.node.get_node_names_and_namespaces.return_value = [('factory', '/')]
            self.node.get_service_names_and_types_by_node.return_value = [(service, ['aimdk_msgs/srv/PlayTts'])]
            with self.assertRaisesRegex(SetupError, '占用'):
                bridge.start()
            self.node.create_service.assert_not_called()
            bridge = SdkBridge(self.node, mode)
            self.node.get_service_names_and_types_by_node.return_value = []
            self.node.get_publishers_info_by_topic.return_value = [NS(node_name='factory', node_namespace='/')]
            with self.assertRaisesRegex(SetupError, '占用'):
                bridge.start()
            self.node.get_publishers_info_by_topic.return_value = []

    def test_late_conflict_rejects_request_and_stops_publishing(self):
        self.bridge.start()
        self.bridge.busy = False
        self.node.get_node_names_and_namespaces.return_value = [('late', '/')]
        self.node.get_service_names_and_types_by_node.return_value = [(self.bridge.service_name, [])]
        self.assertFalse(self.submit().tts_resp.is_success)
        with self.assertRaises(SetupError):
            self.bridge.publish_asr('不能重复执行')
        self.bridge.publisher.publish.assert_not_called()

    def test_playback_mutes_input_rejects_reentry_and_reopens_after_finish(self):
        mic = NativeVoiceInput(None, NS(node=self.node))
        mic.listen(0)
        self.bridge.busy = False
        self.assertTrue(self.submit().tts_resp.is_success)
        def speak(text, on_finished=None):
            self.assertEqual(text, '你好')
            self.assertEqual(mic.phase, 'muted')
            self.assertFalse(self.submit().tts_resp.is_success)
            on_finished()
            self.assertEqual(mic.phase, 'listening')
        self.bridge.drain(speak, mic)
        self.assertIsNone(self.bridge.pending)
        self.assertEqual(mic.phase, 'listening')

    def test_playback_failure_releases_slot_and_waits_for_wake(self):
        mic = NativeVoiceInput(None, NS(node=self.node))
        mic.listen(0)
        self.bridge.busy = False
        self.submit()
        self.bridge.drain(Mock(side_effect=RuntimeError('failed')), mic)
        self.assertIsNone(self.bridge.pending)
        self.assertEqual(mic.phase, 'waiting')
        self.assertTrue(self.submit().tts_resp.is_success)

    def test_network_failure_releases_slot_and_propagates_to_offline_notice(self):
        mic = NativeVoiceInput(None, NS(node=self.node))
        mic.listen(0)
        self.bridge.busy = False
        self.submit()
        with self.assertRaises(TtsNetworkError):
            self.bridge.drain(Mock(side_effect=TtsNetworkError()), mic)
        self.assertIsNone(self.bridge.pending)
        self.assertFalse(self.bridge.busy)
        self.assertEqual(mic.phase, 'waiting')
        self.assertTrue(self.submit().tts_resp.is_success)

    def test_partial_start_and_normal_exit_destroy_owned_endpoints(self):
        self.node.create_service.side_effect = RuntimeError('failed')
        with self.assertRaises(RuntimeError):
            self.bridge.start()
        publisher = self.bridge.publisher
        self.bridge.close()
        self.node.destroy_publisher.assert_called_once_with(publisher)
        self.node.destroy_service.assert_not_called()
