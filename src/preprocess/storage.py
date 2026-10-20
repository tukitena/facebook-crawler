"""Staged CSV generations with a manifest committed last.

Callers own the dataset lock. A failed multi-file publish is detected by the
manifest reader; it is not described as a filesystem transaction.
"""

from collections import Counter
import csv
import hashlib
from itertools import chain
import json
import math
import os
from pathlib import Path
import tempfile

from src.config import AppConfig
from src.errors import AppError
from src.identity import NORMALIZATION_VERSION
from src.preprocess.models import (
    CONFESSION_FIELDS, EXCLUDED_FIELDS, SOURCE_FIELDS, FILTER_VERSION, SPLITTER_VERSION,
    Confession, ExcludedRow, OutputInfo, PreprocessManifest, SourceRow,
)
from src.storage import atomic_write_json, defer_sigint

_SCHEMAS = {
    'confessions.csv': (CONFESSION_FIELDS, Confession),
    'sources.csv': (SOURCE_FIELDS, SourceRow),
    'excluded.csv': (EXCLUDED_FIELDS, ExcludedRow),
}
_MANIFEST = 'preprocess.manifest.json'
_LABELED_FIELDS = ('id', 'content', 'label', 'confidence', 'reason', 'needs_review', 'review_reasons')


def file_sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def json_sha256(value: dict[str, object]) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def validate_preprocess_paths(config: AppConfig, config_path: Path | None = None) -> None:
    p = config.paths
    try:
        root = p.data_dir.resolve()
        directory = p.processed_dir.resolve()
        lock = p.data_dir / '.dataset.lock'
        if root not in directory.parents or lock.resolve() != lock.absolute():
            raise ValueError('Invalid dataset/processed/lock location')
        inputs = [p.raw_csv, p.raw_csv.with_suffix('.meta.json'), config.classification.prompt_path]
        inputs += [path for path in (config_path, config.preprocess.overrides_csv) if path is not None]
        inputs = [path.resolve() for path in inputs]
        protected = [*inputs, lock.resolve(), p.labeled_dir.resolve(), p.log_dir.resolve(),
                     p.reports_dir.resolve(), config.chrome.user_data_dir.resolve()]
        for name in (*_SCHEMAS, _MANIFEST):
            target = p.processed_dir / name
            resolved = target.resolve()
            if (target.absolute() != resolved or directory not in resolved.parents
                    or any(_overlap(resolved, path) for path in protected)):
                raise ValueError('Output redirects or overlaps protected data')
        for name in ('crawl.jsonl', *(f'crawl.jsonl.{i}' for i in range(1, config.logging.backup_count + 1))):
            if (p.log_dir / name).resolve() in inputs:
                raise ValueError('Log would overwrite an input file')
    except (OSError, RuntimeError, ValueError) as exc:
        raise AppError('CONFIG_ERROR', 'Đường dẫn preprocess trùng input hoặc nằm ngoài vùng dữ liệu hợp lệ.', 2) from exc


def _csv_rows(path: Path, header: tuple[str, ...]):
    with path.open('rb') as stream:
        if stream.read(3) != b'\xef\xbb\xbf':
            raise ValueError('Missing UTF-8 BOM')
        stream.seek(-1, os.SEEK_END)
        if stream.read(1) != b'\n':
            raise ValueError('Truncated CSV')
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream, strict=True)
        if next(reader, None) != list(header):
            raise ValueError('Wrong CSV header')
        for row in reader:
            if len(row) != len(header):
                raise ValueError('Wrong CSV field count')
            yield dict(zip(header, row))


def _csv_record(row) -> dict[str, object]:
    data = row.model_dump()
    if isinstance(row, SourceRow):
        data['flags'] = ';'.join(row.flags)
    return data


def _records(path: Path, name: str):
    header, model = _SCHEMAS[name]
    for raw in _csv_rows(path, header):
        values = dict(raw)
        if model is not Confession:
            values['start_line'] = int(raw['start_line'])
            values['end_line'] = int(raw['end_line'])
        if model is SourceRow:
            values['flags'] = tuple(raw['flags'].split(';')) if raw['flags'] else ()
        record = model.model_validate(values)
        if {key: str(value) for key, value in _csv_record(record).items()} != raw:
            raise ValueError('Noncanonical CSV value')
        yield record


