import csv

from src.config import load_config
from src.crawl.facebook_adapter import FacebookAdapter
from src.crawl.service import crawl_feed
from src.models import CrawlStats
from src.storage import RawPostStore


def test_offline_browser_to_csv_and_resume(page, feed_html, tmp_path):
    config_path = tmp_path / 'config.yaml'
    config_path.write_text('crawl: {max_posts: 7, max_scrolls: 1}')
    config = load_config(config_path)
    page.set_content(feed_html)
    adapter = FacebookAdapter(config)
    stats = CrawlStats()
    with RawPostStore(config.paths.raw_csv, config.facebook.page_url) as store:
        assert crawl_feed(page, adapter, store, config, stats, None) == ('max_posts', 0)
    assert stats.saved_posts == 7
    assert stats.candidate_segments == 8
    with config.paths.raw_csv.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 7
    assert rows[1]['content'] == '#3 Nội dung đầy đủ\nGiữ dòng hai.'
    assert rows[3]['post_kind'] == 'profile_update'
    assert all('Comment must not leak' not in row['content'] for row in rows)
    config.crawl.scroll_delay_seconds = 0.01
    config.crawl.feed_wait_timeout_seconds = 0.01
    again = CrawlStats()
    with RawPostStore(config.paths.raw_csv, config.facebook.page_url) as store:
        assert crawl_feed(page, adapter, store, config, again, None) == ('max_scrolls', 0)
        assert len(store.existing) == 7
    assert again.saved_posts == 0
    assert again.duplicate_posts == 7
