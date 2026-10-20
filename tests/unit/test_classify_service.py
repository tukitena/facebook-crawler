import csv
from dataclasses import asdict
from importlib import import_module
import json
import logging
from pathlib import Path
import signal

import pytest

from src.classify.client import ModelAttemptError
from src.classify.models import ModelIdentity, ModelPrediction
from src.errors import AppError
from src.preprocess.service import preprocess
from src.storage import resource_lock


def service():
    return import_module('src.classify.service')


def prediction(**changes):
    return ModelPrediction(**(dict(label='Positive', confidence=0.9, reason='UIT hỗ trợ tốt.',
        target_relation='related', mixed=False, needs_review=False) | changes))


def invalid():
    return ModelAttemptError('MODEL_INVALID_OUTPUT', 'invalid', retryable=True, service_failure=False)


def unavailable(retry_after=None):
    return ModelAttemptError('MODEL_SERVICE_ERROR', 'unavailable', retryable=True,
                             service_failure=True, retry_after=retry_after)


class FakeClient:
    def __init__(self, results=(), preflight_error=None):
        self.results = iter(results)
        self.preflight_error = preflight_error
        self.calls = []
        self.preflights = 0
        self.closed = False

    def __call__(self, config, bundle):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def preflight(self):
        self.preflights += 1
        if self.preflight_error:
            raise self.preflight_error
        return ModelIdentity(tag='qwen3:8b', digest='a' * 64, server_version='test')

    def classify_once(self, content, *, repair=False):
        self.calls.append((content, repair))
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def config(raw_dataset):
    cfg = raw_dataset(['#001\nUIT hỗ trợ sinh viên học tập.',
                       '#002\nPhòng học UIT rất thoải mái.', '#003\nUIT tốt'])
    cfg.classification.prompt_path = Path('src/classify/prompts/uit-impact-v1.txt').resolve()
    preprocess(cfg)
    return cfg


def rows(config, name='labeled.csv'):
    with (config.paths.labeled_dir / name).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def report(config, result):
    return json.loads((config.paths.reports_dir / f'{result.run_id}.json').read_text(encoding='utf-8'))


def test_success_review_counters_progress_and_safe_report(config, caplog):
    caplog.set_level(logging.INFO, logger='crawler')
    client = FakeClient([prediction(confidence=0.5), prediction(), prediction()])
    updates = []
    result = service().classify(config, client_factory=client, progress=updates.append,
                                clock=iter([1.0, 2.5]).__next__)
    assert (result.status, result.exit_code, result.stop_reason) == ('complete', 0, 'completed')
    assert asdict(result.stats) == dict(total=3, already_done=0, attempted=3, newly_saved=3,
        failed=0, remaining=0, reviewed_new=2, reviewed_total=2)
    assert updates[0].newly_saved == 1 and updates[-1].newly_saved == 3
    assert client.closed
    data = report(config, result)
    assert data['stats'] == asdict(result.stats) and data['duration_ms'] == 1500
    assert data['command'] == 'classify' and data['input_generation_id']
    assert set(data['input_hashes']) == {'confessions.csv', 'sources.csv'}
    assert len(data['classification_fingerprint']) == 64
    assert data['model_identity']['digest'] == 'a' * 64
    serialized = json.dumps(data, ensure_ascii=False)
    assert 'UIT hỗ trợ sinh viên học tập.' not in serialized
    assert 'UIT hỗ trợ tốt.' not in serialized
    events = [getattr(item, 'event', None) for item in caplog.records]
    assert events.count('label_saved') == 3 and events[-1] == 'run_finished'


def test_retry_repairs_within_three_request_budget(config):
    client = FakeClient([unavailable(), invalid(), prediction()])
    sleeps = []
    result = service().classify(config, 1, client_factory=client, sleep=sleeps.append)
    assert result.stats.attempted == result.stats.newly_saved == 1
    assert result.status == 'partial' and result.exit_code == 3
    assert [repair for _, repair in client.calls] == [False, False, True]
    assert sleeps == [1, 2] and len(rows(config)) == 1


def test_limit_counts_failed_id_once(config):
    client = FakeClient([invalid(), invalid(), invalid(), prediction()])
    result = service().classify(config, 1, client_factory=client, sleep=lambda _: None)
    assert (result.stats.attempted, result.stats.failed, result.stats.newly_saved, result.stats.remaining) == (1, 1, 0, 3)
    assert (result.status, result.exit_code) == ('partial', 3)
    assert rows(config) == []
    assert rows(config, 'classification_failures.csv')[0]['attempts'] == '3'
    assert len(client.calls) == 3


