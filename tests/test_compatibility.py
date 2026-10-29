"""官方 CLI 协议按磁盘版本校验；旧技能可查看和停用。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from primebuddy.config.storage import SetupError, config_path, write_config
from primebuddy.skills import manager
from primebuddy.skills.adapters import adapt_metadata
from primebuddy.skills.metadata import load_skill_metadata
from primebuddy.skills.registry import Registry


def make_official(root, version='0.3.0'):
    root.mkdir(parents=True)
    (root / 'SKILL.md').write_text('---\nname: diangetai\ndescription: 测试旧技能\n---\n播放歌曲。\n')
    write_config(root / 'cfg/pkg.yaml', {'version': version})
    # 旧公开入口不支持新适配器的 --config / context 参数。
    (root / 'main.py').write_text('import argparse\np = argparse.ArgumentParser()\n'
                                'p.add_argument("command", choices=["configure"], nargs="?")\np.parse_args()\n')
    return root


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.root / 'home'))
        env.start()
        self.addCleanup(env.stop)

    def test_old_cli_is_rejected_before_adapter_can_run_it(self):
        root = make_official(self.root / 'old')
        with self.assertRaisesRegex(SetupError, 'diangetai.*0.3.0.*0.4.0'):
            adapt_metadata(load_skill_metadata(root), root)

    def test_disabled_old_skill_can_be_discovered_and_listed_but_not_enabled(self):
        root = make_official(self.root / 'home/skills/diangetai')
        write_config(config_path('skills.yaml'), {'diangetai': {
            'path': 'diangetai', 'version': '0.4.0', 'ready': True, 'enabled': False}})
        self.assertEqual(manager.discover_skill_records()['diangetai']['version'], '0.3.0')
        self.assertEqual(Registry().catalog(), [])
        with self.assertRaisesRegex(SetupError, 'diangetai'):
            manager.set_enabled('diangetai', True)
        with self.assertRaisesRegex(SetupError, 'diangetai'):
            manager.configure('diangetai', Mock())
        self.assertFalse(manager.load_skill_records()['diangetai']['enabled'])
        manager.set_enabled('diangetai', False)

    def test_active_old_skill_and_install_are_rejected_before_setup(self):
        root = make_official(self.root / 'home/skills/diangetai')
        write_config(config_path('skills.yaml'), {'diangetai': {
            'path': 'diangetai', 'version': '0.4.0', 'ready': True, 'enabled': True}})
        registry = Registry()
        self.assertEqual(registry.catalog(), [])
        self.assertIn('不兼容', registry.unavailable['diangetai']['reason'])
        source = make_official(self.root / 'source')
        with patch.object(manager, 'install_dependencies') as dependencies, \
                patch.object(manager, 'run_setup') as setup, \
                self.assertRaisesRegex(SetupError, 'diangetai'):
            manager.install(source, Mock(), replace=True)
        dependencies.assert_not_called()
        setup.assert_not_called()

    def test_manifest_range_and_ordinary_skill(self):
        from primebuddy.config.compatibility import check_skill, load_release
        manifest = load_release()
        for version in ('0.4.0', '0.4.99'):
            check_skill({'name': 'diangetai', 'version': version}, manifest)
        for version in ('0.3.9', '0.5.0', '1.0.0', 'latest'):
            with self.subTest(version=version), self.assertRaises(SetupError):
                check_skill({'name': 'diangetai', 'version': version}, manifest)
        check_skill({'name': 'custom', 'version': '0.0.0'}, manifest)
        path = self.root / 'release.json'
        for data in ({}, {'schema_version': 2}, dict(manifest, version='latest'),
                     dict(manifest, skills={'diangetai': {'min_version': '0.5.0', 'max_version': '0.4.0',
                                                        'recommended_version': '0.4.0'}})):
            path.write_text(json.dumps(data))
            with self.assertRaises(SetupError):
                load_release(path)

    def test_read_only_discovery_does_not_migrate_legacy_configuration(self):
        home = self.root / 'home'
        make_official(home / 'skills/diangetai')
        write_config(home / 'skills.yaml', {'diangetai': {
            'path': 'diangetai', 'version': '0.4.0', 'ready': True, 'enabled': False}})
        write_config(home / 'config.yaml', {'skills': {'directories': []}})
        write_config(home / 'skill-adapters.yaml', {})
        write_config(home / 'data/diangetai/config.yaml', {'keep': True})
        before = {str(path.relative_to(home)): path.read_bytes() for path in home.rglob('*') if path.is_file()}
        self.assertEqual(manager.discover_skill_records(read_only=True)['diangetai']['version'], '0.3.0')
        after = {str(path.relative_to(home)): path.read_bytes() for path in home.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((home / 'cfg').exists())


if __name__ == '__main__':
    unittest.main()
