"""ZIP / Git 获取、校验、依赖和安装状态；不包含任何技能业务分支。"""
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
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

from .settings import SetupError, home, private_dir, read_json, write_json, skill_name, skill_data, skill_config

MAX_PACKAGE = 100 * 1024 * 1024


@contextmanager
def installation_lock():
    with (private_dir(home()) / 'install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('另一个安装或配置正在进行，请结束后重试。') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def records():
    return read_json(home() / 'skills.json')


def checked_file(root, relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative
            or PurePosixPath(relative).is_absolute() or '..' in PurePosixPath(relative).parts):
        raise SetupError('技能文件路径无效。')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise SetupError('技能入口缺失或超出技能目录。')
    return path


def manifest(root):
    root = Path(root)
    try:
        data = json.loads((root / 'manifest.json').read_text())
        skill_name(data['name'])
        if type(data['api_version']) is not int or data['api_version'] != 1 or not re.fullmatch(r'\d+\.\d+\.\d+', data['version']):
            raise SetupError('技能版本或 api_version 不兼容；当前接口版本为 1。')
        for field in ('entrypoint', 'setup'):
            checked_file(root, data[field])
        checked_file(root, 'SKILL.md')
        if not isinstance(data['description'], str) or not data['description'].strip():
            raise SetupError('技能缺少能力说明。')
        actions = data['actions']
        if not isinstance(actions, dict) or not actions:
            raise SetupError('技能必须声明动作。')
        for name, action in actions.items():
            skill_name(name)
            if not isinstance(action['resources'], list) or any(not isinstance(r, str) or not r for r in action['resources']):
                raise SetupError('技能资源声明无效。')
            for name, field in action['parameters'].items():
                skill_name(name)
                if field['type'] not in ('string', 'boolean', 'integer'):
                    raise SetupError('不支持的技能参数类型。')
                if 'enum' in field and (not isinstance(field['enum'], list) or not field['enum']):
                    raise SetupError('技能枚举声明无效。')
        dependencies = data.get('dependencies', [])
        if not isinstance(dependencies, list) or any(not isinstance(d, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9_.+-]+', d) for d in dependencies):
            raise SetupError('技能 dependencies 须使用包名==固定版本，不接受安装选项或 URL。')
        return data
    except (KeyError, TypeError, AttributeError, ValueError, OSError) as error:
        if isinstance(error, SetupError):
            raise
        raise SetupError('技能 manifest.json 缺失或格式不正确。') from None


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


def command(args, timeout=180, cwd=None):
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


def fetch(source, target, ref=None):
    local = Path(source).expanduser()
    online_zip = source.startswith('https://') and urllib.parse.urlsplit(source).path.lower().endswith('.zip')
    if (local.is_file() and local.suffix.lower() == '.zip') or online_zip:
        if ref:
            raise SetupError('--ref 只适用于 Git 仓库。')
        archive = local
        if online_zip:
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
        fingerprint = hashlib.sha256(archive.read_bytes()).hexdigest()
        tree = target / 'source'
        unpack(archive, tree)
        origin = {'type': 'zip', 'source': source if online_zip else str(local.resolve()), 'sha256': fingerprint}
    else:
        if source.startswith('https://'):
            https_url(source)
        elif source.startswith('ssh://'):
            parts = urllib.parse.urlsplit(source)
            if not parts.hostname or parts.password or parts.query or parts.fragment:
                raise SetupError('SSH 仓库地址无效。')
        elif not re.fullmatch(r'git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+', source) and not (local.is_dir() and (local / '.git').exists()):
            raise SetupError('来源须为本地/HTTPS ZIP，HTTPS/SSH Git 地址或本地 Git 仓库。')
        if ref and (ref.startswith('-') or any(c.isspace() for c in ref)):
            raise SetupError('Git ref 无效。')
        tree = target / 'source'
        # 禁止子模块和递归协议；不执行仓库脚本、hooks 或 shell 字符串。
        command(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--quiet', '--no-checkout', '--', source, str(tree)])
        try:
            revision = command(['git', 'rev-parse', '--verify', '--end-of-options', (ref or 'HEAD') + '^{commit}'], cwd=tree)
        except SetupError:
            if not ref:
                raise
            revision = command(['git', 'rev-parse', '--verify', '--end-of-options', 'refs/remotes/origin/' + ref + '^{commit}'], cwd=tree)
        command(['git', '-c', 'core.hooksPath=/dev/null', 'checkout', '--quiet', '--detach', revision], cwd=tree)
        shutil.rmtree(tree / '.git')
        origin = {'type': 'git', 'source': str(local.resolve()) if local.is_dir() else source, 'ref': ref or 'HEAD', 'commit': revision}
    if any(p.is_symlink() for p in tree.rglob('*')):
        raise SetupError('技能包不允许符号链接。')
    candidates = [tree] if (tree / 'manifest.json').is_file() else [p for p in tree.iterdir() if p.is_dir() and (p / 'manifest.json').is_file()]
    if len(candidates) != 1:
        raise SetupError('技能包根目录或唯一的一层子目录中须包含 manifest.json。')
    return candidates[0], origin


def load_entry(root, relative):
    deps = root / '_deps'
    if deps.is_dir() and str(deps) not in sys.path:
        sys.path.insert(0, str(deps))
    entry = checked_file(root, relative)
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
        command([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                 '--retries', '1', '--timeout', '15', '--progress-bar', 'off',
                 '--target', str(root / '_deps'), *dependencies], timeout=300)


def run_setup(root, data, ui):
    name = data['name']
    # setup 只返回业务配置；框架统一保存和决定是否启用。
    result = load_entry(root, data['setup']).setup(ui, skill_data(name), skill_config(name))
    if result is not None and not isinstance(result, dict):
        raise SetupError('技能安装引导须返回配置对象，或返回 None 表示稍后配置。')
    return result


def install(source, ui=None, ref=None, replace=False):
    report = ui.info if ui is not None else lambda message: print(message, flush=True)
    with installation_lock():
        index = records()
        packages = private_dir(home() / 'packages')
        with tempfile.TemporaryDirectory(prefix='.install-', dir=packages) as temp:
            report('正在获取技能文件…')
            root, origin = fetch(str(source), Path(temp), ref)
            data = manifest(root)
            name = data['name']
            if name in index and not replace:
                raise SetupError(f'{name} 已安装。重新配置用 primebuddy skill configure {name}；更换版本需加 --replace。')
            if (root / '_deps').exists():
                raise SetupError('技能包不能自带 _deps；请在清单中声明依赖。')
            report('正在检查和安装 ' + name + ' 的依赖…')
            install_dependencies(root, data.get('dependencies', []))
            destination = packages / (name + '-' + uuid.uuid4().hex)
            old_config = skill_config(name)
            old = index.get(name)
            shutil.move(str(root), destination)
            committed = False
            try:
                # 引导和运行模块都从最终路径加载，避免 Python 缓存临时依赖路径。
                module = load_entry(destination, data['entrypoint'])
                if any(not callable(getattr(module, field, None)) for field in ('validate', 'preflight', 'describe', 'run')):
                    raise SetupError('技能运行入口缺少 validate/preflight/describe/run 函数。')
                interrupted = None
                try:
                    result = run_setup(destination, data, ui) if ui is not None else None
                except BaseException as error:
                    if old:
                        raise
                    # 首次扫码中断保留文件和依赖，下一次可直接继续配置。
                    result, interrupted = None, error
                if old and result is None:
                    raise SetupError('新版本未完成配置，保留已有安装。')
                try:
                    if result is not None:
                        write_json(skill_data(name) / 'config.json', result)
                    index[name] = {'path': destination.name, 'version': data['version'], 'ready': result is not None,
                                   'enabled': result is not None, **origin}
                    write_json(home() / 'skills.json', index)
                    committed = True
                except BaseException:
                    write_json(skill_data(name) / 'config.json', old_config)
                    raise
            finally:
                if not committed:
                    shutil.rmtree(destination, ignore_errors=True)
            if old:
                shutil.rmtree(package_path(old), ignore_errors=True)
            if interrupted is not None:
                raise interrupted
            return index[name]


def package_path(record):
    root = home() / 'packages'
    path = (root / record['path']).resolve()
    if path == root.resolve() or not path.is_relative_to(root.resolve()) or not path.is_dir():
        raise SetupError('技能安装记录的路径无效。')
    return path


def configure(name, ui):
    skill_name(name)
    with installation_lock():
        index = records()
        if name not in index:
            raise SetupError('技能尚未安装：' + name)
        root = package_path(index[name])
        # 配置期间先撤销就绪状态；中断不能保留虚假的成功标记。
        index[name].update(ready=False, enabled=False)
        write_json(home() / 'skills.json', index)
        result = run_setup(root, manifest(root), ui)
        if result is not None:
            write_json(skill_data(name) / 'config.json', result)
            index[name].update(ready=True, enabled=True)
            write_json(home() / 'skills.json', index)
        return result is not None


def set_enabled(name, enabled):
    skill_name(name)
    with installation_lock():
        index = records()
        if name not in index:
            raise SetupError('技能尚未安装：' + name)
        if enabled and not index[name]['ready']:
            raise SetupError('技能尚未配置完成，请先运行 primebuddy skill configure ' + name)
        index[name]['enabled'] = enabled
        write_json(home() / 'skills.json', index)