def test_retry_after_is_used(config):
    client = FakeClient([unavailable(13), prediction()])
    sleeps = []
    result = service().classify(config, 1, client_factory=client, sleep=sleeps.append)
    assert sleeps == [13] and result.stats.newly_saved == 1


def test_fatal_request_stops_immediately_and_reports_failure(config):
    client = FakeClient([AppError('MODEL_REQUEST_FAILED', 'safe 404')])
    run_id = '00000000-0000-0000-0000-000000000005'
    with pytest.raises(AppError) as error:
        service().classify(config, client_factory=client, run_id=run_id)
    assert error.value.code == 'MODEL_REQUEST_FAILED'
    data = json.loads((config.paths.reports_dir / f'{run_id}.json').read_text())
    assert data['exit_code'] == 1 and data['stats']['attempted'] == 1
    assert data['stats']['failed'] == 1 and data['stats']['remaining'] == 3
    assert rows(config, 'classification_failures.csv')[0]['attempts'] == '1'
    assert len(client.calls) == 1


def test_three_exhausted_service_ids_stop_outage(config, raw_dataset):
    config = raw_dataset([f'#{index:03}\nUIT có nội dung tổng hợp số {index}.' for index in range(1, 5)])
    config.classification.prompt_path = Path('src/classify/prompts/uit-impact-v1.txt').resolve()
    preprocess(config)
    client = FakeClient([unavailable() for _ in range(9)])
    run_id = '00000000-0000-0000-0000-000000000006'
    with pytest.raises(AppError) as error:
        service().classify(config, client_factory=client, run_id=run_id, sleep=lambda _: None)
    assert error.value.code == 'MODEL_UNAVAILABLE'
    data = json.loads((config.paths.reports_dir / f'{run_id}.json').read_text())
    assert data['stats']['failed'] == 3 and data['stats']['attempted'] == 3
    assert data['stats']['remaining'] == 4
    assert len(client.calls) == 9 and len(rows(config, 'classification_failures.csv')) == 3


def test_mixed_validation_resets_service_failure_streak(config):
    config.classification.consecutive_service_failures = 2
    client = FakeClient([unavailable(), unavailable(), unavailable(),
                         unavailable(), invalid(), unavailable(),
                         unavailable(), unavailable(), unavailable()])
    result = service().classify(config, client_factory=client, sleep=lambda _: None)
    assert result.status == 'partial' and result.stats.failed == 3


def test_resume_skips_saved_and_retries_old_failure(config):
    service().classify(config, 1, client_factory=FakeClient([prediction()]))
    service().classify(config, 1, client_factory=FakeClient([invalid()] * 3), sleep=lambda _: None)
    client = FakeClient([prediction(), prediction()])
    result = service().classify(config, client_factory=client)
    assert (result.stats.already_done, result.stats.newly_saved, result.stats.failed) == (1, 2, 0)
    assert result.status == 'complete' and len(rows(config)) == 3
    assert rows(config, 'classification_failures.csv') == []
    assert client.calls[0][0] == 'Phòng học UIT rất thoải mái.'


@pytest.mark.parametrize('empty', [False, True])
def test_empty_or_completed_still_preflights_but_sends_no_input(config, raw_dataset, empty):
    if empty:
        config = raw_dataset([])
        config.classification.prompt_path = Path('src/classify/prompts/uit-impact-v1.txt').resolve()
        preprocess(config)
    else:
        service().classify(config, client_factory=FakeClient([prediction()] * 3))
    client = FakeClient()
    result = service().classify(config, client_factory=client)
    assert result.status == 'complete' and result.stats.attempted == 0
    assert client.preflights == 1 and client.calls == []


def test_budget_failure_is_zero_requests(config):
    config.classification.max_content_utf8_bytes = 5
    client = FakeClient()
    result = service().classify(config, 1, client_factory=client)
    failure = rows(config, 'classification_failures.csv')[0]
    assert failure['error_code'] == 'INPUT_TOO_LONG' and failure['attempts'] == '0'
    assert result.stats.failed == 1 and client.calls == []


