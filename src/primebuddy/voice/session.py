"""唯一语音会话：复用已验证的底层音频实现，不调用旧技能会话。"""
import logging
import queue
import re
import time

from primebuddy.core.engine import execute
from primebuddy.core.planner import plan, continue_plan
from primebuddy.core.grounding import vocabulary, confirmation_for, confirmation_answer
from primebuddy.voice.speech_cache import SpeechCache
from primebuddy.core.timing import measure

logger = logging.getLogger(__name__)


def wake_command(text):
    """双唤醒词开始新请求；只识别句首/句界，避免改写指令中的人名或歌名。"""
    # 先跳过明确引用的歌名/文本，再查找句界上的唤醒词。
    matches = [match for match in re.finditer(
        r'《[^》]*》|“[^”]*”|"[^"]*"|'
        r'(?P<wake>(?:^|[。！？!?])\s*(?:(?:[元源袁][仔崽]|冤哉|原在)[\s，,。！？!?]*){2,})',
        text) if match.group('wake') is not None]
    if matches:
        # 允许“旧话。元仔元仔，新指令”；唤醒前的内容不再参与新一轮计划。
        return True, text[matches[-1].end():].strip(' \t\r\n，,。！？!?：:')
    compact = re.sub(r'[，,。！？!?\s]', '', text)
    if re.fullmatch(r'[元源][仔崽]', compact):
        return True, ''
    # 单个“袁仔”“原在”等有其他含义，不能仅凭近音就删除。
    return False, text


