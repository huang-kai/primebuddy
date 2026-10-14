"""先配置 provider，再选择模型；凭据和候选列表集中在 providers.yaml。"""
import urllib.error

from primebuddy.config.storage import SetupError, application_directory, config_path, load_app_config, read_config, write_config
from primebuddy.providers import PROVIDERS, get_provider

ROLES = ('asr', 'llm', 'tts')


def provider_models(name, settings=None):
    """公开模板提供默认候选，私有配置可按能力替换列表。"""
    get_provider(name)
    defaults = read_config(application_directory() / 'cfg/providers.example.yaml', private=False).get(name, {})
    if settings is None:
        settings = read_config(config_path('providers.yaml')).get(name, {})
    if not isinstance(defaults, dict) or not isinstance(settings, dict):
        raise SetupError('provider 配置须为键值对象：' + name)
    models, overrides = defaults.get('models', {}), settings.get('models', {})
    if not isinstance(models, dict) or not isinstance(overrides, dict):
        raise SetupError('provider models 须按 asr、llm、tts 分组：' + name)
    models = {**models, **overrides}
    for role, choices in models.items():
        if role not in ROLES or not isinstance(choices, list) or not choices or any(
                not isinstance(model, str) or not model or model.strip() != model
                or ':' in model or any(c.isspace() for c in model) for model in choices):
            raise SetupError('provider 模型列表须为非空的模型名称列表：' + name)
        if len(choices) != len(set(choices)):
            raise SetupError('provider 模型列表不能重复：' + name)
    return models


def parse_model_reference(role, reference, providers=None):
    if not isinstance(reference, str) or reference.count(':') != 1:
        raise SetupError(role.upper() + ' 模型须使用 provider:model 格式。')
    provider, model = reference.split(':')
    settings = None if providers is None else providers.get(provider, {})
    if model not in provider_models(provider, settings).get(role, ()):
        raise SetupError(role.upper() + ' 模型尚未接入：' + reference)
    return provider, model


def migrate_model_config():
    previous = config_path('model.yaml')
    config_file, providers_file = config_path('config.yaml'), config_path('providers.yaml')
    if not previous.exists() or (config_file.exists() and providers_file.exists()):
        return
    old = read_config(previous)
    provider = old.get('provider', 'qianwen')
    config = load_app_config()
    for role, field in (('asr', 'model'), ('llm', 'llm_model'), ('tts', 'tts_model')):
        if not config_file.exists() and field in old:
            config[role]['model'] = provider + ':' + old[field]
        parse_model_reference(role, model_options(role, config).get('model'))
    if not config_file.exists() and 'tts_voice' in old:
        config['tts']['tts_voice'] = old['tts_voice']
    settings = {key: old[key] for key in ('api_host', 'api_key') if key in old}
    adapter = get_provider(provider)
    # 校验旧 provider 的凭据，不覆盖已经存在的新模型选择。
    adapter.build_config('asr', provider_models(provider, settings)['asr'][0], settings)
    if not config_file.exists():
        resolve_model('tts', config, {provider: settings})
    if not providers_file.exists():
        write_config(providers_file, {provider: settings})
    if not config_file.exists():
        write_config(config_file, config)
    previous.unlink()


def model_options(role, config):
    options = config.get(role)
    if isinstance(options, str):
        return {'model': options}
    if not isinstance(options, dict):
        raise SetupError(role.upper() + ' 配置须包含 model 字段。')
    return options


def resolve_model(role, config, providers, reference=None):
    options = model_options(role, config)
    provider, model = parse_model_reference(role, options.get('model') if reference is None else reference, providers)
    settings = providers.get(provider)
    if not isinstance(settings, dict):
        raise SetupError('provider 尚未配置：' + provider + '，请运行 primebuddy onboard。')
    return get_provider(provider).build_config(role, model, settings, {key: value for key, value in options.items() if key != 'model'})


def load_model(role='asr', override=None):
    migrate_model_config()
    config = load_app_config()
    providers = read_config(config_path('providers.yaml'))
    if override and ':' not in override:
        provider, _ = parse_model_reference(role, model_options(role, config).get('model'), providers)
        override = provider + ':' + override
    return resolve_model(role, config, providers, override)


def check_model(config, providers):
    """分别检查选定的三个模型；不录音、播放或连接机器人。"""
    result = {}
    for role in ROLES:
        try:
            model = resolve_model(role, config, providers)
            get_provider(model['provider']).check_connection(role, model)
            result[role.upper()] = 'ok'
        except urllib.error.HTTPError as error:
            result[role.upper()] = f'HTTP {error.code}，请检查地域、权限或配额'
        except Exception as error:
            result[role.upper()] = type(error).__name__ + '，请检查配置、网络、模型权限与配额'
    return result


def configure_model(ui):
    migrate_model_config()
    config = load_app_config()
    providers = read_config(config_path('providers.yaml'))
    defaults = read_config(application_directory() / 'cfg/providers.example.yaml', private=False)
    catalogs = {name: provider_models(name, providers.get(name, {})) for name in PROVIDERS}
    names = [name for name, models in catalogs.items() if all(models.get(role) for role in ROLES)]
    if not names:
        raise SetupError('没有同时配置 ASR、LLM、TTS 模型的 provider，请检查 providers.yaml。')
    while True:
        current_provider = model_options('asr', config).get('model', '').split(':', 1)[0]
        selected = ui.choose('模型 provider', names, default=names.index(current_provider) if current_provider in names else 0)
        provider = names[selected]
        previous = {**defaults.get(provider, {}), **providers.get(provider, {})}
        providers[provider] = {**previous, **get_provider(provider).configure(ui, previous), 'models': catalogs[provider]}
        try:
            # Host/Key 格式先校验，再进入三个模型的选择；这里不调用云端。
            get_provider(provider).build_config('asr', catalogs[provider]['asr'][0], providers[provider])
            for role in ROLES:
                models = catalogs[provider][role]
                current = model_options(role, config).get('model', '')
                references = [provider + ':' + model for model in models]
                selected = ui.choose(role.upper() + ' 模型', models, default=references.index(current) if current in references else 0)
                config[role]['model'] = references[selected]
            for role in ROLES:
                resolve_model(role, config, providers)
        except SetupError as error:
            ui.info(str(error))
            continue
        checks = {}
        if ui.confirm('现在分别验证 ASR、LLM、TTS？会产生少量 API 调用', default=True):
            checks = check_model(config, providers)
            for role, status in checks.items():
                ui.info(role + '：' + ('通过。' if status == 'ok' else status))
            if any(status != 'ok' for status in checks.values()):
                if ui.choose('部分能力未通过', ['修改并重试', '保存，稍后检查']) == 0:
                    continue
        write_config(config_path('providers.yaml'), providers)
        write_config(config_path('config.yaml'), config)
        ui.info('模型选择和 provider 配置已保存。' + ('三项检查通过。' if len(checks) == 3 and all(v == 'ok' for v in checks.values()) else '仍有能力待验证。'))
        return
