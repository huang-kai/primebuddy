"""安装用 SSH 连接与专用公钥配置；仅用标准库，密码交给 ssh 终端读取。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

BRAIN_HOST = 'run@10.1.1.101'
MOTION_HOST = 'run@10.1.1.100'
INFO_ROOT = Path('/robot/data/info')
IDENTITY_CODE = """import json
from pathlib import Path
root = Path('/robot/data/info')
print(json.dumps({name: (root / name).read_text().strip()
                  for name in ('series', 'sku', 'soc_index')}))
"""


def local_identity():
    if not INFO_ROOT.exists():
        return None
    try:
        identity = {name: (INFO_ROOT / name).read_text().strip() for name in ('series', 'sku', 'soc_index')}
    except OSError:
        raise RuntimeError('无法读取机器人身份，安装尚未开始。') from None
    if identity['series'] not in ('T1', 'Q1') or identity['soc_index'] not in ('0', '1') or not identity['sku']:
        raise RuntimeError('不支持的机器人身份，安装尚未开始。')
    return identity


def ssh_base():
    return ['ssh', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=5',
            '-o', 'ServerAliveCountMax=2', '-o', 'ForwardAgent=no',
            '-o', 'ClearAllForwardings=yes']


def prepare_ssh_directory():
    directory = Path.home() / '.ssh'
    if directory.is_symlink():
        raise RuntimeError('SSH 目录是符号链接，请先检查其权限；未修改链接目标。')
    directory.mkdir(exist_ok=True, mode=0o700)
    if not os.access(directory, os.W_OK):
        print('需要管理员权限准备当前用户的 SSH 目录；保留已有密钥和授权文件。', flush=True)
        try:
            # 部分原厂镜像的 ~/.ssh 属于 root；只调整目录本身，不递归修改文件。
            subprocess.run(['sudo', 'install', '-d', '-m', '700', '-o', str(os.getuid()),
                            '-g', str(os.getgid()), str(directory)], check=True, timeout=60)
        except (subprocess.SubprocessError, OSError):
            raise RuntimeError('SSH 目录不可写，请由管理员准备：' + str(directory)) from None
        if not os.access(directory, os.W_OK):
            raise RuntimeError('SSH 目录不可写，请由管理员准备：' + str(directory))
    return directory


@contextmanager
def ssh_session(host, label, options=()):
    prepare_ssh_directory()
    # 短路径适配 Unix socket 长度限制；只关闭本次安装创建的连接。
    with tempfile.TemporaryDirectory(prefix='pb-ssh-', dir='/tmp') as directory:
        base = [*ssh_base(), *options, '-S', str(Path(directory) / 'master')]
        try:
            subprocess.run([*base, '-o', 'StrictHostKeyChecking=accept-new', '-o', 'BatchMode=no',
                            '-o', 'ControlMaster=yes', '-o', 'ControlPersist=600', '-Nf', host],
                           check=True, timeout=120)
            subprocess.run([*base, '-O', 'check', host], check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            yield [*base, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes']
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise RuntimeError(label + '连接或操作失败，请检查 SSH 提示后重新运行；不会改为在另一块板安装。') from None
        finally:
            try:
                subprocess.run([*base, '-O', 'exit', host], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                pass


def remote_identity(ssh, host, expected):
    result = subprocess.run([*ssh, '-T', host, 'python3 -c ' + shlex.quote(IDENTITY_CODE)],
                            capture_output=True, text=True, check=True, timeout=20)
    try:
        identity = json.loads(result.stdout)
        if any(identity.get(name) != value for name, value in expected.items()):
            raise ValueError
    except (ValueError, AttributeError):
        raise RuntimeError('目标板身份与预期不符，未安装或配置密钥。') from None
    return identity


def passwordless(host, options=()):
    # 不复用 socket，避免把临时密码会话误判为已具备免密认证。
    result = subprocess.run([*ssh_base(), '-S', 'none', '-o', 'BatchMode=yes',
                             '-o', 'StrictHostKeyChecking=yes', *options, '-T', host, 'true'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    return result.returncode == 0


def configure_host(host, key):
    """为独立技能也提供相同密钥；保留其他 Host、Match 和用户设置。"""
    address = host.split('@', 1)[1]
    directory = Path.home() / '.ssh'
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / 'config'
    if path.is_symlink():
        path = path.resolve()
    original = path.read_text() if path.exists() else ''
    start, end = '# >>> PrimeBuddy ' + host + ' >>>', '# <<< PrimeBuddy ' + host + ' <<<'
    original = re.sub(re.escape(start) + r'.*?' + re.escape(end) + r'\n?', '', original, flags=re.S)
    quoted = str(key).replace('\\', '\\\\').replace('"', '\\"')
    block = (f'{start}\nHost {address}\n    IdentityFile "{quoted}"\n'
             f'Host *\n{end}\n')
    # 放在文件开头并恢复 Host *，不会让原文件的全局选项落入专用 Host。
    content = block + original
    if not path.exists() or path.read_text() != content:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        try:
            temporary.chmod(0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def authorize_key(ssh, host, public_key):
    # 公钥通过 stdin 传递；不读取或传输私钥，不覆盖其他授权。
    code = """import fcntl, os, shlex, sys