def _inspect_files(paths: dict[str, Path]) -> tuple[dict[str, OutputInfo], dict[str, int]]:
    confessions: dict[str, str] = {}
    for row in _records(paths['confessions.csv'], 'confessions.csv'):
        if row.id in confessions:
            raise ValueError('Duplicate confession')
        confessions[row.id] = row.confession_number
    keys = set()
    first_markers: dict[str, str] = {}
    source_count = 0
    for row in _records(paths['sources.csv'], 'sources.csv'):
        key = (row.confession_id, row.post_id, row.start_line)
        if key in keys or row.confession_id not in confessions:
            raise ValueError('Duplicate or orphan source')
        keys.add(key)
        first_markers.setdefault(row.confession_id, row.marker)
        source_count += 1
    if first_markers != confessions:
        raise ValueError('Missing provenance or wrong representative number')
    reasons = Counter(row.reason for row in _records(paths['excluded.csv'], 'excluded.csv'))
    counts = {'confessions.csv': len(confessions), 'sources.csv': source_count, 'excluded.csv': sum(reasons.values())}
    outputs = {name: OutputInfo(sha256=file_sha256(path), rows=counts[name]) for name, path in paths.items()}
    return outputs, dict(reasons)


def _check_manifest(manifest: PreprocessManifest, outputs: dict[str, OutputInfo], reasons: dict[str, int]) -> None:
    if (manifest.normalization_version != NORMALIZATION_VERSION or manifest.filter_version != FILTER_VERSION
            or manifest.splitter_version != SPLITTER_VERSION or manifest.outputs != outputs
            or manifest.effective_config_sha256 != json_sha256(manifest.effective_config)
            or manifest.excluded_by_reason != reasons):
        raise ValueError('Generation metadata mismatch')
    c = manifest.counts
    if (c['unique_confessions'] != outputs['confessions.csv'].rows or c['sources'] != outputs['sources.csv'].rows
            or c['excluded_regions'] != outputs['excluded.csv'].rows
            or c['empty_segments'] != reasons.get('EMPTY_SEGMENT', 0)
            or c['read_posts'] != c['duplicate_posts'] + c['accepted_posts'] + c['excluded_posts']
            or c['candidate_segments'] != c['sources'] + c['empty_segments']
            or c['sources'] != c['unique_confessions'] + c['duplicate_confessions']):
        raise ValueError('Generation counters mismatch')


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON key')
        value[key] = item
    return value


def validate_generation(processed_dir: Path) -> PreprocessManifest:
    try:
        payload = json.loads((processed_dir / _MANIFEST).read_text(encoding='utf-8'), object_pairs_hook=_unique_json_object)
        manifest = PreprocessManifest.model_validate(payload)
        outputs, reasons = _inspect_files({name: processed_dir / name for name in _SCHEMAS})
        _check_manifest(manifest, outputs, reasons)
        return manifest
    except (OSError, ValueError, UnicodeError, csv.Error, TypeError) as exc:
        raise AppError('INVALID_GENERATION', 'Output preprocess thiếu/hỏng hoặc không khớp manifest; chạy lại preprocess.', 2) from exc


def _input_key(manifest: PreprocessManifest):
    return (manifest.outputs['confessions.csv'].sha256, manifest.outputs['sources.csv'].sha256,
            manifest.normalization_version, manifest.filter_version, manifest.splitter_version)


def assert_no_derived_conflict(processed_dir: Path, labeled_dir: Path, proposed: PreprocessManifest) -> None:
    labels = labeled_dir / 'labeled.csv'
    try:
        if not labels.exists() or labels.stat().st_size == 0:
            return
        rows = _csv_rows(labels, _LABELED_FIELDS)
        try:
            first = next(rows, None)
            if first is None:
                return
            previous = validate_generation(processed_dir)
            if _input_key(previous) != _input_key(proposed):
                raise ValueError('Classification input has changed')
            content_by_id = {row.id: row.content for row in _records(processed_dir / 'confessions.csv', 'confessions.csv')}
            seen = set()
            for row in chain((first,), rows):
                confidence = float(row['confidence'])
                if (row['id'] in seen or row['id'] not in content_by_id
                        or row['content'] != content_by_id[row['id']]
                        or row['label'] not in {'Positive', 'Neutral', 'Negative'}
                        or not math.isfinite(confidence) or not 0 <= confidence <= 1
                        or not 1 <= len(row['reason'].strip()) <= 300
                        or row['needs_review'] not in {'true', 'false'}):
                    raise ValueError('Invalid labeled record')
                seen.add(row['id'])
        finally:
            rows.close()
    except OSError as exc:
        raise AppError('STORAGE_ERROR', 'Không đọc được dữ liệu nhãn để kiểm tra conflict.') from exc
    except (ValueError, UnicodeError, csv.Error, AppError) as exc:
        raise AppError('DERIVED_DATA_CONFLICT', 'Nhãn cũ không tương thích hoặc không xác minh được; dùng labeled_dir mới hoặc lưu trữ bộ nhãn cũ.', 2) from exc


