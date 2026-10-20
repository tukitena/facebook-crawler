"""Run ownership and feed orchestration; records are committed before scrolling."""

import logging
import re
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import asdict

from playwright.sync_api import Page, sync_playwright

from src.config import AppConfig
from src.crawl.facebook_adapter import ADAPTER_VERSION, FacebookAdapter
from src.crawl.session import SessionManager
from src.errors import AppError
from src.identity import make_post_id, normalize_display
from src.models import CrawlStats, CrawlSummary, RawPost, SessionState, utc_now
from src.storage import RawPostStore, atomic_write_json, defer_sigint, resource_lock

logger = logging.getLogger('crawler')
MARKER = re.compile(r'^[ \t]*#[0-9]+(?=$|[ \t]|[:.\-–—])', re.MULTILINE)


@contextmanager
def _timed_phase(timings, phase):
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[phase] += (time.perf_counter() - started) * 1000


@contextmanager
def _round_timings(stats):
    started = time.perf_counter()
    round_index = stats.scrolls + 1
    timings = dict.fromkeys(('session_ms', 'extraction_ms', 'storage_ms', 'scroll_ms'), 0.0)
    try:
        yield timings
    finally:
        logger.info('crawl_timing', extra={
            'event': 'crawl_timing', 'round_index': round_index,
            'duration_ms': round((time.perf_counter() - started) * 1000, 3),
            **{key: round(value, 3) for key, value in timings.items()},
            'saved_posts': stats.saved_posts, 'scrolls': stats.scrolls,
            'extraction_errors': stats.extraction_errors,
        })


def _timed_results(results, timings):
    """Measure active extraction, excluding time suspended for storage/progress."""
    try:
        while True:
            with _timed_phase(timings, 'extraction_ms'):
                try:
                    result = next(results)
                except StopIteration:
                    return
            yield result
    finally:
        if close := getattr(results, 'close', None):
            with _timed_phase(timings, 'extraction_ms'):
                close()


def crawl_feed(page: Page, adapter: FacebookAdapter, store: RawPostStore, config: AppConfig,
               stats: CrawlStats, progress: Callable[[CrawlStats], None] | None) -> tuple[str, int]:
    observed: set[str] = set()
    handled_ids: set[str] = set()
    media_seen: set[str] = set()
    unresolved: set[str] = set()
    pending_signatures: frozenset[str] = frozenset()
    viewport_advanced = False
    while True:
        with _round_timings(stats) as timings:
            with _timed_phase(timings, 'session_ms'):
                state = adapter.check_session(page)
            if state != SessionState.AUTHENTICATED:
                return 'session_' + state.value.casefold(), 3
            new_signatures = set(pending_signatures) - observed
            with ExitStack() as extraction:
                with _timed_phase(timings, 'extraction_ms'):
                    results = iter(adapter.extract_visible_posts(page, handled_ids=handled_ids))
                results = _timed_results(results, timings)
                # Close a suspended generator even on max_posts, storage failure or SIGINT.
                if close := getattr(results, 'close', None):
                    extraction.callback(close)
                for result in results:
                    if result.signature not in observed:
                        new_signatures.add(result.signature)
                        observed.add(result.signature)
                    observation_id = result.observation_id or result.signature
                    if result.error_code:
                        unresolved.add(observation_id)
                        stats.extraction_errors += 1
                        logger.warning('extraction_failed', extra={'event': 'extraction_failed', 'error_code': result.error_code,
                                                                  'extraction_errors': stats.extraction_errors})
                        continue
                    unresolved.discard(observation_id)
                    if result.post is None:
                        if result.signature not in media_seen:
                            media_seen.add(result.signature)
                            stats.media_only_posts += 1
                            logger.info('post_skipped', extra={'event': 'post_skipped', 'error_code': 'MEDIA_ONLY'})
                        continue
                    content = normalize_display(result.post.content)
                    post_id = make_post_id(content)
                    if post_id in handled_ids:
                        continue
                    stats.seen_posts += 1
                    new_signatures.add(post_id)
                    post = RawPost(id=post_id, content=content, crawled_at=utc_now(),
                                   source_url=result.post.source_url, post_kind=result.post.post_kind)
                    # Defer SIGINT through both durable append and the matching counters.
                    with defer_sigint():
                        with _timed_phase(timings, 'storage_ms'):
                            saved = store.append(post)
                        handled_ids.add(post_id)
                        if saved:
                            stats.saved_posts += 1
                            stats.candidate_segments += len(MARKER.findall(content))
                        else:
                            stats.duplicate_posts += 1
                    event = 'post_saved' if saved else 'post_skipped'
                    logger.info(event, extra={'event': event, 'record_id': post_id, 'saved_posts': stats.saved_posts})
                    if progress:
                        progress(stats)
                    if stats.saved_posts >= config.crawl.max_posts:
                        return 'max_posts', 3 if unresolved else 0
            observed.update(pending_signatures)
            if stats.scrolls:
                stats.no_progress_rounds = 0 if new_signatures or viewport_advanced else stats.no_progress_rounds + 1
            if progress:
                progress(stats)
            if stats.no_progress_rounds >= config.crawl.no_progress_rounds:
                return 'no_progress', 3 if unresolved else 0
            if stats.scrolls >= config.crawl.max_scrolls:
                return 'max_scrolls', 3 if unresolved else 0
            with _timed_phase(timings, 'scroll_ms'):
                feed_progress = adapter.scroll_and_wait(page)
            pending_signatures = feed_progress.signatures
            viewport_advanced = feed_progress.advanced
            stats.scrolls += 1


