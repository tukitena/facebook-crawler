"""Strict classification persistence. Callers hold the dataset lock throughout."""

import csv
import io
import json
import os
from pathlib import Path
import tempfile

from src.classify.fingerprint import fingerprint_payload_sha256
from src.classify.models import (
    ClassificationMetadata, FAILURE_FIELDS, LABELED_FIELDS, FailureRecord, InputSnapshot, LabeledRecord,
)
from src.config import AppConfig
from src.errors import AppError
from src.preprocess.models import CONFESSION_FIELDS, SOURCE_FIELDS, Confession, SourceRow
from src.preprocess.storage import validate_generation
from src.storage import atomic_write_json, defer_sigint

_LABELS = 'labeled.csv'
_FAILURES = 'classification_failures.csv'
_METADATA = 'labeled.meta.json'


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _unredirected(path: Path) -> Path:
    resolved = path.resolve()
    if resolved != path.absolute():
        raise ValueError('Redirected output path')
    # Multiple links can overwrite inputs even when all path strings differ.
    if path.exists() and path.is_file() and path.stat().st_nlink > 1:
        raise ValueError('Hardlinked output path')
    return resolved


def validate_classify_paths(config: AppConfig, config_path: Path | None = None) -> None:
    """Read-only validation; call before logging/locking and again under the lock."""
    p = config.paths
    try:
        root = _unredirected(p.data_dir)
        labeled, reports, logs = map(_unredirected, (p.labeled_dir, p.reports_dir, p.log_dir))
        lock = _unredirected(p.data_dir / '.dataset.lock')
        if root not in labeled.parents or root not in reports.parents:
            raise ValueError('Classification output must remain in data_dir')
        inputs = [p.raw_csv, p.raw_csv.with_suffix('.meta.json'), p.processed_dir,
                  config.classification.prompt_path, config.chrome.user_data_dir]
        inputs.extend(path for path in (config_path, config.preprocess.overrides_csv) if path is not None)
        protected = [path.resolve() for path in inputs]
        directories = (labeled, reports, logs)
        if any(_overlap(directory, lock) for directory in directories):
            raise ValueError('Output directory overlaps dataset lock')
        if any(_overlap(a, b) for i, a in enumerate(directories) for b in directories[i + 1:]):
            raise ValueError('Output directories overlap')
        if any(_overlap(target, source) for target in (*directories, lock) for source in protected):
            raise ValueError('Output overlaps protected input')
        if _overlap(logs, root):
            raise ValueError('Logs overlap dataset')
        targets = [p.labeled_dir / name for name in (_LABELS, _FAILURES, _METADATA)]
        targets += [p.log_dir / name for name in ('crawl.jsonl', *(f'crawl.jsonl.{i}' for i in range(1, config.logging.backup_count + 1)))]
        # Report names are run IDs supplied by the orchestrator; inspect every
        # existing direct child before any report path may be opened.
        for directory in (p.labeled_dir, p.reports_dir, p.log_dir):
            if directory.exists():
                targets.extend(directory.iterdir())
        for target in targets:
            resolved = _unredirected(target)
            if any(_overlap(resolved, source) for source in (*protected, lock)):
                raise ValueError('Output target overlaps protected input')
            for source in inputs:
                if target.exists() and source.exists() and target.samefile(source):
                    raise ValueError('Output aliases protected input')
    except (OSError, RuntimeError, ValueError) as exc:
        raise AppError('CONFIG_ERROR', 'Classification paths overlap protected data or redirect output.', 2) from exc


def _csv_rows(path: Path, fields: tuple[str, ...], *, allow_empty: bool = False):
    data = path.read_bytes()
    if allow_empty and not data:
        return []
    if not data.startswith(b'\xef\xbb\xbf') or not data.endswith(b'\r\n'):
        raise ValueError('Missing UTF-8 BOM or final CRLF')
    reader = csv.reader(io.StringIO(data.decode('utf-8-sig'), newline=''), strict=True)
    if next(reader, None) != list(fields):
        raise ValueError('Wrong CSV header')
    rows = []
    for values in reader:
        if len(values) != len(fields):
            raise ValueError('Wrong CSV field count')
        rows.append(dict(zip(fields, values)))
    # This also checks record terminators outside quoted multiline fields.
    canonical = io.StringIO(newline='')
    writer = csv.DictWriter(canonical, fieldnames=fields, lineterminator='\r\n')
    writer.writeheader()
    writer.writerows(rows)
    if canonical.getvalue().encode('utf-8-sig') != data:
        raise ValueError('Noncanonical or torn CSV')
    return rows


