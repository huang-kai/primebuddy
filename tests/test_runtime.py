import copy
import importlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from primebuddy.registry import Registry
from primebuddy.engine import Context, execute, validate_plan

ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry(ROOT.parent / 'primebuddy-skills')
        self.plan = json.loads((ROOT / 'examples/parallel.json').read_text())

    def test_all_business_and_voice_modules_are_project_local(self):
        for name in ('qwen_asr', 'qwen_tts', 'native_voice', 'native_agent', 'speech_audio', 'robot_audio'):
            module = importlib.import_module('primebuddy.voice_support.' + name)
            self.assertTrue(Path(module.__file__).is_relative_to(ROOT))
        for skill, names in [('diangetai', ('qqmusic_client', 'stream_audio')),
                             ('yeelight', ('client', 'mcp_client', 'intent'))]:
            package = self.registry.load(skill)
            for name in names:
                module = importlib.import_module(package.__name__ + '.' + name)
                self.assertTrue(Path(module.__file__).is_relative_to(ROOT.parent / 'primebuddy-skills' / skill))

    def test_planning_context_is_readonly_names_without_ids_or_private_fields(self):
        module = self.registry.load('yeelight')
        with patch.object(module.mcp_client, 'LocalMcp') as mcp:
            reader = mcp.return_value.__enter__.return_value
            reader.call.return_value = {'status': 'success', 'result': {'entities': [
                {'id': 'r1', 'type': 'room', 'name': '客厅'},
                {'id': 'd1', 'type': 'device', 'name': '天际线灯', 'roomId': 'r1', 'private': 'not-exported'}]}}
            value = self.registry.planning_context(Context(simulate=False, skill_configs={'yeelight': {'house_id': 'test'}}))
            self.assertEqual(value['yeelight']['devices'], [{'name': '天际线灯', 'room': '客厅'}])
            reader.call.assert_called_once_with('yeelight_home_list_entities', {'houseId': 'test', 'locale': 'zh-CN'})
            self.assertNotIn('not-exported', json.dumps(value))

    def test_parallel_tasks_overlap(self):
        # 灯控必须在歌曲结束前完成，避免把提交顺序误认为并行。
        events = []
        results = execute(self.plan, self.registry, Context(), lambda _: None, events.append)
        self.assertEqual(results['light']['status'], 'success')
        order = [(e['id'], e['state']) for e in events]
        self.assertLess(order.index(('light', 'success')), order.index(('music', 'success')))

    def test_dependency_waits_for_completion_and_cleanup(self):
        self.plan['tasks'][1]['after'] = ['music']
        events = []
        execute(self.plan, self.registry, Context(), lambda _: None, events.append)
        order = [(e['id'], e['state']) for e in events]
        self.assertLess(order.index(('music', 'success')), order.index(('light', 'running')))

    def test_bad_plans_never_announce_or_execute(self):
        mutations = [
            lambda p: p['tasks'][0].update(skill='shell'),
            lambda p: p['tasks'][1].update(action='blink'),
            lambda p: p['tasks'][1]['args'].update(power='false'),
            lambda p: p['tasks'][1]['args'].update(command='anything'),
            lambda p: p['tasks'][1].update(after=['missing']),
            lambda p: p['tasks'][0].update(after=['music']),
            lambda p: p['tasks'][1].update(id='music'),
        ]
        for mutate in mutations:
            proposal = copy.deepcopy(self.plan)
            mutate(proposal)
            spoken = []
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                execute(proposal, self.registry, Context(), spoken.append)
            self.assertEqual(spoken, [])

    def test_cross_task_cycle(self):
        self.plan['tasks'][0]['after'] = ['light']
        self.plan['tasks'][1]['after'] = ['music']
        with self.assertRaises(ValueError):
            validate_plan(self.plan, self.registry)

    def test_resource_conflict_and_explicit_order(self):
        self.plan['tasks'][1] = dict(self.plan['tasks'][0], id='second')
        with self.assertRaises(ValueError):
            validate_plan(self.plan, self.registry)
        self.plan['tasks'][1]['after'] = ['music']
        validate_plan(self.plan, self.registry)

    def test_announcement_failure_prevents_write(self):
        events = []
        def broken(_):
            raise RuntimeError('TTS failed')
        with self.assertRaises(RuntimeError):
            execute(self.plan, self.registry, Context(), broken, events.append)
        self.assertEqual(events, [])

    def test_preflight_entire_plan_before_side_effects(self):
        spoken = []
        with self.assertRaises(ValueError):
            execute(self.plan, self.registry, Context(simulate=False), spoken.append)
        self.assertEqual(spoken, [])

    def test_failure_skips_dependent_but_not_independent(self):
        real_load = self.registry.load
        music = real_load('diangetai')
        music.run = lambda *args: {'status': 'failed', 'message': '失败'}
        self.plan['tasks'][1]['after'] = ['music']
        with patch.object(self.registry, 'load', side_effect=lambda n: music if n == 'diangetai' else real_load(n)):
            result = execute(self.plan, self.registry, Context(), lambda _: None)
            self.assertEqual(result['light']['status'], 'skipped')
            self.plan['tasks'][1]['after'] = []
            result = execute(self.plan, self.registry, Context(), lambda _: None)
            self.assertEqual(result['light']['status'], 'success')

    def test_cancel_waits_for_cleanup(self):
        real_load = self.registry.load
        music = real_load('diangetai')
        started, cleaned = threading.Event(), threading.Event()
        def run(action, args, context):
            try:
                started.set()
                context.cancelled.wait(2)
                context.checkpoint()
            finally:
                cleaned.set()
        music.run = run
        def checkpoint():
            if started.is_set():
                raise KeyboardInterrupt()
        with patch.object(self.registry, 'load', side_effect=lambda n: music if n == 'diangetai' else real_load(n)):
            with self.assertRaises(KeyboardInterrupt):
                execute(self.plan, self.registry, Context(), lambda _: None, checkpoint=checkpoint)
        self.assertTrue(cleaned.is_set())

    def test_new_installed_skill_needs_no_host_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'hello'
            (folder / 'src').mkdir(parents=True)
            (folder / 'SKILL.md').write_text('---\nname: hello\ndescription: 问候\n---\n问候测试。')
            (folder / 'manifest.json').write_text(json.dumps({'name': 'hello', 'description': '问候',
                'version': '0.1.0', 'api_version': 1, 'setup': 'src/setup.py',
                'entrypoint': 'src/skill.py', 'actions': {'hello': {'parameters': {}, 'resources': []}}}))
            (folder / 'src/skill.py').write_text('''
from .actions import result
def validate(action, args): pass
def preflight(action, args, context): pass
def describe(action, args): return '准备问候'
def run(action, args, context): return result()
''')
            (folder / 'src/setup.py').write_text('def setup(ui, data, config): return {}\n')
            (folder / 'src/actions.py').write_text("def result(): return {'status': 'success', 'message': '你好'}\n")
            registry = Registry(temp)
            plan = {'reply': '', 'tasks': [{'id': 'one', 'skill': 'hello', 'action': 'hello', 'args': {}, 'after': []}]}
            self.assertEqual(execute(plan, registry, Context(), lambda _: None)['one']['message'], '你好')

    def test_clarification_calls_no_skills(self):
        spoken = []
        result = execute({'reply': '你想控制哪盏灯？', 'tasks': []}, self.registry, Context(), spoken.append)
        self.assertEqual(result, {})
        self.assertEqual(spoken, ['你想控制哪盏灯？'])

    def test_light_results_require_requested_state(self):
        module = self.registry.load('yeelight')
        args = self.plan['tasks'][1]['args']
        cases = [
            ({'verified': True, 'expectedValue': 'on', 'verifiedValue': 'on'}, 'success'),
            ({'verified': False, 'expectedValue': True, 'verifiedValue': True}, 'failed'),
            ({'verified': True, 'expectedValue': False, 'verifiedValue': False}, 'failed'),
            ({'verified': True, 'expectedValue': True, 'verifiedValue': False}, 'failed'),
        ]
        for result, status in cases:
            with self.subTest(result=result), patch.object(module.mcp_client, 'McpLightClient') as client:
                client.return_value.execute.return_value = {'status': 'success', 'result': result}
                actual = module.run('set_power', args, Context(simulate=False, skill_configs={'yeelight': {'house_id': 'test'}}))
                self.assertEqual(actual['status'], status)
                if status == 'failed':
                    self.assertNotIn('已打开', actual['message'])
                    self.assertNotIn('已关闭', actual['message'])

    def test_light_auth_and_fuzzy_name_do_not_report_success(self):
        module = self.registry.load('yeelight')
        for response, status in [({'status': 'auth_required'}, 'auth_required'),
                ({'status': 'clarification_required', 'suggestedIntent': {'device': '天际线灯'}}, 'clarification_required')]:
            with patch.object(module.mcp_client, 'McpLightClient') as client:
                client.return_value.execute.return_value = response
                actual = module.run('set_power', self.plan['tasks'][1]['args'], Context(simulate=False))
                self.assertEqual(actual['status'], status)

    def test_partial_light_write_is_only_read_back_not_retried(self):
        module = self.registry.load('yeelight')
        args = {'power': False, 'scope': 'device', 'device': '天际线灯', 'room': '客厅'}
        with patch.object(module.mcp_client, 'McpLightClient') as client, \
                patch.object(module.mcp_client, 'LocalMcp') as mcp, patch.object(module.time, 'sleep'):
            client.return_value.execute.return_value = {'status': 'partial'}
            reader = mcp.return_value.__enter__.return_value
            reader.call.side_effect = [
                {'status': 'success', 'result': {'properties': {'online': True, 'power': True}}},
                {'status': 'success', 'result': {'properties': {'online': True, 'power': False}}}]
            result = module.run('set_power', args, Context(simulate=False, skill_configs={'yeelight': {'house_id': 'test'}}))
            self.assertEqual(result['status'], 'success')
            client.return_value.execute.assert_called_once()
            self.assertEqual(reader.call.call_count, 2)
            self.assertTrue(all(c.args[0] == 'yeelight_home_get_state' for c in reader.call.call_args_list))

    def test_delayed_readback_is_bounded_and_offline_is_not_success(self):
        module = self.registry.load('yeelight')
        args = {'power': False, 'scope': 'device', 'device': '天际线灯', 'room': '客厅'}
        with patch.object(module.mcp_client, 'McpLightClient') as client, \
                patch.object(module.mcp_client, 'LocalMcp') as mcp, patch.object(module.time, 'sleep'):
            client.return_value.execute.return_value = {'status': 'partial'}
            reader = mcp.return_value.__enter__.return_value
            reader.call.return_value = {'status': 'success', 'result': {'properties': {'online': False, 'power': False}}}
            result = module.run('set_power', args, Context(simulate=False, skill_configs={'yeelight': {'house_id': 'test'}}))
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(reader.call.call_count, 3)
            client.return_value.execute.assert_called_once()


if __name__ == '__main__':
    unittest.main()
