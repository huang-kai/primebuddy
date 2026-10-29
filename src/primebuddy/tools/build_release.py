#!/usr/bin/env python3
"""生成 RPK（ZIP）；正式发行同时提供完整包、纯平台包与校验清单。"""
from copy import deepcopy
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / '_deps'))
sys.path.insert(0, str(ROOT / 'src'))
from primebuddy.skills.manager import load_skill_metadata
from primebuddy import __version__
from primebuddy.config.compatibility import load_release, release_url


def add_tree(archive, source, prefix, exclude=()):
    for path in sorted(source.rglob('*')):
        if path.relative_to(source).parts[0] in exclude:
            continue
        if path.is_symlink():
            raise ValueError('发布源码不能包含符号链接')
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            archive.write(path, str(Path(prefix) / path.relative_to(source)))


def skill_zip(source, output):
    data = load_skill_metadata(source)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('main.py', 'README.md', 'CHANGELOG.md', 'SKILL.md', 'requirements.txt', 'LICENSE'):
            if not (source / name).is_file():
                continue
            archive.write(source / name, name)
        for name in ('cfg', 'src', 'resource', 'scripts', 'references', 'assets'):
            add_tree(archive, source / name, name)
    return data['name']


def framework_zip(output, bundles, manifest=None):
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('main.py', 'README.md', 'requirements.txt'):
            archive.write(ROOT / name, 'primebuddy/' + name)
        if (ROOT / 'LICENSE').is_file():
            archive.write(ROOT / 'LICENSE', 'primebuddy/LICENSE')
        archive.writestr('primebuddy/release.json', json_text(manifest if manifest is not None else load_release(ROOT / 'release.json')))
        archive.write(ROOT / 'CHANGELOG.md', 'primebuddy/CHANGELOG.md')
        for name in ('config.example.yaml', 'providers.example.yaml', 'skill-adapters.example.yaml'):
            archive.write(ROOT / 'cfg' / name, 'primebuddy/cfg/' + name)
        for name in ('src', 'resource', 'scripts', 'docs'):
            # 随包技能由本次打包生成，不带入旧安装目录中的重复版本。
            add_tree(archive, ROOT / name, 'primebuddy/' + name, exclude=('bundled',) if name == 'resource' else ())
        for path in bundles:
            archive.write(path, 'primebuddy/resource/bundled/' + path.name)


