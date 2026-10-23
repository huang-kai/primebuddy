"""无第三方包的系统 Python 也能先完成依赖引导。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class InstallBootstrapTests(unittest.TestCase):
    def test_dependency_bootstrap_precedes_any_yaml_import(self):
        script = '''from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from primebuddy.tools import install
assert 'yaml' not in sys.modules
def dependencies(stage):
    assert 'yaml' not in sys.modules
    sys.path.insert(0, sys.argv[2])
with patch.object(install, 'prepare_dependencies', side_effect=dependencies) as prepare, \\
        patch.object(install, 'prepare_logs'), patch.object(install, 'configure_shell'):
    launcher = install.install(Path(sys.argv[3]))
    prepare.assert_called_once()
    assert launcher.is_file()
print('BOOTSTRAP_OK')
'''
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, '-S', '-c', script, str(ROOT / 'src'),
                                     str(Path(yaml.__file__).resolve().parent.parent), temporary + '/app'],
                                    env=dict(os.environ, HOME=temporary, PRIMEBUDDY_LOG_DIR=temporary + '/logs'),
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('BOOTSTRAP_OK', result.stdout)

    def test_pending_transaction_blocks_install_before_dependency_bootstrap(self):
        script = '''from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from primebuddy.tools import install
with patch.object(install, 'prepare_dependencies') as prepare:
    try:
        install.install(Path(sys.argv[2]))
    except RuntimeError as error:
        assert 'update --recover' in str(error)
    else:
        raise AssertionError('pending transaction was accepted')
    prepare.assert_not_called()
assert 'yaml' not in sys.modules
print('PENDING_BLOCKED')
'''
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / 'app'
            journal = home / '.update/transaction.json'
            journal.parent.mkdir(parents=True)
            journal.write_text('{}')
            result = subprocess.run([sys.executable, '-S', '-c', script, str(ROOT / 'src'), str(home)],
                                    env=dict(os.environ, HOME=temporary), capture_output=True, text=True)
            self.assertEqual(journal.read_text(), '{}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PENDING_BLOCKED', result.stdout)


if __name__ == '__main__':
    unittest.main()
