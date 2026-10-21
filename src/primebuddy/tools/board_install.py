"""按固定内网地址选择安装板，并在大脑板继续同一份安装包。"""
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

from primebuddy.integrations.primebot.ssh_setup import BRAIN_HOST, local_identity, remote_identity, ssh_session


def brain_available():
    for attempt in range(3):
        try:
            with socket.create_connection(('10.1.1.101', 22), timeout=2):
                return True
        except OSError:
            if attempt < 2:
                time.sleep(.3)
    return False


def installation_board():
    identity = local_identity()
    if identity is None:
        return None
    if identity['soc_index'] == '1':
        print('✓ 已识别 ' + identity['series'] + ' 大脑板（SoC 1），将在本板安装。', flush=True)
        return None
    print('正在检测大脑板 10.1.1.101…', flush=True)
    if brain_available():
        print('✓ 已检测到双板，将连接大脑板继续安装；如有提示，请输入大脑板登录密码。', flush=True)
        return identity
    print('未检测到大脑板，将在运控板（SoC 0）本地安装。', flush=True)
    return None


def install_on_brain(identity, home, no_onboard):
    from primebuddy.tools.install import stage_application
    source = Path(__file__).resolve().parents[3]
    with ssh_session(BRAIN_HOST, '大脑板') as ssh, tempfile.TemporaryDirectory(prefix='pb-package-') as temporary:
        remote_identity(ssh, BRAIN_HOST, {'series': identity['series'], 'soc_index': '1'})
        stage = Path(temporary) / 'source'
        stage_application(source, stage)
        package = Path(temporary) / 'install.zip'
        with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as output:
            for path in sorted(stage.rglob('*')):
                if path.is_file():
                    output.write(path, path.relative_to(stage).as_posix())
        name = '.cache/primebuddy/install-' + uuid.uuid4().hex
        code = ("import io, sys, zipfile\nfrom pathlib import Path\n"
                "target = Path.home() / " + repr(name) + "\n"
                "target.mkdir(parents=True, mode=0o700)\n"
                "with zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read())) as package:\n"
                "    package.extractall(target)\n"
                "print(target)\n")
        print('正在传输安装包到大脑板…', flush=True)
        with package.open('rb') as data:
            result = subprocess.run([*ssh, '-T', BRAIN_HOST, 'python3 -c ' + shlex.quote(code)],
                                    stdin=data, capture_output=True, check=True, timeout=180)
        remote = result.stdout.decode().strip()
        if not Path(remote).is_absolute() or not remote.endswith('/' + name) or '\n' in remote:
            raise RuntimeError('大脑板返回的安装路径无效。')
        command = shlex.join(['python3', remote + '/main.py', 'install', '--home', str(home)])
        if no_onboard:
            command += ' --no-onboard'
        interactive = sys.stdin.isatty()
        if interactive and not no_onboard:
            command += (' && printf "\\n安装与配置完成。当前终端位于大脑板，'
                        '可运行 primebuddy start；exit 返回运控板。\\n" && exec bash -l')
        result = subprocess.run([*ssh, '-tt' if interactive else '-T', BRAIN_HOST,
                                 'bash -c ' + shlex.quote(command)])
        if result.returncode:
            print('大脑板安装或配置尚未完成。安装源保留在：' + remote
                  + '；可重新运行安装命令继续。', flush=True)
            return result.returncode
        cleanup = 'import shutil; shutil.rmtree(' + repr(remote) + ')'
        subprocess.run([*ssh, '-T', BRAIN_HOST, 'python3 -c ' + shlex.quote(cleanup)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        print('大脑板安装流程已结束。当前返回运控板；下次可用 ssh -t '
              + BRAIN_HOST + ' 登录大脑板。', flush=True)
        return 0
