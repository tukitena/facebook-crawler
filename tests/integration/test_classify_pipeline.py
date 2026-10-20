import csv
from functools import partial
import json
from pathlib import Path

import httpx
import pytest

from src.classify.client import OllamaClient
from src.classify.service import classify
from src.errors import AppError
from src.preprocess.service import preprocess
from src.reset import reset_dataset
from src.storage import resource_lock


PREDICTION = dict(label='Positive', confidence=0.9, reason='UIT hỗ trợ học tập tốt.',
                  target_relation='related', mixed=False, needs_review=False)


def response(prediction=PREDICTION):
    return httpx.Response(200, json={'done': True, 'done_reason': 'stop',
        'message': {'content': json.dumps(prediction, ensure_ascii=False)}})


def test_preprocess_http_interrupt_resume_and_reset_preserve_sources(raw_dataset):
    config = raw_dataset(['#001\nUIT có phòng tự học rất tiện lợi.\nCảm ơn nhà trường 😊',
                          '#002\nThư viện UIT có sách hay.', '#003\nUIT tốt'])
    config.classification.prompt_path = Path('src/classify/prompts/uit-impact-v1.txt').resolve()
    preprocess(config)
    inputs = [config.paths.raw_csv, config.paths.raw_csv.with_suffix('.meta.json'),
              *config.paths.processed_dir.iterdir()]
    before = {path: path.read_bytes() for path in inputs}
    outcomes = iter([response(), response({'label': 'Positive'}), response(), KeyboardInterrupt()])
    first_bodies = []

    def handler(request):
        # Preflight, retry and real requests must all stay within ownership.
        with pytest.raises(AppError) as locked:
            with resource_lock(config.paths.data_dir / '.dataset.lock'):
                pass
        assert locked.value.code == 'RESOURCE_LOCKED'
        if request.url.path == '/api/version':
            return httpx.Response(200, json={'version': 'test'})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'qwen3:8b', 'digest': 'a' * 64}]})
        assert request.url.path == '/api/chat'
        first_bodies.append(json.loads(request.content))
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    client_factory = partial(OllamaClient, transport=httpx.MockTransport(handler))
    interrupted = classify(config, client_factory=client_factory, sleep=lambda _: None)
    assert interrupted.exit_code == 130
    assert (interrupted.stats.attempted, interrupted.stats.newly_saved, interrupted.stats.remaining) == (2, 1, 2)
    assert len(first_bodies) == 4  # preflight, invalid, repair, interrupted
    original_input = json.loads(first_bodies[1]['messages'][1]['content'])['content']
    assert original_input == 'UIT có phòng tự học rất tiện lợi.\nCảm ơn nhà trường 😊'
    assert first_bodies[1]['messages'][0]['content'] != first_bodies[2]['messages'][0]['content']
    assert first_bodies[1]['messages'][1] == first_bodies[2]['messages'][1]

    # All resumed attempts succeed, including the required synthetic probe.
    outcomes = iter([response(), response(), response()])
    resumed = classify(config, client_factory=client_factory)
    assert resumed.status == 'complete' and resumed.exit_code == 0
    assert (resumed.stats.already_done, resumed.stats.newly_saved, resumed.stats.reviewed_total) == (1, 2, 1)
    with (config.paths.labeled_dir / 'labeled.csv').open(encoding='utf-8-sig', newline='') as stream:
        labels = list(csv.DictReader(stream))
    assert len({row['id'] for row in labels}) == len(labels) == 3
    assert labels[0]['content'] == original_input
    assert labels[2]['review_reasons'] == 'SHORT_CONTENT'
    assert all(path.read_bytes() == data for path, data in before.items())

    meta = (config.paths.labeled_dir / 'labeled.meta.json').read_bytes()
    outcomes = iter([response()])
    done = classify(config, 1, client_factory=client_factory)
    assert done.stats.attempted == 0 and done.stats.already_done == 3
    assert (config.paths.labeled_dir / 'labeled.meta.json').read_bytes() == meta

    config_path = config.paths.data_dir.parents[1] / 'config.yaml'
    prompt_before = config.classification.prompt_path.read_bytes()
    reports_before = {path: path.read_bytes() for path in config.paths.reports_dir.iterdir()}
    reset_dataset(config, config_path=config_path)
    assert all(not path.exists() for path in inputs)
    assert not (config.paths.labeled_dir / 'labeled.csv').exists()
    assert not (config.paths.labeled_dir / 'labeled.meta.json').exists()
    assert config_path.exists() and config.classification.prompt_path.read_bytes() == prompt_before
    assert all(path.read_bytes() == data for path, data in reports_before.items())


def test_actual_http_timeout_schema_and_500_share_one_three_attempt_budget(raw_dataset):
    config = raw_dataset(['#001\nUIT hỗ trợ sinh viên.'])
    config.classification.prompt_path = Path('src/classify/prompts/uit-impact-v1.txt').resolve()
    preprocess(config)
    chats = []

    def handler(request):
        if request.url.path == '/api/version':
            return httpx.Response(200, json={'version': 'test'})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'qwen3:8b', 'digest': 'a' * 64}]})
        chats.append(json.loads(request.content))
        if len(chats) == 1:
            return response()
        if len(chats) == 2:
            raise httpx.ReadTimeout('transport details never logged', request=request)
        if len(chats) == 3:
            return response({'label': 'Positive'})
        if len(chats) == 4:
            return httpx.Response(500, text='private server details')
        pytest.fail('A fourth dataset request exceeds the total budget')

    delays = []
    result = classify(config, client_factory=partial(OllamaClient, transport=httpx.MockTransport(handler)),
                      sleep=delays.append)
    assert result.status == 'partial' and result.stats.failed == 1 and result.stats.attempted == 1
    assert len(chats) == 4 and delays == [1, 2]
    with (config.paths.labeled_dir / 'classification_failures.csv').open(encoding='utf-8-sig', newline='') as stream:
        failures = list(csv.DictReader(stream))
    assert failures[0]['attempts'] == '3' and failures[0]['error_code'] == 'MODEL_SERVICE_ERROR'
