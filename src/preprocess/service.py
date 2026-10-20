"""Offline preprocessing under one dataset lock, with streaming CSV output."""

from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, replace
from itertools import chain
import logging
import time
import uuid

from src.config import AppConfig
from src.errors import AppError
from src.identity import NORMALIZATION_VERSION, canonical_text, make_confession_id
from src.models import utc_now
from src.preprocess.cleanup import cleanup_tail
from src.preprocess.eligibility import build_filter_policy, evaluate_post, load_overrides
from src.preprocess.models import (
    CONFESSION_FIELDS, FILTER_VERSION, SPLITTER_VERSION, Confession, ExcludedRow,
    PreprocessManifest, PreprocessStats, PreprocessSummary, SourceRow,
)
from src.preprocess.splitter import split_confessions
from src.preprocess.storage import ProcessedWriter, file_sha256, json_sha256, validate_preprocess_paths
from src.storage import atomic_write_json, iter_raw_posts, resource_lock

logger = logging.getLogger('facebook_crawler')


def preprocess(config: AppConfig, *, progress: Callable[[PreprocessStats], None] | None = None,
               run_id: str | None = None) -> PreprocessSummary:
    run_id = run_id or str(uuid.uuid4())
    stats = PreprocessStats()
    summary = PreprocessSummary(run_id, 'failed', 'runtime_error', stats, 1)
    started_at, started = utc_now(), time.monotonic()
    effective = config.preprocess.model_dump(mode='json')
    hashes = {'raw_csv_sha256': None, 'raw_metadata_sha256': None, 'overrides_sha256': None}
    excluded_by_reason = Counter()
    writer = None
    primary_error = None

    # A failure to acquire the lock must not create a report in the locked dataset.
    with resource_lock(config.paths.data_dir / '.dataset.lock'):
        try:
            validate_preprocess_paths(config)
            policy = build_filter_policy(config.preprocess)
            overrides = {}
            if config.preprocess.overrides_csv is not None:
                ids = {post.id for post in iter_raw_posts(config.paths.raw_csv, config.facebook.page_url)}
                overrides = load_overrides(config.preprocess.overrides_csv, ids)

            # Prime the validated reader before hashing so missing input/metadata
            # retains its domain error. The rest remains a stream, even without overrides.
            raw = iter_raw_posts(config.paths.raw_csv, config.facebook.page_url)
            try:
                first = next(raw, None)
                hashes['raw_csv_sha256'] = file_sha256(config.paths.raw_csv)
                hashes['raw_metadata_sha256'] = file_sha256(config.paths.raw_csv.with_suffix('.meta.json'))
                if config.preprocess.overrides_csv is not None:
                    hashes['overrides_sha256'] = file_sha256(config.preprocess.overrides_csv)
                seen_raw: dict[str, str] = {}
                seen_confessions: dict[str, str] = {}
                with ProcessedWriter(config.paths.processed_dir) as writer:
                    def exclude(post_id, start_line, end_line, reason, excerpt):
                        writer.write_excluded(ExcludedRow(post_id=post_id, start_line=start_line, end_line=end_line,
                                                          reason=reason, excerpt=excerpt[:200]))
                        stats.excluded_regions += 1
                        excluded_by_reason[reason] += 1

                    for post in chain(() if first is None else (first,), raw):
                        stats.read_posts += 1
                        canonical = canonical_text(post.content)
                        if post.id in seen_raw:
                            if seen_raw[post.id] != canonical:
                                raise AppError('IDENTITY_CONFLICT', 'Một raw ID có nhiều nội dung khác nhau.', 2)
                            stats.duplicate_posts += 1
                            if progress:
                                progress(replace(stats))
                            continue
                        seen_raw[post.id] = canonical
                        cleaned = cleanup_tail(post.content)
                        stats.ui_controls_removed += sum(r.reason == 'UI_CONTROL' for r in cleaned.removed)
                        stats.footers_removed += sum(r.reason == 'POST_FOOTER' for r in cleaned.removed)
                        decision = evaluate_post(post, cleaned, policy, overrides.get(post.id))
                        if not decision.accepted:
                            stats.excluded_posts += 1
                            exclude(post.id, 1, post.content.count('\n') + 1, decision.reason, post.content)
                        else:
                            stats.accepted_posts += 1
                            result = split_confessions(cleaned, short_content_chars=config.preprocess.short_content_chars)
                            stats.candidate_segments += result.candidate_segments
                            stats.empty_segments += result.empty_segments
                            stats.separators += result.separators
                            for segment in result.segments:
                                identifier = make_confession_id(segment.content)
                                body = canonical_text(segment.content)
                                if identifier in seen_confessions:
                                    if seen_confessions[identifier] != body:
                                        raise AppError('IDENTITY_CONFLICT', 'Một confession ID có nhiều nội dung khác nhau.', 2)
                                    stats.duplicate_confessions += 1
                                else:
                                    writer.write_confession(Confession(id=identifier, content=segment.content,
                                                                      confession_number=segment.marker))
                                    seen_confessions[identifier] = body
                                    stats.unique_confessions += 1
                                writer.write_source(SourceRow(confession_id=identifier, post_id=post.id,
                                    marker=segment.marker, start_line=segment.start_line, end_line=segment.end_line,
                                    source_url=post.source_url, flags=(*decision.flags, *segment.flags)))
                                stats.sources += 1
                            # Stable ties preserve cleanup order (UI before footer).
                            for region in sorted((*cleaned.removed, *result.excluded), key=lambda r: (r.start_line, r.end_line)):
                                exclude(post.id, region.start_line, region.end_line, region.reason, region.excerpt)
                        if progress:
                            progress(replace(stats))
                    outputs = writer.finish()
                    manifest = PreprocessManifest(
                        schema_version=1, generation_id=str(uuid.uuid4()), created_at=utc_now(),
                        page_url=config.facebook.page_url, **hashes, effective_config=effective,
                        effective_config_sha256=json_sha256(effective), normalization_version=NORMALIZATION_VERSION,
                        filter_version=FILTER_VERSION, splitter_version=SPLITTER_VERSION,
                        confession_fields=CONFESSION_FIELDS, outputs=outputs, counts=asdict(stats),
                        excluded_by_reason=dict(excluded_by_reason))
                    writer.publish(manifest, labeled_dir=config.paths.labeled_dir)
                summary.status, summary.stop_reason, summary.exit_code = 'complete', 'completed', 0
            finally:
                raw.close()
        except KeyboardInterrupt as exc:
            primary_error = exc
            summary.status, summary.stop_reason, summary.exit_code = 'interrupted', 'keyboard_interrupt', 130
            raise
        except Exception as exc:
            if isinstance(exc, OSError):
                exc = AppError('STORAGE_ERROR', 'Không đọc/ghi được dữ liệu preprocess; kiểm tra quyền và dung lượng.')
            primary_error = exc
            summary.stop_reason = exc.code if isinstance(exc, AppError) else 'runtime_error'
            summary.exit_code = exc.exit_code if isinstance(exc, AppError) else 1
            raise exc
        finally:
            published = writer.published_manifest if writer is not None else None
            if published is not None:
                summary.published, summary.generation_id = True, published.generation_id
            report = {**asdict(summary), 'command': 'preprocess', 'started_at': started_at, 'finished_at': utc_now(),
                      'duration_ms': round((time.monotonic() - started) * 1000), 'effective_config': effective,
                      **hashes, 'output_hashes': {name: info.sha256 for name, info in published.outputs.items()} if published else {},
                      'excluded_by_reason': dict(excluded_by_reason)}
            try:
                atomic_write_json(config.paths.reports_dir / f'{run_id}.json', report)
            except Exception as exc:
                logger.error('report_failed', extra={'event': 'report_failed', 'error_code': 'REPORT_WRITE'})
                if primary_error is None:
                    raise AppError('STORAGE_ERROR', f'generation {summary.generation_id} đã publish; không ghi được report.') from exc
            finally:
                logger.info('run_finished', extra={'event': 'run_finished', 'status': summary.status,
                                                  'stop_reason': summary.stop_reason, **asdict(stats)})
    return summary
