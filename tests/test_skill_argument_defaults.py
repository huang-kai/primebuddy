"""宿主只补声明的缺省参数；业务要求仍由独立技能校验。"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from primebuddy.config.storage import SetupError
from primebuddy.core.planner import plan
from primebuddy.skills.adapters import adapt_metadata, load_adapters
from primebuddy.skills.registry import Registry

ROOT = Path(__file__).resolve().parents[1]


class SkillArgumentDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.root / 'home'))
        environment.start()
        self.addCleanup(environment.stop)
        self.registry = Registry(ROOT.parent / 'primebuddy-skills')
        self.addCleanup(self.registry.close)
        self.fields = self.registry.skills['diangetai']['actions']['play']['parameters']

    def proposal(self, args):
        def tool(name, arguments, identifier):
            return {'tool_calls': [{'id': identifier, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}]}
        task = {'id': 'music', 'skill': 'diangetai', 'action': 'play', 'args': args, 'after': []}
        with patch('primebuddy.core.planner.chat', side_effect=[
                tool('read_skill_file', {'skill': 'diangetai', 'path': 'SKILL.md'}, 'read'),
                tool('execute_tasks', {'tasks': [task]}, 'execute')]):
            return plan('点歌测试', self.registry)

    def test_artist_only_and_song_only_fill_only_the_missing_field(self):
        for args, expected in (({'artist': '林俊杰'}, {'artist': '林俊杰', 'song': ''}),
                               ({'song': '江南'}, {'artist': '', 'song': '江南'}),
                               ({'artist': '林俊杰', 'song': '江南'}, {'artist': '林俊杰', 'song': '江南'})):
            with self.subTest(args=args):
                self.assertEqual(self.proposal(args)['tasks'][0]['args'], expected)

    def test_empty_defaults_do_not_bypass_the_independent_skill_business_check(self):
        args = self.proposal({})['tasks'][0]['args']
        skill = self.registry.skills['diangetai']['root']
        process = subprocess.run([sys.executable, str(skill / 'main.py'),
            '--config', str(self.root / 'config.yaml'), '--data-dir', str(self.root / 'data'),
            '--runtime-dir', str(self.root / 'runtime'), 'check-play',
            '--artist', args['artist'], '--song', args['song'], '--simulate'],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 1, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['message'], '请提供歌手或歌名')

    def test_unknown_fields_are_still_rejected(self):
        with self.assertRaisesRegex(ValueError, '未知字段'):
            self.proposal({'artist': '林俊杰', 'command': 'unexpected'})

    def test_missing_field_without_default_is_still_required(self):
        self.fields['song'].pop('default', None)
        with self.assertRaisesRegex(ValueError, '缺失'):
            self.proposal({'artist': '林俊杰'})

    def test_defaults_still_pass_type_text_and_enum_validation(self):
        for rule, reason in (({'type': 'string', 'default': False}, '类型错误'),
                             ({'type': 'string', 'default': 'x' * 101}, '文本参数无效'),
                             ({'type': 'string', 'default': '\n'}, '文本参数无效'),
                             ({'type': 'string', 'default': 'other', 'enum': ['allowed']}, '取值错误')):
            self.fields['song'] = rule
            with self.subTest(rule=rule), self.assertRaisesRegex(ValueError, reason):
                self.proposal({'artist': '林俊杰'})

    def test_adapter_rejects_a_default_of_the_wrong_declared_type(self):
        for kind, value in (('string', False), ('boolean', 1), ('integer', True)):
            adapters = copy.deepcopy(load_adapters())
            adapters['diangetai']['actions']['play']['parameters']['song'] = {'type': kind, 'default': value}
            with self.subTest(kind=kind), self.assertRaisesRegex(SetupError, '默认值'):
                adapt_metadata({'name': 'diangetai', 'version': '0.4.0'}, self.registry.skills['diangetai']['root'], adapters)


if __name__ == '__main__':
    unittest.main()
