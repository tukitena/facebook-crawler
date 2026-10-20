from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from src import cli
from src.config import load_config
from src.storage import resource_lock


@pytest.fixture
def reset_config(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({
        'paths': {
            'data_dir': 'dataset',
            'raw_csv': 'dataset/raw/posts.csv',
            'processed_dir': 'dataset/clean',
            'labeled_dir': 'dataset/labels',
            'reports_dir': 'dataset/reports',
        },
        'preprocess': {'overrides_csv': 'dataset/clean/post_overrides.csv'},
        'classification': {'prompt_path': 'dataset/labels/prompt.txt'},
    }), encoding='utf-8')
    return path, load_config(path)


def managed_files(config):
    return [
        config.paths.raw_csv,
        config.paths.raw_csv.with_suffix('.meta.json'),
        *(config.paths.processed_dir / name for name in (
            'confessions.csv', 'sources.csv', 'excluded.csv', 'preprocess.manifest.json')),
        *(config.paths.labeled_dir / name for name in (
            'labeled.csv', 'classification_failures.csv', 'labeled.meta.json')),
    ]


def seed_files(paths):
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'corrupt old data\x00\xff')


def test_reset_help_without_config_or_browser(monkeypatch, capsys):
    run = Mock()
    monkeypatch.setattr(cli, 'crawl', run)
    with pytest.raises(SystemExit) as result:
        cli.main(['reset', '--help'])
    assert result.value.code == 0
    assert 'reset' in capsys.readouterr().out
    run.assert_not_called()


def test_reset_removes_only_managed_files_and_preserves_other_data(reset_config, monkeypatch, capsys):
    path, config = reset_config
    targets = managed_files(config)
    preserved = [
        config.chrome.user_data_dir / 'Default/Cookies',
        config.preprocess.overrides_csv, config.classification.prompt_path,
        config.paths.log_dir / 'history.jsonl', config.paths.reports_dir / 'old.json',
        config.paths.raw_csv.parent / 'notes.csv', config.paths.processed_dir / 'manual.csv',
        config.paths.labeled_dir / 'labeled.csv.backup',
        config.paths.data_dir / 'other-source/raw_posts.csv',
        config.paths.data_dir.parent / 'other-source/raw_posts.csv',
    ]
    seed_files(targets + preserved)
    expected = {item: item.read_bytes() for item in preserved + [path]}
    run = Mock(side_effect=AssertionError('reset must not crawl'))
    monkeypatch.setattr(cli, 'crawl', run)
    with resource_lock(config.chrome.user_data_dir.with_name('chrome-profile.lock')):
        assert cli.main(['--config', str(path), 'reset']) == 0
    assert all(not item.exists() for item in targets)
    assert all(item.read_bytes() == value for item, value in expected.items())
    assert config.paths.processed_dir.is_dir()
    assert (config.paths.data_dir / '.dataset.lock').is_file()
    assert 'python main.py crawl' in capsys.readouterr().err
    run.assert_not_called()


def test_reset_is_idempotent_on_missing_dataset(reset_config):
    path, config = reset_config
    for _ in range(2):
        assert cli.main(['--config', str(path), 'reset']) == 0
        assert all(not item.exists() for item in managed_files(config))
    with resource_lock(config.paths.data_dir / '.dataset.lock'):
        pass


@pytest.mark.parametrize('protected', ['config', 'prompt', 'overrides', 'lock'])
def test_reset_rejects_targets_that_are_protected_files(reset_config, monkeypatch, protected):
    path, config = reset_config
    if protected == 'config':
        path = config.paths.raw_csv
        path.parent.mkdir(parents=True)
        path.write_text('{}', encoding='utf-8')
    elif protected == 'prompt':
        config.classification.prompt_path = config.paths.raw_csv.with_suffix('.meta.json')
    elif protected == 'overrides':
        config.preprocess.overrides_csv = config.paths.processed_dir / 'sources.csv'
    else:
        config.paths.raw_csv = config.paths.data_dir / '.dataset.lock'
    targets = managed_files(config)
    seed_files([item for item in targets if item != path])
    expected = {item: item.read_bytes() for item in targets}
    monkeypatch.setattr(cli, 'load_config', lambda *args: config)
    assert cli.main(['--config', str(path), 'reset']) == 2
    assert all(item.read_bytes() == value for item, value in expected.items())


