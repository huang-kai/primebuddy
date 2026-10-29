"""按 SKILL.md 发现技能；按需读取说明、资源和原生工具。"""
from copy import deepcopy
import logging
import os
from pathlib import Path
import shutil

from primebuddy.config.storage import SetupError, skill_config
from primebuddy.skills.metadata import MAX_DOCUMENT, load_skill_metadata, resolve_skill_file
from primebuddy.skills.adapters import adapt_metadata, load_adapters

logger = logging.getLogger(__name__)


class Registry:
    def __init__(self, root=None):
        self.skills, self.modules, self.unavailable = {}, {}, {}
        adapters = load_adapters()
        if root is None:
            from primebuddy.skills.manager import discover_skill_records
            folders = []
            for name, record in discover_skill_records().items():
                if record.get('error'):
                    self._isolate(name, record.get('root'), record['error'])
                elif record['ready'] and record['enabled']:
                    folders.append(record['root'])
        else:
            folders = sorted(Path(root).iterdir()) if Path(root).is_dir() else []
        for folder in folders:
            if not folder.is_dir() or not (folder / 'SKILL.md').is_file():
                continue
            name = folder.name
            try:
                data = load_skill_metadata(folder)
                name = data['name']
                data = adapt_metadata(data, folder, adapters)
                if name in self.skills or name in self.unavailable:
                    raise SetupError('技能名重复：' + name)
                # 即使未声明配置前置要求，损坏的私有 YAML 也不能交给子进程。
                skill_config(name)
                if root is None and self.missing_requirements(data):
                    continue
                actions = dict(data['actions'])
                if 'script' in actions:
                    raise SetupError('script 为框架固定脚本动作，请为原生工具使用其他名称。')
                if not data.get('adapter') and not data['actions']:
                    actions['script'] = {'parameters': {'path': {'type': 'string'}, 'arguments': {'type': 'array'}},
                                         'resources': ['skill.scripts']}
                self.skills[name] = dict(data, root=folder.resolve(), actions=actions)
            except (SetupError, OSError) as error:
                self._isolate(name, folder, str(error))

    def _isolate(self, name, root, reason):
        self.skills.pop(name, None)
        self.unavailable[name] = {'root': root, 'reason': reason}
        logger.warning('技能 %s 已隔离，本次不加载：%s（目录：%s）。配置和数据已保留。', name, reason, root)

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
        from primebuddy.core.engine import CleanupUnconfirmed
        result = {}
        for skill in ([name] if name else self.skills):
            try:
                provider = getattr(self.load(skill), 'planning_context', None)
                if callable(provider):
                    value = provider(context)
                    if value is not None:
                        result[skill] = value
            except CleanupUnconfirmed:
                raise
            except Exception:
                result[skill] = {'status': 'unavailable'}
        return result

    def load(self, name):
        if name not in self.modules:
            from primebuddy.skills.runtime import SkillRuntime
            self.modules[name] = SkillRuntime(self.skills[name])
        return self.modules[name]

    def close(self):
        errors = []
        for runtime in self.modules.values():
            try:
                close = getattr(runtime, 'close', None)
                if callable(close):
                    close()
            except Exception as error:
                errors.append(type(error).__name__)
        if errors:
            raise SetupError('技能退出清理未完成：' + '、'.join(errors))

    def check_resources(self, context, resources):
        from primebuddy.core.engine import CleanupUnconfirmed
        from primebuddy.skills.runtime import _runtime
        import json
        normalize = lambda resource: 'speaker' if resource.startswith('speaker.') else resource
        requested = {normalize(resource) for resource in resources}
        for marker in _runtime(context).glob('*/cleanup-unconfirmed.json'):
            try:
                blocked = json.loads(marker.read_text())['resources']
                if not isinstance(blocked, list) or any(not isinstance(item, str) for item in blocked):
                    raise ValueError()
            except (OSError, ValueError, KeyError, TypeError):
                raise CleanupUnconfirmed('资源隔离记录不可读，未开始新任务。') from None
            if requested.intersection(normalize(resource) for resource in blocked):
                raise CleanupUnconfirmed('所需资源尚未确认释放。')

    def prepare_voice(self, context):
        from primebuddy.core.engine import CleanupUnconfirmed
        from primebuddy.skills.processes import process_cleanup_confirmed
        from primebuddy.skills.runtime import _runtime
        # 监督器被强杀时可能只有进程账本，尚未来得及写技能隔离记录。
        if not process_cleanup_confirmed(_runtime(context)):
            raise CleanupUnconfirmed('历史技能进程尚未确认退出，未启动语音会话。')
        # 在任何启动播报前发现上次强杀遗留的设备记录；这里只读查询，不重放操作。
        for name, skill in self.skills.items():
            resources = {resource for action in skill['actions'].values() for resource in action['resources']}
            if (skill.get('adapter', {}).get('status') and
                    any(resource == 'speaker' or resource.startswith('speaker.') for resource in resources)):
                self.load(name)._verify(context)
        self.check_resources(context, {'speaker'})

    def validate_args(self, skill, action, args):
        fields = self.skills[skill]['actions'][action]['parameters']
        if (not isinstance(args, dict) or set(args) - set(fields)
                or any(name not in args and 'default' not in rule for name, rule in fields.items())):
            raise ValueError('技能参数缺失或包含未知字段')
        for name, rule in fields.items():
            if name not in args:
                args[name] = deepcopy(rule['default'])
            value = args[name]
            expected = {'string': str, 'boolean': bool, 'integer': int, 'array': list}[rule['type']]
            if type(value) is not expected:
                raise ValueError('技能参数类型错误：' + name)
            if isinstance(value, str) and (len(value) > (200 if action == 'script' else 100) or any(ord(c) < 32 for c in value)):
                raise ValueError('技能文本参数无效：' + name)
            if 'enum' in rule and value not in rule['enum']:
                raise ValueError('技能参数取值错误：' + name)
        self.load(skill).validate(action, args)
