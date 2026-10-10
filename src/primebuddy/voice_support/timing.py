"""仅输出阶段名称和耗时，不记录凭据或资源地址。"""
from contextlib import contextmanager
import json
import time


# 在耗时较长的步骤开始前提示，让控制台不会等到步骤结束才有输出。
STAGE_MESSAGES = {
    '千问ASR': '正在进行 ASR 语音识别…',
    'LLM意图识别': '正在识别点歌意图…',
    'QQ搜索': '正在搜索 QQ 音乐…',
    '歌曲流预缓冲': '正在连接歌曲流并检查时长…',
    '歌曲下载': '正在下载歌曲…',
    '歌曲解码': '正在转换歌曲音频…',
    '歌曲跨板传输': '正在传输歌曲到播放板…',
    '歌名或答复TTS': '正在合成语音答复…',
}


def progress(message):
    print(f'[{time.strftime("%H:%M:%S")}] [点歌台] {message}', flush=True)


@contextmanager
def measure(stage):
    if stage in STAGE_MESSAGES:
        progress(STAGE_MESSAGES[stage])
    start = time.monotonic()
    ok = False
    try:
        yield
        ok = True
    finally:
        print('STAGE_TIME', json.dumps({'stage': stage,
              'seconds': round(time.monotonic() - start, 3), 'ok': ok},
              ensure_ascii=False), flush=True)
