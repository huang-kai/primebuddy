"""公开发行清单：框架版本、官方技能 CLI 兼容范围及可选下载信息。"""
import json
from pathlib import Path
import re

from primebuddy.config.storage import SetupError, application_directory


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise SetupError('版本须为三段数字。')
    return tuple(map(int, value.split('.')))


def release_url(version, filename):
    return 'https://github.com/huang-kai/primebuddy/releases/download/v' + version + '/' + filename


def load_release(path=None):
    path = Path(path) if path is not None else application_directory() / 'release.json'
    try:
        return validate_release(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, UnicodeError, ValueError):
        raise SetupError('发行清单无法读取或格式无效，请使用完整的发布包：' + str(path)) from None


def validate_release(data):
    try:
        if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data['schema_version'] != 1:
            raise ValueError
        version_tuple(data['version'])
        if not isinstance(data['skills'], dict):
            raise ValueError
        if 'bundled_skills' in data:
            bundled = data['bundled_skills']
            if (not isinstance(bundled, list) or any(not isinstance(name, str) for name in bundled)
                    or len(bundled) != len(set(bundled)) or any(name not in data['skills'] for name in bundled)):
                raise ValueError
        for name, rule in data['skills'].items():
            if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or not isinstance(rule, dict):
                raise ValueError
            low, high, recommended = (version_tuple(rule[key]) for key in
                                      ('min_version', 'max_version', 'recommended_version'))
            if not low <= recommended < high:
                raise ValueError
            if 'artifact' in rule:
                _check_artifact(rule['artifact'], name + '-' + rule['recommended_version'] + '.rpk', data['version'])
        if 'framework' in data:
            _check_artifact(data['framework'], 'primebuddy-' + data['version'] + '.rpk', data['version'])
        if 'framework_core' in data:
            _check_artifact(data['framework_core'], 'primebuddy-' + data['version'] + '-core.rpk', data['version'])
        return data
    except (ValueError, KeyError, TypeError):
        raise SetupError('发行清单格式无效，请使用完整的发布包。') from None


def _check_artifact(artifact, filename, version):
    if (not isinstance(artifact, dict) or artifact.get('filename') != filename
            or artifact.get('url') != release_url(version, filename)
            or not isinstance(artifact.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', artifact['sha256'])):
        raise ValueError


def check_skill(data, manifest=None):
    manifest = load_release() if manifest is None else manifest
    rule = manifest['skills'].get(data['name'])
    if rule is None:
        return
    try:
        supported = version_tuple(rule['min_version']) <= version_tuple(data['version']) < version_tuple(rule['max_version'])
    except (KeyError, SetupError):
        supported = False
    if not supported:
        raise SetupError(data['name'] + ' ' + str(data.get('version', '未知'))
                         + ' 与 PrimeBuddy ' + manifest['version'] + ' 不兼容；需要 >='
                         + rule['min_version'] + '、<' + rule['max_version']
                         + '，请升级到推荐版本 ' + rule['recommended_version'] + '。')