from pathlib import Path
key = sys.stdin.read().strip().split()
assert len(key) >= 2 and key[0] == 'ssh-ed25519'
directory = Path.home() / '.ssh'
directory.mkdir(mode=0o700, exist_ok=True)
path = directory / 'authorized_keys'
fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
with os.fdopen(fd, 'r+') as output:
    fcntl.flock(output, fcntl.LOCK_EX)
    original = output.read()
    found = False
    for line in original.splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        try: fields = shlex.split(line)
        except ValueError: continue
        if any(fields[i:i+2] == key[:2] for i in range(len(fields) - 1)):
            found = True
            break
    if not found:
        output.write(('\\n' if original and not original.endswith('\\n') else '')
                     + 'restrict ' + ' '.join(key[:2]) + ' primebuddy\\n')
        output.flush()
        os.fsync(output.fileno())
"""
    subprocess.run([*ssh, '-T', host, 'python3 -c ' + shlex.quote(code)],
                   input=public_key, text=True, check=True, timeout=20)


def setup_voice_ssh(host, identity, info):
    if passwordless(host):
        remote_identity([*ssh_base(), '-S', 'none', '-o', 'BatchMode=yes',
                         '-o', 'StrictHostKeyChecking=yes'], host,
                        {'series': identity['series'], 'soc_index': '0'})
        info('✓ 已验证到运控板的免密连接。')
        return
    ssh_directory = prepare_ssh_directory()
    directory = ssh_directory / 'primebuddy'
    directory.mkdir(exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    key = directory / ('id_ed25519_' + host.replace('@', '_'))
    public_path = Path(str(key) + '.pub')
    if key.is_symlink() or public_path.is_symlink():
        raise RuntimeError('专用 SSH 密钥路径不能是符号链接。')
    if public_path.exists() and not key.exists():
        raise RuntimeError('专用 SSH 私钥缺失，已保留原公钥；请检查 ~/.ssh/primebuddy 后重试。')
    info('正在配置运控板语音连接；如有提示，请输入运控板登录密码。密码不会保存。')
    with ssh_session(host, '运控板', ['-i', str(key)] if key.exists() else []) as ssh:
        remote_identity(ssh, host, {'series': identity['series'], 'soc_index': '0'})
        if not key.exists():
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'primebuddy',
                            '-f', str(key)], check=True, timeout=15)
        key.chmod(0o600)
        public = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', str(key)],
                                capture_output=True, text=True, check=True, timeout=15).stdout
        authorize_key(ssh, host, public)
    if not passwordless(host, ['-i', str(key), '-o', 'IdentitiesOnly=yes']):
        raise RuntimeError('运控板专用密钥验证失败；已保留配置，运行 primebuddy onboard 可重试。')
    configure_host(host, key)
    if not passwordless(host):
        raise RuntimeError('运控板 SSH 配置验证失败，请检查已有 SSH 设置后重试。')
    info('✓ 运控板语音连接已配置；专用密钥由程序保存，日常启动无需输入密码。')
