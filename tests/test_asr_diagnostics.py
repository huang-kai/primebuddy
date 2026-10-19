"""ASR 诊断仅保留网络状态与耗时，不记录请求、识别文本或异常正文。"""
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from primebuddy.providers.qianwen.asr import AsrError, MODEL, recognize


MODULE = 'primebuddy.providers.qianwen.asr'
SECRET = 'private-diagnostic-canary'
CONFIG = {'endpoint': 'https://example.cn-beijing.maas.aliyuncs.com/' + SECRET,
          'api_key': SECRET, 'model': MODEL}


class AsrDiagnosticsTests(unittest.TestCase):
    def events(self, captured):
        self.assertNotIn(SECRET, '\n'.join(captured.output))
        return [json.loads(record.getMessage().split(' ', 1)[1]) for record in captured.records]

    def test_success_separates_headers_and_json_without_logging_payload(self):
        response = Mock(status=200)
        response.read.return_value = json.dumps({'choices': [{'message': {'content': SECRET}}]})
        with patch(MODULE + '.urllib.request.build_opener') as opener,\
                patch(MODULE + '.time.monotonic', side_effect=[10, 12, 15]),\
                self.assertLogs(MODULE, level='INFO') as captured:
            opener.return_value.open.return_value.__enter__.return_value = response
            self.assertEqual(recognize(SECRET, CONFIG), SECRET)
        self.assertEqual(self.events(captured), [
            {'stage': 'headers', 'seconds': 2, 'http_status': 200},
            {'stage': 'json', 'seconds': 3}])

    def test_http_and_network_errors_log_only_safe_fields_without_retry(self):
        failures = [
            (urllib.error.HTTPError(CONFIG['endpoint'], 403, SECRET, {}, None),
             {'stage': 'http_error', 'seconds': 2, 'http_status': 403}),
            (urllib.error.URLError(TimeoutError(110, SECRET)),
             {'stage': 'network_error', 'seconds': 2, 'reason_type': 'TimeoutError', 'errno': 110}),
            (urllib.error.URLError(SECRET),
             {'stage': 'request_error', 'seconds': 2, 'reason_type': 'str', 'errno': None}),
        ]
        for error, expected in failures:
            with self.subTest(kind=expected['stage']),\
                    patch(MODULE + '.urllib.request.build_opener') as opener,\
                    patch(MODULE + '.time.monotonic', side_effect=[10, 12]),\
                    self.assertLogs(MODULE, level='INFO') as captured:
                opener.return_value.open.side_effect = error
                with self.assertRaises(AsrError) as raised:
                    recognize(SECRET, CONFIG)
                opener.return_value.open.assert_called_once()
            self.assertNotIn(SECRET, str(raised.exception))
            self.assertNotIn('Jetson', str(raised.exception))
            self.assertEqual(self.events(captured), [expected])

    def test_response_body_timeout_is_distinct_from_waiting_for_headers(self):
        response = Mock(status=200)
        response.read.side_effect = TimeoutError(110, SECRET)
        with patch(MODULE + '.urllib.request.build_opener') as opener,\
                patch(MODULE + '.time.monotonic', side_effect=[10, 12, 42, 42]),\
                self.assertLogs(MODULE, level='INFO') as captured:
            opener.return_value.open.return_value.__enter__.return_value = response
            with self.assertRaises(AsrError):
                recognize(SECRET, CONFIG)
        self.assertEqual(self.events(captured), [
            {'stage': 'headers', 'seconds': 2, 'http_status': 200},
            {'stage': 'json', 'seconds': 30},
            {'stage': 'network_error', 'seconds': 32, 'reason_type': 'TimeoutError', 'errno': 110}])
