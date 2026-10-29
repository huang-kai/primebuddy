"""从固定官方 Release 更新框架与已安装技能；准备完成后统一提交。"""
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from primebuddy import __version__
from primebuddy.config.compatibility import check_skill, load_release, validate_release, version_tuple, release_url
from primebuddy.config.storage import SetupError, get_data_directory, read_config, write_config
from primebuddy.skills.manager import (discover_skill_records, installation_lock, install_dependencies,
                                       load_skill_metadata, unpack, HttpsRedirect, MAX_PACKAGE)
from primebuddy.tools import update_transaction as transaction

REPOSITORY = 'huang-kai/primebuddy'


def pending_update(home=None):
    return ((home or get_data_directory()) / '.update/transaction.json').exists()


def require_no_update(home=None):
    if pending_update(home):
        raise SetupError('存在未完成升级，请先运行 primebuddy update --recover。')


def fetch_bytes(url, limit, *, api=False):
    headers = {'User-Agent': 'PrimeBuddy/' + __version__}
    if api:
        headers.update(Accept='application/vnd.github+json', **{'X-GitHub-Api-Version': '2026-03-10'})
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.build_opener(HttpsRedirect).open(request, timeout=30) as response:
            value = response.read(limit + 1)
        if len(value) > limit:
            raise SetupError('发布文件超过允许大小。')
        return value
    except (urllib.error.URLError, OSError):
        raise SetupError('无法读取官方发布，请检查网络、GitHub 访问限制及目标版本是否已发布。') from None


def fetch_release(version=None):
    if version is not None:
        version_tuple(version)
    suffix = 'latest' if version is None else 'tags/v' + version
    try:
        release = json.loads(fetch_bytes('https://api.github.com/repos/' + REPOSITORY + '/releases/' + suffix,
                                         1024 * 1024, api=True))
        tag = release['tag_name']
        if release.get('draft') or release.get('prerelease') or not isinstance(tag, str) or not tag.startswith('v'):
            raise ValueError()
        target = tag[1:]
        version_tuple(target)
        if version is not None and target != version:
            raise ValueError()
        assets = [asset for asset in release['assets'] if asset['name'] == 'release.json']
        if not assets:
            raise SetupError('该版本尚未提供自动升级清单，请使用 Release 安装包；旧版 1.0.0 不支持自动升级。')
        if len(assets) != 1 or assets[0]['browser_download_url'] != release_url(target, 'release.json'):
            raise ValueError()
        raw = fetch_bytes(assets[0]['browser_download_url'], 65536)
        digest = assets[0].get('digest')
        if digest and digest != 'sha256:' + hashlib.sha256(raw).hexdigest():
            raise SetupError('发布清单校验失败，未更改安装。')
        manifest = validate_release(json.loads(raw))
        if (manifest['version'] != target or 'framework' not in manifest
                or any('artifact' not in rule for rule in manifest['skills'].values())):
            raise ValueError()
        return manifest
    except SetupError:
        raise
    except (ValueError, KeyError, TypeError):
        raise SetupError('官方发布清单无效或与目标版本不一致，未更改安装。') from None


def download_artifact(artifact, destination):
    value = fetch_bytes(artifact['url'], MAX_PACKAGE)
    if hashlib.sha256(value).hexdigest() != artifact['sha256']:
        raise SetupError('发布包 SHA-256 校验失败，未更改安装：' + artifact['filename'])
    destination.write_bytes(value)


def installed_version(home):
    path = home / 'release.json'
    if path.exists():
        return load_release(path)['version']
    try:
        content = (home / 'src/primebuddy/__init__.py').read_text()
    except OSError:
        raise SetupError('未发现完整安装，请先运行新安装包的 scripts/install.sh。') from None
    match = re.search(r"(?m)^__version__\s*=\s*['\"](\d+\.\d+\.\d+)['\"]", content)
    if not match:
        raise SetupError('无法确认已安装框架版本，请检查安装目录。')
    return match[1]


