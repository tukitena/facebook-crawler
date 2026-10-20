from contextlib import contextmanager
from dataclasses import asdict
import csv
import hashlib
from importlib import import_module
import json
from pathlib import Path
import signal
import uuid

import pytest

from src.errors import AppError
from src.identity import make_confession_id
from src.preprocess.models import Confession, PreprocessStats, SourceRow

CSV_NAMES = ('confessions.csv', 'sources.csv', 'excluded.csv')
LABELED_FIELDS = ['id', 'content', 'label', 'confidence', 'reason', 'needs_review', 'review_reasons']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.reader(stream, strict=True))


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        csv.writer(stream).writerows(rows)


@pytest.fixture
def generation_case():
    @contextmanager
    def create(config, body='A', marker='001'):
        module = import_module('src.preprocess.storage')
        models = import_module('src.preprocess.models')
        with module.ProcessedWriter(config.paths.processed_dir) as writer:
            stats = PreprocessStats()
            if body is not None:
                identifier = make_confession_id(body)
                writer.write_confession(Confession(id=identifier, content=body, confession_number=marker))
                writer.write_source(SourceRow(confession_id=identifier, post_id='post_' + 'a' * 64,
                                               marker=marker, start_line=1, end_line=2,
                                               flags=('SHORT_CONTENT', 'MANUAL_ACCEPT')))
                stats = PreprocessStats(read_posts=1, accepted_posts=1, candidate_segments=1,
                                        unique_confessions=1, sources=1)
            outputs = writer.finish()
            effective = config.preprocess.model_dump(mode='json')
            manifest = models.PreprocessManifest(
                schema_version=1, generation_id=str(uuid.uuid4()), created_at='2026-10-20T00:00:00Z',
                page_url=config.facebook.page_url, raw_csv_sha256=sha(config.paths.raw_csv),
                raw_metadata_sha256=sha(config.paths.raw_csv.with_suffix('.meta.json')),
                effective_config=effective,
                effective_config_sha256=hashlib.sha256(json.dumps(effective, ensure_ascii=False, sort_keys=True,
                                                                 separators=(',', ':')).encode()).hexdigest(),
                overrides_sha256=None, normalization_version='v1', filter_version='preprocess_filter_v1',
                splitter_version='line_start_numeric_v2', confession_fields=('id', 'content', 'confession_number'),
                outputs=outputs, counts=asdict(stats), excluded_by_reason={})
            yield writer, manifest
    return create


def publish(config, generation_case, body='A'):
    with generation_case(config, body) as (writer, manifest):
        return writer.publish(manifest, labeled_dir=config.paths.labeled_dir)


def seed_labels(config, body='A', populated=True):
    rows = [LABELED_FIELDS]
    if populated:
        rows.append([make_confession_id(body), body, 'Neutral', '0.8', 'Không đánh giá trường.', 'false', ''])
    write_csv(config.paths.labeled_dir / 'labeled.csv', rows)


@pytest.mark.parametrize('body', [None, 'Xin chào, "UIT"\nDòng hai 😊'])
def test_publish_roundtrip_and_empty_generation(raw_dataset, generation_case, body):
    config = raw_dataset([])
    manifest = publish(config, generation_case, body)
    module = import_module('src.preprocess.storage')
    assert module.validate_generation(config.paths.processed_dir) == manifest
    for name in CSV_NAMES:
        data = (config.paths.processed_dir / name).read_bytes()
        assert data.count(b'\xef\xbb\xbf') == 1 and data.endswith(b'\r\n')
    rows = read_csv(config.paths.processed_dir / 'confessions.csv')
    assert rows[0] == ['id', 'content', 'confession_number']
    assert len(rows) == (1 if body is None else 2)
    if body is not None:
        assert rows[1][1:] == [body, '001']
        sources = read_csv(config.paths.processed_dir / 'sources.csv')
        assert sources[1][-1] == 'MANUAL_ACCEPT;SHORT_CONTENT'
        assert manifest.outputs['sources.csv'].rows == 1


