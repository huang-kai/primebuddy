"""Mac 扬声器 -> T1 麦克风回归；结束时退出平台并检查原厂模式恢复。"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import threading
import time
import uuid

AUDIO = {'wake': '元仔元仔.m4a', 'command': '播放周杰伦的晴天.m4a',
         'single': '元仔，播放周杰伦的晴天.m4a', 'double': '元仔元仔，播放周杰伦的晴天.m4a'}


def require_clean_turn(lines):
    if any(line.startswith(('TURN_ERROR', 'AUDIO_ERROR', 'DIANGETAI_ERROR')) for line in lines):
        raise RuntimeError('接话轮次发生错误，不能把恢复后的监听窗口算作通过')


class Session:
    def __init__(self, args):
        self.args = args
        self.ssh = ['ssh', '-S', args.ssh_socket, '-o', 'BatchMode=yes', args.host]
        self.remote = args.remote_dir
        self.events = []
        self.changed = threading.Condition()
        self.output = (args.output / 'events.jsonl').open('x')
        os.chmod(self.output.name, 0o600)
        command = ('source /robot/etc/basic_env >/dev/null 2>&1 && '
                   'source /robot/software/housekeeper/bin/setup.bash >/dev/null 2>&1 && '
                   'mkdir -p ' + shlex.quote(self.remote) + ' && echo $$ > ' + shlex.quote(self.remote + '/session.pid') + ' && '
                   'exec /home/run/.primebuddy/bin/primebuddy start')
        self.process = subprocess.Popen(self.ssh + ['bash -c ' + shlex.quote(command)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def record(self, kind, **fields):
        event = dict(at=time.time(), kind=kind, **fields)
        with self.changed:
            self.events.append(event)
            self.output.write(json.dumps(event, ensure_ascii=False) + '\n')
            self.output.flush()
            self.changed.notify_all()
        return len(self.events) - 1

    def read(self):
        for line in self.process.stdout:
            line = line.rstrip()
            self.record('robot', line=line)
            if line.startswith(('ASR ', 'TTS ', 'VOICE_WAKE_TEXT', 'VOICE_WAITING',
                                'AGENT_', 'RESTORE_', 'DIANGETAI_ERROR', 'TURN_ERROR')):
                print(line, flush=True)
        self.record('closed')

    def wait(self, predicate, since, seconds):
        deadline = time.monotonic() + seconds
        with self.changed:
            cursor = since
            while True:
                while cursor < len(self.events):
                    event = self.events[cursor]
                    cursor += 1
                    if event['kind'] == 'robot' and predicate(event['line']):
                        return cursor - 1
                    if event['kind'] == 'closed':
                        raise RuntimeError('PrimeBuddy exited; inspect session log')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Expected robot event not received')
                self.changed.wait(min(remaining, 1))

    def play(self, name):
        self.record('computer_audio_start', audio=name, gain=self.args.gain)
        subprocess.run(['/usr/bin/afplay', '-v', str(self.args.gain), str(self.args.recordings / AUDIO[name])],
                       check=True, timeout=20)
        self.record('computer_audio_end', audio=name)

    def window_after_music(self, since, seconds):
        done = self.wait(lambda line: line == f'TTS 已播放 {seconds} 秒，歌曲已停止。'
                         or line.startswith(('DIANGETAI_ERROR', 'TURN_ERROR')),
                         since, seconds + 100)
        if self.events[done]['line'].startswith(('DIANGETAI_ERROR', 'TURN_ERROR')):
            raise RuntimeError('Music failed; inspect log before further audio tests')
        question = self.wait(lambda line: line == 'TTS 还想做什么？', done, 35)
        window = self.wait(lambda line: '"event": "listen_window_open"' in line, question, 35)
        require_clean_turn([event.get('line', '') for event in self.events[since:window + 1]])
        return window

    def seed(self, seconds):
        begin = len(self.events)
        self.record('seed_begin')
        self.play('wake')
        # 等待原厂唤醒回应结束，状态从 SPEAKING 回到 LISTENING。
        self.wait(lambda line: '"event": "native_state"' in line
                  and '"previous": 3' in line and '"state": 1' in line, begin, 12)
        time.sleep(.6)
        self.play('command')
        return self.window_after_music(begin, seconds)

    def stop(self):
        code = ('import os,signal;from pathlib import Path;'
                f'p=int(Path({self.remote + "/session.pid"!r}).read_text());'
                'a=Path(f"/proc/{p}/cmdline").read_bytes().split(bytes([0]));'
                'assert b"/home/run/primebuddy/main.py" in a and b"start" in a;os.kill(p,signal.SIGINT)')
        try:
            if self.process.poll() is None:
                subprocess.run(self.ssh + ['python3 -c ' + shlex.quote(code)], check=True, timeout=15)
            self.process.wait(timeout=100)
            self.reader.join(timeout=2)
            restored = any(e.get('line') == 'RESTORE_CONFIRMED' for e in self.events)
            self.record('session_end', restore_confirmed=restored, returncode=self.process.returncode)
            print('SESSION_END', 'restore_confirmed=' + str(restored), flush=True)
            if not restored:
                raise RuntimeError('未收到 RESTORE_CONFIRMED，请查看事件日志并核实原厂模式。')
        finally:
            self.output.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, choices=(10, 60), required=True)
    parser.add_argument('--ssh-socket', required=True, help='已登录 SoC 1 的 SSH ControlPath')
    parser.add_argument('--host', default='run@10.1.1.101')
    parser.add_argument('--recordings', type=Path, required=True, help='含四条中文命名 M4A 的目录')
    parser.add_argument('--output', type=Path, required=True, help='新建的私有结果目录')
    parser.add_argument('--gain', type=float, default=1, help='afplay 播放增益；先检查峰值避免削波')
    parser.add_argument('--cases', nargs='+', choices=['no_wake', 'single', 'double', 'split'], default=['no_wake', 'single', 'double', 'split'])
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    if not 0 < args.gain <= 3 or args.repeat < 1:
        parser.error('gain 须为 (0, 3]，repeat 须为正整数')
    for name in AUDIO.values():
        if not (args.recordings / name).is_file():
            parser.error('缺少录音：' + name)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    # 每次测试独立 PID 文件，避免结束时误操作另一轮会话。
    args.remote_dir = '/home/run/.primebuddy/runtime/voice-test-' + uuid.uuid4().hex
    ssh = ['ssh', '-S', args.ssh_socket, '-o', 'BatchMode=yes', args.host]
    # 每次启动前核验真实配置，不在用例中偷偷改变时长或动作开关。
    code = ('import sys;sys.path[:0]=["/home/run/.primebuddy/src","/home/run/.primebuddy/_deps"];'
            'from primebuddy.config.storage import skill_config;c=skill_config("diangetai");'
            f'assert c["play_seconds"]=={args.seconds} and c["music_effects"] is False;'
            'print("TEST_CONFIG_VERIFIED")')
    subprocess.run(ssh + ['python3 -c ' + shlex.quote(code)], check=True, timeout=15)
    session = Session(args)
    results = []
    try:
        session.wait(lambda line: line.startswith('VOICE_WINDOW_OPEN'), 0, 150)
        window = session.seed(args.seconds)
        for repeat in range(args.repeat):
            for case in args.cases:
                if window is None:
                    window = session.seed(args.seconds)
                begin = session.record('case_begin', case=case, repeat=repeat + 1,
                                       music_seconds=args.seconds, window=session.events[window])
                print('CASE_BEGIN', case, repeat + 1, flush=True)
                time.sleep(.5)
                if case in ('no_wake', 'single', 'double'):
                    session.play({'no_wake': 'command', 'single': 'single', 'double': 'double'}[case])
                elif case == 'split':
                    session.play('wake')
                    time.sleep(3)
                    session.play('command')
                else:
                    raise ValueError(case)
                try:
                    heard = session.wait(lambda line: line.startswith('ASR '), begin, 12)
                    asr = session.events[heard]['line'][4:]
                    window = session.window_after_music(begin, args.seconds)
                    lines = [e.get('line', '') for e in session.events[begin:window + 1]]
                    require_clean_turn(lines)
                    targets = [line for line in lines if line.startswith('ASR ')
                               and '周杰伦' in line and '晴天' in line]
                    status = 'passed' if targets and any(
                        line.startswith('TTS ') and '正在准备播放周杰伦的晴天' in line
                        for line in lines) else 'wrong_target'
                    asr = [line[4:] for line in lines if line.startswith('ASR ')]
                except TimeoutError:
                    status = 'failed_or_timeout'
                    asr = next((e['line'][4:] for e in session.events[begin:]
                                if e.get('line', '').startswith('ASR ')), None)
                    window = None
                result = dict(case=case, repeat=repeat + 1, status=status, asr=asr)
                results.append(result)
                session.record('case_result', **result)
                print('CASE_RESULT', json.dumps(result, ensure_ascii=False), flush=True)
                if window is None:
                    # 本次用例失败后再等待已发出的回复结束；恢复唤醒不计入通过。
                    time.sleep(3)
    finally:
        # 结果文件写失败也必须先退出平台，让原厂模式有机会恢复。
        try:
            session.stop()
        finally:
            (args.output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    if any(result['status'] != 'passed' for result in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
