from dataclasses import asdict
from importlib import import_module
import json
from unittest.mock import Mock

import pytest
import yaml

from src import cli
from src.errors import AppError
from src.preprocess.models import PreprocessStats, PreprocessSummary
from src.preprocess.storage import validate_generation


def test_preprocess_help_without_runtime(monkeypatch, capsys):
    crawl = Mock(side_effect=AssertionError('runtime invoked'))
    monkeypatch.setattr(cli, 'crawl', crawl)
    monkeypatch.setattr(cli, 'configure_logging', crawl)
    with pytest.raises(SystemExit) as error:
        cli.main(['--help'])
    assert error.value.code == 0 and 'preprocess' in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        cli.main(['preprocess', '--help'])
    assert error.value.code == 0
    crawl.assert_not_called()


def test_preprocess_dispatch_and_summary(raw_dataset, monkeypatch, capsys):
    config = raw_dataset(['#1 A'])
    config_path = config.paths.data_dir.parent.parent / 'config.yaml'
    result = PreprocessSummary('run', 'complete', 'completed', PreprocessStats(unique_confessions=1), 0, 'gen', True)
    run, crawl = Mock(return_value=result), Mock(side_effect=AssertionError('crawl invoked'))
    monkeypatch.setattr(cli, 'preprocess', run)
    monkeypatch.setattr(cli, 'crawl', crawl)
    assert cli.main(['--config', str(config_path), 'preprocess']) == 0
    run.assert_called_once()
    assert run.call_args.args[0] == config
    assert run.call_args.kwargs['run_id'] and callable(run.call_args.kwargs['progress'])
    crawl.assert_not_called()
    text = capsys.readouterr().err
    assert 'unique_confessions=1' in text and str(config.paths.reports_dir) in text
    assert 'generation=gen' in text and 'chưa phải số confession' not in text


@pytest.mark.parametrize('error,code,phrase', [
    (AppError('INVALID_CSV', 'CSV không hợp lệ.', 2), 2, 'INVALID_CSV'),
    (AppError('STORAGE_ERROR', 'Không ghi được.', 1), 1, 'STORAGE_ERROR'),
    (KeyboardInterrupt(), 130, 'preprocess'),
    (RuntimeError('private-body'), 1, 'preprocess'),
])
def test_preprocess_errors_and_interrupt(raw_dataset, monkeypatch, capsys, error, code, phrase):
    config = raw_dataset([])
    path = config.paths.data_dir.parent.parent / 'config.yaml'
    monkeypatch.setattr(cli, 'preprocess', Mock(side_effect=error))
    assert cli.main(['--config', str(path), 'preprocess']) == code
    text = capsys.readouterr().err
    assert phrase in text and 'private-body' not in text and 'Chrome' not in text
    if code == 130:
        assert 'raw' in text and 'report' in text and 'rebuild' in text


def test_report_failure_after_publish_keeps_generation(raw_dataset, monkeypatch, capsys):
    config = raw_dataset(['#1 A'])
    path = config.paths.data_dir.parent.parent / 'config.yaml'
    service = import_module('src.preprocess.service')
    monkeypatch.setattr(service, 'atomic_write_json', Mock(side_effect=OSError('private')))
    assert cli.main(['--config', str(path), 'preprocess']) == 1
    text = capsys.readouterr().err
    assert 'đã publish' in text and 'report' in text and 'rollback' not in text and 'private' not in text
    validate_generation(config.paths.processed_dir)


@pytest.mark.parametrize('collision', ['config', 'override', 'log'])
def test_output_collision_preserves_operator_input(raw_dataset, monkeypatch, collision):
    config = raw_dataset([])
    root = config.paths.data_dir.parent.parent
    path = root / 'config.yaml'
    values = {}
    if collision == 'config':
        path = config.paths.processed_dir / 'confessions.csv'
        values['paths'] = {key: str(value) for key, value in config.paths.model_dump().items()}
        values['chrome'] = {'user_data_dir': str(config.chrome.user_data_dir)}
    else:
        override = (config.paths.processed_dir / 'sources.csv' if collision == 'override'
                    else config.paths.log_dir / 'crawl.jsonl')
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_bytes(b'operator-owned input')
        values['preprocess'] = {'overrides_csv': str(override)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(values), encoding='utf-8')
    before = {p: p.read_bytes() for p in (path,) if p.exists()}
    if collision != 'config':
        before[override] = override.read_bytes()
    logging = Mock(side_effect=AssertionError('logging before validation'))
    monkeypatch.setattr(cli, 'configure_logging', logging)
    assert cli.main(['--config', str(path), 'preprocess']) == 2
    logging.assert_not_called()
    assert all(p.read_bytes() == data for p, data in before.items())


def test_preprocess_progress_is_throttled(capsys):
    ticks = iter([0., 0., .1, 1.1, 1.2])
    progress = cli.PreprocessConsoleProgress(clock=lambda: next(ticks))
    stats = PreprocessStats(read_posts=3, unique_confessions=2)
    progress(stats)
    progress(stats)
    progress(stats)
    text = capsys.readouterr().err
    assert len(text.splitlines()) == 2 and '\x1b' not in text
    assert 'read_posts=3' in text and 'elapsed=1.1s' in text
    progress(stats, force=True)
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_preprocess_cli_logs_service_completion(raw_dataset):
    config = raw_dataset(['#1 A'])
    path = config.paths.data_dir.parent.parent / 'config.yaml'
    assert cli.main(['--config', str(path), 'preprocess']) == 0
    records = [json.loads(line) for line in (config.paths.log_dir / 'crawl.jsonl').read_text(encoding='utf-8').splitlines()]
    finished = [r for r in records if r['event'] == 'run_finished']
    assert len(finished) == 1 and finished[0]['command'] == 'preprocess'
    assert finished[0]['sources'] == 1 and finished[0]['read_posts'] == 1
