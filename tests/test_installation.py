"""安装协议的端到端测试：临时 ZIP/Git、状态注册、回滚与异常输入。"""
import importlib.util
import fcntl
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

from primebuddy import __version__
from primebuddy.skills import manager as manager
from primebuddy.core.engine import Context, execute
from primebuddy.skills.registry import Registry
from primebuddy.config.storage import SetupError, get_data_directory, config_path, read_config, write_config

ROOT = Path(__file__).resolve().parents[1]


class DummyUI:
    def info(self, text):
        pass


def make_skill(root, setup='return {}', version='0.1.0', name='hello'):
    for directory in ('src', 'cfg', 'docs'):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / 'cfg/pkg.yaml').write_text(json.dumps({
        'user_id': 'robot_local', 'version': version, 'sdk_version': 'v1.0.0',
        'package_name': name, 'author': 'test-only', 'description': '测试问候',
        'primebuddy': {'api_version': 1, 'entrypoint': 'src/skill.py', 'setup': 'src/setup.py',
                      'actions': {'hello': {'parameters': {}, 'resources': []}}}}))
    (root / 'requirements.txt').write_text('# No third-party dependencies.\n')
    (root / 'SKILL.md').write_text('---\nname: ' + name + '\ndescription: 测试问候\n---\n调用 hello 问候用户。')
    (root / 'README.md').write_text('测试技能')
    (root / 'src/setup.py').write_text('def setup(ui, data, config):\n    ' + setup + '\n')
    (root / 'src/skill.py').write_text('''from .helper import reply

def hello(context): return {'status': 'success', 'message': reply()}
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
        self.assertEqual(installed['path'], 'hello')
        self.assertEqual(manager.get_skill_package_directory(installed), get_data_directory() / 'skills/hello')
        self.assertFalse((get_data_directory() / 'packages').exists())
        self.assertFalse(installed['ready'])
        self.assertEqual(Registry().catalog(), [])
        self.assertTrue(manager.configure('hello', DummyUI()))
        registry = Registry()
        self.assertEqual(registry.catalog()[0]['name'], 'hello')
        plan = {'reply': '', 'tasks': [{'id': 'one', 'skill': 'hello', 'action': 'hello', 'args': {}, 'after': []}]}
        result = execute(plan, registry, Context(), lambda _: None)
        self.assertEqual(result['one']['message'], '你好')
        self.assertEqual(set(installed), {'path', 'version', 'ready', 'enabled'})
        manager.set_enabled('hello', False)
        self.assertEqual(Registry().catalog(), [])
        manager.set_enabled('hello', True)
        self.assertEqual(len(Registry().catalog()), 1)

    def test_first_install_cancel_keeps_files_pending_for_resume(self):
        with patch.object(manager, 'run_setup', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            manager.install(str(self.archive), DummyUI())
        self.assertFalse(manager.load_skill_records()['hello']['ready'])
        self.assertTrue(manager.get_skill_package_directory(manager.load_skill_records()['hello']).is_dir())
        self.assertTrue(manager.configure('hello', DummyUI()))

    def test_install_does_not_import_business_and_missing_handler_is_rejected_before_execution(self):
        (self.source / 'src/skill.py').write_text('def run(*args): pass\n')
        zip_skill(self.source, self.archive)
        manager.install(str(self.archive), DummyUI())
        proposal = {'reply': '', 'tasks': [{'id': 'one', 'skill': 'hello', 'action': 'hello', 'args': {}, 'after': []}]}
        with self.assertRaises(SetupError):
            execute(proposal, Registry(), Context(), lambda _: self.fail('无效工具不应播报执行提示'))

    def test_pending_skill_cannot_be_enabled(self):
        manager.install(str(self.archive))
        with self.assertRaises(SetupError):
            manager.set_enabled('hello', True)

    def test_reconfiguration_cancellation_does_not_claim_ready(self):
        manager.install(str(self.archive), DummyUI())
        with patch.object(manager, 'run_setup', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            manager.configure('hello', DummyUI())
        self.assertFalse(manager.load_skill_records()['hello']['ready'])
        self.assertEqual(Registry().catalog(), [])
        self.assertTrue(manager.configure('hello', DummyUI()))

    def test_failed_upgrade_preserves_previous_code_and_config(self):
        manager.install(str(self.archive), DummyUI())
        before = manager.load_skill_records()
        config = get_data_directory() / 'cfg/skills/hello/config.yaml'
        write_config(config, {'keep': True})
        make_skill(self.source, setup="raise RuntimeError('private detail')", version='0.2.0')
        zip_skill(self.source, self.archive)
        with self.assertRaises(Exception):
            manager.install(str(self.archive), DummyUI(), replace=True)
        self.assertEqual(manager.load_skill_records(), before)
        self.assertEqual(read_config(config), {'keep': True})
        self.assertEqual(list((get_data_directory() / 'skills').iterdir()), [get_data_directory() / 'skills/hello'])
        self.assertEqual((get_data_directory() / 'skills/hello/src/setup.py').read_text(), 'def setup(ui, data, config):\n    return {}\n')
        self.assertEqual(len(Registry().catalog()), 1)

    def test_unconfigured_upgrade_preserves_active_version(self):
        manager.install(str(self.archive), DummyUI())
        before = manager.load_skill_records()
        with self.assertRaises(SetupError):
            manager.install(str(self.archive), replace=True)
        self.assertEqual(manager.load_skill_records(), before)

    def test_successful_upgrade_preserves_data_and_removes_old_code(self):
        manager.install(str(self.archive), DummyUI())
        private = get_data_directory() / 'data/hello/account.yaml'
        write_config(private, {'fake': 'test-only'})
        make_skill(self.source, version='0.2.0')
        zip_skill(self.source, self.archive)
        manager.install(str(self.archive), DummyUI(), replace=True)
        self.assertEqual(manager.load_skill_records()['hello']['version'], '0.2.0')
        self.assertEqual(list((get_data_directory() / 'skills').iterdir()), [get_data_directory() / 'skills/hello'])
        self.assertEqual(read_config(private), {'fake': 'test-only'})

    def test_duplicate_install_requires_explicit_replace(self):
        manager.install(str(self.archive))
        with self.assertRaises(SetupError):
            manager.install(str(self.archive))

    def test_dependency_failure_never_registers_or_leaves_staging_directory(self):
        with patch.object(manager, 'install_dependencies', side_effect=SetupError('失败')), self.assertRaises(SetupError):
            manager.install(str(self.archive), DummyUI())
        self.assertEqual(manager.load_skill_records(), {})
        self.assertEqual(list((get_data_directory() / 'skills').iterdir()), [])

    def test_registry_save_failure_restores_code_config_and_index(self):
        manager.install(str(self.archive), DummyUI())
        before = manager.load_skill_records()
        config = get_data_directory() / 'cfg/skills/hello/config.yaml'
        write_config(config, {'keep': 1})
        make_skill(self.source, setup="return {'keep': 2}", version='0.2.0')
        zip_skill(self.source, self.archive)
        save = manager.write_config
        def fail_index(path, value):
            if path.name == 'skills.yaml':
                raise OSError('fixture: failed index write')
            save(path, value)
        with patch.object(manager, 'write_config', side_effect=fail_index), self.assertRaises(OSError):
            manager.install(str(self.archive), DummyUI(), replace=True)
        self.assertEqual(manager.load_skill_records(), before)
        self.assertEqual(read_config(config), {'keep': 1})
        self.assertEqual(Registry().skills['hello']['version'], '0.1.0')
        self.assertEqual(list((get_data_directory() / 'skills').iterdir()), [get_data_directory() / 'skills/hello'])

    def test_skill_list_shows_persisted_disabled_skill_and_directory(self):
        manager.install(str(self.archive), DummyUI())
        manager.set_enabled('hello', False)
        # 用新进程确认列表来自磁盘，同时不向 LLM 暴露已停用技能。
        result = subprocess.run([sys.executable, '-m', 'primebuddy', 'skill', 'list'],
            env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ('hello', '0.1.0', '已停用', str(get_data_directory() / 'skills/hello')):
            self.assertIn(text, result.stdout)
        self.assertEqual(Registry().catalog(), [])

    def test_existing_unrelated_directory_is_never_overwritten(self):
        destination = get_data_directory() / 'skills/hello'
        destination.mkdir(parents=True)
        (destination / 'keep.txt').write_text('keep')
        with self.assertRaises(SetupError):
            manager.install(str(self.archive), DummyUI(), replace=True)
        self.assertEqual((destination / 'keep.txt').read_text(), 'keep')

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

    def test_invalid_metadata_or_requirements_never_executes_setup(self):
        data = json.loads((self.source / 'cfg/pkg.yaml').read_text())
        cases = [dict(data, package_name='../bad'),
                 dict(data, primebuddy=dict(data['primebuddy'], api_version=2)),
                 dict(data, primebuddy=dict(data['primebuddy'], entrypoint='../outside.py'))]
        for bad in cases:
            with self.subTest(metadata=bad):
                (self.source / 'cfg/pkg.yaml').write_text(json.dumps(bad))
                zip_skill(self.source, self.archive)
                with patch.object(manager, 'run_setup') as setup, self.assertRaises(SetupError):
                    manager.install(str(self.archive), DummyUI())
                setup.assert_not_called()
        (self.source / 'cfg/pkg.yaml').write_text(json.dumps(data))
        for requirements in ('--target=/tmp', 'foo @ https://example.com/pkg'):
            with self.subTest(requirements=requirements):
                (self.source / 'requirements.txt').write_text(requirements)
                zip_skill(self.source, self.archive)
                with patch.object(manager, 'run_setup') as setup, self.assertRaises(SetupError):
                    manager.install(str(self.archive), DummyUI())
                setup.assert_not_called()

    def test_https_zip_uses_same_installer(self):
        response = io.BytesIO(self.archive.read_bytes())
        with patch('primebuddy.skills.manager.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            record = manager.install('https://example.com/hello.zip', DummyUI())
        self.assertTrue(record['ready'])
        self.assertEqual(set(record), {'path', 'version', 'ready', 'enabled'})
        self.assertNotIn('https://example.com/hello.zip', config_path('skills.yaml').read_text())

    def test_local_and_https_rpk_use_zip_installation_and_configuration(self):
        archive = self.root / 'hello-0.1.0.rpk'
        shutil.copyfile(self.archive, archive)
        record = manager.install(archive, DummyUI())
        self.assertTrue(record['ready'])
        self.assertTrue((manager.get_skill_package_directory(record) / 'SKILL.md').is_file())
        with patch('primebuddy.skills.manager.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(archive.read_bytes())
            updated = manager.install('https://example.com/hello-0.1.0.rpk', DummyUI(), replace=True)
        self.assertTrue(updated['ready'])
        self.assertEqual(updated['version'], '0.1.0')

    def test_saved_skill_state_removes_old_installation_origin_fields(self):
        record = manager.install(str(self.archive), DummyUI())
        record.update(source='https://example.com/hello.zip', type='zip', ref='v0.1.0',
                      commit='test-only-revision', sha256='test-only-fingerprint')
        write_config(config_path('skills.yaml'), {'hello': record})
        manager.set_enabled('hello', False)
        saved = read_config(config_path('skills.yaml'))['hello']
        self.assertEqual(saved, {'path': 'hello', 'version': '0.1.0', 'ready': True, 'enabled': False})

    def test_urls_with_embedded_secrets_are_rejected(self):
        for source in ('https://user:secret@example.com/skill.git', 'https://example.com/skill.zip?token=secret', 'http://example.com/skill.zip'):
            with self.subTest(source=source), self.assertRaises(SetupError):
                manager.install(source)

    @unittest.skipUnless(shutil.which('git'), '需要本地 Git')
    def test_git_ref_installs_requested_files_without_origin_records(self):
        def git(*args):
            return subprocess.run(['git', *args], cwd=self.source, check=True, capture_output=True, text=True).stdout.strip()
        git('init', '-q')
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
        git('tag', 'v0.1.0')
        (self.source / 'README.md').write_text('new version')
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'new')
        installed = manager.install(str(self.source), DummyUI(), ref='v0.1.0')
        self.assertEqual(set(installed), {'path', 'version', 'ready', 'enabled'})
        self.assertFalse((manager.get_skill_package_directory(installed) / '.git').exists())
        self.assertEqual((manager.get_skill_package_directory(installed) / 'README.md').read_text(), '测试技能')
        git('branch', 'feature')
        updated = manager.install(str(self.source), DummyUI(), ref='feature', replace=True)
        self.assertEqual(set(updated), {'path', 'version', 'ready', 'enabled'})
        self.assertEqual((manager.get_skill_package_directory(updated) / 'README.md').read_text(), 'new version')

    def test_atomic_private_yaml_and_permission_check(self):
        path = get_data_directory() / 'private.yaml'
        write_config(path, {'sample': 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(read_config(path), {'sample': 1})
        path.chmod(0o644)
        with self.assertRaises(SetupError):
            read_config(path)

    def test_mutation_lock_prevents_parallel_install(self):
        with manager.installation_lock(), self.assertRaises(SetupError):
            manager.install(str(self.archive))

    def test_source_simulation_never_creates_installation_configuration(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        result = subprocess.run([sys.executable, '-m', 'primebuddy', 'simulate', '--skills-dir', str(ROOT.parent / 'primebuddy-skills'),
                                 '--plan', str(ROOT / 'tests/fixtures/parallel.json')], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('模拟', result.stdout)
        self.assertFalse(get_data_directory().exists())

    def test_cli_noninteractive_install_stays_pending(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        result = subprocess.run([sys.executable, '-m', 'primebuddy', 'skill', 'install', str(self.archive), '--no-configure'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(manager.load_skill_records()['hello']['ready'])
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
        self.assertFalse(manager.load_skill_records()['hello']['ready'])


class ModelTests(unittest.TestCase):
    def test_provider_host_validation_and_role_specific_configurations(self):
        from primebuddy.providers.qianwen import build_config
        from primebuddy.config.models import provider_models
        settings = {'api_host': 'https://demo.cn-beijing.maas.aliyuncs.com/', 'api_key': 'test-key'}
        models = provider_models('qianwen', settings)
        for role in ('asr', 'llm', 'tts'):
            config = build_config(role, models[role][0], settings)
            self.assertEqual(config['role'], role)
            self.assertEqual(config['api_key'], 'test-key')
            self.assertTrue(config['endpoint'].startswith('https://demo.'))
        self.assertEqual(config['voice'], 'longanlingxin')
        self.assertTrue(config['endpoint'].endswith('/audio/tts/SpeechSynthesizer'))
        for host in ('http://dashscope.aliyuncs.com', 'attacker.example', 'dashscope.aliyuncs.com/path'):
            with self.assertRaises(SetupError):
                build_config('asr', models['asr'][0], dict(settings, api_host=host))
        with self.assertRaises(SetupError):
            build_config('asr', models['asr'][0], dict(settings, api_key=''))

    def test_three_capability_checks_report_independent_results_without_secrets(self):
        from primebuddy.config.models import check_model
        from primebuddy.config.storage import load_app_config
        with patch('primebuddy.providers.qianwen.asr.recognize', side_effect=RuntimeError('secret')), \
                patch('primebuddy.providers.qianwen.llm.complete') as llm, patch('primebuddy.providers.qianwen.tts.synthesize_wav') as tts:
            result = check_model(load_app_config(), {'qianwen': {'api_host': 'dashscope.aliyuncs.com', 'api_key': 'test-only'}})
        self.assertIn('RuntimeError', result['ASR'])
        self.assertNotIn('secret', str(result))
        self.assertEqual(result['LLM'], 'ok')
        self.assertEqual(result['TTS'], 'ok')
        llm.assert_called_once()
        tts.assert_called_once()

    def test_old_model_file_migrates_to_selection_and_provider_files(self):
        from primebuddy.config.models import load_model
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, PRIMEBUDDY_HOME=temp):
            old = get_data_directory() / 'cfg/model.yaml'
            write_config(old, {'provider': 'qianwen', 'api_host': 'dashscope.aliyuncs.com', 'api_key': 'test-only',
                               'model': 'qwen-audio-3.1-asr-flash', 'tts_voice': 'Cherry'})
            self.assertEqual(load_model('asr')['model'], 'qwen-audio-3.1-asr-flash')
            config = read_config(get_data_directory() / 'cfg/config.yaml')
            self.assertEqual(config['asr']['model'], 'qianwen:qwen-audio-3.1-asr-flash')
            self.assertEqual(config['tts']['tts_voice'], 'Cherry')
            self.assertEqual(load_model('tts')['voice'], 'Cherry')
            self.assertNotIn('api_key', str(config))
            settings = read_config(get_data_directory() / 'cfg/providers.yaml')['qianwen']
            self.assertEqual(settings['api_key'], 'test-only')
            self.assertNotIn('tts_voice', settings)
            self.assertFalse(old.exists())

    def test_manual_model_and_provider_changes_are_used_without_rewriting_files(self):
        from primebuddy.config.models import load_model
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, PRIMEBUDDY_HOME=temp):
            config_file, providers_file = get_data_directory() / 'cfg/config.yaml', get_data_directory() / 'cfg/providers.yaml'
            write_config(config_file, {'asr': 'qianwen:qwen-audio-3.1-asr-flash', 'tts': {'tts_voice': 'Cherry'}})
            write_config(providers_file, {'qianwen': {'api_host': 'demo.cn-beijing.maas.aliyuncs.com', 'api_key': 'test-only'}})
            before = config_file.read_bytes(), providers_file.read_bytes()
            self.assertEqual(load_model('asr')['model'], 'qwen-audio-3.1-asr-flash')
            self.assertEqual(load_model('asr', 'qwen3-asr-flash')['model'], 'qwen3-asr-flash')
            self.assertEqual(load_model('asr', 'qianwen:qwen3-asr-flash')['model'], 'qwen3-asr-flash')
            self.assertEqual(load_model('tts')['voice'], 'Cherry')
            self.assertTrue(load_model('llm')['endpoint'].startswith('https://demo.'))
            self.assertEqual((config_file.read_bytes(), providers_file.read_bytes()), before)


class ReleaseTests(unittest.TestCase):
    def installer(self):
        spec = importlib.util.spec_from_file_location('installer_dependency_test', ROOT / 'src/primebuddy/tools/install.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_cli_without_arguments_prints_help_without_building(self):
        from primebuddy.tools import build_release as builder
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'primebuddy'
            with patch.object(builder, 'ROOT', source), \
                    patch.object(sys, 'argv', ['build_release.py']), \
                    patch.object(sys, 'stdout', io.StringIO()) as stdout:
                builder.main()
            self.assertIn('--framework', stdout.getvalue())
            self.assertIn('--skill', stdout.getvalue())
            self.assertFalse(source.exists())

    def test_explicit_cli_builds_versioned_rpks_and_only_bundles_selected_skills(self):
        from primebuddy.tools import build_release as builder
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / 'primebuddy'
            self.installer().stage_application(ROOT, source)
            collection = directory / 'primebuddy-skills'
            make_skill(collection / 'hello')
            make_skill(collection / 'other', version='0.2.0', name='other')
            make_skill(collection / 'unselected', name='unselected')
            (source / 'design').mkdir()
            (source / 'design/private.md').write_text('do-not-ship')
            old_bundle = source / 'resource/bundled/obsolete.zip'
            old_bundle.parent.mkdir(parents=True)
            old_bundle.write_bytes(b'old package must not be copied')
            output = directory / 'release'
            with patch.object(builder, 'ROOT', source), \
                    patch.object(sys, 'argv', ['build_release.py', '--framework',
                        '--skill', str(collection / 'hello'), '--skill', str(collection / 'other'),
                        '--output-dir', str(output)]), \
                    patch.object(sys, 'stdout', io.StringIO()):
                builder.main()
            names = {path.name for path in output.iterdir()}
            self.assertEqual(names, {f'primebuddy-{__version__}.rpk', 'hello-0.1.0.rpk', 'other-0.2.0.rpk'})
            for path in output.iterdir():
                self.assertTrue(zipfile.is_zipfile(path))
                with zipfile.ZipFile(path) as package:
                    self.assertIsNone(package.testzip())
                    self.assertFalse(any('design' in Path(name).parts for name in package.namelist()))
            with zipfile.ZipFile(output / f'primebuddy-{__version__}.rpk') as framework:
                self.assertEqual(framework.read('primebuddy/LICENSE'), (ROOT / 'LICENSE').read_bytes())
                self.assertEqual({name for name in framework.namelist() if name.startswith('primebuddy/resource/bundled/')},
                    {'primebuddy/resource/bundled/hello-0.1.0.rpk', 'primebuddy/resource/bundled/other-0.2.0.rpk'})
                for filename in ('hello-0.1.0.rpk', 'other-0.2.0.rpk'):
                    self.assertEqual(framework.read('primebuddy/resource/bundled/' + filename), (output / filename).read_bytes())

    def test_single_skill_cli_preserves_existing_rpk_unless_overwrite_is_requested(self):
        from primebuddy.tools import build_release as builder
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            skill = make_skill(directory / 'hello')
            output = directory / 'release'
            arguments = ['build_release.py', '--skill', str(skill), '--output-dir', str(output)]
            with patch.object(builder, 'ROOT', directory / 'framework-without-skills'), \
                    patch.object(sys, 'argv', arguments), patch.object(sys, 'stdout', io.StringIO()):
                builder.main()
            archive = output / 'hello-0.1.0.rpk'
            before = archive.read_bytes()
            (skill / 'README.md').write_text('updated package')
            with patch.object(sys, 'argv', arguments), patch.object(sys, 'stderr', io.StringIO()), \
                    self.assertRaises(SystemExit):
                builder.main()
            self.assertEqual(archive.read_bytes(), before)
            with patch.object(sys, 'argv', [*arguments, '--overwrite']), patch.object(sys, 'stdout', io.StringIO()):
                builder.main()
            with zipfile.ZipFile(archive) as package:
                self.assertEqual(package.read('README.md').decode(), 'updated package')
            self.assertEqual([path.name for path in output.iterdir()], ['hello-0.1.0.rpk'])

    def test_failed_rebuild_keeps_existing_packages_and_framework_without_selected_skills_has_no_bundles(self):
        from primebuddy.tools import build_release as builder
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            skill = make_skill(directory / 'hello')
            output = directory / 'release'
            packages = builder.build_releases(output, [skill])
            before = {path: path.read_bytes() for path in packages}
            with patch.object(builder, 'framework_zip', side_effect=OSError('test packaging failure')), self.assertRaises(OSError):
                builder.build_releases(output, [skill], overwrite=True)
            self.assertEqual({path: path.read_bytes() for path in packages}, before)
            self.assertFalse(list(output.glob('.release-*')))
            separate = directory / 'framework-only'
            with patch.object(sys, 'argv', ['build_release.py', '--framework', '--output-dir', str(separate)]), \
                    patch.object(sys, 'stdout', io.StringIO()):
                builder.main()
            self.assertEqual([path.name for path in separate.iterdir()], [f'primebuddy-{__version__}.rpk'])
            with zipfile.ZipFile(separate / f'primebuddy-{__version__}.rpk') as package:
                self.assertFalse(any(name.startswith('primebuddy/resource/bundled/') for name in package.namelist()))

    def test_installer_reuses_system_yaml_without_pip(self):
        installer = self.installer()
        with patch.object(installer.subprocess, 'run') as run:
            run.return_value.returncode = 0
            installer.prepare_dependencies(ROOT)
            self.assertEqual(run.call_count, 1)
            self.assertIn('import yaml', run.call_args.args[0][-1])

    def test_missing_yaml_installs_into_application_deps_and_checks_import(self):
        from types import SimpleNamespace
        installer = self.installer()
        with patch.object(installer.subprocess, 'run', side_effect=[
                SimpleNamespace(returncode=1), SimpleNamespace(returncode=0), SimpleNamespace(returncode=0)]) as run:
            installer.prepare_dependencies(ROOT)
            command = run.call_args_list[1].args[0]
            self.assertEqual(command[command.index('--target') + 1], str(ROOT / '_deps'))
            self.assertNotIn('--user', command)
            self.assertEqual(command[command.index('-r') + 1], str(ROOT / 'requirements.txt'))
            self.assertEqual(run.call_count, 3)

    def test_log_directory_permission_setup_is_scoped_to_requested_path(self):
        installer = self.installer()
        with tempfile.TemporaryDirectory() as temp, patch.object(installer.os, 'access', return_value=False),\
                patch.object(installer.subprocess, 'run') as run:
            path = Path(temp) / 'logs'
            installer.prepare_logs(path)
            self.assertEqual(run.call_args.args[0], ['sudo', 'install', '-d', '-m', '700',
                '-o', str(os.getuid()), '-g', str(os.getgid()), str(path)])

    def test_release_onboard_upgrades_bundle_and_preserves_skill_configuration(self):
        spec = importlib.util.spec_from_file_location('build_upgrade_test', ROOT / 'src/primebuddy/tools/build_release.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            old = zip_skill(make_skill(directory / 'old', setup="return {'keep': 1}"), directory / 'old.zip')
            new = make_skill(directory / 'new', setup="return dict(config, revision=2)", version='0.2.0')
            archive = builder.build_releases(directory / 'release', [new])[0]
            with zipfile.ZipFile(archive) as package:
                package.extractall(directory / 'unpacked')
            env = dict(os.environ, PRIMEBUDDY_HOME=str(directory / 'data'),
                       PYTHONPATH=str(directory / 'unpacked/primebuddy/src'))
            script = '''import sys
from primebuddy.skills import manager as skills
from primebuddy.cli.onboarding import onboard
from primebuddy.config.storage import skill_config
class UI:
    def info(self, message): pass
    def header(self, *args): pass
    def confirm(self, *args, **kwargs): return False
    def multiselect(self, label, choices, default=None): return [0]
ui = UI()
skills.install(sys.argv[1], ui)
onboard(ui)
assert skills.load_skill_records()['hello']['version'] == '0.2.0'
assert skills.load_skill_records()['hello']['ready'] and skills.load_skill_records()['hello']['enabled']
assert skill_config('hello') == {'keep': 1, 'revision': 2}
print('BUNDLE_UPGRADE_OK')
'''
            result = subprocess.run([sys.executable, '-c', script, str(old)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('BUNDLE_UPGRADE_OK', result.stdout)

    def test_installer_rejects_virtualenv_before_writing(self):
        spec = importlib.util.spec_from_file_location('installer_test', ROOT / 'src/primebuddy/tools/install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as temp, patch.object(sys, 'prefix', sys.base_prefix + '/venv'):
            prefix = Path(temp) / 'app'
            with self.assertRaisesRegex(RuntimeError, '系统 python3'):
                installer.install(prefix)
            self.assertFalse(prefix.exists())

    def test_release_extract_install_command_and_bundled_skill(self):
        spec = importlib.util.spec_from_file_location('build_release_test', ROOT / 'src/primebuddy/tools/build_release.py')
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            skill = make_skill(directory / 'hello')
            # 私有文件即使误放在项目根目录，也不会进入发布清单。
            (skill / 'credential.json').write_text('do-not-ship')
            archive = builder.build_releases(directory / 'release', [skill])[0]
            with zipfile.ZipFile(archive) as package:
                package.extractall(directory / 'unpacked')
                self.assertEqual({name.split('/')[0] for name in package.namelist()}, {'primebuddy'})
                self.assertIn('primebuddy/resource/bundled/hello-0.1.0.rpk', package.namelist())
                self.assertIn('primebuddy/README.md', package.namelist())
                self.assertEqual(package.read('primebuddy/LICENSE'), (ROOT / 'LICENSE').read_bytes())
                bundled = io.BytesIO(package.read('primebuddy/resource/bundled/hello-0.1.0.rpk'))
            with zipfile.ZipFile(bundled) as package:
                self.assertNotIn('credential.json', package.namelist())
            prefix = directory / 'install with space'
            bindir = prefix / 'bin'
            app = directory / 'unpacked/primebuddy'
            shell_home = directory / 'shell home'
            shell_home.mkdir()
            env = dict(os.environ, HOME=str(shell_home), SHELL='/bin/bash', PRIMEBUDDY_LOG_DIR=str(directory / 'logs'))
            env.pop('PRIMEBUDDY_HOME', None)
            env.pop('PYTHONPATH', None)
            result = subprocess.run([sys.executable, str(app / 'main.py'), 'install',
                                     '--home', str(prefix), '--no-onboard'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((prefix / 'venv').exists())
            self.assertFalse((prefix / 'app').exists())
            self.assertFalse((bindir / 'primebuddy').is_symlink())
            self.assertTrue((prefix / 'src/primebuddy/cli/commands.py').is_file())
            self.assertEqual((prefix / 'LICENSE').read_bytes(), (ROOT / 'LICENSE').read_bytes())
            self.assertTrue((prefix / 'cfg/config.yaml').is_file())
            self.assertEqual((prefix / 'cfg/providers.yaml').stat().st_mode & 0o777, 0o600)
            # 安装后删除解压目录，仍能从统一目录运行和安装随包技能。
            shutil.rmtree(app)
            self.assertIn(sys.executable, (bindir / 'primebuddy').read_text())
            launcher = str(bindir / 'primebuddy')
            result = subprocess.run([launcher, '--version'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('PrimeBuddy ' + __version__, result.stdout)
            result = subprocess.run([launcher, 'skill', 'install', str((prefix / 'resource/bundled/hello-0.1.0.rpk').relative_to(directory)), '--no-configure'],
                                    cwd=directory, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(read_config(prefix / 'cfg/skills.yaml')['hello']['ready'])
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
            env = dict(os.environ, HOME=str(shell_home), SHELL='/bin/bash', PRIMEBUDDY_LOG_DIR=str(directory / 'logs'), PATH='/usr/bin:/bin', ASSISTANT_PYTHON=sys.executable)
            env.pop('PRIMEBUDDY_HOME', None)
            env.pop('PYTHONPATH', None)
            data = shell_home / '.primebuddy'
            write_config(data / 'cfg/providers.yaml', {'qianwen': {'api_key': 'existing-test-value'}})
            shells = [('/bin/bash', ['--noprofile', '--norc'])]
            if shutil.which('zsh'):
                shells.append((shutil.which('zsh'), ['-f']))
            for shell, flags in shells:
                for _ in range(2):
                    current_env = dict(env, SHELL=shell, ZDOTDIR=str(shell_home))
                    result = subprocess.run([shell, *flags, '-c',
                        'source "$1/scripts/install.sh" --no-onboard && primebuddy --version && primebuddy skill list',
                        'installer-test', str(app)], cwd=directory, env=current_env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('PrimeBuddy ' + __version__, result.stdout)
            self.assertEqual(read_config(data / 'cfg/providers.yaml'), {'qianwen': {'api_key': 'existing-test-value'}})
            startup = (shell_home / '.bashrc').read_text()
            self.assertTrue(startup.startswith('# Keep my settings\n'))
            self.assertEqual(startup.count('# >>> PrimeBuddy PATH >>>'), 1)
            self.assertFalse((data / 'app').exists())
            self.assertTrue((data / 'bin/primebuddy').is_file())
            self.assertTrue((data / 'main.py').is_file())
            self.assertTrue((data / 'docs/README.md').is_file())
            self.assertNotIn(str(app), (data / 'bin/primebuddy').read_text())

    def test_upgrade_replaces_code_and_preserves_private_files_and_installed_skills(self):
        installer = self.installer()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ), \
                patch.object(installer, 'configure_shell'), patch.object(installer, 'prepare_logs'):
            home = Path(temp).resolve() / '.primebuddy'
            command = installer.install(home)
            old_code = home / 'src/primebuddy/obsolete_test_module.py'
            old_code.write_text('# old framework file\n')
            private = {'cfg/config.yaml': '# keep my comments\nvoice:\n  speech_cache_entries: 7\n',
                       'cfg/providers.yaml': 'qianwen:\n  api_key: test-only-key\n',
                       'cfg/skills.yaml': 'hello:\n  path: hello\n  version: 0.1.0\n  ready: true\n  enabled: true\n',
                       'cfg/skills/hello/config.yaml': 'keep: 37\n',
                       'skills/hello/SKILL.md': '---\nname: hello\ndescription: 测试\nmetadata:\n  version: 0.1.0\n---\n保留技能。\n',
                       'data/hello/credential.json': '{"test": "keep-login"}',
                       'runtime/diagnostic.txt': 'keep-runtime-data',
                       'bin/custom-tool': 'keep-user-tool'}
            for name, content in private.items():
                installer.write_atomic(home / name, content, 0o600)
            installer.install(home)
            self.assertFalse(old_code.exists())
            for name, content in private.items():
                self.assertEqual((home / name).read_text(), content, name)
            # 从已安装目录重新执行安装，也不能递归复制或清空自身数据。
            with patch.object(installer, '__file__', str(home / 'src/primebuddy/tools/install.py')):
                installer.install(home)
            result = subprocess.run([str(command), 'skill', 'list'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('hello  0.1.0  已启用', result.stdout)
            self.assertEqual(list(home.glob('.install-*')), [])

    def test_failed_framework_upgrade_restores_previous_code_and_private_files(self):
        installer = self.installer()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ), \
                patch.object(installer, 'prepare_logs'), patch.object(installer, 'configure_shell') as shell:
            home = Path(temp).resolve() / '.primebuddy'
            installer.install(home)
            (home / 'main.py').write_text('# previous code\n')
            before = {name: (home / name).read_bytes() for name in
                      ('main.py', 'bin/primebuddy', 'cfg/config.yaml', 'cfg/providers.yaml')}
            shell.side_effect = OSError('test PATH failure')
            with self.assertRaisesRegex(OSError, 'test PATH failure'):
                installer.install(home)
            for name, content in before.items():
                self.assertEqual((home / name).read_bytes(), content, name)
            self.assertEqual(list(home.glob('.install-*')), [])

    def test_active_session_prevents_framework_upgrade(self):
        installer = self.installer()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ), \
                patch.object(installer, 'prepare_logs'), patch.object(installer, 'configure_shell'):
            home = Path(temp).resolve() / '.primebuddy'
            installer.install(home)
            (home / 'main.py').write_text('# running application\n')
            with (home / 'runtime/audio.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, '正在运行'):
                    installer.install(home)
            self.assertEqual((home / 'main.py').read_text(), '# running application\n')

    def test_release_built_from_installed_home_excludes_private_data(self):
        installer = self.installer()
        from primebuddy.tools import build_release
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ), \
                patch.object(installer, 'prepare_logs'), patch.object(installer, 'configure_shell'):
            home = Path(temp).resolve() / '.primebuddy'
            installer.install(home)
            installer.write_atomic(home / 'cfg/providers.yaml', 'api_key: do-not-ship\n', 0o600)
            installer.write_atomic(home / 'cfg/skills/hello/config.yaml', 'secret: do-not-ship\n', 0o600)
            installer.write_atomic(home / 'data/hello/token', 'do-not-ship', 0o600)
            output = home.parent / 'release'
            with patch.object(build_release, 'ROOT', home):
                archive = build_release.build_releases(output, [])[0]
            with zipfile.ZipFile(archive) as package:
                names = package.namelist()
                self.assertIn('primebuddy/cfg/config.example.yaml', names)
                self.assertIn('primebuddy/cfg/providers.example.yaml', names)
                self.assertNotIn('primebuddy/cfg/config.yaml', names)
                self.assertNotIn('primebuddy/cfg/providers.yaml', names)
                self.assertFalse(any(name.startswith(('primebuddy/data/', 'primebuddy/bin/',
                                                     'primebuddy/cfg/skills/', 'primebuddy/runtime/')) for name in names))

    def test_release_with_docs_can_be_built_installed_and_run(self):
        installer = self.installer()
        from primebuddy.tools import build_release
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ), \
                patch.object(installer, 'prepare_logs'), patch.object(installer, 'configure_shell'):
            directory = Path(temp)
            source = directory / 'source'
            installer.stage_application(ROOT, source)
            with patch.object(build_release, 'ROOT', source):
                archive = build_release.build_releases(directory / 'release', [make_skill(directory / 'hello')])[0]
            with zipfile.ZipFile(archive) as package:
                self.assertIn('primebuddy/docs/README.md', package.namelist())
                package.extractall(directory / 'unpacked')
            app = directory / 'unpacked/primebuddy'
            home = directory / 'installed'
            with patch.object(installer, '__file__', str(app / 'src/primebuddy/tools/install.py')):
                launcher = installer.install(home)
            self.assertTrue((home / 'docs/README.md').is_file())
            self.assertTrue((home / 'resource/bundled/hello-0.1.0.rpk').is_file())
            result = subprocess.run([str(launcher), '--version'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('PrimeBuddy ' + __version__, result.stdout)


class OnboardingTests(unittest.TestCase):
    def test_model_retry_preserves_secret_without_displaying_it(self):
        from unittest.mock import Mock
        from primebuddy.config.models import configure_model
        ui = Mock()
        ui.ask.side_effect = ['dashscope.aliyuncs.com', 'secret-for-test', 'dashscope.aliyuncs.com', 'secret-for-test']
        ui.confirm.return_value = True
        ui.choose.return_value = 0
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PRIMEBUDDY_HOME': temp}), patch('primebuddy.config.models.check_model', side_effect=[{'ASR': 'failed', 'LLM': 'ok', 'TTS': 'ok'}, {'ASR': 'ok', 'LLM': 'ok', 'TTS': 'ok'}]):
            configure_model(ui)
            saved = read_config(get_data_directory() / 'cfg/config.yaml')
            self.assertEqual(saved['asr']['model'], 'qianwen:qwen3-asr-flash')
            self.assertNotIn('secret-for-test', str(saved))
            self.assertEqual(read_config(get_data_directory() / 'cfg/providers.yaml')['qianwen']['api_key'], 'secret-for-test')
        self.assertNotIn('secret-for-test', str(ui.info.call_args_list))
        self.assertTrue(ui.ask.call_args_list[3].kwargs['secret'])
        self.assertEqual(ui.ask.call_args_list[3].args[1], 'secret-for-test')

    def test_skip_cloud_verification_is_explicitly_recorded(self):
        from unittest.mock import Mock
        from primebuddy.config.models import configure_model
        ui = Mock()
        ui.ask.side_effect = ['dashscope.aliyuncs.com', 'secret-for-test']
        ui.confirm.return_value = False
        ui.choose.return_value = 0
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'PRIMEBUDDY_HOME': temp}), patch('primebuddy.config.models.check_model') as check:
            configure_model(ui)
            self.assertEqual(read_config(get_data_directory() / 'cfg/config.yaml')['tts']['model'], 'qianwen:qwen-audio-3.0-tts-plus')
        check.assert_not_called()
        self.assertIn('待验证', str(ui.info.call_args_list))

    def test_reenter_onboard_reconfigures_existing_bundle_without_installing_again(self):
        from unittest.mock import Mock
        from primebuddy.cli.onboarding import onboard
        ui = Mock()
        ui.confirm.return_value = False
        ui.multiselect.return_value = [0]
        bundles = {'hello': {'path': Path('hello.zip'), 'version': '0.1.0'}}
        with patch('primebuddy.cli.onboarding.bundled_skills', return_value=bundles), patch('primebuddy.cli.onboarding.load_skill_records', return_value={'hello': {'ready': True, 'version': '0.1.0'}}), patch('primebuddy.cli.onboarding.install') as install, patch('primebuddy.cli.onboarding.configure') as configure:
            onboard(ui)
        install.assert_not_called()
        configure.assert_called_once_with('hello', ui)

    def test_skipped_bundle_stays_uninstalled_and_does_not_disable_existing(self):
        from unittest.mock import Mock
        from primebuddy.cli.onboarding import onboard
        ui = Mock()
        ui.confirm.return_value = False
        ui.multiselect.return_value = []
        bundles = {'hello': {'path': Path('hello.zip'), 'version': '0.1.0'}}
        with patch('primebuddy.cli.onboarding.bundled_skills', return_value=bundles),\
                patch('primebuddy.cli.onboarding.load_skill_records', return_value={'old': {'ready': True}}),\
                patch('primebuddy.cli.onboarding.install') as install, patch('primebuddy.cli.onboarding.configure') as configure:
            onboard(ui)
        install.assert_not_called()
        configure.assert_not_called()
        self.assertEqual(ui.multiselect.call_args.kwargs['default'], [])

    def test_newer_bundle_is_selected_for_upgrade_and_calls_its_own_setup(self):
        from unittest.mock import Mock
        from primebuddy.cli.onboarding import onboard
        ui = Mock()
        ui.confirm.return_value = False
        ui.multiselect.return_value = [0]
        bundles = {'hello': {'path': Path('hello.zip'), 'version': '0.2.10'}}
        with patch('primebuddy.cli.onboarding.bundled_skills', return_value=bundles),\
                patch('primebuddy.cli.onboarding.load_skill_records', return_value={'hello': {'ready': True, 'version': '0.2.9'}}),\
                patch('primebuddy.cli.onboarding.install', return_value={'ready': True}) as install,\
                patch('primebuddy.cli.onboarding.configure') as configure:
            onboard(ui)
        install.assert_called_once_with('hello.zip', ui, replace=True)
        configure.assert_not_called()
        self.assertEqual(ui.multiselect.call_args.kwargs['default'], [])
        self.assertIn('更新至 0.2.10', ui.multiselect.call_args.args[1][0])

    def test_bundle_does_not_downgrade_newer_installed_skill(self):
        from unittest.mock import Mock
        from primebuddy.cli.onboarding import onboard
        ui = Mock()
        ui.confirm.return_value = False
        ui.multiselect.return_value = [0]
        bundles = {'hello': {'path': Path('hello.zip'), 'version': '0.1.0'}}
        with patch('primebuddy.cli.onboarding.bundled_skills', return_value=bundles),\
                patch('primebuddy.cli.onboarding.load_skill_records', return_value={'hello': {'ready': True, 'version': '0.2.0'}}),\
                patch('primebuddy.cli.onboarding.install') as install, patch('primebuddy.cli.onboarding.configure') as configure:
            onboard(ui)
        install.assert_not_called()
        configure.assert_called_once_with('hello', ui)
        self.assertEqual(ui.multiselect.call_args.kwargs['default'], [])

    def test_unchecked_upgrade_leaves_installed_skill_untouched(self):
        from unittest.mock import Mock
        from primebuddy.cli.onboarding import onboard
        ui = Mock()
        ui.confirm.return_value = False
        ui.multiselect.return_value = []
        bundles = {'hello': {'path': Path('hello.zip'), 'version': '0.2.0'}}
        with patch('primebuddy.cli.onboarding.bundled_skills', return_value=bundles),\
                patch('primebuddy.cli.onboarding.load_skill_records', return_value={'hello': {'ready': True, 'version': '0.1.0'}}),\
                patch('primebuddy.cli.onboarding.install') as install, patch('primebuddy.cli.onboarding.configure') as configure:
            onboard(ui)
        self.assertEqual(ui.multiselect.call_args.kwargs['default'], [])
        install.assert_not_called()
        configure.assert_not_called()

    def test_bundle_versions_are_read_from_the_release_manifest(self):
        from primebuddy.cli.onboarding import bundled_skills
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'resource/bundled').mkdir(parents=True)
            archive = root / 'resource/bundled/hello.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('SKILL.md', '---\nname: hello\ndescription: 测试\n---\n问候。')
                output.writestr('cfg/pkg.yaml', json.dumps({'package_name': 'hello', 'version': '0.2.10'}))
            with patch('primebuddy.cli.onboarding.application_directory', return_value=root):
                self.assertEqual(bundled_skills(), {'hello': {'path': archive.resolve(), 'version': '0.2.10'}})

    def test_invalid_bundled_metadata_is_reported_without_installing(self):
        from primebuddy.cli.onboarding import bundled_skills
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'resource/bundled').mkdir(parents=True)
            archive = root / 'resource/bundled/hello.zip'
            for raw in ('not json', '[]', '{"package_name":"../bad","version":"0.1.0"}',
                        '{"package_name":"hello","version":"latest"}', ' ' * 16385):
                with self.subTest(raw=raw[:60]):
                    with zipfile.ZipFile(archive, 'w') as output:
                        output.writestr('SKILL.md', '---\nname: hello\ndescription: 测试\n---\n问候。')
                        output.writestr('cfg/pkg.yaml', raw)
                    with patch('primebuddy.cli.onboarding.application_directory', return_value=root),\
                            self.assertRaisesRegex(SetupError, '随包技能清单无效'):
                        bundled_skills()