def load_input(processed_dir: Path) -> InputSnapshot:
    """Validate the committed generation before reading ordered input/provenance."""
    manifest = validate_generation(processed_dir)
    try:
        confessions = tuple(Confession.model_validate(row) for row in
                            _csv_rows(processed_dir / 'confessions.csv', CONFESSION_FIELDS))
        flags = {row.id: set() for row in confessions}
        if len(flags) != len(confessions):
            raise ValueError('Duplicate confession ID')
        keys = set()
        for row in _csv_rows(processed_dir / 'sources.csv', SOURCE_FIELDS):
            source = SourceRow.model_validate(row | {'start_line': int(row['start_line']),
                'end_line': int(row['end_line']), 'flags': tuple(row['flags'].split(';')) if row['flags'] else ()})
            key = (source.confession_id, source.post_id, source.start_line)
            if key in keys or source.confession_id not in flags:
                raise ValueError('Duplicate or unknown source')
            keys.add(key)
            flags[source.confession_id].update(source.flags)
        return InputSnapshot(manifest=manifest, confessions=confessions,
                             flags_by_id={identifier: tuple(sorted(value)) for identifier, value in flags.items()})
    except (OSError, ValueError, UnicodeError, csv.Error, TypeError) as exc:
        raise AppError('INVALID_GENERATION', 'Classification input is invalid; rebuild preprocessing.', 2) from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _io_error(exc: OSError) -> AppError:
    if isinstance(exc, PermissionError):
        return AppError('OUTPUT_LOCKED', 'Classification output is locked; close the application holding it. Temporary files are preserved.')
    return AppError('STORAGE_ERROR', 'Classification write failed; check permissions and disk space before resuming.')


