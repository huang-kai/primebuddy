"""SDK 语音兼容入口；复用音频节点，回调只接单，主线程负责播音。"""
import logging
import time
import uuid

from primebuddy.config.storage import SetupError

logger = logging.getLogger(__name__)

ENDPOINTS = {
    'namespaced': ('/primebuddy/asr_result', '/primebuddy/PlayTts'),
    'native': ('/aima/agent/asr_result', '/aimdk_5Fmsgs/srv/PlayTts'),
}


def sdk_types():
    try:
        from aimdk_msgs.msg import AsrResult, CommonState
        from aimdk_msgs.srv import PlayTts
        # 检查实际安装的生成类型，不复制或覆盖机器人上的消息包。
        asr, request, response = AsrResult(), PlayTts.Request(), PlayTts.Response()
        for name in ('stamp', 'frame_id', 'sequence', 'meas_stamp'):
            getattr(asr.header, name)
        for name in ('text', 'event_id', 'is_final', 'confidence', 'language'):
            getattr(asr, name)
        request.header.header.stamp
        for name in ('text', 'priority_weight', 'domain', 'trace_id'):
            getattr(request.tts_req, name)
            getattr(response.tts_resp, name)
        request.tts_req.is_interrupted
        request.tts_req.priority_level.value
        response.tts_resp.priority_level.value
        response.tts_resp.is_success
        response.tts_resp.error_message
        response.tts_resp.estimated_duration
        response.header.header.stamp
        response.header.header.code
        response.header.status.value
        response.header.status.reason
        response.header.message
        CommonState.SUCCESS, CommonState.FAILURE
        CommonState.REASON_NOT_SUPPORTED, CommonState.REASON_TASK_RUNNING, CommonState.REASON_ENGINE_NOT_READY
    except (ImportError, AttributeError):
        raise SetupError('SDK 语音类型不可用或字段不匹配。请使用系统 Python，加载 T1 已安装的 ROS/aimdk_msgs setup.bash；不要覆盖原厂消息包。') from None
    return AsrResult, PlayTts, CommonState


class SdkBridge:
    def __init__(self, node, mode, available=lambda: True):
        self.node, self.available = node, available
        self.topic, self.service_name = ENDPOINTS[mode]
        self.asr_type, self.tts_type, self.state = sdk_types()
        self.publisher = self.service = None
        self.pending = None
        self.busy = True
        self.sequence = 0
        self.error = None
        self.checked_at = 0

    def check(self, force=False):
        if self.error:
            raise self.error
        if not force and time.monotonic() - self.checked_at < 1:
            return
        own = (self.node.get_name(), self.node.get_namespace())
        try:
            occupied = any((info.node_name, info.node_namespace) != own
                           for info in self.node.get_publishers_info_by_topic(self.topic))
            for name, namespace in self.node.get_node_names_and_namespaces():
                if (name, namespace) != own:
                    services = self.node.get_service_names_and_types_by_node(name, namespace)
                    occupied |= any(service == self.service_name for service, _ in services)
            if occupied:
                raise SetupError('SDK 语音入口已被其他节点占用。请使用 --sdk-bridge namespaced 并重映射旧客户端；不要同时启动同名服务。')
        except Exception as error:
            self.error = error if isinstance(error, SetupError) else SetupError('无法确认 SDK 语音入口是否空闲，请检查 ROS 图后重试。')
            raise self.error from None
        self.checked_at = time.monotonic()

    def start(self):
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        self.check(force=True)
        # RELIABLE 发布者兼容 SDK 的 sensor_data 订阅和默认可靠订阅；不重放旧指令。
        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.VOLATILE)
        self.publisher = self.node.create_publisher(self.asr_type, self.topic, qos)
        self.service = self.node.create_service(self.tts_type, self.service_name, self.on_tts)

    def publish_asr(self, text):
        if not text.strip():
            return
        self.check(force=True)
        message = self.asr_type()
        message.header.stamp = self.node.get_clock().now().to_msg()
        # ASR 没有给出采集时间/置信度/语言检测，保留未知值，不伪造测量结果。
        message.header.sequence = self.sequence
        self.sequence = (self.sequence + 1) % (2 ** 32)
        message.text, message.event_id = text, uuid.uuid4().hex
        message.is_final = True
        message.confidence = 0.0
        message.language = ''
        self.publisher.publish(message)

    def on_tts(self, request, response):
        item, result = request.tts_req, response.tts_resp
        for name in ('text', 'priority_weight', 'domain', 'trace_id'):
            setattr(result, name, getattr(item, name))
        result.priority_level.value = item.priority_level.value
        reason = ''
        reason_code = self.state.REASON_NOT_SUPPORTED
        try:
            self.check(force=True)
            if not item.text.strip() or len(item.text) > 100 or not item.domain.strip():
                reason = '需要 1–100 字文本和非空 domain。'
            elif item.priority_level.value != 6 or item.priority_weight != 0:
                reason = '首版仅支持 INTERACTION_L6、weight=0；不支持优先级排队或抢占。'
            elif self.busy or self.pending is not None or not self.available():
                reason = '框架正在收音、处理任务或播音，本次请求未接受。'
                reason_code = self.state.REASON_TASK_RUNNING
        except SetupError:
            reason = 'SDK 入口冲突或 ROS 图不可用，本次请求未接受。'
            reason_code = self.state.REASON_ENGINE_NOT_READY
        if not reason:
            self.pending = item.text
        accepted = not reason
        response.header.header.stamp = self.node.get_clock().now().to_msg()
        response.header.header.code = 0 if accepted else 1
        response.header.status.value = self.state.SUCCESS if accepted else self.state.FAILURE
        response.header.status.reason = 0 if accepted else reason_code
        response.header.message = reason or '请求已接受，不代表播放完成。'
        result.is_success, result.error_message, result.estimated_duration = accepted, reason, 0
        return response

    def drain(self, speak, microphone):
        if self.pending is None:
            return
        self.check(force=True)
        self.busy = True
        generation, phase = microphone.generation, microphone.phase
        microphone.idle(generation)
        completed = False
        try:
            # 仅主循环调用，沿用已有音频锁及完成回调，避免把自己的声音送入 ASR。
            finished = (lambda: microphone.listen(generation)) if phase == 'listening' else None
            speak(self.pending, on_finished=finished)
            completed = True
        except Exception as error:
            logger.error('SDK_TTS_PLAYBACK_FAILED %s', type(error).__name__)
        finally:
            self.pending = None
            self.busy = False
            if not completed or phase != 'listening':
                microphone.wait_for_wake(generation)

    def close(self):
        self.busy = True
        self.pending = None
        if self.service is not None:
            self.node.destroy_service(self.service)
            self.service = None
        if self.publisher is not None:
            self.node.destroy_publisher(self.publisher)
            self.publisher = None
