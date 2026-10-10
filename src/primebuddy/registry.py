"""从本地可信技能目录加载契约；模型不能指定可执行路径。"""
from pathlib import Path


class Registry:
    def __init__(self, root=None):
        self.skills = {}
        self.modules = {}
        if root is None:
            from .skill_manager import records, package_path
            folders = [package_path(r) for r in records().values() if r['ready'] and r['enabled']]
        else:
            # 显式目录只供开发模拟使用，正式运行按安装状态加载。
            folders = sorted(Path(root).iterdir())
        from .skill_manager import manifest
        for folder in folders:
            if not folder.is_dir() or not (folder / 'SKILL.md').is_file():
                continue
            data = manifest(folder)
            name = data['name']
            if name in self.skills:
                raise ValueError('技能名重复')
            self.skills[name] = dict(data, root=folder,
                                     instructions=(folder / 'SKILL.md').read_text())

    def catalog(self):
        return [{k: skill[k] for k in ('name', 'description', 'actions')}
                for skill in self.skills.values()]

    def planning_context(self, context):
        result = {}
        for name in self.skills:
            provider = getattr(self.load(name), 'planning_context', None)
            if provider is not None:
                try:
                    result[name] = provider(context)
                except Exception:
                    result[name] = {'status': 'unavailable'}
        return result

    def load(self, name):
        if name in self.modules:
            return self.modules[name]
        from .skill_manager import load_entry
        skill = self.skills[name]
        module = load_entry(skill['root'], skill['entrypoint'])
        self.modules[name] = module
        return module

    def validate_args(self, skill, action, args):
        definition = self.skills[skill]['actions'][action]
        fields = definition['parameters']
        if not isinstance(args, dict) or set(args) != set(fields):
            raise ValueError('技能参数缺失或包含未知字段')
        for name, rule in fields.items():
            value = args[name]
            expected = {'string': str, 'boolean': bool, 'integer': int}[rule['type']]
            if type(value) is not expected:
                raise ValueError('技能参数类型错误：' + name)
            if isinstance(value, str) and (len(value) > 100 or any(ord(c) < 32 for c in value)):
                raise ValueError('技能文本参数无效：' + name)
            if 'enum' in rule and value not in rule['enum']:
                raise ValueError('技能参数取值错误：' + name)
        # 各技能自己负责跨字段的业务校验，必须在整份计划执行前完成。
        self.load(skill).validate(action, args)
