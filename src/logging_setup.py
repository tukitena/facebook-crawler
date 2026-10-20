import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from src.config import AppConfig
from src.models import utc_now

SAFE_FIELDS = ('record_id', 'attempt', 'duration_ms', 'error_code', 'seen_posts', 'saved_posts',
               'duplicate_posts', 'candidate_segments', 'extraction_errors', 'media_only_posts',
               'scrolls', 'no_progress_rounds', 'status', 'stop_reason', 'round_index',
               'session_ms', 'extraction_ms', 'storage_ms', 'scroll_ms')


class JsonFormatter(logging.Formatter):
    def __init__(self, run_id: str, command: str):
        super().__init__()
        self.run_id = run_id
        self.command = command

    def format(self, record):
        value = {'timestamp': utc_now(), 'level': record.levelname, 'run_id': self.run_id,
                 'command': self.command, 'event': getattr(record, 'event', 'diagnostic')}
        value.update({name: getattr(record, name) for name in SAFE_FIELDS if hasattr(record, name)})
        return json.dumps(value, ensure_ascii=False, allow_nan=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        return f'{record.levelname}: {getattr(record, "event", "diagnostic")} {getattr(record, "error_code", "")}'.rstrip()


def close_logging() -> None:
    logger = logging.getLogger('crawler')
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()


def configure_logging(config: AppConfig, run_id: str, command: str) -> logging.Logger:
    close_logging()
    config.paths.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('crawler')
    logger.setLevel(config.logging.level)
    logger.propagate = False
    file_handler = RotatingFileHandler(config.paths.log_dir / 'crawl.jsonl',
                                      maxBytes=config.logging.max_bytes, backupCount=config.logging.backup_count,
                                      encoding='utf-8')
    file_handler.setFormatter(JsonFormatter(run_id, command))
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(ConsoleFormatter())
    logger.addHandler(file_handler)
    logger.addHandler(console)
    return logger
