"""固定 IP 分流、跨板安装和持久认证；全部使用本地夹具，不连接机器人。"""
from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from primebuddy.cli import onboarding
from primebuddy.config.storage import SetupError
from primebuddy.integrations.primebot import ssh_setup
from primebuddy.integrations.primebot.robot_audio import ssh_options
from primebuddy.tools import board_install, install

ROOT = Path(__file__).resolve().parents[1]


class BoardTests(unittest.TestCase):
    def test_probe_retries_then_detects_brain_without_authentication(self):
        with patch.object(board_install.socket, 'create_connection',
                          side_effect=[TimeoutError, ConnectionRefusedError, nullcontext()]) as connect, \
                patch.object(board_install.time, 'sleep'):
            self.assertTrue(board_install.brain_available())
        self.assertEqual(connect.call_count, 3)
        connect.assert_called_with(('10.1.1.101', 22), timeout=2)

    def test_probe_has_bounded_attempts_when_brain_is_absent(self):
        with patch.object(board_install.socket, 'create_connection', side_effect=TimeoutError) as connect, \
                patch.object(board_install.time, 'sleep'):
            self.assertFalse(board_install.brain_available())
        self.assertEqual(connect.call_count, 3)

    def test_t1_and_q1_use_same_auto_routing(self):
        for series in ('T1', 'Q1'):
            for soc, reachable, remote in [('0', True, True), ('0', False, False), ('1', False, False)]:
                identity = dict(series=series, sku='test', soc_index=soc)
                with self.subTest(series=series, soc=soc, reachable=reachable), \
                        patch.object(board_install, 'local_identity', return_value=identity), \
                        patch.object(board_install, 'brain_available', return_value=reachable) as probe, \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(board_install.installation_board(), identity if remote else None)
                    self.assertEqual(probe.call_count, int(soc == '0'))

    def test_development_machine_never_probes_robot_network(self):
        with patch.object(board_install, 'local_identity', return_value=None), \
                patch.object(board_install, 'brain_available') as probe:
            self.assertIsNone(board_install.installation_board())
        probe.assert_not_called()

    def test_invalid_robot_identity_is_not_treated_as_development_machine(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(ssh_setup, 'INFO_ROOT', Path(temp)):
            with self.assertRaises(RuntimeError):
                ssh_setup.local_identity()
            for name, value in dict(series='Q1', sku='test', soc_index='1').items():
                (Path(temp) / name).write_text(value)
            self.assertEqual(ssh_setup.local_identity()['soc_index'], '1')
            (Path(temp) / 'soc_index').write_text('2')
            with self.assertRaises(RuntimeError):
                ssh_setup.local_identity()

    def test_remote_install_runs_before_local_writes_and_keeps_remote_home_relative(self):
        identity = dict(series='T1', soc_index='0')
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ), \
                patch.object(sys, 'argv', ['installer', '--shell-env-file', temp + '/result']), \
                patch.object(board_install, 'installation_board', return_value=identity), \
                patch.object(board_install, 'install_on_brain', return_value=0) as remote, \
                patch.object(install, 'install') as local:
            os.environ.pop('PRIMEBUDDY_HOME', None)
            self.assertEqual(install.main(), 0)
            remote.assert_called_once_with(identity, Path('~/.primebuddy'), False)
            local.assert_not_called()
            self.assertFalse((Path(temp) / 'result').exists())

    def test_authentication_failure_never_falls_back_to_local_install(self):
        with patch.object(sys, 'argv', ['installer']), \
                patch.object(board_install, 'installation_board', return_value={'series': 'Q1'}), \
                patch.object(board_install, 'install_on_brain', side_effect=RuntimeError('认证失败')), \
                patch.object(install, 'install') as local, patch('sys.stderr', new=io.StringIO()):
            self.assertEqual(install.main(), 1)
        local.assert_not_called()

    def test_relay_transfers_only_package_files_and_runs_wizard_on_brain(self):
        seen = []
        def run(args, **kwargs):
            seen.append(args)
            if kwargs.get('stdin'):
                with zipfile.ZipFile(kwargs['stdin']) as archive:
                    names = archive.namelist()
                self.assertIn('main.py', names)
                self.assertIn('src/primebuddy/tools/board_install.py', names)
                self.assertNotIn('cfg/providers.yaml', names)
                self.assertFalse(any(name.startswith(('.git/', 'runtime/', 'data/', 'skills/')) for name in names))
                code = shlex.split(args[-1])[-1]
                # 只从生成的 Python 常量中读取目录名。
                import ast
                tree = ast.parse(code)
                name = tree.body[2].value.right.value
                return subprocess.CompletedProcess(args, 0, ('/home/run/' + name + '\n').encode())
            return subprocess.CompletedProcess(args, 0)
        with patch.object(board_install, 'ssh_session', return_value=nullcontext(['ssh', '-S', 'test-socket'])), \
                patch.object(board_install, 'remote_identity') as identity, \
                patch.object(board_install.subprocess, 'run', side_effect=run), \
                patch.object(board_install.sys.stdin, 'isatty', return_value=True), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(board_install.install_on_brain({'series': 'Q1'}, Path('~/Buddy with space'), False), 0)
        self.assertEqual(identity.call_args.args[2], {'series': 'Q1', 'soc_index': '1'})
        interactive = next(command for command in seen if '-tt' in command)
        script = shlex.split(interactive[-1])[-1]
        self.assertIn("'~/Buddy with space'", script)
        self.assertIn('exec bash -l', script)
        self.assertNotIn('--no-onboard', script)
        self.assertTrue(all('run@10.1.1.101' in command for command in seen))

    def test_remote_identity_mismatch_does_not_transfer_or_install(self):
        with patch.object(board_install, 'ssh_session', return_value=nullcontext(['ssh'])), \
                patch.object(board_install, 'remote_identity', side_effect=RuntimeError('身份不符')), \
                patch.object(board_install.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                board_install.install_on_brain({'series': 'T1'}, Path('~/.primebuddy'), True)
        run.assert_not_called()

    def test_failed_remote_install_keeps_source_and_never_opens_completion_shell(self):
        def run(args, **kwargs):
            if kwargs.get('stdin'):
                import ast
                code = ast.parse(shlex.split(args[-1])[-1])
                name = code.body[2].value.right.value
                return subprocess.CompletedProcess(args, 0, ('/home/run/' + name + '\n').encode())
            self.assertIn('--no-onboard', args[-1])
            self.assertNotIn('exec bash', args[-1])
            return subprocess.CompletedProcess(args, 1)
        with patch.object(board_install, 'ssh_session', return_value=nullcontext(['ssh'])), \
                patch.object(board_install, 'remote_identity'), \
                patch.object(board_install.subprocess, 'run', side_effect=run) as calls, \
                patch.object(board_install.sys.stdin, 'isatty', return_value=False), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(board_install.install_on_brain({'series': 'T1'}, Path('~/.primebuddy'), True), 1)
        self.assertEqual(calls.call_count, 2, '失败时不能清理远程安装源')
        self.assertIn('安装源保留', output.getvalue())

    def test_sourced_remote_installer_does_not_load_old_motion_board_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            old = home / '.primebuddy/bin'
            old.mkdir(parents=True)
            (old / 'env.sh').write_text('export OLD_MOTION_BOARD_ENV=1\n')
            fake = home / 'python'
            fake.write_text('#!/bin/sh\nexit 0\n')
            fake.chmod(0o700)
            env = dict(os.environ, HOME=temp, PRIMEBUDDY_HOME=str(old.parent), ASSISTANT_PYTHON=str(fake))
            env.pop('OLD_MOTION_BOARD_ENV', None)
            result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c',
                                     'source "$1/scripts/install.sh" && test -z "${OLD_MOTION_BOARD_ENV:-}"',
                                     'test', str(ROOT)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_board_detection_loads_before_third_party_dependencies_are_installed(self):
        code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                'from primebuddy.tools import board_install; assert "yaml" not in sys.modules')
        result = subprocess.run([sys.executable, '-S', '-c', code, str(ROOT / 'src')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class SSHSetupTests(unittest.TestCase):
    def test_writable_ssh_directory_keeps_existing_permissions_and_files(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, 'home', return_value=Path(temp)), \
                patch.object(ssh_setup.subprocess, 'run') as run:
            directory = Path(temp) / '.ssh'
            directory.mkdir(mode=0o755)
            keys = directory / 'authorized_keys'
            keys.write_text('existing user key\n')
            self.assertEqual(ssh_setup.prepare_ssh_directory(), directory)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
            self.assertEqual(keys.read_text(), 'existing user key\n')
        run.assert_not_called()

    def test_unwritable_ssh_directory_repairs_only_directory(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, 'home', return_value=Path(temp)), \
                patch('os.access', side_effect=[False, True]), \
                patch.object(ssh_setup.subprocess, 'run') as run, redirect_stdout(io.StringIO()):
            directory = Path(temp) / '.ssh'
            directory.mkdir()
            keys = directory / 'authorized_keys'
            keys.write_text('existing user key\n')
            self.assertEqual(ssh_setup.prepare_ssh_directory(), directory)
            run.assert_called_once_with(
                ['sudo', 'install', '-d', '-m', '700', '-o', str(os.getuid()),
                 '-g', str(os.getgid()), str(directory)], check=True, timeout=60)
            self.assertEqual(keys.read_text(), 'existing user key\n')

    def test_failed_ssh_permission_repair_stops_with_clear_error(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, 'home', return_value=Path(temp)), \
                patch('os.access', return_value=False), \
                patch.object(ssh_setup.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'sudo')), \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'SSH 目录不可写'):
                ssh_setup.prepare_ssh_directory()

    def test_ssh_directory_symlink_never_gets_privileged_permission_changes(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, 'home', return_value=Path(temp)), \
                patch.object(ssh_setup.subprocess, 'run') as run:
            target = Path(temp) / 'other'
            target.mkdir()
            (Path(temp) / '.ssh').symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, '符号链接'):
                ssh_setup.prepare_ssh_directory()
        run.assert_not_called()

    def test_failed_master_is_checked_and_closed_without_password_capture(self):
        with patch.object(ssh_setup, 'prepare_ssh_directory'), \
                patch.object(ssh_setup.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0), subprocess.CalledProcessError(255, 'ssh'),
                subprocess.CompletedProcess([], 0)]) as run:
            with self.assertRaisesRegex(RuntimeError, '不会改为'):
                with ssh_setup.ssh_session(ssh_setup.BRAIN_HOST, '大脑板'):
                    self.fail('failed master must not be used')
        self.assertIn('StrictHostKeyChecking=accept-new', run.call_args_list[0].args[0])
        self.assertNotIn('input', run.call_args_list[0].kwargs)
        self.assertIn('check', run.call_args_list[1].args[0])
        self.assertIn('exit', run.call_args_list[2].args[0])

    def test_passwordless_probe_cannot_reuse_a_password_master(self):
        with patch.object(ssh_setup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 255)) as run:
            self.assertFalse(ssh_setup.passwordless(ssh_setup.MOTION_HOST))
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('-S') + 1], 'none')
        self.assertIn('BatchMode=yes', args)
        self.assertIn('StrictHostKeyChecking=yes', args)

    def test_existing_authentication_reused_without_generating_key(self):
        with patch.object(ssh_setup, 'passwordless', return_value=True), \
                patch.object(ssh_setup, 'remote_identity') as identity, \
                patch.object(ssh_setup, 'ssh_session') as session, \
                patch.object(ssh_setup, 'configure_host') as config:
            ssh_setup.setup_voice_ssh(ssh_setup.MOTION_HOST, {'series': 'T1'}, Mock())
        identity.assert_called_once()
        session.assert_not_called()
        config.assert_not_called()

    def test_host_config_is_idempotent_preserves_user_settings_and_scopes_key(self):
        with tempfile.TemporaryDirectory(prefix='pb config ') as temp, patch.object(Path, 'home', return_value=Path(temp)):
            directory = Path(temp) / '.ssh'
            directory.mkdir()
            target = Path(temp) / 'my-config'
            original = 'ServerAliveInterval 17\nHost example.org\n    User test-user\n'
            target.write_text(original)
            (directory / 'config').symlink_to(target)
            key = directory / 'primebuddy' / 'id_ed25519'
            ssh_setup.configure_host(ssh_setup.MOTION_HOST, key)
            first = target.read_text()
            ssh_setup.configure_host(ssh_setup.MOTION_HOST, key)
            self.assertEqual(target.read_text(), first)
            self.assertIn(original, first)
            self.assertTrue((directory / 'config').is_symlink())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            for host, should_match in [('10.1.1.100', True), ('example.org', False)]:
                result = subprocess.run(['ssh', '-G', '-F', str(target), host], capture_output=True, text=True, check=True)
                self.assertEqual(str(key) in result.stdout, should_match)
                self.assertIn('serveraliveinterval 17', result.stdout)

    def test_public_key_registration_preserves_existing_entries_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / '.ssh'
            directory.mkdir()
            path = directory / 'authorized_keys'
            original = '# Existing user keys\nssh-ed25519 AAAATESTOTHER keep-user'
            path.write_text(original)
            actual_run = subprocess.run
            def run(args, **kwargs):
                code = shlex.split(args[-1])[-1]
                return actual_run([sys.executable, '-c', code], env=dict(os.environ, HOME=temp), **kwargs)
            with patch.object(ssh_setup.subprocess, 'run', side_effect=run):
                for _ in range(2):
                    ssh_setup.authorize_key(['ssh'], ssh_setup.MOTION_HOST, 'ssh-ed25519 AAAATESTKEY test')
            self.assertEqual(path.read_text(), original + '\nrestrict ssh-ed25519 AAAATESTKEY primebuddy\n')
            # 注释提到相同公钥不能误判为已有授权。
            path.write_text('# ssh-ed25519 AAAATESTKEY comment only\n')
            with patch.object(ssh_setup.subprocess, 'run', side_effect=run):
                ssh_setup.authorize_key(['ssh'], ssh_setup.MOTION_HOST, 'ssh-ed25519 AAAATESTKEY')
            self.assertIn('\nrestrict ssh-ed25519 AAAATESTKEY primebuddy\n', path.read_text())

    def test_managed_key_is_reused_and_verified_without_master_before_configuration(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, 'home', return_value=Path(temp)), \
                patch.object(ssh_setup, 'passwordless', side_effect=[False, True, True, False, True, True]) as probe, \
                patch.object(ssh_setup, 'ssh_session', side_effect=lambda *a: nullcontext(['ssh'])), \
                patch.object(ssh_setup, 'remote_identity'), \
                patch.object(ssh_setup, 'authorize_key') as authorize, \
                patch.object(ssh_setup, 'configure_host') as config:
            # 使用真实 ssh-keygen，但只生成临时测试密钥，不接触用户密钥。
            ssh_setup.setup_voice_ssh(ssh_setup.MOTION_HOST, {'series': 'T1'}, Mock())
            key = Path(temp) / '.ssh/primebuddy/id_ed25519_run_10.1.1.100'
            first = key.read_bytes()
            ssh_setup.setup_voice_ssh(ssh_setup.MOTION_HOST, {'series': 'T1'}, Mock())
            self.assertEqual(key.read_bytes(), first)
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(authorize.call_count, 2)
            self.assertEqual(config.call_count, 2)
            self.assertIn('IdentitiesOnly=yes', probe.call_args_list[1].args[1])

    def test_failed_key_verification_does_not_rewrite_ssh_config(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, 'home', return_value=Path(temp)), \
                patch.object(ssh_setup, 'passwordless', side_effect=[False, False]), \
                patch.object(ssh_setup, 'ssh_session', return_value=nullcontext(['ssh'])), \
                patch.object(ssh_setup, 'remote_identity'), \
                patch.object(ssh_setup, 'authorize_key'), \
                patch.object(ssh_setup, 'configure_host') as config:
            with self.assertRaisesRegex(RuntimeError, '专用密钥验证失败'):
                ssh_setup.setup_voice_ssh(ssh_setup.MOTION_HOST, {'series': 'Q1'}, Mock())
            self.assertTrue(list((Path(temp) / '.ssh/primebuddy').glob('id_ed25519_*')))
        config.assert_not_called()

    def test_runtime_uses_default_host_records_but_preserves_legacy_records(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            self.assertFalse(any('UserKnownHostsFile' in value for value in ssh_options(runtime)))
            (runtime / 'known_hosts').write_text('legacy record')
            self.assertIn('UserKnownHostsFile=' + str(runtime / 'known_hosts'), ssh_options(runtime))


class RobotOnboardingTests(unittest.TestCase):
    def test_single_board_skips_ssh_and_only_queries_agent(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(ssh_setup, 'local_identity', return_value={'series': 'Q1', 'soc_index': '0'}), \
                patch('primebuddy.integrations.primebot.device.audio_host', return_value='local'), \
                patch.object(ssh_setup, 'setup_voice_ssh') as setup, \
                patch('primebuddy.config.runtime.private_runtime', return_value=Path(temp)), \
                patch('primebuddy.integrations.primebot.native_agent.agent_rpc',
                      return_value={'status': 'Running(12)', 'pid': 12}) as rpc:
            onboarding.configure_robot(Mock())
        setup.assert_not_called()
        rpc.assert_called_once_with(Path(temp), 'state')

    def test_connection_failure_stops_before_model_and_skill_configuration(self):
        with patch.object(onboarding, 'configure_robot', side_effect=SetupError('运控板连接失败')), \
                patch.object(onboarding, 'configure_model') as models, \
                patch.object(onboarding, 'bundled_skills') as skills:
            with self.assertRaises(SetupError):
                onboarding.onboard(Mock())
        models.assert_not_called()
        skills.assert_not_called()

    def test_timeout_has_resume_instruction_without_dumping_command(self):
        with patch.object(ssh_setup, 'local_identity', return_value={'series': 'T1', 'soc_index': '1'}), \
                patch('primebuddy.integrations.primebot.device.audio_host', return_value=ssh_setup.MOTION_HOST), \
                patch.object(ssh_setup, 'setup_voice_ssh',
                             side_effect=subprocess.TimeoutExpired(['ssh', 'do-not-display-command'], 20)):
            with self.assertRaises(SetupError) as error:
                onboarding.configure_robot(Mock())
        self.assertIn('primebuddy onboard', str(error.exception))
        self.assertNotIn('do-not-display-command', str(error.exception))

    def test_dual_board_configures_connection_before_read_only_agent_check(self):
        events = []
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(ssh_setup, 'local_identity', return_value={'series': 'Q1', 'soc_index': '1'}), \
                patch('primebuddy.integrations.primebot.device.audio_host', return_value=ssh_setup.MOTION_HOST), \
                patch.object(ssh_setup, 'setup_voice_ssh', side_effect=lambda *a: events.append('ssh')), \
                patch('primebuddy.config.runtime.private_runtime', return_value=Path(temp)), \
                patch('primebuddy.integrations.primebot.native_agent.agent_rpc',
                      side_effect=lambda *a: events.append(a[1]) or {'status': 'Idel(-1)', 'pid': -1}):
            ui = Mock()
            onboarding.configure_robot(ui)
        self.assertEqual(events, ['ssh', 'state'])
        self.assertTrue(any('当前未运行' in call.args[0] for call in ui.info.call_args_list))


if __name__ == '__main__':
    unittest.main()
