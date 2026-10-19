"""网络提示只用于传输故障，HTTP 业务失败与本地配置错误仍独立处理。"""
import errno
import http.client
import json
import socket
import ssl
import unittest
import urllib.error
from unittest.mock import MagicMock, Mock, patch

from primebuddy.providers.errors import (
    AsrError, AsrNetworkError, ProviderNetworkError, TtsError, TtsNetworkError,
    is_network_error,
)
from primebuddy.providers.qianwen.asr import recognize
from primebuddy.providers.qianwen.llm import request_message
from primebuddy.providers.qianwen.tts import synthesize_wav


SECRET = 'private-network-error-canary'
CONFIG = {'endpoint': 'https://example.invalid/' + SECRET, 'api_key': SECRET,
          'model': 'qwen3-asr-flash', 'role': 'tts', 'voice': 'test'}
PROVIDERS = (
    ('asr', lambda: recognize(SECRET, CONFIG), AsrNetworkError, AsrError),
    ('llm', lambda: request_message([], CONFIG), ProviderNetworkError, Exception),
    ('tts', lambda: synthesize_wav('测试。', CONFIG), TtsNetworkError, TtsError),
)


class ProviderNetworkErrorTests(unittest.TestCase):
    def assert_network_failure(self, invoke, network_type, compatible_type, error,
                               body=False):
        opener = MagicMock()
        if body:
            response = Mock(status=200)
            response.read.side_effect = error
            opener.open.return_value.__enter__.return_value = response
        else:
            opener.open.side_effect = error
        with patch('urllib.request.build_opener', return_value=opener),\
                self.assertRaises(network_type) as raised:
            invoke()
        self.assertIsInstance(raised.exception, compatible_type)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        opener.open.assert_called_once()

    def test_dns_timeout_and_connection_failures_have_a_shared_safe_type(self):
        failures = (
            urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, SECRET)),
            urllib.error.URLError(TimeoutError(errno.ETIMEDOUT, SECRET)),
            TimeoutError(SECRET),
            ConnectionRefusedError(errno.ECONNREFUSED, SECRET),
            OSError(errno.ENETUNREACH, SECRET),
        )
        for provider, invoke, network_type, compatible_type in PROVIDERS:
            for error in failures:
                with self.subTest(provider=provider, error=type(error).__name__):
                    self.assert_network_failure(invoke, network_type, compatible_type, error)

    def test_response_body_disconnects_are_also_network_failures(self):
        failures = (ConnectionResetError(errno.ECONNRESET, SECRET),
                    http.client.RemoteDisconnected(SECRET),
                    http.client.IncompleteRead(SECRET.encode(), 100),
                    ssl.SSLEOFError(8, SECRET))
        for provider, invoke, network_type, compatible_type in PROVIDERS:
            for error in failures:
                with self.subTest(provider=provider, error=type(error).__name__):
                    self.assert_network_failure(invoke, network_type, compatible_type,
                                                error, body=True)

    def test_tts_result_download_disconnect_uses_the_same_network_type(self):
        response = Mock()
        response.read.return_value = json.dumps({'output': {
            'finish_reason': 'stop', 'audio': {
                'url': 'https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/test.wav'}}}).encode()
        first = Mock()
        first.__enter__ = Mock(return_value=response)
        first.__exit__ = Mock(return_value=False)
        opener = Mock(open=Mock(side_effect=[first, ConnectionResetError(SECRET)]))
        with patch('urllib.request.build_opener', return_value=opener),\
                self.assertRaises(TtsNetworkError):
            synthesize_wav('测试。', CONFIG)
        self.assertEqual(opener.open.call_count, 2)

    def test_http_auth_quota_and_server_errors_do_not_request_reconnection(self):
        for provider, invoke, _, _ in PROVIDERS:
            for status in (401, 403, 429, 500, 503):
                error = urllib.error.HTTPError(CONFIG['endpoint'], status, SECRET, {}, None)
                with self.subTest(provider=provider, status=status),\
                        patch('urllib.request.build_opener') as opener:
                    opener.return_value.open.side_effect = error
                    with self.assertRaises(Exception) as raised:
                        invoke()
                    self.assertFalse(is_network_error(error))
                    self.assertNotIsInstance(raised.exception, ProviderNetworkError)
                    opener.return_value.open.assert_called_once()

    def test_configuration_and_unclassified_url_errors_are_not_disconnects(self):
        failures = (ssl.SSLCertVerificationError(1, SECRET),
                    urllib.error.URLError(ssl.SSLCertVerificationError(1, SECRET)),
                    urllib.error.URLError(SECRET),
                    OSError(errno.EACCES, SECRET))
        for provider, invoke, _, _ in PROVIDERS:
            for error in failures:
                with self.subTest(provider=provider, error=type(error).__name__),\
                        patch('urllib.request.build_opener') as opener:
                    opener.return_value.open.side_effect = error
                    with self.assertRaises(Exception) as raised:
                        invoke()
                    self.assertFalse(is_network_error(error))
                    self.assertNotIsInstance(raised.exception, ProviderNetworkError)


if __name__ == '__main__':
    unittest.main()
