import hashlib

import pytest

from src.config import AppConfig
from src.crawl.facebook_adapter import FacebookAdapter
from src.crawl.service import crawl_feed
from src.models import CrawlStats, SessionState
from src.storage import RawPostStore


def config_for_viewport(page):
    page.set_viewport_size({'width': 900, 'height': 500})
    config = AppConfig()
    config.crawl.action_timeout_seconds = 0.6
    config.crawl.scroll_delay_seconds = 0.01
    config.crawl.feed_wait_timeout_seconds = 0.01
    config.crawl.max_scrolls = 1
    return config


def feed(*articles):
    return ('<button aria-label="Your profile">Account</button><h1>UIT Confessions</h1>'
            '<div role="feed">' + ''.join(articles) + '</div>')


def article(body, *, style='', permalink='', ident='post'):
    link = f'<a href="{permalink}">Time</a>' if permalink else ''
    return (f'<div role="article" id="{ident}" style="{style}">'
            '<h3><a href="/UITconfess">UIT Confessions</a></h3>' + link
            + f'<div data-ad-preview="message">{body}</div></div>')


@pytest.mark.parametrize('permalink', ['', '/UITconfess/posts/101'])
def test_expanded_content_is_the_only_post_key(page, tmp_path, permalink):
    config = config_for_viewport(page)
    page.set_content(feed(article(
        '#1 Short...<button onclick="this.parentElement.innerHTML = \'#1 Complete text\'">See more</button>',
        style='height: 1800px', permalink=permalink)))
    adapter = FacebookAdapter(config)
    first = list(adapter.extract_visible_posts(page))[0]
    expected = 'post_' + hashlib.sha256(b'post:v1\n#1 Complete text').hexdigest()
    assert first.signature == expected
    assert first.post.content == '#1 Complete text'
    assert list(adapter.extract_visible_posts(page))[0].signature == expected

    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        assert crawl_feed(page, adapter, store, config, stats, None) == ('max_scrolls', 0)
        assert set(store.existing) == {expected}
    assert (stats.seen_posts, stats.saved_posts, stats.duplicate_posts) == (1, 1, 0)


def test_expansion_does_not_double_seen_count_in_real_crawl(page, tmp_path):
    config = config_for_viewport(page)
    page.set_content(feed(article(
        '#1 Short...<button onclick="this.parentElement.innerHTML = \'#1 Complete text\'">See more</button>',
        style='height: 1800px')))
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        crawl_feed(page, FacebookAdapter(config), store, config, stats, None)
    assert (stats.seen_posts, stats.saved_posts, stats.duplicate_posts) == (1, 1, 0)


def test_content_identity_ignores_permalink_and_whitespace(page):
    config = config_for_viewport(page)
    page.set_content(feed(
        article('#1 Same text', permalink='/UITconfess/posts/101'),
        article('#1 Same<br>text', permalink='/UITconfess/posts/202', ident='second')))
    results = list(FacebookAdapter(config).extract_visible_posts(page))
    assert len(results) == 2
    assert results[0].signature == results[1].signature
    page.locator('#second [data-ad-preview]').evaluate("e => e.textContent = '#1 Edited text'")
    changed = list(FacebookAdapter(config).extract_visible_posts(page))
    assert changed[0].signature != changed[1].signature


def test_offscreen_posts_are_not_read_or_expanded(page):
    config = config_for_viewport(page)
    page.set_content(feed(
        article('#1 Above', style='position:absolute;top:-600px', ident='above'),
        article('#2 Visible', style='position:absolute;top:100px', ident='visible'),
        article('#3 Below <button onclick="window.offscreenClicked=true">See more</button>',
                style='position:absolute;top:1600px', ident='below'),
        article('#4 Hidden', style='visibility:hidden;position:absolute;top:200px', ident='hidden'),
        article('#5 Side', style='position:absolute;left:2000px;top:200px', ident='side')))
    page.evaluate('''() => {
        window.textReads = {};
        const read = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'innerText').get;
        for (const body of document.querySelectorAll('[data-ad-preview]')) {
            Object.defineProperty(body, 'innerText', {get() {
                const id = this.parentElement.id;
                window.textReads[id] = (window.textReads[id] || 0) + 1;
                return read.call(this);
            }});
        }
    }''')
    adapter = FacebookAdapter(config)
    results = list(adapter.extract_visible_posts(page))
    adapter.visible_signatures(page)
    assert [r.post.content for r in results if r.post] == ['#2 Visible']
    assert set(page.evaluate('window.textReads')) == {'visible'}
    assert not page.evaluate('Boolean(window.offscreenClicked)')


