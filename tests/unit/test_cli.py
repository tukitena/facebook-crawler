from unittest.mock import Mock

import pytest

from src import cli
from src.errors import AppError
from src.models import CrawlStats, CrawlSummary


def test_help_does_not_start_browser(monkeypatch, capsys):
    run = Mock()
    monkeypatch.setattr(cli, 'crawl', run)
    with pytest.raises(SystemExit) as result:
        cli.main(['crawl', '--help'])
    assert result.value.code == 0
    assert '--max-posts' in capsys.readouterr().out
    run.assert_not_called()


@pytest.mark.parametrize('failure,code', [(KeyboardInterrupt(), 130),
    (AppError('STORAGE_ERROR', 'safe', 1), 1), (AppError('RESOURCE_LOCKED', 'safe', 2), 2),
    (RuntimeError('sensitive browser details'), 1)])
def test_errors_map_to_exit_codes_without_leaking_browser_details(tmp_path, monkeypatch, capsys, failure, code):
    config = tmp_path / 'config.yaml'
    config.write_text('{}')
    monkeypatch.setattr(cli, 'crawl', Mock(side_effect=failure))
    assert cli.main(['--config', str(config), 'crawl']) == code
    assert 'sensitive browser details' not in capsys.readouterr().err


def test_cli_overrides_and_partial_result(tmp_path, monkeypatch, capsys):
    config = tmp_path / 'config.yaml'
    config.write_text('{}')
    run = Mock(return_value=CrawlSummary('test', 'partial', 'session_checkpoint', CrawlStats(saved_posts=2), 3))
    monkeypatch.setattr(cli, 'crawl', run)
    assert cli.main(['--config', str(config), 'crawl', '--max-posts', '3', '--max-scrolls', '4']) == 3
    assert run.call_args.args[0].crawl.max_posts == 3
    assert run.call_args.args[0].crawl.max_scrolls == 4
    assert 'session_checkpoint' in capsys.readouterr().err


def test_missing_config_returns_2(tmp_path, capsys):
    assert cli.main(['--config', str(tmp_path / 'missing'), 'crawl']) == 2
    assert 'config.example.yaml' in capsys.readouterr().err


def test_progress_throttled_plain_lines(capsys):
    moments = iter([0.0, 0.0, 0.1, 1.1])
    progress = cli.ConsoleProgress(clock=lambda: next(moments))
    progress(CrawlStats(saved_posts=1))
    progress(CrawlStats(saved_posts=2))
    progress(CrawlStats(saved_posts=3))
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert 'saved_posts=1' in lines[0]
    assert 'saved_posts=3' in lines[1]
    assert all('\x1b' not in line for line in lines)