def test_manifest_is_last_and_hashes_match(raw_dataset, generation_case, monkeypatch):
    config = raw_dataset([])
    module = import_module('src.preprocess.storage')
    destinations = []
    original = module.os.replace

    def record(source, destination):
        destinations.append(Path(destination).name)
        original(source, destination)

    monkeypatch.setattr(module.os, 'replace', record)
    with generation_case(config) as (writer, manifest):
        writer.publish(manifest, labeled_dir=config.paths.labeled_dir)
        assert writer.published_manifest == manifest
    assert destinations == [*CSV_NAMES, 'preprocess.manifest.json']
    actual = module.validate_generation(config.paths.processed_dir)
    assert actual.outputs == manifest.outputs
    assert actual.confession_fields == ('id', 'content', 'confession_number')


@pytest.mark.parametrize('damage', ['missing', 'json', 'schema', 'version', 'hash', 'header',
    'duplicate_confession', 'duplicate_source', 'foreign_key', 'columns', 'orphan', 'flags', 'counts', 'config_hash'])
def test_generation_rejects_corruption(raw_dataset, generation_case, damage):
    config = raw_dataset([])
    publish(config, generation_case)
    module = import_module('src.preprocess.storage')
    directory = config.paths.processed_dir
    path = directory / 'preprocess.manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if damage == 'missing':
        path.unlink()
    elif damage == 'json':
        path.write_text('{', encoding='utf-8')
    elif damage == 'schema':
        manifest['schema_version'] = 2
    elif damage == 'version':
        manifest['splitter_version'] = 'unsupported'
    elif damage == 'hash':
        (directory / 'sources.csv').write_bytes(b'changed')
    elif damage == 'counts':
        manifest['outputs']['sources.csv']['rows'] = 999
    elif damage == 'config_hash':
        manifest['effective_config_sha256'] = '0' * 64
    else:
        name = 'confessions.csv' if damage in {'header', 'duplicate_confession'} else 'sources.csv'
        rows = read_csv(directory / name)
        if damage == 'header':
            rows[0][0] = 'wrong'
        elif damage.startswith('duplicate'):
            rows.append(rows[1].copy())
        elif damage == 'foreign_key':
            rows[1][0] = 'cfs_' + '0' * 64
        elif damage == 'columns':
            rows[1].append('extra')
        elif damage == 'orphan':
            rows = rows[:1]
        elif damage == 'flags':
            rows[1][-1] = 'SHORT_CONTENT;MANUAL_ACCEPT'
        write_csv(directory / name, rows)
        manifest['outputs'][name] = {'sha256': sha(directory / name), 'rows': len(rows) - 1}
    if damage not in {'missing', 'json'}:
        path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(AppError) as error:
        module.validate_generation(directory)
    assert (error.value.code, error.value.exit_code) == ('INVALID_GENERATION', 2)


@pytest.mark.parametrize('change', ['confessions.csv', 'sources.csv', 'normalization_version',
                                   'filter_version', 'splitter_version'])
def test_derived_conflict_input_key(raw_dataset, generation_case, change):
    config = raw_dataset([])
    old = publish(config, generation_case)
    seed_labels(config)
    module = import_module('src.preprocess.storage')
    if change.endswith('.csv'):
        outputs = dict(old.outputs)
        outputs[change] = outputs[change].model_copy(update={'sha256': '0' * 64})
        proposed = old.model_copy(update={'outputs': outputs})
    else:
        proposed = old.model_copy(update={change: 'changed'})
    before = {p: p.read_bytes() for p in config.paths.processed_dir.iterdir() if p.is_file()}
    label_bytes = (config.paths.labeled_dir / 'labeled.csv').read_bytes()
    with pytest.raises(AppError) as error:
        module.assert_no_derived_conflict(config.paths.processed_dir, config.paths.labeled_dir, proposed)
    assert (error.value.code, error.value.exit_code) == ('DERIVED_DATA_CONFLICT', 2)
    assert all(p.read_bytes() == data for p, data in before.items())
    assert (config.paths.labeled_dir / 'labeled.csv').read_bytes() == label_bytes


