#!/usr/bin/env python3
"""升级文件事务。仅用标准库，复制后可在应用源码缺失时独立恢复。"""
import argparse
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def move(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    sync_directory(source.parent)
    sync_directory(target.parent)


def backup_file(source, target):
    # 普通文件保持原位置可读，再原子替换；升级命令入口不会暂时消失。
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=target.parent)
    os.close(fd)
    try:
        shutil.copy2(source, name)
        with open(name, 'rb') as stream:
            os.fsync(stream.fileno())
        move(Path(name), target)
    finally:
        Path(name).unlink(missing_ok=True)


def remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def target_path(home, name):
    path = PurePosixPath(name)
    allowed = {'main.py', 'README.md', 'CHANGELOG.md', 'LICENSE', 'requirements.txt',
               'release.json', 'src', 'resource', 'scripts', 'docs', '_deps', 'bin/primebuddy', 'bin/env.sh',
               'cfg/config.example.yaml', 'cfg/providers.example.yaml', 'cfg/skill-adapters.example.yaml',
               'cfg/skills.yaml', 'cfg/config.yaml', 'cfg/providers.yaml'}
    if (name not in allowed and not (len(path.parts) == 2 and path.parts[0] == 'skills'
            and all(c.isascii() and (c.isalnum() or c in '-_') for c in path.parts[1]))):
        raise ValueError('升级事务包含无效路径。')
    if path.is_absolute() or '..' in path.parts or str(path) != name:
        raise ValueError('升级事务包含无效路径。')
    target = home / name
    if any(p.is_symlink() for p in [target, *target.parents]):
        raise ValueError('升级目标不能是符号链接。')
    return target


def read_transaction(home):
    directory = home / '.update'
    if directory.is_symlink():
        raise ValueError('升级目录不能是符号链接。')
    journal = directory / 'transaction.json'
    data = json.loads(journal.read_text())
    if data.get('schema_version') != 1 or data.get('phase') not in ('applying', 'rolling_back', 'complete', 'restored'):
        raise ValueError('升级记录无效，请保留升级目录进行检查。')
    entries = data.get('entries')
    if not isinstance(entries, list) or not entries:
        raise ValueError('升级记录缺少文件清单。')
    names = set()
    for entry in entries:
        name = entry['path']
        target_path(home, name)
        if name in names or type(entry['had_original']) is not bool or entry['state'] not in (
                'pending', 'backing_up', 'installing', 'installed', 'restoring', 'restored'):
            raise ValueError('升级记录无效。')
        names.add(name)
        for base in ('new', 'old'):
            target_path(directory / base, name)
    return journal, data


def finish(home):
    directory = home / '.update'
    for name in ('new', 'old'):
        remove(directory / name)
    (directory / 'transaction.json').unlink()
    sync_directory(directory)
    # 保留独立执行器，避免仍在运行的恢复入口被删除。


def recover(home):
    journal, data = read_transaction(home)
    if data['phase'] == 'complete':
        finish(home)
        return
    data['phase'] = 'rolling_back'
    save(journal, data)
    for entry in reversed(data['entries']):
        if entry['state'] == 'restored':
            continue
        target = target_path(home, entry['path'])
        backup = home / '.update/old' / entry['path']
        prior = entry['state']
        entry['state'] = 'restoring'
        save(journal, data)
        if backup.exists():
            # 普通文件直接原子替换，回退中断时恢复入口仍然存在。
            if not backup.is_file() or target.is_dir():
                remove(target)
            move(backup, target)
        elif not entry['had_original']:
            remove(target)
        elif prior not in ('pending', 'backing_up', 'restoring') or not target.exists():
            raise ValueError('原版本备份缺失，请保留升级目录进行检查。')
        entry['state'] = 'restored'
        save(journal, data)
    data['phase'] = 'restored'
    save(journal, data)
    finish(home)


def apply(home):
    journal, data = read_transaction(home)
    if data['phase'] != 'applying' or any(entry['state'] != 'pending' for entry in data['entries']):
        raise ValueError('存在未完成升级，请先恢复原版本。')
    try:
        for entry in data['entries']:
            target = target_path(home, entry['path'])
            staged = home / '.update/new' / entry['path']
            backup = home / '.update/old' / entry['path']
            entry['state'] = 'backing_up'
            save(journal, data)
            if entry['had_original']:
                if target.is_file():
                    backup_file(target, backup)
                else:
                    move(target, backup)
            entry['state'] = 'installing'
            save(journal, data)
            move(staged, target)
            entry['state'] = 'installed'
            save(journal, data)
        data['phase'] = 'complete'
        save(journal, data)
    except BaseException:
        recover(home)
        raise
    finish(home)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('home', type=Path)
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    try:
        with (home / '.update/runner.lock').open('a') as runner_lock:
            try:
                fcntl.flock(runner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('升级执行器仍在工作，请稍后重试。') from None
            return run(home, args.recover)
    except (OSError, ValueError, KeyError, TypeError):
        print('升级未完成。请运行 primebuddy update --recover；如命令不可用，执行：')
        import shlex, sys
        print(shlex.join([sys.executable, str(home / '.update/runner.py'), '--recover', str(home)]))
        return 1


def run(home, recovering):
    if recovering:
        # 正常 apply 的父进程持有安装锁；独立恢复入口自行加锁。
        with (home / 'install.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('另一个安装或升级正在进行，请稍后重试。') from None
            if not (home / '.update/transaction.json').exists():
                print('没有需要恢复的升级。')
                return 0
            recover(home)
            print('升级恢复完成。')
    else:
        apply(home)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
