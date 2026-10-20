import csv
from dataclasses import replace
from importlib import import_module
import io
import json
import signal

import pytest

from src.classify.models import FAILURE_FIELDS, LABELED_FIELDS, FailureRecord, LabeledRecord
from src.errors import AppError
from src.preprocess.service import preprocess
from test_classify_fingerprint import config, snapshot, identity, bundle, metadata


def storage():
    return import_module('src.classify.storage')


def csv_bytes(fields, rows=()):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\r\n')
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig')


@pytest.fixture
def record(snapshot):
    confession = snapshot.confessions[0]
    return LabeledRecord(id=confession.id, content=confession.content, label='Neutral', confidence=0.8,
                         reason='Nội dung tổng hợp.', needs_review=True, review_reasons=('MODEL_UNCERTAIN',))


@pytest.fixture
def failure(record):
    return FailureRecord(id=record.id, error_code='MODEL_TIMEOUT', attempts=3, last_attempt_at='2026-10-20T00:00:00Z')


@pytest.mark.parametrize('initial', ['absent', 'zero', 'header', 'metadata'])
def test_initial_states_and_multiline_roundtrip(tmp_path, metadata, snapshot, record, initial):
    if initial in {'zero', 'header'}:
        (tmp_path / 'labeled.csv').write_bytes(b'' if initial == 'zero' else csv_bytes(LABELED_FIELDS))
    if initial == 'metadata':
        (tmp_path / 'labeled.meta.json').write_text(metadata.model_dump_json(), encoding='utf-8')
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        assert store.completed == set()
        store.append(record)
        assert store.completed == {record.id} and store.reviewed_total == 1
    data = (tmp_path / 'labeled.csv').read_bytes()
    assert data.count(b'\xef\xbb\xbf') == 1 and data.endswith(b'\r\n')
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        assert store.completed == {record.id} and store.reviewed_total == 1
    with (tmp_path / 'labeled.csv').open(encoding='utf-8-sig', newline='') as stream:
        assert LabeledRecord.from_csv_row(next(csv.DictReader(stream))) == record


def test_input_order_and_flags(config, snapshot):
    assert [item.confession_number for item in snapshot.confessions] == ['001', '002']
    assert snapshot.flags_by_id[snapshot.confessions[1].id] == ('SHORT_CONTENT',)


@pytest.mark.parametrize('damage', ['header', 'duplicate', 'foreign_key', 'utf8'])
def test_input_validated_before_use(config, damage):
    path = config.paths.processed_dir / ('sources.csv' if damage == 'foreign_key' else 'confessions.csv')
    data = path.read_bytes()
    if damage == 'header':
        data = data.replace(b'id,content', b'xx,content')
    elif damage == 'utf8':
        data += b'\xff\r\n'
    elif damage == 'foreign_key':
        data = data.replace(b'cfs_', b'bad_', 1)
    else:
        data += data[3:].split(b'\r\n', 1)[1]
    path.write_bytes(data)
    with pytest.raises(AppError) as error:
        storage().load_input(config.paths.processed_dir)
    assert error.value.code == 'INVALID_GENERATION'


@pytest.mark.parametrize('damage', ['missing_metadata', 'mismatch_metadata', 'tampered_payload', 'duplicate_json',
                                   'header', 'truncated', 'multiline', 'utf8', 'duplicate', 'unknown', 'content'])
def test_corrupt_resume_preserves_bytes(tmp_path, metadata, snapshot, record, damage):
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        store.append(record)
    meta = tmp_path / 'labeled.meta.json'
    path = tmp_path / 'labeled.csv'
    if damage == 'missing_metadata':
        meta.unlink()
    elif damage in {'mismatch_metadata', 'tampered_payload'}:
        values = json.loads(meta.read_text(encoding='utf-8'))
        if damage == 'mismatch_metadata':
            values['classification_fingerprint'] = '0' * 64
        else:
            values['fingerprint_payload']['extra'] = True
        meta.write_text(json.dumps(values), encoding='utf-8')
    elif damage == 'duplicate_json':
        meta.write_text(meta.read_text(encoding='utf-8').replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'), encoding='utf-8')
    elif damage == 'header':
        path.write_bytes(path.read_bytes().replace(b'id,content', b'xx,content'))
    elif damage == 'truncated':
        path.write_bytes(path.read_bytes()[:-2])
    elif damage == 'multiline':
        path.write_bytes(csv_bytes(LABELED_FIELDS) + b'"unterminated\r\n')
    elif damage == 'utf8':
        path.write_bytes(path.read_bytes() + b'\xff\r\n')
    else:
        row = record.to_csv_row()
        if damage == 'unknown':
            from src.identity import make_confession_id
            row.update(content='Other', id=make_confession_id('Other'))
        elif damage == 'content':
            row['content'] += 'changed'
        path.write_bytes(csv_bytes(LABELED_FIELDS, [record.to_csv_row(), row] if damage == 'duplicate' else [row]))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(AppError):
        with storage().LabelStore(tmp_path, metadata, snapshot):
            pass
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}


