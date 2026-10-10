"""千问 ASR 配置与短句识别；不控制机器人，不输出密钥。"""
import argparse
import base64
import io
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
import wave

from .runtime import RUNTIME

CONFIG = RUNTIME / 'qwen_asr.json'
from ..models import ASR_MODEL as MODEL
NEW_MODEL = 'qwen-audio-3.1-asr-flash'
MODELS = (MODEL, NEW_MODEL)
SAMPLE = 'https://dashscope.oss-cn-beijing.aliyuncs.com/audios/welcome.mp3'


class AsrError(Exception):
    pass


def endpoint(host):
    host = host.strip().removeprefix('https://').rstrip('/')
    if host != 'dashscope.aliyuncs.com' and not re.fullmatch(r'[a-zA-Z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com', host):
        raise AsrError('请填写北京地域业务空间的 API Host，仅域名，不含接口路径。')
    return 'https://' + host + '/compatible-mode/v1/chat/completions'


def configure():
    from ..models import configure_model
    from ..wizard import Wizard
    configure_model(Wizard())


def load_config(path=None):
    if path is None:
        from ..models import load_model
        current = load_model()
        if current is not None:
            return current
        path = CONFIG  # 老部署只读兼容；新向导不会覆盖旧配置。
    if not path.is_file():
        raise AsrError('未找到 ASR 配置，请先执行 primebuddy onboard。')
    if path.stat().st_mode & 0o077:
        raise AsrError('ASR 配置权限过宽，请设为 600。')
    config = json.loads(path.read_text())
    prefix = 'https://'
    suffix = '/compatible-mode/v1/chat/completions'
    url = config['endpoint']
    if not url.startswith(prefix) or not url.endswith(suffix):
        raise AsrError('ASR endpoint 无效。')
    if endpoint(url[len(prefix):-len(suffix)]) != url:
        raise AsrError('ASR endpoint 无效。')
    key = config.get('api_key')
    if not isinstance(key, str) or not key or any(char.isspace() for char in key):
        raise AsrError('ASR 密钥配置无效。')
    if config.get('model') not in MODELS:
        raise AsrError('不支持的 ASR 模型。')
    return config


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AsrError('ASR 接口发生重定向，请核对控制台 API Host。')


def request_payload(audio, config, model=None, vocabulary=None):
    model = model or config.get('model', MODEL)
    if model not in MODELS:
        raise AsrError('不支持的 ASR 模型。')
    if model == NEW_MODEL:
        words = {word: 4 for word in dict.fromkeys(vocabulary or [])
                 if isinstance(word, str) and word.strip() and len(word) <= 15
                 and not any(ord(c) < 32 for c in word)}
        words = dict(list(words.items())[:200])
        suffix = '/compatible-mode/v1/chat/completions'
        url = config['endpoint']
        if not url.endswith(suffix) or endpoint(url[8:-len(suffix)]) != url:
            raise AsrError('ASR endpoint 无效。')
        url = url[:-len(suffix)] + '/api/v1/services/aigc/multimodal-generation/generation'
        messages = []
        if words:
            messages.append({'role': 'user', 'content': [{'type': 'input_text',
                             'text': ('家居语音控制与点歌。专有名称：' + '、'.join(words))[:400]}]})
        messages.append({'role': 'user', 'content': [{'type': 'input_audio', 'input_audio': {'data': audio}}]})
        return url, {'model': model, 'input': {'messages': messages},
                     'parameters': {'format': 'wav', 'sample_rate': '16000',
                                    'language_hints': ['zh'], 'vocabulary': words}}

    body = {'model': model, 'messages': [{'role': 'user', 'content': [
        {'type': 'input_audio', 'input_audio': {'data': audio}},
    ]}], 'stream': False, 'asr_options': {'language': 'zh', 'enable_itn': False}}
    return config['endpoint'], body


def recognize(audio, config, model=None, vocabulary=None):
    url, body = request_payload(audio, config, model, vocabulary)
    request = urllib.request.Request(url,
        data=json.dumps(body).encode(), method='POST',
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + config['api_key']})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        hints = {401: '核对密钥与地域', 403: '核对模型权限',
                 404: '核对 API Host 和模型', 429: '核对配额、余额或限流'}
        raise AsrError(f'ASR HTTP {error.code}；' + hints.get(error.code, '请核对百炼调用状态')) from None
    except (urllib.error.URLError, TimeoutError):
        raise AsrError('ASR 网络连接失败或超时，请核对 Jetson 外网连接。') from None
    try:
        text = (result['output']['text'] if body['model'] == NEW_MODEL
                else result['choices'][0]['message']['content'])
    except (KeyError, IndexError, TypeError):
        raise AsrError('ASR 返回结构不符合预期。') from None
    if not isinstance(text, str) or not text.strip():
        raise AsrError('ASR 没有返回识别文字。')
    return text.strip()


def transcribe_wav(data, config, model=None, vocabulary=None):
    if not 44 <= len(data) <= 1_000_000:
        raise AsrError('首版仅接受不超过 1 MB 的短句 WAV。')
    with wave.open(io.BytesIO(data), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise AsrError('请使用单声道、16 kHz、16 位 PCM WAV。')
        if not 0 < audio.getnframes() <= 16000 * 30:
            raise AsrError('首版音频时长须大于 0 且不超过 30 秒。')
        if len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
            raise AsrError('WAV 数据不完整。')
    uri = 'data:audio/wav;base64,' + base64.b64encode(data).decode()
    return recognize(uri, config, model, vocabulary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--configure', action='store_true')
    action.add_argument('--sample', action='store_true', help='识别官方示例音频，产生一次 API 调用')
    action.add_argument('--wav', type=Path, help='将短句 WAV 提交给千问识别')
    args = parser.parse_args()
    try:
        if args.configure:
            configure()
        else:
            config = load_config()
            if args.sample:
                text = recognize(SAMPLE, config)
            else:
                with args.wav.open('rb') as source:
                    data = source.read(1_000_001)
                text = transcribe_wav(data, config)
            print('识别结果：' + text)
        return 0
    except KeyboardInterrupt:
        print('已取消。', file=sys.stderr)
        return 130
    except (AsrError, OSError, ValueError, KeyError, TypeError, wave.Error, EOFError) as error:
        # 配置或上游错误可能含敏感信息，只输出异常类型。
        message = str(error) if isinstance(error, AsrError) else '配置或音频读取失败：' + type(error).__name__
        print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
