"""一个坏包不能阻止健康技能发现、执行及管理；不删除配置或解除资源隔离。"""
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from primebuddy.cli.commands import manage_skill
from primebuddy.config.storage import SetupError, config_path, write_config
from primebuddy.core.engine import CleanupUnconfirmed, Context, execute
from primebuddy.skills import manager
from primebuddy.skills.registry import Registry


class DiscoveryIsolationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / 'home'
        env = patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        self.good = self.skill('good')
        (self.good / 'hello.py').write_text('print("healthy skill ran")')

    def skill(self, name):
        root = self.home / 'skills' / name
        root.mkdir(parents=True, exist_ok=True)
        (root / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: test\n---\nhello')
        return root

    def check_healthy(self):
        registry = Registry()
        self.assertEqual(set(registry.skills), {'good'})
        proposal = {'reply': '', 'tasks': [{'id': 'one', 'skill': 'good', 'action': 'script',
                    'args': {'path': 'hello.py', 'arguments': []}, 'after': []}]}
        result = execute(proposal, registry, Context(simulate=False), lambda _: None)
        self.assertIn('healthy skill ran', str(result))
        return registry

    def test_bad_documents_and_unknown_protocol_do_not_block_healthy_execution(self):
        for mode in ('document', 'protocol', 'entry', 'private', 'adapter'):
            with self.subTest(mode=mode):
                root = self.skill(mode)
                if mode == 'document':
                    (root / 'SKILL.md').write_text('broken')
                elif mode == 'protocol':
                    write_config(root / 'cfg/pkg.yaml', {'primebuddy': {'version': 9}})
                elif mode == 'entry':
                    write_config(root / 'cfg/pkg.yaml', {'primebuddy': {'entrypoint': 'missing.py'}})
                elif mode == 'private':
                    path = config_path('config.yaml', skill=mode)
                    write_config(path, {'keep': 'private data'})
                    path.write_text('[')
                else:
                    write_config(root / 'cfg/pkg.yaml', {'primebuddy': {'cli': {'configure': ['missing.py']}}})
                registry = self.check_healthy()
                self.assertIn(mode, registry.unavailable)

    def test_bad_index_entries_and_missing_packages_do_not_hide_other_skills(self):
        write_config(config_path('skills.yaml'), {
            'malformed': None, 'missing': {'path': 'missing', 'ready': True, 'enabled': True},
            'bad-path': {'path': 'unused', 'directory': 7},
            'nul-path': {'path': 'bad\x00path'},
            'escape': {'path': '../../outside', 'ready': True, 'enabled': True}})
        registry = self.check_healthy()
        self.assertEqual(set(registry.unavailable), {'malformed', 'missing', 'escape', 'bad-path', 'nul-path'})

    def test_onboarding_can_configure_healthy_skill_beside_broken_install_record(self):
        from primebuddy.cli.onboarding import onboard
        manager.set_enabled('good', False)
        index = manager.load_skill_records()
        index['good'].pop('ready')
        index['broken'] = None
        write_config(config_path('skills.yaml'), index)
        ui = Mock()
        ui.confirm.return_value = False
        ui.multiselect.return_value = [0]
        with patch('primebuddy.cli.onboarding.configure_robot'), \
                patch('primebuddy.cli.onboarding.bundled_skills', return_value={}):
            onboard(ui)
        self.assertEqual(len(ui.multiselect.call_args.args[1]), 2)
        self.assertTrue(ui.multiselect.call_args.args[1][0].startswith('good'))
        self.assertEqual(ui.multiselect.call_args.args[1][-1], 'None')
        self.assertTrue(manager.load_skill_records()['good']['enabled'])
        self.assertIsNone(manager.load_skill_records()['broken'])

    def test_list_disable_and_configure_work_beside_unknown_protocol(self):
        root = self.skill('bad')
        write_config(root / 'cfg/pkg.yaml', {'primebuddy': {'version': 9}})
        saved = {'path': 'bad', 'version': '0.1.0', 'ready': True, 'enabled': True}
        write_config(config_path('skills.yaml'), {'bad': saved})
        output = io.StringIO()
        with redirect_stdout(output):
            manage_skill(SimpleNamespace(action='list'))
        self.assertIn('bad', output.getvalue())
        self.assertIn('已隔离', output.getvalue())
        self.assertIn('good', output.getvalue())
        manager.set_enabled('bad', False)
        self.assertFalse(manager.load_skill_records()['bad']['enabled'])
        with self.assertRaisesRegex(SetupError, '协议'):
            manager.set_enabled('bad', True)
        self.assertTrue(manager.configure('good', None))
        self.check_healthy()

    def test_isolation_preserves_user_choices_and_data_and_rechecks_on_restart(self):
        root = self.skill('bad')
        write_config(root / 'cfg/pkg.yaml', {'primebuddy': {'version': 9}})
        write_config(config_path('skills.yaml'), {'bad': {'path': 'bad', 'ready': True, 'enabled': True}})
        write_config(config_path('config.yaml', skill='bad'), {'account': 'preserved'})
        before = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.check_healthy()
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        write_config(root / 'cfg/pkg.yaml', {})
        self.assertEqual(set(Registry().skills), {'good', 'bad'})

    def test_duplicate_names_are_both_isolated_without_hiding_other_skills(self):
        self.skill('duplicate')
        extra = self.home / 'extra'
        folder = extra / 'different-folder'
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text('---\nname: duplicate\ndescription: duplicate\n---\n')
        write_config(config_path('config.yaml'), {'skills': {'directories': [str(extra)]}})
        registry = self.check_healthy()
        self.assertIn('重复', registry.unavailable['duplicate']['reason'])

    def test_strict_update_discovery_still_rejects_unreadable_metadata(self):
        root = self.skill('bad')
        (root / 'SKILL.md').write_text('broken')
        with self.assertRaises(SetupError):
            manager.discover_skill_records(read_only=True, strict=True)

    def test_explicit_directory_also_isolates_invalid_packages(self):
        root = self.skill('bad')
        (root / 'SKILL.md').write_text('broken')
        registry = Registry(self.good.parent)
        self.assertEqual(set(registry.skills), {'good'})
        self.assertIn('bad', registry.unavailable)

    def test_context_load_failure_is_local_but_cleanup_uncertainty_is_preserved(self):
        registry = Registry()
        with patch.object(registry, 'load', side_effect=RuntimeError('bad plugin')):
            self.assertEqual(registry.planning_context(Context()), {'good': {'status': 'unavailable'}})
        with patch.object(registry, 'load', side_effect=CleanupUnconfirmed('speaker still owned')):
            with self.assertRaises(CleanupUnconfirmed):
                registry.planning_context(Context())
