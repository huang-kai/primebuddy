"""接收原厂处理后音频；只在点歌窗口把完整 VAD 片段交给自有 ASR。"""
import queue
import json
import logging
import threading
import time

from primebuddy.integrations.primebot.robot_audio import AudioError
from primebuddy.core.timing import progress

logger = logging.getLogger(__name__)

AUDIO_TOPIC = '/agent/process_audio_output'
STATE_TOPIC = '/aima/agent/interaction_state'


def trace(event, **fields):
    logger.info('VOICE_TRACE %s', json.dumps({'at': round(time.monotonic(), 3), 'event': event, **fields}))


class NativeVoiceInput:
    def __init__(self, runtime, robot, stream_id=0):
        self.robot = robot
        self.stream_id = stream_id
        self.events = queue.Queue(maxsize=16)
        self.interrupts = queue.Queue(maxsize=2)
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.generation = 0
        self.error = None
        self.subscriptions = []
        self.phase = 'muted'
        self.deadline = 0
        self.opened_at_ns = 0
        self.buffer = None
        self.started_at = 0
        self.last_packet_at = 0
        self.conversation_state = None
        self.wait_for_new_listening = False
        self.last_health_check = 0
        self.window_packets = 0
        self.window_stream_packets = 0
        self.window_stale_packets = 0

    def current(self, generation):
        return not self.closed.is_set() and generation == self.generation

    def emit(self, kind, value=None):
        try:
            self.events.put_nowait((self.generation, kind, value))
        except queue.Full:
            self.error = AudioError('原厂语音事件积压，技能将退出。')

    def listen(self, generation, *, reset_deadline=True):
        if self.current(generation):
            now = time.monotonic()
            if not reset_deadline and now >= self.deadline:
                self.timeout('未识别到有效语音，等待原厂重新开放监听。')
                return
            self.phase = 'listening'
            self.wait_for_new_listening = False
            self.buffer = None
            if reset_deadline:
                self.deadline = now + 30
                self.opened_at_ns = self.robot.node.get_clock().now().nanoseconds
            self.window_packets = self.window_stream_packets = self.window_stale_packets = 0
            trace('listen_window_open', seconds=round(self.deadline - now, 3), native_state=self.conversation_state)

    def monitor_interrupt(self, generation):
        """暂存完整语句；调用方必须已有明确的取消来源，不能把 VAD 当作唤醒。"""
        if self.current(generation):
            self.phase = 'interrupt'
            self.buffer = None
            self.opened_at_ns = self.robot.node.get_clock().now().nanoseconds
            self.window_packets = self.window_stream_packets = self.window_stale_packets = 0
            self._clear_interrupts()

    def stop_interrupt(self, generation):
        if self.current(generation) and self.phase == 'interrupt':
            self.phase = 'muted'
            self.buffer = None
            self._clear_interrupts()

    def _clear_interrupts(self):
        while True:
            try:
                self.interrupts.get_nowait()
            except queue.Empty:
                return

    def begin_interrupt(self, generation):
        """旧轮失效，收音继续暂存，等待旧任务确认完成业务清理。"""
        if not self.current(generation):
            return None
        self.generation += 1
        trace('interrupt_pending', generation=self.generation)
        return self.generation

    def resume_after_interrupt(self, generation):
        """清理确认后开窗，保留已开始或已完整到达的下一句话。"""
        if not self.current(generation):
            return None
        self.phase = 'listening'
        self.deadline = time.monotonic() + 30
        self.window_packets = self.window_stream_packets = self.window_stale_packets = 0
        while True:
            try:
                audio = self.interrupts.get_nowait()
            except queue.Empty:
                break
            self.emit('utterance', audio)
        trace('interrupt_listen_open', generation=self.generation)
        return self.generation

    def idle(self, generation):
        if self.current(generation):
            self.phase = 'muted'
            self.buffer = None

    def reset_interaction(self, generation):
        """独占应用交接边界：废弃旧回调、残句及事件，保持静音直到宿主恢复监听。"""
        if not self.current(generation):
            return self.generation
        self.idle(generation)
        self.generation += 1
        self.deadline = self.started_at = self.last_packet_at = 0
        self.wait_for_new_listening = False
        self._clear_interrupts()
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        trace('interaction_reset', generation=self.generation)
        return self.generation

    def wait_for_listening(self, generation):
        if self.current(generation):
            self.phase = 'waiting'
            self.buffer = None
            if self.conversation_state == 1 and not self.wait_for_new_listening:
                self.emit('native_listening')
                self.listen(generation)

    def on_state(self, msg):
        if self.closed.is_set() or self.error is not None:
            return
        state = int(msg.main_state.value)
        previous, self.conversation_state = self.conversation_state, state
        if state != previous:
            trace('native_state', previous=previous, state=state, phase=self.phase)
        if self.phase == 'listening' and state == 3:
            # 原厂正在应答时丢弃未完成片段，恢复监听后必须等待新的 BEGIN。
            self.buffer = None
        # 只在空闲等待时跟随原厂开放监听；此状态也可能来自播放结束，不代表唤醒。
        # 跟随首次或当前已知的 LISTENING；已开窗后重复状态不重置，也不取消任务。
        # 超时后须有新的 LISTENING 转换，不能凭缓存或重复状态立即重开。
        if self.phase == 'waiting' and state == 1 and (not self.wait_for_new_listening or previous != 1):
            self.emit('native_listening')
            self.listen(self.generation)

    def on_audio(self, msg):
        if self.closed.is_set() or self.error is not None:
            return
        try:
            if self.phase in ('listening', 'interrupt'):
                self.window_packets += 1
            # 本台 T1 实测内置流为0，不能套用 X2 文档的1或混合多路音频。
            if int(msg.stream_id) != self.stream_id:
                return
            state = int(msg.audio_vad_state.value)
            if state in (1, 3):
                trace('vad_boundary', vad=state, phase=self.phase)
            if self.phase not in ('listening', 'interrupt'):
                return
            self.window_stream_packets += 1
            if self.phase == 'listening' and self.conversation_state == 3:
                if state in (1, 3):
                    trace('discard_native_speaking', vad=state)
                return
            stamp = msg.header.stamp
            timestamp = stamp.sec * 1_000_000_000 + stamp.nanosec
            if timestamp and timestamp < self.opened_at_ns:
                self.window_stale_packets += 1
                if state in (1, 3):
                    trace('discard_stale', vad=state, before_window_ms=round((self.opened_at_ns - timestamp) / 1e6, 1))
                return
            state = int(msg.audio_vad_state.value)
            pcm = bytes(msg.audio_data)
            if state not in (0, 1, 2, 3) or len(pcm) % 2:
                raise AudioError('原厂 VAD 音频格式不符合 PCM16 协议。')
            now = time.monotonic()
            if state == 0:
                self.buffer = None
                return
            if state == 1:
                self.buffer = bytearray()
                self.started_at = now
            if self.buffer is None:
                # 丢失 BEGIN 的片段不拼接到下一句，也不发送残句给 ASR。
                if state == 3:
                    trace('discard_missing_begin')
                return
            self.buffer.extend(pcm)
            self.last_packet_at = now
            if len(self.buffer) > 16000 * 2 * 15:
                if self.phase == 'interrupt':
                    self.buffer = None
                    trace('discard_interrupt_too_long')
                else:
                    self.timeout('原厂语音超过15秒，已丢弃，未提交截断音频。')
            elif state == 3:
                answer = bytes(self.buffer)
                self.buffer = None
                if len(answer) >= 16000 * 2 * .2:
                    trace('utterance', seconds=round(len(answer) / 32000, 3))
                    if self.phase == 'interrupt':
                        try:
                            self.interrupts.put_nowait(answer)
                        except queue.Full:
                            # 只保留有限候选；普通语音和音乐内容不积压为后续请求。
                            trace('discard_interrupt_backlog')
                    else:
                        self.phase = 'muted'
                        self.emit('utterance', answer)
        except Exception as error:
            self.error = error if isinstance(error, AudioError) else AudioError('原厂音频接收失败。')

    def timeout(self, reason=None):
        if reason is None:
            # 没有完整语句不等于用户没说话；区分前端无数据和本地丢帧。
            if self.window_packets == 0:
                reason = '未收到原厂音频，请通过机器人的原厂唤醒方式重新唤醒后再试。'
            elif self.window_stream_packets == 0:
                reason = '未收到本体麦克风流，只有其他音频流。'
            elif self.window_stale_packets == self.window_stream_packets:
                reason = '本体音频均早于监听窗口，请检查跨板时钟和音频延迟。'
            else:
                reason = '未收到完整 VAD 语句，请重新唤醒后再试。'
        trace('timeout', reason=reason, phase=self.phase, native_state=self.conversation_state,
              packets=self.window_packets, body_packets=self.window_stream_packets,
              stale_packets=self.window_stale_packets)
        self.buffer = None
        self.phase = 'muted'
        self.wait_for_new_listening = True
        self.emit('timeout', reason)

    def check_health(self):
        now = time.monotonic()
        if self.phase == 'listening':
            if self.buffer is not None:
                if now - self.last_packet_at > 3 or now - self.started_at > 18:
                    self.timeout('未收到原厂 VAD 结束帧，已丢弃未完成语音。')
            elif now >= self.deadline:
                self.timeout()
        elif self.phase == 'interrupt' and self.buffer is not None:
            if now - self.last_packet_at > 3 or now - self.started_at > 18:
                self.buffer = None
                trace('discard_incomplete_interrupt')
        # 该话题不是连续流；安静时没有音频不能当成采集故障。
        if self.ready.is_set() and now - self.last_health_check > 5:
            self.last_health_check = now
            if any(self.robot.node.count_publishers(topic) == 0 for topic in (AUDIO_TOPIC, STATE_TOPIC)):
                self.error = AudioError('原厂语音或会话状态发布者已离线。')

    def start(self):
        from aimdk_msgs.msg import ProcessedAudioOutput, AgentConversationState, AgentConversationMainState
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        if AgentConversationMainState.LISTENING != 1:
            raise AudioError('原厂会话状态枚举不匹配。')
        qos = QoSProfile(depth=500, reliability=ReliabilityPolicy.BEST_EFFORT,
                         durability=DurabilityPolicy.VOLATILE)
        for msg_type, topic, callback in ((ProcessedAudioOutput, AUDIO_TOPIC, self.on_audio),
                                           (AgentConversationState, STATE_TOPIC, self.on_state)):
            self.subscriptions.append(self.robot.node.create_subscription(msg_type, topic, callback, qos))
        deadline = time.monotonic() + 15
        while any(self.robot.node.count_publishers(topic) == 0 for topic in (AUDIO_TOPIC, STATE_TOPIC)):
            from primebuddy.integrations.primebot.robot_audio import check_cancel
            check_cancel()
            if time.monotonic() >= deadline:
                raise AudioError('原厂语音前端话题未就绪，未回退到原始麦克风。')
            self.robot.spin()
        self.ready.set()
        progress('原厂语音订阅已就绪，等待完整 VAD 音频；ASR、LLM、TTS 使用千问。')

    def close(self):
        self.closed.set()
        self.buffer = None
        self._clear_interrupts()
        for subscription in self.subscriptions:
            self.robot.node.destroy_subscription(subscription)
        self.subscriptions.clear()
