"""RPK / ZIP / Git 获取、校验、依赖和安装状态；不包含任何技能业务分支。"""
from contextlib import contextmanager
import fcntl
import importlib.util
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile

from primebuddy.config.storage import SetupError, get_data_directory, config_path, private_dir, read_config, write_config, skill_name, skill_data, skill_config
from primebuddy.skills.metadata import load_skill_metadata, resolve_skill_file

MAX_PACKAGE = 100 * 1024 * 1024


@contextmanager
def installation_lock():
    with (private_dir(get_data_directory()) / 'install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('另一个安装或配置正在进行，请结束后重试。') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_skill_records():
    index = read_config(config_path('skills.yaml'))
    for record in index.values():
        for key in ('source', 'type', 'ref', 'commit', 'sha256'):
            record.pop(key, None)
    return index


def unpack(archive, directory):
    """拒绝目录穿越、链接和异常解压量，先检查完整清单再写文件。"""
    with zipfile.ZipFile(archive) as zipped:
        entries = zipped.infolist()
        if len(entries) > 10000 or sum(i.file_size for i in entries) > MAX_PACKAGE:
            raise SetupError('技能 ZIP 文件过多或解压后超过 100 MiB。')
        seen = set()
        for item in entries:
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (path.is_absolute() or '..' in path.parts or '\\' in item.filename
                    or ':' in item.filename or '\x00' in item.filename
                    or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or path in seen or '.git' in path.parts):
                raise SetupError('ZIP 包含重复路径、链接或不安全路径。')
            seen.add(path)
        zipped.extractall(directory)


def run_install_command(args, timeout=180, cwd=None):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout, text=True)
    except FileNotFoundError:
        raise SetupError('缺少安装工具：' + Path(args[0]).name) from None
    except subprocess.TimeoutExpired:
        raise SetupError('下载或依赖安装超时，请检查网络后重试。') from None
    if result.returncode:
        if 'No matching distribution found' in result.stderr:
            raise SetupError('当前软件源未找到兼容的依赖版本，请检查软件源访问、Python 版本和 ARM64 支持。')
        if 'No module named pip' in result.stderr:
            raise SetupError('当前 Python 环境缺少 pip，请重新运行框架 install.py。')
        raise SetupError('下载或依赖命令失败，请检查网络、仓库权限、Git ref 和依赖兼容性。')
    return result.stdout.strip()


def https_url(source):
    parts = urllib.parse.urlsplit(source)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment or parts.query:
        raise SetupError('请使用不含账号、令牌、查询参数的 HTTPS 地址。私有 Git 仓库请使用 SSH。')
    return parts


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        # 下载链接可有签名参数，但只允许 HTTPS，且不向下载站发送模型凭据。
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            raise SetupError('下载重定向不是有效的 HTTPS 地址。')
        return super().redirect_request(request, fp, code, message, headers, url)


