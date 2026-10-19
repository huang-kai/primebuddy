"""缓存已校验的提示语 WAV；命中时无需云端合成或下载。"""
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile

from primebuddy.voice.audio_format import MAX_BYTES, validate_wav
from primebuddy.providers import synthesize_wav
from primebuddy.core.timing import measure
from primebuddy.config.storage import application_directory, load_app_config

logger = logging.getLogger(__name__)

IDENTITY = 'custom-synthesizer-pcm16-24k-v1'
DEFAULTS = load_app_config()['voice']
COMMON_PROMPTS = ('PrimeBuddy 已启动，你想做什么？', '我在，请说。', '还想做什么？', '语音助手已退出。')
NETWORK_ERROR_PROMPT = '网络连接出现了问题，请重新连接网络。'


class SpeechCache:
    def __init__(self, directory, synthesize=None, identity=None, limit=DEFAULTS['speech_cache_entries']):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if synthesize is None:
            from primebuddy.config.models import load_model
            config = load_model('tts')
            synthesize = lambda text: synthesize_wav(text, config)
            identity = json.dumps([config['provider'], config['model'], config['endpoint'], config.get('voice'), 'pcm16-24k-v1'])
        self.synthesize, self.identity, self.limit = synthesize, identity or IDENTITY, limit

    def prepare(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 100:
            raise ValueError('提示语须为 1 至 100 字')
        if text == NETWORK_ERROR_PROMPT:
            # 随安装包提供，不受缓存淘汰或云端合成是否可用的影响。
            path = application_directory() / 'resource/voice/network-error.wav'
            validate_wav(path.read_bytes())
            logger.info('TTS_CACHE bundled_network_prompt')
            return path
        key = hashlib.sha256((self.identity + '\0' + text).encode()).hexdigest()
        path = self.directory / (key + '.wav')
        with measure('TTS缓存准备'):
            if path.is_file():
                try:
                    if path.stat().st_size > MAX_BYTES:
                        raise ValueError('缓存文件过大')
                    validate_wav(path.read_bytes())
                except (OSError, ValueError, RuntimeError):
                    path.unlink(missing_ok=True)
                except Exception:
                    # 无效音频只影响这一条缓存，不能播放损坏文件。
                    path.unlink(missing_ok=True)
                else:
                    path.touch()
                    logger.info('TTS_CACHE hit')
                    return path
            logger.info('TTS_CACHE miss')
            data = self.synthesize(text)
            validate_wav(data)
            fd, name = tempfile.mkstemp(dir=self.directory, suffix='.tmp')
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(data)
                os.replace(name, path)
            finally:
                Path(name).unlink(missing_ok=True)
            entries = sorted(self.directory.glob('*.wav'), key=lambda p: p.stat().st_mtime)
            for old in entries[:max(0, len(entries) - self.limit)]:
                if old != path:
                    old.unlink(missing_ok=True)
            return path
