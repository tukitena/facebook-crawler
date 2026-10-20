"""Sequential, resumable labeling under the dataset's exclusive lock."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, replace
import logging
from pathlib import Path
import time
import uuid

from src.classify.client import ModelAttemptError, OllamaClient
from src.classify.fingerprint import build_metadata
from src.classify.models import ClassificationStats, ClassificationSummary, FailureRecord, LabeledRecord
from src.classify.policy import apply_review_policy
from src.classify.prompt import build_messages, load_prompt
from src.classify.storage import LabelStore, load_input, validate_classify_paths
from src.config import AppConfig, validate_ollama_base_url
from src.errors import AppError
from src.models import utc_now
from src.storage import atomic_write_json, defer_sigint, resource_lock

logger = logging.getLogger('crawler')


def _run_id(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError('Not a canonical UUID')
    except (ValueError, AttributeError):
        raise AppError('CONFIG_ERROR', 'Classification run_id must be a canonical UUID.', 2) from None
    return value


def _predict(client, bundle, content, config, identifier, sleep):
    """Return prediction/error, requests made, and exhausted-service status."""
    attempts, repair, all_service_errors = 0, False, True
    while attempts < config.classification.max_attempts:
        try:
            # Check before counting the request, including the larger repair message.
            build_messages(bundle, content, config, repair=repair)
        except AppError as error:
            if error.code == 'INPUT_TOO_LONG':
                return None, error, attempts, False
            raise
        attempts += 1
        try:
            return client.classify_once(content, repair=repair), None, attempts, False
        except ModelAttemptError as error:
            all_service_errors = all_service_errors and error.service_failure
            repair = repair or not error.service_failure
            if not error.retryable or attempts == config.classification.max_attempts:
                return None, error, attempts, all_service_errors and error.retryable
            logger.info('classification_retry', extra={'event': 'classification_retry',
                'record_id': identifier, 'attempt': attempts, 'error_code': error.code})
            sleep(error.retry_after if error.retry_after is not None else attempts)
        except AppError as error:
            return None, error, attempts, False


def _interrupted(summary):
    summary.status, summary.stop_reason, summary.exit_code = 'interrupted', 'keyboard_interrupt', 130


def classify(config: AppConfig, limit: int | None = None, *,
             progress: Callable[[ClassificationStats], None] | None = None,
             run_id: str | None = None, client_factory=OllamaClient,
             sleep=time.sleep, clock=time.monotonic,
             config_path: Path | None = None) -> ClassificationSummary:
    """Return complete/partial/interrupted summaries; raise safe operational errors.

    Preflight never counts as a dataset attempt. A label counts as saved only
    after fsync. Failed IDs remain pending and can be retried by a later run.
    """
    run_id = _run_id(run_id)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise AppError('CONFIG_ERROR', 'Classification limit must be a positive integer.', 2)
    validate_ollama_base_url(config.ollama.base_url)
    validate_classify_paths(config, config_path)
    stats = ClassificationStats()
    summary = ClassificationSummary(run_id, stats, 'failed', 'runtime_error', 1)
    started_at, started = utc_now(), clock()
    snapshot = identity = metadata = store = None
    store_ready, reviewed_before = False, 0
    primary_error = None

    # No report or model activity when ownership or the path recheck fails.
    with resource_lock(config.paths.data_dir / '.dataset.lock'):
        validate_classify_paths(config, config_path)
        try:
            snapshot = load_input(config.paths.processed_dir)
            stats.total = stats.remaining = len(snapshot.confessions)
            bundle = load_prompt(config)
            with client_factory(config, bundle) as client:
                identity = client.preflight()
                metadata = build_metadata(config, snapshot, identity, bundle)
                store = LabelStore(config.paths.labeled_dir, metadata, snapshot)
                with ExitStack() as stack:
                    # Entry can atomically reconcile stale failures. Register
                    # cleanup and initialize counters before delivering SIGINT.
                    with defer_sigint():
                        stack.enter_context(store)
                        stats.already_done = len(store.completed)
                        stats.reviewed_total = reviewed_before = store.reviewed_total
                        stats.remaining = stats.total - stats.already_done
                        store_ready = True
                    store.reconcile_failures()
                    service_streak = 0
                    for confession in snapshot.confessions:
                        if confession.id in store.completed:
                            continue
                        if limit is not None and stats.attempted >= limit:
                            break
                        stats.attempted += 1
                        prediction, error, attempts, service_exhausted = _predict(
                            client, bundle, confession.content, config, confession.id, sleep)
                        if error is None:
                            decision = apply_review_policy(prediction, snapshot.flags_by_id[confession.id],
                                threshold=config.classification.review_threshold)
                            record = LabeledRecord(id=confession.id, content=confession.content,
                                                   **decision.model_dump())
                            with defer_sigint():
                                store.append(record)
                                stats.newly_saved += 1
                                stats.remaining -= 1
                                stats.reviewed_new += int(record.needs_review)
                                stats.reviewed_total = store.reviewed_total
                            store.reconcile_failures()
                            service_streak = 0
                            logger.info('label_saved', extra={'event': 'label_saved',
                                'record_id': confession.id, 'attempt': attempts, **asdict(stats)})
                        else:
                            with defer_sigint():
                                store.save_failure(FailureRecord(id=confession.id, error_code=error.code,
                                    attempts=attempts, last_attempt_at=utc_now()))
                                stats.failed += 1
                            logger.warning('classification_failed', extra={'event': 'classification_failed',
                                'record_id': confession.id, 'attempt': attempts, 'error_code': error.code})
                            service_streak = service_streak + 1 if service_exhausted else 0
                            if isinstance(error, AppError) and error.code != 'INPUT_TOO_LONG':
                                raise error
                            if isinstance(error, ModelAttemptError) and not error.retryable:
                                raise AppError(error.code, 'Classification request failed; check the model service.')
                            if service_streak >= config.classification.consecutive_service_failures:
                                raise AppError('MODEL_UNAVAILABLE', 'Repeated model service failures; saved labels are preserved.')
                        if progress:
                            progress(replace(stats))
                    if stats.remaining == 0:
                        summary.status, summary.stop_reason, summary.exit_code = 'complete', 'completed', 0
                    else:
                        summary.status, summary.exit_code = 'partial', 3
                        summary.stop_reason = ('limit_reached' if limit is not None and stats.attempted >= limit
                                               else 'classification_failures')
        except KeyboardInterrupt as error:
            primary_error = error
            _interrupted(summary)
        except Exception as error:
            if isinstance(error, OSError):
                error = AppError('STORAGE_ERROR', 'Classification storage failed; saved labels are preserved.')
            elif not isinstance(error, AppError):
                error = AppError('CLASSIFICATION_ERROR', 'Classification failed unexpectedly; saved labels are preserved.')
            primary_error = error
            summary.status, summary.stop_reason, summary.exit_code = 'failed', error.code, error.exit_code
        finally:
            if store is not None and store.existing_validated and not store_ready:
                # An initialization write may fail after strict resume validation.
                # Partially read/invalid CSV state must never enter these counters.
                stats.already_done = len(store.completed)
                reviewed_before = store.reviewed_total
                store_ready = True
            if store_ready:
                # Durable store state is authoritative even if an interrupt arrives
                # immediately after append or while reconciling stale failures.
                stats.newly_saved = len(store.completed) - stats.already_done
                stats.remaining = stats.total - len(store.completed)
                stats.reviewed_total = store.reviewed_total
                stats.reviewed_new = store.reviewed_total - reviewed_before
            report = {**asdict(summary), 'command': 'classify', 'started_at': started_at,
                'finished_at': utc_now(), 'duration_ms': max(0, round((clock() - started) * 1000)),
                'input_generation_id': snapshot.manifest.generation_id if snapshot else None,
                'input_hashes': {name: snapshot.manifest.outputs[name].sha256
                    for name in ('confessions.csv', 'sources.csv')} if snapshot else {},
                'classification_fingerprint': metadata.classification_fingerprint if metadata else None,
                'model_identity': identity.model_dump(mode='json') if identity else None,
                'effective_config': {'ollama': config.ollama.model_dump(mode='json'),
                    'classification': config.classification.model_dump(mode='json', exclude={'prompt_path'}),
                    'limit': limit}}
            try:
                try:
                    atomic_write_json(config.paths.reports_dir / f'{run_id}.json', report)
                except KeyboardInterrupt as error:
                    if primary_error is None:
                        primary_error = error
                        _interrupted(summary)
                    # The atomic writer may already have published the earlier
                    # status before delivering SIGINT. Replace it once, bounded.
                    report.update(asdict(summary))
                    atomic_write_json(config.paths.reports_dir / f'{run_id}.json', report)
            except (Exception, KeyboardInterrupt):
                logger.error('report_failed', extra={'event': 'report_failed', 'error_code': 'REPORT_WRITE'})
                if primary_error is None:
                    primary_error = AppError('REPORT_WRITE',
                        f'Cannot write classification report; {stats.newly_saved} newly saved labels are preserved.')
                    summary.status, summary.stop_reason, summary.exit_code = 'failed', 'REPORT_WRITE', 1
            finally:
                logger.info('run_finished', extra={'event': 'run_finished', 'status': summary.status,
                    'stop_reason': summary.stop_reason, **asdict(stats)})
    if primary_error is not None and not isinstance(primary_error, KeyboardInterrupt):
        raise primary_error from None
    return summary
