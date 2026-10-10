"""平台命令的行为边界：启动真实会话，模拟入口不能触发设备运行。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from primebuddy.__main__ import run


class CliTests(unittest.TestCase):
    def test_empty_command_explains_entrypoints_without_loading_skills(self):
        output = io.StringIO()
        with patch('primebuddy.__main__.Registry') as registry, contextlib.redirect_stdout(output):
            run([])
        registry.assert_not_called()
        for command in ('start', 'simulate', 'onboard', 'skill'):
            self.assertIn(command, output.getvalue())
        self.assertNotIn('--allow-only-voice', output.getvalue())

    def test_start_runs_platform_without_requiring_a_business_skill(self):
        registry = Mock(skills={})
        output = io.StringIO()
        with patch('primebuddy.__main__.Registry', return_value=registry), \
                patch('primebuddy.voice.serve') as serve, contextlib.redirect_stdout(output):
            run(['start'])
        args = serve.call_args.args
        self.assertIs(args[0], registry)
        self.assertFalse(args[1].simulate)
        self.assertEqual(serve.call_args.kwargs['sdk_bridge'], 'namespaced')
        self.assertIn('only_voice', output.getvalue())
        self.assertIn('退出', output.getvalue())

    def test_start_passes_only_documented_advanced_options(self):
        with patch('primebuddy.__main__.Registry', return_value=Mock(skills={})), \
                patch('primebuddy.voice.serve') as serve, contextlib.redirect_stdout(io.StringIO()):
            run(['start', '--sdk-bridge', 'off', '--asr-model', 'qwen-audio-3.1-asr-flash'])
        self.assertEqual(serve.call_args.kwargs['sdk_bridge'], 'off')
        self.assertEqual(serve.call_args.args[1].asr_model, 'qwen-audio-3.1-asr-flash')

    def test_old_flags_and_unsafe_option_combinations_are_rejected_before_start(self):
        for args in (['--voice', '--live', '--allow-only-voice'],
                     ['start', '--live'], ['start', '--skills-dir', '/tmp/skills'],
                     ['simulate', '--text', '你好', '--live'], ['simulate']):
            with self.subTest(args=args), patch('primebuddy.voice.serve') as serve, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                run(args)
            self.assertEqual(error.exception.code, 2)
            serve.assert_not_called()

    def test_simulate_executes_json_with_simulation_context(self):
        proposal = {'reply': '测试', 'tasks': []}
        with tempfile.TemporaryDirectory() as temp:
            plan = Path(temp) / 'plan.json'
            plan.write_text(json.dumps(proposal))
            with patch('primebuddy.__main__.Registry', return_value=Mock(skills={})) as registry, \
                    patch('primebuddy.__main__.execute', return_value={}) as execute, \
                    patch('primebuddy.voice.serve') as serve, contextlib.redirect_stdout(io.StringIO()):
                run(['simulate', '--skills-dir', temp, '--plan', str(plan)])
        self.assertEqual(registry.call_args.args, (Path(temp),))
        self.assertEqual(execute.call_args.args[0], proposal)
        self.assertTrue(execute.call_args.args[2].simulate)
        serve.assert_not_called()

    def test_text_simulation_calls_planner_but_not_voice_session(self):
        proposal = {'reply': '测试', 'tasks': []}
        with patch('primebuddy.__main__.Registry', return_value=Mock(skills={})), \
                patch('primebuddy.planner.plan', return_value=proposal) as planner, \
                patch('primebuddy.__main__.execute', return_value={}) as execute, \
                patch('primebuddy.voice.serve') as serve, contextlib.redirect_stdout(io.StringIO()):
            run(['simulate', '--text', '测试'])
        self.assertEqual(planner.call_args.args[0], '测试')
        self.assertTrue(execute.call_args.args[2].simulate)
        serve.assert_not_called()

    def test_tts_warmup_is_separate_from_start_and_deduplicates_text(self):
        from primebuddy.voice_support.speech_cache import COMMON_PROMPTS
        cache = Mock()
        with patch('primebuddy.voice_support.speech_cache.SpeechCache', return_value=cache), \
                patch('primebuddy.voice_support.runtime.private_runtime', return_value=Path('/tmp/test-only')), \
                patch('primebuddy.__main__.Registry') as registry, \
                patch('primebuddy.voice.serve') as serve, contextlib.redirect_stdout(io.StringIO()):
            run(['tts', 'warmup', '--text', '测试', '--text', '测试'])
        self.assertEqual([call.args[0] for call in cache.prepare.call_args_list], [*COMMON_PROMPTS, '测试'])
        registry.assert_not_called()
        serve.assert_not_called()