def fetch_skill_package(source, target, ref=None):
    local = Path(source).expanduser()
    online_package = source.startswith('https://') and urllib.parse.urlsplit(source).path.lower().endswith(('.zip', '.rpk'))
    if local.is_dir() and not (local / '.git').exists():
        if ref:
            raise SetupError('--ref 只适用于 Git 仓库。')
        tree = target / 'source'
        shutil.copytree(local, tree, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    elif (local.is_file() and local.suffix.lower() in ('.zip', '.rpk')) or online_package:
        if ref:
            raise SetupError('--ref 只适用于 Git 仓库。')
        archive = local
        if online_package:
            https_url(source)
            archive = target / 'download.zip'
            size = 0
            with urllib.request.build_opener(HttpsRedirect).open(source, timeout=30) as response, archive.open('wb') as output:
                while chunk := response.read(65536):
                    size += len(chunk)
                    if size > MAX_PACKAGE:
                        raise SetupError('ZIP 下载超过 100 MiB。')
                    output.write(chunk)
        if archive.stat().st_size > MAX_PACKAGE:
            raise SetupError('ZIP 大小超过 100 MiB。')
        tree = target / 'source'
        unpack(archive, tree)
    else:
        if source.startswith('https://'):
            https_url(source)
        elif source.startswith('ssh://'):
            parts = urllib.parse.urlsplit(source)
            if not parts.hostname or parts.password or parts.query or parts.fragment:
                raise SetupError('SSH 仓库地址无效。')
        elif not re.fullmatch(r'git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+', source) and not (local.is_dir() and (local / '.git').exists()):
            raise SetupError('请提供本地/HTTPS RPK 或 ZIP，HTTPS/SSH Git 地址或本地 Git 仓库。')
        if ref and (ref.startswith('-') or any(c.isspace() for c in ref)):
            raise SetupError('Git ref 无效。')
        tree = target / 'source'
        # 禁止子模块和递归协议；不执行仓库脚本、hooks 或 shell 字符串。
        run_install_command(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--quiet', '--no-checkout', '--', source, str(tree)])
        try:
            revision = run_install_command(['git', 'rev-parse', '--verify', '--end-of-options', (ref or 'HEAD') + '^{commit}'], cwd=tree)
        except SetupError:
            if not ref:
                raise
            revision = run_install_command(['git', 'rev-parse', '--verify', '--end-of-options', 'refs/remotes/origin/' + ref + '^{commit}'], cwd=tree)
        run_install_command(['git', '-c', 'core.hooksPath=/dev/null', 'checkout', '--quiet', '--detach', revision], cwd=tree)
        shutil.rmtree(tree / '.git')
    if any(p.is_symlink() for p in tree.rglob('*')):
        raise SetupError('技能包不允许符号链接。')
    candidates = [tree] if (tree / 'SKILL.md').is_file() else [p for p in tree.iterdir() if p.is_dir() and (p / 'SKILL.md').is_file()]
    if len(candidates) != 1:
        raise SetupError('技能包根目录或唯一的一层子目录中须包含 SKILL.md。')
    return candidates[0]


def load_entry(root, relative):
    deps = root / '_deps'
    if deps.is_dir() and str(deps) not in sys.path:
        sys.path.insert(0, str(deps))
    entry = resolve_skill_file(root, relative)
    name = 'primebuddy_skill_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, entry, submodule_search_locations=[str(entry.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


def install_dependencies(root, dependencies):
    if dependencies:
        run_install_command([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                 '--retries', '1', '--timeout', '15', '--progress-bar', 'off',
                 '--target', str(root / '_deps'), '-r', str(root / 'requirements.txt')], timeout=300)


def run_setup(root, data, ui):
    name = data['name']
    if data.get('configure'):
        from primebuddy.skills.runtime import script_command, skill_environment
        environment = skill_environment(root, name)
        # Console 自己读取、保存配置；继承终端，不截获扫码或隐藏输入。
        status = subprocess.call(script_command(root, data['configure'][0], data['configure'][1:]),
                                 cwd=root, env=environment)
        if status:
            raise SetupError('技能 Console 配置未完成，退出码：' + str(status))
        return skill_config(name)
    if not data.get('setup'):
        return skill_config(name)
    if callable(ui):
        ui = ui()
    # 可选原生向导用于复用平台终端控件；普通 skill 不需要此函数。
    result = load_entry(root, data['setup']).setup(ui, skill_data(name), skill_config(name))
    if result is not None and not isinstance(result, dict):
        raise SetupError('技能安装引导须返回配置对象，或返回 None 表示稍后配置。')
    return result


def install(source, ui=None, ref=None, replace=False):
    report = ui.info if ui is not None and not callable(ui) else lambda message: print(message, flush=True)
    with installation_lock():
        index = load_skill_records()
        skills = private_dir(get_data_directory() / 'skills')
        with tempfile.TemporaryDirectory(prefix='.install-', dir=skills) as temp:
            report('正在获取技能文件…')
            root = fetch_skill_package(str(source), Path(temp), ref)
            data = load_skill_metadata(root)
            name = data['name']
            destination = skills / name
            old = index.get(name)
            exists = destination.exists()
            if destination.is_symlink() or (exists and (
                    not destination.is_dir() or load_skill_metadata(destination)['name'] != name)):
                raise SetupError('安装目录已被其他文件或技能占用：' + str(destination))
            if (old or exists) and not replace:
                raise SetupError(f'{name} 已安装。重新配置用 primebuddy skill configure {name}；更换版本需加 --replace。')
            if (root / '_deps').exists():
                raise SetupError('技能包不能自带 _deps；请在 requirements.txt 中声明依赖。')
            report('正在检查和安装 ' + name + ' 的依赖…')
            install_dependencies(root, data.get('dependencies', []))
            old_config = skill_config(name)
            previous = Path(temp) / 'previous'
            committed = placed = False
            try:
                # 在固定目录运行向导；旧代码暂存，配置或索引写入失败时一起回滚。
                if exists:
                    destination.rename(previous)
                root.rename(destination)
                placed = True
                interrupted = None
                try:
                    needs_setup = bool(data.get('setup') or data.get('configure'))
                    result = run_setup(destination, data, ui) if ui is not None or not needs_setup else None
                except BaseException as error:
                    if old or exists:
                        raise
                    # 首次扫码中断保留文件和依赖，下一次可直接继续配置。
                    result, interrupted = None, error
                if (old or exists) and result is None:
                    raise SetupError('新版本未完成配置，保留已有安装。')
                if result is not None:
                    write_config(config_path('config.yaml', skill=name), result)
                index[name] = {'path': name, 'version': data['version'], 'ready': result is not None,
                               'enabled': result is not None}
                write_config(config_path('skills.yaml'), index)
                committed = True
            except BaseException:
                write_config(config_path('config.yaml', skill=name), old_config)
                raise
            finally:
                if not committed:
                    if placed:
                        shutil.rmtree(destination)
                    if previous.exists():
                        previous.rename(destination)
            if interrupted is not None:
                raise interrupted
            return index[name]


def get_skill_package_directory(record):
    if 'directory' in record:
        path = Path(record['directory']).expanduser().resolve()
        if not path.is_dir():
            raise SetupError('技能目录不存在：' + str(path))
        return path
    root = get_data_directory() / 'skills'
    path = (root / record['path']).resolve()
    if path == root.resolve() or not path.is_relative_to(root.resolve()) or not path.is_dir():
        raise SetupError('技能安装记录的路径无效。')
    return path


def discover_skill_records():
    """安装索引用于版本管理；直接复制的 skill 同样进入目录。"""
    from primebuddy.config.storage import load_app_config, application_directory
    index = {name: dict(record, root=get_skill_package_directory(record))
             for name, record in load_skill_records().items()
             if 'directory' not in record or Path(record['directory']).expanduser().is_dir()}
    settings = load_app_config().get('skills', {})
    if not isinstance(settings, dict):
        raise SetupError('skills 配置须为键值对象。')
    directories = settings.get('directories', [])
    if not isinstance(directories, list) or any(not isinstance(path, str) for path in directories):
        raise SetupError('skills.directories 须为目录路径列表。')
    roots = [get_data_directory() / 'skills', application_directory() / '.agents/skills',
             *(Path(path).expanduser() for path in directories)]
    for root in roots:
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            if folder.name.startswith('.') or not folder.is_dir() or not (folder / 'SKILL.md').is_file():
                continue
            data = load_skill_metadata(folder)
            name = data['name']
            if name in index:
                if index[name]['root'].resolve() != folder.resolve():
                    raise SetupError('技能名称重复，请移除重复目录：' + name)
                continue
            configured = (not (data.get('setup') or data.get('configure'))
                          or config_path('config.yaml', skill=name).is_file())
            index[name] = {'root': folder.resolve(), 'version': data['version'],
                           'ready': configured, 'enabled': True}
    return index


def configure(name, ui):
    skill_name(name)
    with installation_lock():
        index = load_skill_records()
        discovered = discover_skill_records()
        if name not in discovered:
            raise SetupError('技能尚未安装：' + name)
        root = discovered[name]['root']
        if name not in index:
            index[name] = {key: value for key, value in discovered[name].items() if key != 'root'}
            index[name]['directory'] = str(root)
        # 配置期间先撤销就绪状态；中断不能保留虚假的成功标记。
        index[name].update(ready=False, enabled=False)
        write_config(config_path('skills.yaml'), index)
        result = run_setup(root, load_skill_metadata(root), ui)
        if result is not None:
            write_config(config_path('config.yaml', skill=name), result)
            index[name].update(ready=True, enabled=True)
            write_config(config_path('skills.yaml'), index)
        return result is not None


def set_enabled(name, enabled):
    skill_name(name)
    with installation_lock():
        index = load_skill_records()
        discovered = discover_skill_records()
        if name not in index and name in discovered:
            index[name] = {key: value for key, value in discovered[name].items() if key != 'root'}
            index[name]['directory'] = str(discovered[name]['root'])
        if name not in index:
            raise SetupError('技能尚未安装：' + name)
        if enabled and not index[name]['ready']:
            raise SetupError('技能尚未配置完成，请先运行 primebuddy skill configure ' + name)
        index[name]['enabled'] = enabled
        write_config(config_path('skills.yaml'), index)