def test_non_semantic_manifest_changes_do_not_conflict(raw_dataset, generation_case):
    config = raw_dataset([])
    old = publish(config, generation_case)
    seed_labels(config)
    module = import_module('src.preprocess.storage')
    outputs = dict(old.outputs)
    outputs['excluded.csv'] = outputs['excluded.csv'].model_copy(update={'sha256': '0' * 64})
    proposed = old.model_copy(update={'generation_id': str(uuid.uuid4()), 'created_at': '2026-10-21T00:00:00Z', 'outputs': outputs})
    module.assert_no_derived_conflict(config.paths.processed_dir, config.paths.labeled_dir, proposed)


@pytest.mark.parametrize('condition', ['missing', 'zero_byte', 'header_only'])
def test_empty_labels_allow_rebuild_without_old_manifest(raw_dataset, generation_case, condition):
    config = raw_dataset([])
    module = import_module('src.preprocess.storage')
    if condition != 'missing':
        seed_labels(config, populated=False)
        if condition == 'zero_byte':
            (config.paths.labeled_dir / 'labeled.csv').write_bytes(b'')
    with generation_case(config) as (writer, manifest):
        writer.publish(manifest, labeled_dir=config.paths.labeled_dir)
    assert module.validate_generation(config.paths.processed_dir) == manifest


@pytest.mark.parametrize('damage', ['missing_manifest', 'invalid_manifest', 'bad_header', 'bad_row',
                                   'wrong_content', 'duplicate', 'bad_label', 'nan', 'bad_bool', 'truncated'])
def test_labeled_damage_is_conflict(raw_dataset, generation_case, damage):
    config = raw_dataset([])
    old = publish(config, generation_case)
    seed_labels(config)
    module = import_module('src.preprocess.storage')
    labels = config.paths.labeled_dir / 'labeled.csv'
    rows = read_csv(labels)
    if damage == 'missing_manifest':
        (config.paths.processed_dir / 'preprocess.manifest.json').unlink()
    elif damage == 'invalid_manifest':
        (config.paths.processed_dir / 'preprocess.manifest.json').write_text('{}')
    elif damage == 'truncated':
        labels.write_bytes(labels.read_bytes()[:-2])
    else:
        if damage == 'bad_header':
            rows[0][0] = 'wrong'
        elif damage == 'bad_row':
            rows[1].pop()
        elif damage == 'wrong_content':
            rows[1][1] = 'changed'
        elif damage == 'duplicate':
            rows.append(rows[1].copy())
        elif damage == 'bad_label':
            rows[1][2] = 'Other'
        elif damage == 'nan':
            rows[1][3] = 'NaN'
        elif damage == 'bad_bool':
            rows[1][5] = 'maybe'
        write_csv(labels, rows)
    with pytest.raises(AppError) as error:
        module.assert_no_derived_conflict(config.paths.processed_dir, config.paths.labeled_dir, old)
    assert error.value.code == 'DERIVED_DATA_CONFLICT'


@pytest.mark.parametrize('kind,name', [('override', 'confessions.csv'), ('override', 'sources.csv'),
                                      ('config', 'preprocess.manifest.json'), ('log', 'crawl.jsonl')])
def test_output_collision_preserves_operator_input(raw_dataset, kind, name):
    config = raw_dataset([])
    module = import_module('src.preprocess.storage')
    path = (config.paths.log_dir if kind == 'log' else config.paths.processed_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'operator-owned')
    if kind == 'override':
        config.preprocess.overrides_csv = path
    with pytest.raises(AppError) as error:
        module.validate_preprocess_paths(config, path if kind != 'override' else None)
    assert (error.value.code, error.value.exit_code) == ('CONFIG_ERROR', 2)
    assert path.read_bytes() == b'operator-owned'


