#!/usr/bin/env python3
"""T1 SDK 音频播放、反馈与跨板临时文件管理；不负责运动或原厂 Agent 启停。"""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
import signal
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave

from ..settings import SetupError

CANCEL = threading.Event()
SOC0 = 'run@10.1.1.100'


class AudioError(SetupError):
    pass


def check_cancel():
    if CANCEL.is_set():
        raise AudioError('连接关闭或收到退出信号。')


def successful(response):
    return response.header.code == 0 and response.status.value == 1


def ssh_options(runtime):
    return ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=5',
            '-o', 'ServerAliveCountMax=2', '-o', f'ControlPath={runtime}/soc0.sock',
            '-o', f'UserKnownHostsFile={runtime}/known_hosts']


def require_jetson():
    identity = Path('/robot/data/info/soc_index')
    if not identity.is_file() or identity.read_text().strip() != '1':
        raise AudioError('机器人音频任务仅在 Jetson（SoC 1）运行。')


def connect_soc0(runtime):
    require_jetson()
    # 串行建连，避免两个启动进程同时清理或创建控制 socket。
    with (runtime / 'soc0-connect.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AudioError('另一个任务正在建立跨板 SSH 连接，请稍后重试。') from None
        if soc0_connected(runtime):
            return
        path = runtime / 'soc0.sock'
        try:
            previous = path.lstat()
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.getuid():
                raise AudioError('跨板 SSH 控制路径被其他文件占用，未删除。')
            # check 失败不等于失效；仅删除确定拒绝连接且属于当前用户的 socket。
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    current = path.lstat()
                    if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
                        raise AudioError('跨板 SSH 连接状态已变化，请重试。')
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    raise AudioError('无法确认跨板 SSH 控制连接状态，未删除。') from None
                else:
                    raise AudioError('跨板 SSH 控制连接仍被占用，但复用检查失败，未删除。')
        try:
            subprocess.run(['ssh', '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=5',
                            '-o', 'ServerAliveCountMax=2', '-o', f'ControlPath={path}',
                            '-o', f'UserKnownHostsFile={runtime}/known_hosts',
                            '-o', 'ControlMaster=yes', '-o', 'ControlPersist=1800', '-Nf', SOC0],
                           check=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            raise AudioError('跨板 SSH 登录失败或超时，请检查连接与密码后重试。') from None
        # SSH 返回 0 也可能已禁用复用，必须确认实际控制连接可用。
        if not soc0_connected(runtime):
            raise AudioError('跨板 SSH 复用连接未建立，请检查 SSH 提示后重试。')


def soc0_connected(runtime):
    try:
        result = subprocess.run(['ssh', *ssh_options(runtime), '-O', 'check', SOC0],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


@contextlib.contextmanager
def audio_session(runtime, factory=None):
    CANCEL.clear()
    old_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    for sig in old_signals:
        signal.signal(sig, lambda *_: CANCEL.set())
    audio = None
    try:
        with (runtime / 'audio.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AudioError('已有点歌台任务运行。')
            audio = (factory or RobotAudio)(runtime)
            yield audio
    finally:
        if audio is not None:
            audio.close()
        for sig, handler in old_signals.items():
            signal.signal(sig, handler)


def run_child(args, timeout, payload=None, checkpoint=None):
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.DEVNULL)
        try:
            if payload is not None:
                proc.stdin.write(payload)
                proc.stdin.close()
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                check_cancel()
                if checkpoint is not None:
                    checkpoint()
                if time.monotonic() > deadline:
                    raise AudioError('音频子任务超时。')
                time.sleep(.05)
            if proc.returncode:
                raise AudioError('音频合成、解码或识别子任务失败。')
            output.seek(0)
            return output.read(65536)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


class RobotAudio:
    def __init__(self, runtime):
        require_jetson()
        self.runtime = runtime
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.signals import SignalHandlerOptions
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        from aimdk_msgs.msg import PlayStateChange
        from aimdk_msgs.srv import PlayAudioFile, StopAudioPlay
        self.ros = rclpy
        self.context = Context()
        rclpy.init(context=self.context, signal_handler_options=SignalHandlerOptions.NO)
        self.node = rclpy.create_node('qqmusic_audio_' + uuid.uuid4().hex[:8], context=self.context)
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.qos = QoSProfile(depth=50, reliability=ReliabilityPolicy.BEST_EFFORT,
                              durability=DurabilityPolicy.VOLATILE)
        self.playback_events = []
        self.playback_owner = ''
        self.playback_submitted = False
        self.node.create_subscription(PlayStateChange, '/aima/hal/audio/play_state', self.on_state, self.qos)
        self.play_client = self.node.create_client(PlayAudioFile, '/aimdk_5Fmsgs/srv/PlayAudioFile')
        self.stop_client = self.node.create_client(StopAudioPlay, '/aimdk_5Fmsgs/srv/StopAudioPlay')

    def on_state(self, msg):
        self.playback_events.append((time.monotonic(), msg.pkg_name, int(msg.state.value)))
        self.playback_events = self.playback_events[-100:]

    def spin(self, seconds=.05, cancellable=True):
        if cancellable:
            check_cancel()
        self.executor.spin_once(timeout_sec=seconds)

    def call(self, client, request, timeout=5, cancellable=True):
        deadline = time.monotonic() + timeout
        while not client.service_is_ready():
            self.spin(cancellable=cancellable)
            if time.monotonic() > deadline:
                raise AudioError('音频服务不可用。')
        future = client.call_async(request)
        while not future.done():
            self.spin(cancellable=cancellable)
            if time.monotonic() > deadline:
                raise AudioError('音频请求超时，执行结果未知；未重发。')
        result = future.result()
        header = getattr(result, 'response', getattr(result, 'header', None))
        accepted = header is not None and successful(header)
        if not accepted:
            raise AudioError('音频服务返回失败状态。')
        return result

    def check(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            self.spin()
        return {'soc_index': 1, 'play_service': self.play_client.service_is_ready(),
                'stop_service': self.stop_client.service_is_ready(),
                'soc0_ssh_connected': soc0_connected(self.runtime)}

    def stop_owned(self):
        from aimdk_msgs.srv import StopAudioPlay
        # 停止接口没有任务字段；检测到其他音频接管时，不发送全局停止。
        other = [e for e in self.playback_events if e[1] != self.playback_owner and e[2] == 1 and e[0] >= self.playback_requested_at]
        if other:
            raise AudioError('其他音频已接管；未调用全局停止，需确认现场声音状态。')
        request = StopAudioPlay.Request()
        request.request.header.stamp = self.node.get_clock().now().to_msg()
        self.call(self.stop_client, request, cancellable=False)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.spin(cancellable=False)
            if any(e[1] == self.playback_owner and e[2] in (0, 2) for e in self.playback_events):
                self.playback_submitted = False
                return
        raise AudioError('已请求音频停止，但未收到本任务的停止反馈。')

    def play(self, path, on_finished=None):
        with self.prepared_audio(path) as remote:
            result = self.play_file(path, remote)
            # 播放已确认结束即开放接话，不等 SSH 临时文件清理完成。
            if on_finished is not None:
                on_finished()
            return result

    @contextlib.contextmanager
    def prepared_audio(self, path, checkpoint=None):
        # 音频服务位于 SoC 0，先将 Jetson 文件传到本任务独占的临时目录。
        raw = run_child(['ssh', *ssh_options(self.runtime), SOC0,
                         'mktemp -d /tmp/qiyuan-qqmusic.XXXXXXXX'], 12, checkpoint=checkpoint)
        directory = raw.decode().strip()
        import re
        if not re.fullmatch(r'/tmp/qiyuan-qqmusic\.[a-zA-Z0-9]{8}', directory):
            raise AudioError('未取得可验证的临时音频目录。')
        remote = directory + '/audio.wav'
        try:
            run_child(['scp', *ssh_options(self.runtime), str(path), f'{SOC0}:{remote}'], 45, checkpoint=checkpoint)
            yield Path(remote)
        finally:
            # 只清理本次创建的媒体目录，不删除原厂文件或日志。
            try:
                subprocess.run(['ssh', *ssh_options(self.runtime), SOC0,
                                'rm -f ' + shlex.quote(remote) + ' && rmdir ' + shlex.quote(directory)],
                               check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                print('临时音频清理未确认，请检查本次 /tmp/qiyuan-qqmusic.* 目录。', file=sys.stderr)

    def start_file(self, path, remote_path, checkpoint=None):
        from aimdk_msgs.srv import PlayAudioFile
        with wave.open(str(path)) as audio:
            channels, rate, width, frames = audio.getnchannels(), audio.getframerate(), audio.getsampwidth(), audio.getnframes()
        duration = frames / rate
        if channels != 1 or width != 2 or rate != 24000 or not 0 < duration <= 605:
            raise AudioError('只接受 24kHz 单声道 PCM16 WAV，最长 605 秒。')
        # 已观察到其他播放任务时拒绝开始，避免抢占扬声器。
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if checkpoint is not None:
                checkpoint()
            self.spin()
        latest = {}
        for _, playback_owner, state in self.playback_events:
            latest[playback_owner] = state
        if any(state == 1 for state in latest.values()):
            raise AudioError('检测到其他音频正在播放，本次未提交。')
        if checkpoint is not None:
            checkpoint()
        self.playback_owner = 'qqmusic_' + uuid.uuid4().hex
        self.playback_events.clear()
        req = PlayAudioFile.Request()
        req.request.header.stamp = self.node.get_clock().now().to_msg()
        req.file.pkg_name, req.file.file_name, req.file.file_path = self.playback_owner, remote_path.name, str(remote_path.parent)
        req.file.info.channels, req.file.info.sample_rate, req.file.info.size = channels, rate, frames * width
        req.file.info.sample_format, req.file.info.coding_format = 'S16_LE', 'wave'
        req.file.priority, req.file.priority_weight = 6, 0
        self.playback_requested_at = time.monotonic()
        self.playback_submitted = True  # 超时请求也可能已生效，退出时仍须检查并停止。
        try:
            self.call(self.play_client, req)
            self.duration_seconds = duration
        except BaseException:
            if self.playback_submitted:
                self.stop_owned()
            raise

    @property
    def playback_started(self):
        own = [e for e in self.playback_events if e[1] == self.playback_owner]
        return bool(own and own[-1][2] == 1)

    def poll_playback(self, max_seconds=None):
        own = [e for e in self.playback_events if e[1] == self.playback_owner]
        began = next((e[0] for e in own if e[2] == 1), None)
        ended = next((e[0] for e in own if began is not None and e[0] >= began and e[2] in (0, 2)), None)
        if ended is not None:
            self.playback_submitted = False
            if ended - began < self.duration_seconds - max(1, self.duration_seconds * .03):
                raise AudioError('音频提前结束，未确认完整播放。')
            return {'seconds': round(self.duration_seconds, 2), 'playback_feedback': 'started_and_stopped'}
        if began is not None and max_seconds is not None and time.monotonic() - began >= max_seconds:
            elapsed = time.monotonic() - began
            self.stop_owned()
            return {'seconds': round(elapsed, 2), 'playback_feedback': 'stopped_at_limit'}
        if began is None and time.monotonic() - self.playback_requested_at > 8:
            raise AudioError('未收到本任务开始播放的状态，不能确认已出声。')
        if time.monotonic() - self.playback_requested_at >= self.duration_seconds + 12:
            raise AudioError('未收到本任务播放结束的状态。')
        return None

    def play_file(self, path, remote_path):
        try:
            self.start_file(path, remote_path)
            while True:
                self.spin()
                result = self.poll_playback()
                if result is not None:
                    return result
        finally:
            if self.playback_submitted:
                self.stop_owned()

    def close(self):
        self.executor.remove_node(self.node)
        self.executor.shutdown()
        self.node.destroy_node()
        self.context.shutdown()
