import json
from unittest.mock import Mock

import pytest

from src.config import load_config
from src.crawl.service import crawl, crawl_feed
from src.models import CrawlStats, ExtractedPost, ExtractionResult, FeedProgress, SessionState
from src.storage import RawPostStore, resource_lock

URL = 'https://www.facebook.com/UITconfess'


def config_at(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('crawl: {max_posts: 1, max_scrolls: 1}')
    return load_config(path)


def test_interrupt_then_resume_without_duplicates(tmp_path):
    config = config_at(tmp_path)
    config.crawl.max_posts = 2
    adapter = Mock()
    adapter.check_session.return_value = SessionState.AUTHENTICATED
    adapter.extract_visible_posts.return_value = [ExtractionResult('first', ExtractedPost('#1 Full', post_kind='text'))]
    adapter.scroll_and_wait.side_effect = KeyboardInterrupt
    stats = CrawlStats()
    with RawPostStore(config.paths.raw_csv, URL) as store:
        with pytest.raises(KeyboardInterrupt):
            crawl_feed(None, adapter, store, config, stats, None)
    assert stats.saved_posts == 1
    adapter.scroll_and_wait.side_effect = None
    adapter.scroll_and_wait.return_value = FeedProgress(frozenset({'first'}))
    with RawPostStore(config.paths.raw_csv, URL) as store:
        assert len(store.existing) == 1
        new_stats = CrawlStats()
        crawl_feed(None, adapter, store, config, new_stats, None)
        assert new_stats.saved_posts == 0
        assert len(store.existing) == 1


def test_interrupt_during_commit_updates_counters_before_cleanup(tmp_path, monkeypatch):
    import signal
    import src.storage as storage
    config = config_at(tmp_path)
    adapter = Mock()
    adapter.check_session.return_value = SessionState.AUTHENTICATED
    adapter.extract_visible_posts.return_value = [ExtractionResult('first', ExtractedPost('#1 Full', post_kind='text'))]
    stats = CrawlStats()
    fsync = storage.os.fsync
    with RawPostStore(config.paths.raw_csv, URL) as store:
        def interrupt(fd):
            signal.raise_signal(signal.SIGINT)
            fsync(fd)
        monkeypatch.setattr(storage.os, 'fsync', interrupt)
        with pytest.raises(KeyboardInterrupt):
            crawl_feed(None, adapter, store, config, stats, None)
        assert stats.saved_posts == 1
    monkeypatch.setattr(storage.os, 'fsync', fsync)
    with RawPostStore(config.paths.raw_csv, URL) as store:
        assert len(store.existing) == 1


@pytest.mark.parametrize('failure', [KeyboardInterrupt(), RuntimeError('synthetic crash')])
def test_resources_release_and_report_on_failure(tmp_path, monkeypatch, failure):
    import src.crawl.service as module
    config = config_at(tmp_path)
    runtime = Mock()
    runtime.__enter__ = Mock(return_value=runtime)
    runtime.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(module, 'sync_playwright', lambda: runtime)
    context = Mock()
    context.pages = [Mock()]
    session = Mock()
    session.ensure_authenticated.return_value = context
    monkeypatch.setattr(module, 'SessionManager', lambda *a, **k: session)
    monkeypatch.setattr(module, 'crawl_feed', Mock(side_effect=failure))
    with pytest.raises(type(failure)):
        crawl(config)
    context.close.assert_called_once()
    runtime.__exit__.assert_called_once()
    with resource_lock(config.paths.data_dir / '.dataset.lock'):
        pass
    profile_lock = config.chrome.user_data_dir.with_name(config.chrome.user_data_dir.name + '.lock')
    with resource_lock(profile_lock):
        pass
    reports = list(config.paths.reports_dir.glob('*.json'))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding='utf-8'))
    assert report['status'] == ('interrupted' if isinstance(failure, KeyboardInterrupt) else 'failed')
