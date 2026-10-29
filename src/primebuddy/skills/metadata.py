"""Agent Skills 文档是发现入口；平台适配和安装依赖均为可选。"""
from pathlib import Path
import re

from primebuddy.config.storage import SetupError, parse_config, read_config, skill_name

MAX_DOCUMENT = 65536


def validate_interaction(action):
    if 'interaction' in action and action['interaction'] != 'exclusive':
        raise SetupError('interaction 仅支持 exclusive；普通动作不声明该字段。')


def parse_skill_document(content, path):
    if len(content.encode('utf-8')) > MAX_DOCUMENT:
        raise SetupError('技能说明超过 64 KiB：' + str(path))
    lines = content.removeprefix('\ufeff').splitlines()
    if not lines or lines[0].strip() != '---':
        raise SetupError('SKILL.md 须以包含 name 和 description 的 YAML frontmatter 开始。')
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        raise SetupError('SKILL.md 的 YAML frontmatter 未结束。')
    metadata = parse_config('\n'.join(lines[1:end]), path)
    name = metadata.get('name')
    if (not isinstance(name, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name)
            or len(name) > 64):
        raise SetupError('技能 name 须为 1～64 个小写字母、数字或单连字符。')
    skill_name(name)
    description = metadata.get('description')
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise SetupError('技能 description 须为 1～1024 字的说明。')
    return metadata, '\n'.join(lines[end + 1:]).strip()


def resolve_skill_file(root, relative):
    root = Path(root)
    if (not isinstance(relative, str) or not relative or '\\' in relative
            or Path(relative).is_absolute() or '..' in Path(relative).parts
            or any(part in ('.git', '_deps', '__pycache__') for part in Path(relative).parts)):
        raise SetupError('技能文件路径无效。')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise SetupError('技能文件缺失或超出技能目录。')
    return path


def load_skill_metadata(root):
    root = Path(root)
    try:
        path = resolve_skill_file(root, 'SKILL.md')
        if path.stat().st_size > MAX_DOCUMENT:
            raise SetupError('技能说明超过 64 KiB：' + str(path))
        document = path.read_text(encoding='utf-8')
        frontmatter, instructions = parse_skill_document(document, root / 'SKILL.md')
        package = read_config(root / 'cfg/pkg.yaml', private=False)
        if package.get('package_name', frontmatter['name']) != frontmatter['name']:
            raise SetupError('cfg/pkg.yaml 的 package_name 与 SKILL.md 的 name 不一致。')
        extra = frontmatter.get('metadata', {})
        if not isinstance(extra, dict):
            raise SetupError('SKILL.md 的 metadata 须为键值对象。')
        version = package.get('version', extra.get('version', '0.0.0'))
        if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise SetupError('技能 version 须为三段数字；不声明时使用 0.0.0。')
        options = package.get('primebuddy', {})
        if not isinstance(options, dict):
            raise SetupError('primebuddy 可选适配须为键值对象。')
        protocol = options.get('version')
        if 'version' in options:
            if type(protocol) is not int or protocol != 1:
                raise SetupError('不支持的技能 CLI 协议版本；当前支持 version: 1。')
            cli = options.get('cli')
            if not isinstance(cli, dict) or not cli.get('configure'):
                raise SetupError('协议 v1 技能必须提供 primebuddy.cli.configure 安装向导入口。')
            # 在下载依赖或替换已有安装前检查脚本、路径和参数模板。
            from primebuddy.skills.adapters import validate_command
            validate_command(root, cli['configure'], set())
        mode = options.get('mode', 'task')
        if not isinstance(mode, str) or mode not in ('task', 'exclusive'):
            raise SetupError('primebuddy.mode 仅支持 task 或 exclusive。')
        if 'cli' in options:
            if not isinstance(options['cli'], dict):
                raise SetupError('primebuddy.cli 须为命令映射对象。')
            if any(key in options for key in ('entrypoint', 'actions', 'setup', 'configure')):
                raise SetupError('primebuddy.cli 不能与旧原生入口或配置声明混用。')
        if type(options.get('api_version', 1)) is not int or options.get('api_version', 1) != 1:
            raise SetupError('不支持的 PrimeBuddy 原生工具适配版本。')
        data = dict(options, cli_version=protocol, name=frontmatter['name'], description=frontmatter['description'],
                    version=version, mode=mode, instructions=instructions, document=document, package=package)
        for field in ('entrypoint', 'setup'):
            if field in data:
                resolve_skill_file(root, data[field])
        configure = data.get('configure')
        if configure is not None and data.get('setup'):
            raise SetupError('configure 与 setup 只需声明一种配置方式。')
        if configure is not None:
            if (not isinstance(configure, list) or not configure
                    or any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in configure)):
                raise SetupError('configure 须为 Console 命令参数列表。')
            # 配置命令由作者声明，首项是技能内脚本；不交给 shell 解释。
            resolve_skill_file(root, configure[0])
        actions = data.setdefault('actions', {})
        if not isinstance(actions, dict):
            raise SetupError('原生 actions 须为键值对象。')
        for name, action in actions.items():
            skill_name(name)
            if not isinstance(action, dict) or not isinstance(action.get('parameters'), dict):
                raise SetupError('原生工具须声明参数。')
            validate_interaction(action)
            resources = action.setdefault('resources', ['skill.' + data['name']])
            if not isinstance(resources, list) or any(not isinstance(r, str) or not r for r in resources):
                raise SetupError('技能资源声明无效。')
            for parameter, rule in action['parameters'].items():
                skill_name(parameter)
                if not isinstance(rule, dict) or rule.get('type') not in ('string', 'boolean', 'integer'):
                    raise SetupError('不支持的原生工具参数类型。')
                if 'enum' in rule and (not isinstance(rule['enum'], list) or not rule['enum']):
                    raise SetupError('技能枚举声明无效。')
            if not isinstance(action.get('handler', name), str) or not action.get('handler', name).isidentifier():
                raise SetupError('原生工具 handler 须为函数名称。')
        if actions and 'entrypoint' not in data:
            raise SetupError('声明原生 actions 时须提供 entrypoint。')
        requires = data.setdefault('requires', {})
        if not isinstance(requires, dict) or set(requires) - {'bins', 'env', 'config'}:
            raise SetupError('requires 仅支持 bins、env 和 config。')
        for values in requires.values():
            if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                raise SetupError('技能前置要求须为文本列表。')
        requirements = root / 'requirements.txt'
        dependencies = [line.split('#', 1)[0].strip() for line in
                        (resolve_skill_file(root, 'requirements.txt').read_text().splitlines() if requirements.exists() else [])]
        data['dependencies'] = [line for line in dependencies if line]
        if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9_.+-]+', d)
               for d in data['dependencies']):
            raise SetupError('requirements.txt 须使用包名==固定版本，不接受安装选项或 URL。')
        return data
    except (OSError, UnicodeError):
        raise SetupError('技能文档无法读取：' + str(root)) from None
