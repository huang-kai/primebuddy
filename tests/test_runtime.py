import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from primebuddy.skills.registry import Registry
from primebuddy.core.engine import Context, execute, validate_plan

ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.root / 'home'))
        environment.start()
        self.addCleanup(environment.stop)
        adapters = patch('primebuddy.skills.registry.load_adapters', return_value={})
        adapters.start()
        self.addCleanup(adapters.stop)
        # 调度用本地无设备 fixture，旧原生入口仍经真实兼容子进程执行。
        definitions = [
            ('diangetai', 'play', {'artist': {'type': 'string'}, 'song': {'type': 'string'}}, ['speaker.music'],
             'def play(context, artist, song):\n    time.sleep(.3)\n    return "歌曲完成"\n'),
            ('yeelight', 'set_power', {'power': {'type': 'boolean'}, 'scope': {'type': 'string'},
             'room': {'type': 'string'}, 'device': {'type': 'string'}}, ['yeelight.home'],
             'def set_power(context, power, scope, room, device):\n    time.sleep(.02)\n    return "灯控完成"\n'),
        ]
        for name, action, parameters, resources, code in definitions:
            folder = self.root / 'skills' / name
            (folder / 'cfg').mkdir(parents=True)
            (folder / 'SKILL.md').write_text('---\nname: ' + name + '\ndescription: 调度测试\n---\nfixture')
            (folder / 'cfg/pkg.yaml').write_text(json.dumps({'primebuddy': {
                'entrypoint': 'actions.py', 'actions': {action: {'parameters': parameters, 'resources': resources}}}}))
            (folder / 'actions.py').write_text('import time\n'
                'def preflight(action, args, context):\n'
                '    if not context.simulate: raise ValueError("fixture prohibits real device operations")\n' + code)
        self.registry = Registry(self.root / 'skills')
        self.addCleanup(self.registry.close)
        self.plan = json.loads((ROOT / 'tests/fixtures/parallel.json').read_text())

    def test_all_business_and_voice_modules_are_project_local(self):
        for name in ('providers.qianwen.asr', 'providers.qianwen.tts',
                     'integrations.primebot.native_voice', 'integrations.primebot.native_agent',
                     'integrations.primebot.speech_audio', 'integrations.primebot.robot_audio',
                     'integrations.primebot.native_media', 'integrations.primebot.prepared_audio',
                     'integrations.primebot.prepare_audio_process'):
            module = importlib.import_module('primebuddy.' + name)
            self.assertTrue(Path(module.__file__).is_relative_to(ROOT))
        with patch('primebuddy.skills.manager.load_entry') as load:
            for skill in ('diangetai', 'yeelight'):
                runtime = self.registry.load(skill)
                host_module = importlib.import_module(type(runtime).__module__)
                self.assertTrue(Path(host_module.__file__).is_relative_to(ROOT))
        load.assert_not_called()
        self.assertFalse(any(getattr(module, '__file__', None)
                             and Path(module.__file__).is_relative_to(self.root / 'skills')
                             for module in tuple(sys.modules.values())))
        # 业务源仍属于 skill 自己，验证路径即可，不在宿主导入 SDK/第三方依赖。
        for skill, relative in [('diangetai', 'music/qqmusic_client.py'),
                                ('yeelight', 'business/feedback.py'), ('yeelight', 'integrations/mcp_client.py'),
                                ('yeelight', 'business/intent.py')]:
            folder = ROOT.parent / 'primebuddy-skills' / skill
            path = (folder / 'src' / relative).resolve()
            self.assertTrue(path.is_file() and path.is_relative_to(folder))


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
