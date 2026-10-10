"""唯一语音会话：复用已验证的底层音频实现，不调用旧技能会话。"""
import json
import queue
import re
import time

from .engine import execute
from .planner import plan
from .grounding import vocabulary, confirmation_for, confirmation_answer
from .voice_support.speech_cache import SpeechCache
from .voice_support.timing import measure


def wake_only(text):
    compact = re.sub(r'[，,。！？!?\s]', '', text)
    # 仅处理现场已观察到的重复唤醒误字，不吞掉带操作内容的整句。
    if compact in {'冤哉冤哉', '原在原在'}:
        return True
    return re.fullmatch(r'(?:[元源][仔崽])+', compact) is not None


def serve(registry, context, sdk_bridge='namespaced'):
    """由 start 进入 T1 会话；设备预检、互斥和模式恢复集中在此管理。"""
    from .voice_support.native_voice import NativeVoiceInput
    from .voice_support.native_agent import voice_only_agent
    from .voice_support.qwen_asr import transcribe_wav, load_config
    from .voice_support.runtime import private_runtime
    from .voice_support.robot_audio import audio_session, connect_soc0, check_cancel, AudioError
    from .voice_support.speech_audio import encode_asr_wav, native_preflight
    from .sdk_bridge import SdkBridge

    context.runtime = private_runtime()
    context.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    connect_soc0(context.runtime)
    native_preflight()
    config = load_config()
    skill_context = registry.planning_context(context)
    vocabulary_words = vocabulary(skill_context)
    snapshot_at = time.monotonic()
    print('ASR_MODEL', context.asr_model, 'VOCABULARY_COUNT', len(vocabulary_words), flush=True)
    speech = SpeechCache(context.runtime / 'tts-cache')
    with audio_session(context.runtime) as robot:
        status = robot.check()
        required = {'play_service': '播放服务', 'stop_service': '停止播放服务',
                    'soc0_ssh_connected': '跨板 SSH 连接'}
        missing = [label for key, label in required.items() if not status[key]]
        if missing:
            raise AudioError('机器人音频接口未就绪：' + '、'.join(missing) + '。未接管原厂 Agent。')

        def speak(text, on_finished=None):
            # 下层 TTS 最长 100 字；按句拆分，所有播放均在主线程。
            if not text:
                return
            print('TTS', text, flush=True)
            with measure('播报总耗时'):
                for offset in range(0, len(text), 90):
                    path = speech.prepare(text[offset:offset + 90])
                    with measure('提示音传输与播放'):
                        try:
                            if on_finished is not None and offset + 90 >= len(text):
                                robot.play(path, on_finished=on_finished)
                            else:
                                robot.play(path)
                        except AudioError as error:
                            print('AUDIO_ERROR', str(error), flush=True)
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
                    print('SDK_BRIDGE', bridge.topic, bridge.service_name, flush=True)
                ask('PrimeBuddy 已启动，你想做什么？', microphone.generation)
                print('VOICE_WINDOW_OPEN 30s · 等待原厂 VAD 音频', flush=True)
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
                        print('VOICE_WAITING_FOR_WAKE', audio or 'silence', flush=True)
                        continue
                    if kind == 'wake':
                        # 原厂前端负责唤醒回应；不追加第二段 TTS，也不重置已开始的收音。
                        print('VOICE_WINDOW_OPEN 30s after wake', flush=True)
                        continue
                    if bridge:
                        bridge.busy = True
                    microphone.idle(generation)
                    turn_started = time.monotonic()
                    print('VOICE_PROCESSING', flush=True)
                    try:
                        if time.monotonic() - snapshot_at > 300:
                            skill_context = registry.planning_context(context)
                            vocabulary_words = vocabulary(skill_context)
                            snapshot_at = time.monotonic()
                        with measure('ASR识别'):
                            text = transcribe_wav(encode_asr_wav(audio, sample_rate=16000), config,
                                                  context.asr_model, vocabulary_words)
                        print('ASR', text, flush=True)
                        if bridge:
                            bridge.publish_asr(text)
                        if wake_only(text):
                            microphone.listen(generation)
                            print('VOICE_WINDOW_OPEN 30s after wake text', flush=True)
                            continue
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
                        if pending_plan is not None and answer is True:
                            proposal, pending_plan = pending_plan, None
                        else:
                            pending_plan = None
                            with measure('LLM计划'):
                                proposal = plan(text, registry, config, history, skill_context)
                            question = confirmation_for(proposal, registry, text, skill_context)
                            if question:
                                pending_plan = proposal
                                history.clear()
                                ask(question, generation)
                                print('VOICE_CONFIRMATION_PENDING', flush=True)
                                continue
                        print('PLAN_READY_SECONDS', round(time.monotonic() - turn_started, 3), flush=True)
                        if proposal['tasks']:
                            with measure('任务执行与反馈'):
                                execute(proposal, registry, context, speak, checkpoint=checkpoint)
                            history.clear()
                            ask('还想做什么？', generation)
                        else:
                            history.extend([{'role': 'user', 'content': text},
                                            {'role': 'assistant', 'content': json.dumps(proposal, ensure_ascii=False)}])
                            history = history[-6:]
                            ask(proposal['reply'] or '请告诉我你想做什么。', generation)
                    except Exception as error:
                        check_cancel()
                        print('TURN_ERROR', type(error).__name__, flush=True)
                        if isinstance(error, AudioError):
                            print('AUDIO_ERROR', str(error), flush=True)
                        history.clear()
                        pending_plan = None
                        microphone.idle(generation)
                        ask('本轮未能完整完成，执行结果请以实际状态为准。请重新告诉我你的要求。', generation)
                    print('TURN_SECONDS', round(time.monotonic() - turn_started, 3), flush=True)
                    print('VOICE_WINDOW_OPEN 30s · 等待原厂 VAD 音频', flush=True)
            finally:
                context.cancelled.set()
                try:
                    if bridge:
                        bridge.close()
                finally:
                    microphone.close()
