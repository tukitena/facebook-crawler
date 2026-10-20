"""Offline HTTP boundary tests; every model response is synthetic."""

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from src.classify.client import ModelAttemptError, OllamaClient
from src.classify.prompt import load_prompt
from src.config import AppConfig
from src.errors import AppError


DIGEST = 'a1' * 32
PREDICTION = dict(label='Neutral', confidence=0.9, reason='Chuyện cá nhân.',
                  target_relation='unrelated', mixed=False, needs_review=False)


def chat_response(content=None, **changes):
    return {'done': True, 'done_reason': 'stop',
            'message': {'role': 'assistant', 'content': content or json.dumps(PREDICTION)},
            **changes}


def client_for(handler, config=None):
    config = config or AppConfig()
    return OllamaClient(config, load_prompt(config), transport=httpx.MockTransport(handler))


def test_chat_sends_schema_options_unicode_and_timeouts():
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.method == 'POST' and request.url.path == '/api/chat'
        assert body['model'] == 'qwen3:8b'
        assert body['stream'] is False and body['think'] is False
        assert body['keep_alive'] == '5m'
        assert body['format']['additionalProperties'] is False
        assert set(body['format']['required']) == set(PREDICTION)
        assert body['options'] == dict(temperature=0, seed=42, num_ctx=8192, num_predict=512)
        assert [message['role'] for message in body['messages']] == ['system', 'user']
        assert json.loads(body['messages'][1]['content'])['content'] == 'Mình thất tình.\nBuồn quá!'
        assert request.extensions['timeout'] == dict(connect=5, read=120, write=10, pool=5)
        return httpx.Response(200, json=chat_response())

    with client_for(handler) as client:
        result = client.classify_once('Mình thất tình.\nBuồn quá!')
    assert result.label == 'Neutral' and result.reason == 'Chuyện cá nhân.'
    assert len(requests) == 1


@pytest.mark.parametrize(('target_relation', 'label'), [
    ('unrelated', 'Positive'), ('unrelated', 'Negative'),
    ('uncertain', 'Positive'), ('uncertain', 'Negative'),
])
def test_nonrelated_sentiment_is_normalized_to_neutral(target_relation, label):
    content = json.dumps(PREDICTION | {'target_relation': target_relation, 'label': label})
    response = chat_response(content=content)
    with client_for(lambda _: httpx.Response(200, json=response)) as client:
        result = client.classify_once('synthetic')
    assert result.label == 'Neutral'
    assert result.target_relation == target_relation


@pytest.mark.parametrize('status,service', [(429, True), (500, True), (503, True)])
def test_retryable_http_error_is_one_request_and_safe(status, service):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text='PRIVATE RESPONSE', headers={'Retry-After': '7'})

    with client_for(handler) as client, pytest.raises(ModelAttemptError) as caught:
        client.classify_once('PRIVATE INPUT')
    assert caught.value.retryable is True and caught.value.service_failure is service
    assert caught.value.retry_after == (7 if status == 429 else None)
    assert len(requests) == 1
    assert 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('status', [400, 401, 404, 422, 301, 302, 307, 308])
def test_fatal_http_and_redirects_do_not_retry_or_expose_body(status):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(status, text='PRIVATE unsupported option',
                              headers={'Location': 'https://example.com/api/chat'})

    with client_for(handler) as client, pytest.raises(AppError) as caught:
        client.classify_once('test')
    assert caught.value.exit_code == 1
    assert 'PRIVATE' not in str(caught.value)
    assert seen == ['http://127.0.0.1:11434/api/chat']


@pytest.mark.parametrize('error', [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError,
                                  httpx.RemoteProtocolError])
def test_transport_errors_are_retryable_service_failures(error):
    calls = []

    def handler(request):
        calls.append(request)
        raise error('PRIVATE', request=request)

    with client_for(handler) as client, pytest.raises(ModelAttemptError) as caught:
        client.classify_once('test')
    assert caught.value.retryable and caught.value.service_failure
    assert len(calls) == 1 and 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('response', [
    [], None, {}, {'done': True}, chat_response(done=False), chat_response(done=1),
    chat_response(done_reason='length'), chat_response(done_reason='unknown'),
    chat_response(message=None), chat_response(message={'content': {}}),
    chat_response(content='```json\n' + json.dumps(PREDICTION) + '\n```'),
    chat_response(content='Result: ' + json.dumps(PREDICTION)),
    chat_response(content='{"label":"Neutral","label":"Positive"}'),
    chat_response(content=json.dumps(PREDICTION | {'confidence': float('nan')})),
    chat_response(content=json.dumps(PREDICTION | {'confidence': '0.9'})),
    chat_response(content=json.dumps(PREDICTION | {'extra': True})),
    chat_response(content='[]'), chat_response(content='{}'),
])
def test_invalid_output_is_retryable_validation_error(response):
    with client_for(lambda _: httpx.Response(200, content=json.dumps(response))) as client:
        with pytest.raises(ModelAttemptError) as caught:
            client.classify_once('test')
    assert caught.value.retryable and not caught.value.service_failure