class LabelStore:
    def __init__(self, directory: Path, metadata: ClassificationMetadata, snapshot: InputSnapshot):
        self.directory = Path(directory)
        self.metadata = metadata
        self.snapshot = snapshot
        self.completed: set[str] = set()
        self.reviewed_total = 0
        # Consumers may trust completed only after all resume files validated,
        # even when a subsequent initialization write/reconciliation fails.
        self.existing_validated = False
        self._content = {row.id: row.content for row in snapshot.confessions}
        self._failures: dict[str, FailureRecord] = {}
        self._stream = None
        self._poisoned = False

    def _read_metadata(self) -> bool:
        path = self.directory / _METADATA
        try:
            if fingerprint_payload_sha256(self.metadata.fingerprint_payload) != self.metadata.classification_fingerprint:
                raise ValueError('Invalid expected fingerprint')
            if not path.exists():
                return False
            existing = ClassificationMetadata.model_validate(json.loads(path.read_text(encoding='utf-8'),
                                                                         object_pairs_hook=_unique_object))
            actual = fingerprint_payload_sha256(existing.fingerprint_payload)
            if (actual != existing.classification_fingerprint
                    or actual != self.metadata.classification_fingerprint):
                raise ValueError('Classification fingerprint changed')
            return True
        except (ValueError, TypeError, UnicodeError) as exc:
            raise AppError('RESUME_MISMATCH', 'Classification metadata is invalid or incompatible; use a new labeled directory.', 2) from exc

    def _check_label(self, record: LabeledRecord) -> None:
        if record.id not in self._content or record.content != self._content[record.id]:
            raise ValueError('Unknown ID or mismatched content')

    def _read_existing(self):
        has_metadata = self._read_metadata()
        try:
            path = self.directory / _LABELS
            labels = _csv_rows(path, LABELED_FIELDS, allow_empty=True) if path.exists() else []
            path = self.directory / _FAILURES
            failures = _csv_rows(path, FAILURE_FIELDS, allow_empty=True) if path.exists() else []
            if not has_metadata and (labels or failures):
                raise AppError('RESUME_MISMATCH', 'Existing classification records require metadata.', 2)
            for row in labels:
                record = LabeledRecord.from_csv_row(row)
                self._check_label(record)
                if record.id in self.completed:
                    raise ValueError('Duplicate labeled ID')
                self.completed.add(record.id)
                self.reviewed_total += int(record.needs_review)
            for row in failures:
                record = FailureRecord.from_csv_row(row)
                if record.id not in self._content or record.id in self._failures:
                    raise ValueError('Duplicate or unknown failure ID')
                self._failures[record.id] = record
        except (ValueError, UnicodeError, csv.Error, TypeError) as exc:
            raise AppError('INVALID_CSV', 'Classification CSV is invalid; preserve it for inspection before resuming.', 2) from exc
        return has_metadata

    def __enter__(self):
        try:
            if self._stream is not None or self._poisoned:
                raise AppError('STORAGE_ERROR', 'Classification writer cannot be reopened.')
            self.existing_validated = False
            has_metadata = self._read_existing()
            self.existing_validated = True
            self.directory.mkdir(parents=True, exist_ok=True)
            if not has_metadata:
                atomic_write_json(self.directory / _METADATA, self.metadata.model_dump(mode='json'))
            path = self.directory / _LABELS
            if not path.exists() or path.stat().st_size == 0:
                with defer_sigint(), path.open('w', encoding='utf-8-sig', newline='') as stream:
                    csv.writer(stream, lineterminator='\r\n').writerow(LABELED_FIELDS)
                    stream.flush()
                    os.fsync(stream.fileno())
            self.reconcile_failures()
            self._stream = path.open('a', encoding='utf-8', newline='')
            self._writer = csv.DictWriter(self._stream, fieldnames=LABELED_FIELDS, lineterminator='\r\n')
            return self
        except OSError as exc:
            self._poisoned = True
            raise _io_error(exc) from exc

    def _writable(self):
        if self._poisoned or self._stream is None:
            raise AppError('STORAGE_ERROR', 'Classification writer is closed or a previous write failed.')

    def append(self, record: LabeledRecord) -> None:
        self._writable()
        try:
            record = LabeledRecord.model_validate(record.model_dump())
            self._check_label(record)
            if record.id in self.completed:
                raise ValueError('ID already completed')
        except (ValueError, TypeError) as exc:
            raise AppError('INVALID_CSV', 'Refusing an invalid or duplicate classification record.', 2) from exc
        try:
            with defer_sigint():
                self._writer.writerow(record.to_csv_row())
                self._stream.flush()
                os.fsync(self._stream.fileno())
                self.completed.add(record.id)
                self.reviewed_total += int(record.needs_review)
        except OSError as exc:
            self._poisoned = True
            raise _io_error(exc) from exc

    def _write_failures(self, failures: dict[str, FailureRecord]) -> None:
        if self._poisoned:
            raise AppError('STORAGE_ERROR', 'Classification writer was poisoned by an earlier failure.')
        try:
            with defer_sigint():
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='', dir=self.directory,
                        prefix=_FAILURES + '.', suffix='.tmp', delete=False) as stream:
                    writer = csv.DictWriter(stream, fieldnames=FAILURE_FIELDS, lineterminator='\r\n')
                    writer.writeheader()
                    writer.writerows(record.to_csv_row() for record in failures.values())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(stream.name, self.directory / _FAILURES)
                self._failures = failures
        except OSError as exc:
            self._poisoned = True
            raise _io_error(exc) from exc

    def save_failure(self, record: FailureRecord) -> None:
        self._writable()
        try:
            record = FailureRecord.model_validate(record.model_dump())
            if record.id not in self._content or record.id in self.completed:
                raise ValueError('Unknown or completed failure ID')
        except (ValueError, TypeError) as exc:
            raise AppError('INVALID_CSV', 'Refusing a failure for an unknown or completed ID.', 2) from exc
        self._write_failures(self._failures | {record.id: record})

    def reconcile_failures(self) -> None:
        remaining = {key: value for key, value in self._failures.items() if key not in self.completed}
        if remaining != self._failures:
            self._write_failures(remaining)

    def __exit__(self, exc_type, exc, traceback):
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError as error:
                self._poisoned = True
                if exc_type is None:
                    raise _io_error(error) from error
            finally:
                self._stream = None