def json_text(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def artifact(path, version):
    return {'filename': path.name, 'url': release_url(version, path.name),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def release_notes(manifest):
    content = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    heading = re.search(r'^##[ \t]+\[?' + re.escape(manifest['version']) + r'\]?(?=[ \t]|$)([^\n]*)\n', content, re.M)
    if heading is None:
        raise ValueError('CHANGELOG.md 缺少当前版本：' + manifest['version'])
    section = re.split(r'^##\s+', content[heading.end():], maxsplit=1, flags=re.M)[0].strip()
    if not section:
        raise ValueError('CHANGELOG.md 当前版本没有更新说明。')
    table = ['| Skill | Supported versions | Recommended |', '| --- | --- | --- |']
    for name, rule in manifest['skills'].items():
        table.append(f"| `{name}` | `>={rule['min_version']}, <{rule['max_version']}` | `{rule['recommended_version']}` |")
    compatibility = '### Compatibility\n\n' + '\n'.join(table)
    pattern = re.compile(r'^###[ \t]+Compatibility[ \t]*\n.*?(?=^###[ \t]+|\Z)', re.M | re.S)
    existing = pattern.search(section)
    if existing:
        section = section[:existing.start()] + compatibility + '\n\n' + pattern.sub('', section[existing.end():])
    else:
        section += '\n\n' + compatibility
    return '# PrimeBuddy ' + manifest['version'] + heading.group(1).rstrip() + '\n\n' + section.rstrip() + '\n'


def build_releases(output_dir, skills, framework=True, overwrite=False, release=False):
    metadata = [load_skill_metadata(source) for source in skills]
    if len({data['name'] for data in metadata}) != len(metadata):
        raise ValueError('技能名称重复')
    manifest = load_release(ROOT / 'release.json') if framework or release else None
    if manifest is not None and manifest['version'] != __version__:
        raise ValueError('发行清单与框架代码版本不一致。')
    notes = None
    if release:
        if not framework:
            raise ValueError('--release 必须与 --framework 一起使用。')
        if {data['name'] for data in metadata} != set(manifest['skills']):
            raise ValueError('完整发行须通过 --skill 提供清单中的全部官方技能，且不能附加其他技能。')
        for data in metadata:
            if data.get('cli_version') != 1:
                raise ValueError('正式发行技能必须声明 version: 1 和包内 configure 向导：' + data['name'])
            if data['version'] != manifest['skills'][data['name']]['recommended_version']:
                raise ValueError('官方技能版本与发行清单推荐版本不一致：' + data['name'])
        notes = release_notes(manifest)
    filenames = ([f'primebuddy-{__version__}.rpk'] if framework else [])
    if release:
        filenames.append(f'primebuddy-{__version__}-core.rpk')
    filenames += [data['name'] + '-' + data['version'] + '.rpk' for data in metadata]
    if release:
        filenames += ['release.json', 'release-notes.md', 'SHA256SUMS']
    if len(set(filenames)) != len(filenames):
        raise ValueError('发布包名称重复')
    outputs = [output_dir / name for name in filenames]
    for path in outputs:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('发布目标不是普通文件：' + str(path))
        if path.exists() and not overwrite:
            raise ValueError('发布包已存在，更新版本或使用 --overwrite：' + str(path))
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.release-', dir=output_dir) as temp:
        stage = Path(temp)
        bundles = []
        for source, data in zip(skills, metadata):
            path = stage / (data['name'] + '-' + data['version'] + '.rpk')
            zipped = path.with_suffix('.zip')
            skill_zip(source, zipped)
            zipped.rename(path)
            bundles.append(path)
            if release:
                manifest['skills'][data['name']]['artifact'] = artifact(path, manifest['version'])
        if framework:
            manifest.pop('framework', None)
            manifest.pop('framework_core', None)
            path = stage / f'primebuddy-{__version__}.rpk'
            zipped = path.with_suffix('.zip')
            # 正式发行的兼容目录可包含另行下载的技能，不等于预装候选列表。
            included = set(manifest.get('bundled_skills', manifest['skills'])) if release else None
            framework_bundles = [bundle for bundle, data in zip(bundles, metadata)
                                 if included is None or data['name'] in included]
            framework_zip(zipped, framework_bundles, manifest)
            zipped.rename(path)
        if release:
            core_path = stage / f'primebuddy-{__version__}-core.rpk'
            core_manifest = deepcopy(manifest)
            core_manifest['bundled_skills'] = []
            framework_zip(core_path, [], core_manifest)
            manifest['framework'] = artifact(path, manifest['version'])
            manifest['framework_core'] = artifact(core_path, manifest['version'])
            (stage / 'release.json').write_text(json_text(manifest), encoding='utf-8')
            (stage / 'release-notes.md').write_text(notes, encoding='utf-8')
            checksums = ''.join(hashlib.sha256((stage / name).read_bytes()).hexdigest() + '  ' + name + '\n'
                                for name in filenames if name != 'SHA256SUMS')
            (stage / 'SHA256SUMS').write_text(checksums, encoding='utf-8')
        for path in outputs:
            if path.suffix != '.rpk':
                continue
            with zipfile.ZipFile(stage / path.name) as archive:
                if archive.testzip() is not None:
                    raise ValueError('发布包 ZIP 校验失败：' + path.name)
        for path in outputs:
            (stage / path.name).replace(path)
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist', help='输出目录，默认框架 dist；文件名自动生成')
    parser.add_argument('--framework', action='store_true', help='打包框架；只有指定 --skill 时才附带技能')
    parser.add_argument('--skill', type=Path, action='append', help='打包指定技能源码目录，可重复；与 --framework 同用时放入框架包')
    parser.add_argument('--skills-dir', type=Path, help='完整发行的技能源码根目录；按 release.json 收集全部官方技能，与 --skill 互斥')
    parser.add_argument('--overwrite', action='store_true', help='替换输出目录中已有的同版本 RPK')
    parser.add_argument('--release', action='store_true', help='生成完整发行：完整包、纯平台 core 包、独立技能包、清单、SHA256SUMS 和更新说明；须提供清单推荐的全部技能')
    args = parser.parse_args()
    if args.skills_dir and (not args.release or args.skill):
        parser.error('--skills-dir 须与 --release 一起使用，且不能与 --skill 混用')
    if args.skills_dir:
        args.skill = [args.skills_dir / name for name in load_release(ROOT / 'release.json')['skills']]
    if not args.framework and not args.skill and not args.release:
        parser.print_help()
        return
    try:
        outputs = build_releases(args.output_dir, args.skill or [], framework=args.framework,
                                 overwrite=args.overwrite, release=args.release)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    for path in outputs:
        print(path.resolve())


if __name__ == '__main__':
    main()
