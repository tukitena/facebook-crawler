from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


@pytest.fixture(scope='session')
def browser():
    with sync_playwright() as pw:
        # A fresh ephemeral context, never the crawler's login profile.
        instance = pw.chromium.launch(channel='chrome', headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    tab = context.new_page()
    tab.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='<html></html>'))
    tab.goto('https://www.facebook.com/UITconfess')
    yield tab
    context.close()


@pytest.fixture
def feed_html():
    return (Path(__file__).parent / 'fixtures/facebook_feed.html').read_text(encoding='utf-8')


@pytest.fixture
def raw_dataset(tmp_path):
    """Seed independent, synthetic datasets without requiring a browser."""
    from itertools import count
    from src.config import load_config
    from src.identity import make_post_id, normalize_display
    from src.models import RawPost
    from src.storage import RawPostStore

    sequence = count()

    def create(texts):
        root = tmp_path / f'dataset-{next(sequence)}'
        root.mkdir()
        config_path = root / 'config.yaml'
        config_path.write_text('{}', encoding='utf-8')
        config = load_config(config_path)
        with RawPostStore(config.paths.raw_csv, config.facebook.page_url) as writer:
            for text in texts:
                content = normalize_display(text)
                writer.append(RawPost(id=make_post_id(content), content=content,
                                      crawled_at='2026-10-20T00:00:00Z', post_kind='text'))
        return config

    return create
