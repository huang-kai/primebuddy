"""麦克风预检、语音端点检测及 ASR WAV 编码。"""
from collections import deque
import io
import json
from pathlib import Path
import shlex
import subprocess
import wave

from primebuddy.providers.errors import AsrError
from primebuddy.config.models import load_model
from primebuddy.integrations.primebot.robot_audio import require_robot, ssh_options

from .device import host_command
from primebuddy.config.runtime import RUNTIME
SOURCE = 'alsa_input.platform-aw89403_sound.pro-input-0'
RATE = 48000
FRAME_SAMPLES = 960
FRAME_BYTES = FRAME_SAMPLES * 8 * 2


def preflight(robot=None):
    require_robot()
    load_model('asr')
    capture_publishers = robot.node.count_publishers('/aima/hal/audio/capture') if robot is not None else 0
    code = (
        'import json,os,subprocess,urllib.request;'
        'r=urllib.request.urlopen("http://127.0.0.1:50080/json/get_all_apps_info",timeout=8);'
        'd=json.load(r);a=d["data"]["agent"];'
        'e=dict(os.environ,PULSE_SERVER="unix:/run/user/1000/pulse/native");'
        's=subprocess.check_output(["pactl","get-default-source"],env=e,timeout=8,text=True).strip();'
        'lines=subprocess.check_output(["pactl","list","sources","short"],env=e,timeout=8,text=True).splitlines();'
        'matches=[x.split("\\t") for x in lines if x.split("\\t")[1]==s];'
        'print(json.dumps({"agent_status":a["status"],"agent_pid":a["pid"],'
        '"source":s,"format":matches[0][3] if matches else ""}))'
    )
    if capture_publishers:
        # SDK存在真实发布者时不再要求PulseAudio；实际格式由首帧校验。
        code = ('import json,urllib.request;'
                'd=json.load(urllib.request.urlopen("http://127.0.0.1:50080/json/get_all_apps_info",timeout=8));'
                'a=d["data"]["agent"];'
                'print(json.dumps({"agent_status":a["status"],"agent_pid":a["pid"]}))')
    result = subprocess.run(host_command(RUNTIME, 'python3 -c ' + shlex.quote(code)),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
    if result.returncode:
        raise AsrError('跨板预检失败，请核对 SSH 登录和机器人连接。')
    info = json.loads(result.stdout)
    if capture_publishers:
        return {**info, 'capture_backend': 'sdk', 'capture_publishers': capture_publishers}
    if info['source'] != SOURCE or info['format'] != 's16le 8ch 48000Hz':
        raise AsrError('默认麦克风或格式与已测基线不同，未猜测采集参数。')
    return info


class SpeechEndpoint:
    """每帧 20 毫秒；连续静音 0.8 秒后结束一句，短噪声不会提交 ASR。"""
    def __init__(self):
        self.warmup_frames = 0
        self.pre_roll = deque(maxlen=20)
        self.chunks = []
        self.start_frames = self.voice_frames = self.silence_frames = 0

    def feed(self, pcm, active):
        if self.warmup_frames < 50:
            self.warmup_frames += 1
            return None
        if not self.chunks:
            self.pre_roll.append(pcm)
            self.start_frames = self.start_frames + 1 if active else 0
            if self.start_frames >= 8:
                self.chunks.extend(self.pre_roll)
                self.voice_frames = 8
            return None
        self.chunks.append(pcm)
        if active:
            self.voice_frames += 1
            self.silence_frames = 0
        else:
            self.silence_frames += 1
        if self.silence_frames >= 40:
            if self.voice_frames >= 20:
                return b''.join(self.chunks)
            self.chunks.clear()
            self.pre_roll.clear()
            self.start_frames = self.voice_frames = self.silence_frames = 0
        if len(self.chunks) >= 650:
            raise AsrError('回答持续过长，未提交截断音频；请用一句话点歌。')
        return None


def encode_asr_wav(pcm, sample_rate=48000):
    if not pcm or len(pcm) % 2 or sample_rate not in (16000, 48000):
        raise AsrError('短句音频格式无效，未调用 ASR。')
    if sample_rate != 16000:
        result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error',
            '-f', 's16le', '-ar', str(sample_rate), '-ac', '1', '-i', 'pipe:0',
            '-ar', '16000', '-ac', '1', '-f', 's16le', 'pipe:1'],
            input=pcm, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        if result.returncode or not result.stdout:
            raise AsrError('短句音频转换失败，未调用 ASR。')
        pcm = result.stdout
    # 原厂前端已经输出16kHz PCM，直接封装WAV，避免重复重采样。
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(pcm)
    return output.getvalue()


def native_preflight():
    """新流程只要求原厂消息定义和云配置，不再要求原始采音或本地唤醒模型。"""
    models = {role: load_model(role) for role in ('asr', 'llm', 'tts')}
    try:
        from aimdk_msgs.msg import ProcessedAudioOutput, AgentConversationState
    except ImportError:
        raise AsrError('目标 T1/Q1 的原厂语音消息不可用，请加载该设备的 ROS 环境。') from None
    for msg_type, fields in ((ProcessedAudioOutput, {'header', 'stream_id', 'audio_vad_state', 'audio_data'}),
                             (AgentConversationState, {'main_state'})):
        if not fields.issubset(msg_type.get_fields_and_field_types()):
            raise AsrError('原厂语音消息定义不匹配。')
    return {'capture_backend': 'native_processed_audio', 'sample_rate': 16000,
            'stream_id': 0, 'cloud': {role: model['provider'] + ':' + model['model'] for role, model in models.items()}}
