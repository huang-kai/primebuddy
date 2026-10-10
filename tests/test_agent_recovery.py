"""使用真实恢复文件和模拟 Agent，覆盖中断后的下一次启动。"""
import contextlib
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from primebuddy.voice_support import native_agent as agent
from primebuddy.voice_support.robot_audio import AudioError, audio_session


class AgentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.record = self.runtime / 'agent_mode.json'
        self.mode = 'only_voice'
        self.running = True
        self.calls = []
        self.fail_restart = False
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, impl in [('agent_backend', lambda _: 'http'), ('agent_rpc', self.rpc),
                           ('agent_mode', self.read_write), ('restart_agent', self.restart),
                           ('confirm_agent', self.confirm)]:
            self.stack.enter_context(patch.object(agent, name, side_effect=impl))
        self.stack.enter_context(patch.object(agent, 'progress'))

    def save(self, previous='normal', **changes):
        data = dict(previous_running=True, previous_mode=previous,
                    purpose='music_session', backend='http')
        data.update(changes)
        self.record.write_text(json.dumps(data))
        return self.record.read_bytes()

    def rpc(self, runtime, operation, backend):
        self.calls.append(('rpc', operation))
        if operation == 'start':
            self.assertTrue(self.record.exists())
            self.running = True
        return {'status': 'Running(123)' if self.running else 'Idel(-1)',
                'pid': 123 if self.running else -1}

    def confirm(self, runtime, running, backend):
        self.assertEqual(self.running, running)

    def read_write(self, runtime, value=None):
        if value is not None:
            self.assertTrue(self.record.exists(), '改模式前必须保留恢复目标')
            self.calls.append(('mode', value))
            self.mode = value
        return self.mode

    def restart(self, runtime, backend):
        self.assertTrue(self.record.exists(), '重启中断时仍须能恢复')
        self.calls.append(('restart', self.mode))
        if self.fail_restart:
            raise AudioError('连接中断')

    def test_interrupted_session_takes_over_directly_and_preserves_original(self):
        original = self.save()
        with agent.voice_only_agent(self.runtime):
            self.assertEqual(self.mode, 'only_voice')
            self.assertEqual(self.record.read_bytes(), original)
            self.assertEqual([c for c in self.calls if c[0] == 'mode'],
                             [('mode', 'only_voice')])
            self.assertEqual([c for c in self.calls if c[0] == 'restart'],
                             [('restart', 'only_voice')])
        self.assertEqual(self.mode, 'normal')
        self.assertFalse(self.record.exists())

    def test_stopped_agent_with_record_is_started_before_takeover(self):
        self.save()
        self.running = False
        with agent.voice_only_agent(self.runtime):
            self.assertIn(('rpc', 'start'), self.calls)

    def test_original_only_voice_is_preserved(self):
        self.save(previous='only_voice')
        with agent.voice_only_agent(self.runtime):
            pass
        self.assertEqual(self.mode, 'only_voice')
        self.assertFalse(self.record.exists())

    def test_repeated_restart_failure_preserves_exact_record_and_can_retry(self):
        original = self.save()
        self.fail_restart = True
        with self.assertRaisesRegex(AudioError, '恢复.*记录.*保留'):
            with agent.voice_only_agent(self.runtime):
                self.fail('重启失败不应进入业务')
        self.assertEqual(self.record.read_bytes(), original)
        self.fail_restart = False
        with agent.voice_only_agent(self.runtime):
            self.assertEqual(self.mode, 'only_voice')
        self.assertFalse(self.record.exists())

    def test_mode_readback_mismatch_preserves_record(self):
        original = self.save()
        with patch.object(agent, 'agent_mode', return_value='only_voice'):
            with self.assertRaisesRegex(AudioError, '恢复'):
                with agent.voice_only_agent(self.runtime):
                    pass
        self.assertEqual(self.record.read_bytes(), original)

    def test_takeover_readback_mismatch_does_not_enter_business(self):
        self.save()
        with patch.object(agent, 'agent_mode', return_value='normal'):
            with self.assertRaisesRegex(AudioError, '未确认 only_voice'):
                with agent.voice_only_agent(self.runtime):
                    self.fail('接管模式未确认时不能进入业务')
        self.assertFalse(self.record.exists(), '已经确认回到原模式')

    def test_reentry_after_partial_restore_still_skips_normal_mode_write(self):
        self.save()
        self.mode = 'normal'
        with agent.voice_only_agent(self.runtime):
            self.assertEqual([c for c in self.calls if c[0] == 'mode'], [('mode', 'only_voice')])

    def test_another_live_session_blocks_reentry_before_agent_changes(self):
        original = self.save()
        with (self.runtime / 'audio.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(AudioError, '已有.*任务运行'):
                with audio_session(self.runtime):
                    with agent.voice_only_agent(self.runtime):
                        self.fail('已有会话时不能接管')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.record.read_bytes(), original)

    def test_invalid_records_never_change_agent(self):
        for changes in ({'previous_mode': 'unknown'}, {'previous_running': False},
                        {'purpose': 'unknown'}, {'backend': 'aima'}):
            with self.subTest(changes=changes):
                original = self.save(**changes)
                with self.assertRaises(AudioError):
                    with agent.voice_only_agent(self.runtime):
                        self.fail('非法记录不能接管')
                self.assertEqual(self.record.read_bytes(), original)
                self.assertEqual(self.calls, [])
        for content in ('broken json', '[]'):
            self.record.write_text(content)
            with self.assertRaises(AudioError):
                with agent.voice_only_agent(self.runtime):
                    self.fail('损坏记录不能接管')
            self.assertEqual(self.record.read_text(), content)

    def test_legacy_probe_record_is_not_deleted_or_interpreted(self):
        legacy = self.runtime / 'agent_voice_probe.json'
        legacy.write_text('{}')
        with self.assertRaisesRegex(AudioError, 'agent_voice_probe.json'):
            with agent.voice_only_agent(self.runtime):
                self.fail('其他程序记录不能套用当前恢复协议')
        self.assertTrue(legacy.exists())
        self.assertEqual(self.calls, [])

    def test_normal_start_and_business_exception_restore_mode(self):
        self.mode = 'normal'
        with self.assertRaisesRegex(ValueError, 'business'):
            with agent.voice_only_agent(self.runtime):
                raise ValueError('business')
        self.assertEqual(self.mode, 'normal')
        self.assertFalse(self.record.exists())

    def test_failed_exit_cleanup_is_recovered_on_next_start(self):
        self.mode = 'normal'
        with self.assertRaises(AudioError):
            with agent.voice_only_agent(self.runtime):
                self.fail_restart = True
        self.assertTrue(self.record.exists())
        self.fail_restart = False
        with agent.voice_only_agent(self.runtime):
            self.assertEqual(json.loads(self.record.read_text())['previous_mode'], 'normal')
        self.assertFalse(self.record.exists())
