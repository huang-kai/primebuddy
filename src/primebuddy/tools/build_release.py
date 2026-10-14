#!/usr/bin/env python3
"""将框架和独立技能打成可分发 ZIP；只复制发布清单，不收集私有运行数据。"""
import argparse
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / '_deps'))
sys.path.insert(0, str(ROOT / 'src'))
from primebuddy.skills.manager import load_skill_metadata


def add_tree(archive, source, prefix):
    for path in sorted(source.rglob('*')):
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
        for name in ('cfg', 'src', 'resource', 'docs', 'scripts', 'references', 'assets'):
            add_tree(archive, source / name, name)
    return data['name']


def build(output, skills):
    if output.exists():
        raise ValueError('输出文件已存在，请选择新路径。')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        bundles = []
        for source in skills:
            data = load_skill_metadata(source)
            path = Path(temp) / (data['name'] + '.zip')
            if path.exists():
                raise ValueError('随包技能名称重复')
            skill_zip(source, path)
            bundles.append(path)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in ('main.py', 'README.md', 'requirements.txt'):
                archive.write(ROOT / name, 'primebuddy/' + name)
            for name in ('development-plan.md', 'sdk-compatibility.md', 'architecture.md',
                         'skill-development.md', 'local-development.md', 'model-providers.md', 'voice-limitations.md',
                         'refactor-2026-10-13.md', 'native-media-followup-2026-10-13.md',
                         'voice-recording-test-2026-10-13.md'):
                path = ROOT / 'docs' / name
                if path.is_file():
                    archive.write(path, 'primebuddy/docs/' + name)
            add_tree(archive, ROOT / 'docs/examples', 'primebuddy/docs/examples')
            for name in ('config.example.yaml', 'providers.example.yaml'):
                archive.write(ROOT / 'cfg' / name, 'primebuddy/cfg/' + name)
            for name in ('src', 'resource', 'scripts'):
                add_tree(archive, ROOT / name, 'primebuddy/' + name)
            for path in bundles:
                archive.write(path, 'primebuddy/resource/bundled/' + path.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--skill', type=Path, action='append', help='可重复；默认附带独立点歌台项目')
    parser.add_argument('--skill-only', type=Path, help='只打包一个独立技能，不打包框架')
    args = parser.parse_args()
    if args.skill_only:
        if args.output.exists():
            parser.error('输出文件已存在')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        skill_zip(args.skill_only, args.output)
    else:
        build(args.output, args.skill or [ROOT.parent / 'primebuddy-skills/diangetai'])
    print(args.output.resolve())


if __name__ == '__main__':
    main()
