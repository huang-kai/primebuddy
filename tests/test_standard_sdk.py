"""在污染和缺包环境中验证标准 SDK 加载，不允许回退到私有副本。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RobotEnvironmentTests(unittest.TestCase):
    def fixture(self, root, series):
        identity = root / 'data/info/series'
        identity.parent.mkdir(parents=True)
        identity.write_text(series + '\n')
        ros = root / 'ros/humble/setup.bash'
        ros.parent.mkdir(parents=True)
        ros.write_text('export ROS_DISTRO=humble\n')
        prefix = root / 'software/interface/aimdk_msgs'
        py = prefix / f'local/lib/python{sys.version_info.major}.{sys.version_info.minor}/dist-packages'
        (py / 'aimdk_msgs').mkdir(parents=True)
        (py / 'aimdk_msgs/__init__.py').write_text('')
        (prefix / 'lib').mkdir()
        for kind in ('rosidl_generator_c', 'rosidl_generator_py', 'rosidl_typesupport_c'):
            (prefix / f'lib/libaimdk_msgs__{kind}.so').touch()
        setup = prefix / 'share/aimdk_msgs/local_setup.bash'
        setup.parent.mkdir(parents=True)
        setup.write_text('export STANDARD_PACKAGE_HOOK=loaded\n')
        old = root / 'software/housekeeper/bin/aimdk_msgs'
        (old / 'python/aimdk_msgs').mkdir(parents=True)
        (old / 'python/aimdk_msgs/__init__.py').write_text('raise RuntimeError("old SDK imported")\n')
        (old / 'lib').mkdir()
        (old / 'lib/libaimdk_msgs__rosidl_typesupport_c.so').touch()
        (old / 'share/aimdk_msgs').mkdir(parents=True)
        (old.parent / 'setup.bash').write_text('exit 99 # Must never be sourced\n')
        basic = root / 'etc/basic_env'
        basic.parent.mkdir()
        basic.write_text(f'export PYTHONPATH={old}/python LD_LIBRARY_PATH={old}/lib AMENT_PREFIX_PATH={old}\n')
        script = root / 'environment.sh'
        script.write_text((ROOT / 'scripts/robot_env.sh').read_text().replace('/robot/', str(root) + '/').replace('/opt/ros/', str(root / 'ros') + '/'))
        return prefix, py, setup, script

    def run_shell(self, script):
        return subprocess.run(['bash', '--noprofile', '--norc', '-uc',
            'source "$1" || exit $?; printf "%s\\n" "$PYTHONPATH" "$LD_LIBRARY_PATH" "$AMENT_PREFIX_PATH" "$STANDARD_PACKAGE_HOOK"',
            'test', str(script)], capture_output=True, text=True, timeout=10,
            env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','LD_LIBRARY_PATH','AMENT_PREFIX_PATH')})

    def test_both_models_discard_old_sdk_and_load_standard_hooks(self):
        for series in ('Q1', 'T1'):
            with self.subTest(series=series), tempfile.TemporaryDirectory() as temp:
                prefix, py, _, script = self.fixture(Path(temp), series)
                result = self.run_shell(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [str(py), str(prefix/'lib'), str(prefix), 'loaded'])

    def test_missing_standard_library_never_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix, _, _, script = self.fixture(Path(temp), 'T1')
            (prefix/'lib/libaimdk_msgs__rosidl_generator_c.so').unlink()
            result = self.run_shell(script)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')

    def test_failed_package_hook_stops_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, setup, script = self.fixture(Path(temp), 'Q1')
            setup.write_text('return 7\n')
            self.assertEqual(self.run_shell(script).returncode, 7)

    def test_remote_environment_preserves_command_stdin(self):
        from primebuddy.integrations.primebot.native_sdk import shell_environment
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.fixture(root, 'T1')
            command = shell_environment().replace('/robot/', str(root)+'/').replace('/opt/ros/',str(root/'ros')+'/')
            command += 'test "$STANDARD_PACKAGE_HOOK" = loaded || exit 9; cat'
            result = subprocess.run(['bash','-c',command], input='caller stdin',capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout,'caller stdin')