def test_failure_update_and_success_reconciliation(tmp_path, metadata, snapshot, record, failure):
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        store.save_failure(failure)
        store.save_failure(failure.model_copy(update={'attempts': 2}))
        with (tmp_path / 'classification_failures.csv').open(encoding='utf-8-sig', newline='') as stream:
            assert list(csv.DictReader(stream))[0]['attempts'] == '2'
        store.append(record)
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        assert store.completed == {record.id}
    with (tmp_path / 'classification_failures.csv').open(encoding='utf-8-sig', newline='') as stream:
        assert list(csv.DictReader(stream)) == []


@pytest.mark.parametrize('damage', ['missing_metadata', 'duplicate', 'unknown', 'truncated', 'header', 'utf8'])
def test_invalid_failure_csv_rejected(tmp_path, metadata, snapshot, failure, damage):
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        store.save_failure(failure)
    path = tmp_path / 'classification_failures.csv'
    if damage == 'missing_metadata':
        (tmp_path / 'labeled.meta.json').unlink()
    elif damage == 'duplicate':
        path.write_bytes(csv_bytes(FAILURE_FIELDS, [failure.to_csv_row()] * 2))
    elif damage == 'unknown':
        path.write_bytes(csv_bytes(FAILURE_FIELDS, [failure.to_csv_row() | {'id': 'cfs_' + '0' * 64}]))
    elif damage == 'truncated':
        path.write_bytes(path.read_bytes()[:-1])
    elif damage == 'header':
        path.write_bytes(b'\xef\xbb\xbfwrong\r\n')
    else:
        path.write_bytes(path.read_bytes() + b'\xff\r\n')
    before = path.read_bytes()
    with pytest.raises(AppError):
        with storage().LabelStore(tmp_path, metadata, snapshot):
            pass
    assert path.read_bytes() == before


def test_disk_full_does_not_commit_and_poisons_writer(tmp_path, metadata, snapshot, record, monkeypatch):
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        def fail(fd):
            raise OSError(28, 'disk full')
        with monkeypatch.context() as patch:
            patch.setattr(storage().os, 'fsync', fail)
            with pytest.raises(AppError) as error:
                store.append(record)
        assert error.value.code == 'STORAGE_ERROR'
        assert not store.completed and store.reviewed_total == 0
        with pytest.raises(AppError):
            store.append(record)


@pytest.mark.parametrize('stage', ['write', 'fsync'])
def test_sigint_updates_completed_before_delivery(tmp_path, metadata, snapshot, record, monkeypatch, stage):
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        original = storage().os.fsync if stage == 'fsync' else store._writer.writerow
        def interrupt(value):
            original(value)
            signal.raise_signal(signal.SIGINT)
        with monkeypatch.context() as patch:
            patch.setattr(storage().os if stage == 'fsync' else store._writer,
                          'fsync' if stage == 'fsync' else 'writerow', interrupt)
            with pytest.raises(KeyboardInterrupt):
                store.append(record)
        assert store.completed == {record.id} and store.reviewed_total == 1
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        assert store.completed == {record.id}


def test_atomic_failure_permission_keeps_previous_and_temp(tmp_path, metadata, snapshot, failure, monkeypatch):
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        store.save_failure(failure)
        path = tmp_path / 'classification_failures.csv'
        before = path.read_bytes()
        def fail(src, dst):
            raise PermissionError('held open')
        with monkeypatch.context() as patch:
            patch.setattr(storage().os, 'replace', fail)
            with pytest.raises(AppError) as error:
                store.save_failure(failure.model_copy(update={'attempts': 1}))
        assert error.value.code == 'OUTPUT_LOCKED'
        assert path.read_bytes() == before
        assert list(tmp_path.glob('classification_failures.csv.*.tmp'))
        with pytest.raises(AppError):
            store.save_failure(failure)


