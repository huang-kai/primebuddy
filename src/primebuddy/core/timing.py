"""仅输出阶段名称和耗时，不记录凭据或资源地址。"""
from contextlib import contextmanager
import json
import logging
import time

logger = logging.getLogger(__name__)


def progress(message):
    logger.info('%s', message)


@contextmanager
def measure(stage, message=None):
    if message:
        progress(message)
    start = time.monotonic()
    ok = False
    try:
        yield
        ok = True
    finally:
        logger.info('STAGE_TIME %s', json.dumps({'stage': stage,
              'seconds': round(time.monotonic() - start, 3), 'ok': ok},
              ensure_ascii=False))
