"""独占交互声明及成语应用的宿主 CLI 契约。"""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from primebuddy.config.storage import SetupError
from primebuddy.skills.adapters import adapt_metadata, load_adapters
from primebuddy.skills.metadata import load_skill_metadata
from primebuddy.skills.registry import Registry


class InteractionAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.root / 'home'))
        environment.start()
        self.addCleanup(environment.stop)
        self.skill = self.root / 'skills/voice-game'
        (self.skill / 'cfg').mkdir(parents=True)
        (self.skill / 'SKILL.md').write_text('---\nname: voice-game\ndescription: 独立语音游戏\n---\nfixture')
        (self.skill / 'main.py').write_text('')
        self.cli = {operation: ['main.py', '--config', '{config}', '--data-dir', '{data}',
                    '--runtime-dir', '{runtime}', operation]
                    for operation in ('configure', 'context', 'status', 'recover')}
        self.cli['actions'] = {'play': {
            'command': ['main.py', '--config', '{config}', '--data-dir', '{data}',
                        '--runtime-dir', '{runtime}', 'play', '--hosted'],
            'parameters': {}, 'resources': ['microphone', 'speaker', 'robot.motion', 'robot.expression'],
            'timeout_seconds': 86400, 'stop_grace_seconds': [25, 2, 2],
            'preflight': ['main.py', 'check-play']}}

    def metadata(self, **options):
        (self.skill / 'cfg/pkg.yaml').write_text(json.dumps({'primebuddy': options}))
        return load_skill_metadata(self.skill)

    def test_game_uses_a_supervised_foreground_hosted_cli_and_hides_raw_script(self):
        self.metadata(mode='exclusive', cli=self.cli)
        with patch('primebuddy.skills.registry.load_adapters', return_value={}):
            registry = Registry(self.skill.parent)
        action = registry.skills['voice-game']['actions']['play']
        self.assertEqual(action['interaction'], 'exclusive')
        self.assertEqual(action['parameters'], {})
        self.assertEqual(action['command'][-2:], ['play', '--hosted'])
        self.assertEqual(action['resources'], ['microphone', 'speaker', 'robot.motion', 'robot.expression'])
        self.assertEqual(action['timeout_seconds'], 86400)
        self.assertEqual(action['stop_grace_seconds'], [25, 2, 2])
        self.assertEqual(action['preflight'][-1], 'check-play')
        self.assertNotIn('script', registry.skills['voice-game']['actions'])
        for operation in ('configure', 'context', 'status', 'recover'):
            self.assertEqual(registry.skills['voice-game']['adapter'][operation][-1], operation)

    def test_ordinary_mode_is_default_and_does_not_take_over_voice(self):
        for options in ({}, {'mode': 'task'}):
            data = self.metadata(cli=self.cli, **options)
            result = adapt_metadata(data, self.skill, {})
            self.assertNotIn('interaction', result['actions']['play'])

    def test_invalid_modes_are_rejected_before_discovery(self):
        for mode in (None, False, True, '', 'shared', 'EXCLUSIVE', [], {}):
            with self.subTest(mode=mode), self.assertRaisesRegex(SetupError, 'mode'):
                self.metadata(mode=mode, cli=self.cli)

    def test_exclusive_mode_claims_audio_resources_without_mutating_the_manifest(self):
        self.cli['actions']['play']['resources'] = ['robot.expression']
        data = self.metadata(mode='exclusive', cli=self.cli)
        result = adapt_metadata(data, self.skill, {})
        self.assertEqual(result['actions']['play']['resources'], ['robot.expression', 'microphone', 'speaker'])
        self.assertEqual(data['cli']['actions']['play']['resources'], ['robot.expression'])
        self.assertNotIn('interaction', data['cli']['actions']['play'])

    def test_private_cli_override_keeps_the_skill_exclusive(self):
        data = self.metadata(mode='exclusive', cli=self.cli)
        override = copy.deepcopy(self.cli)
        override['actions']['play']['command'].append('--custom')
        result = adapt_metadata(data, self.skill, {'voice-game': override})
        self.assertEqual(result['actions']['play']['command'][-1], '--custom')
        self.assertEqual(result['actions']['play']['interaction'], 'exclusive')
        with self.assertRaisesRegex(SetupError, '独占'):
            adapt_metadata(data, self.skill, {'voice-game': None})

    def test_exclusive_mode_cannot_fall_back_to_a_raw_script(self):
        for options in ({}, {'cli': {}}, {'cli': {'actions': {}}}):
            data = self.metadata(mode='exclusive', **options)
            with self.subTest(options=options), self.assertRaisesRegex(SetupError, '独占'):
                adapt_metadata(data, self.skill, {})

    def test_package_cli_reuses_path_validation_and_rejects_mixed_native_hooks(self):
        self.cli['actions']['play']['command'][0] = '../main.py'
        data = self.metadata(mode='exclusive', cli=self.cli)
        with self.assertRaisesRegex(SetupError, '路径'):
            adapt_metadata(data, self.skill, {})
        with self.assertRaisesRegex(SetupError, 'cli'):
            self.metadata(cli=self.cli, entrypoint='main.py', actions={})
        for cli in (None, [], ''):
            with self.subTest(cli=cli), self.assertRaisesRegex(SetupError, 'cli'):
                self.metadata(cli=cli)

    def test_installer_uses_the_package_configuration_command(self):
        from primebuddy.skills.manager import run_setup
        data = self.metadata(mode='exclusive', cli=self.cli)
        with patch('primebuddy.skills.manager.subprocess.call', return_value=0) as configure:
            self.assertEqual(run_setup(self.skill, data, None), {})
        command = configure.call_args.args[0]
        self.assertEqual(command[1], str(self.skill / 'main.py'))
        self.assertEqual(command[-1], 'configure')
        self.assertIn(str(self.root / 'home/cfg/skills/voice-game/config.yaml'), command)

    def test_legacy_installed_game_keeps_its_host_adapter(self):
        (self.skill / 'SKILL.md').write_text('---\nname: chengyu-jielong\ndescription: 旧名成语接龙\n---\nfixture')
        result = adapt_metadata(self.metadata(), self.skill, load_adapters())
        self.assertEqual(result['actions']['play']['interaction'], 'exclusive')
        self.assertEqual(result['actions']['play']['command'][-2:], ['play', '--hosted'])

    def test_unknown_interaction_declarations_are_rejected_for_cli_and_native_actions(self):
        for value in (None, False, True, '', 'shared', [], {}):
            cli = copy.deepcopy(self.cli)
            cli['actions']['play']['interaction'] = value
            data = self.metadata(mode='exclusive', cli=cli)
            with self.subTest(value=value), self.assertRaisesRegex(SetupError, 'interaction'):
                adapt_metadata(data, self.skill, {})
            (self.skill / 'cfg/pkg.yaml').write_text(json.dumps({'primebuddy': {
                'entrypoint': 'main.py', 'actions': {'play': {'parameters': {}, 'interaction': value}}}}))
            with self.subTest(native=value), self.assertRaisesRegex(SetupError, 'interaction'):
                load_skill_metadata(self.skill)

    def test_exclusive_application_requires_independent_status_and_recovery(self):
        for missing in ('status', 'recover'):
            cli = copy.deepcopy(self.cli)
            cli.pop(missing)
            data = self.metadata(mode='exclusive', cli=cli)
            with self.subTest(missing=missing), self.assertRaisesRegex(SetupError, 'status 和 recover'):
                adapt_metadata(data, self.skill, {})


if __name__ == '__main__':
    unittest.main()
