"""播放板音频准备进程；只传递媒体元数据，不输出签名地址。"""
import json
from pathlib import Path
import queue
import re
import shlex
import subprocess
import threading
import time

from primebuddy.integrations.primebot.robot_audio import AudioError


from .device import host_command


class PreparedAudio:
    def __init__(self, runtime, source, max_seconds, expected_seconds):
        self.events = queue.Queue()
        self.path = None
        self.closed = self.error = False
        script = Path(__file__).with_name('prepare_audio_process.py').read_text()
        self.proc = subprocess.Popen(host_command(runtime, 'python3 -u -c ' + shlex.quote(script)),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        def read():
            for line in self.proc.stdout:
                try:
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError('invalid event')
                    self.events.put(item)
                except ValueError:
                    self.events.put({'event': 'error'})
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            self.proc.stdin.write(json.dumps({'url': source, 'max_seconds': max_seconds,
                                             'expected_seconds': expected_seconds}) + '\n')
            self.proc.stdin.flush()
        except BaseException:
            self.close()
            raise

    def update(self):
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                break
            if item.get('event') == 'ready':
                if not re.fullmatch(r'/tmp/primebuddy-audio-[a-zA-Z0-9_]+/audio.wav', item.get('path', '')):
                    raise AudioError('音频临时路径无效。')
                self.path, self.seconds, self.limited = item['path'], item['seconds'], item['limited']
            elif item.get('event') == 'closed':
                self.closed = item.get('confirmed') is True
            elif item.get('event') == 'error':
                self.error = True

    def wait_ready(self, checkpoint):
        deadline = time.monotonic() + 45
        while True:
            checkpoint()
            self.update()
            if self.error or self.proc.poll() is not None or time.monotonic() > deadline:
                raise AudioError('音频准备或时长校验失败。')
            if self.path:
                return
            time.sleep(.05)

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write('stop\n')
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        self.reader.join(timeout=1)
        try:
            self.update()
        finally:
            self.proc.stdin.close()
            self.proc.stdout.close()
        if not self.closed:
            raise AudioError('音频临时文件清理未确认。')
