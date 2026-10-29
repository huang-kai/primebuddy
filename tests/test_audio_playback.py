"""音频准备、原厂播放反馈及退出清理。"""
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from primebuddy.voice.audio import AudioOutput
from primebuddy.core.engine import CleanupUnconfirmed
from primebuddy.integrations.primebot.robot_audio import AudioError
from types import SimpleNamespace
from unittest.mock import Mock
from primebuddy.integrations.primebot.native_media import NativeMedia, matched_publishers
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import wave
from primebuddy.integrations.primebot import prepare_audio_process


class AudioOutputTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        events = self.events
        class Robot:
            def __init__(self, runtime): pass
            def spin(self, *args, **kwargs): events.append('spin')
            def close(self): events.append('node_close')
        class Prepared:
            def __init__(self, *args, **kwargs):
                self.path, self.seconds, self.limited = '/tmp/primebuddy-audio-test/audio.wav', 60, True
                events.append('prepare')
            def wait_ready(self, checkpoint): checkpoint()
            def close(self): events.append('file_cleanup')
        class Media:
            def __init__(self, robot):
                self.started = None
                self.count = 0
            def start(self, path, seconds):
                self.count = 0
                self.started = None
                events.append(('play', seconds))
            def poll(self):
                self.count += 1
                self.started = 1
                return self.count > 1
            def stop(self): events.append('stop_owned')
            def close(self): pass
        self.Robot, self.Prepared, self.Media = Robot, Prepared, Media
        self.output = AudioOutput(Path('/unused'), lambda: None)
        self.addCleanup(self.output.close)
        for name, value in [('RobotAudio', Robot), ('PreparedAudio', Prepared), ('NativeMedia', Media)]:
            patcher = patch('primebuddy.voice.audio.' + name, value)
            patcher.start(); self.addCleanup(patcher.stop)

    def test_callbacks_and_cleanup_follow_confirmed_playback(self):
        result = self.output.play('https://example.com/a.mp3', max_seconds=60, expected_seconds=120,
            before_play=lambda: self.events.append('ready'), on_started=lambda: self.events.append('started'),
            on_tick=lambda: self.events.append('tick'), on_finished=lambda: self.events.append('finished'))
        self.output.close()
        self.assertTrue(result['limited'])
        events = [e for e in self.events if e != 'spin']
        self.assertEqual(events, ['prepare', 'ready', ('play', 60), 'started', 'tick',
                                  'finished', 'stop_owned', 'file_cleanup', 'node_close'])

    def test_recovery_requires_confirmed_idle_audio(self):
        self.output.ensure_idle()
        self.output.media = NS(submitted=True, close=lambda: None)
        with self.assertRaises(CleanupUnconfirmed):
            self.output.ensure_idle()
        self.output.media.submitted = False
        self.output.ensure_idle()
        with self.output.lock:
            with self.assertRaises(CleanupUnconfirmed):
                self.output.ensure_idle()
        self.output.close()
        with self.assertRaises(CleanupUnconfirmed):
            self.output.ensure_idle()

    def test_failed_preparation_never_runs_motion_or_play(self):
        with patch.object(self.Prepared, 'wait_ready', side_effect=AudioError('decode failed')):
            with self.assertRaises(AudioError):
                self.output.play('https://example.com/a', before_play=lambda: self.fail('motion'))
        self.output.close()
        self.assertEqual(self.events, ['prepare', 'file_cleanup', 'node_close'])

    def test_failed_callback_stops_audio_before_file_cleanup(self):
        def failed(): raise RuntimeError('effect failed')
        with self.assertRaises(RuntimeError):
            self.output.play('https://example.com/a', on_started=failed)
        self.output.close()
        self.assertEqual(self.events[-3:], ['stop_owned', 'file_cleanup', 'node_close'])

    def test_cancel_while_waiting_for_speaker_does_not_create_node(self):
        self.output.lock.acquire()
        def cancelled(): raise RuntimeError('cancelled')
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.output.play('https://example.com/a', checkpoint=cancelled)
        self.output.lock.release()
        self.assertEqual(self.events, [])

    def test_invalid_input_has_no_side_effects(self):
        for source, args in [('file:///robot/protected/no', {}), ('https://user:pass@example.com/a', {}),
                             ('https://example.com/a', {'max_seconds': -1}),
                             ('https://example.com/a', {'expected_seconds': float('nan')})]:
            with self.subTest(source=source), self.assertRaises(AudioError):
                self.output.play(source, **args)
        self.assertEqual(self.events, [])

    def test_callback_cannot_reenter_playback(self):
        with self.assertRaisesRegex(AudioError, '重复'):
            self.output.play('https://example.com/a', before_play=lambda: self.output.play('https://example.com/b'))

    def test_two_callers_cannot_overlap_playback_or_cleanup(self):
        first_ready, second_waiting, release = (threading.Event() for _ in range(3))
        failures = []
        original = self.Prepared.wait_ready
        def wait(prepared, checkpoint):
            original(prepared, checkpoint)
            if not first_ready.is_set():
                first_ready.set()
                if not release.wait(2): raise TimeoutError('test blocked')
        def run(**kwargs):
            try: self.output.play('https://example.com/audio', **kwargs)
            except BaseException as error: failures.append(error)
        with patch.object(self.Prepared, 'wait_ready', wait):
            first = threading.Thread(target=run)
            second = threading.Thread(target=lambda: run(checkpoint=second_waiting.set))
            first.start()
            self.assertTrue(first_ready.wait(1))
            second.start()
            try:
                self.assertTrue(second_waiting.wait(1))
                self.assertEqual(self.events.count('prepare'), 1)
            finally:
                release.set()
                first.join(2); second.join(2)
        self.assertEqual(failures, [])
        self.assertFalse(first.is_alive() or second.is_alive())
        second_prepare = self.events.index('prepare', 1)
        self.assertLess(self.events.index('file_cleanup'), second_prepare)
        self.assertNotIn('node_close', self.events)
        self.output.close()
        self.assertEqual(self.events.count('node_close'), 1)

    def test_queued_callbacks_keep_playback_timing_with_nonblocking_host_checkpoint(self):
        # 真机复现：32 个旧 callback + 每次额外等待 50ms，把正常 4.72 秒播放测成提前结束。
        for host_wait in (.05, 0):
            now, players = [0.0], []
            class BufferedRobot:
                def __init__(self, runtime):
                    self.pending = [(0, lambda: None) for _ in range(32)]
                def spin(self, seconds=.05, **kwargs):
                    if self.pending:
                        ready_at, callback = self.pending[0]
                        now[0] += min(seconds, max(0, ready_at - now[0]))
                        if ready_at <= now[0]:
                            self.pending.pop(0)
                            callback()
                def close(self):
                    pass
            class Prepared(self.Prepared):
                def __init__(self, *args):
                    super().__init__(*args)
                    self.seconds = 4.72
                    self.limited = False
            def media(robot):
                player = NativeMediaTests().player()
                player.robot, player.submitted = robot, False
                player.play_type = SimpleNamespace(Request=lambda: SimpleNamespace(
                    media_file_req=SimpleNamespace(priority_level=SimpleNamespace(value=0))))
                player.play_client = object()
                def call(client, request):
                    started_at = now[0]
                    for offset, status in ((0, 2), (4.72, 3)):
                        message = SimpleNamespace(trace_id=player.trace_id, tts_status=SimpleNamespace(value=status))
                        robot.pending.append((started_at + offset, lambda message=message: player.on_status(message)))
                    return SimpleNamespace(tts_resp=SimpleNamespace(is_success=True))
                player.call = call
                player.close = player.stop
                players.append(player)
                return player
            def host_checkpoint():
                now[0] += host_wait
            with self.subTest(host_wait=host_wait), \
                    patch('primebuddy.voice.audio.RobotAudio', BufferedRobot), \
                    patch('primebuddy.voice.audio.PreparedAudio', Prepared), \
                    patch('primebuddy.voice.audio.NativeMedia', media), \
                    patch('primebuddy.integrations.primebot.native_media.time.monotonic', side_effect=lambda: now[0]):
                output = AudioOutput(Path('/unused'), lambda: None)
                try:
                    if host_wait:
                        with self.assertRaisesRegex(AudioError, '提前停止'):
                            output.play('https://example.com/audio', checkpoint=host_checkpoint)
                    else:
                        result = output.play('https://example.com/audio', checkpoint=host_checkpoint)
                        self.assertEqual(result, {'seconds': 4.72, 'limited': False})
                    self.assertFalse(players[0].submitted)
                finally:
                    output.close()


