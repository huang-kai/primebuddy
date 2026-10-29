"""唯一语音会话：复用已验证的底层音频实现，不调用旧技能会话。"""
import logging
import queue
import time

from primebuddy.core.engine import execute, CleanupUnconfirmed, exclusive_interaction
from primebuddy.core.planner import plan, continue_plan
from primebuddy.core.grounding import vocabulary, confirmation_for, confirmation_answer
from primebuddy.voice.speech_cache import SpeechCache, NETWORK_ERROR_PROMPT
from primebuddy.providers.errors import ProviderNetworkError
from primebuddy.core.timing import measure

logger = logging.getLogger(__name__)
INTERACTION_RECOVERY_INTERVAL = 5.0


def serve(registry, context, sdk_bridge='namespaced'):
    """由 start 进入自动识别的机器人语音会话；设备预检、互斥和模式恢复集中在此管理。"""
    from primebuddy.integrations.primebot.device import create_voice
    from primebuddy.voice.audio import AudioOutput
    from primebuddy.integrations.primebot.native_agent import voice_only_agent
    from primebuddy.providers import transcribe_wav
    from primebuddy.config.models import load_model
    from primebuddy.config.runtime import private_runtime
    from primebuddy.integrations.primebot.robot_audio import audio_session, connect_soc0, check_cancel, AudioError
    from primebuddy.integrations.primebot.speech_audio import encode_asr_wav
    from primebuddy.integrations.primebot.sdk_bridge import SdkBridge

    voice = create_voice()
    logger.info('ROBOT_VOICE %s %s', voice.device.series, voice.device.sku)
    config = load_model('asr', context.asr_model)
    llm_config = load_model('llm')
    context.runtime = private_runtime()
    context.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    registry.prepare_voice(context)
    connect_soc0(context.runtime)
    voice.preflight()
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

        audio_output = AudioOutput(context.runtime, context.checkpoint)
        last_network_notice = None

        def speak(text, on_finished=None):
            nonlocal last_network_notice
            # 下层 TTS 最长 100 字；按句拆分，所有播放均在主线程。
            if not text:
                return
            check_cancel()
            registry.check_resources(context, {'speaker'})
            network_notice = text == NETWORK_ERROR_PROMPT
            if network_notice and last_network_notice is not None and time.monotonic() - last_network_notice < 60:
                logger.info('NETWORK_NOTICE suppressed')
                if on_finished is not None:
                    on_finished()
                return
            logger.info('TTS %s', text)
            with measure('播报总耗时'):
                for offset in range(0, len(text), 90):
                    check_cancel()
                    path = speech.prepare(text[offset:offset + 90])
                    with measure('提示音传输与播放'):
                        try:
                            # 媒体执行器负责等待；宿主额外阻塞会延迟积压的开始反馈，误判播放时长。
                            if on_finished is not None and offset + 90 >= len(text):
                                audio_output.play(path, on_finished=on_finished, checkpoint=lambda: robot.spin(seconds=0))
                            else:
                                audio_output.play(path, checkpoint=lambda: robot.spin(seconds=0))
                        except AudioError as error:
                            logger.error('AUDIO_ERROR %s', str(error))
                            raise
            if network_notice:
                last_network_notice = time.monotonic()

        with voice_only_agent(context.runtime):
            microphone = voice.microphone(context.runtime, robot)
            bridge = None if sdk_bridge == 'off' else SdkBridge(
                robot.node, sdk_bridge,
                available=lambda: microphone.phase in ('listening', 'waiting')
                and microphone.buffer is None and microphone.events.empty())
            history = []
            pending_plan = None
            def ask(text, generation):
                speak(text, on_finished=lambda: microphone.listen(generation))

            def recover_prompt(text, generation):
                try:
                    try:
                        ask(text, generation)
                    except ProviderNetworkError:
                        if text == NETWORK_ERROR_PROMPT:
                            raise
                        ask(NETWORK_ERROR_PROMPT, generation)
                except CleanupUnconfirmed:
                    raise
                except Exception as error:
                    check_cancel()
                    registry.check_resources(context, {'speaker'})
                    audio_output.ensure_idle()
                    logger.error('RECOVERY_PROMPT_FAILED %s', type(error).__name__)
                    # 错误播报也可能失败；确认已停止后，等待原厂重新开放监听。
                    microphone.wait_for_listening(generation)
                    logger.info('VOICE_WAITING_FOR_NATIVE_LISTENING recovery')

            def checkpoint():
                check_cancel()
                robot.spin()
                if microphone.phase == 'exclusive':
                    return
                microphone.check_health()
                if microphone.error:
                    raise microphone.error
                if bridge and bridge.publisher is not None:
                    bridge.check()

            def cleanup_checkpoint():
                # 清理期间继续处理原厂订阅；不清除取消状态、不开始新任务或播报。
                robot.spin(cancellable=False)
                if microphone.phase == 'exclusive':
                    return
                microphone.check_health()
                if microphone.error:
                    raise microphone.error

            def release_interaction(task, generation, *, recover=False):
                # 子进程已由 execute 监督完成；独立查询设备状态后才能废弃游戏事件并交还监听。
                resources = registry.skills[task['skill']]['actions'][task['action']]['resources']
                while True:
                    check_cancel()
                    try:
                        if not context.simulate:
                            skill = registry.load(task['skill'])
                            if recover:
                                skill._verify(context, recover=True)
                            else:
                                skill._verify(context)
                        registry.check_resources(context, resources)
                    except CleanupUnconfirmed:
                        # 旧进程/设备未释放时保持宿主存活和语音暂停，不能把清理失败当作退出 Buddy。
                        # recover 仅处理该技能记录的资源，且由 _verify 先核对旧进程身份。
                        logger.error('INTERACTION_RECOVERY_PENDING skill=%s', task['skill'])
                        recover = True
                        retry_at = time.monotonic() + INTERACTION_RECOVERY_INTERVAL
                        while time.monotonic() < retry_at:
                            check_cancel()
                            robot.spin(cancellable=False)
                            time.sleep(.02)
                    else:
                        logger.info('INTERACTION_RELEASED skill=%s', task['skill'])
                        return microphone.reset_interaction(generation)
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
                try:
                    speak('PrimeBuddy 已启动，你想做什么？')
                except ProviderNetworkError:
                    speak(NETWORK_ERROR_PROMPT)
                microphone.wait_for_listening(microphone.generation)
                logger.info('VOICE_WAITING_FOR_NATIVE_LISTENING startup')
                while True:
                    if bridge:
                        bridge.busy = False
                    checkpoint()
                    if bridge:
                        try:
                            bridge.drain(speak, microphone)
                        except ProviderNetworkError:
                            check_cancel()
                            history.clear()
                            pending_plan = None
                            ask(NETWORK_ERROR_PROMPT, microphone.generation)
                            continue
                    try:
                        generation, kind, audio = microphone.events.get_nowait()
                    except queue.Empty:
                        continue
                    if not microphone.current(generation):
                        continue
                    if kind == 'timeout':
                        history.clear()
                        pending_plan = None
                        microphone.wait_for_listening(generation)
                        logger.info('VOICE_WAITING_FOR_NATIVE_LISTENING %s', audio or 'silence')
                        continue
                    if kind == 'native_listening':
                        # 原厂已开放监听；不声称检测到唤醒，也不追加 TTS 或重置收音。
                        history.clear()
                        pending_plan = None
                        logger.info('VOICE_WINDOW_OPEN 30s after native listening')
                        continue
                    if bridge:
                        bridge.busy = True
                    microphone.idle(generation)
                    context.cancelled.clear()
                    turn_started = time.monotonic()
                    logger.info('VOICE_PROCESSING')
                    try:
                        vocabulary_words = vocabulary(skill_context)
                        with measure('ASR识别'):
                            text = transcribe_wav(encode_asr_wav(audio, sample_rate=16000), config,
                                                  vocabulary=vocabulary_words)
                        check_cancel()
                        if not text.strip():
                            logger.info('ASR_NO_SPEECH')
                            # 空识别不进入规划、不发声，也不为噪声重新续满30秒。
                            microphone.listen(generation, reset_deadline=False)
                            continue
                        logger.info('ASR %s', text)
                        if bridge:
                            bridge.publish_asr(text)
                        if text.strip('。！？!? ') in {'退出助手', '退出语音助手'}:
                            speak('语音助手已退出。')
                            return
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
                            check_cancel()
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
                            interaction = exclusive_interaction(proposal, registry)
                            if interaction:
                                generation = microphone.suspend_interaction(generation)
                            try:
                                with measure('任务执行与反馈'):
                                    results = execute(proposal, registry, context, speak, checkpoint=checkpoint,
                                                      cleanup_checkpoint=cleanup_checkpoint)
                            except CleanupUnconfirmed as error:
                                if not interaction:
                                    raise
                                generation = release_interaction(interaction, generation, recover=True)
                                raise RuntimeError('独占技能异常，资源已恢复，请重新发出指令。') from error
                            except BaseException:
                                if interaction:
                                    generation = release_interaction(interaction, generation)
                                raise
                            if interaction:
                                generation = release_interaction(interaction, generation)
                            executed = True
                            history.clear()
                            check_cancel()
                            if any(result.get('error_code') == 'network_unavailable' for result in results.values()):
                                raise ProviderNetworkError('技能网络连接失败')
                            if interaction:
                                # 整局已结束，不能让旧启动请求经模型继续安排其他技能。
                                break
                            with measure('Agent结果回传'):
                                proposal = continue_plan(proposal, results, registry)
                        if pending_plan is not None:
                            continue
                        if executed:
                            # 任务结束且资源核验通过后，以一条新提问交还宿主监听。
                            ask('接下来，你还想让我帮你做些什么呢？', generation)
                        else:
                            history.extend([{'role': 'user', 'content': text},
                                            {'role': 'assistant', 'content': proposal['reply']}])
                            history = history[-6:]
                            ask(proposal['reply'] or '请告诉我你想做什么。', generation)
                    except CleanupUnconfirmed:
                        logger.error('VOICE_CLEANUP_UNCONFIRMED 已停止接收新任务，设备资源释放未确认。')
                        raise
                    except ProviderNetworkError as error:
                        check_cancel()
                        # execute 仅在清理完成后传播原异常；隔离检查仍须先于恢复播报。
                        registry.check_resources(context, {'speaker'})
                        audio_output.ensure_idle()
                        context.cancelled.clear()
                        logger.error('TURN_NETWORK_ERROR %s', type(error).__name__)
                        history.clear()
                        pending_plan = None
                        microphone.idle(generation)
                        recover_prompt(NETWORK_ERROR_PROMPT, generation)
                    except Exception as error:
                        check_cancel()
                        # execute 已等待本轮清理；先确认资源可用，再解除本轮取消，允许恢复播报。
                        registry.check_resources(context, {'speaker'})
                        audio_output.ensure_idle()
                        context.cancelled.clear()
                        logger.error('TURN_ERROR %s', type(error).__name__)
                        if isinstance(error, AudioError):
                            logger.error('AUDIO_ERROR %s', str(error))
                        history.clear()
                        pending_plan = None
                        microphone.idle(generation)
                        recover_prompt('本轮未能完整完成，执行结果请以实际状态为准。请重新告诉我你的要求。', generation)
                    logger.info('TURN_SECONDS %s', round(time.monotonic() - turn_started, 3))
                    if microphone.phase == 'listening':
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
                        audio_output.close()
