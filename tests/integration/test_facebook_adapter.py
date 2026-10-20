from dataclasses import replace

import pytest

from src.config import AppConfig
from src.errors import AppError
from src.crawl.facebook_adapter import FacebookAdapter
from src.models import SessionState


def adapter():
    config = AppConfig()
    config.crawl.action_timeout_seconds = 0.15
    return FacebookAdapter(config)


def test_only_top_level_messages_and_scoped_expansion(page, feed_html):
    page.set_content(feed_html)
    results = list(adapter().extract_visible_posts(page))
    posts = [result.post for result in results if result.post]
    assert len(posts) == 7
    assert posts[0].content == '#1 Admin ơi, thư viện hỗ trợ tốt.\n#2 Cảm ơn UIT 😊'
    assert posts[1].content == '#3 Nội dung đầy đủ\nGiữ dòng hai.'
    assert posts[0].source_url.endswith('/posts/101')
    assert [p.post_kind.value for p in posts] == ['text', 'text', 'text', 'profile_update', 'cover_update', 'shared', 'unknown']
    assert not page.evaluate('Boolean(window.commentClicked)')
    assert all('#999' not in p.content and '#888' not in p.content and '#777' not in p.content for p in posts)
    assert any(r.post is None and r.error_code is None for r in results)


@pytest.mark.parametrize('onclick', ['', 'this.closest("[role=article]").remove()'])
def test_incomplete_or_detached_post_not_saved(page, feed_html, onclick):
    page.set_content(feed_html)
    page.locator('#expand [role=button]').evaluate('(e, code) => e.setAttribute("onclick", code)', onclick)
    results = list(adapter().extract_visible_posts(page))
    assert any(r.error_code == 'INCOMPLETE_CONTENT' for r in results)
    assert not any(r.post and r.post.content.startswith('#3') for r in results)


@pytest.mark.parametrize('extra,state', [
    ('', SessionState.AUTHENTICATED),
    ('<input name="email"><input name="pass" type="password">', SessionState.LOGIN_REQUIRED),
    ('<form action="/checkpoint/"><input name="approvals_code"></form>', SessionState.CHECKPOINT),
])
def test_session_signals_precedence(page, feed_html, extra, state):
    page.set_content(feed_html + extra)
    assert adapter().check_session(page) == state


def test_wrong_page_or_cookie_alone_not_authenticated(page, feed_html):
    page.set_content(feed_html.replace('Your profile', 'Other button'))
    page.context.add_cookies([{'name': 'c_user', 'value': 'synthetic', 'url': page.url}])
    assert adapter().check_session(page) == SessionState.UNKNOWN
    page.set_content(feed_html.replace('<h1>UIT Confessions</h1>', '<h1>Other page</h1>'))
    assert adapter().check_session(page) == SessionState.UNKNOWN
    page.goto('https://www.facebook.com/otherpage')
    page.set_content(feed_html)
    assert adapter().check_session(page) == SessionState.UNKNOWN


def test_reactions_do_not_change_signatures(page, feed_html):
    page.set_content(feed_html)
    instance = adapter()
    before = instance.visible_signatures(page)
    page.locator('.reactions').inner_text()
    page.locator('.reactions').evaluate('(e) => e.textContent = "99 reactions"')
    assert instance.visible_signatures(page) == before


def test_virtualized_same_height_feed_progresses(page, feed_html):
    page.set_content(feed_html)
    instance = adapter()
    instance.config.crawl.scroll_delay_seconds = 0.01
    instance.config.crawl.feed_wait_timeout_seconds = 0.1
    before = instance.visible_signatures(page)
    page.evaluate('''() => { const scroll = window.scrollBy; window.scrollBy = (...args) => {
        document.querySelector('#first a[href*="posts"]').href='/UITconfess/posts/999'; scroll(...args);
    }; }''')
    assert instance.scroll_and_wait(page).signatures != before


def test_see_more_disappearing_before_text_arrives_does_not_save_prefix(page, feed_html):
    page.set_content(feed_html)
    page.locator('#expand [role=button]').evaluate('''e => e.onclick = function() {
        const body = this.parentElement;
        this.remove();
        setTimeout(() => body.innerHTML = '#3 Nội dung đầy đủ<br>Giữ dòng hai.', 200);
    }''')
    instance = adapter()
    instance.config.crawl.action_timeout_seconds = 1
    results = list(instance.extract_visible_posts(page))
    target = next(r for r in results if r.signature.endswith('/posts/102'))
    assert target.error_code is None
    assert target.post.content == '#3 Nội dung đầy đủ\nGiữ dòng hai.'


def test_unsupported_message_with_avatar_is_not_assumed_media_only(page):
    page.set_content('''<div role="feed"><div role="article">
      <h3><a href="/UITconfess"><img alt="avatar">UIT Confessions</a></h3>
      <div class="new-message-layout">#1 Full text in an unsupported layout</div>
    </div></div>''')
    result = list(adapter().extract_visible_posts(page))[0]
    assert result.post is None
    assert result.error_code == 'UNSUPPORTED_LAYOUT'


def test_unsupported_configured_identity_fails_closed():
    config = AppConfig()
    config.facebook.page_url = 'https://www.facebook.com/pages/UITconfess/123'
    with pytest.raises(AppError) as error:
        FacebookAdapter(config)
    assert error.value.code == 'CONFIG_ERROR'


def test_expansion_control_removed_without_new_text_stays_incomplete(page, feed_html):
    page.set_content(feed_html)
    page.locator('#expand [role=button]').evaluate('e => e.onclick = function() { this.remove(); }')
    results = list(adapter().extract_visible_posts(page))
    target = next(r for r in results if r.signature.endswith('/posts/102'))
    assert target.error_code == 'INCOMPLETE_CONTENT'
    assert target.post is None
