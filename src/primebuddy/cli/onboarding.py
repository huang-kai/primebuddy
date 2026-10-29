"""框架主持向导；随包技能也走普通 RPK / ZIP 安装路径。"""
import re
import subprocess
import zipfile

from primebuddy.config.models import configure_model
from primebuddy.config.storage import SetupError, application_directory, parse_config
from primebuddy.skills.metadata import parse_skill_document
from primebuddy.skills.manager import install, configure, load_skill_records, discover_skill_records


def bundled_skills():
    # RPK 内容为 ZIP；保留旧 ZIP 包兼容，源码中不保留第二份业务实现。
    bundles = {}
    for archive in sorted((application_directory() / 'resource/bundled').glob('*')):
        if archive.suffix.lower() not in ('.rpk', '.zip'):
            continue
        try:
            with zipfile.ZipFile(archive) as package:
                with package.open('SKILL.md') as source:
                    raw = source.read(65537).decode('utf-8')
                metadata, _ = parse_skill_document(raw, archive)
                if 'cfg/pkg.yaml' in package.namelist():
                    with package.open('cfg/pkg.yaml') as source:
                        raw = source.read(16385)
                    if len(raw) > 16384:
                        raise ValueError('清单过大')
                    data = parse_config(raw, archive)
                else:
                    data = {}
            name = metadata['name']
            version = data.get('version', metadata.get('metadata', {}).get('version', '0.0.0'))
            if data.get('package_name', name) != name or not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
                raise ValueError('版本格式无效')
        except (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile):
            raise SetupError('随包技能清单无效：' + archive.name) from None
        if name in bundles:
            raise SetupError('随包技能名称重复：' + name)
        bundles[name] = {'path': archive.resolve(), 'version': version}
    return bundles


def configure_robot(ui):
    from primebuddy.integrations.primebot.ssh_setup import local_identity, setup_voice_ssh
    try:
        identity = local_identity()
        if identity is None:
            return
        from primebuddy.integrations.primebot.device import audio_host
        from primebuddy.integrations.primebot.native_agent import agent_rpc
        from primebuddy.config.runtime import private_runtime
        ui.header('机器人连接', '自动连接原厂语音服务')
        host = audio_host()
        if host == 'local':
            ui.info('使用本板原厂语音服务，无需配置跨板 SSH。')
        else:
            setup_voice_ssh(host, identity, ui.info)
        state = agent_rpc(private_runtime(), 'state')
        ui.info('✓ 原厂 Agent 状态查询通过：' + str(state['status']))
        if not str(state['status']).startswith('Running('):
            ui.info('原厂 Agent 当前未运行；请恢复原厂 Agent 后再启动 Buddy。')
    except subprocess.SubprocessError:
        raise SetupError('机器人连接超时或验证失败；已保留安装文件，运行 primebuddy onboard 可重试。') from None
    except (RuntimeError, OSError, ValueError) as error:
        raise SetupError('机器人连接尚未完成：' + str(error)
                         + '。已保留安装文件，运行 primebuddy onboard 可继续配置。') from None


def onboard(ui):
    from primebuddy import __version__
    ui.header('PrimeBuddy ' + __version__, '平台配置 · 机器人连接 → 模型服务 → 技能配置')
    ui.info('方向键移动，空格多选，回车确认；Ctrl+C 可取消并保留已完成步骤。')
    configure_robot(ui)
    ui.header('1 / 2 · 模型服务')
    if ui.confirm('配置模型服务？', default=True):
        configure_model(ui)
    ui.header('2 / 2 · 安装与配置技能', '技能自行提供配置步骤，使用相同的交互界面')
    index = load_skill_records()
    actual = discover_skill_records()
    for name, record in index.items():
        if name in actual:
            record['version'] = actual[name]['version']
    bundles = bundled_skills()
    updates = {name for name in bundles if name in index
               and tuple(map(int, bundles[name]['version'].split('.')))
               > tuple(map(int, index[name]['version'].split('.')))}
    names = list(dict.fromkeys([*bundles, *index]))
    labels = []
    for name in names:
        if name in updates:
            suffix = ' · 更新至 ' + bundles[name]['version']
        elif name in index:
            suffix = ' · 重新配置' if index[name]['ready'] else ' · 继续配置'
        else:
            suffix = ' · 官方随包'
        labels.append(name + suffix)
    ui.info('技能均为可选。可以全不选，仅安装平台；未选中的已有技能保持原样。')
    chosen = ui.multiselect('选择本次要安装或配置的技能（可全不选）', labels, default=[]) if names else []
    for position in chosen:
        name = names[position]
        ui.header(name + ' · 配置引导')
        try:
            if name in index and name not in updates:
                ready = configure(name, ui)
            else:
                ready = install(str(bundles[name]['path']), ui, replace=name in index)['ready']
            ui.info('技能已就绪。' if ready else '文件已安装，配置待完成。')
        except SetupError as error:
            ui.info(str(error))
    ui.info('向导结束。使用 primebuddy skill list 查看状态，primebuddy start 启动平台；primebuddy onboard 可再次配置。')
