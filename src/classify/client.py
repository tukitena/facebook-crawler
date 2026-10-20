"""Local Ollama HTTP boundary; dataset retries belong to the service."""

import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from pydantic import ValidationError

from src.classify.models import ModelIdentity, ModelPrediction, PromptBundle
from src.classify.prompt import build_messages
from src.config import AppConfig, validate_ollama_base_url
from src.errors import AppError

PREFLIGHT_CONTENT = 'Đây là ví dụ tổng hợp: mình đang buồn vì chuyện tình cảm cá nhân.'


class ModelAttemptError(Exception):
    """Safe per-request failure, carrying only retry policy information."""

    def __init__(self, code: str, message: str, *, retryable: bool,
                 service_failure: bool, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.service_failure = service_failure
        self.retry_after = retry_after


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('Nonfinite JSON constant')


def _strict_json(raw):
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _validation_error() -> ModelAttemptError:
    return ModelAttemptError('MODEL_INVALID_OUTPUT', 'Ollama returned invalid or incomplete structured output.',
                             retryable=True, service_failure=False)


def _normalize_target_label(value):
    if (isinstance(value, dict)
            and value.get('target_relation') in {'unrelated', 'uncertain'}
            and value.get('label') in {'Positive', 'Negative'}):
        return value | {'label': 'Neutral'}
    return value


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r'[0-9]+', value):
        # Avoid integer conversion limits for an untrusted huge header.
        return min(float(value), 60.0)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return max(0.0, min(60.0, (date - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return None


class OllamaClient:
    def __init__(self, config: AppConfig, bundle: PromptBundle, *, transport=None):
        self.config = config
        self.bundle = bundle
        options = config.ollama
        base_url = validate_ollama_base_url(options.base_url)
        self._client = httpx.Client(
            base_url=base_url, trust_env=False, follow_redirects=False, transport=transport,
            timeout=httpx.Timeout(connect=options.connect_timeout_seconds,
                                  read=options.read_timeout_seconds,
                                  write=options.write_timeout_seconds,
                                  pool=options.pool_timeout_seconds))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs):
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.DecodingError:
            raise _validation_error() from None
        except httpx.TransportError:
            raise ModelAttemptError('MODEL_SERVICE_ERROR', 'Cannot communicate with local Ollama.',
                                    retryable=True, service_failure=True) from None
        if response.status_code == 429 or 500 <= response.status_code <= 599:
            raise ModelAttemptError(
                'MODEL_SERVICE_ERROR', 'Ollama is temporarily unavailable.',
                retryable=True, service_failure=True,
                retry_after=_retry_after(response.headers.get('Retry-After'))
                if response.status_code == 429 else None)
        if not 200 <= response.status_code <= 299:
            raise AppError('MODEL_REQUEST_FAILED', 'Ollama rejected the request or returned a redirect.')
        try:
            result = _strict_json(response.content)
        except (ValueError, UnicodeError, RecursionError):
            raise _validation_error() from None
        if not isinstance(result, dict):
            raise _validation_error()
        if 'error' in result:
            raise AppError('MODEL_REQUEST_FAILED', 'Ollama reported a request or option error.')
        return result

    def classify_once(self, content: str, *, repair: bool = False) -> ModelPrediction:
        """Send exactly one chat request, never rewriting content or retrying it."""
        messages = build_messages(self.bundle, content, self.config, repair=repair)
        options = self.config.ollama
        result = self._request('POST', '/api/chat', json={
            'model': options.model, 'messages': messages, 'format': self.bundle.schema,
            'stream': False, 'think': options.think, 'keep_alive': options.keep_alive,
            'options': {'temperature': options.temperature, 'seed': options.seed,
                        'num_ctx': options.num_ctx, 'num_predict': options.num_predict}})
        message = result.get('message')
        if (result.get('done') is not True or result.get('done_reason') != 'stop'
                or not isinstance(message, dict) or not isinstance(message.get('content'), str)):
            raise _validation_error()
        try:
            value = _normalize_target_label(_strict_json(message['content']))
            return ModelPrediction.model_validate(value)
        except (ValueError, UnicodeError, RecursionError, ValidationError):
            raise _validation_error() from None

    def preflight(self) -> ModelIdentity:
        """Check identity and probe the real prompt/options with synthetic content."""
        try:
            version = self._request('GET', '/api/version')
            tags = self._request('GET', '/api/tags')
        except ModelAttemptError as exc:
            code = 'MODEL_UNAVAILABLE' if exc.service_failure else 'MODEL_PREFLIGHT_FAILED'
            raise AppError(code, 'Cannot validate the local Ollama server identity.') from None
        server_version = version.get('version')
        models = tags.get('models')
        if (not isinstance(server_version, str) or not server_version.strip()
                or not isinstance(models, list)
                or any(not isinstance(model, dict) or not isinstance(model.get('name'), str)
                       for model in models)):
            raise AppError('MODEL_PREFLIGHT_FAILED', 'Ollama returned an invalid version or model listing.')
        matches = [model for model in models if model['name'] == self.config.ollama.model]
        if not matches:
            raise AppError('MODEL_NOT_FOUND',
                           f'Configured model is not installed. Run: ollama pull {self.config.ollama.model}', 2)
        digest = matches[0].get('digest')
        if (len(matches) != 1 or not isinstance(digest, str)
                or re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}', digest) is None):
            raise AppError('MODEL_PREFLIGHT_FAILED', 'Ollama did not provide one full model digest.')
        identity = ModelIdentity(tag=self.config.ollama.model, digest=digest, server_version=server_version)
        repair = False
        for attempt in range(1, self.config.classification.max_attempts + 1):
            try:
                self.classify_once(PREFLIGHT_CONTENT, repair=repair)
                return identity
            except ModelAttemptError as exc:
                if not exc.retryable or attempt == self.config.classification.max_attempts:
                    code = 'MODEL_UNAVAILABLE' if exc.service_failure else 'MODEL_PREFLIGHT_FAILED'
                    raise AppError(code, 'Ollama synthetic structured-output preflight failed.') from None
                repair = repair or not exc.service_failure
                time.sleep(exc.retry_after if exc.retry_after is not None else 2 ** (attempt - 1))
        raise AppError('MODEL_PREFLIGHT_FAILED', 'Ollama preflight requires at least one attempt.')
