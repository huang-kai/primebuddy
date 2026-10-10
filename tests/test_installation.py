"""安装协议的端到端测试：临时 ZIP/Git、状态注册、回滚与异常输入。"""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from primebuddy import __version__, skill_manager as manager
from primebuddy.engine import Context, execute
from primebuddy.registry import Registry
from primebuddy.settings import SetupError, home, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


class DummyUI:
    def info(self, text):
        pass


def make_skill(root, setup='return {}', version='0.1.0'):
    (root / 'src').mkdir(parents=True, exist_ok=True)
    (root / 'manifest.json').write_text(json.dumps({
        'name': 'hello', 'version': version, 'api_version': 1, 'description': '测试问候',
        'entrypoint': 'src/skill.py', 'setup': 'src/setup.py', 'dependencies': [],
        'actions': {'hello': {'parameters': {}, 'resources': []}}}))
    (root / 'SKILL.md').write_text('调用 hello 问候用户。')
    (root / 'README.md').write_text('测试技能')
    (root / 'src/setup.py').write_text('def setup(ui, data, config):\n    ' + setup + '\n')
    (root / 'src/skill.py').write_text('''from .helper import reply

def validate(action, args): pass
def preflight(action, args, context): pass
def describe(action, args): return '准备问候'
def run(action, args, context): return {'status': 'success', 'message': reply()}
''')
    (root / 'src/helper.py').write_text("def reply(): return '你好'\n")
    return root


