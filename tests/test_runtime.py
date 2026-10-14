import copy
import importlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from primebuddy.skills.registry import Registry
from primebuddy.core.engine import Context, execute, validate_plan

ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry(ROOT.parent / 'primebuddy-skills')
        self.plan = json.loads((ROOT / 'tests/fixtures/parallel.json').read_text())

    def test_all_business_and_voice_modules_are_project_local(self):
        for name in ('providers.qianwen.asr', 'providers.qianwen.tts',
                     'integrations.t1.native_voice', 'integrations.t1.native_agent',
                     'integrations.t1.speech_audio', 'integrations.t1.robot_audio',
                     'integrations.t1.native_media', 'integrations.t1.prepared_audio',
                     'integrations.t1.prepare_audio_process'):
            module = importlib.import_module('primebuddy.' + name)
            self.assertTrue(Path(module.__file__).is_relative_to(ROOT))
        for skill, names in [('diangetai', ('music.qqmusic_client',)),
                             ('yeelight', ('business.feedback', 'integrations.mcp_client', 'business.intent'))]:
            package = self.registry.load(skill)
            for name in names:
                module = importlib.import_module(package.__name__ + '.' + name)
                self.assertTrue(Path(module.__file__).is_relative_to(ROOT.parent / 'primebuddy-skills' / skill))


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
            for directory in ('src', 'cfg', 'docs'):
                (folder / directory).mkdir(parents=True)
            (folder / 'SKILL.md').write_text('---\nname: hello\ndescription: 问候\n---\n问候测试。')
            (folder / 'cfg/pkg.yaml').write_text(json.dumps({
                'package_name': 'hello', 'description': '问候', 'author': 'test-only', 'user_id': 'robot_local',
                'sdk_version': 'v1.0.0', 'version': '0.1.0', 'primebuddy': {
                    'api_version': 1, 'setup': 'src/setup.py', 'entrypoint': 'src/skill.py',
                    'actions': {'hello': {'parameters': {}, 'resources': []}}}}))
            (folder / 'requirements.txt').write_text('')
            (folder / 'src/skill.py').write_text('''
from .actions import result
def hello(context): return result()
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





    def test_parallel_batch_announces_before_any_skill_can_claim_speaker(self):
        started = threading.Event()
        announcements = []
        def run(*args):
            started.set()
            return {'status': 'success', 'message': '完成'}
        def announce(text):
            if text != '完成':
                announcements.append(text)
                if len(announcements) == 2:
                    self.assertFalse(started.wait(.1), '首个技能提前占用扬声器会阻塞第二个任务播报')
        with patch.object(self.registry.load('diangetai'), 'run', side_effect=run), \
                patch.object(self.registry.load('yeelight'), 'run', side_effect=run):
            execute(self.plan, self.registry, Context(), announce)
        self.assertTrue(started.is_set())


if __name__ == '__main__':
    unittest.main()
