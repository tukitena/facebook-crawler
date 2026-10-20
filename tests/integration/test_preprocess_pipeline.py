import csv
from importlib import import_module
import json
from pathlib import Path
import signal

import pytest

from src.errors import AppError
from src.identity import make_confession_id
from src.preprocess.storage import validate_generation
from src.storage import resource_lock


@pytest.fixture
def service():
    return import_module('src.preprocess.service')


def rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def rewrite(path, records, fields=None):
    fields = fields or list(records[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(records)


def snapshot(config):
    return {p.name: p.read_bytes() for p in config.paths.processed_dir.iterdir() if not p.name.endswith('.tmp')}


def test_split_dedup_numbers_and_provenance(service, raw_dataset):
    config = raw_dataset(['#001 Xin chào\n#2 Xin chào\n#UITconfessions\nSee less', '#001 Nội dung khác'])
    result = service.preprocess(config, run_id='test-preprocess')
    assert result.exit_code == 0 and result.published
    confessions = rows(config.paths.processed_dir / 'confessions.csv')
    sources = rows(config.paths.processed_dir / 'sources.csv')
    assert [(r['content'], r['confession_number']) for r in confessions] == [('Xin chào', '001'), ('Nội dung khác', '001')]
    assert [r['marker'] for r in sources] == ['001', '2', '001']
    assert len({r['id'] for r in confessions}) == 2
    assert (result.stats.sources, result.stats.unique_confessions, result.stats.duplicate_confessions) == (3, 2, 1)
    assert validate_generation(config.paths.processed_dir).generation_id == result.generation_id
    report = json.loads((config.paths.reports_dir / 'test-preprocess.json').read_text(encoding='utf-8'))
    assert report['published'] and report['command'] == 'preprocess'
    assert report['output_hashes']['confessions.csv'] and report['raw_csv_sha256']
    assert 'Xin chào' not in json.dumps(report, ensure_ascii=False)


def test_dedup_raw_whitespace_and_repeated_numbers(service, raw_dataset):
    config = raw_dataset(['#001 Xin\tchào\n#001 Khác', '#9 Xin cha\u0300o\n#001 Còn khác'])
    raw = rows(config.paths.raw_csv)
    rewrite(config.paths.raw_csv, raw + [raw[0]])
    result = service.preprocess(config)
    confessions = rows(config.paths.processed_dir / 'confessions.csv')
    sources = rows(config.paths.processed_dir / 'sources.csv')
    assert [r['content'] for r in confessions] == ['Xin\tchào', 'Khác', 'Còn khác']
    assert confessions[0]['confession_number'] == '001'
    assert [r['flags'] for r in sources] == ['REPEATED_MARKER;SHORT_CONTENT', 'REPEATED_MARKER;SHORT_CONTENT', 'SHORT_CONTENT', 'SHORT_CONTENT']
    assert result.stats.duplicate_posts == 1 and result.stats.sources == 4


def test_excluded_order_and_counter_invariants(service, raw_dataset):
    config = raw_dataset(['Mở đầu\n#1 A\n--\nNgoài\n#2\n#UITconfessions Ẩn bớt', 'Không marker See less'])
    result = service.preprocess(config)
    audit = rows(config.paths.processed_dir / 'excluded.csv')
    assert [(r['reason'], int(r['start_line']), int(r['end_line'])) for r in audit] == [
        ('PREAMBLE', 1, 1), ('AFTER_SEPARATOR', 4, 4), ('EMPTY_SEGMENT', 5, 5),
        ('UI_CONTROL', 6, 6), ('POST_FOOTER', 6, 6), ('NO_MARKER', 1, 1)]
    s = result.stats
    assert (s.read_posts, s.accepted_posts, s.excluded_posts, s.candidate_segments, s.empty_segments) == (2, 1, 1, 2, 1)
    assert (s.unique_confessions, s.duplicate_confessions, s.sources, s.excluded_regions) == (1, 0, 1, 6)
    assert (s.separators, s.ui_controls_removed, s.footers_removed) == (1, 2, 1)
    assert s.read_posts == s.duplicate_posts + s.accepted_posts + s.excluded_posts
    assert s.candidate_segments == s.sources + s.empty_segments
    assert s.sources == s.unique_confessions + s.duplicate_confessions


@pytest.mark.parametrize('texts', [[], ['Không có marker']])
def test_empty_dataset_and_all_excluded(service, raw_dataset, texts):
    config = raw_dataset(texts)
    summary = service.preprocess(config)
    assert summary.published and summary.stats.unique_confessions == 0
    assert rows(config.paths.processed_dir / 'confessions.csv') == []
    assert rows(config.paths.processed_dir / 'sources.csv') == []
    validate_generation(config.paths.processed_dir)


def test_raw_and_metadata_are_unchanged(service, raw_dataset):
    config = raw_dataset(['#1 Nội dung'])
    before = {p: p.read_bytes() for p in (config.paths.raw_csv, config.paths.raw_csv.with_suffix('.meta.json'))}
    service.preprocess(config)
    assert all(p.read_bytes() == data for p, data in before.items())


def test_late_raw_error_keeps_previous_generation(service, raw_dataset):
    config = raw_dataset(['#1 Cũ'])
    service.preprocess(config)
    before = snapshot(config)
    with config.paths.raw_csv.open('a', encoding='utf-8', newline='') as stream:
        csv.writer(stream).writerow(['bad', 'secret raw body', 'wrong'])
    with pytest.raises(AppError) as error:
        service.preprocess(config, run_id='bad-raw')
    assert error.value.code == 'INVALID_CSV' and error.value.exit_code == 2
    assert snapshot(config) == before
    validate_generation(config.paths.processed_dir)
    assert json.loads((config.paths.reports_dir / 'bad-raw.json').read_text())['status'] == 'failed'


@pytest.mark.parametrize('damage', ['unknown', 'duplicate', 'accept-no-marker'])
def test_invalid_override_never_publishes(service, raw_dataset, damage):
    config = raw_dataset(['Không marker'])
    identifier = rows(config.paths.raw_csv)[0]['id']
    override = config.paths.data_dir / 'overrides.csv'
    records = [{'post_id': identifier, 'decision': 'accept', 'note': 'private note'}]
    if damage == 'unknown':
        records[0]['post_id'] = 'post_' + 'f' * 64
    if damage == 'duplicate':
        records += records
    rewrite(override, records)
    config.preprocess.overrides_csv = override
    if damage == 'accept-no-marker':
        assert service.preprocess(config).stats.excluded_posts == 1
        assert rows(config.paths.processed_dir / 'excluded.csv')[0]['reason'] == 'NO_MARKER'
    else:
        with pytest.raises(AppError, match='override') as error:
            service.preprocess(config)
        assert error.value.code == 'CONFIG_ERROR'
        assert not (config.paths.processed_dir / 'preprocess.manifest.json').exists()


def test_pipeline_is_byte_deterministic_and_manual_flags(service, raw_dataset):
    config = raw_dataset(['#001 A\n#2 B #UITconfessions See less'])
    override = config.paths.data_dir / 'overrides.csv'
    rewrite(override, [{'post_id': rows(config.paths.raw_csv)[0]['id'], 'decision': 'accept', 'note': ''}])
    config.preprocess.overrides_csv = override
    first = service.preprocess(config)
    before = snapshot(config)
    second = service.preprocess(config)
    assert first.generation_id != second.generation_id
    assert all((config.paths.processed_dir / name).read_bytes() == data for name, data in before.items() if name.endswith('.csv'))
    assert all('MANUAL_ACCEPT' in r['flags'] for r in rows(config.paths.processed_dir / 'sources.csv'))


def test_conflicting_labeled_blocks_pipeline(service, raw_dataset):
    config = raw_dataset(['#1 A', '#2 B'])
    service.preprocess(config)
    rewrite(config.paths.labeled_dir / 'labeled.csv', [{'id': make_confession_id('A'), 'content': 'A', 'label': 'Neutral',
            'confidence': '0.8', 'reason': 'Không đánh giá.', 'needs_review': 'false', 'review_reasons': ''}])
    before = snapshot(config)
    rewrite(config.paths.raw_csv, rows(config.paths.raw_csv)[:1])
    with pytest.raises(AppError) as error:
        service.preprocess(config)
    assert error.value.code == 'DERIVED_DATA_CONFLICT' and snapshot(config) == before


def test_identity_collision_does_not_publish(service, raw_dataset, monkeypatch):
    config = raw_dataset(['#1 A\n#2 B'])
    monkeypatch.setattr(service, 'make_confession_id', lambda _: make_confession_id('A'))
    with pytest.raises(AppError) as error:
        service.preprocess(config)
    assert error.value.code == 'IDENTITY_CONFLICT'
    assert not (config.paths.processed_dir / 'preprocess.manifest.json').exists()


def test_lock_is_exclusive_and_released(service, raw_dataset):
    config = raw_dataset(['#1 A'])
    lock = config.paths.data_dir / '.dataset.lock'
    with resource_lock(lock):
        with pytest.raises(AppError) as error:
            service.preprocess(config, run_id='locked')
        assert error.value.code == 'RESOURCE_LOCKED'
    assert not (config.paths.reports_dir / 'locked.json').exists()
    service.preprocess(config)
    with resource_lock(lock):
        pass


def test_interrupt_before_publish_keeps_previous_generation(service, raw_dataset):
    config = raw_dataset(['#1 A'])
    service.preprocess(config)
    before = snapshot(config)
    def interrupt(stats):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        service.preprocess(config, progress=interrupt, run_id='interrupted')
    assert snapshot(config) == before
    report = json.loads((config.paths.reports_dir / 'interrupted.json').read_text())
    assert report['status'] == 'interrupted' and not report['published'] and report['exit_code'] == 130
    with resource_lock(config.paths.data_dir / '.dataset.lock'):
        pass


def test_interrupt_during_publish_reports_published_generation(service, raw_dataset, monkeypatch):
    config = raw_dataset(['#1 A'])
    storage = import_module('src.preprocess.storage')
    original = storage.os.replace
    def interrupt(source, destination):
        original(source, destination)
        if Path(destination).name == 'confessions.csv':
            signal.raise_signal(signal.SIGINT)
    monkeypatch.setattr(storage.os, 'replace', interrupt)
    with pytest.raises(KeyboardInterrupt):
        service.preprocess(config, run_id='published-interrupt')
    manifest = validate_generation(config.paths.processed_dir)
    report = json.loads((config.paths.reports_dir / 'published-interrupt.json').read_text())
    assert report['published'] and report['generation_id'] == manifest.generation_id and report['exit_code'] == 130


def test_report_failure_after_publish_keeps_generation(service, raw_dataset, monkeypatch):
    config = raw_dataset(['#1 A'])
    def failure(*args):
        raise OSError('private message')
    monkeypatch.setattr(service, 'atomic_write_json', failure)
    with pytest.raises(AppError) as error:
        service.preprocess(config)
    assert error.value.code == 'STORAGE_ERROR' and error.value.exit_code == 1
    assert 'generation' in str(error.value) and 'report' in str(error.value)
    validate_generation(config.paths.processed_dir)


def test_report_failure_does_not_mask_primary_error(service, raw_dataset, monkeypatch, caplog):
    config = raw_dataset(['#1 A'])
    config.paths.raw_csv.write_bytes(b'private broken body\r\n')
    def failure(*args):
        raise OSError('private report detail')
    monkeypatch.setattr(service, 'atomic_write_json', failure)
    with pytest.raises(AppError) as error:
        service.preprocess(config)
    assert error.value.code == 'INVALID_CSV'
    assert any(getattr(record, 'error_code', '') == 'REPORT_WRITE' for record in caplog.records)
    assert 'private' not in caplog.text


def test_report_is_written_under_lock_and_progress_is_snapshot(service, raw_dataset, monkeypatch):
    config = raw_dataset(['#1 A', '#2 B'])
    original = service.atomic_write_json
    def report(path, payload):
        with pytest.raises(AppError) as error:
            with resource_lock(config.paths.data_dir / '.dataset.lock'):
                pass
        assert error.value.code == 'RESOURCE_LOCKED'
        original(path, payload)
    def progress(stats):
        stats.sources = 999
    monkeypatch.setattr(service, 'atomic_write_json', report)
    assert service.preprocess(config, progress=progress).stats.sources == 2


@pytest.mark.parametrize('missing,code', [('raw', 'INPUT_NOT_FOUND'), ('metadata', 'RESUME_MISMATCH')])
def test_missing_input_error_codes(service, raw_dataset, missing, code):
    config = raw_dataset(['#1 A'])
    path = config.paths.raw_csv if missing == 'raw' else config.paths.raw_csv.with_suffix('.meta.json')
    path.unlink()
    with pytest.raises(AppError) as error:
        service.preprocess(config)
    assert error.value.code == code
