"""运行目录选择与私有权限；存放会话锁、模式恢复记录和语音缓存。"""
import os
from pathlib import Path
from primebuddy.config.storage import get_data_directory, load_app_config


def runtime_directory():
    path = Path(os.environ.get('PRIMEBUDDY_RUNTIME', load_app_config()['runtime']['directory'])).expanduser()
    return path if path.is_absolute() else get_data_directory() / path


RUNTIME = runtime_directory()


def private_runtime():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    RUNTIME.chmod(0o700)
    return RUNTIME
