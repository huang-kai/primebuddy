"""用户手写 YAML、原子保存和敏感内容保护。"""
from pathlib import Path
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from primebuddy.config.storage import SetupError, config_path, read_config, write_config, write_json, skill_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.yaml'

    def manual(self, content):
        self.path.write_text(content, encoding='utf-8')
        self.path.chmod(0o600)

    def test_manual_yaml_comments_and_types(self):
        self.manual('# 整首歌\nplay_seconds: 0\nmusic_effects: false\nname: 点歌台\n')
        self.assertEqual(read_config(self.path),
                         {'play_seconds': 0, 'music_effects': False, 'name': '点歌台'})

    def test_writer_roundtrips_strings_and_nested_values(self):
        value = {'name': '点歌台', 'token': '0123', 'other': 'true', 'items': ['on', 2],
                 'child': {'enabled': False}, 'empty': None}
        write_config(self.path, value)
        self.assertEqual(read_config(self.path), value)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.path.read_text().startswith('name: 点歌台\n'))

    def test_yaml_read_does_not_rewrite_comments(self):
        write_json(self.path.with_suffix('.json'), {'play_seconds': 60})
        self.manual('# keep comment\nplay_seconds: 0\n')
        before = self.path.read_bytes()
        self.assertEqual(read_config(self.path), {'play_seconds': 0})
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_yaml_never_falls_back_or_exposes_secret(self):
        write_json(self.path.with_suffix('.json'), {'ok': True})
        self.manual('api_key: [top-secret\n')
        with self.assertRaises(SetupError) as result:
            read_config(self.path)
        self.assertNotIn('top-secret', str(result.exception))
        self.assertIn('行', str(result.exception))

    def test_rejects_non_mapping_duplicate_keys_and_python_tags(self):
        for content in ('', '[]', 'plain text', 'a: 1\na: 2',
                        'child:\n  a: 1\n  a: 2', '!!python/object:secret {}'):
            with self.subTest(content=content):
                self.manual(content)
                with self.assertRaises(SetupError):
                    read_config(self.path)

    def test_rejects_broad_permissions_before_reading(self):
        self.manual('{}')
        self.path.chmod(0o644)
        with self.assertRaises(SetupError):
            read_config(self.path)

    def test_missing_file_returns_default_without_creating_files(self):
        self.assertEqual(read_config(self.path), {})
        self.assertEqual(read_config(self.path, {'enabled': False}), {'enabled': False})
        self.assertFalse(self.path.exists())

    def test_failed_atomic_write_keeps_original(self):
        write_config(self.path, {'keep': 1})
        before = self.path.read_bytes()
        with patch('primebuddy.config.storage.os.replace', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                write_config(self.path, {'keep': 2})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_runtime_directory_uses_configuration_or_environment_and_keeps_recovery_records(self):
        source = Path(__file__).resolve().parents[1] / 'src/primebuddy/config/runtime.py'
        configured = Path(self.temp.name) / 'configured-runtime'
        override = Path(self.temp.name) / 'override-runtime'
        with patch.dict(os.environ, PRIMEBUDDY_HOME=self.temp.name):
            write_config(config_path('config.yaml'), {'runtime': {'directory': str(configured)}})
            for directory, environment in ((configured, None), (override, str(override))):
                with self.subTest(directory=directory), patch.dict(os.environ):
                    os.environ.pop('PRIMEBUDDY_RUNTIME', None)
                    if environment is not None:
                        os.environ['PRIMEBUDDY_RUNTIME'] = environment
                    directory.mkdir(mode=0o755)
                    directory.chmod(0o755)
                    record = directory / 'agent_mode.json'
                    record.write_bytes(b'pending-recovery')
                    spec = importlib.util.spec_from_file_location('runtime_directory_test', source)
                    runtime = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(runtime)
                    self.assertEqual(runtime.private_runtime(), directory)
                    self.assertEqual(runtime.private_runtime(), directory)
                    self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(record.read_bytes(), b'pending-recovery')

    def test_default_application_and_runtime_directories_share_one_home(self):
        from primebuddy.config.storage import get_data_directory
        from primebuddy.config.runtime import runtime_directory
        with patch.dict(os.environ, HOME=self.temp.name):
            os.environ.pop('PRIMEBUDDY_HOME', None)
            os.environ.pop('PRIMEBUDDY_RUNTIME', None)
            expected = Path(self.temp.name).resolve() / '.primebuddy'
            self.assertEqual(get_data_directory(), expected)
            self.assertEqual(runtime_directory(), expected / 'runtime')
            os.environ['PRIMEBUDDY_HOME'] = str(Path(self.temp.name) / 'custom')
            self.assertEqual(runtime_directory(), get_data_directory() / 'runtime')

    def test_json_configuration_is_not_loaded(self):
        write_json(self.path.with_suffix('.json'), {'play_seconds': 60})
        self.assertEqual(read_config(self.path), {})
        self.assertFalse(self.path.exists())

    def test_old_yaml_configuration_migrates_without_changing_bytes_or_login_data(self):
        root = Path(self.temp.name)
        with patch.dict(os.environ, PRIMEBUDDY_HOME=str(root)):
            for filename in ('model.yaml', 'skills.yaml'):
                old = root / filename
                write_config(old, {'keep': '值'})
                before = old.read_bytes()
                target = config_path(filename)
                self.assertEqual(target, root.resolve() / 'cfg' / filename)
                self.assertEqual(target.read_bytes(), before)
                self.assertFalse(old.exists())
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            old = root / 'data/diangetai/config.yaml'
            write_config(old, {'play_seconds': 37})
            credential = old.parent / 'credential.json'
            credential.write_bytes(b'test-only-login')
            self.assertEqual(skill_config('diangetai'), {'play_seconds': 37})
            self.assertFalse(old.exists())
            self.assertEqual(credential.read_bytes(), b'test-only-login')

    def test_new_configuration_wins_and_invalid_old_configuration_is_not_moved(self):
        root = Path(self.temp.name)
        with patch.dict(os.environ, PRIMEBUDDY_HOME=str(root)):
            old, target = root / 'model.yaml', root / 'cfg/model.yaml'
            write_config(old, {'keep': 'old'})
            write_config(target, {'keep': 'new'})
            self.assertEqual(read_config(config_path('model.yaml')), {'keep': 'new'})
            self.assertEqual(read_config(old), {'keep': 'old'})
            target.unlink()
            old.write_text('api_key: [test-secret\n')
            with self.assertRaises(SetupError) as error:
                config_path('model.yaml')
            self.assertNotIn('test-secret', str(error.exception))
            self.assertTrue(old.exists())
            self.assertFalse(target.exists())


class ModelConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_setup_saves_role_selections_and_secrets_in_separate_files(self):
        from primebuddy.config.models import configure_model, load_model
        ui = Mock()
        ui.choose.side_effect = [0, 1, 0, 0]
        ui.ask.side_effect = ['dashscope.aliyuncs.com', 'private-test-key']
        ui.confirm.return_value = False
        write_config(config_path('config.yaml'), {'tts': {'tts_voice': 'Cherry'}})
        configure_model(ui)
        config = read_config(config_path('config.yaml'))
        providers = read_config(config_path('providers.yaml'))
        self.assertEqual(config['asr']['model'], 'qianwen:qwen-audio-3.1-asr-flash')
        self.assertEqual(config['llm']['model'], 'qianwen:qwen-plus')
        self.assertEqual(config['tts'], {'model': 'qianwen:qwen-audio-3.0-tts-plus', 'tts_voice': 'Cherry'})
        self.assertNotIn('private-test-key', str(config))
        self.assertNotIn('private-test-key', str(ui.info.call_args_list))
        self.assertTrue(ui.ask.call_args_list[1].kwargs['secret'])
        self.assertEqual(providers['qianwen']['api_key'], 'private-test-key')
        self.assertNotIn('tts_voice', providers['qianwen'])
        self.assertEqual(config_path('providers.yaml').stat().st_mode & 0o777, 0o600)
        self.assertEqual(load_model('asr')['model'], 'qwen-audio-3.1-asr-flash')
        self.assertEqual(load_model('tts')['voice'], 'Cherry')
        self.assertEqual([(call[0], call.args[0]) for call in ui.method_calls if call[0] in ('choose', 'ask')], [
            ('choose', '模型 provider'), ('ask', '千问北京地域 API Host'), ('ask', '千问 API Key'),
            ('choose', 'ASR 模型'), ('choose', 'LLM 模型'), ('choose', 'TTS 模型')])
        self.assertEqual(providers['qianwen']['models']['asr'],
                         ['qwen3-asr-flash', 'qwen-audio-3.1-asr-flash'])

    def test_yaml_catalog_controls_wizard_choices_and_runtime_validation(self):
        from primebuddy.config.models import configure_model, load_model
        settings = {'api_host': 'dashscope.aliyuncs.com', 'api_key': 'private-test-key',
                    'models': {'asr': ['qwen-audio-3.1-asr-flash'],
                               'llm': ['fixture-chat', 'qwen-plus']}}
        write_config(config_path('providers.yaml'), {'qianwen': settings})
        ui = Mock()
        ui.choose.return_value = 0
        ui.ask.side_effect = ['dashscope.aliyuncs.com', 'private-test-key']
        ui.confirm.return_value = False
        configure_model(ui)
        self.assertEqual(ui.choose.call_args_list[1].args[1], ['qwen-audio-3.1-asr-flash'])
        self.assertEqual(ui.choose.call_args_list[2].args[1], ['fixture-chat', 'qwen-plus'])
        self.assertEqual(load_model('llm')['model'], 'fixture-chat')
        self.assertEqual(load_model('tts')['model'], 'qwen-audio-3.0-tts-plus')
        with self.assertRaises(SetupError):
            load_model('asr', 'qwen3-asr-flash')

    def test_bad_model_catalog_is_rejected_without_displaying_credentials(self):
        from primebuddy.config.models import provider_models
        for models in ('private-test-key', {'asr': []}, {'asr': 'private-test-key'},
                       {'asr': [123]}, {'asr': ['same', 'same']}, {'llm': ['bad:model']},
                       {'other': ['private-test-key']}):
            with self.subTest(models=models):
                with self.assertRaises(SetupError) as result:
                    provider_models('qianwen', {'models': models, 'api_key': 'private-test-key'})
                self.assertNotIn('private-test-key', str(result.exception))

    def test_invalid_connection_retries_before_model_selection_and_cancel_keeps_files(self):
        from primebuddy.config.models import configure_model
        write_config(config_path('providers.yaml'), {
            'qianwen': {'api_host': 'dashscope.aliyuncs.com', 'api_key': 'existing-test-key'}})
        write_config(config_path('config.yaml'), {'asr': {'model': 'qianwen:qwen3-asr-flash'}})
        before = {name: config_path(name).read_bytes() for name in ('config.yaml', 'providers.yaml')}
        ui = Mock()
        ui.choose.side_effect = [0, KeyboardInterrupt]
        ui.ask.side_effect = ['invalid.example', 'private-test-key']
        with self.assertRaises(KeyboardInterrupt):
            configure_model(ui)
        self.assertEqual([call.args[0] for call in ui.choose.call_args_list], ['模型 provider', '模型 provider'])
        self.assertNotIn('private-test-key', str(ui.info.call_args_list))
        for name, content in before.items():
            self.assertEqual(config_path(name).read_bytes(), content)

    def test_mixed_providers_route_each_role_and_tts_cache_uses_selected_model(self):
        import io
        import sys
        from types import ModuleType
        import wave
        from primebuddy.config.models import load_model
        from primebuddy.config.storage import load_app_config
        from primebuddy.providers import PROVIDERS, transcribe_wav
        from primebuddy.core.planner import plan
        from primebuddy.voice.speech_cache import SpeechCache
        sample = io.BytesIO()
        with wave.open(sample, 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 240)
        adapter = ModuleType('primebuddy.providers.test_only')
        adapter.build_config = lambda role, model, settings, options: {
            'provider': 'test_only', 'role': role, 'model': model, 'endpoint': 'fixture://local', 'key_secret': settings['key_secret']}
        adapter.chat = Mock(return_value={'role': 'assistant', 'content': '测试'})
        adapter.synthesize_wav = Mock(return_value=sample.getvalue())
        config = load_app_config()
        config.update(llm={'model': 'test_only:chat'}, tts={'model': 'test_only:voice-a'})
        write_config(config_path('config.yaml'), config)
        write_config(config_path('providers.yaml'), {
            'qianwen': {'api_host': 'dashscope.aliyuncs.com', 'api_key': 'asr-only'},
            'test_only': {'key_secret': 'other-only', 'models': {'llm': ['chat'], 'tts': ['voice-a', 'voice-b']}}})
        with patch.dict(PROVIDERS, test_only=adapter.__name__), patch.dict(sys.modules, {adapter.__name__: adapter}), \
                patch('primebuddy.providers.qianwen.transcribe_wav', return_value='识别成功') as asr:
            self.assertEqual(transcribe_wav(b'test'), '识别成功')
            self.assertEqual(asr.call_args.args[1]['api_key'], 'asr-only')
            registry = Mock(skills={}, catalog=Mock(return_value=[]))
            self.assertEqual(plan('测试', registry), {'reply': '测试', 'tasks': []})
            self.assertEqual(adapter.chat.call_args.args[2]['key_secret'], 'other-only')
            cache = self.root / 'cache'
            first = SpeechCache(cache).prepare('相同文字')
            config['tts']['model'] = 'test_only:voice-b'
            write_config(config_path('config.yaml'), config)
            second = SpeechCache(cache).prepare('相同文字')
            self.assertNotEqual(first, second)
            self.assertEqual(adapter.synthesize_wav.call_count, 2)
            self.assertEqual(load_model('tts')['model'], 'voice-b')

    def test_invalid_selection_and_missing_provider_fail_without_exposing_credentials(self):
        from primebuddy.config.models import load_model
        write_config(config_path('providers.yaml'), {'qianwen': {'api_host': 'dashscope.aliyuncs.com', 'api_key': 'private-test-key'}})
        for reference in (None, 123, 'qwen3-asr-flash', 'unknown:model', 'qianwen:unknown', 'qianwen:qwen-plus'):
            with self.subTest(reference=reference):
                write_config(config_path('config.yaml'), {'asr': {'model': reference}})
                with self.assertRaises(SetupError) as error:
                    load_model('asr')
                self.assertNotIn('private-test-key', str(error.exception))
        write_config(config_path('config.yaml'), {'asr': {'model': 'qianwen:qwen3-asr-flash'}})
        config_path('providers.yaml').unlink()
        with self.assertRaisesRegex(SetupError, '尚未配置'):
            load_model('asr')

    def test_tts_options_do_not_override_provider_credentials_and_cache_tracks_voice(self):
        from primebuddy.config.models import load_model
        from primebuddy.voice.speech_cache import SpeechCache
        write_config(config_path('providers.yaml'), {
            'qianwen': {'api_host': 'dashscope.aliyuncs.com', 'api_key': 'provider-key'}})
        options = {'tts_voice': 'Cherry', 'api_key': 'role-key', 'api_host': 'invalid.example'}
        write_config(config_path('config.yaml'), {'tts': options})
        model = load_model('tts')
        self.assertEqual(model['voice'], 'Cherry')
        self.assertEqual(model['api_key'], 'provider-key')
        self.assertEqual(model['api_host'], 'dashscope.aliyuncs.com')
        first = SpeechCache(self.root / 'cache').identity
        options['tts_voice'] = 'longanlingxin'
        write_config(config_path('config.yaml'), {'tts': options})
        self.assertNotEqual(first, SpeechCache(self.root / 'cache').identity)
        options['tts_voice'] = ''
        write_config(config_path('config.yaml'), {'tts': options})
        with self.assertRaisesRegex(SetupError, '音色不能为空'):
            load_model('tts')
