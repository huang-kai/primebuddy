"""有限任务 DAG：独立任务并行，依赖成功完成才启动后续任务。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import threading
import time
import random
from .voice_support.timing import measure


@dataclass
class Context:
    runtime: object = None
    skill_configs: dict = field(default_factory=dict)
    simulate: bool = True
    asr_model: str = 'qwen3-asr-flash'
    cancelled: threading.Event = field(default_factory=threading.Event)

    def config_for(self, name):
        return self.skill_configs.get(name, {})

    def checkpoint(self):
        if self.cancelled.is_set():
            raise RuntimeError('任务已取消')


def validate_plan(plan, registry):
    if not isinstance(plan, dict) or set(plan) != {'reply', 'tasks'}:
        raise ValueError('计划必须包含 reply 和 tasks')
    if not isinstance(plan['reply'], str) or len(plan['reply']) > 200:
        raise ValueError('回复格式无效')
    tasks = plan['tasks']
    if not isinstance(tasks, list) or len(tasks) > 8:
        raise ValueError('每轮最多执行八个任务')
    nodes = {}
    for task in tasks:
        if not isinstance(task, dict) or set(task) != {'id', 'skill', 'action', 'args', 'after'}:
            raise ValueError('任务字段无效')
        name = task['id']
        if not isinstance(name, str) or not name or len(name) > 40 or name in nodes:
            raise ValueError('任务编号无效或重复')
        if not isinstance(task['after'], list) or any(not isinstance(x, str) for x in task['after']):
            raise ValueError('任务依赖无效')
        if (not isinstance(task['skill'], str) or task['skill'] not in registry.skills
                or not isinstance(task['action'], str)
                or task['action'] not in registry.skills[task['skill']]['actions']):
            raise ValueError('未安装的技能或不支持的动作')
        registry.validate_args(task['skill'], task['action'], task['args'])
        nodes[name] = task
    ancestors = {}

    def visit(name, visiting):
        if name not in nodes or name in visiting:
            raise ValueError('任务依赖不存在或成环')
        if name not in ancestors:
            found = set()
            for dependency in nodes[name]['after']:
                found.add(dependency)
                found.update(visit(dependency, visiting | {name}))
            ancestors[name] = found
        return ancestors[name]

    for name in nodes:
        visit(name, set())
    # 同一资源只能由有明确先后关系的任务使用，避免重复播放、相反灯控竞争。
    for i, first in enumerate(tasks):
        first_resources = set(registry.skills[first['skill']]['actions'][first['action']]['resources'])
        for second in tasks[i + 1:]:
            second_resources = set(registry.skills[second['skill']]['actions'][second['action']]['resources'])
            ordered = first['id'] in ancestors[second['id']] or second['id'] in ancestors[first['id']]
            if first_resources & second_resources and not ordered:
                raise ValueError('同一资源的任务必须指定先后顺序')
    return plan


def execute(plan, registry, context, announce=print, emit=lambda event: None, checkpoint=lambda: None):
    validate_plan(plan, registry)
    tasks = plan['tasks']
    if not tasks:
        if plan['reply']:
            announce(plan['reply'])
        return {}
    modules = {t['skill']: registry.load(t['skill']) for t in tasks}
    for task in tasks:
        modules[task['skill']].preflight(task['action'], task['args'], context)
    pending, running, results = list(tasks), {}, {}

    def run(task):
        context.checkpoint()
        emit({'id': task['id'], 'state': 'running'})
        try:
            with measure('技能执行:' + task['skill'] + '.' + task['action']):
                result = modules[task['skill']].run(task['action'], task['args'], context)
            if (not isinstance(result, dict) or result.get('status') not in
                    {'success', 'failed', 'clarification_required', 'auth_required'}
                    or not isinstance(result.get('message'), str)):
                raise ValueError('技能结果不符合契约')
            return result
        except Exception as error:
            # 不输出异常正文，避免第三方错误携带 URL/token；不重试写入。
            return {'status': 'failed', 'message': task['skill'] + '执行失败，结果未确认。',
                    'error_type': type(error).__name__}

    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            while pending or running:
                checkpoint()
                context.checkpoint()
                for future, task in list(running.items()):
                    if future.done():
                        result = results[task['id']] = future.result()
                        emit({'id': task['id'], 'state': result['status']})
                        del running[future]
                        announce(result['message'])
                for task in list(pending):
                    deps = task['after']
                    if any(d in results and results[d]['status'] != 'success' for d in deps):
                        results[task['id']] = {'status': 'skipped', 'message': '前置任务未成功，后续任务未执行。'}
                        emit({'id': task['id'], 'state': 'skipped'})
                        announce(results[task['id']]['message'])
                        pending.remove(task)
                    elif all(d in results for d in deps):
                        # 主线程串行播报，成功后才提交该任务；禁止技能直接使用 TTS。
                        announce(random.choice(('好的，', '收到，', '明白，')) +
                                 modules[task['skill']].describe(task['action'], task['args']))
                        running[pool.submit(run, task)] = task
                        pending.remove(task)
                if pending or running:
                    time.sleep(.02)
        except BaseException:
            context.cancelled.set()
            raise
    return results
