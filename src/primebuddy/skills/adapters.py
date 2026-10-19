"""宿主侧可选 CLI 映射；普通 SKILL.md 和脚本无需适配清单。"""
from string import Formatter

from primebuddy.config.storage import application_directory, config_path, read_config, SetupError
from primebuddy.skills.metadata import resolve_skill_file


def load_adapters():
    defaults = read_config(application_directory() / 'cfg/skill-adapters.example.yaml', private=False)
    defaults.update(read_config(config_path('skill-adapters.yaml')))
    return defaults


def adapt_metadata(data, root, adapters=None):
    adapter = (load_adapters() if adapters is None else adapters).get(data['name'])
    if adapter is None:
        return data
    if not isinstance(adapter, dict):
        raise SetupError('技能 CLI 适配须为键值对象。')
    actions = adapter.get('actions', {})
    if not isinstance(actions, dict) or 'script' in actions:
        raise SetupError('CLI actions 无效，script 是保留动作。')
    for name, action in actions.items():
        if not isinstance(name, str) or not isinstance(action, dict):
            raise SetupError('CLI 动作声明无效。')
        fields = action.get('parameters', {})
        if (not isinstance(fields, dict) or set(fields).intersection({'config', 'data', 'runtime', 'request_text'})
                or any(not isinstance(rule, dict)
                or rule.get('type') not in ('string', 'boolean', 'integer') for rule in fields.values())):
            raise SetupError('CLI 参数声明无效。')
        if any('default' in rule and type(rule['default']) is not
               {'string': str, 'boolean': bool, 'integer': int}[rule['type']] for rule in fields.values()):
            raise SetupError('CLI 参数默认值类型错误。')
        resources = action.get('resources', ['skill.' + data['name']])
        if not isinstance(resources, list) or any(not isinstance(r, str) or not r for r in resources):
            raise SetupError('CLI 资源声明无效。')
        timeout = action.get('timeout_seconds', 600)
        if type(timeout) not in (int, float) or not 0 < timeout <= 86400:
            raise SetupError('CLI 硬超时须在 0～86400 秒之间。')
        grace = action.get('stop_grace_seconds', [3, 2, 2])
        if (not isinstance(grace, list) or len(grace) != 3
                or any(type(value) not in (int, float) or not 0 <= value <= 60 for value in grace)):
            raise SetupError('CLI 三段停止宽限须分别在 0～60 秒之间。')
        validate_command(root, action.get('command'), set(fields))
        description = action.get('description', '')
        try:
            if not isinstance(description, str):
                raise ValueError()
            for _, field, spec, conversion in Formatter().parse(description):
                if field is not None and (field not in fields or spec or conversion):
                    raise ValueError()
        except ValueError:
            raise SetupError('CLI 动作说明模板无效。') from None
        for key in ('preflight', 'confirmation'):
            if key in action:
                validate_command(root, action[key], set(fields) | {'request_text'})
    for key in ('configure', 'context', 'status', 'recover'):
        if key in adapter:
            validate_command(root, adapter[key], set())
    result = dict(data, adapter=adapter, actions={name: dict(action,
                  resources=action.get('resources', ['skill.' + data['name']])) for name, action in actions.items()})
    # 交互式配置同样使用技能自己的普通命令。
    result.pop('setup', None)
    result.pop('entrypoint', None)
    if 'configure' in adapter:
        result['configure'] = adapter['configure']
    return result


def validate_command(root, command, parameters):
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) or '\x00' in arg for arg in command):
        raise SetupError('CLI command 须为脚本路径和参数列表。')
    resolve_skill_file(root, command[0])
    if '{' in command[0] or '}' in command[0]:
        raise SetupError('CLI 脚本路径不能由模型参数指定。')
    allowed = parameters | {'config', 'data', 'runtime'}
    try:
        for argument in command[1:]:
            for _, field, spec, conversion in Formatter().parse(argument):
                if field is not None and (field not in allowed or spec or conversion):
                    raise ValueError()
    except ValueError:
        raise SetupError('CLI 参数模板只允许已声明的简单占位符。') from None


def command_arguments(command, values):
    values = {name: ('true' if value is True else 'false' if value is False else str(value))
              for name, value in values.items()}
    return command[0], [argument.format_map(values) for argument in command[1:]]
