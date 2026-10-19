"""旧原生 skill 的一次性兼容进程；普通 CLI skill 不使用此协议。"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading

from primebuddy.skills.metadata import load_skill_metadata, resolve_skill_file


@dataclass
class Context:
    runtime: Path | None = None
    skill_configs: dict = field(default_factory=dict)
    simulate: bool = True
    cancelled: threading.Event = field(default_factory=threading.Event)

    def config_for(self, name):
        return self.skill_configs.get(name, {})

    def checkpoint(self):
        if self.cancelled.is_set():
            raise RuntimeError('任务已取消')


def execute(request, cancelled):
    if not isinstance(request, dict) or not isinstance(request.get('root'), str):
        raise ValueError('原生适配请求无效。')
    root = Path(request['root']).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('技能目录无效。')
    data = load_skill_metadata(root)
    entrypoint = request.get('entrypoint')
    if entrypoint != data.get('entrypoint') or not isinstance(entrypoint, str):
        raise ValueError('原生入口与技能声明不一致。')
    resolve_skill_file(root, entrypoint)
    operation = request.get('operation')
    if operation not in ('validate', 'preflight', 'describe', 'planning_context', 'confirmation', 'run'):
        raise ValueError('原生适配操作无效。')
    args = request.get('args', {})
    raw_context = request.get('context', {})
    if not isinstance(args, dict) or not isinstance(raw_context, dict):
        raise ValueError('原生适配参数无效。')
    runtime = raw_context.get('runtime')
    configs = raw_context.get('skill_configs', {})
    simulate = raw_context.get('simulate', True)
    if ((runtime is not None and not isinstance(runtime, str))
            or not isinstance(configs, dict) or type(simulate) is not bool):
        raise ValueError('原生适配上下文无效。')
    context = Context(Path(runtime) if runtime is not None else None, configs, simulate, cancelled)
    action = request.get('action')
    handler = None
    if operation != 'planning_context':
        if not isinstance(action, str) or action not in data['actions']:
            raise ValueError('未声明的原生动作。')
        expected = data['actions'][action].get('handler', action)
        handler = request.get('handler', expected)
        if handler != expected:
            raise ValueError('原生函数与技能声明不一致。')
    from primebuddy.skills.manager import load_entry
    module = load_entry(root, entrypoint)
    try:
        if operation in ('run', 'validate') and not callable(getattr(module, handler, None)):
            raise ValueError('原生动作函数不存在。')
        if operation == 'run':
            return getattr(module, handler)(context=context, **args)
        hook = getattr(module, operation, None)
        if not callable(hook):
            return None
        if operation == 'planning_context':
            return hook(context)
        if operation == 'preflight':
            return hook(action, args, context)
        if operation == 'confirmation':
            text, snapshot = request.get('text', ''), request.get('snapshot', {})
            if not isinstance(text, str) or not isinstance(snapshot, dict):
                raise ValueError('确认上下文无效。')
            return hook(action, args, text, snapshot)
        return hook(action, args)
    finally:
        shutdown = getattr(module, 'shutdown', None)
        if callable(shutdown):
            shutdown()


def _write_result(path, result):
    text = json.dumps(result, ensure_ascii=False, allow_nan=False)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            output.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        return 2
    request_path, result_path = map(Path, arguments)
    cancelled = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: cancelled.set())
    try:
        if request_path.stat().st_size > 1024 * 1024:
            raise ValueError('原生适配请求过大。')
        request = json.loads(request_path.read_text())
        value = execute(request, cancelled)
        _write_result(result_path, {'value': value})
        return 0
    except BaseException as error:
        # 异常正文可能包含登录态或第三方签名 URL，只返回类型。
        try:
            _write_result(result_path, {'error_type': type(error).__name__})
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
