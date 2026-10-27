"""千问 ASR 配置与短句识别；不控制机器人，不输出密钥。"""
import argparse
import base64
import http.client
import io
import json
import logging
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
import wave

from primebuddy.providers.errors import AsrError, AsrNetworkError, is_network_error

# 两个已实现的请求协议；向导的候选列表由 providers.yaml 维护。
MODEL = 'qwen3-asr-flash'
NEW_MODEL = 'qwen-audio-3.1-asr-flash'
SAMPLE = 'https://dashscope.oss-cn-beijing.aliyuncs.com/audios/welcome.mp3'
logger = logging.getLogger(__name__)
# 只保留官方已列出的错误码；未知值可能含请求内容，不能直接记录。
# https://help.aliyun.com/zh/model-studio/error-code
HTTP_ERROR_CODES = frozenset({
    'InvalidParameter', 'InternalError.Algo.InvalidParameter',
    'InvalidFile.AudioLengthError', 'InvalidFile.DownloadFailed',
    'DataInspectionFailed', 'data_inspection_failed', 'CLIENT_ERROR',
})
HTTP_ERROR_LIMIT = 4096


def request_log(stage, elapsed, **details):
    # 仅接收状态、类型、errno、耗时及已校验的错误码/请求 ID，不记录正文。
    logger.info('ASR_REQUEST %s', json.dumps(dict(stage=stage, seconds=round(elapsed, 3), **details)))


def http_error_details(error):
    try:
        with error:
            data = error.read(HTTP_ERROR_LIMIT + 1)
        result = json.loads(data) if len(data) <= HTTP_ERROR_LIMIT else {}
    except (OSError, ValueError, TypeError, RecursionError, http.client.HTTPException):
        result = {}
    if not isinstance(result, dict):
        result = {}
    problem = result.get('error', result)
    code = problem.get('code') if isinstance(problem, dict) else None
    details = {}
    if isinstance(code, str) and code in HTTP_ERROR_CODES:
        details['service_code'] = code
    # 3.1 实测静音返回此精确组合；不能把其他参数/权限错误吞成空识别。
    if code == 'CLIENT_ERROR' and problem.get('message') == 'ASR_RESPONSE_HAVE_NO_WORDS':
        details['reason'] = 'no_words'
    header_id = error.headers.get('x-request-id') if error.headers else None
    for request_id in (result.get('request_id'), header_id):
        if isinstance(request_id, str) and re.fullmatch(
                r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', request_id):
            details['request_id'] = request_id.lower()
            break
    return details


def endpoint(host):
    host = host.strip().removeprefix('https://').rstrip('/')
    if host != 'dashscope.aliyuncs.com' and not re.fullmatch(r'[a-zA-Z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com', host):
        raise AsrError('请填写北京地域业务空间的 API Host，仅域名，不含接口路径。')
    return 'https://' + host + '/compatible-mode/v1/chat/completions'


def configure():
    from primebuddy.config.models import configure_model
    from primebuddy.cli.wizard import Wizard
    configure_model(Wizard())


def load_config():
    from primebuddy.config.models import load_model
    return load_model('asr')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AsrError('ASR 接口发生重定向，请核对控制台 API Host。')


def request_payload(audio, config, model=None, vocabulary=None):
    model = model or config.get('model', MODEL)
    if model not in (MODEL, NEW_MODEL):
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
    started = time.monotonic()
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            received = time.monotonic()
            status = response.status if type(response.status) is int else None
            request_log('headers', received - started, http_status=status)
            try:
                result = json.load(response)
            finally:
                request_log('json', time.monotonic() - received)
    except urllib.error.HTTPError as error:
        status = error.code if type(error.code) is int else None
        elapsed = time.monotonic() - started
        details = http_error_details(error)
        request_log('http_error', elapsed, http_status=status, **details)
        if status == 400 and body['model'] == NEW_MODEL and details.get('reason') == 'no_words':
            return ''
        hints = {401: '核对密钥与地域', 403: '核对模型权限',
                 404: '核对 API Host 和模型', 429: '核对配额、余额或限流'}
        raise AsrError(f'ASR HTTP {status}；' + hints.get(status, '请核对百炼调用状态')) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        number = getattr(reason, 'errno', None)
        network = is_network_error(error)
        request_log('network_error' if network else 'request_error',
                    time.monotonic() - started, reason_type=type(reason).__name__,
                    errno=number if type(number) is int else None)
        if network:
            raise AsrNetworkError('ASR 网络连接失败或超时，请核对当前网络连接。') from None
        raise AsrError('ASR 请求失败，请核对服务连接配置。') from None
    try:
        text = (result['output']['text'] if body['model'] == NEW_MODEL
                else result['choices'][0]['message']['content'])
    except (KeyError, IndexError, TypeError):
        raise AsrError('ASR 返回结构不符合预期。') from None
    if not isinstance(text, str):
        raise AsrError('ASR 返回的识别文字格式无效。')
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
