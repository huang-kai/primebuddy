"""所有 skill 共用的终端交互；业务流程由 skill 的 setup(ui, data, config) 决定。"""
import getpass
import os
import re
import select
import shutil
import sys
import termios
import tty
import unicodedata

from primebuddy.config.storage import SetupError


class Wizard:
    def __init__(self):
        if not sys.stdin.isatty():
            raise SetupError('安装引导需要交互终端。SSH 请使用 ssh -t；仅安装文件可用 --no-configure。')

    def info(self, message):
        print(message, flush=True)

    def interactive(self):
        return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get('TERM', 'dumb') != 'dumb'

    def paint(self, text, color):
        return '\033[' + color + 'm' + text + '\033[0m' if self.interactive() and 'NO_COLOR' not in os.environ else text

    def header(self, title, subtitle=''):
        self.info('\n' + self.paint('╭─ ' + title, '1;36'))
        if subtitle:
            self.info('│  ' + subtitle)
        self.info(self.paint('╰' + '─' * 38, '36'))

    def ask(self, label, default='', secret=False):
        while True:
            suffix = ' [回车保留]' if secret and default else (f' [{default}]' if default else '')
            value = (getpass.getpass if secret else input)(self.paint('◆ ', '36') + label + suffix + '：').strip()
            if value or default:
                return value or default
            self.info('不能为空，请重新输入。')

    def confirm(self, label, default=True):
        if self.interactive():
            return self.choose(label, ['是，继续', '否，稍后'], 0 if default else 1) == 0
        while True:
            answer = input(label + (' [Y/n]：' if default else ' [y/N]：')).strip().lower()
            if not answer:
                return default
            if answer in ('y', 'yes', '是'):
                return True
            if answer in ('n', 'no', '否'):
                return False
            self.info('请输入 y 或 n。')

    def choose(self, label, choices, default=0):
        if not choices or not 0 <= default < len(choices):
            raise ValueError('选择列表或默认项无效')
        if self.interactive():
            return self._select(label, choices, default, None)
        self.info(label)
        for index, choice in enumerate(choices, 1):
            self.info(f'  {index}. {choice}')
        while True:
            answer = input(f'请选择 [{default + 1}]：').strip()
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(choices):
                return int(answer) - 1
            self.info('请输入列表中的序号。')

    def multiselect(self, label, choices, default=None):
        selected = set(default or [])
        if not selected.issubset(range(len(choices))):
            raise ValueError('默认选中项无效')
        if not choices:
            return []
        if self.interactive():
            return self._select(label, choices, 0, selected)
        self.info(label)
        for index, choice in enumerate(choices):
            self.info(f'  {index + 1}. [{"x" if index in selected else " "}] {choice}')
        while True:
            answer = input('请选择序号（逗号分隔；0 全不选；回车保留默认）：').strip()
            if not answer:
                return sorted(selected)
            if answer == '0':
                return []
            numbers = re.split(r'[,，\s]+', answer)
            if all(n.isdigit() and 1 <= int(n) <= len(choices) for n in numbers):
                return sorted({int(n) - 1 for n in numbers})
            self.info('请输入列表中的序号，用逗号分隔。')

    @staticmethod
    def clip(text, width):
        """按显示列裁剪中文，避免窄终端自动换行破坏菜单重绘。"""
        result, used = '', 0
        for char in str(text):
            if not char.isprintable():
                char = ' '
            size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1)
            if used + size > width:
                break
            result += char
            used += size
        return result

    @staticmethod
    def key(fd):
        value = os.read(fd, 1)
        if not value or value == b'\x04':
            raise EOFError
        if value == b'\x03':
            raise KeyboardInterrupt
        if value == b'\x1b':
            # SSH 可将转义序列拆包；等待后续字节，不把方向键误当文本。
            while len(value) < 8 and select.select([fd], [], [], .2)[0]:
                tail = os.read(fd, 1)
                if not tail:
                    raise EOFError
                value += tail
                if len(value) >= 3 and (value[-1:].isalpha() or value.endswith(b'~')):
                    break
        return value

    def _select(self, label, choices, cursor, selected):
        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        rows = 0
        def clear():
            if rows:
                sys.stdout.write(f'\033[{rows}A\r\033[J')
        try:
            tty.setcbreak(fd)
            sys.stdout.write('\033[?25l')
            while True:
                clear()
                size = shutil.get_terminal_size((100, 30))
                visible = min(len(choices), max(1, size.lines - 4))
                start = min(max(0, cursor - visible + 1), len(choices) - visible)
                width = max(1, size.columns - 2)
                lines = [self.paint(self.clip('◆ ' + label, width), '1;36')]
                for index in range(start, start + visible):
                    checked = index == cursor if selected is None else index in selected
                    marker = ('●' if checked else '○') if selected is None else ('[x]' if checked else '[ ]')
                    text = ('❯ ' if index == cursor else '  ') + marker + ' ' + str(choices[index])
                    lines.append(self.paint(self.clip(text, width), '36' if index == cursor else '32' if checked else '2'))
                hint = '↑↓ 移动 · Enter 确认 · Ctrl+C 退出'
                if selected is not None:
                    hint = f'↑↓ 移动 · 空格选择 · Enter 确认 · 已选 {len(selected)}/{len(choices)}'
                if visible < len(choices):
                    hint += f' · {cursor + 1}/{len(choices)}'
                lines.append(self.paint(self.clip(hint, width), '2'))
                sys.stdout.write('\n'.join(lines) + '\n')
                sys.stdout.flush()
                rows = len(lines)
                key = self.key(fd)
                if key in (b'\x1b[A', b'\x1bOA', b'k'):
                    cursor = (cursor - 1) % len(choices)
                elif key in (b'\x1b[B', b'\x1bOB', b'j'):
                    cursor = (cursor + 1) % len(choices)
                elif key == b' ' and selected is not None:
                    selected.symmetric_difference_update({cursor})
                elif key in (b'\r', b'\n'):
                    result = cursor if selected is None else sorted(selected)
                    clear()
                    rows = 0
                    names = str(choices[cursor]) if selected is None else '、'.join(str(choices[i]) for i in result) or '暂不选择'
                    self.info(self.paint('✓ ' + label, '32') + ' · ' + names)
                    return result
        finally:
            # 包括 Ctrl+C、EOF、绘制异常在内，始终恢复终端回显及光标。
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)
            sys.stdout.write('\033[?25h')
            sys.stdout.flush()
