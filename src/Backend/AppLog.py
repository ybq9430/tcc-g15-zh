"""把 print 和未捕获异常写进轮转日志文件。

打包时用了 -w（无控制台窗口），sys.stdout 是 None，所有 print 都会被丢弃。
用户反馈"风扇不动了""自己退出了"时完全没有线索，所以这里统一接管输出：

    %LOCALAPPDATA%\\TCC-G15\\logs\\app.log

单文件上限 1 MB，保留 3 个备份，不需要用户手动清理。
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

APP_DIR_NAME = 'TCC-G15'
LOG_FILE_NAME = 'app.log'
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

_inited = False


def logDir() -> str:
    # 某些精简终端环境里 LOCALAPPDATA 可能没导出，退回到约定路径，别把日志写到用户根目录
    base = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
    return os.path.join(base, APP_DIR_NAME, 'logs')


def logPath() -> str:
    return os.path.join(logDir(), LOG_FILE_NAME)


class _StreamToLogger:
    """顶替 sys.stdout / sys.stderr，按行转交给 logging"""

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level
        self._buf = ''

    def write(self, message: str) -> int:
        if not message:
            return 0
        self._buf += message
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            line = line.rstrip()
            if line:
                self._logger.log(self._level, line)
        return len(message)

    def flush(self) -> None:
        if self._buf.strip():
            self._logger.log(self._level, self._buf.strip())
            self._buf = ''

    def isatty(self) -> bool:
        return False


def setupLogging(appVersion: str = '', level: int = logging.INFO) -> str:
    """配置根日志。返回日志文件路径，失败时返回空串。

    重复调用无副作用（进程内只初始化一次）。
    """
    global _inited
    if _inited:
        return logPath()
    _inited = True

    try:
        os.makedirs(logDir(), exist_ok=True)
    except Exception:
        return ''

    logger = logging.getLogger('tcc')
    if logger.handlers:
        return logPath()
    logger.setLevel(level)
    logger.propagate = False

    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S')

    try:
        fh = RotatingFileHandler(logPath(), maxBytes=MAX_BYTES,
                                 backupCount=BACKUP_COUNT, encoding='utf-8')
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        return ''

    # 从源码/控制台运行时，仍然同步打一份到真实控制台，方便开发
    if sys.__stdout__ is not None and not os.environ.get('TCC_NO_CONSOLE_LOG'):
        try:
            sh = logging.StreamHandler(sys.__stdout__)
            sh.setFormatter(fmt)
            logger.addHandler(sh)
        except Exception:
            pass

    sys.stdout = _StreamToLogger(logger, logging.INFO)  # type: ignore[assignment]
    sys.stderr = _StreamToLogger(logger, logging.ERROR)  # type: ignore[assignment]

    def _excepthook(excType, excValue, tb) -> None:
        logger.error('未捕获的异常：', exc_info=(excType, excValue, tb))

    sys.excepthook = _excepthook

    logger.info('=== 启动 TCC-G15 %s，Python %s，日志：%s ===',
                appVersion or '', sys.version.split()[0], logPath())
    return logPath()


def log(text: str, level: int = logging.INFO) -> None:
    """给业务代码用的轻量日志入口"""
    logging.getLogger('tcc').log(level, text)
