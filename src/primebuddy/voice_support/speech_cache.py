"""缓存已校验的提示语 WAV；命中时无需云端合成或下载。"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .qwen_tts import ENDPOINT, MODEL, VOICE, MAX_BYTES, synthesize_wav, validate_wav
from .timing import measure

IDENTITY = json.dumps([ENDPOINT, MODEL, VOICE, 'Chinese', 'pcm16-24k-v1'])
COMMON_PROMPTS = (
    'PrimeBuddy 已启动，你想做什么？', '我在，请说。', '还想做什么？',
    '语音助手已退出。',
)


class SpeechCache:
    def __init__(self, directory, synthesize=synthesize_wav, identity=IDENTITY, limit=128):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.synthesize, self.identity, self.limit = synthesize, identity, limit

    def prepare(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 100:
            raise ValueError('提示语须为 1 至 100 字')
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
                    print('TTS_CACHE hit', flush=True)
                    return path
            print('TTS_CACHE miss', flush=True)
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