def test_reset_revalidates_paths_before_deleting(reset_config, monkeypatch):
    path, config = reset_config
    # Covers a path changed after config load (including a changed junction).
    config.paths.labeled_dir = config.paths.data_dir.parent / 'outside'
    targets = managed_files(config)
    seed_files(targets)
    monkeypatch.setattr(cli, 'load_config', lambda *args: config)
    assert cli.main(['--config', str(path), 'reset']) == 2
    assert all(item.exists() for item in targets)


@pytest.mark.parametrize('failure,code', [(PermissionError('open in Excel'), 1),
                                        (OSError('disk error'), 1), (KeyboardInterrupt(), 130)])
def test_reset_partial_failure_is_not_success_and_can_be_retried(reset_config, monkeypatch, capsys, failure, code):
    path, config = reset_config
    targets = managed_files(config)
    seed_files(targets)
    failed_path = targets[2]
    original = Path.unlink

    def fail_one(self, *args, **kwargs):
        if self == failed_path:
            raise failure
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail_one)
        assert cli.main(['--config', str(path), 'reset']) == code
    message = capsys.readouterr().err
    assert 'reset' in message
    if code == 1:
        assert str(failed_path) in message
    assert failed_path.exists()
    assert not targets[0].exists()
    assert cli.main(['--config', str(path), 'reset']) == 0
    assert all(not item.exists() for item in targets)


def test_reset_rejects_invalid_config_without_changes(tmp_path):
    outside = tmp_path / 'outside.csv'
    seed_files([outside])
    path = tmp_path / 'config.yaml'
    path.write_text('paths: {raw_csv: outside.csv}', encoding='utf-8')
    assert cli.main(['--config', str(path), 'reset']) == 2
    assert outside.read_bytes() == b'corrupt old data\x00\xff'


def test_reset_missing_config_returns_2(tmp_path):
    assert cli.main(['--config', str(tmp_path / 'missing.yaml'), 'reset']) == 2


def test_reset_cannot_report_success_if_a_file_remains(reset_config, monkeypatch):
    path, config = reset_config
    targets = managed_files(config)
    seed_files(targets)
    original = Path.unlink

    def retain_raw(self, *args, **kwargs):
        if self != config.paths.raw_csv:
            return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', retain_raw)
    assert cli.main(['--config', str(path), 'reset']) == 1
    assert config.paths.raw_csv.exists()


@pytest.mark.parametrize('protected', ['config', 'prompt', 'overrides'])
@pytest.mark.parametrize('suffix', ['', '.1'])
def test_reset_rejects_logger_aliases_before_writing_or_rotating(reset_config, protected, suffix):
    path, config = reset_config
    collision = config.paths.log_dir / ('crawl.jsonl' + suffix)
    collision.parent.mkdir(parents=True)
    collision.write_text('operator-owned input\n', encoding='utf-8')
    config.logging.max_bytes = 1  # Force rotation on the first event.
    if protected == 'config':
        path = collision
    elif protected == 'prompt':
        config.classification.prompt_path = collision
    else:
        config.preprocess.overrides_csv = collision
    path.write_text(yaml.safe_dump(config.model_dump(mode='json')), encoding='utf-8')
    targets = managed_files(config)
    seed_files(targets)
    before = collision.read_bytes()
    assert cli.main(['--config', str(path), 'reset']) == 2
    assert collision.read_bytes() == before
    assert all(item.exists() for item in targets)