def test_repair_budget_does_not_send_second_request(config):
    from src.classify.prompt import build_messages, load_prompt
    content = 'UIT hỗ trợ sinh viên học tập.'
    messages = build_messages(load_prompt(config), content, config)
    size = len(json.dumps(messages, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    config.ollama.num_ctx = size + config.ollama.num_predict + config.classification.context_margin
    client = FakeClient([invalid()])
    result = service().classify(config, 1, client_factory=client, sleep=lambda _: None)
    failure = rows(config, 'classification_failures.csv')[0]
    assert failure['error_code'] == 'INPUT_TOO_LONG' and failure['attempts'] == '1'
    assert result.stats.failed == 1 and len(client.calls) == 1


def test_preflight_failure_does_not_create_labels(config):
    with pytest.raises(AppError):
        service().classify(config, client_factory=FakeClient(preflight_error=AppError('MODEL_NOT_FOUND', 'missing', 2)))
    assert not config.paths.labeled_dir.exists()


def test_invalid_generation_does_not_call_client(config):
    (config.paths.processed_dir / 'confessions.csv').write_bytes(b'broken')
    client = FakeClient()
    with pytest.raises(AppError) as error:
        service().classify(config, client_factory=client)
    assert error.value.code == 'INVALID_GENERATION' and client.preflights == 0


def test_lock_contention_has_no_report_or_model_call(config):
    before = set(config.paths.reports_dir.iterdir())
    client = FakeClient()
    with resource_lock(config.paths.data_dir / '.dataset.lock'):
        with pytest.raises(AppError) as error:
            service().classify(config, client_factory=client)
    assert error.value.code == 'RESOURCE_LOCKED' and error.value.exit_code == 2
    assert set(config.paths.reports_dir.iterdir()) == before and not client.preflights


@pytest.mark.parametrize('kwargs', [{'limit': 0}, {'limit': True}, {'limit': -1},
                                  {'run_id': '../outside'}, {'run_id': ''}])
def test_invalid_run_parameters_do_not_touch_dataset(config, kwargs):
    before = set(config.paths.reports_dir.iterdir())
    with pytest.raises(AppError) as error:
        service().classify(config, client_factory=FakeClient(), **kwargs)
    assert error.value.exit_code == 2
    assert set(config.paths.reports_dir.iterdir()) == before


def test_config_path_collision_rejected_before_lock(config):
    with pytest.raises(AppError) as error:
        service().classify(config, config_path=config.paths.labeled_dir / 'labeled.csv', client_factory=FakeClient())
    assert error.value.code == 'CONFIG_ERROR' and not config.paths.labeled_dir.exists()


def test_interrupt_request_preserves_saved_labels_and_pending_id(config):
    result = service().classify(config, client_factory=FakeClient([prediction(), KeyboardInterrupt()]))
    assert (result.status, result.exit_code) == ('interrupted', 130)
    assert (result.stats.attempted, result.stats.newly_saved, result.stats.failed, result.stats.remaining) == (2, 1, 0, 2)
    assert len(rows(config)) == 1 and report(config, result)['status'] == 'interrupted'


def test_interrupt_retry_backoff_does_not_create_failure(config):
    def interrupt(_):
        raise KeyboardInterrupt
    result = service().classify(config, client_factory=FakeClient([invalid()]), sleep=interrupt)
    assert result.exit_code == 130 and result.stats.attempted == 1 and result.stats.failed == 0
    assert not (config.paths.labeled_dir / 'classification_failures.csv').exists()


@pytest.mark.parametrize('operation', ['append', 'save_failure'])
def test_signal_after_commit_keeps_counters_consistent(config, monkeypatch, operation):
    module = service()
    original = getattr(module.LabelStore, operation)
    def interrupt(store, record):
        original(store, record)
        signal.raise_signal(signal.SIGINT)
    monkeypatch.setattr(module.LabelStore, operation, interrupt)
    client = FakeClient([prediction()] if operation == 'append' else [invalid()] * 3)
    result = module.classify(config, client_factory=client, sleep=lambda _: None)
    assert result.exit_code == 130
    assert result.stats.newly_saved == (1 if operation == 'append' else 0)
    assert result.stats.failed == (1 if operation == 'save_failure' else 0)
    assert report(config, result)['stats'] == asdict(result.stats)


def test_interrupt_report_rewrites_interrupted_status(config, monkeypatch):
    module = service()
    original = module.atomic_write_json
    calls = []
    def interrupt(path, payload):
        calls.append(payload['status'])
        original(path, payload)
        if len(calls) == 1:
            raise KeyboardInterrupt
    monkeypatch.setattr(module, 'atomic_write_json', interrupt)
    result = module.classify(config, 1, client_factory=FakeClient([prediction()]))
    assert result.exit_code == 130 and result.stats.newly_saved == 1
    assert report(config, result)['status'] == 'interrupted'


@pytest.mark.parametrize('primary', [None, KeyboardInterrupt(), AppError('MODEL_REQUEST_FAILED', 'fatal')])
def test_report_failure_never_hides_primary_error_or_committed_label(config, monkeypatch, primary):
    def fail(*args):
        raise AppError('OUTPUT_LOCKED', 'report locked')
    monkeypatch.setattr(service(), 'atomic_write_json', fail)
    client = FakeClient([prediction(), primary] if primary is not None else [prediction()])
    if isinstance(primary, KeyboardInterrupt):
        result = service().classify(config, client_factory=client)
        assert result.exit_code == 130 and result.stats.newly_saved == 1
    else:
        with pytest.raises(AppError) as error:
            service().classify(config, None if primary else 1, client_factory=client)
        assert error.value.code == ('MODEL_REQUEST_FAILED' if primary else 'REPORT_WRITE')
        if primary is None:
            assert 'saved' in str(error.value).lower()
    assert len(rows(config)) == 1


@pytest.mark.parametrize('max_attempts', [1, 2])
def test_lower_attempt_setting_is_respected(config, max_attempts):
    config.classification.max_attempts = max_attempts
    client = FakeClient([invalid()] * 3)
    result = service().classify(config, 1, client_factory=client, sleep=lambda _: None)
    assert result.stats.failed == 1 and len(client.calls) == max_attempts
    assert rows(config, 'classification_failures.csv')[0]['attempts'] == str(max_attempts)


def test_path_rechecked_under_lock_before_model_or_report(config, monkeypatch):
    from contextlib import contextmanager
    module = service()
    before = set(config.paths.reports_dir.iterdir())
    client = FakeClient()

    @contextmanager
    def changed_after_lock(path):
        with resource_lock(path):
            config.paths.labeled_dir = config.paths.processed_dir
            yield

    monkeypatch.setattr(module, 'resource_lock', changed_after_lock)
    with pytest.raises(AppError) as error:
        module.classify(config, client_factory=client)
    assert error.value.code == 'CONFIG_ERROR' and client.preflights == 0
    assert set(config.paths.reports_dir.iterdir()) == before


def test_interrupt_after_durable_append_reconciles_even_direct_exception(config, monkeypatch):
    module = service()
    original = module.LabelStore.append

    def interrupt(store, record):
        original(store, record)
        raise KeyboardInterrupt

    monkeypatch.setattr(module.LabelStore, 'append', interrupt)
    result = module.classify(config, client_factory=FakeClient([prediction()]))
    assert result.stats.newly_saved == 1 and result.stats.remaining == 2
    assert len(rows(config)) == 1 and report(config, result)['stats']['newly_saved'] == 1


def test_reconcile_failure_keeps_saved_label_and_report_count(config, monkeypatch):
    module = service()
    original = module.LabelStore.reconcile_failures

    def fail_after_label(store):
        if store.completed:
            raise AppError('OUTPUT_LOCKED', 'failure file held')
        original(store)

    monkeypatch.setattr(module.LabelStore, 'reconcile_failures', fail_after_label)
    run_id = '00000000-0000-0000-0000-000000000007'
    with pytest.raises(AppError) as error:
        module.classify(config, client_factory=FakeClient([prediction()]), run_id=run_id)
    assert error.value.code == 'OUTPUT_LOCKED' and len(rows(config)) == 1
    data = json.loads((config.paths.reports_dir / f'{run_id}.json').read_text())
    assert data['stats']['newly_saved'] == 1 and data['stats']['remaining'] == 2


def test_report_and_client_shutdown_remain_under_dataset_lock(config, monkeypatch):
    module = service()
    original = module.atomic_write_json

    def assert_owned():
        with pytest.raises(AppError) as error:
            with resource_lock(config.paths.data_dir / '.dataset.lock'):
                pass
        assert error.value.code == 'RESOURCE_LOCKED'

    class Client(FakeClient):
        def __exit__(self, *args):
            assert_owned()
            super().__exit__(*args)

    def write(path, payload):
        assert_owned()
        original(path, payload)

    monkeypatch.setattr(module, 'atomic_write_json', write)
    result = module.classify(config, 1, client_factory=Client([prediction()]))
    assert result.stats.newly_saved == 1
    with resource_lock(config.paths.data_dir / '.dataset.lock'):
        pass
