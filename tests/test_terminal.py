"""用真实 PTY 验证按键和终端恢复，不连接机器人或调用云服务。"""
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TerminalTests(unittest.TestCase):
    def start(self, expression, term='xterm-256color', size=(100, 30)):
        self.master, self.slave = pty.openpty()
        self.before = termios.tcgetattr(self.slave)
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), TERM=term, COLUMNS=str(size[0]), LINES=str(size[1]))
        code = 'import json\nfrom primebuddy.wizard import Wizard\nui = Wizard()\n'
        code += 'try:\n    result = ' + expression + '\n    print("RESULT", json.dumps(result))\n'
        code += 'except (KeyboardInterrupt, EOFError):\n    print("CANCELLED")\n'
        self.process = subprocess.Popen([sys.executable, '-c', code], stdin=self.slave,
                                        stdout=self.slave, stderr=self.slave, env=env)
        self.output = b''
        self.addCleanup(self.close)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=3)
        os.close(self.master)
        os.close(self.slave)

    def until(self, token):
        deadline = time.monotonic() + 4
        while token not in self.output:
            if time.monotonic() > deadline:
                self.fail('未出现预期输出：' + repr(self.output))
            if select.select([self.master], [], [], .1)[0]:
                self.output += os.read(self.master, 65536)

    def restored(self):
        self.process.wait(timeout=3)
        after = termios.tcgetattr(self.slave)
        # macOS 内核会自行设置 PENDIN（待重显输入），它不是被修改的终端模式。
        after[3] &= ~getattr(termios, 'PENDIN', 0)
        self.before[3] &= ~getattr(termios, 'PENDIN', 0)
        self.assertEqual(after, self.before)
        self.assertIn(b'\x1b[?25h', self.output)

    def test_arrows_select_without_numeric_input(self):
        self.start("ui.choose('Select model', ['first', 'second'])")
        self.until(b'second')
        os.write(self.master, b'\x1b[B\r')
        self.until(b'RESULT 1')
        self.restored()

    def test_space_multiselect_can_uncheck_default(self):
        self.start("ui.multiselect('Skills', ['music', 'lights', 'other'], default=[0])")
        self.until(b'other')
        os.write(self.master, b' \x1b[B \r')
        self.until(b'RESULT [1]')
        self.restored()

    def test_multiselect_allows_empty_selection(self):
        self.start("ui.multiselect('Skills', ['music'], default=[0])")
        self.until(b'music')
        os.write(self.master, b' \r')
        self.until(b'RESULT []')
        self.restored()

    def test_interrupt_restores_echo_and_cursor(self):
        self.start("ui.choose('Select', ['first', 'second'])")
        self.until(b'second')
        self.process.send_signal(signal.SIGINT)
        self.until(b'CANCELLED')
        self.restored()

    def test_dumb_terminal_uses_numbered_fallback(self):
        self.start("ui.multiselect('Skills', ['music', 'lights'])", term='dumb')
        self.until('请选择'.encode())
        os.write(self.master, b'2\n')
        self.until(b'RESULT [1]')
        self.assertNotIn(b'\x1b', self.output)

    def test_small_terminal_scrolls_to_choices_beyond_first_page(self):
        self.start("ui.choose('Select', ['choice-' + str(i) for i in range(12)])", size=(24, 8))
        self.until(b'choice-3')
        os.write(self.master, b'\x1b[B' * 8 + b'\r')
        self.until(b'RESULT 8')
        self.restored()

    def test_secret_input_is_not_echoed(self):
        self.start("bool(ui.ask('API Key', secret=True))")
        self.until('：'.encode())
        os.write(self.master, b'private-test-key\n')
        self.until(b'RESULT true')
        self.assertNotIn(b'private-test-key', self.output)


if __name__ == '__main__':
    unittest.main()
