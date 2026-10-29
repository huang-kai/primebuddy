"""离线更新与故障恢复；所有安装、包和账号均在临时目录。"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from primebuddy.config.storage import SetupError, write_config


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.stage = self.home / 'staging'
        for root, value in ((self.home, 'old'), (self.stage, 'new')):
            for name in ('src/code.py', 'skills/demo/main.py'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
        self.paths = ['src', 'skills/demo']

    def test_group_update_preserves_private_data(self):
        from primebuddy.tools.update import commit_update
        private = self.home / 'data/demo/account'
        private.parent.mkdir(parents=True)
        private.write_bytes(b'private-test-data')
        commit_update(self.home, self.stage, self.paths)
        self.assertEqual((self.home / 'src/code.py').read_text(), 'new')
        self.assertEqual((self.home / 'skills/demo/main.py').read_text(), 'new')
        self.assertEqual(private.read_bytes(), b'private-test-data')
        self.assertFalse((self.home / '.update/transaction.json').exists())

    def test_failure_after_framework_replacement_restores_both_components(self):
        from primebuddy.tools.update import prepare_transaction
        from primebuddy.tools import update_transaction as runner
        prepare_transaction(self.home, self.stage, self.paths)
        original = runner.move
        def fail(source, target):
            if source == self.home / '.update/new/skills/demo':
                raise OSError('injected write failure')
            original(source, target)
        with patch.object(runner, 'move', side_effect=fail), self.assertRaises(OSError):
            runner.apply(self.home)
        for name in ('src/code.py', 'skills/demo/main.py'):
            self.assertEqual((self.home / name).read_text(), 'old')

    def test_recovery_after_process_death_needs_no_application_modules(self):
        from primebuddy.tools.update import prepare_transaction
        from primebuddy.tools import update_transaction as runner
        prepare_transaction(self.home, self.stage, self.paths)
        journal = self.home / '.update/transaction.json'
        data = json.loads(journal.read_text())
        data['entries'][0]['state'] = 'installing'
        runner.save(journal, data)
        runner.move(self.home / 'src', self.home / '.update/old/src')
        runner.move(self.home / '.update/new/src', self.home / 'src')
        import subprocess, sys
        result = subprocess.run([sys.executable, str(self.home / '.update/runner.py'),
                                 '--recover', str(self.home)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / 'src/code.py').read_text(), 'old')
        self.assertEqual((self.home / 'skills/demo/main.py').read_text(), 'old')
        self.assertFalse(journal.exists())

    def test_symlink_target_is_rejected_before_any_replacement(self):
        from primebuddy.tools.update import commit_update
        (self.home / 'skills/demo/main.py').unlink()
        (self.home / 'skills/demo').rmdir()
        (self.home / 'skills/demo').symlink_to(self.stage / 'skills/demo', target_is_directory=True)
        with self.assertRaises((SetupError, ValueError)):
            commit_update(self.home, self.stage, self.paths)
        self.assertEqual((self.home / 'src/code.py').read_text(), 'old')


class UpdatePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.manifest = {'schema_version': 1, 'version': '1.1.0', 'skills': {
            'diangetai': {'min_version': '0.4.0', 'max_version': '0.5.0', 'recommended_version': '0.4.0'}}}
        self.env = patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.home))
        self.env.start()
        self.addCleanup(self.env.stop)

    def skill(self, version='0.3.0', enabled=False, external=False):
        root = self.home / ('external' if external else 'skills/diangetai')
        root.mkdir(parents=True)
        (root / 'SKILL.md').write_text('---\nname: diangetai\ndescription: test\n---\n')
        (root / 'main.py').write_text('')
        (root / 'cfg').mkdir()
        (root / 'cfg/pkg.yaml').write_text('version: ' + version + '\n')
        record = dict(version='9.9.9', enabled=enabled, ready=True)
        record.update(directory=str(root)) if external else record.update(path='diangetai')
        write_config(self.home / 'cfg/skills.yaml', {'diangetai': record})
        return root

    def test_empty_install_does_not_add_optional_skills(self):
        from primebuddy.tools.update import skill_updates
        self.assertEqual(skill_updates(self.home, self.manifest), [])

    def test_plan_uses_actual_version_and_preserves_disabled_state(self):
        from primebuddy.tools.update import skill_updates
        self.skill()
        plan = skill_updates(self.home, self.manifest)
        self.assertEqual(len(plan), 1)
        self.assertEqual((plan[0]['current'], plan[0]['target']), ('0.3.0', '0.4.0'))
        self.assertFalse(plan[0]['record']['enabled'])

    def test_incompatible_external_skill_is_reported_without_overwrite(self):
        from primebuddy.tools.update import skill_updates
        root = self.skill(enabled=True, external=True)
        with self.assertRaisesRegex(SetupError, '外部'):
            skill_updates(self.home, self.manifest)
        self.assertEqual((root / 'cfg/pkg.yaml').read_text(), 'version: 0.3.0\n')

    def test_compatible_newer_skill_is_not_downgraded(self):
        from primebuddy.tools.update import skill_updates
        self.skill(version='0.4.2', enabled=True)
        self.assertEqual(skill_updates(self.home, self.manifest), [])

    def test_compatible_older_skill_is_not_upgraded_to_recommended(self):
        from primebuddy.tools.update import skill_updates
        self.skill(version='0.4.0', enabled=True)
        self.manifest['skills']['diangetai']['recommended_version'] = '0.4.2'
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(skill_updates(self.home, self.manifest), [])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def test_preview_does_not_download_packages_or_change_files(self):
        from primebuddy.tools import update
        self.skill()
        (self.home / 'release.json').write_text(json.dumps(dict(self.manifest, version='1.0.0')))
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        with patch.object(update, 'fetch_release', return_value=self.manifest), \
                patch.object(update, 'download_artifact') as download, contextlib.redirect_stdout(io.StringIO()) as output:
            update.run_update(check=True)
        self.assertIn('0.3.0', output.getvalue())
        download.assert_not_called()
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def test_another_update_finishing_before_lock_cannot_cause_downgrade(self):
        from primebuddy.tools import update
        release = self.home / 'release.json'
        release.write_text(json.dumps(dict(self.manifest, version='1.0.0')))
        def concurrent_update(stack, home):
            release.write_text(json.dumps(dict(self.manifest, version='1.2.0')))
            return ()
        with patch.object(update, 'fetch_release', return_value=self.manifest), \
                patch.object(update, 'update_lock', side_effect=concurrent_update), \
                patch.object(update, 'download_artifact') as download, \
                self.assertRaisesRegex(SetupError, '降级'):
            update.run_update()
        download.assert_not_called()
        self.assertEqual(update.installed_version(self.home), '1.2.0')


class UpdateIntegrationTests(unittest.TestCase):
    def setUp(self):
        from primebuddy.tools import build_release
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'
        self.release = self.root / 'release'
        project = Path(__file__).resolve().parents[1]
        collection = project.parent / 'primebuddy-skills'
        build_release.build_releases(self.release, [collection / name for name in ('diangetai', 'yeelight', 'chengyu-chain')], release=True)
        self.manifest = json.loads((self.release / 'release.json').read_text())
        self.environment = patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.home),
            PRIMEBUDDY_RUNTIME=str(self.home / 'runtime'), PRIMEBUDDY_LOG_DIR=str(self.root / 'logs'))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        (self.home / 'src/primebuddy').mkdir(parents=True)
        (self.home / 'src/primebuddy/__init__.py').write_text("__version__ = '1.0.0'\n")
        (self.home / 'main.py').write_text('# old framework\n')
        write_config(self.home / 'cfg/config.yaml', {'voice': {'speech_cache_entries': 7}})
        write_config(self.home / 'cfg/providers.yaml', {'test': {'api_key': 'test-only-key'}})
        for name in ('diangetai', 'yeelight'):
            skill = self.home / 'skills' / name
            (skill / 'cfg').mkdir(parents=True)
            (skill / 'SKILL.md').write_text('---\nname: ' + name + '\ndescription: test\n---\n')
            version = '0.3.0' if name == 'diangetai' else '0.2.0'
            (skill / 'cfg/pkg.yaml').write_text('version: ' + version + '\n')
            (skill / 'main.py').write_text('# old skill\n')
            write_config(self.home / 'cfg/skills' / name / 'config.yaml', {'test': 'keep'})
        write_config(self.home / 'cfg/skills.yaml', {
            'diangetai': dict(path='diangetai', version='0.3.0', enabled=True, ready=True),
            'yeelight': dict(path='yeelight', version='0.2.0', enabled=False, ready=True)})
        private = self.home / 'data/diangetai'
        private.mkdir(parents=True)
        (private / 'credential.json').write_bytes(b'keep-credential')
        (private / 'device.json').write_bytes(b'keep-device')
        self.private = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*')
                        if p.is_file() and (p.is_relative_to(self.home / 'data') or
                        p.is_relative_to(self.home / 'cfg')) and p.name != 'skills.yaml'}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('primebuddy.tools.install.prepare_dependencies'))
        self.stack.enter_context(patch('primebuddy.tools.install.prepare_logs'))
        self.stack.enter_context(patch('primebuddy.tools.install.configure_shell'))
        self.dependencies = self.stack.enter_context(patch('primebuddy.tools.update.install_dependencies'))
        self.stack.enter_context(patch('primebuddy.tools.update.fetch_release', return_value=self.manifest))

    def download(self, artifact, destination):
        shutil.copy2(self.release / artifact['filename'], destination)

    def assert_upgraded(self):
        from primebuddy.config.storage import read_config
        from primebuddy.tools.update import installed_version
        self.assertEqual(installed_version(self.home), '1.2.0')
        index = read_config(self.home / 'cfg/skills.yaml')
        self.assertEqual(index['diangetai']['version'], '0.4.1')
        self.assertEqual(index['yeelight']['version'], '0.3.1')
        self.assertTrue(index['diangetai']['enabled'])
        self.assertFalse(index['yeelight']['enabled'])
        for name, content in self.private.items():
            self.assertEqual((self.home / name).read_bytes(), content, name)
        self.assertFalse((self.home / '.update/transaction.json').exists())

    def test_online_update_changes_all_versions_without_configuring_skills(self):
        from primebuddy.tools import update
        with patch.object(update, 'download_artifact', side_effect=self.download), \
                patch('primebuddy.skills.manager.run_setup') as configure:
            update.run_update()
        configure.assert_not_called()
        self.assert_upgraded()

    def test_platform_update_preserves_compatible_skill_files_and_optional_absence(self):
        from primebuddy.tools import update
        skill = self.home / 'skills/diangetai'
        (skill / 'cfg/pkg.yaml').write_text('version: 0.4.0\n')
        self.manifest['skills']['diangetai']['recommended_version'] = '0.4.1'
        # This skill should never be downloaded, even when recommendation changed.
        before = {str(p.relative_to(skill)): p.read_bytes() for p in skill.rglob('*') if p.is_file()}
        self.assertNotIn('diangetai', [item['name'] for item in update.skill_updates(self.home, self.manifest)])
        # Restore valid published manifest for the framework/embedded-manifest comparison.
        self.manifest['skills']['diangetai']['recommended_version'] = '0.4.1'
        with patch.object(update, 'download_artifact', side_effect=self.download), \
                patch('primebuddy.skills.manager.run_setup') as configure:
            update.run_update()
        configure.assert_not_called()
        self.assertEqual(before, {str(p.relative_to(skill)): p.read_bytes() for p in skill.rglob('*') if p.is_file()})
        self.assertFalse((self.home / 'skills/chengyu-chain').exists())
        for name, content in self.private.items():
            self.assertEqual((self.home / name).read_bytes(), content)

    def test_old_install_bootstrap_uses_bundled_versions_in_one_transaction(self):
        from primebuddy.skills.manager import unpack
        from primebuddy.tools import install
        unpack(self.release / 'primebuddy-1.2.0.rpk', self.root / 'unpacked')
        source = self.root / 'unpacked/primebuddy'
        # 易来不在默认大包内；离线升级该旧技能须另行提供其独立包。
        artifact = self.manifest['skills']['yeelight']['artifact']
        shutil.copy2(self.release / artifact['filename'], source / 'resource/bundled' / artifact['filename'])
        with patch.object(install, '__file__', str(source / 'src/primebuddy/tools/install.py')), \
                patch('primebuddy.tools.update.download_artifact') as download, \
                patch('primebuddy.skills.manager.run_setup') as configure:
            install.install(self.home)
        configure.assert_not_called()
        download.assert_not_called()
        self.assert_upgraded()

    def test_dependency_failure_keeps_entire_old_installation(self):
        from primebuddy.tools import update
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.dependencies.side_effect = SetupError('test dependency failure')
        with patch.object(update, 'download_artifact', side_effect=self.download), self.assertRaises(SetupError):
            update.run_update()
        for path, value in before.items():
            self.assertEqual(Path(path).read_bytes(), value)
        self.assertFalse((self.home / '.update/transaction.json').exists())

    def test_online_and_offline_upgrade_refuse_pending_runtime_recovery(self):
        from primebuddy.tools import update, install
        from primebuddy.skills.manager import unpack
        unpack(self.release / 'primebuddy-1.2.0.rpk', self.root / 'unpacked')
        source = self.root / 'unpacked/primebuddy'
        marker = self.home / 'runtime/diangetai/cleanup-unconfirmed.json'
        marker.parent.mkdir(parents=True)
        marker.write_text('{"resources":["speaker.music"]}')
        with patch.object(update, 'download_artifact') as download, self.assertRaisesRegex(SetupError, '恢复'):
            update.run_update()
        download.assert_not_called()
        with patch.object(install, '__file__', str(source / 'src/primebuddy/tools/install.py')), \
                self.assertRaisesRegex(SetupError, '恢复'):
            install.install(self.home)
        self.assertEqual((self.home / 'main.py').read_text(), '# old framework\n')
        self.assertTrue(marker.exists())

    def test_existing_install_does_not_probe_or_move_to_another_board(self):
        from primebuddy.tools import install
        with patch.object(sys, 'argv', ['install.py', '--home', str(self.home), '--no-onboard']), \
                patch('primebuddy.tools.board_install.installation_board') as board, \
                patch('primebuddy.tools.board_install.install_on_brain') as remote, \
                patch.object(install, 'install', return_value=self.home / 'bin/primebuddy') as local:
            self.assertEqual(install.main(), 0)
        board.assert_not_called()
        remote.assert_not_called()
        local.assert_called_once_with(self.home)


if __name__ == '__main__':
    unittest.main()
