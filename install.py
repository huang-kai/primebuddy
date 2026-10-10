#!/usr/bin/env python3
"""就地安装：应用保留在解压目录，注册命令和终端 PATH，再进入配置向导。"""
import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

START = '# >>> PrimeBuddy PATH >>>'
END = '# <<< PrimeBuddy PATH <<<'


def write_atomic(path, content, mode=None):
    # 生成文件原子替换；若原位置是链接，不能沿链接覆盖其他应用文件。
    path.parent.mkdir(parents=True, exist_ok=True)
    permissions = mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o644)
    fd, name = tempfile.mkstemp(prefix='.primebuddy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
        os.chmod(name, permissions)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def configure_shell(environment):
    shell_home = Path.home()
    if Path(os.environ.get('SHELL', '/bin/bash')).name == 'zsh':
        shell_home = Path(os.environ.get('ZDOTDIR', shell_home)).expanduser()
        files = [shell_home / '.zshrc', shell_home / '.zprofile']
    else:
        login = next((shell_home / name for name in ('.bash_profile', '.bash_login')
                      if (shell_home / name).exists()), shell_home / '.profile')
        files = [shell_home / '.bashrc', login]
    quoted = shlex.quote(str(environment))
    block = f'{START}\n[ ! -f {quoted} ] || . {quoted}\n{END}\n'
    pattern = re.compile(re.escape(START) + r'.*?' + re.escape(END) + r'\n?', re.S)
    for path in files:
        # 用户可能通过 dotfiles 链接管理 shell 配置，保留该链接和原权限。
        path = path.resolve() if path.is_symlink() else path
        original = path.read_text() if path.exists() else ''
        # 重复安装或切换目录只留一份 PATH 入口，不覆盖用户其他设置。
        text = pattern.sub('', original).rstrip('\n')
        write_atomic(path, (text + '\n' if text else '') + block)


def install(data_dir, bin_dir):
    if sys.version_info < (3, 10):
        raise RuntimeError('需要 Python 3.10 或更高版本。')
    if sys.prefix != sys.base_prefix:
        raise RuntimeError('请退出虚拟环境，使用 T1 系统 python3 运行安装器，以匹配系统 ROS 2。')
    source = Path(__file__).resolve().parent
    if not (source / 'src/primebuddy/__main__.py').is_file():
        raise RuntimeError('缺少框架源码，请完整解压安装包。')
    if ':' in str(bin_dir):
        raise RuntimeError('命令目录不能包含 PATH 分隔符“:”。')
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)
    launcher = source / 'primebuddy'
    # 系统解释器固定；继承当前终端的 ROS 环境。配置路径沿用旧版，不搬动登录数据。
    content = '#!/bin/sh\n'
    content += 'if [ -z "${PRIMEBUDDY_HOME:-}" ]; then\n'
    content += '  export PRIMEBUDDY_HOME=' + shlex.quote(str(data_dir)) + '\nfi\n'
    content += 'export PYTHONPATH=' + shlex.quote(str(source / 'src')) + '"${PYTHONPATH:+:$PYTHONPATH}"\n'
    content += 'exec ' + shlex.quote(sys.executable) + ' -m primebuddy "$@"\n'
    write_atomic(launcher, content, 0o755)
    bin_dir.mkdir(parents=True, exist_ok=True)
    command = bin_dir / 'primebuddy'
    if command != launcher:
        if command.is_dir():
            raise RuntimeError('命令位置是目录，无法创建入口：' + str(command))
        with tempfile.TemporaryDirectory(prefix='.primebuddy-', dir=bin_dir) as temp:
            pending = Path(temp) / 'primebuddy'
            pending.symlink_to(launcher)
            os.replace(pending, command)
    environment = source / 'env.sh'
    quoted = shlex.quote(str(bin_dir))
    write_atomic(environment, f'# PrimeBuddy 命令路径，可重复加载\ncase ":$PATH:" in\n'
                 f'  *:{quoted}:*) ;;\n  *) export PATH={quoted}:"$PATH" ;;\nesac\n')
    configure_shell(environment)
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('PRIMEBUDDY_HOME', Path.home() / '.local/share/primebuddy')),
                        help='配置和已安装技能的数据目录；应用代码始终在当前解压目录')
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    parser.add_argument('--no-onboard', action='store_true', help='仅注册命令，稍后执行 primebuddy onboard')
    args = parser.parse_args()
    try:
        launcher = install(args.data_dir.expanduser().resolve(), args.bin_dir.expanduser().resolve())
        source = Path(__file__).resolve().parent
        print('PrimeBuddy 已安装到：' + str(source), flush=True)
        print('命令入口：' + str(launcher), flush=True)
        print('新终端已配置 PATH；当前终端可执行：source ' + shlex.quote(str(source / 'env.sh')), flush=True)
        if not args.no_onboard:
            return subprocess.call([str(launcher), 'onboard'])
        return 0
    except (KeyboardInterrupt, EOFError):
        print('配置已取消，命令已安装；可运行 primebuddy onboard 继续。', file=sys.stderr)
        return 130
    except (RuntimeError, OSError) as error:
        print('安装未完成：' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
