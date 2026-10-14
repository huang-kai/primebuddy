#!/usr/bin/env python3
"""PrimeBuddy 主入口：加载本地源码和应用依赖。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / '_deps')]

if __name__ == '__main__':
    if sys.argv[1:2] == ['install']:
        from primebuddy.tools.install import main
        del sys.argv[1]
    else:
        from primebuddy.cli.commands import main
    raise SystemExit(main())