def skill_updates(home, manifest):
    updates = []
    for name, record in discover_skill_records(read_only=True).items():
        spec = manifest['skills'].get(name)
        if spec is None:
            continue
        root = record['root']
        data = load_skill_metadata(root)
        current, target = data['version'], spec['recommended_version']
        try:
            check_skill(data, manifest)
            compatible = True
        except SetupError:
            compatible = False
        if compatible:
            # 推荐版本用于新安装，不代表兼容的现有技能必须随平台更新。
            continue
        managed = root == home / 'skills' / name and not root.is_symlink() and 'directory' not in record
        if not managed:
            if not compatible and record['enabled']:
                raise SetupError('外部技能 ' + name + ' ' + current + ' 不兼容；请自行更新或禁用，升级程序不会覆盖该目录。')
            continue
        if version_tuple(current) > version_tuple(target):
            if not compatible:
                raise SetupError(name + ' 已安装版本 ' + current + ' 超出兼容范围，升级不会自动降级。')
            continue
        if current != target:
            updates.append(dict(name=name, current=current, target=target, root=root, record=record))
    return updates


def prepare_skills(source, stage, manifest, updates, *, online=False):
    if not updates:
        return []
    home = get_data_directory()
    index_path = home / 'cfg/skills.yaml'
    index = read_config(index_path if index_path.exists() else home / 'skills.yaml')
    for item in updates:
        name = item['name']
        artifact = manifest['skills'][name].get('artifact')
        if artifact is None:
            raise SetupError('安装包缺少 ' + name + ' 的升级清单，请使用包含配套技能的完整 Release 包。')
        package = source / 'resource/bundled' / artifact['filename']
        with tempfile.TemporaryDirectory(prefix='skill-update-', dir=stage.parent) as temp:
            temporary = Path(temp)
            if package.is_file():
                if hashlib.sha256(package.read_bytes()).hexdigest() != artifact['sha256']:
                    raise SetupError('随包技能校验失败：' + name)
            elif online:
                package = temporary / artifact['filename']
                download_artifact(artifact, package)
            else:
                raise SetupError('缺少配套技能 ' + name + '，请下载完整 Release 安装包后重试。')
            root = temporary / 'source'
            unpack(package, root)
            data = load_skill_metadata(root)
            if (data['name'], data['version']) != (name, item['target']):
                raise SetupError('技能包名称或版本与发布清单不一致。')
            check_skill(data, manifest)
            if (root / '_deps').exists():
                raise SetupError('技能发布包不能自带 _deps。')
            install_dependencies(root, data['dependencies'])
            destination = stage / 'skills' / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root), destination)
        record = {key: value for key, value in item['record'].items() if key != 'root'}
        record.update(path=name, version=item['target'])
        index[name] = record
    write_config(stage / 'cfg/skills.yaml', index)
    return ['skills/' + item['name'] for item in updates] + ['cfg/skills.yaml']


def prepare_transaction(home, stage, names):
    require_no_update(home)
    names = list(dict.fromkeys(names))
    if 'bin/primebuddy' in names:
        names.remove('bin/primebuddy')
        names.insert(0, 'bin/primebuddy')
    entries = []
    for name in names:
        target = transaction.target_path(home, name)
        source = stage / name
        if not source.exists() or source.is_symlink() or any(p.is_symlink() for p in source.rglob('*')):
            raise SetupError('升级暂存文件缺失或包含符号链接。')
        if target.exists() and target.is_dir() != source.is_dir():
            raise SetupError('升级目标类型不匹配：' + name)
        entries.append(dict(path=name, had_original=target.exists(), state='pending'))
    directory = home / '.update'
    if directory.is_symlink():
        raise SetupError('升级目录不能是符号链接。')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    if (directory / 'runner.py').is_symlink():
        raise SetupError('升级恢复执行器不能是符号链接。')
    # 只有无事务时才能清理上次尚未进入提交阶段的暂存内容。
    for name in ('new', 'old'):
        transaction.remove(directory / name)
    shutil.copy2(Path(transaction.__file__), directory / 'runner.py')
    (directory / 'runner.py').chmod(0o700)
    shutil.move(str(stage), directory / 'new')
    (directory / 'old').mkdir()
    for path in [directory / 'runner.py', *(p for p in (directory / 'new').rglob('*') if p.is_file())]:
        with path.open('rb') as stream:
            os.fsync(stream.fileno())
    for path in sorted((p for p in (directory / 'new').rglob('*') if p.is_dir()),
                       key=lambda p: len(p.parts), reverse=True):
        transaction.sync_directory(path)
    for path in (directory / 'new', directory / 'old', directory, home):
        transaction.sync_directory(path)
    transaction.save(directory / 'transaction.json', dict(schema_version=1, phase='applying', entries=entries))


