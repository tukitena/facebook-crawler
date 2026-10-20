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
