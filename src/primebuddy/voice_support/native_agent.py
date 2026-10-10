"""保留原厂语音前端，切换 only_voice 并在退出时恢复原模式。"""
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time

from .robot_audio import AudioError, SOC0, ssh_options, check_cancel
from .timing import progress

RPC_CODE = '''import json,sys,urllib.request
op=sys.argv[1]
if op=='state':
 with urllib.request.urlopen('http://127.0.0.1:50080/json/get_all_apps_info',timeout=8) as r:
  a=json.load(r)['data']['agent'];print(json.dumps({'status':a['status'],'pid':a['pid']}))
else:
 q=urllib.request.Request('http://127.0.0.1:50080/json/'+op+'_app',data=b'{"app_name":"agent"}',headers={'Content-Type':'application/json'},method='POST')
 with urllib.request.urlopen(q,timeout=8) as r:d=json.load(r)
 print(json.dumps({'status':d.get('status'),'message':d.get('message')}))
'''


def backend_for_version(version):
    match = re.fullmatch(r'v(\d+)d(\d+)', version)
    if match is None:
        raise AudioError('硬件版本格式未知，未猜测原厂 Agent 启停方式。')
    return 'http' if tuple(map(int, match.groups())) >= (2, 1) else 'aima'


def agent_backend(runtime):
    result = subprocess.run(['ssh', '-T', *ssh_options(runtime), SOC0,
                             'cat /robot/data/info/hardware_version'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=18)
    if result.returncode:
        raise AudioError('未读取到播放板硬件版本，未启停原厂 Agent。')
    version = result.stdout.decode().strip()
    backend = backend_for_version(version)
    progress(f'硬件版本 {version}，原厂 Agent 管理方式：{backend}。')
    return backend


def parse_doctor_state(output):
    # 只解析agent行，不输出doctor中其他应用的信息；不认识的格式拒绝接管。
    output = re.sub(r'\x1b\[[0-9;]*m', '', output)
    rows = [line for line in output.splitlines() if re.match(r'^\s*\|?\s*agent(?:\s|\|)', line)]
    if len(rows) != 1:
        raise AudioError('aima em doctor 未返回唯一的 Agent 状态行。')
    running = re.search(r'\bRunning\((\d+)\)', rows[0])
    if running and int(running[1]) > 0:
        return {'status': running[0], 'pid': int(running[1])}
    if re.search(r'\b(?:Idel|Idle)\(-1\)', rows[0]):
        return {'status': 'Idel(-1)', 'pid': -1}
    raise AudioError('aima em doctor 的 Agent 状态无法确认，未猜测运行状态。')


def agent_rpc(runtime, operation, backend='http'):
    if operation not in {'state', 'start', 'stop'} or backend not in {'http', 'aima'}:
        raise AudioError('原厂 Agent 管理请求无效。')
    if backend == 'http':
        command = 'python3 -c ' + shlex.quote(RPC_CODE) + ' ' + shlex.quote(operation)
    else:
        # 在播放板执行CLI；退出恢复使用同一后端，不回退到另一套管理接口。
        cli = 'doctor' if operation == 'state' else operation + '-app agent'
        command = 'source /robot/etc/basic_env && aima em ' + cli
    result = subprocess.run(['ssh', '-T', *ssh_options(runtime), SOC0, command],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=18)
    if result.returncode:
        raise AudioError('原厂 Agent 管理连接或命令失败。')
    if backend == 'http':
        data = json.loads(result.stdout)
    else:
        data = parse_doctor_state(result.stdout.decode()) if operation == 'state' else {'status': 'success'}
    if operation != 'state' and data.get('status') != 'success':
        raise AudioError('原厂 Agent 启停请求失败。')
    print('AGENT_' + operation.upper(), json.dumps(data), flush=True)
    return data


def confirm_agent(runtime, running, backend='http'):
    deadline = time.monotonic() + 35
    while True:
        state = agent_rpc(runtime, 'state', backend)
        if running:
            if str(state['status']).startswith('Running(') and state['pid'] > 0:
                return
        elif state['status'] == 'Idel(-1)' and state['pid'] == -1:
            return
        if time.monotonic() >= deadline:
            raise AudioError('原厂 Agent 状态未确认；恢复记录保留。')
        time.sleep(.5)


def agent_mode(runtime, value=None):
    # 每次调用使用独立进程，重启 Agent 后不会复用旧 ROS 服务句柄。
    source = Path(__file__).with_name('agent_mode_rpc.py').read_text()
    command = ('source /robot/etc/basic_env >/dev/null 2>&1 && '
               'source /robot/software/app_proxy/bin/setup.bash >/dev/null 2>&1 && '
               'python3 -c ' + shlex.quote(source))
    if value is not None:
        command += ' ' + shlex.quote(value)
    result = subprocess.run(['ssh', '-T', *ssh_options(runtime), SOC0, command],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=75)
    if result.returncode:
        raise AudioError('原厂 Agent 模式调用失败；请核对恢复记录。')
    try:
        return json.loads(result.stdout)['mode']
    except (ValueError, KeyError):
        raise AudioError('原厂 Agent 模式响应格式无效。') from None


def restart_agent(runtime, backend):
    agent_rpc(runtime, 'stop', backend)
    confirm_agent(runtime, False, backend)
    agent_rpc(runtime, 'start', backend)
    confirm_agent(runtime, True, backend)
    # 进程 Running 早于服务发现稳定；后续模式读回另建 ROS 客户端。
    time.sleep(5)


def restore_agent(runtime, previous, backend):
    """退出时恢复；确认完成前始终保留最初的模式记录。"""
    progress('正在恢复原厂 Agent 模式：' + previous)
    phase = '确认进程'
    try:
        current = agent_rpc(runtime, 'state', backend)
        if not str(current['status']).startswith('Running('):
            agent_rpc(runtime, 'start', backend)
        confirm_agent(runtime, True, backend)
        phase = '写入原模式'
        agent_mode(runtime, previous)
        phase = '重启 Agent'
        restart_agent(runtime, backend)
        phase = '读回原模式'
        if agent_mode(runtime) != previous:
            raise AudioError('模式读回不一致')
        phase = '完成恢复记录'
        (runtime / 'agent_mode.json').unlink()
    except Exception as error:
        raise AudioError('原厂 Agent 恢复未完成：' + phase + '（' + type(error).__name__
                         + '）；恢复记录保留。检查连接后重新运行语音命令，可自动继续接管。') from error
    progress('原厂 Agent 已恢复模式：' + previous)
    print('RESTORE_CONFIRMED', flush=True)


@contextlib.contextmanager
def voice_only_agent(runtime):
    # 调用者已经持有 audio.lock；不能在另一个会话运行时接管。
    record = runtime / 'agent_mode.json'
    for name in ('agent_one_turn.json', 'agent_voice_probe.json',
                 'agent_sdk_dependency_probe.json'):
        if (runtime / name).exists():
            raise AudioError('存在其他工具的恢复记录 ' + name + '，须先由对应工具恢复，未接管。')
    backend = agent_backend(runtime)
    previous = None
    if record.exists():
        try:
            saved = json.loads(record.read_text())
        except (OSError, ValueError):
            raise AudioError('Agent 恢复记录无法读取，已保留；请检查 agent_mode.json。') from None
        if (not isinstance(saved, dict) or saved.get('previous_running') is not True
                or saved.get('previous_mode') not in ('normal', 'only_voice')
                or saved.get('purpose') != 'music_session' or saved.get('backend') != backend):
            raise AudioError('Agent 恢复记录与当前设备不匹配，已保留，未修改原厂状态。')
        previous = saved['previous_mode']
        progress('发现上次未完成的会话，直接继续接管 only_voice；保留原始模式：' + previous)
    check_cancel()
    state = agent_rpc(runtime, 'state', backend)
    running = str(state['status']).startswith('Running(') and state['pid'] > 0
    if not running and (state['status'] != 'Idel(-1)' or state['pid'] != -1):
        raise AudioError('原厂 Agent 进程状态未确认，未接管。')
    if previous is None:
        if not running:
            raise AudioError('原厂 Agent 未运行且没有恢复记录，无法确认原模式，未接管。')
        previous = agent_mode(runtime)
        if previous not in {'normal', 'only_voice'}:
            raise AudioError('仅支持从 normal 或 only_voice 进入技能，未修改其他模式。')
        fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump({'previous_running': True, 'previous_mode': previous,
                       'purpose': 'music_session', 'backend': backend}, output)
            output.flush()
            os.fsync(output.fileno())
    try:
        if not running:
            agent_rpc(runtime, 'start', backend)
            confirm_agent(runtime, True, backend)
        # 属性读回不证明重启已经生效；中断重入也只做这一轮 only_voice 重启。
        agent_mode(runtime, 'only_voice')
        restart_agent(runtime, backend)
        if agent_mode(runtime) != 'only_voice':
            raise AudioError('重启后未确认 only_voice，未开始点歌。')
        yield
    finally:
        # 超时也可能已生效；恢复不能因为退出信号而跳过。
        restore_agent(runtime, previous, backend)