def _storage_error(exc: OSError) -> AppError:
    if isinstance(exc, PermissionError):
        return AppError('OUTPUT_LOCKED', 'Output đang bị giữ; đóng ứng dụng đang mở file rồi chạy lại. File tạm được giữ lại.')
    return AppError('STORAGE_ERROR', 'Không ghi được output preprocess; kiểm tra dung lượng/quyền ghi rồi chạy lại.')


class ProcessedWriter:
    def __init__(self, processed_dir: Path):
        self.directory = processed_dir
        self.published_manifest: PreprocessManifest | None = None
        self._paths: dict[str, Path] = {}
        self._streams = {}
        self._writers = {}
        self._finished: dict[str, OutputInfo] | None = None
        self._publishing = False
        self._keep_temps = False

    def __enter__(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            for name, (header, _) in _SCHEMAS.items():
                stream = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='', dir=self.directory,
                                                     prefix=name + '.', suffix='.tmp', delete=False)
                self._paths[name] = Path(stream.name)
                self._streams[name] = stream
                self._writers[name] = csv.DictWriter(stream, fieldnames=header, lineterminator='\r\n')
                self._writers[name].writeheader()
            return self
        except OSError as exc:
            self._keep_temps = isinstance(exc, PermissionError)
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise _storage_error(exc) from exc

    def _write(self, name: str, row) -> None:
        if self._finished is not None or name not in self._writers or self._streams[name].closed:
            raise AppError('STORAGE_ERROR', 'Writer preprocess đã đóng hoặc chưa mở.')
        try:
            row = _SCHEMAS[name][1].model_validate(row.model_dump())
            self._writers[name].writerow(_csv_record(row))
        except OSError as exc:
            self._keep_temps = isinstance(exc, PermissionError)
            raise _storage_error(exc) from exc

    def write_confession(self, row: Confession) -> None:
        self._write('confessions.csv', row)

    def write_source(self, row: SourceRow) -> None:
        self._write('sources.csv', row)

    def write_excluded(self, row: ExcludedRow) -> None:
        self._write('excluded.csv', row)

    def finish(self) -> dict[str, OutputInfo]:
        if self._finished is not None:
            return dict(self._finished)
        try:
            for stream in self._streams.values():
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
            self._finished, _ = _inspect_files(self._paths)
            return dict(self._finished)
        except OSError as exc:
            self._keep_temps = isinstance(exc, PermissionError)
            raise _storage_error(exc) from exc
        except (ValueError, UnicodeError, csv.Error) as exc:
            raise AppError('INVALID_GENERATION', 'CSV tạm không đạt schema/identity/provenance.', 2) from exc

    def publish(self, manifest: PreprocessManifest, *, labeled_dir: Path) -> PreprocessManifest:
        if self._finished is None or self._publishing:
            raise AppError('STORAGE_ERROR', 'Phải finish một lần trước khi publish generation.')
        try:
            outputs, reasons = _inspect_files(self._paths)
            _check_manifest(manifest, outputs, reasons)
            assert_no_derived_conflict(self.directory, labeled_dir, manifest)
            self._publishing = True
            with defer_sigint():
                for name, path in self._paths.items():
                    os.replace(path, self.directory / name)
                atomic_write_json(self.directory / _MANIFEST, manifest.model_dump(mode='json'))
                self.published_manifest = manifest
            return manifest
        except OSError as exc:
            self._keep_temps = isinstance(exc, PermissionError)
            raise _storage_error(exc) from exc
        except (ValueError, UnicodeError, csv.Error) as exc:
            raise AppError('INVALID_GENERATION', 'Generation tạm không khớp manifest.', 2) from exc

    def __exit__(self, exc_type, exc, traceback):
        cleanup_error = None
        for stream in self._streams.values():
            try:
                stream.close()
            except OSError as error:
                cleanup_error = error
        if not self._keep_temps and not self._publishing:
            for path in self._paths.values():
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    cleanup_error = error
        if cleanup_error and exc_type is None:
            raise _storage_error(cleanup_error) from cleanup_error
