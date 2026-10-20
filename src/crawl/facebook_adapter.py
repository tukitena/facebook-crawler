"""Conservative DOM boundary for Facebook. Selectors require live-page smoke tests.

Unknown layouts fail visibly; never fall back to extracting the whole page/article.
"""

import hashlib
import re
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeout

from src.config import AppConfig
from src.errors import AppError
from src.identity import make_post_id, normalize_display
from src.models import ExtractedPost, ExtractionResult, FeedProgress, PostKind, SessionState

ADAPTER_VERSION = 'facebook_dom_v5'
MESSAGE = '[data-ad-preview="message"], [data-ad-rendering-role="story_message"], [data-testid="post_message"]'
HEADER = '[data-ad-rendering-role="story_header"], h2, h3'
POSITIONED_POST = '[aria-posinset]:has([data-virtualized])'
POST_BOUNDARY = f'[role="article"], {POSITIONED_POST}'
ARTICLE = f'[role="feed"] [role="article"], [role="main"] {POSITIONED_POST}:has({HEADER}, {MESSAGE})'
MORE = {'see more', 'xem thêm'}
EXPANSION_STATE = 'facebook-crawler.pending-expansion'
COMPLETED_STATE = 'facebook-crawler.completed-post'
ACCOUNT = re.compile(r'^(Your profile|Your account|Account|Trang cá nhân của bạn|Tài khoản của bạn|Tài khoản)$', re.I)

IN_VIEWPORT = '''e => {
    const r = e.getBoundingClientRect();
    if (!e.isConnected || r.width <= 0 || r.height <= 0 || r.bottom <= 0 ||
        r.top >= innerHeight || r.right <= 0 || r.left >= innerWidth) return false;
    const visibility = getComputedStyle(e).visibility;
    return visibility !== 'hidden' && visibility !== 'collapse';
}'''
# Discover candidates once, then let the browser track viewport intersections.
# Only intersecting nodes need geometry/ownership checks on subsequent reads.
VIEWPORT_ARTICLES = '''async args => {
    const key = Symbol.for('facebook-crawler.viewport-index-v5');
    let state = document[key];
    if (!state) {
        const selector = '[role="article"], [aria-posinset]';
        const candidates = new Set(), visible = new Set();
        const intersections = entries => {
            for (const entry of entries) {
                if (entry.isIntersecting && candidates.has(entry.target)) visible.add(entry.target);
                else visible.delete(entry.target);
            }
        };
        const viewport = new IntersectionObserver(intersections);
        const add = node => {
            if (!node.isConnected || candidates.has(node)) return;
            candidates.add(node);
            viewport.observe(node);
        };
        const remove = node => {
            candidates.delete(node);
            visible.delete(node);
            viewport.unobserve(node);
        };
        const collect = (node, add) => {
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            if (node.matches(selector)) add(node);
            node.querySelectorAll(selector).forEach(add);
        };
        const update = records => {
            for (const record of records) {
                if (record.type === 'attributes') {
                    if (record.target.matches(selector) && record.target.isConnected) add(record.target);
                    else remove(record.target);
                } else {
                    record.removedNodes.forEach(node => collect(node, remove));
                    record.addedNodes.forEach(node => collect(node, add));
                }
            }
        };
        const observer = new MutationObserver(update);
        observer.observe(document, {subtree: true, childList: true, attributes: true,
            attributeFilter: ['role', 'aria-posinset']});
        document.querySelectorAll(selector).forEach(add);
        state = document[key] = {candidates, visible, viewport, intersections, observer, update};
    }
    state.update(state.observer.takeRecords());
    if (args.visible !== false) {
        // A fresh observer's initial delivery follows an intersection update.
        // takeRecords alone cannot refresh intersections after a scroll/resize.
        await new Promise((resolve, reject) => {
            const probe = new IntersectionObserver(() => {
                clearTimeout(timer);
                probe.disconnect();
                state.intersections(state.viewport.takeRecords());
                resolve();
            });
            const timer = setTimeout(() => {
                probe.disconnect();
                reject(new Error('Viewport observation timed out'));
            }, 2000);
            probe.observe(document.documentElement);
        });
    }
    const visible = [];
    for (const node of args.visible === false ? state.candidates : state.visible) {
        if (!node.isConnected) continue;
        if (args.visible !== false && !(''' + IN_VIEWPORT + ''')(node)) continue;
        if (node.matches(args.selector) && !node.parentElement.closest(args.boundary)) {
            if (args.first) return [node];
            visible.push(node);
        }
    }
    return visible.sort((a, b) => a === b ? 0 :
        a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
}'''
ARTICLE_ARGS = {'selector': ARTICLE, 'boundary': POST_BOUNDARY, 'header': HEADER, 'body': MESSAGE}

