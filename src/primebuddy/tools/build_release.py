#!/usr/bin/env python3
"""生成项目名-版本号.rpk；内容为 ZIP，不收集私有数据和开发文档。"""
import argparse
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / '_deps'))
sys.path.insert(0, str(ROOT / 'src'))
from primebuddy.skills.manager import load_skill_metadata
from primebuddy import __version__


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
        for name in ('main.py', 'README.md', 'SKILL.md', 'requirements.txt'):
            if not (source / name).is_file():
                continue
            archive.write(source / name, name)
        for name in ('cfg', 'src', 'resource', 'scripts', 'references', 'assets'):
            add_tree(archive, source / name, name)
    return data['name']


def framework_zip(output, bundles):
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('main.py', 'README.md', 'requirements.txt'):
            archive.write(ROOT / name, 'primebuddy/' + name)
        for name in ('config.example.yaml', 'providers.example.yaml'):
            archive.write(ROOT / 'cfg' / name, 'primebuddy/cfg/' + name)
        for name in ('src', 'resource', 'scripts'):
            # 随包技能由本次打包生成，不带入旧安装目录中的重复版本。
            add_tree(archive, ROOT / name, 'primebuddy/' + name, exclude=('bundled',) if name == 'resource' else ())
        for path in bundles:
            archive.write(path, 'primebuddy/resource/bundled/' + path.name)


def default_skills():
    directory = ROOT.parent / 'primebuddy-skills'
    return [path for path in sorted(directory.iterdir()) if path.is_dir() and (path / 'SKILL.md').is_file()] if directory.is_dir() else []


def build_releases(output_dir, skills, framework=True, overwrite=False):
    metadata = [load_skill_metadata(source) for source in skills]
    if len({data['name'] for data in metadata}) != len(metadata):
        raise ValueError('技能名称重复')
    filenames = ([f'primebuddy-{__version__}.rpk'] if framework else [])
    filenames += [data['name'] + '-' + data['version'] + '.rpk' for data in metadata]
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
        if framework:
            path = stage / f'primebuddy-{__version__}.rpk'
            zipped = path.with_suffix('.zip')
            framework_zip(zipped, bundles)
            zipped.rename(path)
        for path in outputs:
            with zipfile.ZipFile(stage / path.name) as archive:
                if archive.testzip() is not None:
                    raise ValueError('发布包 ZIP 校验失败：' + path.name)
        for path in outputs:
            (stage / path.name).replace(path)
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist', help='输出目录，默认框架 dist；文件名自动生成')
    parser.add_argument('--skill', type=Path, action='append', help='选择技能目录，可重复；默认打包相邻 primebuddy-skills 中的所有技能')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--skill-only', type=Path, help='只打包一个独立技能')
    mode.add_argument('--framework-only', action='store_true', help='只打包框架，不附带技能')
    parser.add_argument('--overwrite', action='store_true', help='替换输出目录中已有的同版本 RPK')
    args = parser.parse_args()
    if args.skill and (args.skill_only or args.framework_only):
        parser.error('--skill 不能与 --skill-only 或 --framework-only 同用')
    try:
        skills = [args.skill_only] if args.skill_only else ([] if args.framework_only else args.skill or default_skills())
        outputs = build_releases(args.output_dir, skills, framework=not args.skill_only, overwrite=args.overwrite)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    for path in outputs:
        print(path.resolve())


if __name__ == '__main__':
    main()
