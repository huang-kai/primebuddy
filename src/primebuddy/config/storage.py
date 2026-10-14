"""框架配置和技能私有数据；原子写入，避免中断留下半份配置。"""
import json
import os
from pathlib import Path
import re
import tempfile

import yaml


class SetupError(ValueError):
    """可以直接展示给用户的、不含凭据的错误。"""


class ConfigLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key, value in self.construct_pairs(node, deep=deep):
            # 重复键不能静默覆盖，避免手改配置后实际生效的值不明确。
            if not isinstance(key, str) or key in result:
                raise yaml.constructor.ConstructorError(None, None, '配置键必须是唯一的文本', node.start_mark)
            result[key] = value
        return result


def get_data_directory():
    return Path(os.environ.get('PRIMEBUDDY_HOME', Path.home() / '.primebuddy')).expanduser().resolve()


def application_directory():
    return Path(__file__).resolve().parents[3]


def load_app_config():
    config = read_config(application_directory() / 'cfg/config.example.yaml', private=False)
    for key, value in read_config(config_path('config.yaml')).items():
        # 兼容旧的 provider:model 文本；当前文件按能力分组，读取不改写用户注释。
        if key in ('asr', 'llm', 'tts') and isinstance(value, str):
            value = {'model': value}
        if isinstance(config.get(key), dict) and isinstance(value, dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def config_path(filename, skill=None):
    """迁移旧私有 YAML 配置；运行数据与登录态留在原目录。"""
    root = get_data_directory()
    if skill is None:
        target, previous = root / 'cfg' / filename, root / filename
    else:
        name = skill_name(skill)
        target = root / 'cfg/skills' / name / filename
        previous = root / 'data' / name / filename
    if not target.exists() and previous.exists():
        read_config(previous)
        private_dir(target.parent)
        os.replace(previous, target)
    return target


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return {} if default is None else default
    if path.stat().st_mode & 0o077:
        raise SetupError('配置权限过宽，请将文件权限设为 600：' + str(path))
    try:
        value = json.loads(path.read_text())
    except (ValueError, OSError):
        raise SetupError('配置无法读取，请检查：' + str(path)) from None
    if not isinstance(value, dict):
        raise SetupError('配置必须是 JSON 对象：' + str(path))
    return value


def write_json(path, value):
    _write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2))


def read_config(path, default=None, *, private=True):
    path = Path(path)
    if not path.exists():
        return {} if default is None else default
    if private and path.stat().st_mode & 0o077:
        raise SetupError('配置权限过宽，请将文件权限设为 600：' + str(path))
    try:
        return parse_config(path.read_text(encoding='utf-8'), path)
    except (OSError, UnicodeError):
        raise SetupError('配置无法读取，请检查：' + str(path)) from None


def parse_config(content, path):
    try:
        value = yaml.load(content, Loader=ConfigLoader)
    except yaml.YAMLError as error:
        mark = getattr(error, 'problem_mark', None)
        position = f'（第 {mark.line + 1} 行，第 {mark.column + 1} 列）' if mark else ''
        raise SetupError('YAML 配置格式错误' + position + '：' + str(path)) from None
    if not isinstance(value, dict):
        raise SetupError('YAML 配置必须是键值对象：' + str(path))
    return value


def write_config(path, value):
    _write_atomic(path, yaml.safe_dump(value, allow_unicode=True, sort_keys=False, default_flow_style=False))


def _write_atomic(path, content):
    path = Path(path)
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def skill_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
        raise SetupError('名称须为小写字母、数字、下划线或连字符。')
    return name


def skill_data(name):
    return private_dir(get_data_directory() / 'data' / skill_name(name))


def skill_config(name):
    return read_config(config_path('config.yaml', skill=name))
