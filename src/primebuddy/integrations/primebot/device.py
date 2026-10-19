"""规范身份识别与语音服务主机选择；SoC 仅决定传输，不限制功能。"""
from dataclasses import dataclass
import os
from pathlib import Path
import re

from primebuddy.config.storage import SetupError


@dataclass(frozen=True)
class Device:
    series: str
    sku: str
    soc_index: str


def detect_device(root=Path('/robot/data/info')):
    try:
        values = {name: (root / name).read_text().strip() for name in ('series', 'sku', 'soc_index')}
    except OSError:
        raise SetupError('无法读取机器人规范身份，请在目标机器的 ROS 运行环境启动。') from None
    if values['series'] not in ('T1', 'Q1') or values['soc_index'] not in ('0', '1') or not values['sku']:
        raise SetupError('不支持的机器人型号或控制器身份，未默认使用 T1。')
    return Device(**values)


def audio_host():
    value = os.environ.get('PRIMEBOT_AUDIO_HOST')
    if value is None:
        value = 'local' if detect_device().soc_index == '0' else 'run@10.1.1.100'
    if value != 'local' and not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9.-]*', value):
        raise SetupError('PRIMEBOT_AUDIO_HOST 须为 local 或 user@host。')
    return value


def host_command(runtime, command, *, ros=False):
    if ros:
        setups = ['/robot/software/app_proxy/bin/setup.bash', '/robot/software/housekeeper/bin/setup.bash']
        if detect_device().series == 'Q1':
            setups.reverse()
        command = ('source /robot/etc/basic_env >/dev/null 2>&1 && '
                   'for setup in ' + ' '.join(setups) + '; '
                   'do if test -f "$setup"; then source "$setup" >/dev/null 2>&1; break; fi; done; ' + command)
    if audio_host() == 'local':
        return ['bash', '-c', command]
    from .robot_audio import ssh_options
    return ['ssh', '-T', *ssh_options(runtime), audio_host(), command]


class VoiceFacade:
    """只装配框架自身的语音；不向 skill 注入设备能力。"""
    sample_rate = 16000
    stream_id = 0

    def __init__(self, device):
        self.device = device

    def preflight(self):
        from .speech_audio import native_preflight
        return native_preflight()

    def microphone(self, runtime, robot):
        from .native_voice import NativeVoiceInput
        return NativeVoiceInput(runtime, robot, stream_id=self.stream_id)


class T1Voice(VoiceFacade):
    pass


class Q1Voice(VoiceFacade):
    pass


def create_voice():
    device = detect_device()
    return {'T1': T1Voice, 'Q1': Q1Voice}[device.series](device)
