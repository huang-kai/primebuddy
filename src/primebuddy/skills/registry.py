"""按 SKILL.md 发现技能；按需读取说明、资源和原生工具。"""
import os
from pathlib import Path
import shutil

from primebuddy.config.storage import SetupError, skill_config
from primebuddy.skills.metadata import MAX_DOCUMENT, load_skill_metadata, resolve_skill_file


class Registry:
    def __init__(self, root=None):
        self.skills, self.modules = {}, {}
        if root is None:
            from primebuddy.skills.manager import discover_skill_records
            folders = [record['root'] for record in discover_skill_records().values()
                       if record['ready'] and record['enabled']]
        else:
            folders = sorted(Path(root).iterdir()) if Path(root).is_dir() else []
        for folder in folders:
            if not folder.is_dir() or not (folder / 'SKILL.md').is_file():
                continue
            data = load_skill_metadata(folder)
            name = data['name']
            if name in self.skills:
                raise SetupError('技能名重复：' + name)
            if root is None and self.missing_requirements(data):
                continue
            # 脚本资源默认串行；原生工具沿用明确声明的资源。
            actions = dict(data['actions'])
            if 'script' in actions:
                raise SetupError('script 为框架固定脚本动作，请为原生工具使用其他名称。')
            actions['script'] = {'parameters': {'path': {'type': 'string'}, 'arguments': {'type': 'array'}},
                                 'resources': ['skill.scripts']}
            self.skills[name] = dict(data, root=folder.resolve(), actions=actions)

    @staticmethod
    def missing_requirements(skill):
        requires = skill['requires']
        config = skill_config(skill['name']) if requires.get('config') else {}
        return ([name for name in requires.get('bins', []) if not shutil.which(name)]
                + [name for name in requires.get('env', []) if not os.environ.get(name)]
                + [name for name in requires.get('config', []) if name not in config or config[name] in (None, '')])

    def catalog(self):
        # 首轮仅提供发现信息；动作定义和正文在读取技能后提供。
        return [{'name': skill['name'], 'description': skill['description'], 'location': 'SKILL.md'}
                for skill in self.skills.values()]

    def read_file(self, name, relative='SKILL.md'):
        path = resolve_skill_file(self.skills[name]['root'], relative)
        if path.stat().st_size > MAX_DOCUMENT:
            raise SetupError('技能文件超过 64 KiB。')
        content = path.read_text(encoding='utf-8')
        if relative == 'SKILL.md':
            return {'instructions': content, 'actions': self.skills[name]['actions']}
        return {'content': content}

    def planning_context(self, context, name=None):
        result = {}
        for skill in ([name] if name else self.skills):
            provider = getattr(self.load(skill), 'planning_context', None)
            if callable(provider):
                try:
                    result[skill] = provider(context)
                except Exception:
                    result[skill] = {'status': 'unavailable'}
        return result

    def load(self, name):
        if name not in self.modules:
            from primebuddy.skills.runtime import SkillRuntime
            self.modules[name] = SkillRuntime(self.skills[name])
        return self.modules[name]

    def validate_args(self, skill, action, args):
        fields = self.skills[skill]['actions'][action]['parameters']
        if not isinstance(args, dict) or set(args) != set(fields):
            raise ValueError('技能参数缺失或包含未知字段')
        for name, rule in fields.items():
            value = args[name]
            expected = {'string': str, 'boolean': bool, 'integer': int, 'array': list}[rule['type']]
            if type(value) is not expected:
                raise ValueError('技能参数类型错误：' + name)
            if isinstance(value, str) and (len(value) > (200 if action == 'script' else 100) or any(ord(c) < 32 for c in value)):
                raise ValueError('技能文本参数无效：' + name)
            if 'enum' in rule and value not in rule['enum']:
                raise ValueError('技能参数取值错误：' + name)
        self.load(skill).validate(action, args)
