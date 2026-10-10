import unittest
from types import SimpleNamespace
from unittest.mock import patch

from primebuddy.voice import wake_only
from primebuddy.voice_support.native_voice import NativeVoiceInput


class VoiceTests(unittest.TestCase):
    def microphone(self):
        clock = SimpleNamespace(now=lambda: SimpleNamespace(nanoseconds=1))
        node = SimpleNamespace(get_clock=lambda: clock)
        return NativeVoiceInput(None, SimpleNamespace(node=node))

    def test_listening_window_is_thirty_seconds(self):
        mic = self.microphone()
        with patch('primebuddy.voice_support.native_voice.time.monotonic', return_value=100):
            mic.listen(0)
        self.assertEqual(mic.deadline, 130)
        with patch('primebuddy.voice_support.native_voice.time.monotonic', return_value=111):
            mic.check_health()
        self.assertEqual(mic.phase, 'listening')

    def test_wake_transition_emits_one_event(self):
        mic = self.microphone()
        mic.wait_for_wake(0)
        mic.conversation_state = 0
        msg = SimpleNamespace(main_state=SimpleNamespace(value=1))
        mic.on_state(msg)
        mic.on_state(msg)
        self.assertEqual(mic.events.qsize(), 1)
        self.assertEqual(mic.events.get()[1], 'wake')

    def test_wake_filter_keeps_real_commands(self):
        self.assertTrue(wake_only('元仔，元仔。'))
        self.assertTrue(wake_only('源崽源崽'))
        self.assertFalse(wake_only('元仔，关闭餐厅灯。'))
        self.assertFalse(wake_only('人在，人在。'))

    def test_observed_repeated_wake_homophones_skip_planner(self):
        self.assertTrue(wake_only('冤哉冤哉。'))
        self.assertTrue(wake_only('原在，原在。'))
        self.assertFalse(wake_only('原在'))
        self.assertFalse(wake_only('原在原在，打开天际线灯。'))

    def test_no_frontend_audio_is_not_reported_as_user_silence(self):
        mic = self.microphone()
        mic.listen(0)
        mic.timeout()
        _, kind, reason = mic.events.get_nowait()
        self.assertEqual(kind, 'timeout')
        self.assertIn('未收到原厂音频', reason)

    def test_wrong_stream_and_stale_audio_are_distinguished(self):
        mic = self.microphone()
        msg = SimpleNamespace(stream_id=2, audio_vad_state=SimpleNamespace(value=1))
        mic.listen(0)
        mic.on_audio(msg)
        mic.timeout()
        self.assertIn('其他音频流', mic.events.get_nowait()[2])
        mic.listen(0)
        mic.opened_at_ns = 100
        msg.stream_id = 0
        msg.header = SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=1))
        mic.on_audio(msg)
        mic.timeout()
        self.assertIn('跨板时钟', mic.events.get_nowait()[2])

    def test_incomplete_segment_is_not_reported_as_no_audio(self):
        mic = self.microphone()
        mic.listen(0)
        mic.on_audio(SimpleNamespace(stream_id=0, audio_vad_state=SimpleNamespace(value=3),
            header=SimpleNamespace(stamp=SimpleNamespace(sec=0, nanosec=2)), audio_data=b'\0'*6400))
        mic.timeout()
        self.assertIn('未收到完整 VAD', mic.events.get_nowait()[2])

    def test_new_window_resets_packet_diagnostics(self):
        mic = self.microphone()
        mic.window_packets, mic.window_stream_packets, mic.window_stale_packets = 10, 9, 8
        mic.listen(0)
        self.assertEqual((mic.window_packets, mic.window_stream_packets, mic.window_stale_packets), (0, 0, 0))
