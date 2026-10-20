import pytest
import yaml

from src import cli
from src.classify.models import ClassificationStats, ClassificationSummary
from src.config import load_config
from src.errors import AppError


def forbidden(*args, **kwargs):
    raise AssertionError('crawl/browser/runtime must not run')


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(cli, 'crawl', forbidden)
    return path


def test_classify_help_without_runtime(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'crawl', forbidden)
    monkeypatch.setattr(cli, 'load_config', forbidden)
    monkeypatch.setattr(cli, 'configure_logging', forbidden)
    for args in (['--help'], ['classify', '--help']):
        with pytest.raises(SystemExit) as error:
            cli.main(args)
        assert error.value.code == 0
        output = capsys.readouterr().out
        assert 'classify' in output
    assert '--limit' in output


@pytest.mark.parametrize('limit', ['0', '-1', 'text', '1.5', 'true'])
def test_classify_rejects_invalid_limit_before_config(limit, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'load_config', forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(['classify', '--limit', limit])
    assert error.value.code == 2
    assert '--limit' in capsys.readouterr().err


@pytest.mark.parametrize('status,reason,code,limit', [
    ('complete', 'completed', 0, None),
    ('partial', 'limit_reached', 3, 2),
    ('interrupted', 'keyboard_interrupt', 130, None),
])
def test_classify_dispatch_summary_and_limit(config_path, monkeypatch, capsys, status, reason, code, limit):
    def run(config, limit=None, *, progress, run_id, config_path):
        assert config_path == path
        assert config.crawl.max_posts == 200
        assert limit == expected_limit
        stats = ClassificationStats(total=10, already_done=3, attempted=2, newly_saved=2,
                                    remaining=5, reviewed_new=1, reviewed_total=2)
        progress(stats)
        return ClassificationSummary(run_id, stats, status, reason, code)

    def load(path, overrides):
        assert overrides == {}  # --limit must never become a crawl override.
        return load_config(path, overrides)

    path, expected_limit = config_path, limit
    monkeypatch.setattr(cli, 'classify', run, raising=False)
    monkeypatch.setattr(cli, 'load_config', load)
    args = ['--config', str(path), 'classify']
    if limit is not None:
        args.extend(['--limit', str(limit)])
    assert cli.main(args) == code
    output = capsys.readouterr().err
    assert f'classify: {status}' in output and reason in output
    assert str(path.parent / 'data/uit/reports') in output
    assert output.count('newly_saved=2') == 2  # Final output bypasses throttle.
    assert 'review_rate=50.0%' in output
    assert 'Chrome' not in output and 'candidate_segments' not in output


@pytest.mark.parametrize('error,code,phrase', [
    (AppError('MODEL_UNAVAILABLE', 'Model service unavailable.'), 1, 'MODEL_UNAVAILABLE'),
    (KeyboardInterrupt(), 130, 'classify'),
    (RuntimeError('private-content'), 1, 'classify'),
])
def test_classify_error_messages_are_safe(config_path, monkeypatch, capsys, error, code, phrase):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(cli, 'classify', fail, raising=False)
    assert cli.main(['--config', str(config_path), 'classify']) == code
    output = capsys.readouterr().err
    assert phrase in output and 'private-content' not in output
    assert 'Chrome' not in output and 'candidate_segments' not in output


@pytest.mark.parametrize('collision', ['config', 'prompt'])
def test_classify_validates_paths_before_logging(config_path, monkeypatch, collision):
    config = load_config(config_path)
    values = {'paths': {name: str(value) for name, value in config.paths.model_dump().items()}}
    if collision == 'config':
        path = config.paths.log_dir / 'crawl.jsonl'
    else:
        path = config_path
        prompt = config.paths.log_dir / 'crawl.jsonl'
        prompt.parent.mkdir(parents=True)
        prompt.write_text('private prompt', encoding='utf-8')
        values['classification'] = {'prompt_path': str(prompt)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(values), encoding='utf-8')
    before = path.read_bytes()
    monkeypatch.setattr(cli, 'configure_logging', forbidden)
    monkeypatch.setattr(cli, 'classify', forbidden, raising=False)
    assert cli.main(['--config', str(path), 'classify']) == 2
    assert path.read_bytes() == before
    if collision == 'prompt':
        assert prompt.read_text(encoding='utf-8') == 'private prompt'


def test_classify_progress_throttles_but_forces_failures_and_final(capsys):
    ticks = iter([0., 0., .1, .2, .3, 1.3, 1.4])
    progress = cli.ClassificationConsoleProgress(clock=lambda: next(ticks))
    stats = ClassificationStats(total=10, already_done=3, newly_saved=2, remaining=5, reviewed_new=1)
    progress(stats)
    progress(stats)
    assert len(capsys.readouterr().err.splitlines()) == 1
    stats.failed = 1
    progress(stats)
    output = capsys.readouterr().err
    assert len(output.splitlines()) == 1 and 'failed=1' in output
    progress(stats)
    assert capsys.readouterr().err == ''
    progress(stats)
    output = capsys.readouterr().err
    assert 'total=10' in output and 'already_done=3' in output
    assert 'newly_saved=2' in output and 'remaining=5' in output
    assert 'review_rate=50.0%' in output and '\x1b' not in output
    progress(ClassificationStats(), force=True)
    output = capsys.readouterr().err
    assert len(output.splitlines()) == 1 and 'review_rate=0.0%' in output
