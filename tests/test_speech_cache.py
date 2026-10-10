import io
import json
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import Mock

from primebuddy.voice_support.speech_cache import SpeechCache


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
