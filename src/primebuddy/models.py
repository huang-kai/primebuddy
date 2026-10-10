"""首版千问服务配置：统一凭据，按能力路由到各自协议。"""
import re
import urllib.error

from .settings import SetupError, home, read_json, write_json

ASR_MODEL = 'qwen3-asr-flash'
LLM_MODEL = 'qwen-plus'
TTS_MODEL = 'qwen-audio-3.0-tts-plus'
TTS_VOICE = 'longanlingxin'
TTS_PATH = '/api/v1/services/audio/tts/SpeechSynthesizer'
TTS_ENDPOINT = 'https://dashscope.aliyuncs.com' + TTS_PATH


def qianwen_config(host, key):
    if not isinstance(host, str):
        raise SetupError('API Host 必须是域名文本。')
    host = host.strip().removeprefix('https://').rstrip('/')
    if host != 'dashscope.aliyuncs.com' and not re.fullmatch(r'[a-zA-Z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com', host):
        raise SetupError('首版支持北京地域：dashscope.aliyuncs.com 或业务空间的 *.cn-beijing.maas.aliyuncs.com；只填域名。')
    if not isinstance(key, str) or not key or any(c.isspace() for c in key):
        raise SetupError('API Key 不能为空或包含空白。')
    return {'provider': 'qianwen', 'api_host': host, 'api_key': key,
            'endpoint': f'https://{host}/compatible-mode/v1/chat/completions',
            'model': ASR_MODEL, 'llm_model': LLM_MODEL,
            'tts_endpoint': f'https://{host}{TTS_PATH}', 'tts_model': TTS_MODEL, 'tts_voice': TTS_VOICE}


def load_model():
    saved = read_json(home() / 'model.json')
    if not saved:
        return None
    if saved.get('provider') != 'qianwen':
        raise SetupError('不支持此模型服务，请运行 primebuddy onboard。')
    return qianwen_config(saved.get('api_host', ''), saved.get('api_key', ''))


def check_model(config):
    """分别发起最小请求；不播放、不录音、不连接机器人。"""
    from .voice_support.qwen_asr import recognize, SAMPLE
    from .voice_support.qwen_tts import synthesize_wav
    from .planner import plan
    class EmptyRegistry:
        skills = {}
        def catalog(self):
            return []
    checks = {
        'ASR': lambda: recognize(SAMPLE, config),
        'LLM': lambda: plan('没有技能时请返回空任务列表。', EmptyRegistry(), config),
        'TTS': lambda: synthesize_wav('连接成功。', config),
    }
    result = {}
    for name, check in checks.items():
        try:
            check()
            result[name] = 'ok'
        except urllib.error.HTTPError as error:
            result[name] = f'HTTP {error.code}，请检查地域、权限或配额'
        except Exception as error:
            # 不打印上游异常正文，避免 URL、密钥或服务器响应进入安装日志。
            result[name] = type(error).__name__ + '，请检查网络、地域、模型权限与配额'
    return result


def configure_model(ui):
    ui.choose('模型服务（ASR / LLM / TTS 共用凭据）', ['千问 Qianwen'])
    try:
        previous = load_model() or {}
    except SetupError:
        ui.info('已有模型配置无效，请重新填写。')
        previous = {}
    while True:
        host = ui.ask('北京地域 API Host', previous.get('api_host', 'dashscope.aliyuncs.com'))
        key = ui.ask('API Key', previous.get('api_key', ''), secret=True)
        try:
            config = qianwen_config(host, key)
        except SetupError as error:
            ui.info(str(error))
            continue
        checks = {}
        if ui.confirm('现在分别验证 ASR、LLM、TTS？会产生少量 API 调用', default=True):
            ui.info('正在检查三项云端能力，不会播放音频。')
            checks = check_model(config)
            for name, status in checks.items():
                ui.info(f'{name}：' + ('通过' if status == 'ok' else status))
            if any(status != 'ok' for status in checks.values()):
                if ui.choose('部分能力未通过', ['修改并重试', '保存，稍后检查']) == 0:
                    previous = config
                    continue
        write_json(home() / 'model.json', dict(config, checks=checks))
        ui.info('千问配置已保存。' + ('三项检查通过。' if len(checks) == 3 and all(v == 'ok' for v in checks.values()) else '仍有能力待验证。'))
        return