def test_rebuilt_same_generation_resumes(config, metadata, snapshot, record, identity, bundle):
    with storage().LabelStore(config.paths.labeled_dir, metadata, snapshot) as store:
        store.append(record)
    preprocess(config)
    rebuilt = storage().load_input(config.paths.processed_dir)
    assert rebuilt.manifest.generation_id != snapshot.manifest.generation_id
    current = import_module('src.classify.fingerprint').build_metadata(config, rebuilt, identity, bundle)
    with storage().LabelStore(config.paths.labeled_dir, current, rebuilt) as store:
        assert store.completed == {record.id}
    old = json.loads((config.paths.labeled_dir / 'labeled.meta.json').read_text(encoding='utf-8'))
    assert old['input_generation_id'] == metadata.input_generation_id


def test_input_unions_all_source_flags(raw_dataset):
    cfg = raw_dataset(['#001\nUIT tốt', '#002\nUIT tốt\n#002\nMột confession khác dài hơn'])
    preprocess(cfg)
    actual = storage().load_input(cfg.paths.processed_dir)
    assert len(actual.confessions) == 2
    assert actual.flags_by_id[actual.confessions[0].id] == ('REPEATED_MARKER', 'SHORT_CONTENT')


def test_metadata_commit_survives_header_creation_failure(tmp_path, metadata, snapshot, monkeypatch):
    from pathlib import Path
    original = Path.open
    def fail_header(path, *args, **kwargs):
        if path == tmp_path / 'labeled.csv' and args and args[0] == 'w':
            raise OSError(28, 'disk full')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', fail_header)
        with pytest.raises(AppError):
            with storage().LabelStore(tmp_path, metadata, snapshot):
                pass
    assert (tmp_path / 'labeled.meta.json').exists()
    assert not (tmp_path / 'labeled.csv').exists()
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        assert not store.completed


def test_failure_disk_full_keeps_previous_csv(tmp_path, metadata, snapshot, failure, monkeypatch):
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        store.save_failure(failure)
        path = tmp_path / 'classification_failures.csv'
        before = path.read_bytes()
        def fail(fd):
            raise OSError(28, 'disk full')
        with monkeypatch.context() as patch:
            patch.setattr(storage().os, 'fsync', fail)
            with pytest.raises(AppError):
                store.save_failure(failure.model_copy(update={'attempts': 1}))
        assert path.read_bytes() == before
        with pytest.raises(AppError):
            store.save_failure(failure)


@pytest.mark.parametrize('kind', ['content', 'flags'])
def test_changed_preprocessing_input_cannot_resume(config, metadata, snapshot, record, identity, bundle, kind):
    from src.identity import make_post_id
    from src.models import RawPost
    from src.storage import RawPostStore
    with storage().LabelStore(config.paths.labeled_dir, metadata, snapshot) as store:
        store.append(record)
    old_directory = config.paths.labeled_dir
    config.paths.labeled_dir = config.paths.data_dir / 'other-labels'
    if kind == 'flags':
        config.preprocess.short_content_chars = 100
    else:
        text = '#003\nConfession mới'
        with RawPostStore(config.paths.raw_csv, config.facebook.page_url) as store:
            store.append(RawPost(id=make_post_id(text), content=text, crawled_at='2026-10-20T00:00:00Z', post_kind='text'))
    preprocess(config)
    current = storage().load_input(config.paths.processed_dir)
    updated = import_module('src.classify.fingerprint').build_metadata(config, current, identity, bundle)
    with pytest.raises(AppError) as error:
        with storage().LabelStore(old_directory, updated, current):
            pass
    assert error.value.code == 'RESUME_MISMATCH'


def test_reject_mutated_record_duplicate_or_unknown_on_append(tmp_path, metadata, snapshot, record):
    with storage().LabelStore(tmp_path, metadata, snapshot) as store:
        with pytest.raises(AppError):
            store.append(record.model_copy(update={'content': 'wrong content'}))
        assert not store.completed
        store.append(record)
        before = (tmp_path / 'labeled.csv').read_bytes()
        with pytest.raises(AppError):
            store.append(record)
        assert (tmp_path / 'labeled.csv').read_bytes() == before
