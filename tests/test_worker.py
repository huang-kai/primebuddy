"""旧 Python skill 只在兼容子进程加载；退出清理不依赖 stdout 协议。"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from primebuddy.config.storage import write_config


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skill = self.root / 'skill'
        self.skill.mkdir()
        (self.skill / 'SKILL.md').write_text('---\nname: fixture\ndescription: worker fixture\n---\n测试')
        write_config(self.skill / 'cfg/pkg.yaml', {'primebuddy': {'entrypoint': 'entry.py', 'actions': {
            'play': {'handler': 'perform', 'parameters': {'name': {'type': 'string'}}}}}})
        self.request = self.root / 'request.json'
        self.result = self.root / 'result.json'
        self.env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))

    def prepare(self, source, operation='run', **extra):
        (self.skill / 'entry.py').write_text(textwrap.dedent(source))
        value = dict(root=str(self.skill), entrypoint='entry.py', operation=operation, action='play',
                     args={'name': '测试'}, context={'runtime': str(self.root),
                     'skill_configs': {'fixture': {'saved': 9}}, 'simulate': False})
        value.update(extra)
        self.request.write_text(json.dumps(value))

    def command(self):
        return [sys.executable, '-m', 'primebuddy.skills.worker', str(self.request), str(self.result)]

    def run_worker(self):
        result = subprocess.run(self.command(), env=self.env, capture_output=True, text=True, timeout=5)
        return result, json.loads(self.result.read_text())

    def test_native_import_and_run_stay_in_child_and_shutdown_runs(self):
        marker = self.root / 'cleanup'
        self.prepare('''
            import os
            from pathlib import Path
            print('business stdout is not the protocol')
            def perform(context, name):
                context.checkpoint()
                return {'pid': os.getpid(), 'runtime': str(context.runtime),
                        'saved': context.config_for('fixture')['saved'], 'name': name}
            def shutdown():
                Path(%r).write_text('closed')
        ''' % str(marker))
        result, response = self.run_worker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(response['value']['pid'], os.getpid())
        self.assertEqual(response['value']['saved'], 9)
        self.assertIn('not the protocol', result.stdout)
        self.assertEqual(marker.read_text(), 'closed')
        self.assertFalse(self.result.stat().st_mode & 0o077)
        self.assertFalse(any(str(self.skill / 'entry.py') == getattr(module, '__file__', None)
                             for module in tuple(sys.modules.values()) if module is not None))

    def test_optional_hooks_can_be_absent(self):
        for operation in ('validate', 'preflight', 'describe', 'planning_context', 'confirmation'):
            with self.subTest(operation=operation):
                self.prepare('def perform(context, name): return name', operation)
                process, response = self.run_worker()
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(response, {'value': None})

    def test_optional_hook_signatures_and_context(self):
        source = '''
            def perform(context, name): return name
            def validate(action, args): return [action, args['name']]
            def preflight(action, args, context): return context.simulate
            def describe(action, args): return 'describe:' + args['name']
            def planning_context(context): return context.config_for('fixture')
            def confirmation(action, args, text, snapshot): return [text, snapshot['state']]
        '''
        expected = {'validate': ['play', '测试'], 'preflight': False, 'describe': 'describe:测试',
                    'planning_context': {'saved': 9}, 'confirmation': ['request', 'ready']}
        for operation, value in expected.items():
            with self.subTest(operation=operation):
                self.prepare(source, operation, text='request', snapshot={'state': 'ready'})
                process, response = self.run_worker()
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(response, {'value': value})

    def test_error_body_is_redacted_and_shutdown_still_runs(self):
        marker = self.root / 'cleanup'
        self.prepare('''
            from pathlib import Path
            def perform(context, name): raise ValueError('secret-token-body')
            def shutdown(): Path(%r).write_text('closed')
        ''' % str(marker))
        process, response = self.run_worker()
        self.assertEqual(process.returncode, 1)
        self.assertEqual(response, {'error_type': 'ValueError'})
        self.assertNotIn('secret-token-body', process.stdout + process.stderr + self.result.read_text())
        self.assertEqual(marker.read_text(), 'closed')

    def test_cancellation_signal_sets_event_and_runs_finally_and_shutdown(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                ready, cleaned = self.root / 'ready', self.root / 'cleaned'
                ready.unlink(missing_ok=True)
                cleaned.unlink(missing_ok=True)
                self.prepare('''
                    import time
                    from pathlib import Path
                    def perform(context, name):
                        Path(%r).touch()
                        try:
                            while True:
                                context.checkpoint()
                                time.sleep(.01)
                        finally:
                            Path(%r).write_text('finally')
                    def shutdown():
                        path = Path(%r)
                        path.write_text(path.read_text() + '+shutdown')
                ''' % (str(ready), str(cleaned), str(cleaned)))
                process = subprocess.Popen(self.command(), env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists())
                    process.send_signal(sig)
                    output, errors = process.communicate(timeout=3)
                    self.assertEqual(process.returncode, 1, errors)
                    self.assertEqual(cleaned.read_text(), 'finally+shutdown')
                    self.assertEqual(json.loads(self.result.read_text()), {'error_type': 'RuntimeError'})
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)

    def test_entrypoint_escape_and_handler_override_are_rejected_before_import(self):
        for extra in ({'entrypoint': '../outside.py'}, {'handler': 'shutdown'}):
            with self.subTest(extra=extra):
                self.prepare("raise AssertionError('must not import')", **extra)
                process, response = self.run_worker()
                self.assertEqual(process.returncode, 1)
                self.assertEqual(response, {'error_type': 'ValueError'})


if __name__ == '__main__':
    unittest.main()
