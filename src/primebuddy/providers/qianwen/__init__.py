"""千问连接配置；候选模型在 YAML 中，协议实现留在各能力模块。"""
import re
from primebuddy.config.storage import SetupError


def build_config(role, model, settings, options=None):
    host = settings.get('api_host', '')
    key = settings.get('api_key', '')
    if not isinstance(host, str):
        raise SetupError('API Host 必须是域名文本。')
    host = host.strip().removeprefix('https://').rstrip('/')
    if host != 'dashscope.aliyuncs.com' and not re.fullmatch(r'[a-zA-Z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com', host):
        raise SetupError('千问支持北京地域：dashscope.aliyuncs.com 或业务空间域名；只填域名。')
    if not isinstance(key, str) or not key or any(c.isspace() for c in key):
        raise SetupError('千问 API Key 不能为空或包含空白，请配置 providers.yaml。')
    endpoint = '/compatible-mode/v1/chat/completions'
    result = {'provider': 'qianwen', 'role': role, 'api_host': host, 'api_key': key, 'model': model}
    if role == 'tts':
        endpoint = '/api/v1/services/audio/tts/SpeechSynthesizer'
        voice = (options or {}).get('tts_voice', 'longanlingxin')
        if not isinstance(voice, str) or not voice.strip():
            raise SetupError('千问 TTS 音色不能为空。')
        result['voice'] = voice
    result['endpoint'] = 'https://' + host + endpoint
    return result


def configure(ui, previous):
    return {'api_host': ui.ask('千问北京地域 API Host', previous.get('api_host', 'dashscope.aliyuncs.com')),
            'api_key': ui.ask('千问 API Key', previous.get('api_key', ''), secret=True)}


def recognize(*args, **kwargs):
    from .asr import recognize as call
    return call(*args, **kwargs)


def transcribe_wav(*args, **kwargs):
    from .asr import transcribe_wav as call
    return call(*args, **kwargs)


def synthesize_wav(*args, **kwargs):
    from .tts import synthesize_wav as call
    return call(*args, **kwargs)


def complete(*args, **kwargs):
    from .llm import complete as call
    return call(*args, **kwargs)


def chat(*args, **kwargs):
    from .llm import chat as call
    return call(*args, **kwargs)


def check_connection(role, config):
    if role == 'asr':
        from .asr import SAMPLE
        recognize(SAMPLE, config)
    elif role == 'llm':
        complete([{'role': 'user', 'content': '请返回 JSON 对象 {"ok": true}。'}], config)
    else:
        synthesize_wav('连接成功。', config)
