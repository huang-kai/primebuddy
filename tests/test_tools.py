import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('voice_e2e', Path(__file__).resolve().parents[1] / 'src/primebuddy/tools/voice_e2e.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class VoiceE2eTests(unittest.TestCase):
    def test_error_recovery_window_cannot_be_counted_as_success(self):
        lines = ['ASR 播放周杰伦的晴天。', 'TTS 已播放 60 秒，歌曲已停止。', 'TTS 还想做什么？',
                 'AUDIO_ERROR 原厂音频提前停止或播放失败。', 'TURN_ERROR AudioError',
                 'TTS 本轮未能完整完成', 'VOICE_WINDOW_OPEN 30s']
        with self.assertRaisesRegex(RuntimeError, '不能'):
            module.require_clean_turn(lines)

    def test_clean_turn_can_pass(self):
        module.require_clean_turn(['ASR 播放周杰伦的晴天。', 'TTS 已播放 60 秒，歌曲已停止。',
                                   'TTS 还想做什么？', 'VOICE_WINDOW_OPEN 30s'])