class NativeMediaTests(unittest.TestCase):
    def player(self):
        player = NativeMedia.__new__(NativeMedia)
        player.trace_id = 'owned'
        player.events = []
        player.submitted = True
        player.duration = 35
        player.requested_at = 1
        player.playing_status, player.end_status = 2, 3
        player.confirmed_terminal, player.failure_statuses = {3, 4}, {5, 7}
        player.stop_failed = False
        player.subscription = SimpleNamespace(get_publisher_count=lambda: 1)
        return player

    def test_target_prefixed_enums_distinguish_stopped_from_error(self):
        import sys
        class StatusType:
            TTS_STATUS_TYPE_PLAYING = 2
            TTS_STATUE_TYPE_END = 3
            TTS_STATUS_TYPE_STOP = 4
            TTS_STATUS_TYPE_ERROR = 5
            TTS_STATUS_TYPE_NOT_INQUE = 7
        class Status:
            def __init__(self): self.tts_status = StatusType()
        with patch.dict(sys.modules, {'aimdk_msgs.msg': SimpleNamespace(TtsStatus=Status),
                'aimdk_msgs.srv': SimpleNamespace(PlayMediaFile=object, StopTtsTraceId=object)}):
            p = NativeMedia(SimpleNamespace(node=Mock(), qos=object()))
        self.assertEqual((p.playing_status, p.end_status), (2, 3))
        self.assertEqual(p.confirmed_terminal, {3, 4})
        self.assertTrue(p.failure_statuses.isdisjoint(p.confirmed_terminal))

    def test_waits_for_actual_subscription_match_before_play_request(self):
        p = self.player()
        p.submitted = False
        p.robot = Mock()
        p.play_type = SimpleNamespace(Request=lambda: SimpleNamespace(
            media_file_req=SimpleNamespace(priority_level=SimpleNamespace(value=0))))
        p.play_client = object()
        order = []
        p.robot.spin.side_effect = lambda **kwargs: order.append('spin')
        p.call = Mock(side_effect=lambda *args: (order.append('play'),
            SimpleNamespace(tts_resp=SimpleNamespace(is_success=True)))[1])
        with patch('primebuddy.integrations.primebot.native_media.matched_publishers', side_effect=[0, 0, 1]):
            p.start('/tmp/test/audio.wav', 1)
        self.assertEqual(order, ['spin', 'spin', 'play'])

    def test_unmatched_subscription_never_submits_play(self):
        p = self.player()
        p.submitted = False
        p.robot, p.call = Mock(), Mock()
        with patch('primebuddy.integrations.primebot.native_media.matched_publishers', return_value=0),\
                patch('primebuddy.integrations.primebot.native_media.time.monotonic', side_effect=[0, 7]):
            with self.assertRaisesRegex(AudioError, '尚未匹配'):
                p.start('/tmp/test/audio.wav', 1)
        self.assertFalse(p.submitted)
        p.call.assert_not_called()

    def test_humble_match_count_uses_protected_rcl_handle(self):
        class Handle:
            pointer = 123
            entered = False
            def __enter__(self): self.entered = True
            def __exit__(self, *args): self.entered = False
        handle = Handle()
        def query(pointer, count):
            self.assertTrue(handle.entered)
            self.assertEqual(pointer, handle.pointer)
            count._obj.value = 2
            return 0
        with patch('ctypes.CDLL', return_value=SimpleNamespace(rcl_subscription_get_publisher_count=query)):
            self.assertEqual(matched_publishers(SimpleNamespace(handle=handle)), 2)
        self.assertFalse(handle.entered)

    def test_unavailable_match_query_is_not_treated_as_matched(self):
        with patch('ctypes.CDLL', side_effect=OSError()):
            with self.assertRaises(AudioError):
                matched_publishers(SimpleNamespace())

    def event(self, player, status, trace='owned', at=2):
        from unittest.mock import patch
        with patch('primebuddy.integrations.primebot.native_media.time.monotonic', return_value=at):
            player.on_status(SimpleNamespace(trace_id=trace, tts_status=SimpleNamespace(value=status)))

    def test_foreign_end_cannot_finish_own_playback(self):
        p = self.player()
        self.event(p, 2, at=2)
        self.event(p, 3, trace='other', at=37)
        self.assertIsNone(p.poll(now=38))
        self.assertTrue(p.submitted)

    def test_requires_started_and_full_duration(self):
        p = self.player()
        self.event(p, 3, at=37)
        with self.assertRaises(AudioError):
            p.poll(now=37)
        p = self.player()
        self.event(p, 2, at=2)
        self.event(p, 3, at=5)
        with self.assertRaises(AudioError):
            p.poll(now=5)

    def test_long_playback_finishes_on_real_end(self):
        p = self.player()
        self.event(p, 2, at=2)
        self.assertIsNone(p.poll(now=34))
        self.event(p, 3, at=37.2)
        self.assertTrue(p.poll(now=37.2))
        self.assertFalse(p.submitted)

    def test_error_or_stop_does_not_report_success(self):
        for status in (4, 5, 7):
            p = self.player()
            self.event(p, 2)
            self.event(p, status, at=10)
            with self.assertRaises(AudioError):
                p.poll(now=10)
            self.assertEqual(p.submitted, status in (5, 7))

    def test_missing_feedback_times_out_without_resubmit(self):
        p = self.player()
        with self.assertRaises(AudioError):
            p.poll(now=20)
        self.assertTrue(p.submitted)

    def test_stop_targets_only_own_trace_even_when_cancelled(self):
        p = self.player()
        p.stop_type = SimpleNamespace(Request=lambda: SimpleNamespace(trace_id=''))
        p.stop_client = object()
        p.robot = Mock()
        def call(client, request, cancellable):
            self.assertEqual(request.trace_id, 'owned')
            self.assertFalse(cancellable)
            self.event(p, 4, at=5)
        p.call = call
        p.stop()
        self.assertFalse(p.submitted)

    def test_error_during_stop_does_not_confirm_release_or_repeat_request(self):
        for status in (5, 7):
            p = self.player()
            p.stop_type = SimpleNamespace(Request=lambda: SimpleNamespace(trace_id=''))
            p.stop_client, p.robot = object(), Mock()
            p.events = [(2, status)]
            p.call = Mock()
            with patch('primebuddy.integrations.primebot.native_media.time.monotonic', side_effect=[0, 0, 6]):
                with self.assertRaisesRegex(AudioError, '停止未确认'):
                    p.stop()
            self.assertTrue(p.submitted)
            with self.assertRaisesRegex(AudioError, '未重复提交'):
                p.stop()
            p.call.assert_called_once()

    def test_natural_end_while_waiting_to_stop_needs_no_stop_request(self):
        p = self.player()
        p.stop_type = SimpleNamespace(Request=lambda: SimpleNamespace(trace_id=''))
        p.stop_client, p.call = object(), Mock()
        p.wait_feedback = lambda **kwargs: self.event(p, 3, at=37)
        p.stop()
        self.assertFalse(p.submitted)
        p.call.assert_not_called()

    def test_open_listen_before_file_cleanup_after_confirmed_end(self):
        import tempfile
        import wave
        from pathlib import Path
        from contextlib import contextmanager
        p = self.player()
        order = []
        @contextmanager
        def prepared(path):
            with wave.open(str(path)) as converted:
                self.assertEqual(converted.getframerate(), 16000)
            yield Path('/tmp/test/audio.wav')
            order.append('cleanup')
        p.robot = SimpleNamespace(prepared_audio=prepared, spin=lambda: None)
        p.start = lambda *args: order.append('start')
        p.poll = lambda: True
        p.stop = lambda: None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tts.wav'
            with wave.open(str(path), 'wb') as output:
                output.setparams((1, 2, 24000, 0, 'NONE', ''))
                output.writeframes(bytes(48000))
            p.play(path, on_finished=lambda: order.append('listen'))
        self.assertEqual(order, ['start', 'listen', 'cleanup'])


