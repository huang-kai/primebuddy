"""本地校验 skill 清单和入口，不执行业务。"""
import argparse
from primebuddy.skills.manager import load_skill_metadata
from primebuddy.config.storage import SetupError


def main(directory):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='校验本技能的清单和文件路径')
    parser.add_argument('command', nargs='?', choices=['configure'], help='运行本技能可选的 Console 配置')
    args = parser.parse_args()
    if not args.check and args.command is None:
        parser.print_help()
        return 0
    try:
        skill = load_skill_metadata(directory)
        if args.command == 'configure':
            from primebuddy.cli.wizard import Wizard
            from primebuddy.skills.manager import run_setup
            from primebuddy.config.storage import config_path, write_config
            # 原生向导不再是必需入口；直接开发目录也可以独立配置。
            result = run_setup(directory, skill, Wizard)
            if result is None:
                print('配置尚未完成。')
                return 1
            write_config(config_path('config.yaml', skill=skill['name']), result)
            print('配置完成。')
            return 0
    except SetupError as error:
        parser.exit(1, str(error) + '\n')
    print(skill['name'] + ' 清单与入口校验通过。')
    return 0
