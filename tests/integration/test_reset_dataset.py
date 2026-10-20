import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

from src import cli
from src.config import load_config
from src.crawl.service import crawl_feed
from src.models import CrawlStats, ExtractedPost, ExtractionResult, FeedProgress, SessionState
from src.storage import RawPostStore, resource_lock


def test_reset_refuses_dataset_owned_by_another_process(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('{}', encoding='utf-8')
    config = load_config(path)
    config.paths.raw_csv.parent.mkdir(parents=True)
    config.paths.raw_csv.write_bytes(b'old data')
    command = [sys.executable, str(Path(cli.__file__).resolve().parent.parent / 'main.py'),
               '--config', str(path), 'reset']
    with resource_lock(config.paths.data_dir / '.dataset.lock'):
        result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=10)
    assert result.returncode == 2, result.stderr
    assert 'RESOURCE_LOCKED' in result.stderr
    assert config.paths.raw_csv.read_bytes() == b'old data'
    result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
    assert not config.paths.raw_csv.exists()


def test_crawl_can_save_old_posts_again_after_reset(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text('crawl: {max_posts: 1, max_scrolls: 1}', encoding='utf-8')
    config = load_config(path)
    adapter = Mock()
    adapter.check_session.return_value = SessionState.AUTHENTICATED
    adapter.extract_visible_posts.return_value = [
        ExtractionResult('same', ExtractedPost('#1 Synthetic confession', post_kind='text'))]
    adapter.scroll_and_wait.return_value = FeedProgress(frozenset({'same'}))

    def run_feed():
        stats = CrawlStats()
        with RawPostStore(config.paths.raw_csv, config.facebook.page_url) as store:
            crawl_feed(None, adapter, store, config, stats, None)
        return stats

    assert run_feed().saved_posts == 1
    assert run_feed().duplicate_posts == 1
    assert cli.main(['--config', str(path), 'reset']) == 0
    assert run_feed().saved_posts == 1
    with RawPostStore(config.paths.raw_csv, config.facebook.page_url) as store:
        assert len(store.existing) == 1
