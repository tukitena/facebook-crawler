from dataclasses import replace
from importlib import import_module
from pathlib import Path

import pytest

from src.classify.models import ModelIdentity
from src.classify.prompt import load_prompt
from src.preprocess.service import preprocess


@pytest.fixture
def config(raw_dataset):
    config = raw_dataset(['#001\nXin chào, "UIT"\nDòng hai 😊', '#002\nUIT tốt'])
    config.classification.prompt_path = Path('src/classify/prompts/uit-impact-v1.txt').resolve()
    preprocess(config)
    return config


@pytest.fixture
def snapshot(config):
    return import_module('src.classify.storage').load_input(config.paths.processed_dir)


@pytest.fixture
def identity():
    return ModelIdentity(tag='qwen3:8b', digest='a' * 64, server_version='test')


@pytest.fixture
def bundle(config):
    return load_prompt(config)


@pytest.fixture
def metadata(config, snapshot, identity, bundle):
    return import_module('src.classify.fingerprint').build_metadata(config, snapshot, identity, bundle)


@pytest.mark.parametrize('section,key,value', [
    ('school', 'name', 'New university'), ('school', 'aliases', ['New alias']),
    ('ollama', 'temperature', 0.1), ('ollama', 'seed', 17), ('ollama', 'num_ctx', 9000),
    ('ollama', 'num_predict', 600), ('ollama', 'think', True), ('ollama', 'keep_alive', '10m'),
    ('classification', 'review_threshold', 0.5), ('classification', 'max_content_utf8_bytes', 5000),
    ('classification', 'context_margin', 512),
])
def test_semantic_settings_change_fingerprint(config, snapshot, identity, bundle, metadata, section, key, value):
    changed = config.model_copy(deep=True)
    setattr(getattr(changed, section), key, value)
    actual = import_module('src.classify.fingerprint').build_metadata(changed, snapshot, identity, bundle)
    assert actual.classification_fingerprint != metadata.classification_fingerprint


@pytest.mark.parametrize('change', ['tag', 'digest', 'server_version', 'prompt_sha256', 'schema_sha256',
                                   'confessions.csv', 'sources.csv', 'normalization_version',
                                   'filter_version', 'splitter_version', 'policy', 'system_budget'])
def test_semantic_inputs_change_fingerprint(config, snapshot, identity, bundle, metadata, change, monkeypatch):
    module = import_module('src.classify.fingerprint')
    if change in {'tag', 'digest', 'server_version'}:
        identity = identity.model_copy(update={change: 'changed'})
    elif change.endswith('_sha256'):
        bundle = replace(bundle, **{change: '0' * 64})
    elif change.endswith('.csv'):
        outputs = dict(snapshot.manifest.outputs)
        outputs[change] = outputs[change].model_copy(update={'sha256': '0' * 64})
        snapshot = replace(snapshot, manifest=snapshot.manifest.model_copy(update={'outputs': outputs}))
    elif change == 'policy':
        monkeypatch.setattr(module, 'REVIEW_POLICY_VERSION', 'new_policy')
    elif change == 'system_budget':
        monkeypatch.setattr(module, 'SYSTEM_MAX_UTF8_BYTES', 3999)
    else:
        snapshot = replace(snapshot, manifest=snapshot.manifest.model_copy(update={change: 'new_version'}))
    assert module.build_metadata(config, snapshot, identity, bundle).classification_fingerprint != metadata.classification_fingerprint


def test_operational_changes_and_generation_uuid_do_not_change_fingerprint(config, snapshot, identity, bundle, metadata):
    config.ollama.read_timeout_seconds = 240
    config.ollama.connect_timeout_seconds = 30
    config.ollama.write_timeout_seconds = 30
    config.ollama.pool_timeout_seconds = 30
    config.classification.max_attempts = 1
    config.classification.consecutive_service_failures = 1
    config.paths.log_dir = Path('other-logs')
    manifest = snapshot.manifest.model_copy(update={'generation_id': 'another-id', 'created_at': '2026-10-21T00:00:00Z'})
    actual = import_module('src.classify.fingerprint').build_metadata(config, replace(snapshot, manifest=manifest), identity, bundle)
    assert actual.classification_fingerprint == metadata.classification_fingerprint
    assert actual.input_generation_id == 'another-id'