@pytest.mark.parametrize('raw', ['not JSON', '{"done":false,"done":true}', '{"count":NaN}',
                                  '{"count":Infinity}'])
def test_outer_response_is_strict_json(raw):
    with client_for(lambda _: httpx.Response(200, text=raw)) as client:
        with pytest.raises(ModelAttemptError):
            client.classify_once('test')


def test_error_envelope_is_fatal_without_raw_error():
    with client_for(lambda _: httpx.Response(200, json={'error': 'PRIVATE unsupported think'})) as client:
        with pytest.raises(AppError) as caught:
            client.classify_once('test')
    assert caught.value.exit_code == 1 and 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('surrogate', ['\ud800', '\udfff'])
def test_escaped_unpaired_surrogate_is_retryable_validation_error(surrogate):
    content = json.dumps(PREDICTION | {'reason': 'PRIVATE' + surrogate})
    with client_for(lambda _: httpx.Response(200, json=chat_response(content=content))) as client:
        with pytest.raises(ModelAttemptError) as caught:
            client.classify_once('synthetic')
    assert caught.value.code == 'MODEL_INVALID_OUTPUT'
    assert caught.value.retryable and not caught.value.service_failure
    assert 'PRIVATE' not in str(caught.value)


def test_valid_escaped_non_bmp_unicode_remains_utf8_encodable():
    content = json.dumps(PREDICTION | {'reason': 'Chuyện cá nhân. \U0001f642'})
    with client_for(lambda _: httpx.Response(200, json=chat_response(content=content))) as client:
        result = client.classify_once('synthetic')
    assert result.reason.encode('utf-8') == 'Chuyện cá nhân. \U0001f642'.encode('utf-8')


def test_malformed_http_compression_is_safe_retryable_validation_error():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={'Content-Encoding': 'gzip'}, content=b'PRIVATE broken gzip')

    with client_for(handler) as client, pytest.raises(ModelAttemptError) as caught:
        client.classify_once('synthetic')
    assert len(requests) == 1
    assert caught.value.code == 'MODEL_INVALID_OUTPUT'
    assert caught.value.retryable and not caught.value.service_failure
    assert caught.value.__suppress_context__ and 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('header,want', [('9999', 60), ('0', 0), ('-5', None), ('NaN', None),
                                       ('1.5', None), ('nonsense', None)])
def test_retry_after_seconds_are_bounded(header, want):
    with client_for(lambda _: httpx.Response(429, headers={'Retry-After': header})) as client:
        with pytest.raises(ModelAttemptError) as caught:
            client.classify_once('test')
    assert caught.value.retry_after == want


def test_retry_after_http_date():
    header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    with client_for(lambda _: httpx.Response(429, headers={'Retry-After': header})) as client:
        with pytest.raises(ModelAttemptError) as caught:
            client.classify_once('test')
    assert 28 <= caught.value.retry_after <= 30


def test_budget_failure_sends_no_request():
    def handler(_):
        pytest.fail('Oversized input must never reach transport')

    with client_for(handler) as client, pytest.raises(AppError) as caught:
        client.classify_once('a' * 6001)
    assert caught.value.code == 'INPUT_TOO_LONG'


def test_endpoint_is_validated_before_transport():
    config = AppConfig()
    config.ollama.base_url = 'https://example.com'
    with pytest.raises(AppError) as caught:
        client_for(lambda _: pytest.fail('remote request'), config)
    assert caught.value.code == 'CONFIG_ERROR'


