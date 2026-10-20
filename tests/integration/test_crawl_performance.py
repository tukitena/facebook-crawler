import pytest
from playwright.sync_api import ElementHandle, Error as PlaywrightError

from src.config import AppConfig
from src.crawl.facebook_adapter import FacebookAdapter
from src.crawl.service import crawl_feed
from src.models import CrawlStats
from src.storage import RawPostStore


def article(text='#1 Full text', *, ident='post', height=1800):
    return (f'<div role="article" id="{ident}" style="height:{height}px">'
            '<h3><a href="/UITconfess">UIT Confessions</a></h3>'
            f'<a class="timestamp" href="/UITconfess/posts/{ident}">Time</a>'
            f'<div data-ad-preview="message">{text}</div></div>')


def setup_feed(page, *posts):
    page.set_viewport_size({'width': 900, 'height': 500})
    page.set_content('<button aria-label="Your profile">Account</button>'
                     '<h1>UIT Confessions</h1><div role="feed">' + ''.join(posts) + '</div>')
    config = AppConfig()
    config.crawl.scroll_delay_seconds = 0.01
    config.crawl.feed_wait_timeout_seconds = 0.01
    config.crawl.action_timeout_seconds = 0.1
    config.crawl.max_scrolls = 2
    return config


def element_handles(page):
    # The browser driver owns remote handles even after Python locals disappear.
    return sum(type(obj).__name__ == 'ElementHandle'
               for obj in page._impl_obj._connection._objects.values())


@pytest.mark.parametrize('text,early_close', [
    ('#1 Full text', False),
    ('#1 Short...<button>See more</button>', False),
    ('#1 Full text', True),
])
def test_extraction_releases_handles_on_success_failure_and_close(page, text, early_close):
    config = setup_feed(page, article(text), article('#2 Next', ident='next', height=100))
    adapter = FacebookAdapter(config)
    before = element_handles(page)
    for _ in range(3):
        results = adapter.extract_visible_posts(page)
        if early_close:
            next(results)
            results.close()
        else:
            list(results)
        assert element_handles(page) == before


def test_overlapping_viewports_do_not_reread_committed_post_metadata(page, tmp_path):
    config = setup_feed(page, article(height=2800))
    config.crawl.max_scrolls = 4
    adapter = FacebookAdapter(config)
    details = adapter._details
    metadata_reads = []

    def count_metadata_reads(*args):
        metadata_reads.append(1)
        return details(*args)

    adapter._details = count_metadata_reads
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        assert crawl_feed(page, adapter, store, config, stats, None) == ('max_scrolls', 0)
    assert stats.saved_posts == stats.seen_posts == 1
    assert len(metadata_reads) == 2


def test_edited_content_on_a_committed_dom_node_is_still_saved(page, tmp_path):
    config = setup_feed(page, article())
    config.crawl.max_posts = 2
    page.evaluate('''() => { window.scrollBy = () => {
        document.querySelector('[data-ad-preview]').textContent = '#1 Edited text';
    }; }''')
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        assert crawl_feed(page, FacebookAdapter(config), store, config, stats, None) == ('max_posts', 0)
        assert set(store.existing.values()) == {'#1 Full text', '#1 Edited text'}
    assert stats.saved_posts == 2


@pytest.mark.parametrize('permalink', [True, False])
def test_failed_refresh_of_committed_post_can_resolve_to_its_original_text(page, tmp_path, permalink):
    config = setup_feed(page, article())
    if not permalink:
        page.locator('.timestamp').evaluate('e => e.remove()')
    page.evaluate('''() => {
        let round = 0;
        window.scrollBy = () => {
            const body = document.querySelector('[data-ad-preview]');
            if (++round === 1)
                body.innerHTML = '#1 Short...<button onclick="this.remove()">See more</button>';
            else body.textContent = '#1 Full text';
        };
    }''')
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        assert crawl_feed(page, FacebookAdapter(config), store, config, stats, None) == ('max_scrolls', 0)
        assert set(store.existing.values()) == {'#1 Full text'}
    assert stats.saved_posts == stats.seen_posts == 1
    assert stats.extraction_errors == 1


@pytest.mark.parametrize('fault', ['ambiguous', 'missing'])
def test_temporary_message_layout_error_does_not_reuse_a_stale_completion(page, tmp_path, fault):
    config = setup_feed(page, article())
    page.evaluate('''fault => {
        let round = 0;
        window.scrollBy = () => {
            const post = document.querySelector('#post');
            const body = post.querySelector('[data-ad-preview], .original-message');
            if (++round === 1) {
                if (fault === 'ambiguous')
                    post.insertAdjacentHTML('beforeend', '<div data-ad-preview="message" class="extra">Ambiguous</div>');
                else { body.className = 'original-message'; body.removeAttribute('data-ad-preview'); }
            } else {
                post.querySelector('.extra')?.remove();
                body.setAttribute('data-ad-preview', 'message');
            }
        };
    }''', fault)
    stats = CrawlStats()
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        assert crawl_feed(page, FacebookAdapter(config), store, config, stats, None) == ('max_scrolls', 0)
    assert stats.saved_posts == stats.seen_posts == 1
    assert stats.extraction_errors == 1


