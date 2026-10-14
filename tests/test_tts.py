"""云端合成协议、音频校验及持久缓存。"""
import io
import json
import unittest
import urllib.error
import wave
from unittest.mock import Mock, patch
from primebuddy.providers.qianwen import build_config
from primebuddy.providers.qianwen.tts import TtsError, synthesize_wav
from pathlib import Path
import tempfile
from unittest.mock import Mock
from primebuddy.voice.speech_cache import SpeechCache


class TtsTests(unittest.TestCase):
    def setUp(self):
        self.config = build_config('tts', 'qwen-audio-3.0-tts-plus', {'api_host': 'demo.cn-beijing.maas.aliyuncs.com', 'api_key': 'test-only'})
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

    def test_asr_configuration_is_not_reused_for_tts(self):
        config = build_config('asr', 'qwen3-asr-flash', {'api_host': 'dashscope.aliyuncs.com', 'api_key': 'test-only'})
        with patch('urllib.request.build_opener') as opener, self.assertRaises(TtsError):
            synthesize_wav('我在，请说。', config)
        opener.assert_not_called()

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
                self.config['endpoint'], 403, 'private detail', {}, None)))
        with patch('urllib.request.build_opener', return_value=opener), self.assertRaises(TtsError) as raised:
            synthesize_wav('连接成功。', self.config)
        self.assertIn('403', str(raised.exception))
        self.assertNotIn('private detail', str(raised.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_truncated_audio_is_rejected(self):
        opener = self.opener(audio=self.wav[:-2])
        with patch('urllib.request.build_opener', return_value=opener), self.assertRaises(TtsError):
            synthesize_wav('连接成功。', self.config)


def wav():
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * 240)
    return output.getvalue()


class SpeechCacheTests(unittest.TestCase):
    def test_repeated_prompt_and_new_session_do_not_call_cloud_again(self):
        with tempfile.TemporaryDirectory() as root:
            synth = Mock(return_value=wav())
            first = SpeechCache(Path(root), synth)
            path = first.prepare('正在关闭餐厅灯')
            self.assertEqual(first.prepare('正在关闭餐厅灯'), path)
            second = SpeechCache(Path(root), synth)
            self.assertEqual(second.prepare('正在关闭餐厅灯'), path)
            synth.assert_called_once_with('正在关闭餐厅灯')

    def test_corrupt_cache_is_regenerated(self):
        with tempfile.TemporaryDirectory() as root:
            synth = Mock(return_value=wav())
            cache = SpeechCache(Path(root), synth)
            path = cache.prepare('我在，请说。')
            path.write_bytes(b'truncated')
            self.assertEqual(cache.prepare('我在，请说。').read_bytes(), wav())
            self.assertEqual(synth.call_count, 2)

    def test_text_and_voice_revision_do_not_share_audio(self):
        with tempfile.TemporaryDirectory() as root:
            synth = Mock(return_value=wav())
            cache = SpeechCache(Path(root), synth, identity='voice-A')
            one = cache.prepare('开灯')
            two = cache.prepare('关灯')
            three = SpeechCache(Path(root), synth, identity='voice-B').prepare('开灯')
            self.assertEqual(len({one, two, three}), 3)

    def test_plus_upgrade_does_not_reuse_cached_cherry_audio(self):
        with tempfile.TemporaryDirectory() as root:
            old_identity = json.dumps([
                'https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation',
                'qwen3-tts-flash', 'Cherry', 'Chinese', 'pcm16-24k-v1'])
            old_path = SpeechCache(root, Mock(return_value=wav()), identity=old_identity).prepare('我在，请说。')
            synth = Mock(return_value=wav())
            new_path = SpeechCache(root, synth).prepare('我在，请说。')
            self.assertNotEqual(old_path, new_path)
            synth.assert_called_once_with('我在，请说。')

    def test_invalid_synthesis_is_never_cached(self):
        with tempfile.TemporaryDirectory() as root:
            cache = SpeechCache(Path(root), Mock(return_value=b'bad'))
            with self.assertRaises(Exception):
                cache.prepare('开灯')
            self.assertEqual(list(Path(root).glob('*.wav')), [])

    def test_cache_is_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            cache = SpeechCache(Path(root), Mock(return_value=wav()), limit=2)
            for phrase in ('第一句', '第二句', '第三句'):
                path = cache.prepare(phrase)
                self.assertTrue(path.exists())
            self.assertEqual(len(list(Path(root).glob('*.wav'))), 2)
