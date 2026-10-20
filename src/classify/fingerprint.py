"""Semantic experiment identity, independent of run IDs and operational retries."""

import hashlib
import json

from src.classify.models import ClassificationMetadata, InputSnapshot, ModelIdentity, PromptBundle, METADATA_SCHEMA_VERSION
from src.classify.policy import REVIEW_POLICY_VERSION
from src.classify.prompt import SYSTEM_MAX_UTF8_BYTES
from src.config import AppConfig
from src.models import utc_now


def fingerprint_payload_sha256(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def build_metadata(config: AppConfig, snapshot: InputSnapshot, identity: ModelIdentity,
                   bundle: PromptBundle) -> ClassificationMetadata:
    manifest = snapshot.manifest
    payload = {
        'metadata_schema_version': METADATA_SCHEMA_VERSION,
        'input': {
            'confessions_sha256': manifest.outputs['confessions.csv'].sha256,
            'sources_sha256': manifest.outputs['sources.csv'].sha256,
            'confession_fields': list(manifest.confession_fields),
            'normalization_version': manifest.normalization_version,
            'filter_version': manifest.filter_version,
            'splitter_version': manifest.splitter_version,
        },
        'model': identity.model_dump(mode='json'),
        'prompt_sha256': bundle.prompt_sha256,
        'schema_sha256': bundle.schema_sha256,
        'school': {'name': config.school.name, 'aliases': list(config.school.aliases)},
        'inference': {name: getattr(config.ollama, name) for name in
                      ('temperature', 'seed', 'num_ctx', 'num_predict', 'think', 'keep_alive')},
        'review_policy_version': REVIEW_POLICY_VERSION,
        'review_threshold': config.classification.review_threshold,
        'budgets': {'max_content_utf8_bytes': config.classification.max_content_utf8_bytes,
                    'system_max_utf8_bytes': SYSTEM_MAX_UTF8_BYTES,
                    'context_margin': config.classification.context_margin},
    }
    return ClassificationMetadata(schema_version=METADATA_SCHEMA_VERSION,
        classification_fingerprint=fingerprint_payload_sha256(payload), fingerprint_payload=payload,
        input_generation_id=manifest.generation_id, created_at=utc_now())
