"""安装向导契约独立于技能业务版本；旧包继续可读。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from primebuddy.config.storage import SetupError
from primebuddy.skills.metadata import load_skill_metadata
from primebuddy.skills.adapters import adapt_metadata


class SkillProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'cfg').mkdir()
        (self.root / 'SKILL.md').write_text('---\nname: example\ndescription: Test\n---\n')
        (self.root / 'wizard.py').write_text('')
        self.options = {'version': 1, 'cli': {'configure': ['wizard.py', '--config', '{config}']}}

    def load(self):
        (self.root / 'cfg/pkg.yaml').write_text(json.dumps({'primebuddy': self.options}))
        return load_skill_metadata(self.root)

    def test_custom_wizard_filename_is_supported(self):
        self.assertEqual(self.load()['cli']['configure'][0], 'wizard.py')

    def test_package_version_and_protocol_version_are_independent(self):
        (self.root / 'cfg/pkg.yaml').write_text(json.dumps({'version': '2.3.4', 'primebuddy': self.options}))
        data = load_skill_metadata(self.root)
        self.assertEqual(data['version'], '2.3.4')
        self.assertEqual(data['cli_version'], 1)

    def test_missing_wizard_entry_or_script_rejected(self):
        for command in (None, [], ['missing.py'], ['../outside.py'], ['wizard.py', '{unknown}']):
            with self.subTest(command=command):
                self.options['cli']['configure'] = command
                with self.assertRaises(SetupError):
                    self.load()

    def test_future_or_invalid_protocol_rejected(self):
        for version in (2, True, '1', None):
            self.options['version'] = version
            with self.assertRaises(SetupError):
                self.load()

    def test_legacy_document_skill_remains_supported(self):
        self.options = {}
        self.assertEqual(self.load()['name'], 'example')

    def test_package_contract_is_not_replaced_by_host_defaults(self):
        with patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.root / 'home')):
            data = adapt_metadata(self.load(), self.root,
                                  {'example': {'configure': ['missing.py']}}, check_compatibility=False)
        self.assertEqual(data['configure'][0], 'wizard.py')

    def test_user_override_remains_explicit(self):
        home = self.root / 'home'
        (home / 'cfg').mkdir(parents=True)
        (home / 'cfg/skill-adapters.yaml').write_text(json.dumps({'example': {'configure': ['wizard.py', 'custom']}}))
        (home / 'cfg/skill-adapters.yaml').chmod(0o600)
        with patch.dict(os.environ, PRIMEBUDDY_HOME=str(home)):
            data = adapt_metadata(self.load(), self.root, {}, check_compatibility=False)
        self.assertEqual(data['configure'], ['wizard.py', 'custom'])