def test_expansion_rechecks_viewport_before_processing_next_post(page):
    config = config_for_viewport(page)
    page.set_content(feed(
        article('#1 Short...<button onclick="this.parentElement.innerHTML = '
                '\'<div style=height:1800px>#1 Complete text</div>\'">See more</button>'),
        article('#2 Next', ident='second')))
    adapter = FacebookAdapter(config)
    results = list(adapter.extract_visible_posts(page))
    assert [r.post.content for r in results if r.post] == ['#1 Complete text']
    page.locator('#second').scroll_into_view_if_needed()
    results = list(adapter.extract_visible_posts(page))
    assert any(r.post and r.post.content == '#2 Next' for r in results)


def test_empty_viewport_is_not_an_unsupported_layout_or_lost_session(page):
    config = config_for_viewport(page)
    page.set_content('''<button aria-label="Your profile">Account</button><h1>UIT Confessions</h1>
        <main role="main"><div aria-posinset="1" style="position:absolute;top:1600px">
        <div data-virtualized="false"><h3><a href="/UITconfess">UIT Confessions</a></h3>
        <div data-ad-preview="message">#1 Later</div></div></div></main>''')
    adapter = FacebookAdapter(config)
    assert adapter.check_session(page) == SessionState.AUTHENTICATED
    assert list(adapter.extract_visible_posts(page)) == []
    assert not adapter.visible_signatures(page)


def test_crawl_crosses_long_posts_without_stopping_for_no_progress(page, tmp_path):
    config = config_for_viewport(page)
    config.crawl.max_posts = 2
    config.crawl.max_scrolls = 20
    config.crawl.no_progress_rounds = 1
    page.set_content(feed(article('#1 Long', style='height: 2800px'), article('#2 Next', ident='second')))
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        assert crawl_feed(page, FacebookAdapter(config), store, config, stats, None) == ('max_posts', 0)
    assert stats.scrolls >= 5
    assert (stats.seen_posts, stats.saved_posts) == (2, 2)


def test_failed_expansion_is_not_counted_as_a_seen_content_key(page, tmp_path):
    config = config_for_viewport(page)
    page.set_content(feed(article('#1 Short...<button>See more</button>', style='height:1800px')))
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        assert crawl_feed(page, FacebookAdapter(config), store, config, stats, None) == ('max_scrolls', 3)
        assert not store.existing
    assert (stats.seen_posts, stats.saved_posts) == (0, 0)
    assert stats.extraction_errors > 0


def test_retry_resolves_error_when_full_content_has_a_different_key(page, tmp_path):
    config = config_for_viewport(page)
    page.set_content(feed(article(
        '#1 Short...<button onclick="window.attempts = (window.attempts || 0) + 1; '
        'if (window.attempts > 1) this.parentElement.innerHTML = \'#1 Complete text\'">See more</button>',
        style='height:1800px')))
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        assert crawl_feed(page, FacebookAdapter(config), store, config, stats, None) == ('max_scrolls', 0)
    assert (stats.seen_posts, stats.saved_posts, stats.extraction_errors) == (1, 1, 1)


def test_removed_expansion_control_cannot_turn_failed_prefix_into_a_saved_post(page, tmp_path):
    config = config_for_viewport(page)
    page.set_content(feed(article(
        '#1 Short...<button onclick="this.remove()">See more</button>', style='height:1800px')))
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        assert crawl_feed(page, FacebookAdapter(config), store, config, stats, None) == ('max_scrolls', 3)
        assert not store.existing
    assert stats.seen_posts == stats.saved_posts == 0


def test_text_arriving_after_expansion_timeout_can_resolve_the_original_error(page, tmp_path):
    config = config_for_viewport(page)
    config.crawl.action_timeout_seconds = 0.25
    page.set_content(feed(article(
        '#1 Short...<button onclick="const body = this.parentElement; this.remove(); '
        'setTimeout(() => body.innerHTML = \'#1 Complete text\', 350)">See more</button>',
        style='height:1800px')))
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        assert crawl_feed(page, FacebookAdapter(config), store, config, stats, None) == ('max_scrolls', 0)
        assert set(store.existing.values()) == {'#1 Complete text'}
    assert (stats.seen_posts, stats.saved_posts, stats.extraction_errors) == (1, 1, 1)


def test_clicking_more_below_viewport_does_not_skip_the_following_short_post(page, tmp_path):
    config = config_for_viewport(page)
    config.crawl.max_posts = 2
    config.crawl.max_scrolls = 10
    config.crawl.no_progress_rounds = 1
    page.set_content(feed(
        article('<div style="height:1760px">#1 Short...</div>'
                '<button onclick="this.parentElement.innerHTML = '
                '\'<div style=height:1780px>#1 Complete text</div>\'">See more</button>'),
        article('#2 Short post', ident='second')) + '<div style="height:2000px"></div>')
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        assert crawl_feed(page, FacebookAdapter(config), store, config, stats, None) == ('max_posts', 0)
        assert set(store.existing.values()) == {'#1 Complete text', '#2 Short post'}
