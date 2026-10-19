"""普通 CLI 技能与宿主适配、监督器、调度器的真实进程集成。"""
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from primebuddy.core.engine import CleanupUnconfirmed, Context, TurnInterrupted, execute, validate_plan
from primebuddy.integrations.primebot.native_voice import NativeVoiceInput
from primebuddy.skills.processes import run_process
from primebuddy.skills.registry import Registry


CLI = '''import argparse
import json
import os
from pathlib import Path
import signal
import time

if __name__ != '__main__':
    raise AssertionError('CLI must not be imported')

parser = argparse.ArgumentParser()
parser.add_argument('operation')
parser.add_argument('--runtime', type=Path, required=True)
parser.add_argument('--mode', default='ok')
args = parser.parse_args()
args.runtime.mkdir(parents=True, exist_ok=True)
busy = args.runtime / 'busy'
stuck = args.runtime / 'stuck'
if args.operation == 'status':
    print(json.dumps({'status': 'success', 'resources_released': not busy.exists()}))
elif args.operation == 'recover':
    if not stuck.exists():
        busy.unlink(missing_ok=True)
        (args.runtime / 'recovered').touch()
    print(json.dumps({'status': 'success', 'resources_released': not busy.exists()}))
elif args.operation == 'play':
    with (args.runtime / 'executed').open('a') as log:
        log.write(args.mode + '\\n')
    busy.touch()
    if args.mode == 'stuck':
        stuck.touch()
        os._exit(7)
    if args.mode == 'abnormal':
        os._exit(7)
    if args.mode == 'orphan':
        print('done')
        raise SystemExit(0)
    if args.mode == 'hang':
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (args.runtime / 'ready').touch()
        time.sleep(60)
    elif args.mode in ('wait', 'slow-wait'):
        cancelled = False
        def stop(*_):
            global cancelled
            cancelled = True
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        try:
            (args.runtime / 'ready').touch()
            while not cancelled:
                time.sleep(.01)
        finally:
            if args.mode == 'slow-wait':
                (args.runtime / 'cleanup-started').touch()
                deadline = time.monotonic() + 5
                while not (args.runtime / 'cleanup-continue').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                if not (args.runtime / 'cleanup-continue').exists():
                    raise RuntimeError('cleanup checkpoint did not release fixture')
            busy.unlink(missing_ok=True)
            (args.runtime / 'cleaned').touch()
    else:
        busy.unlink(missing_ok=True)
        print('done:' + str(os.getpid()))
'''


