"""原厂媒体生命周期：真实播放期间暂停双工超时，仅停止自己的 trace。"""
import logging
import tempfile
import time
import uuid
import wave
from pathlib import Path

from primebuddy.integrations.primebot.robot_audio import AudioError

logger = logging.getLogger(__name__)


def matched_publishers(subscription):
    method = getattr(subscription, 'get_publisher_count', None)
    if callable(method):
        return method()
    # Humble handle.pointer 指向 rcl_subscription_t，须在句柄保护期间查询。
    import ctypes
    try:
        library = ctypes.CDLL('librcl.so')
        count_publishers = library.rcl_subscription_get_publisher_count
        count_publishers.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
        count_publishers.restype = ctypes.c_int32
        count = ctypes.c_size_t()
        with subscription.handle:
            code = count_publishers(subscription.handle.pointer, ctypes.byref(count))
        if code != 0:
            raise AudioError('无法确认原厂媒体状态订阅匹配。')
        return count.value
    except (AttributeError, OSError):
        raise AudioError('当前 ROS 环境不能确认媒体状态订阅匹配，未提交播放。') from None


def status_value(enum, names, default=None):
    return next((value for name in names if type(value := getattr(enum, name, None)) is int), default)


class NativeMedia:
    def __init__(self, robot):
        from aimdk_msgs.msg import TtsStatus
        from aimdk_msgs.srv import PlayMediaFile, StopTtsTraceId
        self.robot = robot
        self.play_type, self.stop_type = PlayMediaFile, StopTtsTraceId
        self.play_client = robot.node.create_client(PlayMediaFile, '/aimdk_5Fmsgs/srv/PlayMediaFile')
        self.stop_client = robot.node.create_client(StopTtsTraceId, '/aimdk_5Fmsgs/srv/StopTtsTraceId')
        self.subscription = robot.node.create_subscription(TtsStatus, '/interaction/tts_status', self.on_status, robot.qos)
        self.trace_id = ''
        self.events = []
        self.submitted = False
        self.stop_failed = False
        enum = type(TtsStatus().tts_status)
        self.playing_status = status_value(enum, ('TTS_STATUS_TYPE_PLAYING', 'PLAYING'), 2)
        self.end_status = status_value(enum, ('TTS_STATUE_TYPE_END', 'TTS_STATUS_TYPE_END', 'END'), 3)
        self.confirmed_terminal = {self.end_status}
        stop = status_value(enum, ('TTS_STATUS_TYPE_STOP', 'STOP', 'STOPPED'))
        if stop is not None:
            self.confirmed_terminal.add(stop)
        self.failure_statuses = {status_value(enum, ('TTS_STATUS_TYPE_ERROR', 'ERROR'), 5),
                                 status_value(enum, ('TTS_STATUS_TYPE_NOT_INQUE', 'NOT_INQUE'), 7)}

    def wait_feedback(self, cancellable=True):
        deadline = time.monotonic() + 6
        while matched_publishers(self.subscription) < 1:
            self.robot.spin(cancellable=cancellable)
            if time.monotonic() >= deadline:
                raise AudioError('原厂媒体状态订阅尚未匹配，未提交请求。')

    def on_status(self, message):
        if message.trace_id == self.trace_id:
            self.events.append((time.monotonic(), int(message.tts_status.value)))
            self.events = self.events[-32:]

    def call(self, client, request, cancellable=True):
        deadline = time.monotonic() + 12
        while not client.service_is_ready():
            self.robot.spin(cancellable=cancellable)
            if time.monotonic() >= deadline:
                raise AudioError('原厂媒体服务未就绪。')
        request.header.header.stamp = self.robot.node.get_clock().now().to_msg()
        future = client.call_async(request)
        while not future.done():
            self.robot.spin(cancellable=cancellable)
            if time.monotonic() >= deadline:
                raise AudioError('原厂媒体请求超时，未重复提交。')
        result = future.result()
        if result is None or result.header.header.code != 0:
            raise AudioError('原厂媒体请求失败。')
        return result

    def start(self, remote_path, duration):
        if self.submitted:
            raise AudioError('本任务仍有未结束的音频。')
        if not 0 < duration <= 605 or not str(remote_path).startswith('/tmp/'):
            raise AudioError('原厂媒体路径或时长无效。')
        self.wait_feedback()
        self.trace_id = 'primebuddy_' + uuid.uuid4().hex
        self.stop_failed = False
        self.events.clear()
        self.duration, self.requested_at = duration, time.monotonic()
        request = self.play_type.Request()
        media = request.media_file_req
        media.file_name, media.sample_rate = str(remote_path), 16000
        media.priority_level.value = 1
        media.domain, media.trace_id = 'primebuddy', self.trace_id
        media.is_interrupted = False
        self.submitted = True  # 即使 RPC 超时，也只能按本 trace 清理。
        result = self.call(self.play_client, request)
        if not result.tts_resp.is_success:
            self.submitted = False
            raise AudioError('原厂播放器未接受本次音频。')

    @property
    def started(self):
        return next((at for at, state in self.events if state == self.playing_status), None)

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        terminal = next(((at, state) for at, state in self.events
                         if state in self.confirmed_terminal | self.failure_statuses), None)
        if terminal:
            at, state = terminal
            if state in self.confirmed_terminal:
                self.submitted = False
            if state != self.end_status or self.started is None or at - self.started < self.duration - max(1, self.duration * .03):
                logger.error('MEDIA_FAILURE %s', {'states': [s for _, s in self.events],
                      'expected': round(self.duration, 3),
                      'elapsed': None if self.started is None else round(at - self.started, 3)})
                raise AudioError('原厂音频提前停止或播放失败。')
            return True
        if self.started is None and now - self.requested_at > 12:
            raise AudioError('原厂音频开始未确认。')
        if now - self.requested_at > self.duration + 20:
            raise AudioError('原厂音频结束未确认。')
        return None

    def stop(self):
        if not self.submitted:
            return
        if any(state in self.confirmed_terminal for _, state in self.events):
            self.submitted = False
            return
        if self.stop_failed:
            raise AudioError('本次原厂音频停止未确认，未重复提交。')
        request = self.stop_type.Request()
        request.trace_id = self.trace_id
        try:
            self.wait_feedback(cancellable=False)
            if any(state in self.confirmed_terminal for _, state in self.events):
                self.submitted = False
                return
            self.call(self.stop_client, request, cancellable=False)
            deadline = time.monotonic() + 5
            while not any(state in self.confirmed_terminal for _, state in self.events):
                self.robot.spin(cancellable=False)
                if time.monotonic() >= deadline:
                    raise AudioError('本次原厂音频停止未确认。')
        except BaseException:
            self.stop_failed = True
            raise
        self.submitted = False

    def play(self, path, on_finished=None):
        # 千问缓存保持原始 24k；只为原厂 16k 媒体接口生成一次性转换文件。
        import audioop
        with wave.open(str(path), 'rb') as source:
            if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != 24000:
                raise AudioError('提示音必须为 24k 单声道 PCM16。')
            data, _ = audioop.ratecv(source.readframes(source.getnframes()), 2, 1, 24000, 16000, None)
        with tempfile.TemporaryDirectory(prefix='primebuddy-media-') as directory:
            converted = Path(directory) / 'audio.wav'
            with wave.open(str(converted), 'wb') as target:
                target.setparams((1, 2, 16000, 0, 'NONE', ''))
                target.writeframes(data)
            with self.robot.prepared_audio(converted) as remote:
                try:
                    self.start(remote, len(data) / 32000)
                    while True:
                        self.robot.spin()
                        if self.poll():
                            if on_finished is not None:
                                on_finished()
                            return
                finally:
                    self.stop()

    def close(self):
        try:
            self.stop()
        finally:
            self.robot.node.destroy_subscription(self.subscription)
            self.robot.node.destroy_client(self.play_client)
            self.robot.node.destroy_client(self.stop_client)
