"""有限任务 DAG：独立任务并行，依赖成功完成才启动后续任务。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
import threading
import time
import random
import uuid
from primebuddy.core.timing import measure


class TurnInterrupted(RuntimeError):
    """调用方中断当前轮；返回前必须完成业务清理。"""


class CleanupUnconfirmed(RuntimeError):
    """进程或设备清理未确认，禁止继续占用相同资源及播报。"""


@dataclass
class Context:
    runtime: object = None
    skill_configs: dict = field(default_factory=dict)
    simulate: bool = True
    asr_model: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    owner_id: str = ''
    task_id: str = ''

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
        first_resources = {r.split('.')[0] if r.startswith('speaker.') else r
                           for r in registry.skills[first['skill']]['actions'][first['action']]['resources']}
        for second in tasks[i + 1:]:
            second_resources = {r.split('.')[0] if r.startswith('speaker.') else r
                                for r in registry.skills[second['skill']]['actions'][second['action']]['resources']}
            ordered = first['id'] in ancestors[second['id']] or second['id'] in ancestors[first['id']]
            if first_resources & second_resources and not ordered:
                raise ValueError('同一资源的任务必须指定先后顺序')
    return plan


def execute(plan, registry, context, announce=print, emit=lambda event: None, checkpoint=lambda: None,
            cleanup_checkpoint=lambda: None):
    validate_plan(plan, registry)
    tasks = plan['tasks']
    if not tasks:
        if plan['reply']:
            announce(plan['reply'])
        return {}
    modules = {task['skill']: registry.load(task['skill']) for task in tasks}
    owner = uuid.uuid4().hex
    contexts = {task['id']: replace(context, owner_id=owner, task_id=task['id']) for task in tasks}
    for task in tasks:
        modules[task['skill']].preflight(task['action'], task['args'], contexts[task['id']])
    if not context.simulate:
        registry.check_resources(context, {resource for task in tasks
            for resource in registry.skills[task['skill']]['actions'][task['action']]['resources']})
    pending, running, results = list(tasks), {}, {}
    futures = []
    deferred_announcements = []
    failure = None

    def check():
        checkpoint()
        context.checkpoint()

    def speaker_busy():
        return any(any(resource == 'speaker' or resource.startswith('speaker.')
                       for resource in registry.skills[task['skill']]['actions'][task['action']]['resources'])
                   for task in running.values())

    def run(task):
        child_context = contexts[task['id']]
        child_context.checkpoint()
        emit({'id': task['id'], 'state': 'running'})
        try:
            with measure('技能执行:' + task['skill'] + '.' + task['action']):
                result = modules[task['skill']].run(task['action'], task['args'], child_context)
            if (not isinstance(result, dict) or result.get('status') not in
                    {'success', 'failed', 'clarification_required', 'auth_required'}
                    or not isinstance(result.get('message'), str)):
                raise ValueError('技能结果不符合契约')
            return result
        except CleanupUnconfirmed:
            return {'status': 'failed', 'message': '技能清理未确认，相关资源已隔离。',
                    'cleanup_confirmed': False}
        except Exception as error:
            return {'status': 'failed', 'message': task['skill'] + '执行失败，结果未确认。',
                    'error_type': type(error).__name__}

    with ThreadPoolExecutor(max_workers=4) as pool:
        try:
            while pending or running:
                check()
                for future, task in list(running.items()):
                    if future.done():
                        result = results[task['id']] = future.result()
                        if result.get('cleanup_confirmed') is False:
                            raise CleanupUnconfirmed('技能或设备清理未确认。')
                        emit({'id': task['id'], 'state': result['status']})
                        del running[future]
                        deferred_announcements.append(result['message'])
                if not speaker_busy():
                    for message in deferred_announcements:
                        check()
                        announce(message)
                    deferred_announcements.clear()
                ready = []
                for task in list(pending):
                    dependencies = task['after']
                    if any(dep in results and results[dep]['status'] != 'success' for dep in dependencies):
                        results[task['id']] = {'status': 'skipped', 'message': '前置任务未成功，后续任务未执行。'}
                        emit({'id': task['id'], 'state': 'skipped'})
                        deferred_announcements.append(results[task['id']]['message'])
                        pending.remove(task)
                    elif not speaker_busy() and all(dep in results for dep in dependencies):
                        ready.append(task)
                # 先完成整批提示，再允许业务程序占用扬声器。
                for task in ready:
                    check()
                    announce(random.choice(('好的，', '收到，', '明白，')) +
                             modules[task['skill']].describe(task['action'], task['args']))
                for task in ready:
                    check()
                    future = pool.submit(run, task)
                    running[future] = task
                    futures.append(future)
                    pending.remove(task)
                if pending or running:
                    time.sleep(.02)
            for message in deferred_announcements:
                check()
                announce(message)
        except BaseException as error:
            context.cancelled.set()
            failure = error
            # 旧任务正在独立进程中有界清理；主线程仍须泵 ROS，把新 generation
            # 的 VAD 留在麦克风缓存。此阶段不使用会再次识别/取消的正常 checkpoint。
            pump = cleanup_checkpoint
            while any(not future.done() for future in futures):
                try:
                    pump()
                except BaseException as cleanup_error:
                    failure = cleanup_error
                    pump = lambda: None
                time.sleep(.02)
        # pool 等待的是受监督子进程的有界取消和业务恢复，不能在清理前开启新轮。
    for future in futures:
        if future.done() and not future.cancelled():
            try:
                result = future.result()
            except Exception:
                continue
            if result.get('cleanup_confirmed') is False:
                raise CleanupUnconfirmed('旧轮清理未确认，未启动新轮。') from failure
    if failure is not None:
        raise failure
    return results
