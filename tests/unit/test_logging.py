import json
from dataclasses import asdict

from src.config import load_config
from src.logging_setup import close_logging, configure_logging
from src.preprocess.models import PreprocessStats


def test_preprocess_log_allows_counters_not_text(tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('{}')
    config = load_config(config_path)
    logger = configure_logging(config, 'preprocess-test', 'preprocess')
    counters = {name: i for i, name in enumerate(asdict(PreprocessStats()), 1)}
    logger.info('private-body', extra={'event': 'run_finished', **counters,
                                     'content': 'private-body', 'excerpt': 'private-excerpt', 'note': 'private-note'})
    close_logging()
    text = (config.paths.log_dir / 'crawl.jsonl').read_text(encoding='utf-8')
    record = json.loads(text)
    assert record['command'] == 'preprocess'
    assert all(record.get(name) == value for name, value in counters.items())
    assert 'private' not in text


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


def test_classification_events_keep_counters_and_exclude_private_payloads(tmp_path, capsys):
    from src.classify.models import ClassificationStats

    config_path = tmp_path / 'config.yaml'
    config_path.write_text('{}')
    config = load_config(config_path)
    logger = configure_logging(config, 'classification-test', 'classify')
    counters = asdict(ClassificationStats(total=10, already_done=3, attempted=4,
                    newly_saved=2, failed=2, remaining=5, reviewed_new=1, reviewed_total=2))
    for event in ('classification_retry', 'label_saved', 'classification_failed', 'run_finished'):
        logger.warning('private confession', extra={
            'event': event, 'attempt': 2, 'duration_ms': 123, 'error_code': 'MODEL_TIMEOUT',
            'classification_fingerprint': 'a' * 64, **counters,
            'content': 'private confession', 'prompt': 'private system prompt',
            'response_body': 'private HTTP error body', 'reason': 'private model reason',
        })
    close_logging()
    output = (config.paths.log_dir / 'crawl.jsonl').read_text(encoding='utf-8')
    records = [json.loads(line) for line in output.splitlines()]
    assert len(records) == 4
    for record in records:
        assert record['command'] == 'classify'
        assert all(record.get(name) == value for name, value in counters.items())
        assert record['attempt'] == 2 and record['duration_ms'] == 123
        assert record['error_code'] == 'MODEL_TIMEOUT'
        assert record['classification_fingerprint'] == 'a' * 64
    assert 'private' not in output + capsys.readouterr().err
