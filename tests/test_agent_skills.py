"""标准技能：目录发现、可选配置、固定脚本与发布安装。"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from primebuddy.config.storage import SetupError, config_path, write_config
from primebuddy.core.engine import Context, execute
from primebuddy.core.planner import plan
from primebuddy.skills import manager
from primebuddy.skills.registry import Registry
from primebuddy.skills.runtime import run_script
from primebuddy.tools.build_release import skill_zip


def create_skill(root, name='hello', body='向用户问候。'):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'SKILL.md').write_text('---\nname: ' + name + '\ndescription: 问候测试\n---\n' + body, encoding='utf-8')
    return root


class AgentSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        environment = patch.dict(os.environ, PRIMEBUDDY_HOME=str(self.home))
        environment.start()
        self.addCleanup(environment.stop)

    def test_document_only_skill_needs_no_package_dependencies_or_python_hooks(self):
        source = create_skill(self.root / 'hello')
        with patch('primebuddy.skills.manager.load_entry') as load, redirect_stdout(io.StringIO()):
            record = manager.install(source)
            catalog = Registry().catalog()
        load.assert_not_called()
        self.assertTrue(record['ready'] and record['enabled'])
        self.assertEqual(record['version'], '0.0.0')
        self.assertEqual(catalog[0]['name'], 'hello')
        self.assertNotIn('actions', catalog[0])

    def test_model_receives_only_available_installed_skills_after_reload(self):
        source = create_skill(self.root / 'hello', body='仅在选择技能后读取的详细步骤。')
        with redirect_stdout(io.StringIO()):
            manager.install(source)
        create_skill(self.home / 'skills/disabled', name='disabled')
        manager.set_enabled('disabled', False)
        pending = create_skill(self.home / 'skills/pending', name='pending')
        (pending / 'src').mkdir()
        (pending / 'src/configure.py').write_text('raise SystemExit(0)\n')
        write_config(pending / 'cfg/pkg.yaml', {'primebuddy': {'configure': ['src/configure.py']}})
        missing = create_skill(self.home / 'skills/missing', name='missing')
        write_config(missing / 'cfg/pkg.yaml', {'primebuddy': {'requires': {'config': ['account']}}})

        # 重新从磁盘建立注册表，再检查真正交给模型的首轮请求。
        registry = Registry()
        with patch('primebuddy.core.planner.chat', return_value={'role': 'assistant', 'content': '你好。'}) as chat:
            plan('你好', registry)
        system = chat.call_args.args[0][0]['content']
        catalog = json.loads(system.split('\n可发现技能：\n', 1)[1])
        self.assertEqual(catalog, [{'name': 'hello', 'description': '问候测试', 'location': 'SKILL.md'}])
        self.assertNotIn('仅在选择技能后读取', system)
        self.assertEqual(set(manager.discover_skill_records()), {'hello', 'disabled', 'pending', 'missing'})

    def test_copy_to_skills_directory_is_discovered_and_can_be_disabled(self):
        source = create_skill(self.home / 'skills/hello')
        self.assertEqual(Registry().catalog()[0]['name'], 'hello')
        manager.set_enabled('hello', False)
        self.assertEqual(Registry().catalog(), [])

        manager.set_enabled('hello', True)
        self.assertEqual(Registry().skills['hello']['root'], source.resolve())
        self.assertTrue(source.exists())
        (source / 'SKILL.md').unlink()
        source.rmdir()
        self.assertEqual(Registry().catalog(), [])

    def test_install_replaces_copied_skill_in_same_directory_only_when_requested(self):
        copied = create_skill(self.home / 'skills/hello', body='旧说明')
        source = create_skill(self.root / 'replacement', body='新说明')
        with redirect_stdout(io.StringIO()), self.assertRaises(SetupError):
            manager.install(source)
        self.assertIn('旧说明', (copied / 'SKILL.md').read_text())
        with redirect_stdout(io.StringIO()):
            manager.install(source, replace=True)
        self.assertEqual(Registry().skills['hello']['root'], copied.resolve())
        self.assertIn('新说明', (copied / 'SKILL.md').read_text())

    def test_hidden_staging_directory_is_not_discovered(self):
        create_skill(self.home / 'skills/.install-interrupted', name='incomplete')
        self.assertEqual(Registry().catalog(), [])

    def test_extra_skill_directory_and_duplicate_name_are_explicit(self):
        source = create_skill(self.root / 'external/hello')
        write_config(config_path('config.yaml'), {'skills': {'directories': [str(source.parent)]}})
        self.assertEqual(Registry().skills['hello']['root'], source.resolve())
        create_skill(self.home / 'skills/hello')
        with self.assertRaisesRegex(SetupError, '重复'):
            Registry()

    def test_metadata_and_file_paths_reject_invalid_documents_without_imports(self):
        source = create_skill(self.root / 'hello')
        for text in ('no frontmatter', '---\nname: hello\n---\n',
                     '---\nname: ../bad\ndescription: 测试\n---\n',
                     '---\nname: hello\nname: other\ndescription: 测试\n---\n'):
            with self.subTest(text=text), self.assertRaises(SetupError):
                (source / 'SKILL.md').write_text(text)
                manager.load_skill_metadata(source)
        create_skill(source)
        registry = Registry(source.parent)
        for path in ('../SKILL.md', '/etc/passwd', '.git/config', '_deps/module.py'):
            with self.subTest(path=path), self.assertRaises(SetupError):
                registry.read_file('hello', path)

    def test_console_script_can_save_config_and_manual_copy_is_ready_after_configuration(self):
        source = create_skill(self.home / 'skills/hello')
        (source / 'cfg').mkdir()
        (source / 'scripts').mkdir()
        write_config(source / 'cfg/pkg.yaml', {'primebuddy': {'configure': ['scripts/configure.py'],
                                                           'requires': {'config': ['greeting']}}})
        (source / 'scripts/configure.py').write_text('''import os
from primebuddy.config.storage import write_config
write_config(os.environ['PRIMEBUDDY_SKILL_CONFIG'], {'greeting': '你好'})
''')
        self.assertEqual(Registry().catalog(), [])
        self.assertTrue(manager.configure('hello', None))
        self.assertEqual(Registry().catalog()[0]['name'], 'hello')
        self.assertTrue(manager.load_skill_records()['hello']['ready'])

    def test_dependency_gates_are_visible_in_inventory_without_loading_code(self):
        source = create_skill(self.home / 'skills/hello')
        (source / 'cfg').mkdir()
        write_config(source / 'cfg/pkg.yaml', {'primebuddy': {'requires': {'env': ['PRIMEBUDDY_TEST_ONLY_TOKEN']}}})
        with patch.dict(os.environ):
            os.environ.pop('PRIMEBUDDY_TEST_ONLY_TOKEN', None)
            self.assertIn('hello', manager.discover_skill_records())
            self.assertEqual(Registry().catalog(), [])
            os.environ['PRIMEBUDDY_TEST_ONLY_TOKEN'] = 'fixture'
            self.assertEqual(Registry().catalog()[0]['name'], 'hello')

    def test_failed_console_upgrade_restores_previous_configuration(self):
        source = create_skill(self.root / 'hello')
        with redirect_stdout(io.StringIO()):
            manager.install(source)
        config = config_path('config.yaml', skill='hello')
        write_config(config, {'keep': '原配置'})
        before = manager.load_skill_records()
        (source / 'cfg').mkdir()
        (source / 'src').mkdir()
        write_config(source / 'cfg/pkg.yaml', {'primebuddy': {'configure': ['src/configure.py']}})
        (source / 'src/configure.py').write_text('''import os
import sys
from primebuddy.config.storage import write_config
write_config(os.environ['PRIMEBUDDY_SKILL_CONFIG'], {'keep': '不完整新配置'})
sys.exit(1)
''')
        with redirect_stdout(io.StringIO()), self.assertRaises(SetupError):
            manager.install(source, ui=lambda: None, replace=True)
        from primebuddy.config.storage import read_config
        self.assertEqual(read_config(config), {'keep': '原配置'})
        self.assertEqual(manager.load_skill_records(), before)

    def test_installing_replacement_does_not_delete_copied_source_directory(self):
        copied = create_skill(self.home / 'skills/hello')
        manager.set_enabled('hello', False)
        # 被复制的原目录由用户维护；换成托管包前移出扫描目录。
        moved = copied.rename(self.root / 'copied-source')
        records = manager.load_skill_records()
        records['hello']['directory'] = str(moved)
        write_config(config_path('skills.yaml'), records)
        source = create_skill(self.root / 'replacement')
        with redirect_stdout(io.StringIO()):
            manager.install(source, replace=True)
        self.assertTrue(moved.exists())

    def script_registry(self, code):
        source = create_skill(self.root / 'skills/hello')
        (source / 'src').mkdir(exist_ok=True)
        (source / 'src/task.py').write_text(code)
        return Registry(source.parent)

    def test_fixed_script_output_is_returned_and_failure_cannot_report_success(self):
        registry = self.script_registry('import sys\nprint("你好 " + sys.argv[1])\n')
        args = {'path': 'src/task.py', 'arguments': ['世界;$(do-not-execute)']}
        result = run_script(registry.skills['hello'], args, Context(simulate=False))
        self.assertIn('世界;$(do-not-execute)', result['message'])
        (registry.skills['hello']['root'] / 'src/task.py').write_text('import sys\nprint("成功")\nsys.exit(1)\n')
        result = run_script(registry.skills['hello'], args, Context(simulate=False))
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('成功', result['message'])

    def test_simulation_never_imports_or_executes_script(self):
        registry = self.script_registry('raise AssertionError("不应运行")\n')
        proposal = {'reply': '', 'tasks': [{'id': 'one', 'skill': 'hello', 'action': 'script',
                     'args': {'path': 'src/task.py', 'arguments': []}, 'after': []}]}
        with patch('primebuddy.skills.runtime.subprocess.Popen') as process:
            result = execute(proposal, registry, Context(), lambda _: None)
        process.assert_not_called()
        self.assertTrue(result['one']['simulated'])

    def test_cancelling_script_waits_for_finally_cleanup(self):
        started, cleaned = self.root / 'started', self.root / 'cleaned'
        code = '''from pathlib import Path
import time
try:
    Path(%r).write_text('started')
    while True: time.sleep(.02)
finally:
    Path(%r).write_text('cleaned')
''' % (str(started), str(cleaned))
        registry = self.script_registry(code)
        context = Context(simulate=False)
        def cancel():
            deadline = time.monotonic() + 3
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            context.cancelled.set()
        thread = threading.Thread(target=cancel)
        thread.start()
        try:
            with self.assertRaisesRegex(RuntimeError, '取消'):
                run_script(registry.skills['hello'], {'path': 'src/task.py', 'arguments': []}, context)
        finally:
            thread.join(4)
        self.assertTrue(cleaned.exists())

    def test_standard_skill_files_and_license_survive_packaging(self):
        source = create_skill(self.root / 'source')
        (source / 'LICENSE').write_text('test-only license')
        for name in ('scripts', 'references', 'assets'):
            (source / name).mkdir()
            (source / name / 'example.txt').write_text('公开资源')
        archive = self.root / 'hello.zip'
        skill_zip(source, archive)
        with zipfile.ZipFile(archive) as package:
            self.assertEqual(package.read('LICENSE').decode(), 'test-only license')
            self.assertIn('scripts/example.txt', package.namelist())
            self.assertIn('references/example.txt', package.namelist())
        with redirect_stdout(io.StringIO()):
            record = manager.install(archive)
        self.assertTrue(record['ready'])
        self.assertEqual((manager.get_skill_package_directory(record) / 'LICENSE').read_text(), 'test-only license')
        self.assertEqual(Registry().read_file('hello', 'references/example.txt')['content'], '公开资源')

    def test_cli_install_without_configuration_does_not_require_a_terminal(self):
        source = create_skill(self.root / 'hello')
        framework = Path(__file__).resolve().parents[1]
        process = subprocess.run([sys.executable, str(framework / 'main.py'), 'skill', 'install', str(source)],
                                 env=dict(os.environ), capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(manager.load_skill_records()['hello']['ready'])