def serve(registry, context, sdk_bridge='namespaced'):
    """由 start 进入 T1 会话；设备预检、互斥和模式恢复集中在此管理。"""
    from primebuddy.integrations.t1.native_voice import NativeVoiceInput
    from primebuddy.voice.audio import AudioOutput
    from primebuddy.integrations.t1.native_agent import voice_only_agent
    from primebuddy.providers import transcribe_wav
    from primebuddy.config.models import load_model
    from primebuddy.config.runtime import private_runtime
    from primebuddy.integrations.t1.robot_audio import audio_session, connect_soc0, check_cancel, AudioError
    from primebuddy.integrations.t1.speech_audio import encode_asr_wav, native_preflight
    from primebuddy.integrations.t1.sdk_bridge import SdkBridge

    config = load_model('asr', context.asr_model)
    llm_config = load_model('llm')
    context.runtime = private_runtime()
    context.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    connect_soc0(context.runtime)
    native_preflight()
    skill_context = {}
    vocabulary_words = vocabulary(skill_context)
    logger.info('ASR_MODEL %s:%s VOCABULARY_COUNT %s', config['provider'], config['model'], len(vocabulary_words))
    speech = SpeechCache(context.runtime / 'tts-cache')
    with audio_session(context.runtime) as robot:
        status = robot.check()
        required = {'play_service': '播放服务', 'stop_service': '停止播放服务',
                    'soc0_ssh_connected': '跨板 SSH 连接'}
        missing = [label for key, label in required.items() if not status[key]]
        if missing:
            raise AudioError('机器人音频接口未就绪：' + '、'.join(missing) + '。未接管原厂 Agent。')

        context.audio = AudioOutput(context.runtime, context.checkpoint)

        def speak(text, on_finished=None):
            # 下层 TTS 最长 100 字；按句拆分，所有播放均在主线程。
            if not text:
                return
            logger.info('TTS %s', text)
            with measure('播报总耗时'):
                for offset in range(0, len(text), 90):
                    path = speech.prepare(text[offset:offset + 90])
                    with measure('提示音传输与播放'):
                        try:
                            if on_finished is not None and offset + 90 >= len(text):
                                context.audio.play(path, on_finished=on_finished, checkpoint=robot.spin)
                            else:
                                context.audio.play(path, checkpoint=robot.spin)
                        except AudioError as error:
                            logger.error('AUDIO_ERROR %s', str(error))
                            raise

        with voice_only_agent(context.runtime):
            microphone = NativeVoiceInput(context.runtime, robot)
            bridge = None if sdk_bridge == 'off' else SdkBridge(
                robot.node, sdk_bridge,
                available=lambda: microphone.phase in ('listening', 'waiting')
                and microphone.buffer is None and microphone.events.empty())
            history = []
            pending_plan = None
            def ask(text, generation):
                speak(text, on_finished=lambda: microphone.listen(generation))

            def checkpoint():
                check_cancel()
                robot.spin()
                microphone.check_health()
                if microphone.error:
                    raise microphone.error
                if bridge and bridge.publisher is not None:
                    bridge.check()
            try:
                microphone.start()
                deadline = time.monotonic() + 10
                while not microphone.ready.is_set():
                    checkpoint()
                    if time.monotonic() > deadline:
                        raise RuntimeError('麦克风未就绪')
                if bridge:
                    # DDS 图异步发现；启动前留出发现窗口，运行中继续检查迟到节点。
                    discover_until = time.monotonic() + 1
                    while time.monotonic() < discover_until:
                        checkpoint()
                    bridge.start()
                    logger.info('SDK_BRIDGE %s %s', bridge.topic, bridge.service_name)
                ask('PrimeBuddy 已启动，你想做什么？', microphone.generation)
                logger.info('VOICE_WINDOW_OPEN 30s · 等待原厂 VAD 音频')
                while True:
                    if bridge:
                        bridge.busy = False
                    checkpoint()
                    if bridge:
                        bridge.drain(speak, microphone)
                    try:
                        generation, kind, audio = microphone.events.get_nowait()
                    except queue.Empty:
                        continue
                    if not microphone.current(generation):
                        continue
                    if kind == 'timeout':
                        history.clear()
                        pending_plan = None
                        microphone.wait_for_wake(generation)
                        logger.info('VOICE_WAITING_FOR_WAKE %s', audio or 'silence')
                        continue
                    if kind == 'wake':
                        # 原厂前端负责唤醒回应；不追加第二段 TTS，也不重置已开始的收音。
                        history.clear()
                        pending_plan = None
                        logger.info('VOICE_WINDOW_OPEN 30s after wake')
                        continue
                    if bridge:
                        bridge.busy = True
                    microphone.idle(generation)
                    turn_started = time.monotonic()
                    logger.info('VOICE_PROCESSING')
                    try:
                        vocabulary_words = vocabulary(skill_context)
                        with measure('ASR识别'):
                            text = transcribe_wav(encode_asr_wav(audio, sample_rate=16000), config,
                                                  vocabulary=vocabulary_words)
                        logger.info('ASR %s', text)
                        woken, text = wake_command(text)
                        if woken:
                            history.clear()
                            pending_plan = None
                            logger.info('VOICE_WAKE_TEXT %s', 'command' if text else 'listen')
                        if woken and not text:
                            microphone.listen(generation)
                            logger.info('VOICE_WINDOW_OPEN 30s after wake text')
                            continue
                        if bridge:
                            bridge.publish_asr(text)
                        if text.strip('。！？!? ') in {'退出助手', '退出语音助手'}:
                            speak('语音助手已退出。')
                            return
                        context.cancelled.clear()
                        answer = confirmation_answer(text)
                        if pending_plan is not None and answer is False:
                            pending_plan = None
                            history.clear()
                            ask('已取消，请重新告诉我你的要求。', generation)
                            continue
                        confirmed = pending_plan is not None and answer is True
                        if confirmed:
                            proposal, pending_plan = pending_plan, None
                        else:
                            pending_plan = None
                            with measure('LLM计划'):
                                proposal = plan(text, registry, llm_config, history, skill_context, context=context)
                        logger.info('PLAN_READY_SECONDS %s', round(time.monotonic() - turn_started, 3))
                        executed = False
                        while proposal['tasks']:
                            if not confirmed:
                                instruction = (next(message['content'] for message in reversed(proposal.state.messages)
                                                    if message['role'] == 'user') if hasattr(proposal, 'state') else text)
                                question = confirmation_for(proposal, registry, instruction, skill_context)
                                if question:
                                    pending_plan = proposal
                                    history.clear()
                                    ask(question, generation)
                                    logger.info('VOICE_CONFIRMATION_PENDING')
                                    break
                            confirmed = False
                            with measure('任务执行与反馈'):
                                results = execute(proposal, registry, context, speak, checkpoint=checkpoint)
                            executed = True
                            history.clear()
                            with measure('Agent结果回传'):
                                proposal = continue_plan(proposal, results, registry)
                        if pending_plan is not None:
                            continue
                        if executed:
                            # 实际业务结果由 execute 播报，避免模型总结改变未核实状态。
                            ask('还想做什么？', generation)
                        else:
                            history.extend([{'role': 'user', 'content': text},
                                            {'role': 'assistant', 'content': proposal['reply']}])
                            history = history[-6:]
                            ask(proposal['reply'] or '请告诉我你想做什么。', generation)
                    except Exception as error:
                        check_cancel()
                        logger.error('TURN_ERROR %s', type(error).__name__)
                        if isinstance(error, AudioError):
                            logger.error('AUDIO_ERROR %s', str(error))
                        history.clear()
                        pending_plan = None
                        microphone.idle(generation)
                        ask('本轮未能完整完成，执行结果请以实际状态为准。请重新告诉我你的要求。', generation)
                    logger.info('TURN_SECONDS %s', round(time.monotonic() - turn_started, 3))
                    logger.info('VOICE_WINDOW_OPEN 30s · 等待原厂 VAD 音频')
            finally:
                context.cancelled.set()
                try:
                    if bridge:
                        bridge.close()
                finally:
                    try:
                        microphone.close()
                    finally:
                        context.audio.close()
                        context.audio = None