def test_redirected_output_path_is_rejected(raw_dataset, monkeypatch):
    config = raw_dataset([])
    module = import_module('src.preprocess.storage')
    target = config.paths.processed_dir / 'sources.csv'
    original = Path.resolve
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'resolve', lambda path, *a, **kw: original(config.paths.raw_csv) if path == target else original(path, *a, **kw))
        with pytest.raises(AppError) as error:
            module.validate_preprocess_paths(config)
    assert error.value.code == 'CONFIG_ERROR'


@pytest.mark.parametrize('failure_at', [1, 2, 3, 4])
def test_publish_failure_and_recovery(raw_dataset, generation_case, monkeypatch, failure_at):
    config = raw_dataset([])
    old = publish(config, generation_case, 'Old body')
    seed_labels(config, populated=False)
    labels = (config.paths.labeled_dir / 'labeled.csv').read_bytes()
    module = import_module('src.preprocess.storage')
    replace = module.os.replace
    calls = []

    def fail(source, destination):
        calls.append(Path(destination).name)
        if len(calls) == failure_at:
            raise PermissionError('held by Excel') if failure_at == 1 else OSError('disk error')
        replace(source, destination)

    with generation_case(config, 'New body') as (writer, manifest):
        with monkeypatch.context() as patch:
            patch.setattr(module.os, 'replace', fail)
            with pytest.raises(AppError) as error:
                writer.publish(manifest, labeled_dir=config.paths.labeled_dir)
        assert error.value.code == ('OUTPUT_LOCKED' if failure_at == 1 else 'STORAGE_ERROR')
    assert list(config.paths.processed_dir.glob('*.tmp'))
    assert (config.paths.labeled_dir / 'labeled.csv').read_bytes() == labels
    if failure_at == 1:
        assert module.validate_generation(config.paths.processed_dir) == old
    else:
        with pytest.raises(AppError):
            module.validate_generation(config.paths.processed_dir)
    recovered = publish(config, generation_case, 'New body')
    assert module.validate_generation(config.paths.processed_dir) == recovered


def test_fsync_failure_never_replaces_previous_generation(raw_dataset, generation_case, monkeypatch):
    config = raw_dataset([])
    old = publish(config, generation_case)
    module = import_module('src.preprocess.storage')
    with module.ProcessedWriter(config.paths.processed_dir) as writer:
        with monkeypatch.context() as patch:
            patch.setattr(module.os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError('full disk')))
            with pytest.raises(AppError) as error:
                writer.finish()
        assert error.value.code == 'STORAGE_ERROR'
    assert module.validate_generation(config.paths.processed_dir) == old


def test_publish_defers_sigint_until_manifest(raw_dataset, generation_case, monkeypatch):
    config = raw_dataset([])
    module = import_module('src.preprocess.storage')
    replace = module.os.replace

    def interrupted(source, destination):
        replace(source, destination)
        if Path(destination).name == 'confessions.csv':
            signal.raise_signal(signal.SIGINT)

    with generation_case(config) as (writer, manifest):
        with monkeypatch.context() as patch:
            patch.setattr(module.os, 'replace', interrupted)
            with pytest.raises(KeyboardInterrupt):
                writer.publish(manifest, labeled_dir=config.paths.labeled_dir)
        assert writer.published_manifest == manifest
    assert module.validate_generation(config.paths.processed_dir) == manifest


def test_writer_rejects_writes_after_finish_and_staged_hash_mismatch(raw_dataset, generation_case):
    config = raw_dataset([])
    with generation_case(config) as (writer, manifest):
        with pytest.raises(AppError):
            writer.write_confession(Confession(id=make_confession_id('B'), content='B', confession_number='2'))
        outputs = dict(manifest.outputs)
        outputs['confessions.csv'] = outputs['confessions.csv'].model_copy(update={'sha256': '0' * 64})
        with pytest.raises(AppError):
            writer.publish(manifest.model_copy(update={'outputs': outputs}), labeled_dir=config.paths.labeled_dir)
    assert not (config.paths.processed_dir / 'preprocess.manifest.json').exists()
