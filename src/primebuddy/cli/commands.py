"""平台命令入口；启动、技能管理和开发模拟各自独立。"""
import argparse
from contextlib import ExitStack
import json
import logging
from pathlib import Path
import sys

from primebuddy import __version__
from primebuddy.core.engine import Context, execute
from primebuddy.skills.registry import Registry
from primebuddy.config.storage import SetupError, skill_config

logger = logging.getLogger(__name__)


def dispatch_command(argv=None):
    parser = argparse.ArgumentParser(prog='primebuddy', description='PrimeBuddy：通过独立技能扩展能力的机器人平台',
        epilog='日常使用：primebuddy start；首次配置：primebuddy onboard；查看子命令参数：<命令> --help')
    parser.add_argument('--version', action='version', version='PrimeBuddy ' + __version__)
    commands = parser.add_subparsers(dest='command', metavar='命令')
    commands.add_parser('onboard', help='配置模型服务，安装或配置随包技能')
    start = commands.add_parser('start', help='启动平台，当前使用 T1 语音入口',
        description='启动 T1 平台会话并执行已启用技能。接管原厂 Agent 为 only_voice 并重启，Ctrl+C 退出时恢复原模式。')
    start.add_argument('--sdk-bridge', choices=('namespaced', 'native', 'off'), default='namespaced',
        help='ROS ASR/TTS 兼容入口：namespaced 使用 /primebuddy（默认）；native 使用原厂名称，冲突则退出；off 关闭')
    start.add_argument('--asr-model', help='仅覆盖本次会话的 ASR provider:model；不指定时读取 config.yaml，失败不自动回退')
    simulate = commands.add_parser('simulate', help='开发模拟：校验计划或 Agent 工具循环',
        description='--plan 不调用 LLM；--text 使用云端 Agent。模拟不执行固定脚本或设备控制。')
    simulate.add_argument('--skills-dir', type=Path, help='开发技能集合目录；不指定时使用已安装、已就绪且已启用的技能')
    source = simulate.add_mutually_exclusive_group(required=True)
    source.add_argument('--plan', type=Path, help='读取本地 JSON 计划')
    source.add_argument('--text', help='将文字交给 LLM 生成计划，再模拟执行')
    group = commands.add_parser('skill', help='安装、配置和查看技能')
    actions = group.add_subparsers(dest='action', required=True)
    actions.add_parser('list', help='查看技能名称、版本、状态和安装目录')
    actions.add_parser('catalog', help='查看提供给 LLM 的就绪能力目录（JSON）')
    install = actions.add_parser('install', help='从目录、RPK、ZIP 或 Git 仓库安装')
    install.add_argument('source', help='本地技能目录、本地/HTTPS RPK 或 ZIP，或 HTTPS/SSH Git 地址')
    install.add_argument('--ref', help='Git 标签、分支或提交')
    install.add_argument('--no-configure', action='store_true', help='仅安装文件和依赖，标记为待配置')
    install.add_argument('--replace', action='store_true', help='配置成功后替换已有版本')
    for name, help_text in (('configure', '重新进入技能自己的配置向导'),
                            ('enable', '启用已经完成配置的技能'), ('disable', '停用技能，保留代码与配置')):
        action = actions.add_parser(name, help=help_text)
        action.add_argument('name', help='技能 ID，例如 diangetai')
    tts = commands.add_parser('tts', help='语音合成维护工具')
    warmup = tts.add_subparsers(dest='action', required=True).add_parser('warmup',
        help='预生成常用提示音缓存，不播放或接管机器人；可能产生云端调用',
        description='生成常用提示音及额外文本的缓存；缓存未命中时调用云端 TTS，不播放或接管机器人。')
    warmup.add_argument('--text', action='append', default=[], help='额外缓存的提示语（1～100字），可重复指定')
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return
    if args.command == 'onboard':
        from primebuddy.cli.onboarding import onboard
        from primebuddy.cli.wizard import Wizard
        onboard(Wizard())
        return
    if args.command == 'skill':
        manage_skill(args)
        return
    if args.command == 'tts':
        from primebuddy.voice.speech_cache import SpeechCache, COMMON_PROMPTS
        from primebuddy.config.runtime import private_runtime
        speech = SpeechCache(private_runtime() / 'tts-cache')
        for text in dict.fromkeys([*COMMON_PROMPTS, *args.text]):
            speech.prepare(text)
        print('TTS_WARMUP_READY')
        return

    # start 也发现用户复制到技能目录的 SKILL.md；显式目录用于开发模拟。
    skills_dir = args.skills_dir if args.command == 'simulate' else None
    registry = Registry(skills_dir)
    configs = {} if skills_dir else {name: skill_config(name) for name in registry.skills}
    context = Context(skill_configs=configs, simulate=args.command == 'simulate')
    if args.command == 'start':
        from primebuddy.voice.session import serve
        context.asr_model = args.asr_model
        logger.info('启动 T1 平台：预检通过后接管原厂 Agent 为 only_voice 并重启；Ctrl+C 退出并恢复原模式。')
        serve(registry, context, sdk_bridge=args.sdk_bridge)
        return
    if args.text is not None:
        from primebuddy.core.planner import plan
        proposal = plan(args.text, registry, context=context)
    else:
        try:
            proposal = json.loads(args.plan.read_text())
        except (OSError, ValueError):
            raise SetupError('无法读取 JSON 计划，请检查 --plan 的文件路径和内容。') from None
    from primebuddy.core.planner import continue_plan
    while True:
        print('PLAN', json.dumps(proposal, ensure_ascii=False))
        result = execute(proposal, registry, context,
                         emit=lambda event: print('EVENT', json.dumps(event, ensure_ascii=False)))
        print('RESULT', json.dumps(result, ensure_ascii=False))
        if not args.text or not proposal['tasks']:
            break
        proposal = continue_plan(proposal, result, registry)