# One ownership rule for both extraction and the final atomic text read. A
# message may carry a chain of nested markers; branching markers remain ambiguous.
OWNED_NODES = '''(article, args) => {
    const nodes = Array.from(article.querySelectorAll(args.selector)).filter(
        e => e.closest(args.boundary) === article && !e.closest('blockquote'));
    if (!args.message) return nodes;
    const leaves = nodes.filter(e => !nodes.some(other => other !== e && e.contains(other)));
    if (leaves.length > 1) return leaves;
    return nodes.filter(e => !nodes.some(other => other !== e && other.contains(e)));
}'''

POST_FINGERPRINT = '''(article, args) => {
    const owned = (''' + OWNED_NODES + ''');
    const headers = owned(article, {...args, selector: args.header, message: false});
    const messages = owned(article, {...args, selector: args.body, message: true});
    return JSON.stringify([
        headers.map(e => [e.innerText, Array.from(e.querySelectorAll('a[href]'),
            a => a.getAttribute('href'))]), messages.map(e => e.innerText)]);
}'''


def _facebook_url(value: str):
    try:
        url = urlsplit(urljoin('https://www.facebook.com/', value))
        if (url.scheme != 'https' or url.hostname not in {'facebook.com', 'www.facebook.com'}
                or url.username or url.password or url.port not in {None, 443}):
            return None
        return url
    except ValueError:
        return None


def page_identity(value: str) -> str | None:
    url = _facebook_url(value)
    if url is None:
        return None
    path = url.path.strip('/')
    if path == 'profile.php':
        ident = parse_qs(url.query).get('id', [''])[0]
        return f'id:{ident}' if ident.isdigit() else None
    if path and '/' not in path and '.' not in path:
        return path.casefold()
    return None


def post_permalink(value: str) -> str:
    url = _facebook_url(value)
    if url is None:
        return ''
    if re.fullmatch(r'/[^/]+/posts/[^/]+/?', url.path):
        return urlunsplit(('https', 'www.facebook.com', url.path.rstrip('/'), '', ''))
    if url.path in {'/story.php', '/permalink.php'}:
        query = parse_qs(url.query)
        if query.get('story_fbid') and query.get('id'):
            params = {key: query[key][0] for key in ('story_fbid', 'id')}
            return urlunsplit(('https', 'www.facebook.com', url.path, urlencode(params), ''))
    return ''


