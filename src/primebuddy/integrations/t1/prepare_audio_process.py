"""SoC 0 只解码本次音频片段，不出声；stdin 关闭后清理独占临时文件。"""
import json
import math
import os
from pathlib import Path
import select
import signal
import sys
import tempfile
import time
import wave


def decode(data, path, stopped, Gst):
    expected, limit = data.get('expected_seconds'), data.get('max_seconds', 0)
    if expected is not None and (not math.isfinite(expected) or not 0 < expected <= 600):
        raise ValueError('invalid duration')
    if type(limit) is not int or not 0 <= limit <= 600:
        raise ValueError('invalid playback limit')
    pipeline = Gst.parse_launch('uridecodebin name=source ! audioconvert ! audioresample ! '
                               'audio/x-raw,format=S16LE,rate=16000,channels=1 ! '
                               'appsink name=pcm sync=false max-buffers=8 drop=false')
    pipeline.get_by_name('source').set_property('uri', data['url'])
    sink, bus = pipeline.get_by_name('pcm'), pipeline.get_bus()
    deadline = time.monotonic() + 40
    def check():
        if stopped() or time.monotonic() > deadline:
            raise RuntimeError('decode cancelled or timed out')
        if bus.pop_filtered(Gst.MessageType.ERROR):
            raise RuntimeError('decode failed')
    try:
        pipeline.set_state(Gst.State.PAUSED)
        while True:
            check()
            _, state, _ = pipeline.get_state(50 * Gst.MSECOND)
            ok, duration = pipeline.query_duration(Gst.Format.TIME)
            if state == Gst.State.PAUSED and ok:
                actual = duration / Gst.SECOND
                if not math.isfinite(actual) or not 0 < actual <= 600:
                    raise RuntimeError('invalid media duration')
                if expected is not None and abs(actual - expected) > max(3, expected * .03):
                    raise RuntimeError('resource duration mismatch')
                break
        target = min(actual, limit) if limit else actual
        wanted = int(target * 16000) * 2
        pipeline.set_state(Gst.State.PLAYING)
        written = 0
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, 16000, 0, 'NONE', ''))
            while written < wanted:
                check()
                sample = sink.emit('try-pull-sample', 50 * Gst.MSECOND)
                if sample is None:
                    if sink.get_property('eos'):
                        break
                    continue
                buffer = sample.get_buffer()
                pcm = buffer.extract_dup(0, buffer.get_size())[:wanted - written]
                output.writeframesraw(pcm)
                written += len(pcm)
        if written < wanted - 32000 * max(1, target * .03):
            raise RuntimeError('premature end')
        return written / 32000, bool(limit and limit < actual)
    finally:
        pipeline.set_state(Gst.State.NULL)
        _, state, _ = pipeline.get_state(5 * Gst.SECOND)
        if state != Gst.State.NULL:
            raise RuntimeError('decoder close unconfirmed')


def main():
    os.environ['GST_DEBUG'] = '0'
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init(None)
    cancelled = False
    def stop(*_):
        nonlocal cancelled
        cancelled = True
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    def stopped():
        if select.select([sys.stdin], [], [], 0)[0]:
            stop()  # EOF 或 stop 均取消；解码阶段不接受其他命令。
        return cancelled
    def emit(event, **fields):
        print(json.dumps(dict(event=event, **fields)), flush=True)
    directory = None
    try:
        data = json.loads(sys.stdin.readline())
        directory = tempfile.TemporaryDirectory(prefix='primebuddy-audio-')
        path = Path(directory.name) / 'audio.wav'
        seconds, limited = decode(data, path, stopped, Gst)
        emit('ready', path=str(path), seconds=seconds, limited=limited)
        deadline = time.monotonic() + seconds + 120
        while not stopped() and time.monotonic() < deadline:
            time.sleep(.05)
    except Exception:
        emit('error')  # 解码异常可能含签名 URL，不能直接写入日志。
    finally:
        closed = False
        try:
            if directory is not None:
                directory.cleanup()
            closed = True
        finally:
            emit('closed', confirmed=closed)


if __name__ == '__main__':
    main()
