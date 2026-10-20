from importlib import import_module
import os
import subprocess

import pytest

from src.errors import AppError
from test_classify_fingerprint import config


def validate(config, config_path=None):
    return import_module('src.classify.storage').validate_classify_paths(config, config_path)


def test_default_paths_are_valid(config):
    validate(config)


@pytest.mark.parametrize('target', ['labeled', 'reports', 'log', 'lock'])
@pytest.mark.parametrize('source', ['raw', 'processed', 'prompt', 'config', 'override', 'profile'])
def test_existing_hardlink_targets_rejected_without_input_changes(config, target, source):
    root = config.paths.data_dir.parent.parent
    config_path = root / 'config.yaml'
    override = root / 'overrides.csv'
    override.write_bytes(b'private overrides')
    config.preprocess.overrides_csv = override
    profile = config.chrome.user_data_dir / 'state'
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_bytes(b'private profile')
    inputs = {'raw': config.paths.raw_csv, 'processed': config.paths.processed_dir / 'confessions.csv',
              'prompt': config.classification.prompt_path, 'config': config_path,
              'override': override, 'profile': profile}
    source_path = inputs[source]
    dest = {'labeled': config.paths.labeled_dir / 'labeled.csv', 'reports': config.paths.reports_dir / 'run.json',
            'log': config.paths.log_dir / 'crawl.jsonl', 'lock': config.paths.data_dir / '.dataset.lock'}[target]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    os.link(source_path, dest)
    before = source_path.read_bytes()
    try:
        with pytest.raises(AppError) as error:
            validate(config, config_path)
        assert error.value.code == 'CONFIG_ERROR'
        assert source_path.read_bytes() == before
    finally:
        dest.unlink()


@pytest.mark.parametrize('output,input_name', [('labeled_dir', 'processed_dir'), ('reports_dir', 'processed_dir'),
                                              ('log_dir', 'processed_dir'), ('labeled_dir', 'log_dir')])
def test_overlapping_directories_rejected(config, output, input_name):
    setattr(config.paths, output, getattr(config.paths, input_name))
    with pytest.raises(AppError):
        validate(config)


def test_report_directory_cannot_replace_absent_lock(config):
    lock = config.paths.data_dir / '.dataset.lock'
    lock.unlink()
    config.paths.reports_dir = lock
    with pytest.raises(AppError) as error:
        validate(config)
    assert error.value.code == 'CONFIG_ERROR'
    assert not lock.exists()


def test_symlink_output_rejected(config):
    destination = config.paths.labeled_dir
    try:
        destination.symlink_to(config.paths.processed_dir, target_is_directory=True)
    except OSError:
        pytest.skip('Host does not permit symlink creation')
    with pytest.raises(AppError):
        validate(config)


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction protection')
def test_junction_output_rejected(config):
    destination = config.paths.labeled_dir
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(destination), str(config.paths.processed_dir)],
                            capture_output=True, check=False)
    if result.returncode:
        pytest.skip('Host does not permit junction creation')
    try:
        with pytest.raises(AppError):
            validate(config)
    finally:
        # Remove only the junction itself, never traverse the input directory.
        os.rmdir(destination)