class FacebookAdapter:
    def __init__(self, config: AppConfig):
        self.config = config
        self.identity = page_identity(config.facebook.page_url)
        if not self.identity:
            raise AppError('CONFIG_ERROR', 'URL page chưa được adapter hỗ trợ; dùng URL tên page hoặc profile.php?id=...', 2)

    def check_session(self, page: Page) -> SessionState:
        url = urlsplit(page.url)
        if any(part in url.path for part in ('/checkpoint', '/two_step_verification', '/challenge')):
            return SessionState.CHECKPOINT
        if page.locator('form[action*="checkpoint"], input[name="approvals_code"]').count():
            return SessionState.CHECKPOINT
        if '/login' in url.path or page.locator('input[name="pass"]:visible').count():
            return SessionState.LOGIN_REQUIRED
        current_identity = page_identity(page.url)
        if not current_identity or current_identity != self.identity:
            return SessionState.UNKNOWN
        headings = page.get_by_role('heading', name=self.config.facebook.page_name, exact=True)
        has_heading = headings.filter(visible=True).evaluate_all(
            '(nodes, boundary) => nodes.some(e => !e.closest(boundary))', POST_BOUNDARY)
        account = page.get_by_role('button', name=ACCOUNT)
        has_account = bool(account.filter(visible=True).count())
        if has_heading and has_account and (page.locator('[role="feed"]').count() or page.evaluate(
                'async args => (await (' + VIEWPORT_ARTICLES + ')(args)).length',
                {**ARTICLE_ARGS, 'visible': False, 'first': True})):
            return SessionState.AUTHENTICATED
        return SessionState.UNKNOWN

    @contextmanager
    def _articles(self, page):
        with ExitStack() as handles:
            nodes = page.evaluate_handle(VIEWPORT_ARTICLES, ARTICLE_ARGS)
            handles.callback(nodes.dispose)
            articles = []
            for handle in nodes.get_properties().values():
                handles.callback(handle.dispose)
                if (element := handle.as_element()) is not None:
                    articles.append(element)
            yield articles

    def _details(self, article):
        data = article.evaluate('''(article, args) => {
            const owned = (''' + OWNED_NODES + ''');
            const header = owned(article, {...args, selector: args.header, message: false})[0];
            const author = header && header.querySelector('a[href]');
            if (!author) return null;
            const messages = owned(article, {...args, selector: args.body, message: true});
            const links = owned(article, {...args, selector: 'a[href]', message: false});
            return {author: author.getAttribute('href'), name: author.innerText,
                header: header.innerText, count: messages.length,
                text: messages.length === 1 ? messages[0].innerText : '',
                links: links.filter(e => !e.closest(args.body)).map(e => e.getAttribute('href'))};
        }''', ARTICLE_ARGS)
        if data is None:
            return None
        identity = page_identity(data['author'] or '')
        name = normalize_display(data['name'])
        action = normalize_display(data['header']).removeprefix(name).strip().casefold()
        kind = PostKind.UNKNOWN
        if re.search(r'updated (?:their|his|her|its) profile picture|đã cập nhật ảnh đại diện', action):
            kind = PostKind.PROFILE_UPDATE
        elif re.search(r'updated (?:their|his|her|its) cover photo|đã cập nhật ảnh bìa', action):
            kind = PostKind.COVER_UPDATE
        elif re.search(r'shared (?:a|an|the)|đã chia sẻ', action):
            kind = PostKind.SHARED
        elif not action:
            kind = PostKind.TEXT
        # Links inside message text are data, not evidence of a source permalink.
        permalinks = [post_permalink(href or '') for href in data['links']]
        permalink = next((value for value in permalinks if value), '')
        # This pre-expansion token only correlates extraction failures with retries.
        signature_text = data['text'] + '\n' + action
        signature = permalink or 'dom_' + hashlib.sha256(signature_text.encode('utf-8')).hexdigest()
        return identity, kind, signature, permalink, data['count']

    def visible_signatures(self, page: Page) -> frozenset[str]:
        """Cheap viewport snapshots for scroll progress, never post identities."""
        snapshots = page.evaluate('''async args => {
            const visible = await (''' + VIEWPORT_ARTICLES + ''')(args);
            const owned = (''' + OWNED_NODES + ''');
            return visible.map(article => {
                const headers = owned(article, {...args, selector: args.header, message: false});
                const header = headers[0];
                const author = header && header.querySelector('a[href]');
                const messages = owned(article, {...args, selector: args.body, message: true});
                return {author: author && author.getAttribute('href'),
                    text: JSON.stringify([header ? header.innerText : '', messages.map(e => e.innerText)])};
            });
        }''', ARTICLE_ARGS)
        return frozenset('viewport_' + hashlib.sha256(item['text'].encode('utf-8')).hexdigest()
                         for item in snapshots if page_identity(item['author'] or '') == self.identity)

    @staticmethod
    def _message_snapshot(body):
        return body.evaluate('''(body, key) => ({
            connected: body.isConnected,
            text: body.innerText,
            pending: body[Symbol.for(key)] || null,
            collapsed: Array.from(body.querySelectorAll('button, [role="button"]')).some(
                e => ['see more', 'xem thêm'].includes(e.innerText.trim().toLowerCase())),
            busy: Boolean(body.closest('[aria-busy="true"]') || body.querySelector('[aria-busy="true"], [role="progressbar"]'))
        })''', EXPANSION_STATE)

    def _expand(self, page, body, control, observation_id) -> str:
        # Removing the control alone is not evidence that the delayed text arrived.
        # Labels are removed only from the comparison; stored content stays intact.
        def comparison(text):
            return ' '.join(re.sub(r'see more|xem thêm', '', text, flags=re.I).split())

        snapshot = self._message_snapshot(body)
        pending = snapshot['pending'] or {'before': comparison(snapshot['text']), 'observation_id': observation_id}
        # Keep this on the node across retries: a vanished button does not prove completion.
        body.evaluate('(body, args) => { body[Symbol.for(args.key)] = args.pending; }',
                      {'key': EXPANSION_STATE, 'pending': pending})
        before = pending['before']
        timeout = self.config.crawl.action_timeout_seconds
        deadline = time.monotonic() + timeout
        quiet_seconds = min(0.25, timeout / 3)
        if control is not None:
            control.click(timeout=timeout * 1000)
        candidate = None
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            snapshot = self._message_snapshot(body)
            if not snapshot['connected']:
                raise PlaywrightTimeout('Message detached during expansion')
            text = normalize_display(snapshot['text'])
            ready = not snapshot['collapsed'] and not snapshot['busy'] and comparison(text) != before
            if not ready or text != candidate:
                candidate = text if ready else None
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= quiet_seconds:
                return text
            page.wait_for_timeout(min(25, max(1, (deadline - time.monotonic()) * 1000)))
        raise PlaywrightTimeout('Expansion did not produce settled full text')

    def extract_visible_posts(self, page: Page, *, handled_ids: set[str] | None = None) -> Iterator[ExtractionResult]:
        with self._articles(page) as articles:
            if not articles:
                if not page.evaluate('async args => (await (' + VIEWPORT_ARTICLES + ')(args)).length',
                                     {**ARTICLE_ARGS, 'visible': False, 'first': True}):
                    yield ExtractionResult('layout:no_articles', error_code='UNSUPPORTED_LAYOUT')
                return
            for article in articles:
                signature = 'layout:unrecognized'
                with ExitStack() as handles:
                    try:
                        # Expanding an earlier post can move the next one below the viewport.
                        if not article.evaluate(IN_VIEWPORT):
                            continue
                        if handled_ids:
                            cached_id = article.evaluate('''(article, args) => {
                                const cached = article[Symbol.for(args.key)];
                                if (!cached) return null;
                                if (cached.fingerprint === (''' + POST_FINGERPRINT + ''')(article, args))
                                    return cached.id;
                                // A changed node must finish a fresh extraction, even if
                                // a failed refresh subsequently restores its old text.
                                delete article[Symbol.for(args.key)];
                                return null;
                            }''', {**ARTICLE_ARGS, 'key': COMPLETED_STATE})
                            if cached_id in handled_ids:
                                continue
                        details = self._details(article)
                        if details is None:
                            yield ExtractionResult(signature, error_code='UNKNOWN_AUTHOR')
                            continue
                        identity, kind, signature, permalink, message_count = details
                        if not identity:
                            yield ExtractionResult(signature, error_code='UNKNOWN_AUTHOR')
                            continue
                        if identity != self.identity:
                            continue
                        if message_count > 1:
                            yield ExtractionResult(signature, error_code='AMBIGUOUS_MESSAGE')
                            continue
                        if not message_count:
                            # An avatar or attachment does not prove a caption is absent.
                            yield ExtractionResult(signature, error_code='UNSUPPORTED_LAYOUT')
                            continue
                        body_handle = article.evaluate_handle('''(article, args) => {
                            const messages = (''' + OWNED_NODES + ''')(article, args);
                            return messages.length === 1 ? messages[0] : null;
                        }''', {'selector': MESSAGE, 'boundary': POST_BOUNDARY, 'message': True})
                        handles.callback(body_handle.dispose)
                        body = body_handle.as_element()
                        if body is None:
                            raise PlaywrightTimeout('Message changed during extraction')
                        control_nodes = body.query_selector_all('button, [role="button"]')
                        for node in control_nodes:
                            handles.callback(node.dispose)
                        controls = [node for node in control_nodes
                                    if node.inner_text().strip().casefold() in MORE]
                        snapshot = self._message_snapshot(body)
                        if snapshot['pending']:
                            signature = snapshot['pending']['observation_id']
                        expanded_text = None
                        if controls or snapshot['pending']:
                            position = page.evaluate('({x: window.scrollX, y: window.scrollY})')
                            try:
                                expanded_text = self._expand(page, body, controls[0] if controls else None, signature)
                            finally:
                                # Playwright may scroll to the control; keep unread posts ahead of the cursor.
                                if not page.is_closed():
                                    page.evaluate('p => window.scrollTo({left: p.x, top: p.y, behavior: "instant"})', position)
                        if not body.evaluate('(e) => e.isConnected'):
                            raise PlaywrightTimeout('Message detached')
                        after = self._details(article)
                        if not after or after[0] != identity or after[3] != permalink or after[4] != 1:
                            raise PlaywrightTimeout('Article changed during extraction')
                        # Revalidate ownership, text and the cache token in one browser task.
                        final = body.evaluate('''(body, args) => {
                            const owned = (''' + OWNED_NODES + ''')(args.article, args);
                            if (!body.isConnected || owned.length !== 1 || owned[0] !== body)
                                throw new Error('Message changed during extraction');
                            return {text: body.innerText,
                                fingerprint: (''' + POST_FINGERPRINT + ''')(args.article, args)};
                        }''', {**ARTICLE_ARGS, 'article': article, 'selector': MESSAGE, 'message': True})
                        text = normalize_display(final['text'])
                        if expanded_text is not None and text != expanded_text:
                            raise PlaywrightTimeout('Expanded message changed before extraction')
                        if expanded_text is not None:
                            body.evaluate('(body, key) => delete body[Symbol.for(key)]', EXPANSION_STATE)
                        if text:
                            post_id = make_post_id(text)
                            article.evaluate('''(article, args) => {
                                if ((''' + POST_FINGERPRINT + ''')(article, args) === args.fingerprint)
                                    article[Symbol.for(args.key)] = {id: args.id, fingerprint: args.fingerprint};
                            }''', {**ARTICLE_ARGS, 'key': COMPLETED_STATE, 'id': post_id,
                                   'fingerprint': final['fingerprint']})
                            yield ExtractionResult(post_id, ExtractedPost(text, permalink, kind),
                                                   observation_id=signature)
                        else:
                            yield ExtractionResult(signature)
                    except PlaywrightError:
                        if page.is_closed():
                            raise
                        yield ExtractionResult(signature, error_code='INCOMPLETE_CONTENT')

    def scroll_and_wait(self, page: Page) -> FeedProgress:
        before = self.visible_signatures(page)
        before_y = page.evaluate('window.scrollY')
        page.evaluate('window.scrollBy(0, Math.max(1, window.innerHeight * 0.85))')
        page.wait_for_timeout(self.config.crawl.scroll_delay_seconds * 1000)
        deadline = time.monotonic() + self.config.crawl.feed_wait_timeout_seconds
        while True:
            signatures = self.visible_signatures(page)
            advanced = page.evaluate('window.scrollY') > before_y
            if advanced or signatures != before or time.monotonic() >= deadline:
                return FeedProgress(signatures, advanced)
            if self.check_session(page) != SessionState.AUTHENTICATED:
                return FeedProgress(signatures, advanced)
            page.wait_for_timeout(min(250, max(1, (deadline - time.monotonic()) * 1000)))
