"""应用日志配置；轮转使用标准库，仅补充私有权限和写盘失败处理。"""
from contextlib import contextmanager
import logging
import logging.config
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

from primebuddy.config.storage import SetupError, load_app_config


class PrivateRotatingFileHandler(RotatingFileHandler):
    failed = False

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, 'a', encoding='utf-8')

    def handleError(self, record):
        self.failed = True
        try:
            sys.stderr.write('PrimeBuddy 日志写入失败，本次会话继续输出到终端。\n')
        except OSError:
            pass

    def emit(self, record):
        if not self.failed:
            super().emit(record)


def log_directory():
    config = load_app_config()['logging']
    return Path(os.environ.get('PRIMEBUDDY_LOG_DIR', config['directory'])).expanduser()


@contextmanager
def runtime_log():
    settings = load_app_config()['logging']
    directory = log_directory()
    logger = logging.getLogger('primebuddy')
    previous = logger.handlers[:], logger.level, logger.propagate
    config = {
        'version': 1, 'disable_existing_loggers': False,
        'formatters': {'console': {'format': '%(message)s'},
                       'file': {'format': '%(asctime)s [%(levelname)s] %(name)s: %(message)s', 'datefmt': '%Y-%m-%d %H:%M:%S'}},
        'handlers': {'console': {'class': 'logging.StreamHandler', 'stream': 'ext://sys.stdout', 'formatter': 'console'},
                     'file': {'class': 'primebuddy.config.logging_setup.PrivateRotatingFileHandler',
                              'filename': str(directory / 'primebuddy.log'), 'formatter': 'file', 'encoding': 'utf-8',
                              'maxBytes': settings['max_bytes'], 'backupCount': settings['backup_count']}},
        'loggers': {'primebuddy': {'level': settings['level'], 'handlers': ['console', 'file'], 'propagate': False}},
    }
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        logging.config.dictConfig(config)
    except (OSError, ValueError):
        for handler in logger.handlers:
            if handler not in previous[0]:
                handler.close()
        logger.handlers, logger.level, logger.propagate = previous
        raise SetupError('日志目录不可写或日志配置无效，请检查：' + str(directory)) from None
    try:
        yield
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers, logger.level, logger.propagate = previous
