"""使用现有北京地域百炼配置生成千问提示音，输出 SDK 可播放的 WAV。"""
import argparse
import io
import json
import os
from pathlib import Path
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import wave

from .qwen_asr import load_config
from .timing import measure

from ..models import TTS_ENDPOINT as ENDPOINT, TTS_MODEL as MODEL, TTS_VOICE as VOICE, qianwen_config
MAX_BYTES = 8 * 1024 * 1024


class TtsError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TtsError('TTS 请求发生重定向，未继续访问。')


def audio_url(url):
    if not isinstance(url, str):
        raise TtsError('TTS 未返回音频地址。')
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme not in {'http', 'https'} or parts.username or parts.password
            or parts.hostname != 'dashscope-result-bj.oss-cn-beijing.aliyuncs.com'
            or parts.port not in {None, 80, 443}):
        raise TtsError('TTS 音频地址不属于已核对的北京结果域名。')
    return urllib.parse.urlunsplit(parts._replace(scheme='https', netloc=parts.hostname))


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


def normalize_wav(data):
    # 只修复已观测到的流式 WAV 长度占位头；普通截断文件仍必须校验失败。
    if (len(data) >= 44 and data[:4] == b'RIFF' and data[8:16] == b'WAVEfmt '
            and data[36:40] == b'data'
            and struct.unpack_from('<I', data, 4)[0] == 0x7fffffbf
            and struct.unpack_from('<I', data, 40)[0] == 0x7fffff9b):
        if len(data) > MAX_BYTES or (len(data) - 44) % 2:
            raise TtsError('TTS 流式 WAV 数据长度异常。')
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 24000):
                raise TtsError('TTS 流式 WAV 格式异常。')
        output = io.BytesIO()
        with wave.open(output, 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(data[44:])
        data = output.getvalue()
    validate_wav(data)
    return data


def synthesize_wav(text, config=None):
    if not isinstance(text, str) or not text.strip() or len(text) > 100:
        raise TtsError('首版提示语须为 1 至 100 字。')
    config = load_config() if config is None else config
    # 旧 ASR 配置没有 TTS 字段，仍沿用其已验证的业务空间和凭据。
    host = urllib.parse.urlsplit(config.get('endpoint', ENDPOINT)).hostname
    endpoint = config.get('tts_endpoint') or qianwen_config(host, config['api_key'])['tts_endpoint']
    body = {'model': config.get('tts_model', MODEL), 'input': {
        'text': text, 'voice': config.get('tts_voice', VOICE),
        'format': 'wav', 'sample_rate': 24000, 'language_hints': ['zh']}}
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode(), method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + config['api_key']})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with measure('TTS云端请求'):
            with opener.open(request, timeout=30) as response:
                raw = response.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise TtsError('TTS 响应超过预期大小。')
        result = json.loads(raw)
        if result.get('code') or result.get('status_code', 200) != 200:
            raise TtsError('TTS 返回失败，请核对模型权限和百炼调用状态。')
        if result['output'].get('finish_reason') != 'stop':
            raise TtsError('TTS 合成尚未完成，未下载或缓存音频。')
        url = audio_url(result['output']['audio']['url'])
        # 下载合成音频时不附带百炼密钥，防止凭据发送到另一个服务。
        with measure('TTS结果下载'):
            with opener.open(urllib.request.Request(url), timeout=30) as response:
                data = response.read(MAX_BYTES + 1)
        return normalize_wav(data)
    except urllib.error.HTTPError as error:
        raise TtsError(f'TTS HTTP {error.code}；请核对北京地域密钥、模型权限和配额。') from None
    except (urllib.error.URLError, TimeoutError):
        raise TtsError('TTS 网络连接失败或超时。') from None
    except (KeyError, TypeError, ValueError):
        raise TtsError('TTS 返回结构不符合预期。') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--text', default='点歌台启动，你想听什么歌？')
    parser.add_argument('--output', type=Path, required=True, help='只保存提示语，不播放或启停 Agent')
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise TtsError('输出文件已存在，未覆盖。')
        data = synthesize_wav(args.text)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        print(f'TTS 合成成功：{validate_wav(data):.2f} 秒；未播放。')
        return 0
    except KeyboardInterrupt:
        print('已取消。', file=sys.stderr)
        return 130
    except Exception as error:
        print(str(error) if isinstance(error, TtsError) else 'TTS 失败：' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
