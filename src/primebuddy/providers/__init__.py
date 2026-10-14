"""按已注册 provider 分发模型调用；配置不能指定任意 Python 模块。"""
from importlib import import_module
from primebuddy.config.storage import SetupError

PROVIDERS = {'qianwen': 'primebuddy.providers.qianwen'}


def get_provider(name):
    if name not in PROVIDERS:
        raise SetupError('未安装模型 provider：' + name)
    return import_module(PROVIDERS[name])


def recognize(audio, config, model=None, vocabulary=None):
    return get_provider(config['provider']).recognize(audio, config, model, vocabulary)


def transcribe_wav(data, config=None, model=None, vocabulary=None):
    from primebuddy.config.models import load_model
    config = load_model('asr', model) if model or config is None else config
    return get_provider(config['provider']).transcribe_wav(data, config, vocabulary=vocabulary)


def synthesize_wav(text, config=None):
    from primebuddy.config.models import load_model
    config = load_model('tts') if config is None else config
    return get_provider(config['provider']).synthesize_wav(text, config)


def chat(messages, tools, config=None):
    from primebuddy.config.models import load_model
    config = load_model('llm') if config is None else config
    provider = get_provider(config['provider'])
    if not callable(getattr(provider, 'chat', None)):
        raise SetupError('当前 LLM provider 尚未接入工具调用。')
    return provider.chat(messages, tools, config)
