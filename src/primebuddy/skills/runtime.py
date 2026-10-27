"""普通 CLI 与旧原生入口均在独立进程中执行；宿主监督期限和清理。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

from primebuddy.config.storage import (SetupError, application_directory, config_path,
    get_data_directory, skill_data, private_dir, write_json)
from primebuddy.skills.metadata import resolve_skill_file
from primebuddy.skills.adapters import command_arguments
from primebuddy.skills.processes import run_process, process_cleanup_confirmed


def script_command(root, relative, arguments):
    path = resolve_skill_file(root, relative)
    if (not isinstance(arguments, list) or len(arguments) > 64
            or any(not isinstance(arg, str) or len(arg) > 1000 or '\x00' in arg for arg in arguments)):
        raise ValueError('脚本参数须为最多 64 项的文本列表。')
    interpreter = {'.py': sys.executable, '.sh': 'bash', '.js': 'node'}.get(path.suffix)
    if interpreter is None:
        raise ValueError('固定脚本支持 Python、Bash 和 JavaScript。')
    return [interpreter, str(path), *arguments]


def skill_environment(root, name):
    environment = dict(os.environ)
    sources = [application_directory() / 'src', application_directory() / '_deps',
               Path(root) / '_deps', Path(root) / 'src']
    environment['PYTHONPATH'] = os.pathsep.join([*(str(path) for path in sources), environment.get('PYTHONPATH', '')])
    environment['PRIMEBUDDY_HOME'] = str(get_data_directory())
    environment['PRIMEBUDDY_SKILL_DATA'] = str(skill_data(name))
    environment['PRIMEBUDDY_SKILL_CONFIG'] = str(config_path('config.yaml', skill=name))
    return environment


def _runtime(context):
    if context.runtime is not None:
        return private_dir(context.runtime)
    from primebuddy.config.runtime import runtime_directory
    return private_dir(runtime_directory())


def _process(skill, command, context, timeout=600, *, cleanup=False, stop_grace=None):
    options = {'stop_grace': stop_grace} if stop_grace is not None else {}
    execution_id = None
    try:
        result = run_process(command, cwd=skill['root'], env=skill_environment(skill['root'], skill['name']),
                             runtime=_runtime(context), cancelled=threading.Event() if cleanup else context.cancelled,
                             timeout=timeout, owner_id=context.owner_id, task_id=context.task_id, **options)
        execution_id = result.get('execution_id')
    except Exception as error:
        if getattr(error, 'cleanup_confirmed', False) is True:
            raise
        result = None
        execution_id = getattr(error, 'execution_id', None)
    if result is None or not result['cleanup_confirmed']:
        from primebuddy.core.engine import CleanupUnconfirmed
        try:
            marker = private_dir(_runtime(context) / skill['name']) / 'cleanup-unconfirmed.json'
            previous = json.loads(marker.read_text()) if marker.exists() else {'process_executions': []}
            executions = previous.get('process_executions', [None])
            if not isinstance(executions, list):
                executions = [None]
            write_json(marker, {'resources': sorted({resource for action in skill['actions'].values()
                                                   for resource in action['resources']}),
                                'process_executions': executions + [execution_id]})
        except (OSError, ValueError, AttributeError):
            raise CleanupUnconfirmed('进程清理未确认，且无法保存隔离记录。') from None
        raise CleanupUnconfirmed('技能进程清理未确认。')
    return result


def _finished(process):
    return process['status'] == 'exited' and process['returncode'] == 0 and process['cleanup_confirmed']


def _json_output(process):
    try:
        value = json.loads(process['stdout'])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _result(process):
    if not process['cleanup_confirmed']:
        return {'status': 'failed', 'message': '技能进程清理未确认，相关资源已隔离。', 'cleanup_confirmed': False}
    value = _json_output(process)
    if process['status'] != 'exited':
        messages = {'cancelled': '任务已取消。', 'timed_out': '任务超过硬执行期限，已终止。',
                    'output_limit': '任务输出超过限额，已终止。'}
        return {'status': 'failed', 'message': messages.get(process['status'], '技能执行未完成。'),
                'termination_reason': process['status']}
    if (value and value.get('status') in {'success', 'failed', 'auth_required', 'clarification_required'}
            and isinstance(value.get('message'), str)):
        if process['returncode'] and value['status'] == 'success':
            return {'status': 'failed', 'message': '技能进程异常退出，业务结果未确认。',
                    'termination_reason': 'abnormal_exit'}
        if process['returncode']:
            value['termination_reason'] = 'abnormal_exit'
        return value
    if process['returncode']:
        return {'status': 'failed', 'message': '技能进程执行失败，退出码：' + str(process['returncode']),
                'termination_reason': 'abnormal_exit'}
    text = process['stdout'].strip()[:65536]
    return {'status': 'success', 'message': text[:200] or '脚本执行完成。', 'output': text}


def run_script(skill, args, context):
    command = script_command(skill['root'], args['path'], args['arguments'])
    context.checkpoint()
    if context.simulate:
        return {'status': 'success', 'message': '模拟：未执行 ' + args['path'] + '。', 'simulated': True}
    return _result(_process(skill, command, context))


class SkillRuntime:
    """宿主适配器；不会在 Buddy 进程内导入技能模块。"""
    def __init__(self, skill):
        self.skill = skill
        self.adapter = skill.get('adapter', {})
        self.descriptions = {}

    def close(self):
        # 每次子进程执行自行清理，没有跨任务复用的业务 Python 客户端。
        pass

    def _values(self, context, args=None):
        return dict(args or {}, config=str(config_path('config.yaml', skill=self.skill['name'])),
                    data=str(skill_data(self.skill['name'])),
                    runtime=str(private_dir(_runtime(context) / self.skill['name'])))

    def _command(self, template, context, args=None):
        path, arguments = command_arguments(template, self._values(context, args))
        return script_command(self.skill['root'], path, arguments)

    def _query(self, template, context, args=None, *, cleanup=False):
        process = _process(self.skill, self._command(template, context, args), context, 30, cleanup=cleanup)
        if not _finished(process):
            raise SetupError('技能只读查询或清理命令未完成。')
        result = _json_output(process)
        if result is None:
            raise SetupError('已配置的技能查询命令没有返回 JSON 对象。')
        return result

    def _native(self, operation, context=None, action=None, args=None, **extra):
        if not self.skill.get('entrypoint'):
            return None
        if context is None:
            from primebuddy.core.engine import Context
            context = Context()
        runtime = _runtime(context)
        request = dict(root=str(self.skill['root']), entrypoint=self.skill['entrypoint'], operation=operation,
                       action=action, args=args or {}, context=dict(runtime=str(runtime),
                       skill_configs=context.skill_configs, simulate=context.simulate), **extra)
        if action:
            request['handler'] = self.skill['actions'][action].get('handler', action)
        with tempfile.TemporaryDirectory(prefix='skill-call-', dir=runtime) as temporary:
            incoming, outgoing = Path(temporary) / 'request.json', Path(temporary) / 'result.json'
            write_json(incoming, request)
            command = [sys.executable, '-m', 'primebuddy.skills.worker', str(incoming), str(outgoing)]
            process = _process(self.skill, command, context, 600 if operation == 'run' else 30)
            if operation == 'run' and process['status'] != 'exited':
                return _result(process)
            if (not _finished(process) or not outgoing.is_file() or outgoing.stat().st_size > 65536):
                raise SetupError('旧原生适配子进程执行未完成。')
            result = json.loads(outgoing.read_text())
            if 'value' not in result:
                raise SetupError('旧原生适配没有返回结果。')
            return result['value']

    def validate(self, action, args):
        if action == 'script':
            script_command(self.skill['root'], args['path'], args['arguments'])
        elif self.adapter:
            template = self.skill['actions'][action]['command']
            path, arguments = command_arguments(template, dict(args, config='', data='', runtime=''))
            script_command(self.skill['root'], path, arguments)
        else:
            self._native('validate', action=action, args=args)

    def _isolate(self, context):
        try:
            marker = Path(self._values(context)['runtime']) / 'cleanup-unconfirmed.json'
            if not marker.exists():
                write_json(marker, {'resources': sorted({resource for action in self.skill['actions'].values()
                                                       for resource in action['resources']}),
                                    'process_executions': []})
        except OSError:
            from primebuddy.core.engine import CleanupUnconfirmed
            raise CleanupUnconfirmed('设备清理未确认，且无法保存隔离记录。') from None

    def _verify(self, context, *, recover=False):
        from primebuddy.core.engine import CleanupUnconfirmed
        marker = Path(self._values(context)['runtime']) / 'cleanup-unconfirmed.json'
        if marker.exists():
            try:
                executions = json.loads(marker.read_text())['process_executions']
            except (OSError, ValueError, KeyError, TypeError):
                raise CleanupUnconfirmed('资源隔离记录缺少进程身份，未解除隔离。') from None
            if not isinstance(executions, list) or not process_cleanup_confirmed(_runtime(context), executions):
                raise CleanupUnconfirmed('旧技能进程尚未确认退出，未解除隔离。')
        try:
            if recover and self.adapter.get('recover'):
                try:
                    self._query(self.adapter['recover'], context, cleanup=True)
                except SetupError:
                    pass
            state = self._query(self.adapter['status'], context, cleanup=True)
            if state.get('status') != 'success' or state.get('resources_released') is not True:
                raise SetupError('设备状态未确认。')
        except Exception:
            self._isolate(context)
            raise CleanupUnconfirmed('技能设备清理未确认，保留恢复记录。') from None
        marker.unlink(missing_ok=True)

    def preflight(self, action, args, context):
        from primebuddy.core.engine import CleanupUnconfirmed
        if (self.skill['actions'][action].get('interaction') == 'exclusive'
                and not all(self.adapter.get(key) for key in ('status', 'recover'))):
            raise SetupError('独占交互应用须使用带 status 和 recover 的 CLI 适配。')
        if context.simulate and self.adapter:
            return
        marker = Path(self._values(context)['runtime']) / 'cleanup-unconfirmed.json'
        if not context.simulate:
            if self.adapter.get('status'):
                self._verify(context)
            elif marker.exists():
                raise CleanupUnconfirmed('技能资源尚未确认释放。')
        if action == 'script':
            return
        if self.adapter:
            template = self.skill['actions'][action].get('preflight')
            if template and not context.simulate:
                result = self._query(template, context, args)
                if result.get('status') != 'success':
                    raise ValueError('技能预检未通过。')
        else:
            self._native('preflight', context, action, args)

    def describe(self, action, args):
        if action == 'script':
            return '正在执行' + self.skill['name'] + '技能'
        if self.adapter:
            key = (action, json.dumps(args, sort_keys=True, ensure_ascii=False))
            return self.descriptions.get(key) or self.skill['actions'][action].get(
                'description', '正在执行' + self.skill['name']).format_map(args)
        return self._native('describe', action=action, args=args) or '正在执行' + self.skill['name']

    def planning_context(self, context):
        if self.adapter.get('context'):
            return self._query(self.adapter['context'], context)
        return self._native('planning_context', context)

    def confirmation(self, action, args, text, snapshot):
        if self.adapter:
            template = self.skill['actions'][action].get('confirmation')
            if not template:
                return None
            from primebuddy.core.engine import Context
            result = self._query(template, Context(), dict(args, request_text=text))
            if isinstance(result.get('description'), str):
                self.descriptions[(action, json.dumps(args, sort_keys=True, ensure_ascii=False))] = result['description']
            return result.get('question')
        return self._native('confirmation', action=action, args=args, text=text, snapshot=snapshot)

    def run(self, action, args, context):
        from primebuddy.core.engine import CleanupUnconfirmed
        try:
            return self._run(action, args, context)
        except CleanupUnconfirmed:
            raise
        except BaseException:
            if not context.simulate and self.adapter.get('status'):
                self._verify(context, recover=True)
            elif not context.simulate and self._uses_hardware(action):
                self._isolate(context)
                raise CleanupUnconfirmed('设备任务异常，缺少独立状态确认能力。') from None
            raise

    def _uses_hardware(self, action):
        return any(resource == 'speaker' or resource.startswith(('speaker.', 'robot.'))
                   for resource in self.skill['actions'][action]['resources'])

    def _run(self, action, args, context):
        if action == 'script':
            result = run_script(self.skill, args, context)
        elif self.adapter:
            definition = self.skill['actions'][action]
            if context.simulate:
                return {'status': 'success', 'message': '模拟：未执行 ' + self.skill['name'] + '。', 'simulated': True}
            command = self._command(definition['command'], context, args)
            result = _result(_process(self.skill, command, context, definition.get('timeout_seconds', 600),
                                     stop_grace=definition.get('stop_grace_seconds')))
        else:
            result = self._native('run', context, action, args)
            if isinstance(result, str):
                result = {'status': 'success', 'message': result}
        if not context.simulate:
            if self.adapter.get('status'):
                self._verify(context, recover=result.get('status') != 'success')
            elif result.get('cleanup_confirmed') is False:
                self._isolate(context)
                from primebuddy.core.engine import CleanupUnconfirmed
                raise CleanupUnconfirmed('技能进程清理未确认。')
            elif result.get('termination_reason') and self._uses_hardware(action):
                self._isolate(context)
                from primebuddy.core.engine import CleanupUnconfirmed
                raise CleanupUnconfirmed('设备任务已终止，但外部设备状态未确认。')
        return result