def test_client_disables_proxy_environment_and_closes(monkeypatch):
    original = httpx.Client
    created = []

    def capture(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        result = original(**kwargs)
        created.append(result)
        return result

    monkeypatch.setattr(httpx, 'Client', capture)
    with client_for(lambda _: httpx.Response(200, json=chat_response())) as client:
        client.classify_once('test')
    assert created[0].is_closed


def preflight_handler(*, version=None, tags=None, chat=None, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        if request.url.path == '/api/version':
            return httpx.Response(200, json=version if version is not None else {'version': '0.40.6'})
        if request.url.path == '/api/tags':
            return httpx.Response(200, json=tags if tags is not None else {
                'models': [{'name': 'qwen3:8b', 'model': 'qwen3:8b', 'digest': DIGEST}]})
        assert request.url.path == '/api/chat'
        return chat(request) if chat else httpx.Response(200, json=chat_response())
    return handler


def test_preflight_returns_full_identity_only_and_never_writes_dataset(tmp_path):
    config = AppConfig()
    config.paths.labeled_dir = tmp_path / 'labels'
    requests = []
    with client_for(preflight_handler(requests=requests), config) as client:
        identity = client.preflight()
    assert identity.model_dump() == dict(tag='qwen3:8b', digest=DIGEST, server_version='0.40.6')
    assert [r.method for r in requests] == ['GET', 'GET', 'POST']
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('tags', [{'models': []}, {'models': [{'name': 'qwen3:latest', 'digest': DIGEST}]}])
def test_missing_exact_model_has_pull_guidance_and_exit_two(tags):
    with client_for(preflight_handler(tags=tags)) as client, pytest.raises(AppError) as caught:
        client.preflight()
    assert caught.value.exit_code == 2
    assert 'ollama pull qwen3:8b' in str(caught.value)


@pytest.mark.parametrize('version,tags', [
    ({}, None), ({'version': 4}, None), ([], None), ({'version': ' '}, None),
    (None, {}), (None, []), (None, {'models': {}}), (None, {'models': [None]}),
    (None, {'models': [{'name': 'qwen3:8b', 'digest': '123456789abc'}]}),
    (None, {'models': [{'name': 'qwen3:8b', 'digest': 'g' * 64}]}),
    (None, {'models': [{'name': 'qwen3:8b'}]}),
    (None, {'models': [{'name': 'qwen3:8b', 'digest': DIGEST}] * 2}),
])
def test_malformed_preflight_identity_is_fatal(version, tags):
    with client_for(preflight_handler(version=version, tags=tags)) as client:
        with pytest.raises(AppError) as caught:
            client.preflight()
    assert caught.value.exit_code == 1


def test_fixture_retries_validation_with_repair_and_no_history(monkeypatch):
    sleeps, requests = [], []
    monkeypatch.setattr('src.classify.client.time.sleep', sleeps.append)

    def chat(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=chat_response(content='{}') if len(requests) < 3 else chat_response())

    with client_for(preflight_handler(chat=chat)) as client:
        assert client.preflight().digest == DIGEST
    assert sleeps == [1, 2] and len(requests) == 3
    assert len(requests[-1]['messages']) == 2
    assert requests[0]['messages'][0] != requests[1]['messages'][0]
    assert requests[0]['messages'][1] == requests[1]['messages'][1]


def test_preflight_stops_at_three_requests_on_service_failure(monkeypatch, tmp_path):
    sleeps, attempts = [], []
    monkeypatch.setattr('src.classify.client.time.sleep', sleeps.append)
    config = AppConfig()
    config.paths.labeled_dir = tmp_path / 'labels'

    def chat(request):
        attempts.append(request)
        return httpx.Response(503)

    with client_for(preflight_handler(chat=chat), config) as client, pytest.raises(AppError) as caught:
        client.preflight()
    assert caught.value.code == 'MODEL_UNAVAILABLE' and caught.value.exit_code == 1
    assert len(attempts) == 3 and sleeps == [1, 2]
    assert not list(tmp_path.iterdir())


def test_preflight_respects_single_attempt_config(monkeypatch):
    monkeypatch.setattr('src.classify.client.time.sleep', lambda _: pytest.fail('No sleep'))
    config = AppConfig()
    config.classification.max_attempts = 1
    with client_for(preflight_handler(chat=lambda _: httpx.Response(200, json=chat_response(content='{}'))), config) as client:
        with pytest.raises(AppError) as caught:
            client.preflight()
    assert caught.value.code == 'MODEL_PREFLIGHT_FAILED'


def test_keyboard_interrupt_propagates():
    def handler(_):
        raise KeyboardInterrupt

    with client_for(handler) as client, pytest.raises(KeyboardInterrupt):
        client.preflight()
