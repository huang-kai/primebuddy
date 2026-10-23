"""主入口、配置终端交互与模拟执行。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from primebuddy.cli.commands import dispatch_command as run
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time


class CliTests(unittest.TestCase):
    def test_update_is_dispatched_without_loading_skills_or_voice(self):
        for arguments, expected in ((['update'], dict(check=False, version=None, recover=False)),
                (['update', '--check', '--version', '1.1.0'], dict(check=True, version='1.1.0', recover=False)),
                (['update', '--recover'], dict(check=False, version=None, recover=True))):
            with self.subTest(arguments=arguments), patch('primebuddy.tools.update.run_update') as update, \
                    patch('primebuddy.cli.commands.Registry') as registry, patch('primebuddy.voice.session.serve') as serve:
                run(arguments)
            update.assert_called_once_with(**expected)
            registry.assert_not_called()
            serve.assert_not_called()

    def test_empty_command_explains_entrypoints_without_loading_skills(self):
        output = io.StringIO()
        with patch('primebuddy.cli.commands.Registry') as registry, contextlib.redirect_stdout(output):
            run([])
        registry.assert_not_called()
        for command in ('start', 'simulate', 'onboard', 'skill'):
            self.assertIn(command, output.getvalue())
        self.assertNotIn('--allow-only-voice', output.getvalue())

    def test_start_runs_platform_without_requiring_a_business_skill(self):
        registry = Mock(skills={})
        output = io.StringIO()
        with patch('primebuddy.cli.commands.Registry', return_value=registry),\
                patch('primebuddy.voice.session.serve') as serve, self.assertLogs('primebuddy', level='INFO') as output:
            run(['start'])
        args = serve.call_args.args
        self.assertIs(args[0], registry)
        self.assertFalse(args[1].simulate)
        self.assertEqual(serve.call_args.kwargs['sdk_bridge'], 'namespaced')
        self.assertIn('only_voice', '\n'.join(output.output))
        self.assertIn('退出', '\n'.join(output.output))

    def test_start_passes_only_documented_advanced_options(self):
        with patch('primebuddy.cli.commands.Registry', return_value=Mock(skills={})),\
                patch('primebuddy.voice.session.serve') as serve, contextlib.redirect_stdout(io.StringIO()):
            run(['start', '--sdk-bridge', 'off', '--asr-model', 'qwen-audio-3.1-asr-flash'])
        self.assertEqual(serve.call_args.kwargs['sdk_bridge'], 'off')
        self.assertEqual(serve.call_args.args[1].asr_model, 'qwen-audio-3.1-asr-flash')

    def test_old_flags_and_unsafe_option_combinations_are_rejected_before_start(self):
        for args in (['--voice', '--live', '--allow-only-voice'],
                     ['start', '--live'], ['start', '--skills-dir', '/tmp/skills'],
                     ['simulate', '--text', '你好', '--live'], ['simulate']):
            with self.subTest(args=args), patch('primebuddy.voice.session.serve') as serve,\
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                run(args)
            self.assertEqual(error.exception.code, 2)
            serve.assert_not_called()

    def test_simulate_executes_json_with_simulation_context(self):
        proposal = {'reply': '测试', 'tasks': []}
        with tempfile.TemporaryDirectory() as temp:
            plan = Path(temp) / 'plan.json'
            plan.write_text(json.dumps(proposal))
            with patch('primebuddy.cli.commands.Registry', return_value=Mock(skills={})) as registry,\
                    patch('primebuddy.cli.commands.execute', return_value={}) as execute,\
                    patch('primebuddy.voice.session.serve') as serve, contextlib.redirect_stdout(io.StringIO()):
                run(['simulate', '--skills-dir', temp, '--plan', str(plan)])
        self.assertEqual(registry.call_args.args, (Path(temp),))
        self.assertEqual(execute.call_args.args[0], proposal)
        self.assertTrue(execute.call_args.args[2].simulate)
        serve.assert_not_called()

    def test_text_simulation_calls_planner_but_not_voice_session(self):
        proposal = {'reply': '测试', 'tasks': []}
        with patch('primebuddy.cli.commands.Registry', return_value=Mock(skills={})),\
                patch('primebuddy.core.planner.plan', return_value=proposal) as planner,\
                patch('primebuddy.cli.commands.execute', return_value={}) as execute,\
                patch('primebuddy.voice.session.serve') as serve, contextlib.redirect_stdout(io.StringIO()):
            run(['simulate', '--text', '测试'])
        self.assertEqual(planner.call_args.args[0], '测试')
        self.assertTrue(execute.call_args.args[2].simulate)
        serve.assert_not_called()

    def test_tts_warmup_is_separate_from_start_and_deduplicates_text(self):
        from primebuddy.voice.speech_cache import COMMON_PROMPTS
        cache = Mock()
        with patch('primebuddy.voice.speech_cache.SpeechCache', return_value=cache),\
                patch('primebuddy.config.runtime.private_runtime', return_value=Path('/tmp/test-only')),\
                patch('primebuddy.cli.commands.Registry') as registry,\
                patch('primebuddy.voice.session.serve') as serve, contextlib.redirect_stdout(io.StringIO()):
            run(['tts', 'warmup', '--text', '测试', '--text', '测试'])
        self.assertEqual([call.args[0] for call in cache.prepare.call_args_list], [*COMMON_PROMPTS, '测试'])
        registry.assert_not_called()
        serve.assert_not_called()


ROOT = Path(__file__).resolve().parents[1]


class TerminalTests(unittest.TestCase):
    def start(self, expression, term='xterm-256color', size=(100, 30)):
        self.master, self.slave = pty.openpty()
        self.before = termios.tcgetattr(self.slave)
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), TERM=term, COLUMNS=str(size[0]), LINES=str(size[1]))
        code = 'import json\nfrom primebuddy.cli.wizard import Wizard\nui = Wizard()\n'
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