def zip_skill(root, archive, prefix=''):
    with zipfile.ZipFile(archive, 'w') as output:
        for path in root.rglob('*'):
            if path.is_file() and '.git' not in path.parts:
                output.write(path, prefix + path.relative_to(root).as_posix())
    return archive


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.dict(os.environ, {'PRIMEBUDDY_HOME': str(self.root / 'home')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = make_skill(self.root / 'hello')
        self.archive = zip_skill(self.source, self.root / 'hello.zip', 'hello-main/')

    def test_zip_install_configure_and_execute_through_registered_catalog(self):
        installed = manager.install(str(self.archive))
        self.assertFalse(installed['ready'])
        self.assertEqual(Registry().catalog(), [])
        self.assertTrue(manager.configure('hello', DummyUI()))
        registry = Registry()
        self.assertEqual(registry.catalog()[0]['name'], 'hello')
        plan = {'reply': '', 'tasks': [{'id': 'one', 'skill': 'hello', 'action': 'hello', 'args': {}, 'after': []}]}
        result = execute(plan, registry, Context(), lambda _: None)
        self.assertEqual(result['one']['message'], '你好')
        self.assertTrue(installed['sha256'])
        manager.set_enabled('hello', False)
        self.assertEqual(Registry().catalog(), [])
        manager.set_enabled('hello', True)
        self.assertEqual(len(Registry().catalog()), 1)

    def test_first_install_cancel_keeps_files_pending_for_resume(self):
        with patch.object(manager, 'run_setup', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            manager.install(str(self.archive), DummyUI())
        self.assertFalse(manager.records()['hello']['ready'])
        self.assertTrue(manager.package_path(manager.records()['hello']).is_dir())
        self.assertTrue(manager.configure('hello', DummyUI()))

    def test_invalid_runtime_cannot_be_registered(self):
        (self.source / 'src/skill.py').write_text('def run(*args): pass\n')
        zip_skill(self.source, self.archive)
        with self.assertRaises(SetupError):
            manager.install(str(self.archive), DummyUI())
        self.assertEqual(manager.records(), {})

    def test_pending_skill_cannot_be_enabled(self):
        manager.install(str(self.archive))
        with self.assertRaises(SetupError):
            manager.set_enabled('hello', True)

    def test_reconfiguration_cancellation_does_not_claim_ready(self):
        manager.install(str(self.archive), DummyUI())
        with patch.object(manager, 'run_setup', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            manager.configure('hello', DummyUI())
        self.assertFalse(manager.records()['hello']['ready'])
        self.assertEqual(Registry().catalog(), [])
        self.assertTrue(manager.configure('hello', DummyUI()))

    def test_failed_upgrade_preserves_previous_code_and_config(self):
        manager.install(str(self.archive), DummyUI())
        before = manager.records()
        config = home() / 'data/hello/config.json'
        write_json(config, {'keep': True})
        make_skill(self.source, setup="raise RuntimeError('private detail')", version='0.2.0')
        zip_skill(self.source, self.archive)
        with self.assertRaises(Exception):
            manager.install(str(self.archive), DummyUI(), replace=True)
        self.assertEqual(manager.records(), before)
        self.assertEqual(read_json(config), {'keep': True})
        self.assertEqual(len(list((home() / 'packages').iterdir())), 1)
        self.assertEqual(len(Registry().catalog()), 1)

    def test_unconfigured_upgrade_preserves_active_version(self):
        manager.install(str(self.archive), DummyUI())
        before = manager.records()
        with self.assertRaises(SetupError):
            manager.install(str(self.archive), replace=True)
        self.assertEqual(manager.records(), before)

    def test_successful_upgrade_preserves_data_and_removes_old_code(self):
        manager.install(str(self.archive), DummyUI())
        private = home() / 'data/hello/account.json'
        write_json(private, {'fake': 'test-only'})
        make_skill(self.source, version='0.2.0')
        zip_skill(self.source, self.archive)
        manager.install(str(self.archive), DummyUI(), replace=True)
        self.assertEqual(manager.records()['hello']['version'], '0.2.0')
        self.assertEqual(len(list((home() / 'packages').iterdir())), 1)
        self.assertEqual(read_json(private), {'fake': 'test-only'})

    def test_duplicate_install_requires_explicit_replace(self):
        manager.install(str(self.archive))
        with self.assertRaises(SetupError):
            manager.install(str(self.archive))

    def test_dependency_failure_never_registers_or_leaves_staging_directory(self):
        with patch.object(manager, 'install_dependencies', side_effect=SetupError('失败')), self.assertRaises(SetupError):
            manager.install(str(self.archive), DummyUI())
        self.assertEqual(manager.records(), {})
        self.assertEqual(list((home() / 'packages').iterdir()), [])

    def test_zip_path_escape_and_symlink_rejected_before_writes(self):
        for name in ('../outside', '/tmp/outside', 'hello/../../outside', 'hello\\outside'):
            with self.subTest(name=name):
                with zipfile.ZipFile(self.archive, 'w') as output:
                    output.writestr(name, 'bad')
                with self.assertRaises(SetupError):
                    manager.install(str(self.archive))
        with zipfile.ZipFile(self.archive, 'w') as output:
            info = zipfile.ZipInfo('symlink')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            output.writestr(info, '/tmp/outside')
        with self.assertRaises(SetupError):
            manager.install(str(self.archive))
        self.assertFalse((self.root / 'outside').exists())

    def test_bad_manifest_never_executes_setup(self):
        data = json.loads((self.source / 'manifest.json').read_text())
        for key, bad in [('api_version', 2), ('name', '../bad'), ('entrypoint', '../outside.py'),
                         ('dependencies', ['--target=/tmp']), ('dependencies', ['foo @ https://example.com/pkg'])]:
            with self.subTest(key=key):
                (self.source / 'manifest.json').write_text(json.dumps(dict(data, **{key: bad})))
                zip_skill(self.source, self.archive)
                with patch.object(manager, 'run_setup') as setup, self.assertRaises(SetupError):
                    manager.install(str(self.archive), DummyUI())
                setup.assert_not_called()

    def test_https_zip_uses_same_installer(self):
        response = io.BytesIO(self.archive.read_bytes())
        with patch('primebuddy.skill_manager.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            record = manager.install('https://example.com/hello.zip', DummyUI())
        self.assertTrue(record['ready'])
        self.assertEqual(record['source'], 'https://example.com/hello.zip')

    def test_urls_with_embedded_secrets_are_rejected(self):
        for source in ('https://user:secret@example.com/skill.git', 'https://example.com/skill.zip?token=secret', 'http://example.com/skill.zip'):
            with self.subTest(source=source), self.assertRaises(SetupError):
                manager.install(source)

    @unittest.skipUnless(shutil.which('git'), '需要本地 Git')
    def test_git_tag_install_records_exact_commit(self):
        def git(*args):
            return subprocess.run(['git', *args], cwd=self.source, check=True, capture_output=True, text=True).stdout.strip()
        git('init', '-q')
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
        git('tag', 'v0.1.0')
        revision = git('rev-parse', 'HEAD')
        (self.source / 'README.md').write_text('new version')
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'new')
        installed = manager.install(str(self.source), DummyUI(), ref='v0.1.0')
        self.assertEqual(installed['commit'], revision)
        self.assertFalse((manager.package_path(installed) / '.git').exists())
        self.assertEqual((manager.package_path(installed) / 'README.md').read_text(), '测试技能')
        git('branch', 'feature')
        updated = manager.install(str(self.source), DummyUI(), ref='feature', replace=True)
        self.assertEqual(updated['commit'], git('rev-parse', 'feature'))

    def test_atomic_private_json_and_permission_check(self):
        path = home() / 'private.json'
        write_json(path, {'sample': 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(read_json(path), {'sample': 1})
        path.chmod(0o644)
        with self.assertRaises(SetupError):
            read_json(path)

    def test_mutation_lock_prevents_parallel_install(self):
        with manager.installation_lock(), self.assertRaises(SetupError):
            manager.install(str(self.archive))

    def test_source_simulation_never_creates_installation_configuration(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        result = subprocess.run([sys.executable, '-m', 'primebuddy', 'simulate', '--skills-dir', str(ROOT.parent / 'primebuddy-skills'),
                                 '--plan', str(ROOT / 'examples/parallel.json')], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('模拟', result.stdout)
        self.assertFalse(home().exists())

    def test_cli_noninteractive_install_stays_pending(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        result = subprocess.run([sys.executable, '-m', 'primebuddy', 'skill', 'install', str(self.archive), '--no-configure'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(manager.records()['hello']['ready'])
        result = subprocess.run([sys.executable, '-m', 'primebuddy', 'skill', 'configure', 'hello'], env=env, input='', capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('交互终端', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_singular_skill_command_installs_and_lists(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        for args in (['skill', 'install', str(self.archive), '--no-configure'], ['skill', 'list']):
            result = subprocess.run([sys.executable, '-m', 'primebuddy', *args], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('hello', result.stdout)
        self.assertFalse(manager.records()['hello']['ready'])


class ModelTests(unittest.TestCase):
    def test_host_validation_and_single_credentials_for_three_roles(self):
        from primebuddy.models import qianwen_config
        config = qianwen_config('https://demo.cn-beijing.maas.aliyuncs.com/', 'test-key')
        self.assertEqual(config['api_key'], 'test-key')
        self.assertTrue(config['endpoint'].startswith('https://demo.'))
        self.assertEqual(config['tts_model'], 'qwen-audio-3.0-tts-plus')
        self.assertEqual(config['tts_voice'], 'longanlingxin')
        self.assertEqual(config['tts_endpoint'],
                         'https://demo.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer')
        for host in ('http://dashscope.aliyuncs.com', 'attacker.example', 'dashscope.aliyuncs.com/path'):
            with self.assertRaises(SetupError):
                qianwen_config(host, 'test-key')
        with self.assertRaises(SetupError):
            qianwen_config('dashscope.aliyuncs.com', '')

    def test_three_capability_checks_report_independent_results_without_secrets(self):
        from primebuddy.models import check_model, qianwen_config
        with patch('primebuddy.voice_support.qwen_asr.recognize', side_effect=RuntimeError('secret')), patch('primebuddy.planner.plan') as llm, patch('primebuddy.voice_support.qwen_tts.synthesize_wav') as tts:
            result = check_model(qianwen_config('dashscope.aliyuncs.com', 'test-only'))
        self.assertIn('RuntimeError', result['ASR'])
        self.assertNotIn('secret', str(result))
        self.assertEqual(result['LLM'], 'ok')
        self.assertEqual(result['TTS'], 'ok')
        llm.assert_called_once()
        tts.assert_called_once()

    def test_saved_model_is_used_by_legacy_voice_entry(self):
        from primebuddy.models import qianwen_config
        from primebuddy.voice_support.qwen_asr import load_config
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PRIMEBUDDY_HOME': temp}):
            config = qianwen_config('dashscope.aliyuncs.com', 'test-only')
            write_json(home() / 'model.json', config)
            self.assertEqual(load_config(), config)

    def test_upgrade_refreshes_tts_presets_without_changing_saved_credentials(self):
        from primebuddy.models import load_model, qianwen_config
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PRIMEBUDDY_HOME': temp}):
            config = qianwen_config('demo.cn-beijing.maas.aliyuncs.com', 'test-only')
            saved = dict(config, tts_model='qwen3-tts-flash', tts_voice='Cherry',
                         tts_endpoint='https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation')
            path = home() / 'model.json'
            write_json(path, saved)
            before = path.read_bytes()
            current = load_model()
            self.assertEqual(current['tts_model'], 'qwen-audio-3.0-tts-plus')
            self.assertEqual(current, config)
            self.assertEqual(path.read_bytes(), before)


class ReleaseTests(unittest.TestCase):
    def test_release_onboard_upgrades_bundle_and_preserves_skill_configuration(self):
        spec = importlib.util.spec_from_file_location('build_upgrade_test', ROOT / 'tools/build_release.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            old = zip_skill(make_skill(directory / 'old', setup="return {'keep': 1}"), directory / 'old.zip')
            new = make_skill(directory / 'new', setup="return dict(config, revision=2)", version='0.2.0')
            archive = directory / 'release.zip'
            builder.build(archive, [new])
            with zipfile.ZipFile(archive) as package:
                package.extractall(directory / 'unpacked')
            env = dict(os.environ, PRIMEBUDDY_HOME=str(directory / 'data'),
                       PYTHONPATH=str(directory / 'unpacked/primebuddy/src'))
            script = '''import sys
from primebuddy import skill_manager as skills
from primebuddy.onboarding import onboard
from primebuddy.settings import skill_config
class UI:
    def info(self, message): pass
    def header(self, *args): pass
    def confirm(self, *args, **kwargs): return False
    def multiselect(self, label, choices, default=None): return default
ui = UI()
skills.install(sys.argv[1], ui)
onboard(ui)
assert skills.records()['hello']['version'] == '0.2.0'
assert skills.records()['hello']['ready'] and skills.records()['hello']['enabled']
assert skill_config('hello') == {'keep': 1, 'revision': 2}
print('BUNDLE_UPGRADE_OK')
'''
            result = subprocess.run([sys.executable, '-c', script, str(old)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('BUNDLE_UPGRADE_OK', result.stdout)

    def test_installer_rejects_virtualenv_before_writing(self):
        spec = importlib.util.spec_from_file_location('installer_test', ROOT / 'install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as temp, patch.object(sys, 'prefix', sys.base_prefix + '/venv'):
            prefix = Path(temp) / 'app'
            with self.assertRaisesRegex(RuntimeError, '系统 python3'):
                installer.install(prefix, Path(temp) / 'bin')
            self.assertFalse(prefix.exists())

    def test_release_extract_install_command_and_bundled_skill(self):
        spec = importlib.util.spec_from_file_location('build_release_test', ROOT / 'tools/build_release.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            skill = make_skill(directory / 'hello')
            # 私有文件即使误放在项目根目录，也不会进入发布清单。
            (skill / 'credential.json').write_text('do-not-ship')
            archive = directory / 'release.zip'
            builder.build(archive, [skill])
            with zipfile.ZipFile(archive) as package:
                package.extractall(directory / 'unpacked')
                self.assertEqual({name.split('/')[0] for name in package.namelist()}, {'primebuddy'})
                self.assertIn('primebuddy/bundled/hello.zip', package.namelist())
                self.assertIn('primebuddy/docs/sdk-compatibility.md', package.namelist())
                bundled = io.BytesIO(package.read('primebuddy/bundled/hello.zip'))
            with zipfile.ZipFile(bundled) as package:
                self.assertNotIn('credential.json', package.namelist())
            prefix = directory / 'install with space'
            bindir = directory / 'bin with space'
            app = directory / 'unpacked/primebuddy'
            shell_home = directory / 'shell home'
            shell_home.mkdir()
            env = dict(os.environ, HOME=str(shell_home), SHELL='/bin/bash')
            env.pop('PRIMEBUDDY_HOME', None)
            env.pop('PYTHONPATH', None)
            result = subprocess.run([sys.executable, str(app / 'install.py'),
                                     '--data-dir', str(prefix), '--bin-dir', str(bindir), '--no-onboard'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((prefix / 'venv').exists())
            self.assertFalse((prefix / 'app').exists())
            self.assertEqual((bindir / 'primebuddy').resolve(), (app / 'primebuddy').resolve())
            self.assertIn(sys.executable, (bindir / 'primebuddy').read_text())
            launcher = str(bindir / 'primebuddy')
            result = subprocess.run([launcher, '--version'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('PrimeBuddy ' + __version__, result.stdout)
            result = subprocess.run([launcher, 'skill', 'install', str(app / 'bundled/hello.zip'), '--no-configure'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(read_json(prefix / 'skills.json')['hello']['ready'])
            # 从新终端启动时无需再手动给 PATH 加目录。
            result = subprocess.run(['/bin/bash', '--noprofile', '--rcfile', str(shell_home / '.bashrc'),
                                     '-ic', 'primebuddy --version'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_sourced_installer_updates_current_shell_and_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            app = directory / "app with space 'quote'"
            shutil.copytree(ROOT, app, ignore=shutil.ignore_patterns('dist', '__pycache__'))
            shell_home = directory / 'home'
            shell_home.mkdir()
            (shell_home / '.bashrc').write_text('# Keep my settings\n')
            env = dict(os.environ, HOME=str(shell_home), SHELL='/bin/bash', PATH='/usr/bin:/bin', ASSISTANT_PYTHON=sys.executable)
            env.pop('PRIMEBUDDY_HOME', None)
            env.pop('PYTHONPATH', None)
            data = shell_home / '.local/share/primebuddy'
            write_json(data / 'model.json', {'provider': 'existing-test-value'})
            shells = [('/bin/bash', ['--noprofile', '--norc'])]
            if shutil.which('zsh'):
                shells.append((shutil.which('zsh'), ['-f']))
            for shell, flags in shells:
                for _ in range(2):
                    current_env = dict(env, SHELL=shell, ZDOTDIR=str(shell_home))
                    result = subprocess.run([shell, *flags, '-c',
                        'source "$1/install.sh" --no-onboard && primebuddy --version && primebuddy skill list',
                        'installer-test', str(app)], cwd=directory, env=current_env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('PrimeBuddy ' + __version__, result.stdout)
            self.assertEqual(read_json(data / 'model.json'), {'provider': 'existing-test-value'})
            startup = (shell_home / '.bashrc').read_text()
            self.assertTrue(startup.startswith('# Keep my settings\n'))
            self.assertEqual(startup.count('# >>> PrimeBuddy PATH >>>'), 1)
            self.assertFalse((data / 'app').exists())


class OnboardingTests(unittest.TestCase):
    def test_model_retry_preserves_secret_without_displaying_it(self):
        from unittest.mock import Mock
        from primebuddy.models import configure_model
        ui = Mock()
        ui.ask.side_effect = ['dashscope.aliyuncs.com', 'secret-for-test', 'dashscope.aliyuncs.com', 'secret-for-test']
        ui.confirm.return_value = True
        ui.choose.return_value = 0
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PRIMEBUDDY_HOME': temp}), patch('primebuddy.models.check_model', side_effect=[{'ASR': 'failed', 'LLM': 'ok', 'TTS': 'ok'}, {'ASR': 'ok', 'LLM': 'ok', 'TTS': 'ok'}]):
            configure_model(ui)
            saved = read_json(home() / 'model.json')
            self.assertEqual(saved['checks'], {'ASR': 'ok', 'LLM': 'ok', 'TTS': 'ok'})
        self.assertNotIn('secret-for-test', str(ui.info.call_args_list))
        self.assertTrue(ui.ask.call_args_list[3].kwargs['secret'])
        self.assertEqual(ui.ask.call_args_list[3].args[1], 'secret-for-test')

    def test_skip_cloud_verification_is_explicitly_recorded(self):
        from unittest.mock import Mock
        from primebuddy.models import configure_model
        ui = Mock()
        ui.ask.side_effect = ['dashscope.aliyuncs.com', 'secret-for-test']
        ui.confirm.return_value = False
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PRIMEBUDDY_HOME': temp}), patch('primebuddy.models.check_model') as check:
            configure_model(ui)
            self.assertEqual(read_json(home() / 'model.json')['checks'], {})
        check.assert_not_called()
        self.assertIn('待验证', str(ui.info.call_args_list))

    def test_reenter_onboard_reconfigures_existing_bundle_without_installing_again(self):
        from unittest.mock import Mock
        from primebuddy.onboarding import onboard
        ui = Mock()
        ui.confirm.return_value = False
        ui.multiselect.return_value = [0]
        bundles = {'hello': {'path': Path('hello.zip'), 'version': '0.1.0'}}
        with patch('primebuddy.onboarding.bundled_skills', return_value=bundles), patch('primebuddy.onboarding.records', return_value={'hello': {'ready': True, 'version': '0.1.0'}}), patch('primebuddy.onboarding.install') as install, patch('primebuddy.onboarding.configure') as configure:
            onboard(ui)
        install.assert_not_called()
        configure.assert_called_once_with('hello', ui)

    def test_skipped_bundle_stays_uninstalled_and_does_not_disable_existing(self):
        from unittest.mock import Mock
        from primebuddy.onboarding import onboard
        ui = Mock()
        ui.confirm.return_value = False
        ui.multiselect.return_value = []
        bundles = {'hello': {'path': Path('hello.zip'), 'version': '0.1.0'}}
        with patch('primebuddy.onboarding.bundled_skills', return_value=bundles), \
                patch('primebuddy.onboarding.records', return_value={'old': {'ready': True}}), \
                patch('primebuddy.onboarding.install') as install, patch('primebuddy.onboarding.configure') as configure:
            onboard(ui)
        install.assert_not_called()
        configure.assert_not_called()
        self.assertEqual(ui.multiselect.call_args.kwargs['default'], [0])

    def test_newer_bundle_is_selected_for_upgrade_and_calls_its_own_setup(self):
        from unittest.mock import Mock
        from primebuddy.onboarding import onboard
        ui = Mock()
        ui.confirm.return_value = False
        ui.multiselect.return_value = [0]
        bundles = {'hello': {'path': Path('hello.zip'), 'version': '0.2.10'}}
        with patch('primebuddy.onboarding.bundled_skills', return_value=bundles), \
                patch('primebuddy.onboarding.records', return_value={'hello': {'ready': True, 'version': '0.2.9'}}), \
                patch('primebuddy.onboarding.install', return_value={'ready': True}) as install, \
                patch('primebuddy.onboarding.configure') as configure:
            onboard(ui)
        install.assert_called_once_with('hello.zip', ui, replace=True)
        configure.assert_not_called()
        self.assertEqual(ui.multiselect.call_args.kwargs['default'], [0])
        self.assertIn('更新至 0.2.10', ui.multiselect.call_args.args[1][0])

    def test_bundle_does_not_downgrade_newer_installed_skill(self):
        from unittest.mock import Mock
        from primebuddy.onboarding import onboard
        ui = Mock()
        ui.confirm.return_value = False
        ui.multiselect.return_value = [0]
        bundles = {'hello': {'path': Path('hello.zip'), 'version': '0.1.0'}}
        with patch('primebuddy.onboarding.bundled_skills', return_value=bundles), \
                patch('primebuddy.onboarding.records', return_value={'hello': {'ready': True, 'version': '0.2.0'}}), \
                patch('primebuddy.onboarding.install') as install, patch('primebuddy.onboarding.configure') as configure:
            onboard(ui)
        install.assert_not_called()
        configure.assert_called_once_with('hello', ui)
        self.assertEqual(ui.multiselect.call_args.kwargs['default'], [])

    def test_unchecked_upgrade_leaves_installed_skill_untouched(self):
        from unittest.mock import Mock
        from primebuddy.onboarding import onboard
        ui = Mock()
        ui.confirm.return_value = False
        ui.multiselect.return_value = []
        bundles = {'hello': {'path': Path('hello.zip'), 'version': '0.2.0'}}
        with patch('primebuddy.onboarding.bundled_skills', return_value=bundles), \
                patch('primebuddy.onboarding.records', return_value={'hello': {'ready': True, 'version': '0.1.0'}}), \
                patch('primebuddy.onboarding.install') as install, patch('primebuddy.onboarding.configure') as configure:
            onboard(ui)
        self.assertEqual(ui.multiselect.call_args.kwargs['default'], [0])
        install.assert_not_called()
        configure.assert_not_called()

    def test_bundle_versions_are_read_from_the_release_manifest(self):
        from primebuddy.onboarding import bundled_skills
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'bundled').mkdir()
            archive = root / 'bundled/hello.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('manifest.json', json.dumps({'name': 'hello', 'version': '0.2.10'}))
            with patch('primebuddy.onboarding.__file__', str(root / 'src/primebuddy/onboarding.py')):
                self.assertEqual(bundled_skills(), {'hello': {'path': archive.resolve(), 'version': '0.2.10'}})

    def test_invalid_bundled_metadata_is_reported_without_installing(self):
        from primebuddy.onboarding import bundled_skills
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'bundled').mkdir()
            archive = root / 'bundled/hello.zip'
            for raw in ('not json', '[]', '{"name":"../bad","version":"0.1.0"}',
                        '{"name":"hello","version":"latest"}', ' ' * 16385):
                with self.subTest(raw=raw[:60]):
                    with zipfile.ZipFile(archive, 'w') as output:
                        output.writestr('manifest.json', raw)
                    with patch('primebuddy.onboarding.__file__', str(root / 'src/primebuddy/onboarding.py')), \
                            self.assertRaisesRegex(SetupError, '随包技能清单无效'):
                        bundled_skills()
