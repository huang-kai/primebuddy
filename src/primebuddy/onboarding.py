"""框架主持向导；随包技能也走普通 ZIP 安装路径。"""
import json
from pathlib import Path
import re
import zipfile

from .models import configure_model
from .settings import SetupError, skill_name
from .skill_manager import install, configure, records


def bundled_skills():
    # 发布时将独立技能打包成 ZIP 放在此目录，源码中不保留第二份业务实现。
    bundles = {}
    for archive in sorted((Path(__file__).resolve().parents[2] / 'bundled').glob('*.zip')):
        try:
            with zipfile.ZipFile(archive) as package:
                with package.open('manifest.json') as source:
                    raw = source.read(16385)
            if len(raw) > 16384:
                raise ValueError('清单过大')
            data = json.loads(raw)
            name = skill_name(data['name'])
            if not re.fullmatch(r'\d+\.\d+\.\d+', data['version']):
                raise ValueError('版本格式无效')
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
            raise SetupError('随包技能清单无效：' + archive.name) from None
        if name in bundles:
            raise SetupError('随包技能名称重复：' + name)
        bundles[name] = {'path': archive, 'version': data['version']}
    return bundles


def onboard(ui):
    from . import __version__
    ui.header('PrimeBuddy ' + __version__, '平台配置 · 模型服务 → 技能选择 → 技能配置')
    ui.info('方向键移动，空格多选，回车确认；Ctrl+C 可取消并保留已完成步骤。')
    ui.header('1 / 2 · 模型服务')
    if ui.confirm('配置模型服务？', default=True):
        configure_model(ui)
    ui.header('2 / 2 · 安装与配置技能', '技能自行提供配置步骤，使用相同的交互界面')
    index = records()
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
    defaults = [i for i, name in enumerate(names) if name in updates or name not in index or not index[name]['ready']]
    chosen = ui.multiselect('选择本次要安装或配置的技能', labels, default=defaults) if names else []
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
