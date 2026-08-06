from pathlib import Path
from sys import stdout
from tempfile import gettempdir

from loguru import logger

from conf import BASE_DIR


def log_formatter(record: dict) -> str:
    """
    Formatter for log records.
    :param dict record: Log object containing log metadata & message.
    :returns: str
    """
    colors = {
        "TRACE": "#cfe2f3",
        "INFO": "#9cbfdd",
        "DEBUG": "#8598ea",
        "WARNING": "#dcad5a",
        "SUCCESS": "#3dd08d",
        "ERROR": "#ae2c2c"
    }
    color = colors.get(record["level"].name, "#b3cfe7")
    return f"<fg #70acde>{{time:YYYY-MM-DD HH:mm:ss}}</fg #70acde> | <fg {color}>{{level}}</fg {color}>: <light-white>{{message}}</light-white>\n"


def _resolve_log_root() -> Path | None:
    for candidate in (
        Path(BASE_DIR) / "logs",
        Path(gettempdir()) / "Fashetai" / "logs",
    ):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return None


LOG_ROOT = _resolve_log_root()


def configure_console_logger(sink) -> bool:
    if sink is None:
        return False
    logger.add(sink, colorize=True, format=log_formatter)
    return True


def create_logger(log_name: str, file_path: str):
    """
    Create custom logger for different business modules.
    :param str log_name: name of log
    :param str file_path: Optional path to log file
    :returns: Configured logger
    """
    def filter_record(record):
        return record["extra"].get("business_name") == log_name

    if LOG_ROOT is not None:
        try:
            logger.add(
                LOG_ROOT / Path(file_path).name,
                filter=filter_record,
                level="INFO",
                rotation="10 MB",
                retention="10 days",
                backtrace=True,
                diagnose=True,
            )
        except (OSError, ValueError):
            pass
    return logger.bind(business_name=log_name)


# Remove all existing handlers
logger.remove()
# Windows 的 PyInstaller windowed 模式没有 stdout，日志不能阻断应用启动。
configure_console_logger(stdout)

douyin_logger = create_logger('douyin', 'logs/douyin.log')
tencent_logger = create_logger('tencent', 'logs/tencent.log')
xhs_logger = create_logger('xhs', 'logs/xhs.log')
tiktok_logger = create_logger('tiktok', 'logs/tiktok.log')
youtube_logger = create_logger('youtube', 'logs/youtube.log')
meta_logger = create_logger('meta', 'logs/meta.log')
bilibili_logger = create_logger('bilibili', 'logs/bilibili.log')
kuaishou_logger = create_logger('kuaishou', 'logs/kuaishou.log')
baijiahao_logger = create_logger('baijiahao', 'logs/baijiahao.log')
xiaohongshu_logger = create_logger('xiaohongshu', 'logs/xiaohongshu.log')

# screenshot directories (created on import)
_SCREENSHOT_BASE = LOG_ROOT or (Path(gettempdir()) / "Fashetai" / "logs")
DOUYIN_SCREENSHOT_DIR = str(_SCREENSHOT_BASE / "douyin_screenshot")
KUAISHOU_SCREENSHOT_DIR = str(_SCREENSHOT_BASE / "kuaishou_screenshot")
TIKTOK_SCREENSHOT_DIR = str(_SCREENSHOT_BASE / "tiktok_screenshot")
XIAOHONGSHU_SCREENSHOT_DIR = str(_SCREENSHOT_BASE / "xiaohongshu_screenshot")
TENCENT_SCREENSHOT_DIR = str(_SCREENSHOT_BASE / "tencent_screenshot")

for screenshot_dir in (
    DOUYIN_SCREENSHOT_DIR,
    KUAISHOU_SCREENSHOT_DIR,
    TIKTOK_SCREENSHOT_DIR,
    XIAOHONGSHU_SCREENSHOT_DIR,
    TENCENT_SCREENSHOT_DIR,
):
    try:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