def commit_update(home, stage, names, *, lock_fds=()):
    prepare_transaction(home, stage, names)
    import shlex
    print('升级中断时可独立恢复：' + shlex.join([
        sys.executable, str(home / '.update/runner.py'), '--recover', str(home)]), flush=True)
    result = subprocess.run([sys.executable, str(home / '.update/runner.py'), str(home)], pass_fds=lock_fds)
    if result.returncode:
        raise SetupError('升级未完成；已恢复的组件保持原版本，如仍有升级记录请运行 primebuddy update --recover。')


def require_idle_runtime(runtime):
    from primebuddy.skills.processes import process_cleanup_confirmed
    if ((runtime / 'agent_mode.json').exists() or any(runtime.glob('*/cleanup-unconfirmed.json'))
            or not process_cleanup_confirmed(runtime)):
        raise SetupError('存在未确认的运行或恢复记录，请先完成原会话恢复再升级。')


def update_lock(stack, home):
    install_lock = stack.enter_context(installation_lock())
    require_no_update(home)
    from primebuddy.config.runtime import runtime_directory
    runtime = runtime_directory()
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = stack.enter_context((runtime / 'audio.lock').open('a'))
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SetupError('PrimeBuddy 正在运行，请先正常退出会话再升级。') from None
    require_idle_runtime(runtime)
    return (install_lock.fileno(), lock.fileno())


def run_update(*, check=False, version=None, recover=False):
    home = get_data_directory()
    if recover:
        runner = home / '.update/runner.py'
        if not pending_update(home):
            print('没有需要恢复的升级。')
            return
        if not runner.is_file() or runner.is_symlink():
            raise SetupError('升级恢复执行器缺失，请保留 .update 目录并使用新安装包恢复。')
        result = subprocess.run([sys.executable, str(runner), '--recover', str(home)])
        if result.returncode:
            raise SetupError('升级恢复未完成，请保留升级目录进行检查。')
        return
    require_no_update(home)
    manifest = fetch_release(version)
    current = installed_version(home)
    if version_tuple(manifest['version']) < version_tuple(current):
        raise SetupError('目标版本低于当前版本，update 不执行降级。')
    updates = skill_updates(home, manifest)
    print('PrimeBuddy：' + current + ' → ' + manifest['version'])
    for item in updates:
        print(item['name'] + '：' + item['current'] + ' → ' + item['target']
              + ('（保持停用）' if not item['record']['enabled'] else ''))
    if check:
        print('检查完成，尚未更改安装。' if updates or current != manifest['version'] else '当前版本及技能已兼容。')
        return
    if current == manifest['version'] and not updates:
        print('当前版本及技能已兼容，无需升级。')
        return
    with ExitStack() as stack:
        lock_fds = update_lock(stack, home)
        # 持锁后重读，防止预览与提交间的安装状态变化。
        current = installed_version(home)
        if version_tuple(manifest['version']) < version_tuple(current):
            raise SetupError('当前版本已变化，update 不执行降级；请重新检查发布。')
        updates = skill_updates(home, manifest)
        if current == manifest['version'] and not updates:
            print('当前版本及技能已兼容，无需升级。')
            return
        temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.update-download-', dir=home)))
        archive = temporary / 'framework.rpk'
        if 'framework' not in manifest:
            raise SetupError('发布清单缺少框架安装包。')
        download_artifact(manifest['framework'], archive)
        unpack(archive, temporary / 'unpacked')
        source = temporary / 'unpacked/primebuddy'
        embedded = load_release(source / 'release.json')
        if (embedded['version'], embedded['skills'], embedded.get('bundled_skills')) != (
                manifest['version'], manifest['skills'], manifest.get('bundled_skills')):
            raise SetupError('框架包内兼容清单与发布清单不一致。')
        from primebuddy.tools.install import stage_application, prepare_dependencies, stage_launcher
        stage = temporary / 'stage'
        names = stage_application(source, stage)
        prepare_dependencies(stage)
        if (stage / '_deps').exists():
            names.append('_deps')
        names.extend(stage_launcher(stage, home))
        names.extend(prepare_skills(source, stage, manifest, updates, online=True))
        commit_update(home, stage, names, lock_fds=lock_fds)
    print('升级完成：PrimeBuddy ' + manifest['version'] + '。配置、登录数据和技能启用状态已保留。')
