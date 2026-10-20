from unittest.mock import Mock

import pytest

from src.config import AppConfig
from src.crawl.service import crawl_feed
from src.errors import AppError
from src.identity import make_post_id
from src.models import CrawlStats, ExtractedPost, ExtractionResult, FeedProgress, RawPost, SessionState
from src.storage import RawPostStore

URL = 'https://www.facebook.com/UITconfess'


def result(text, signature=None):
    return ExtractionResult(signature or text, ExtractedPost(text, post_kind='text'))


class FakeAdapter:
    def __init__(self, screens, interrupt=False):
        self.screens = screens
        self.index = 0
        self.interrupt = interrupt

    def check_session(self, page):
        return SessionState.AUTHENTICATED

    def extract_visible_posts(self, page, *, handled_ids=None):
        yield from self.screens[min(self.index, len(self.screens) - 1)]

    def scroll_and_wait(self, page):
        if self.interrupt:
            raise KeyboardInterrupt
        self.index += 1
        return FeedProgress(frozenset(r.signature for r in self.screens[min(self.index, len(self.screens) - 1)]))


def test_resume_crosses_duplicate_only_screens(tmp_path):
    config = AppConfig()
    config.crawl.max_posts = 1
    config.crawl.no_progress_rounds = 1
    adapter = FakeAdapter([[result('#1 Old')], [result('#2 Old')], [result('#3 New')]])
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', URL) as store:
        for text in ['#1 Old', '#2 Old']:
            store.append(RawPost(id=make_post_id(text), content=text, crawled_at='2026-10-19T00:00:00Z'))
        reason, code = crawl_feed(None, adapter, store, config, stats, None)
        assert len(store.existing) == 3
    assert (reason, code) == ('max_posts', 0)
    assert stats.saved_posts == 1
    assert stats.duplicate_posts == 2
    assert stats.seen_posts == 3
    assert stats.scrolls == 2
    assert stats.candidate_segments == 1


def test_no_progress_is_consecutive(tmp_path):
    config = AppConfig()
    config.crawl.no_progress_rounds = 2
    adapter = FakeAdapter([[result('#1 A')], [result('#1 A')], [result('#2 B')]])
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', URL) as store:
        assert crawl_feed(None, adapter, store, config, stats, None) == ('no_progress', 0)
    assert stats.scrolls == 4
    assert stats.saved_posts == 2


def test_max_scrolls_and_retry_incomplete_content(tmp_path):
    config = AppConfig()
    config.crawl.max_scrolls = 1
    adapter = FakeAdapter([[ExtractionResult('post', error_code='INCOMPLETE_CONTENT')], [result('#1 Full', 'post')]])
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', URL) as store:
        assert crawl_feed(None, adapter, store, config, stats, None) == ('max_scrolls', 0)
    assert stats.saved_posts == 1
    assert stats.extraction_errors == 1


def test_unresolved_extraction_is_partial(tmp_path):
    config = AppConfig()
    config.crawl.max_scrolls = 1
    adapter = FakeAdapter([[ExtractionResult('post', error_code='INCOMPLETE_CONTENT')]])
    with RawPostStore(tmp_path / 'raw.csv', URL) as store:
        assert crawl_feed(None, adapter, store, config, CrawlStats(), None) == ('max_scrolls', 3)


def test_session_loss_returns_partial_without_relogin(tmp_path):
    adapter = FakeAdapter([[result('#1 First')], [result('#2 Second')]])
    adapter.check_session = Mock(side_effect=[SessionState.AUTHENTICATED, SessionState.CHECKPOINT])
    with RawPostStore(tmp_path / 'raw.csv', URL) as store:
        reason, code = crawl_feed(None, adapter, store, AppConfig(), CrawlStats(), None)
        assert len(store.existing) == 1
    assert (reason, code) == ('session_checkpoint', 3)


def test_storage_error_stops_and_does_not_announce_saved():
    store = Mock()
    store.append.side_effect = AppError('STORAGE_ERROR', 'disk full')
    stats = CrawlStats()
    progress = Mock()
    adapter = FakeAdapter([[result('#1 First'), result('#2 Second')]])
    with pytest.raises(AppError):
        crawl_feed(None, adapter, store, AppConfig(), stats, progress)
    assert stats.saved_posts == 0
    assert store.append.call_count == 1
    assert adapter.index == 0


def test_edited_post_same_permalink_is_saved(tmp_path):
    config = AppConfig()
    config.crawl.max_posts = 2
    adapter = FakeAdapter([[result('#1 First', 'same_url')], [result('#1 Edited', 'same_url')]])
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', URL) as store:
        assert crawl_feed(None, adapter, store, config, stats, None) == ('max_posts', 0)
        assert len(store.existing) == 2
    assert stats.seen_posts == stats.saved_posts == 2


def test_round_timings_separate_extraction_storage_scroll_and_progress(monkeypatch, caplog):
    from src.crawl import service

    clock = [0.0]
    monkeypatch.setattr(service.time, 'perf_counter', lambda: clock[0])

    class TimedAdapter(FakeAdapter):
        def check_session(self, page):
            clock[0] += 0.01
            return super().check_session(page)

        def extract_visible_posts(self, page, *, handled_ids=None):
            clock[0] += 0.02
            yield from super().extract_visible_posts(page, handled_ids=handled_ids)
            clock[0] += 0.03

        def scroll_and_wait(self, page):
            clock[0] += 0.05
            return super().scroll_and_wait(page)

    def append(post):
        clock[0] += 0.04
        return True

    def progress(stats):
        clock[0] += 0.07

    config = AppConfig()
    config.crawl.max_scrolls = 1
    store = Mock(append=Mock(side_effect=append))
    with caplog.at_level('INFO', logger='crawler'):
        assert crawl_feed(None, TimedAdapter([[result('#1 First')]]), store, config,
                          CrawlStats(), progress) == ('max_scrolls', 0)
    records = [r for r in caplog.records if getattr(r, 'event', None) == 'crawl_timing']
    assert len(records) == 2
    assert records[0].session_ms == pytest.approx(10)
    assert records[0].extraction_ms == pytest.approx(50)
    assert records[0].storage_ms == pytest.approx(40)
    assert records[0].scroll_ms == pytest.approx(50)
    assert records[0].duration_ms == pytest.approx(290)
    assert records[1].extraction_ms == pytest.approx(50)
    assert records[1].storage_ms == records[1].scroll_ms == 0


def test_round_timings_are_emitted_on_early_stop_and_storage_failure(caplog):
    config = AppConfig()
    config.crawl.max_posts = 1
    adapter = FakeAdapter([[result('#1 First')]])
    with caplog.at_level('INFO', logger='crawler'):
        assert crawl_feed(None, adapter, Mock(append=Mock(return_value=True)), config,
                          CrawlStats(), None) == ('max_posts', 0)
        with pytest.raises(AppError):
            crawl_feed(None, adapter, Mock(append=Mock(side_effect=AppError('STORAGE_ERROR', 'disk full'))),
                       config, CrawlStats(), None)
    records = [r for r in caplog.records if getattr(r, 'event', None) == 'crawl_timing']
    assert len(records) == 2
    assert all(r.duration_ms >= r.storage_ms >= 0 for r in records)
