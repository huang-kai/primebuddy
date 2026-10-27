"""ASR 诊断仅保留安全元数据，不记录请求、识别文本或异常正文。"""
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from primebuddy.providers.qianwen.asr import AsrError, MODEL, NEW_MODEL, recognize


MODULE = 'primebuddy.providers.qianwen.asr'
SECRET = 'private-diagnostic-canary'
CONFIG = {'endpoint': 'https://example.cn-beijing.maas.aliyuncs.com/' + SECRET,
          'api_key': SECRET, 'model': MODEL}
REQUEST_ID = '40e0734d-096f-9ae3-86c1-a8c013287561'


class AsrDiagnosticsTests(unittest.TestCase):
    def test_confirmed_native_no_words_is_empty_input_without_retry(self):
        config = dict(CONFIG, endpoint='https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
                      model=NEW_MODEL)
        body = {'code': 'CLIENT_ERROR', 'message': 'ASR_RESPONSE_HAVE_NO_WORDS',
                'request_id': REQUEST_ID}
        error = urllib.error.HTTPError(config['endpoint'], 400, SECRET, {},
                                       io.BytesIO(json.dumps(body).encode()))
        with patch(MODULE + '.urllib.request.build_opener') as opener, \
                self.assertLogs(MODULE, level='INFO') as captured:
            opener.return_value.open.side_effect = error
            self.assertEqual(recognize(SECRET, config), '')
            opener.return_value.open.assert_called_once()
        event = self.events(captured)[0]
        self.assertEqual(event['service_code'], 'CLIENT_ERROR')
        self.assertEqual(event['reason'], 'no_words')
        self.assertEqual(event['request_id'], REQUEST_ID)

    def test_other_client_errors_and_non_400_are_not_silence(self):
        config = dict(CONFIG, endpoint='https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
                      model=NEW_MODEL)
        for status, code, message in [(400, 'CLIENT_ERROR', 'invalid format'),
                                      (400, 'InvalidParameter', 'ASR_RESPONSE_HAVE_NO_WORDS'),
                                      (401, 'CLIENT_ERROR', 'ASR_RESPONSE_HAVE_NO_WORDS'),
                                      (400, 'CLIENT_ERROR', 'ASR_RESPONSE_HAVE_NO_WORDS ' + SECRET)]:
            with self.subTest(status=status, code=code, message=message):
                body = {'code': code, 'message': message}
                error = urllib.error.HTTPError(config['endpoint'], status, SECRET, {},
                                               io.BytesIO(json.dumps(body).encode()))
                with patch(MODULE + '.urllib.request.build_opener') as opener:
                    opener.return_value.open.side_effect = error
                    with self.assertRaises(AsrError):
                        recognize(SECRET, config)

    def test_successful_empty_text_is_empty_input_but_malformed_text_is_error(self):
        config = dict(CONFIG, endpoint='https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
                      model=NEW_MODEL)
        for value in ('', '  ', None, 123):
            response = Mock(status=200)
            response.read.return_value = json.dumps({'output': {'text': value}})
            with self.subTest(value=value), patch(MODULE + '.urllib.request.build_opener') as opener:
                opener.return_value.open.return_value.__enter__.return_value = response
                if isinstance(value, str):
                    self.assertEqual(recognize(SECRET, config), '')
                else:
                    with self.assertRaises(AsrError):
                        recognize(SECRET, config)

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

    def http_error_event(self, body, headers=None, read_error=None):
        source = io.BytesIO(body)
        source.read = Mock(wraps=source.read)
        if read_error is not None:
            source.read.side_effect = read_error
        error = urllib.error.HTTPError(CONFIG['endpoint'], 400, SECRET, headers or {}, source)
        with patch(MODULE + '.urllib.request.build_opener') as opener,\
                patch(MODULE + '.time.monotonic', side_effect=[10, 12]),\
                self.assertLogs(MODULE, level='INFO') as captured:
            opener.return_value.open.side_effect = error
            with self.assertRaises(AsrError) as raised:
                recognize(SECRET, CONFIG)
            opener.return_value.open.assert_called_once()
        self.assertIs(type(raised.exception), AsrError)
        self.assertEqual(str(raised.exception), 'ASR HTTP 400；请核对百炼调用状态')
        events = self.events(captured)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['stage'], 'http_error')
        self.assertEqual(events[0]['http_status'], 400)
        self.assertEqual(events[0]['seconds'], 2)
        source.read.assert_called_once_with(4097)
        return events[0]

    def test_native_http_error_logs_only_allowlisted_code_and_uuid(self):
        for code in ('InvalidParameter', 'InternalError.Algo.InvalidParameter',
                     'InvalidFile.AudioLengthError', 'InvalidFile.DownloadFailed',
                     'DataInspectionFailed', 'data_inspection_failed'):
            with self.subTest(code=code):
                event = self.http_error_event(json.dumps({
                    'code': code, 'request_id': REQUEST_ID.upper(),
                    'message': SECRET, 'body': SECRET, 'audio': SECRET,
                    'api_key': SECRET, 'endpoint': CONFIG['endpoint'],
                }).encode())
                self.assertEqual(event, {'stage': 'http_error', 'seconds': 2, 'http_status': 400,
                                         'service_code': code, 'request_id': REQUEST_ID})

    def test_compatible_http_error_uses_code_and_request_id_header(self):
        event = self.http_error_event(
            json.dumps({'error': {'code': 'InvalidParameter', 'message': SECRET}}).encode(),
            {'x-request-id': REQUEST_ID, 'Authorization': SECRET})
        self.assertEqual(event, {'stage': 'http_error', 'seconds': 2, 'http_status': 400,
                                 'service_code': 'InvalidParameter', 'request_id': REQUEST_ID})

    def test_unknown_and_malicious_codes_are_omitted(self):
        for code in ('UnknownVendorCode', SECRET, 'InvalidParameter.' + SECRET,
                     'InvalidParameter\n' + SECRET, ['InvalidParameter'], {'code': SECRET}, None):
            with self.subTest(code=code):
                event = self.http_error_event(json.dumps({
                    'code': code, 'request_id': REQUEST_ID, 'message': SECRET}).encode())
                self.assertEqual(event, {'stage': 'http_error', 'seconds': 2, 'http_status': 400,
                                         'request_id': REQUEST_ID})

    def test_noncanonical_request_ids_are_omitted_from_body_and_headers(self):
        for request_id in (SECRET, REQUEST_ID + '\n', '{' + REQUEST_ID + '}',
                           REQUEST_ID.replace('-', ''), REQUEST_ID + SECRET,
                           CONFIG['endpoint'], [REQUEST_ID], None):
            with self.subTest(request_id=request_id):
                event = self.http_error_event(json.dumps({
                    'code': 'InvalidParameter', 'request_id': request_id}).encode(),
                    {'x-request-id': request_id})
                self.assertEqual(event, {'stage': 'http_error', 'seconds': 2, 'http_status': 400,
                                         'service_code': 'InvalidParameter'})

    def test_unreadable_or_oversized_error_body_preserves_http_error(self):
        bodies = (b'not-json', b'\xff', b'[]', b'null', b'"' + SECRET.encode() + b'"',
                  b'[' * 2000 + b']' * 2000,
                  json.dumps({'code': 'InvalidParameter', 'request_id': REQUEST_ID,
                              'message': SECRET * 300}).encode())
        for body in bodies:
            with self.subTest(length=len(body)):
                self.assertEqual(self.http_error_event(body),
                                 {'stage': 'http_error', 'seconds': 2, 'http_status': 400})
        self.assertEqual(self.http_error_event(b'', read_error=TimeoutError(110, SECRET)),
                         {'stage': 'http_error', 'seconds': 2, 'http_status': 400})
