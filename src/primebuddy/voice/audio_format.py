"""框架提示音的统一 WAV 契约：24kHz 单声道 PCM16。"""
import io
import wave
from primebuddy.providers.errors import TtsError

MAX_BYTES = 8 * 1024 * 1024


def validate_wav(data):
    if not 44 <= len(data) <= MAX_BYTES:
        raise TtsError('TTS 音频大小异常。')
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 24000):
                raise TtsError('TTS 音频不是 24kHz 单声道 PCM16 WAV。')
            if not 0 < audio.getnframes() <= 24000 * 60:
                raise TtsError('TTS 提示音时长异常。')
            if len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
                raise TtsError('TTS 音频不完整。')
            return audio.getnframes() / 24000
    except (wave.Error, EOFError):
        raise TtsError('TTS 返回的文件不是有效 WAV。') from None
