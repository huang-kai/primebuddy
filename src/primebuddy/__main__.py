"""兼容 python -m primebuddy；命令实现统一在 cli。"""
from primebuddy.cli.commands import main

if __name__ == '__main__':
    raise SystemExit(main())