class AudioPrepareTests(unittest.TestCase):
    def prepare(self, expected=60, limit=10, actual=None, chunks=10, cancelled=False):
        module = prepare_audio_process
        pipeline, sink = Mock(), Mock()
        state = [0]
        pipeline.set_state.side_effect = lambda s: state.__setitem__(0, s)
        pipeline.get_state.side_effect = lambda _: (None, state[0], None)
        pipeline.query_duration.return_value = (True, actual if actual is not None else expected)
        pipeline.get_bus.return_value.pop_filtered.return_value = None
        pipeline.get_by_name.side_effect = lambda name: sink if name == 'pcm' else Mock()
        buffer = NS(get_size=lambda: 32000, extract_dup=lambda *args: b'\0' * 32000)
        samples = iter([NS(get_buffer=lambda: buffer) for _ in range(chunks)])
        sink.emit.side_effect = lambda *args: next(samples, None)
        sink.get_property.return_value = True
        gst = NS(parse_launch=lambda _: pipeline, SECOND=1, MSECOND=.001,
                 State=NS(PAUSED=1, PLAYING=2, NULL=0), Format=NS(TIME=1), MessageType=NS(ERROR=1))
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'audio.wav'
            try:
                result = module.decode({'url': 'unused', 'expected_seconds': expected, 'max_seconds': limit},
                                       path, lambda: cancelled, gst)
                with wave.open(str(path)) as audio:
                    self.assertEqual(audio.getnframes(), int(result[0] * 16000))
                return result
            finally:
                self.assertEqual(state[0], 0)

    def test_limit_is_encoded_in_file_even_if_controller_stalls(self):
        self.assertEqual(self.prepare(), (10, True))

    def test_short_song_finishes_before_limit(self):
        self.assertEqual(self.prepare(expected=3, limit=60, chunks=3), (3, False))

    def test_full_song(self):
        self.assertEqual(self.prepare(expected=4, limit=0, chunks=4), (4, False))

    def test_preview_is_rejected_before_decode(self):
        with self.assertRaisesRegex(RuntimeError, 'duration mismatch'):
            self.prepare(expected=120, actual=30)

    def test_truncated_data_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'premature'):
            self.prepare(chunks=3)

    def test_cancellation_releases_decoder(self):
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.prepare(cancelled=True)

    def test_failed_file_cleanup_is_not_confirmed(self):
        from contextlib import redirect_stdout
        from unittest.mock import patch
        import io
        import json
        module = prepare_audio_process
        directory = Mock(name='/tmp/unused')
        directory.name = '/tmp/unused'
        directory.cleanup.side_effect = OSError('cleanup failed')
        output = io.StringIO()
        with patch.dict('sys.modules', {'gi': NS(require_version=lambda *args: None),
                                       'gi.repository': NS(Gst=NS(init=lambda _: None))}),\
                patch.object(module.sys, 'stdin', io.StringIO('{}\n')),\
                patch.object(module.signal, 'signal'),\
                patch.object(module.tempfile, 'TemporaryDirectory', return_value=directory),\
                patch.object(module, 'decode', return_value=(10, True)),\
                patch.object(module.select, 'select', return_value=([1], [], [])),\
                redirect_stdout(output):
            with self.assertRaises(OSError):
                module.main()
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1]),
                         {'event': 'closed', 'confirmed': False})

    def test_generic_audio_does_not_require_music_metadata(self):
        self.assertEqual(self.prepare(expected=None, actual=60, limit=10), (10, True))
