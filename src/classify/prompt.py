"""Isolated classification messages and conservative UTF-8 context budgets."""

import hashlib
import json
from pathlib import Path

from src.classify.models import ModelPrediction, PromptBundle
from src.config import AppConfig
from src.errors import AppError

SYSTEM_MAX_UTF8_BYTES = 4000
REPAIR_INSTRUCTION = '\nReturn only one JSON object matching the schema and target rules; no extra text.'


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _check_system(system: str) -> None:
    if len(system.encode('utf-8')) > SYSTEM_MAX_UTF8_BYTES:
        raise AppError('CONFIG_ERROR', 'Classification system prompt exceeds 4000 UTF-8 bytes.', 2)


def load_prompt(config: AppConfig) -> PromptBundle:
    """Read strict UTF-8, preserving raw bytes for the experiment fingerprint."""
    path = config.classification.prompt_path
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    try:
        raw = path.read_bytes()
        rubric = raw.decode('utf-8')
    except (OSError, UnicodeError) as exc:
        raise AppError('CONFIG_ERROR', 'Cannot read classification prompt as UTF-8.', 2) from exc
    if not rubric.strip():
        raise AppError('CONFIG_ERROR', 'Classification prompt must not be empty.', 2)
    schema = ModelPrediction.model_json_schema()
    schema_json = _canonical_json(schema)
    system = rubric + '\nJSON schema:\n' + schema_json
    _check_system(system)
    return PromptBundle(system=system, schema=schema,
                        prompt_sha256=hashlib.sha256(raw).hexdigest(),
                        schema_sha256=hashlib.sha256(schema_json.encode('utf-8')).hexdigest())


def build_messages(bundle: PromptBundle, content: str, config: AppConfig,
                   *, repair: bool = False) -> list[dict[str, str]]:
    """Build a fresh two-message request; a repair never includes failed output."""
    system = bundle.system + (REPAIR_INSTRUCTION if repair else '')
    _check_system(system)
    user = json.dumps({'school_name': config.school.name,
                       'school_aliases': config.school.aliases, 'content': content},
                      ensure_ascii=False, separators=(',', ':'))
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
    check_budget(content, messages, config)
    return messages


def check_budget(content: str, messages: list[dict[str, str]], config: AppConfig) -> None:
    """Use byte counts as a conservative heuristic, not an exact tokenizer."""
    body_bytes = len(content.encode('utf-8'))
    prompt_bytes = len(json.dumps(messages, ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8'))
    if (body_bytes > config.classification.max_content_utf8_bytes
            or prompt_bytes + config.classification.context_margin
            + config.ollama.num_predict > config.ollama.num_ctx):
        raise AppError('INPUT_TOO_LONG', 'Classification input exceeds the configured UTF-8 budget.')
