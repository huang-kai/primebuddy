"""框架配置和技能私有数据；原子写入，避免中断留下半份配置。"""
import json
import os
from pathlib import Path
import re
import tempfile


class SetupError(ValueError):
    """可以直接展示给用户的、不含凭据的错误。"""


def home():
    return Path(os.environ.get('PRIMEBUDDY_HOME', Path.home() / '.local/share/primebuddy')).expanduser().resolve()


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
    path = Path(path)
    private_dir(path.parent)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def skill_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name):
        raise SetupError('技能名称须为小写字母、数字、下划线或连字符，以字母开头。')
    return name


def skill_data(name):
    return private_dir(home() / 'data' / skill_name(name))


def skill_config(name):
    return read_json(skill_data(name) / 'config.json')
