import json

from src.config import load_config
from src.logging_setup import close_logging, configure_logging


def test_json_log_contains_safe_fields_only(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('{}')
    config = load_config(config_path)
    logger = configure_logging(config, 'run-test', 'crawl')
    logger.info('private confession text', extra={'event': 'post_saved', 'record_id': 'post_123',
                                                 'cookie': 'private-cookie', 'content': 'private body'})
    close_logging()
    data = (config.paths.log_dir / 'crawl.jsonl').read_text(encoding='utf-8')
    record = json.loads(data)
    assert record['event'] == 'post_saved'
    assert record['run_id'] == 'run-test'
    assert record['command'] == 'crawl'
    assert record['timestamp'].endswith('Z')
    assert record['record_id'] == 'post_123'
    assert 'private' not in data


def test_reconfigure_closes_old_handlers(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('{}')
    config = load_config(config_path)
    logger = configure_logging(config, 'first', 'crawl')
    old = list(logger.handlers)
    configure_logging(config, 'second', 'crawl')
    assert len(logger.handlers) == 2
    assert all(handler not in logger.handlers for handler in old)
    close_logging()


def test_round_timing_fields_are_written_without_post_content(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('{}')
    config = load_config(config_path)
    logger = configure_logging(config, 'timing-test', 'crawl')
    timings = {'session_ms': 10.5, 'extraction_ms': 20.25, 'storage_ms': 2.5,
               'scroll_ms': 100.0, 'duration_ms': 150.0}
    logger.info('crawl_timing', extra={'event': 'crawl_timing', **timings, 'content': 'private body'})
    close_logging()
    record = json.loads((config.paths.log_dir / 'crawl.jsonl').read_text(encoding='utf-8'))
    assert all(record.get(key) == value for key, value in timings.items())
    assert 'content' not in record
