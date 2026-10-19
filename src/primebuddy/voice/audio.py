"""框架自身的语音播报：串行播放、转码、原厂会话生命周期与任务清理。"""
from contextlib import ExitStack
import math
from pathlib import Path
import threading
from urllib.parse import urlsplit
import wave

from primebuddy.integrations.primebot.robot_audio import AudioError, RobotAudio, check_cancel
from primebuddy.integrations.primebot.native_media import NativeMedia
from primebuddy.integrations.primebot.prepared_audio import PreparedAudio


class AudioOutput:
    def __init__(self, runtime, checkpoint):
        self.runtime, self.checkpoint = runtime, checkpoint
        self.lock = threading.Lock()
        self.owner = None
        self.robot = self.media = None
        self.closed = False

    def play(self, source, *, max_seconds=0, expected_seconds=None, before_play=None,
             on_started=None, on_tick=None, on_finished=None, checkpoint=None):
        """阻塞至播放及清理完成；回调在调用线程执行，不能嵌套播放。"""
        if type(max_seconds) is not int or not 0 <= max_seconds <= 600:
            raise AudioError('播放时长须为 0～600 的整数，0 表示完整音频。')
        if expected_seconds is not None and (not isinstance(expected_seconds, (int, float))
                or isinstance(expected_seconds, bool) or not math.isfinite(expected_seconds)
                or not 0 < expected_seconds <= 600):
            raise AudioError('预期音频时长须在 0～600 秒之间。')
        local = isinstance(source, Path) or isinstance(source, str) and '://' not in source
        if local:
            source = Path(source).expanduser().resolve()
            if not source.is_file() or not 0 < source.stat().st_size <= 64 * 1024 * 1024:
                raise AudioError('音频文件不存在或超过 64 MiB。')
        else:
            parts = urlsplit(source) if isinstance(source, str) else None
            if parts is None or parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password:
                raise AudioError('音源须为本地文件或 HTTP(S) 地址，不能包含登录凭据。')
        if self.owner == threading.get_ident():
            raise AudioError('音频回调不能重复进入播放接口。')
        def check():
            check_cancel()
            self.checkpoint()
            if checkpoint is not None:
                checkpoint()
        while not self.lock.acquire(timeout=.05):
            check()
        self.owner = threading.get_ident()
        spin = None
        try:
            check()
            if self.closed:
                raise AudioError('音频会话已经关闭。')
            if self.robot is None:
                self.robot = RobotAudio(self.runtime)
            robot = self.robot
            spin = robot.spin
            with ExitStack() as stack:
                # 独立于语音订阅的常驻执行器，所有线程在音频锁内串行使用。
                # 不在每条短提示前重建 DDS 订阅，避免开始反馈尚未匹配就已发出。
                def checked_spin(seconds=.05, cancellable=True):
                    if cancellable:
                        check()
                    spin(seconds, cancellable=cancellable)
                robot.spin = checked_spin
                # 已缓存的标准提示语复用快速路径，避免再次远端解码。
                fast_prompt = False
                if local and max_seconds == 0 and expected_seconds is None and not any((before_play, on_started, on_tick)):
                    try:
                        with wave.open(str(source)) as audio:
                            fast_prompt = (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 24000)
                            seconds = audio.getnframes() / 24000
                    except (wave.Error, EOFError):
                        pass
                if fast_prompt:
                    if not 0 < seconds <= 600:
                        raise AudioError('音频时长须在 0～600 秒之间。')
                    media = self._player()
                    stack.callback(media.stop)
                    media.play(source, on_finished=on_finished)
                    return {'seconds': seconds, 'limited': False}
                if local:
                    remote = stack.enter_context(robot.prepared_audio(source, checkpoint=check))
                    source = remote.as_uri()
                prepared = PreparedAudio(self.runtime, source, max_seconds, expected_seconds)
                stack.callback(prepared.close)
                prepared.wait_ready(check)
                if before_play:
                    before_play()
                check()
                media = self._player()
                stack.callback(media.stop)  # 先停自有音频，再删文件；节点保留至会话结束。
                media.start(prepared.path, prepared.seconds)
                notified = False
                while True:
                    robot.spin()
                    if media.poll():
                        if on_finished:
                            on_finished()
                        return {'seconds': prepared.seconds, 'limited': prepared.limited}
                    if media.started is not None:
                        if not notified:
                            notified = True
                            if on_started:
                                on_started()
                        if on_tick:
                            on_tick()
        finally:
            if self.robot is not None and spin is not None:
                self.robot.spin = spin
            self.owner = None
            self.lock.release()

    def _player(self):
        if self.media is None:
            self.media = NativeMedia(self.robot)
        return self.media

    def close(self):
        # serve 在任务线程退出之后关闭；清理不能被全局取消标志跳过。
        with self.lock:
            self.closed = True
            try:
                if self.media is not None:
                    self.media.close()
            finally:
                self.media = None
                if self.robot is not None:
                    self.robot.close()
                    self.robot = None