def manage_skill(args):
    from primebuddy.skills import manager as skills
    from primebuddy.cli.wizard import Wizard
    if args.action == 'list':
        index = skills.discover_skill_records()
        if not index:
            print('尚未安装技能。使用 primebuddy skill install <RPK、ZIP 或 Git 地址>。')
        for name, record in sorted(index.items()):
            metadata = skills.load_skill_metadata(record['root'])
            missing = Registry.missing_requirements(metadata)
            state = '待配置' if not record['ready'] else ('已启用' if record['enabled'] else '已停用')
            if missing:
                state += ' · 缺少前置要求：' + '、'.join(missing)
            print(f"{name}  {record['version']}  {state}\n  目录：{record['root']}")
    elif args.action == 'catalog':
        print(json.dumps(Registry().catalog(), ensure_ascii=False, indent=2))
    elif args.action == 'install':
        result = skills.install(args.source, None if args.no_configure else Wizard, args.ref, args.replace)
        print('技能安装完成，已启用。' if result['ready'] else '文件已安装，尚未启用；请运行 primebuddy skill configure <技能名>。')
    elif args.action == 'configure':
        ready = skills.configure(args.name, Wizard)
        print('配置完成，已启用。' if ready else '配置未完成，可稍后继续。')
    else:
        skills.set_enabled(args.name, args.action == 'enable')
        print('已启用。' if args.action == 'enable' else '已停用。')


def main():
    running = sys.argv[1:2] == ['start']
    with ExitStack() as stack:
        try:
            if running:
                from primebuddy.config.logging_setup import runtime_log
                stack.enter_context(runtime_log())
            dispatch_command()
            return 0
        except (KeyboardInterrupt, EOFError):
            if running:
                logger.info('操作已取消。')
            else:
                print('操作已取消。', file=sys.stderr)
            return 130
        except SetupError as error:
            if running and logger.hasHandlers():
                logger.error('%s', error)
            else:
                print(str(error), file=sys.stderr)
            return 1
        except Exception as error:
            message = '操作未完成：' + type(error).__name__ + '。请检查输入、配置和运行环境。'
            if running and logger.hasHandlers():
                logger.error('%s', message)
            else:
                print(message, file=sys.stderr)
            return 1

if __name__ == '__main__':
    raise SystemExit(main())
