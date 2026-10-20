from pathlib import Path

import pytest

from src.config import AppConfig
from src.crawl.facebook_adapter import FacebookAdapter
from src.models import SessionState


@pytest.fixture
def page_adapter(page):
    page.set_content((Path(__file__).parents[1] / 'fixtures/facebook_page.html').read_text(encoding='utf-8'))
    config = AppConfig()
    config.facebook.page_name = 'UIT Confession'
    config.crawl.action_timeout_seconds = 0.5
    return FacebookAdapter(config)


def test_page_without_feed_role_or_h1_is_authenticated(page, page_adapter):
    assert page_adapter.check_session(page) == SessionState.AUTHENTICATED


@pytest.mark.parametrize('missing', ['#page-name', 'button[aria-label="Your profile"]'])
def test_page_still_requires_page_title_and_account(page, page_adapter, missing):
    page.locator(missing).evaluate('(e) => e.remove()')
    assert page_adapter.check_session(page) == SessionState.UNKNOWN


def test_page_rejects_wrong_url(page, page_adapter):
    page.evaluate("history.replaceState(null, '', '/otherpage')")
    assert page_adapter.check_session(page) == SessionState.UNKNOWN


def test_positioned_posts_expand_and_exclude_comments_quotes_sidebar_and_skeletons(page, page_adapter):
    results = list(page_adapter.extract_visible_posts(page))
    assert all(result.error_code is None for result in results)
    posts = [result.post for result in results if result.post]
    assert [post.content for post in posts] == [
        '#101 Nội dung đầy đủ\nGiữ dòng thứ hai.', '#102 Bài thứ hai.',
    ]
    assert [post.source_url for post in posts] == [
        'https://www.facebook.com/UITconfess/posts/201',
        'https://www.facebook.com/UITconfess/posts/202',
    ]
    assert not page.evaluate('Boolean(window.commentClicked)')


@pytest.mark.parametrize('container', ['#second [data-virtualized]', '#second [data-ad-rendering-role="story_message"]'])
def test_distinct_sibling_messages_remain_ambiguous(page, page_adapter, container):
    page.locator(container).evaluate('''e => {
        const extra = document.createElement('div');
        extra.setAttribute('data-ad-preview', 'message');
        extra.textContent = '#103 A different message';
        e.append(extra);
    }''')
    results = list(page_adapter.extract_visible_posts(page))
    target = next(result for result in results if result.signature.endswith('/posts/202'))
    assert target.error_code == 'AMBIGUOUS_MESSAGE'
    assert target.post is None


def test_positioned_post_with_unknown_message_layout_is_not_silently_skipped(page, page_adapter):
    page.locator('#second [data-ad-rendering-role="story_message"]').evaluate('''e => {
        e.removeAttribute('data-ad-rendering-role');
        e.firstElementChild.removeAttribute('data-ad-preview');
    }''')
    results = list(page_adapter.extract_visible_posts(page))
    target = next(result for result in results if result.signature.endswith('/posts/202'))
    assert target.error_code == 'UNSUPPORTED_LAYOUT'
    assert target.post is None


def test_replacing_message_during_expansion_cannot_save_stale_text(page, page_adapter):
    page.locator('#first [data-ad-rendering-role="story_message"] [role="button"]').evaluate('''e => {
        e.onclick = () => {
            const body = e.closest('[data-ad-rendering-role="story_message"]');
            body.outerHTML = '<div data-ad-rendering-role="story_message">#101 New body</div>';
        };
    }''')
    results = list(page_adapter.extract_visible_posts(page))
    target = next(result for result in results if result.signature.endswith('/posts/201'))
    assert target.error_code == 'INCOMPLETE_CONTENT'
    assert target.post is None
