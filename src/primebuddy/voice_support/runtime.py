"""运行状态与源码分离；沿用已有锁、登录与恢复记录，避免绕过旧会话互斥。"""
import os
from pathlib import Path

LEGACY = Path('/mnt/user_packages/qqmusic/runtime')
RUNTIME = Path(os.environ.get('PRIMEBUDDY_RUNTIME', str(LEGACY if LEGACY.is_dir() else Path.home() / '.local/state/primebuddy')))

def private_runtime():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    RUNTIME.chmod(0o700)
    return RUNTIME
