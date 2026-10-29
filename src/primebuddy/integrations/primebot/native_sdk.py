"""标准机载 SDK 的独立入口与远程命令环境；三个发行包保持同一约定。"""
import importlib.util
import os
from pathlib import Path
import shlex
import sys

PREFIX = Path('/robot/software/interface/aimdk_msgs')


def environment_script():
    for parent in Path(__file__).resolve().parents:
        script = parent / 'scripts/robot_env.sh'
        if script.is_file():
            return script
    raise RuntimeError('缺少随包标准 SDK 环境脚本。')


def shell_environment():
    # 在远程主机执行同一脚本，不依赖远端也安装了当前应用。
    return 'source /dev/stdin <<\'PRIMEBOT_SDK_ENV\'\n' + environment_script().read_text() + '\nPRIMEBOT_SDK_ENV\n'


def bootstrap():
    if not Path('/robot/data/info/series').is_file():
        return
    if os.environ.get('PRIMEBOT_STANDARD_SDK') == str(PREFIX):
        spec = importlib.util.find_spec('aimdk_msgs')
        if spec and spec.origin and Path(spec.origin).resolve().is_relative_to(PREFIX.resolve()):
            return
        raise SystemExit('aimdk_msgs 来源与机载标准 SDK 不符，拒绝启动。')
    # 动态链接器在进程启动时读取 LD_LIBRARY_PATH；不能只改当前 Python 的 sys.path。
    command = 'source ' + shlex.quote(str(environment_script())) + ' || exit $?; exec ' + shlex.join([sys.executable, *sys.argv])
    os.execv('/bin/bash', ['bash', '-c', command])