def crawl(config: AppConfig, *, progress: Callable[[CrawlStats], None] | None = None,
          run_id: str | None = None) -> CrawlSummary:
    run_id = run_id or str(uuid.uuid4())
    stats = CrawlStats()
    summary = CrawlSummary(run_id, 'failed', 'runtime_error', stats, 1)
    started_at = utc_now()
    started = time.monotonic()
    report_allowed = False
    logger.info('run_started', extra={'event': 'run_started'})
    try:
        with resource_lock(config.paths.data_dir / '.dataset.lock'):
            report_allowed = True
            profile = config.chrome.user_data_dir
            with resource_lock(profile.with_name(profile.name + '.lock')):
                with RawPostStore(config.paths.raw_csv, config.facebook.page_url) as store:
                    adapter = FacebookAdapter(config)
                    with sync_playwright() as playwright:
                        context = SessionManager(config, playwright.chromium, adapter).ensure_authenticated()
                        try:
                            reason, code = crawl_feed(context.pages[0], adapter, store, config, stats, progress)
                            summary.stop_reason = reason
                            summary.exit_code = code
                            summary.status = 'partial' if code else 'complete'
                        finally:
                            active_error = sys.exc_info()[0] is not None
                            try:
                                context.close()
                            except Exception:
                                if not active_error:
                                    raise
                                logger.error('cleanup_failed', extra={'event': 'cleanup_failed', 'error_code': 'BROWSER_CLOSE'})
        return summary
    except KeyboardInterrupt:
        summary.status, summary.stop_reason, summary.exit_code = 'interrupted', 'keyboard_interrupt', 130
        raise
    except Exception as exc:
        summary.status = 'failed'
        summary.stop_reason = exc.code if isinstance(exc, AppError) else 'runtime_error'
        summary.exit_code = exc.exit_code if isinstance(exc, AppError) else 1
        raise
    finally:
        report = {**asdict(summary), 'started_at': started_at, 'finished_at': utc_now(),
                  'duration_ms': int((time.monotonic() - started) * 1000), 'command': 'crawl',
                  'adapter_version': ADAPTER_VERSION,
                  'effective_config': {
                      'page_url': config.facebook.page_url, 'crawl': config.crawl.model_dump(mode='json'),
                      'headless': config.chrome.headless, 'schema_version': config.schema_version,
                  }}
        active_error = sys.exc_info()[0] is not None
        if report_allowed:
            try:
                atomic_write_json(config.paths.reports_dir / f'{run_id}.json', report)
            except (OSError, AppError):
                if not active_error:
                    raise
                logger.error('report_failed', extra={'event': 'report_failed', 'error_code': 'REPORT_WRITE'})
        logger.info('run_finished', extra={'event': 'run_finished', 'status': summary.status,
                                          'stop_reason': summary.stop_reason, **asdict(stats)})
