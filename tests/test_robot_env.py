"""验证新终端按型号加载环境，原厂 setup 的失败不能被启动器吞掉。"""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RobotEnvironmentTests(unittest.TestCase):
    def test_q1_overlay_keeps_python_and_native_libraries_together(self):
        for complete in (True, False):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                identity = root / 'data/info/series'
                identity.parent.mkdir(parents=True)
                identity.write_text('Q1\n')
                setup = root / 'software/housekeeper/bin/setup.bash'
                setup.parent.mkdir(parents=True)
                setup.write_text('export PYTHONPATH=/old/python LD_LIBRARY_PATH=/old/lib AMENT_PREFIX_PATH=/old\n')
                prefix = root / 'software/interface/aimdk_msgs'
                (prefix / 'local/lib/python3.10/dist-packages/aimdk_msgs').mkdir(parents=True)
                if complete:
                    (prefix / 'lib').mkdir()
                    (prefix / 'lib/libaimdk_msgs__rosidl_typesupport_c.so').touch()
                script = root / 'environment.sh'
                script.write_text((ROOT / 'scripts/robot_env.sh').read_text().replace('/robot/', str(root) + '/'))
                result = subprocess.run(['bash', '--noprofile', '--norc', '-c',
                    'source "$1" && printf "%s\\n" "$PYTHONPATH" "$LD_LIBRARY_PATH" "$AMENT_PREFIX_PATH"',
                    'test', str(script)], capture_output=True, text=True, timeout=5)
                if complete:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines(), [
                        str(prefix / 'local/lib/python3.10/dist-packages') + ':/old/python',
                        str(prefix / 'lib') + ':/old/lib', str(prefix) + ':/old'])
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, '')

    def test_each_model_loads_its_preferred_sdk_in_a_clean_shell(self):
        for series, selected in [('T1', 'app_proxy'), ('Q1', 'housekeeper')]:
            with self.subTest(series=series), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                identity = root / 'data/info/series'
                identity.parent.mkdir(parents=True)
                identity.write_text(series + '\n')
                basic = root / 'etc/basic_env'
                basic.parent.mkdir()
                basic.write_text('export PRIMEBUDDY_ENV_BASE=loaded\n')
                for name in ('app_proxy', 'housekeeper'):
                    setup = root / 'software' / name / 'bin/setup.bash'
                    setup.parent.mkdir(parents=True)
                    setup.write_text('export PRIMEBUDDY_ENV_SDK=' + name + '\n')
                script = root / 'environment.sh'
                script.write_text((ROOT / 'scripts/robot_env.sh').read_text().replace('/robot/', str(root) + '/'))
                result = subprocess.run(['bash', '--noprofile', '--norc', '-c',
                    'source "$1" && test "$PRIMEBUDDY_ENV_BASE" = loaded && test "$PRIMEBUDDY_ENV_SDK" = "$2"',
                    'test', str(script), selected], capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_vendor_setup_is_a_failed_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / 'data/info/series'
            identity.parent.mkdir(parents=True)
            identity.write_text('Q1\n')
            setup = root / 'software/housekeeper/bin/setup.bash'
            setup.parent.mkdir(parents=True)
            setup.write_text('return 7\n')
            script = root / 'environment.sh'
            script.write_text((ROOT / 'scripts/robot_env.sh').read_text().replace('/robot/', str(root) + '/'))
            result = subprocess.run(['bash', '-c', 'source "$1"', 'test', str(script)], timeout=5)
            self.assertEqual(result.returncode, 7)