def test_repeated_viewport_reads_reuse_article_discovery(page):
    config = setup_feed(page, article())
    adapter = FacebookAdapter(config)
    list(adapter.extract_visible_posts(page))
    page.evaluate('''() => {
        window.documentScans = 0;
        const original = document.querySelectorAll.bind(document);
        document.querySelectorAll = selector => {
            if (selector.includes('article') || selector.includes('aria-posinset'))
                window.documentScans++;
            return original(selector);
        };
    }''')
    for _ in range(3):
        adapter.visible_signatures(page)
        assert list(adapter.extract_visible_posts(page))[0].post.content == '#1 Full text'
    assert page.evaluate('window.documentScans') == 0


@pytest.mark.parametrize('post_count', [100, 1000])
def test_viewport_geometry_work_does_not_grow_with_old_posts(page, post_count):
    config = setup_feed(page, *(article(f'#{i} Text', ident=f'post-{i}') for i in range(post_count)))
    adapter = FacebookAdapter(config)
    adapter.visible_signatures(page)  # Bootstrap discovery before measuring repeated reads.
    page.evaluate('''() => {
        window.geometryReads = 0;
        const original = Element.prototype.getBoundingClientRect;
        Element.prototype.getBoundingClientRect = function() {
            if (this.matches('[role="article"]')) window.geometryReads++;
            return original.call(this);
        };
    }''')
    for _ in range(2):
        assert len(adapter.visible_signatures(page)) == 1
    assert list(adapter.extract_visible_posts(page))[0].post.content == '#0 Text'
    assert page.evaluate('window.geometryReads') <= 8

    # A jump must observe the new viewport immediately, without processing all old posts.
    page.evaluate('''() => {
        const last = document.querySelector('[role="feed"]').lastElementChild;
        scrollTo(0, last.offsetTop + 20);
        window.geometryReads = 0;
    }''')
    assert list(adapter.extract_visible_posts(page))[0].post.content == f'#{post_count - 1} Text'
    assert page.evaluate('window.geometryReads') <= 4


def test_viewport_tracker_refreshes_after_layout_changes_without_scrolling(page):
    config = setup_feed(page, article('#1 First'), article('#2 Second', ident='second'))
    adapter = FacebookAdapter(config)
    assert list(adapter.extract_visible_posts(page))[0].post.content == '#1 First'
    page.locator('#post').evaluate("e => { e.style.height = '0px'; e.style.overflow = 'hidden'; }")
    assert [r.post.content for r in adapter.extract_visible_posts(page)] == ['#2 Second']
    page.locator('#post').evaluate("e => e.style.height = '1800px'")
    assert [r.post.content for r in adapter.extract_visible_posts(page)] == ['#1 First']


def test_article_discovery_tracks_added_removed_and_repurposed_nodes(page):
    config = setup_feed(page, article())
    adapter = FacebookAdapter(config)
    list(adapter.extract_visible_posts(page))
    page.locator('[role="feed"]').evaluate('(feed, html) => feed.innerHTML = html', article('#2 New'))
    assert list(adapter.extract_visible_posts(page))[0].post.content == '#2 New'
    page.locator('#post').evaluate("e => e.removeAttribute('role')")
    assert not adapter.visible_signatures(page)
    page.locator('#post').evaluate("e => e.setAttribute('role', 'article')")
    assert list(adapter.extract_visible_posts(page))[0].post.content == '#2 New'


def test_virtualized_posts_do_not_retain_detached_dom_nodes(page):
    config = setup_feed(page, article())
    adapter = FacebookAdapter(config)
    cdp = page.context.new_cdp_session(page)
    try:
        list(adapter.extract_visible_posts(page))
        cdp.send('HeapProfiler.collectGarbage')
        before = cdp.send('Memory.getDOMCounters')['nodes']
        for number in range(12):
            page.locator('[role="feed"]').evaluate(
                '(feed, html) => feed.innerHTML = html', article(f'#{number} Recycled text'))
            list(adapter.extract_visible_posts(page))
        cdp.send('HeapProfiler.collectGarbage')
        assert cdp.send('Memory.getDOMCounters')['nodes'] <= before + 10
    finally:
        cdp.detach()


def test_all_control_handles_are_released_if_reading_one_control_fails(page, monkeypatch):
    config = setup_feed(page, article('#1 Text<button>See more</button><button>Other</button>'))
    before = element_handles(page)

    def unavailable_text(self, *args, **kwargs):
        raise PlaywrightError('synthetic detached control')

    monkeypatch.setattr(ElementHandle, 'inner_text', unavailable_text)
    results = list(FacebookAdapter(config).extract_visible_posts(page))
    assert results[0].error_code == 'INCOMPLETE_CONTENT'
    assert element_handles(page) == before


@pytest.mark.parametrize('fail', [False, True])
def test_feed_closes_extraction_on_limit_or_storage_failure(page, tmp_path, fail):
    config = setup_feed(page, article())
    config.crawl.max_posts = 1
    adapter = FacebookAdapter(config)
    before = element_handles(page)
    retained = []
    extract = adapter.extract_visible_posts

    def keep_iterator(*args, **kwargs):
        results = extract(*args, **kwargs)
        retained.append(results)
        return results

    adapter.extract_visible_posts = keep_iterator
    with RawPostStore(tmp_path / 'raw.csv', config.facebook.page_url) as store:
        if fail:
            def failure(post):
                raise RuntimeError('synthetic storage failure')
            store.append = failure
            with pytest.raises(RuntimeError, match='synthetic storage failure'):
                crawl_feed(page, adapter, store, config, CrawlStats(), None)
        else:
            assert crawl_feed(page, adapter, store, config, CrawlStats(), None) == ('max_posts', 0)
    assert element_handles(page) == before
