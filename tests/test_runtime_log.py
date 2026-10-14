"""日志输出、轮转、并发记录和异常退出；不连接机器人。"""
from contextlib import redirect_stdout, redirect_stderr
import io
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from primebuddy.cli.commands import main
from primebuddy.config.logging_setup import PrivateRotatingFileHandler, runtime_log
from primebuddy.config.storage import SetupError


class RuntimeLogTests(unittest.TestCase):
    def test_module_logs_reach_console_and_private_file_without_intercepting_stdout(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, PRIMEBUDDY_LOG_DIR=temp):
            output = io.StringIO()
            with redirect_stdout(output), runtime_log():
                logging.getLogger('primebuddy.voice').info('TTS 你好')
                logging.getLogger('primebuddy.skills.diangetai').error('播放失败')
                print('命令结果')
                self.assertIs(sys.stdout, output)
            path = Path(temp) / 'primebuddy.log'
            text = path.read_text()
            self.assertIn('[INFO] primebuddy.voice: TTS 你好', text)
            self.assertIn('[ERROR] primebuddy.skills.diangetai: 播放失败', text)
            self.assertNotIn('命令结果', text)
            self.assertIn('命令结果', output.getvalue())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rotation_is_bounded_and_keeps_private_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'primebuddy.log'
            handler = PrivateRotatingFileHandler(path, maxBytes=60, backupCount=2, encoding='utf-8')
            try:
                for i in range(20):
                    handler.handle(logging.LogRecord('primebuddy', logging.INFO, '', 0, '%s complete log line', (i,), None))
            finally:
                handler.close()
            self.assertEqual(len(list(Path(temp).iterdir())), 3)
            self.assertIn('19 complete log line', path.read_text())
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in Path(temp).iterdir()))

    def test_concurrent_records_remain_complete_and_debug_is_filtered(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, PRIMEBUDDY_LOG_DIR=temp):
            logger = logging.getLogger('primebuddy.worker')
            with redirect_stdout(io.StringIO()), runtime_log():
                logger.debug('filtered')
                threads = [threading.Thread(target=logger.info, args=('%s-done', name)) for name in ('a', 'b')]
                for thread in threads: thread.start()
                for thread in threads: thread.join(timeout=5)
            text = (Path(temp) / 'primebuddy.log').read_text()
            self.assertIn('a-done', text)
            self.assertIn('b-done', text)
            self.assertNotIn('filtered', text)

    def test_cli_records_start_error_without_starting_hardware(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, PRIMEBUDDY_LOG_DIR=temp), \
                patch.object(sys, 'argv', ['primebuddy', 'start']), \
                patch('primebuddy.cli.commands.dispatch_command', side_effect=SetupError('配置格式错误')), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 1)
            self.assertIn('配置格式错误', (Path(temp) / 'primebuddy.log').read_text())

    def test_unwritable_log_fails_before_platform_start(self):
        with patch.object(sys, 'argv', ['primebuddy', 'start']), \
                patch('primebuddy.config.logging_setup.PrivateRotatingFileHandler', side_effect=PermissionError), \
                patch('primebuddy.config.logging_setup.Path.mkdir'), \
                patch('primebuddy.cli.commands.dispatch_command') as dispatch, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(), 1)
            dispatch.assert_not_called()
            self.assertIn('日志目录不可写', errors.getvalue())

    def test_disk_error_does_not_interrupt_platform_cleanup_or_console(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, PRIMEBUDDY_LOG_DIR=temp):
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors), runtime_log():
                logger = logging.getLogger('primebuddy')
                handler = next(h for h in logger.handlers if isinstance(h, PrivateRotatingFileHandler))
                with patch.object(handler.stream, 'write', side_effect=OSError('disk full')):
                    logger.info('播放结束')
                    logger.info('RESTORE_CONFIRMED')
            self.assertEqual(errors.getvalue().count('日志写入失败'), 1)
            self.assertIn('RESTORE_CONFIRMED', output.getvalue())
