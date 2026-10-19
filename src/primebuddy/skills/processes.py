"""独立命令进程监督；只管理执行生命周期，不规定 skill 函数或输出协议。"""
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import uuid


def _process_table():
    """活进程的父 PID、组和启动身份，不读取命令参数。"""
    if sys.platform.startswith('linux'):
        members = {}
        for path in Path('/proc').glob('[0-9]*/stat'):
            try:
                # comm 可以含空格和括号，最后一个右括号后才是固定字段。
                fields = path.read_text().rsplit(')', 1)[1].split()
                if fields[0] not in ('Z', 'X'):
                    members[int(path.parent.name)] = (int(fields[1]), int(fields[2]), fields[19])
            except (OSError, ValueError, IndexError):
                continue
        return members
    result = subprocess.run(['ps', '-axo', 'pid=,ppid=,pgid=,stat=,lstart='],
                            capture_output=True, text=True, timeout=1, check=True)
    members = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5 and not fields[3].startswith('Z'):
            members[int(fields[0])] = (int(fields[1]), int(fields[2]), fields[4])
    return members


def _group_members(group):
    return {pid: info[2] for pid, info in _process_table().items() if info[1] == group}


def _save_record(path, record):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(record, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_process(command, *, cwd, env, runtime, cancelled, timeout=600,
                max_output_bytes=1024 * 1024, task_id='', owner_id='', stop_grace=(3.0, 2.0, 2.0)):
    """运行 argv；等待超时与取消均停止进程组，返回真实退出和清理状态。

    status: exited/cancelled/timed_out/output_limit/cleanup_failed。
    只有 exited + returncode=0 + cleanup_confirmed 才是成功。输出无需 JSON。
    task_id/owner_id 是宿主内部标识；账本不保存命令、参数、环境或输出正文。
    """
    if (not isinstance(command, list) or not command
            or any(not isinstance(arg, str) or '\x00' in arg for arg in command)):
        raise ValueError('命令须为非空文本参数列表。')
    if (not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0
            or type(max_output_bytes) is not int or max_output_bytes <= 0 or len(stop_grace) != 3
            or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in stop_grace)):
        raise ValueError('运行期限、输出限额和停止宽限期无效。')
    if os.name != 'posix':
        raise ValueError('独立进程监督需要 POSIX 进程组。')
    directory = Path(runtime) / 'processes'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    execution_id = uuid.uuid4().hex
    record_path = directory / (execution_id + '.json')
    started = time.monotonic()
    deadline = started + timeout
    record = dict(execution_id=execution_id, task_id=task_id, owner_id=owner_id,
                  state='starting', started_at=time.time(), timeout_seconds=timeout,
                  cleanup_confirmed=False)
    _save_record(record_path, record)
    selector = selectors.DefaultSelector()
    output_files = {}
    output_paths = {}
    process = None
    known = {}
    output_size = 0
    output_limit = False
    reason = 'exited'
    cleanup_confirmed = False

    def pump(wait):
        nonlocal output_size, output_limit
        for key, _ in selector.select(wait):
            try:
                chunk = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            available = max(0, max_output_bytes - output_size)
            output_files[key.data].write(chunk[:available])
            output_size += min(available, len(chunk))
            if len(chunk) > available:
                output_limit = True

    def members():
        current = _group_members(process.pid)
        # 同组成员在根进程尚未退出时均属于本次执行；根退出后至少要有一个
        # 已知的启动身份仍匹配，才能对整个组发信号，防止 PGID 被复用后误杀。
        root_running = process.returncode is None
        if not root_running and current and not any(current.get(pid) == identity for pid, identity in known.items()):
            return None
        known.update(current)
        table = _process_table()
        active = {pid: identity for pid, identity in known.items()
                  if pid in table and table[pid][2] == identity}
        # 同组的当前成员，以及先前已观察到的后代，都是发现新后代的根。
        # 已记录的后代换 session/被重新收养后仍按 PID+启动身份追踪。
        parents = set(active)
        while True:
            children = {pid: info[2] for pid, info in table.items() if info[0] in parents and pid not in parents}
            if not children:
                break
            active.update(children)
            parents.update(children)
        known.update(active)
        return current | active

    def stop_group():
        for sig, grace in zip((signal.SIGINT, signal.SIGTERM, signal.SIGKILL), stop_grace):
            current = members()
            if current is None:
                return False
            if not current:
                return True
            if process.pid == os.getpgrp():
                return False
            group = _group_members(process.pid)
            if group:
                # 上面 members 已验证组身份；这里再复核以免组在探测后复用。
                if not any(group.get(pid) == identity for pid, identity in known.items()):
                    return False
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
            for pid, identity in current.items():
                if pid in group:
                    continue
                info = _process_table().get(pid)
                if info is not None and info[2] == identity:
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            end = time.monotonic() + grace
            while time.monotonic() < end:
                pump(min(.02, max(0, end - time.monotonic())))
                process.poll()
                current = members()
                if current is None:
                    return False
                if not current:
                    return True
        return members() == {}

    try:
        for name in ('stdout', 'stderr'):
            path = directory / (execution_id + '.' + name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            output_files[name] = os.fdopen(fd, 'wb')
            output_paths[name] = path
        if cancelled.is_set():
            reason, cleanup_confirmed = 'cancelled', True
        else:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            # 在 poll 回收根进程之前记录成员，即使根进程很快退出也能追踪遗留子进程。
            known.update(_group_members(process.pid))
            record.update(state='running', pid=process.pid, pgid=process.pid,
                          root_start_identity=known.get(process.pid))
            _save_record(record_path, record)
            for name in ('stdout', 'stderr'):
                pipe = getattr(process, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            while True:
                pump(.02)
                members()
                code = process.poll()
                if cancelled.is_set():
                    reason = 'cancelled'
                    break
                if time.monotonic() >= deadline:
                    reason = 'timed_out'
                    break
                if output_limit:
                    reason = 'output_limit'
                    break
                if code is not None:
                    break
            record['state'] = 'cleaning'
            _save_record(record_path, record)
            cleanup_confirmed = stop_group()
            # 获取退出前留在管道里的尾部；不等待已退出根进程的 EOF 无限阻塞。
            drain_until = time.monotonic() + .2
            while selector.get_map() and time.monotonic() < drain_until:
                pump(.01)
            process.poll()
            if output_limit and reason == 'exited':
                reason = 'output_limit'
    except BaseException as error:
        reason = 'failed'
        if process is not None:
            try:
                cleanup_confirmed = stop_group()
            except Exception:
                # 状态探测本身失败时仍回收尚未 wait 的直接子进程；不能据此
                # 证明整个组已停止，故不标记 cleanup_confirmed。
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=.5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        else:
            cleanup_confirmed = True
        error.cleanup_confirmed = cleanup_confirmed
        raise
    finally:
        selector.close()
        if process is not None:
            for name in ('stdout', 'stderr'):
                getattr(process, name).close()
            process.poll()
        for output in output_files.values():
            output.close()
        record['termination_reason'] = reason
        if not cleanup_confirmed:
            reason = 'cleanup_failed'
        record.update(state=reason, returncode=process.returncode if process else None,
                      cleanup_confirmed=cleanup_confirmed, finished_at=time.time(),
                      output_bytes=output_size, observed_pids=sorted(known))
        try:
            _save_record(record_path, record)
        except BaseException as error:
            error.cleanup_confirmed = cleanup_confirmed
            raise
    return dict(execution_id=execution_id, status=reason, returncode=record['returncode'],
                stdout=output_paths['stdout'].read_bytes().decode('utf-8', errors='replace'),
                stderr=output_paths['stderr'].read_bytes().decode('utf-8', errors='replace'),
                cleanup_confirmed=cleanup_confirmed, record_path=str(record_path))
