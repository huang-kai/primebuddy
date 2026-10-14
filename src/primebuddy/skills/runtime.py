"""固定脚本和可选原生工具；模拟不启动脚本，取消等待业务清理。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from primebuddy.config.storage import SetupError, application_directory, config_path, get_data_directory, skill_data
from primebuddy.skills.metadata import resolve_skill_file


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


def stop_script(process):
    # 脚本可在 SIGINT 的 finally 中清理；超时后终止整个子进程组。
    for sig, timeout in ((signal.SIGINT, 3), (signal.SIGTERM, 2), (signal.SIGKILL, 2)):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, sig)
            process.wait(timeout=timeout)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            continue


def run_script(skill, args, context):
    command = script_command(skill['root'], args['path'], args['arguments'])
    context.checkpoint()
    if context.simulate:
        return {'status': 'success', 'message': '模拟：未执行 ' + args['path'] + '。', 'simulated': True}
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, cwd=skill['root'], env=skill_environment(skill['root'], skill['name']),
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=errors, start_new_session=True)
        deadline = time.monotonic() + 600
        try:
            while process.poll() is None:
                context.checkpoint()
                if time.monotonic() > deadline:
                    raise TimeoutError('技能脚本超过 600 秒。')
                if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > 1024 * 1024:
                    raise ValueError('技能脚本输出超过 1 MiB。')
                time.sleep(.02)
        finally:
            stop_script(process)
        output.seek(0)
        text = output.read(65536).decode('utf-8', errors='replace').strip()
        if process.returncode:
            return {'status': 'failed', 'message': '技能脚本执行失败，退出码：' + str(process.returncode)}
        try:
            result = json.loads(text)
        except ValueError:
            result = None
        if isinstance(result, dict) and result.get('status') in {
                'success', 'failed', 'auth_required', 'clarification_required'} and isinstance(result.get('message'), str):
            return result
        return {'status': 'success', 'message': text[:200] or '脚本执行完成。', 'output': text}


class SkillRuntime:
    """调度器内部适配；skill 作者无需实现这一组方法。"""
    def __init__(self, skill):
        self.skill = skill
        self.module = None

    def native_module(self):
        if self.module is None and self.skill.get('entrypoint'):
            from primebuddy.skills.manager import load_entry
            self.module = load_entry(self.skill['root'], self.skill['entrypoint'])
        return self.module

    def __getattr__(self, name):
        module = self.native_module()
        if module is None:
            raise AttributeError(name)
        return getattr(module, name)

    def validate(self, action, args):
        if action == 'script':
            script_command(self.skill['root'], args['path'], args['arguments'])
        else:
            module = self.native_module()
            handler = self.skill['actions'][action].get('handler', action)
            if not callable(getattr(module, handler, None)):
                raise SetupError('原生工具函数不存在：' + self.skill['name'] + '.' + handler)
            if callable(getattr(module, 'validate', None)):
                module.validate(action, args)

    def preflight(self, action, args, context):
        if action != 'script':
            module = self.native_module()
            if callable(getattr(module, 'preflight', None)):
                module.preflight(action, args, context)

    def describe(self, action, args):
        if action == 'script':
            return '正在执行' + self.skill['name'] + '技能'
        module = self.native_module()
        if callable(getattr(module, 'describe', None)):
            return module.describe(action, args)
        return '正在执行' + self.skill['name'] + '的' + action

    def run(self, action, args, context):
        if action == 'script':
            return run_script(self.skill, args, context)
        handler = self.skill['actions'][action].get('handler', action)
        function = getattr(self.native_module(), handler, None)
        if not callable(function):
            raise SetupError('原生工具函数不存在：' + self.skill['name'] + '.' + handler)
        result = function(context=context, **args)
        if isinstance(result, str):
            return {'status': 'success', 'message': result}
        return result
