"""写入回执丢失时只读确认；不能重发写入或放行未知状态。"""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from primebuddy.integrations.primebot import agent_mode_rpc as rpc
from primebuddy.integrations.primebot import native_agent
from primebuddy.integrations.primebot.robot_audio import AudioError


def response(mode='only_voice', *, code=0, status=1):
    return NS(header=NS(header=NS(code=code), status=NS(value=status)),
              contents=NS(properties=[NS(key=NS(value=2), value=mode)]))


class AgentModeRpcTests(unittest.TestCase):
    def run_rpc(self, *, write=True, set_result=None, get_result=None, unavailable=None, preflight_result=None):
        requests = []
        self.settled = False
        get_calls = 0
        self.requests = requests
        def request():
            return NS(header=NS(header=NS()), contents=NS(properties=[]))
        services = {name: type(name, (), {'Request': staticmethod(request)}) for name in
                    ('SetAgentPropertiesRequest', 'GetAgentPropertiesRequest')}
        replies = {'SetAgentPropertiesRequest': response() if set_result is None else set_result,
                   'GetAgentPropertiesRequest': response() if get_result is None else get_result}
        def create_client(service, _):
            def call(value):
                nonlocal get_calls
                self.assertTrue(self.settled, "request sent before discovery settled")
                requests.append((service.__name__, value))
                reply = replies[service.__name__]
                if service.__name__ == 'GetAgentPropertiesRequest':
                    get_calls += 1
                    if write and get_calls == 1:
                        reply = response('normal') if preflight_result is None else preflight_result
                return NS(done=lambda: reply != 'timeout', result=lambda: reply)
            return NS(wait_for_service=lambda **_: service.__name__ != unavailable,
                      call_async=call)
        node = Mock()
        node.create_client.side_effect = create_client
        node.get_clock.return_value.now.return_value.to_msg.return_value = object()
        ros = Mock(create_node=Mock(return_value=node))
        property_id = lambda **kwargs: NS(**kwargs)
        property_id.AGENT_PROPERTY_RUN_MODE = 2
        modules = {'rclpy': ros, 'aimdk_msgs': NS(),
                   'aimdk_msgs.msg': NS(AgentPropertyIdType=property_id,
                                        AgentPropertiesValue=lambda **kwargs: NS(**kwargs)),
                   'aimdk_msgs.srv': NS(**services)}
        output = io.StringIO()
        with patch.dict('sys.modules', modules), patch('sys.argv', ['rpc'] + (['only_voice'] if write else [])), \
                patch.object(rpc, 'settle_discovery', side_effect=lambda *_: setattr(self, 'settled', True)), \
                contextlib.redirect_stdout(output):
            try:
                rpc.main()
            finally:
                node.destroy_node.assert_called_once()
                ros.shutdown.assert_called_once()
        return json.loads(output.getvalue())

    def test_success_requires_write_and_matching_readback(self):
        result = self.run_rpc()
        self.assertEqual(result, {'mode': 'only_voice', 'write_response_timeout': False})
        self.assertEqual([name for name, _ in self.requests],
                         ['GetAgentPropertiesRequest', 'SetAgentPropertiesRequest', 'GetAgentPropertiesRequest'])

    def test_lost_write_ack_is_reconciled_without_resending(self):
        self.assertEqual(self.run_rpc(set_result='timeout'),
                         {'mode': 'only_voice', 'write_response_timeout': True})
        self.assertEqual([name for name, _ in self.requests],
                         ['GetAgentPropertiesRequest', 'SetAgentPropertiesRequest', 'GetAgentPropertiesRequest'])

    def test_lost_write_ack_with_old_mode_is_not_success(self):
        with self.assertRaisesRegex(rpc.ModeRpcError, 'readback_mismatch'):
            self.run_rpc(set_result='timeout', get_result=response('normal'))
        self.assertEqual(len(self.requests), 3)

    def test_unconfirmed_readback_is_not_success(self):
        with self.assertRaisesRegex(rpc.ModeRpcError, 'GetAgentPropertiesRequest: response_timeout'):
            self.run_rpc(set_result='timeout', get_result='timeout')
        self.assertEqual(len(self.requests), 3)

    def test_explicit_write_rejection_is_not_reconciled(self):
        with self.assertRaisesRegex(rpc.ModeRpcError, 'write_rejected'):
            self.run_rpc(set_result=response(status=-1))
        self.assertEqual(len(self.requests), 2)

    def test_failed_read_handshake_never_changes_mode(self):
        with self.assertRaisesRegex(rpc.ModeRpcError, 'response_timeout'):
            self.run_rpc(preflight_result='timeout')
        self.assertEqual([name for name, _ in self.requests], ['GetAgentPropertiesRequest'])

    def test_service_unavailable_does_not_write(self):
        with self.assertRaisesRegex(rpc.ModeRpcError, 'service_unavailable'):
            self.run_rpc(unavailable='SetAgentPropertiesRequest')
        self.assertEqual(self.requests, [])

    def test_invalid_readback_is_rejected(self):
        for value in (response('unknown'), NS(header=response().header, contents=NS(properties=[]))):
            with self.subTest(value=value), self.assertRaisesRegex(rpc.ModeRpcError, 'invalid_mode'):
                self.run_rpc(get_result=value)

    def test_read_only_never_writes(self):
        self.assertEqual(self.run_rpc(write=False, get_result=response('normal'))['mode'], 'normal')
        self.assertEqual([name for name, _ in self.requests], ['GetAgentPropertiesRequest'])


class AgentModeDiagnosticsTests(unittest.TestCase):
    def test_parent_logs_structured_failure(self):
        result = NS(returncode=1, stdout=b'{"error":{"service":"GetAgentPropertiesRequest","reason":"response_timeout"}}', stderr=b'')
        with patch.object(native_agent, 'host_command', return_value=['fixture']), \
                patch.object(native_agent.subprocess, 'run', return_value=result), \
                self.assertLogs(native_agent.logger, 'ERROR') as logs, self.assertRaises(AudioError):
            native_agent.agent_mode(Path('/tmp'), 'only_voice')
        self.assertIn('GetAgentPropertiesRequest', logs.output[0])
        self.assertIn('response_timeout', logs.output[0])

    def test_parent_logs_confirmed_lost_ack(self):
        result = NS(returncode=0, stdout=b'{"mode":"only_voice","write_response_timeout":true}', stderr=b'')
        with patch.object(native_agent, 'host_command', return_value=['fixture']), \
                patch.object(native_agent.subprocess, 'run', return_value=result), \
                self.assertLogs(native_agent.logger, 'WARNING') as logs:
            self.assertEqual(native_agent.agent_mode(Path('/tmp'), 'only_voice'), 'only_voice')
        self.assertIn('AGENT_MODE_WRITE_RESPONSE_TIMEOUT_CONFIRMED', logs.output[0])


if __name__ == '__main__':
    unittest.main()