@unittest.skipUnless(os.name == 'posix', 'POSIX 进程组')
class SkillProcessRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skills = self.root / 'skills'
        self.skill = self.skills / 'media-fixture'
        self.skill.mkdir(parents=True)
        (self.skill / 'SKILL.md').write_text('---\nname: media-fixture\ndescription: 普通 CLI 测试\n---\n运行 main.py。')
        (self.skill / 'main.py').write_text(CLI)
        adapter = {'media-fixture': {'actions': {'play': {
            'parameters': {'mode': {'type': 'string'}}, 'resources': ['speaker.music'],
            'command': ['main.py', 'play', '--runtime', '{runtime}', '--mode', '{mode}'],
            'timeout_seconds': 5}},
            'status': ['main.py', 'status', '--runtime', '{runtime}'],
            'recover': ['main.py', 'recover', '--runtime', '{runtime}']}}
        environment = patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.root / 'home'))
        environment.start()
        self.addCleanup(environment.stop)
        adapters = patch('primebuddy.skills.registry.load_adapters', return_value=adapter)
        adapters.start()
        self.addCleanup(adapters.stop)
        # 用真实监督器，只缩短测试用信号宽限，不改变宿主声明的硬运行期限。
        def supervise(command, **kwargs):
            return run_process(command, **kwargs, stop_grace=(1, .25, 1))
        supervision = patch('primebuddy.skills.runtime.run_process', side_effect=supervise)
        supervision.start()
        self.addCleanup(supervision.stop)
        self.registry = Registry(self.skills)
        self.addCleanup(self.registry.close)
        self.context = Context(runtime=self.root / 'runtime', simulate=False)
        self.data = self.context.runtime / 'media-fixture'

    def task(self, name='first', mode='ok', after=None, *, script=False):
        args = ({'path': 'main.py', 'arguments': ['play', '--runtime', str(self.data), '--mode', mode]}
                if script else {'mode': mode})
        return dict(id=name, skill='media-fixture', action='script' if script else 'play', args=args, after=after or [])

    def plan(self, *tasks):
        return {'reply': '', 'tasks': list(tasks)}

    def executed(self):
        path = self.data / 'executed'
        return path.read_text().splitlines() if path.exists() else []

    def test_plain_cli_text_succeeds_without_native_import(self):
        announcements = []
        with patch('primebuddy.skills.manager.load_entry') as load:
            results = execute(self.plan(self.task()), self.registry, self.context, announce=announcements.append)
        load.assert_not_called()
        result = results['first']
        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['message'].startswith('done:'))
        self.assertNotEqual(int(result['message'].split(':')[1]), os.getpid())
        self.assertFalse((self.data / 'busy').exists())
        self.assertEqual(announcements[-1], result['message'])
        self.assertNotIn('primebuddy', CLI)

    def test_cancel_and_turn_interrupt_wait_for_cleanup_before_returning(self):
        for interrupted in (False, True):
            with self.subTest(turn_interrupt=interrupted):
                for name in ('ready', 'cleaned'):
                    (self.data / name).unlink(missing_ok=True)
                context = Context(runtime=self.context.runtime, simulate=False)
                announcements = []
                def checkpoint():
                    if (self.data / 'ready').exists():
                        if interrupted:
                            raise TurnInterrupted('external cancellation')
                        context.cancelled.set()
                with self.assertRaises(TurnInterrupted if interrupted else RuntimeError):
                    execute(self.plan(self.task(mode='wait')), self.registry, context,
                            announce=announcements.append, checkpoint=checkpoint)
                self.assertTrue((self.data / 'cleaned').exists())
                self.assertFalse((self.data / 'busy').exists())
                self.assertEqual(len(announcements), 1)  # 只允许任务开始前播报。

    def test_hard_timeout_stops_process_and_skips_dependent_task(self):
        self.registry.skills['media-fixture']['actions']['play']['timeout_seconds'] = 1.5
        started = time.monotonic()
        result = execute(self.plan(self.task(mode='hang'), self.task('second', after=['first'])),
                         self.registry, self.context, announce=lambda _: None)
        self.assertEqual(result['first']['status'], 'failed')
        self.assertEqual(result['first']['termination_reason'], 'timed_out')
        self.assertEqual(result['second']['status'], 'skipped')
        self.assertEqual(self.executed(), ['hang'])
        self.assertTrue((self.data / 'recovered').exists())
        records = [json.loads(path.read_text()) for path in (self.context.runtime / 'processes').glob('*.json')]
        timed_out = [record for record in records if record['termination_reason'] == 'timed_out']
        self.assertEqual(len(timed_out), 1)
        self.assertEqual(timed_out[0]['timeout_seconds'], 1.5)
        self.assertEqual(timed_out[0]['returncode'], -signal.SIGKILL)
        self.assertTrue(timed_out[0]['cleanup_confirmed'])
        self.assertLess(time.monotonic() - started, 15)

    def test_slow_process_cleanup_keeps_new_generation_vad_boundaries(self):
        node = SimpleNamespace(get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=1)))
        microphone = NativeVoiceInput(None, SimpleNamespace(node=node))
        microphone.monitor_interrupt(0)
        frames = []
        announcements = []
        def checkpoint():
            if (self.data / 'ready').exists():
                self.assertEqual(microphone.begin_interrupt(0), 1)
                raise TurnInterrupted('external cancellation')
        def cleanup_checkpoint():
            self.assertTrue(self.context.cancelled.is_set())
            self.assertTrue(microphone.events.empty())
            if (self.data / 'cleanup-started').exists() and len(frames) < 2:
                state = (1, 3)[len(frames)]
                microphone.on_audio(SimpleNamespace(stream_id=0, audio_vad_state=SimpleNamespace(value=state),
                    header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=2)),
                    audio_data=bytes([state]) * 6400))
                frames.append(state)
                if len(frames) == 2:
                    (self.data / 'cleanup-continue').touch()
        def supervise(command, **kwargs):
            return run_process(command, **kwargs, stop_grace=(6, .25, 1))
        with patch('primebuddy.skills.runtime.run_process', side_effect=supervise), self.assertRaises(TurnInterrupted):
            execute(self.plan(self.task(mode='slow-wait'), self.task('second', after=['first'])),
                    self.registry, self.context, announce=announcements.append, checkpoint=checkpoint,
                    cleanup_checkpoint=cleanup_checkpoint)
        self.assertEqual(frames, [1, 3])
        self.assertTrue((self.data / 'cleaned').exists())
        self.assertTrue(self.context.cancelled.is_set())
        self.assertEqual(self.executed(), ['slow-wait'])
        self.assertEqual(len(announcements), 1)
        microphone.resume_after_interrupt(1)
        self.assertEqual(microphone.events.get_nowait(),
                         (1, 'utterance', b'\x01' * 6400 + b'\x03' * 6400))

    def test_abnormal_exit_recovers_independently_then_next_turn_can_run(self):
        first = execute(self.plan(self.task(mode='abnormal')), self.registry, self.context, announce=lambda _: None)
        self.assertEqual(first['first']['status'], 'failed')
        self.assertTrue((self.data / 'recovered').exists())
        self.assertFalse((self.data / 'busy').exists())
        second = execute(self.plan(self.task('next')), self.registry,
                         Context(runtime=self.context.runtime, simulate=False), announce=lambda _: None)
        self.assertEqual(second['next']['status'], 'success')
        self.assertEqual(self.executed(), ['abnormal', 'ok'])
        self.assertFalse((self.data / 'cleanup-unconfirmed.json').exists())

    def test_unreleased_device_blocks_tts_dependencies_and_later_turn(self):
        announcements = []
        with self.assertRaises(CleanupUnconfirmed):
            execute(self.plan(self.task(mode='stuck'), self.task('second', after=['first'])),
                    self.registry, self.context, announce=announcements.append)
        self.assertEqual(self.executed(), ['stuck'])
        self.assertEqual(len(announcements), 1)
        self.assertTrue((self.data / 'cleanup-unconfirmed.json').exists())
        retry_announcements = []
        with self.assertRaises(CleanupUnconfirmed):
            execute(self.plan(self.task('next')), self.registry,
                    Context(runtime=self.context.runtime, simulate=False), announce=retry_announcements.append)
        self.assertEqual(retry_announcements, [])
        self.assertEqual(self.executed(), ['stuck'])

    def test_profile_does_not_expose_raw_script_resource_bypass(self):
        self.assertNotIn('script', self.registry.skills['media-fixture']['actions'])
        with self.assertRaises(ValueError):
            validate_plan(self.plan(self.task(script=True), self.task('second')), self.registry)

    def test_profile_status_guard_and_raw_script_rejection_prevent_bypass(self):
        # 成功退出但遗留设备占用，也不能播报完成或继续任务。
        announcements = []
        with self.assertRaises(CleanupUnconfirmed):
            execute(self.plan(self.task(mode='orphan'), self.task('second', after=['first'])),
                    self.registry, self.context, announce=announcements.append)
        self.assertEqual(self.executed(), ['orphan'])
        self.assertEqual(len(announcements), 1)
        # 已被隔离后，模型也不能绕过宿主映射改为原始脚本。
        with self.assertRaises(ValueError):
            execute(self.plan(self.task('retry', script=True)), self.registry,
                    Context(runtime=self.context.runtime, simulate=False), announce=announcements.append)
        self.assertEqual(self.executed(), ['orphan'])

    def test_supervisor_exception_cannot_be_overridden_by_idle_device(self):
        def supervise(command, **kwargs):
            if 'play' in command:
                # 模拟进程监督故障，不能据设备此刻空闲推断残留进程不会再写入。
                raise OSError('probe failed')
            return run_process(command, **kwargs)
        announcements = []
        with patch('primebuddy.skills.runtime.run_process', side_effect=supervise), self.assertRaises(CleanupUnconfirmed):
            execute(self.plan(self.task()), self.registry, self.context, announce=announcements.append)
        self.assertEqual(len(announcements), 1)
        self.assertTrue((self.data / 'cleanup-unconfirmed.json').exists())
        self.assertEqual(self.executed(), [])

    def test_abnormal_hardware_cli_without_status_remains_isolated(self):
        runtime = self.registry.load('media-fixture')
        runtime.adapter.pop('status')
        runtime.adapter.pop('recover')
        announcements = []
        with self.assertRaises(CleanupUnconfirmed):
            execute(self.plan(self.task(mode='abnormal')), self.registry, self.context, announce=announcements.append)
        self.assertEqual(len(announcements), 1)
        self.assertTrue((self.data / 'cleanup-unconfirmed.json').exists())

    def test_context_query_cannot_hide_uncertain_cleanup(self):
        with patch.object(self.registry.load('media-fixture'), 'planning_context', side_effect=CleanupUnconfirmed()):
            with self.assertRaises(CleanupUnconfirmed):
                self.registry.planning_context(self.context, 'media-fixture')


if __name__ == '__main__':
    unittest.main()
