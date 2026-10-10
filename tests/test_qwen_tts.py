import io
import json
import unittest
import urllib.error
import wave
from unittest.mock import Mock, patch

from primebuddy.models import qianwen_config
from primebuddy.voice_support.qwen_tts import TtsError, synthesize_wav


class TtsTests(unittest.TestCase):
    def setUp(self):
        self.config = qianwen_config('demo.cn-beijing.maas.aliyuncs.com', 'test-only')
        output = io.BytesIO()
        with wave.open(output, 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 240)
        self.wav = output.getvalue()
        self.result = {'output': {'finish_reason': 'stop', 'audio': {
            'url': 'http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/pre/test.wav?signature=test'}}}

    def opener(self, result=None, audio=None):
        return Mock(open=Mock(side_effect=[
            io.BytesIO(json.dumps(self.result if result is None else result).encode()),
            io.BytesIO(self.wav if audio is None else audio),
        ]))

    def test_plus_http_contract_and_download_without_credentials(self):
        opener = self.opener()
        with patch('urllib.request.build_opener', return_value=opener):
            self.assertEqual(synthesize_wav('连接成功。', self.config), self.wav)
        request, download = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(request.full_url,
                         'https://demo.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-only')
        self.assertEqual(json.loads(request.data), {
            'model': 'qwen-audio-3.0-tts-plus', 'input': {
                'text': '连接成功。', 'voice': 'longanlingxin',
                'format': 'wav', 'sample_rate': 24000, 'language_hints': ['zh']}})
        self.assertTrue(download.full_url.startswith('https://dashscope-result-bj.'))
        self.assertIsNone(download.get_header('Authorization'))

    def test_legacy_asr_credentials_use_plus_at_the_same_workspace_host(self):
        config = {key: self.config[key] for key in ('endpoint', 'api_key', 'model')}
        opener = self.opener()
        with patch('urllib.request.build_opener', return_value=opener):
            synthesize_wav('我在，请说。', config)
        request = opener.open.call_args_list[0].args[0]
        self.assertEqual(request.full_url,
                         'https://demo.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer')
        self.assertEqual(json.loads(request.data)['model'], 'qwen-audio-3.0-tts-plus')

    def test_failed_incomplete_or_untrusted_response_does_not_download(self):
        for result in ({'code': 'InvalidParameter', 'message': 'private upstream detail'},
                       {'output': {'finish_reason': None, 'audio': self.result['output']['audio']}},
                       {'output': {'finish_reason': 'stop', 'audio': {'url': 'https://untrusted.example/a.wav'}}}):
            with self.subTest(result=result):
                opener = self.opener(result)
                with patch('urllib.request.build_opener', return_value=opener), self.assertRaises(TtsError) as raised:
                    synthesize_wav('连接成功。', self.config)
                self.assertNotIn('private upstream detail', str(raised.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_http_failure_is_reported_without_retrying_a_different_model(self):
        opener = Mock(open=Mock(side_effect=urllib.error.HTTPError(
            self.config['tts_endpoint'], 403, 'private detail', {}, None)))
        with patch('urllib.request.build_opener', return_value=opener), self.assertRaises(TtsError) as raised:
            synthesize_wav('连接成功。', self.config)
        self.assertIn('403', str(raised.exception))
        self.assertNotIn('private detail', str(raised.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_truncated_audio_is_rejected(self):
        opener = self.opener(audio=self.wav[:-2])
        with patch('urllib.request.build_opener', return_value=opener), self.assertRaises(TtsError):
            synthesize_wav('连接成功。', self.config)
