"""Strict raw CSV persistence. Callers hold the dataset lock for its entire lifetime."""

import csv
import json
import os
import signal
import sys
import tempfile
import threading
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

import portalocker
from pydantic import ValidationError

from src.errors import AppError
from src.identity import NORMALIZATION_VERSION, canonical_text, make_post_id
from src.models import RawPost

RAW_FIELDS = ['id', 'content', 'crawled_at', 'source_url', 'post_kind']
csv.field_size_limit(10 * 1024 * 1024)


@contextmanager
def defer_sigint():
    """Finish a durable commit before delivering a normal Ctrl+C (main thread)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)
    pending = []
    signal.signal(signal.SIGINT, lambda signum, frame: pending.append((signum, frame)))
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        if pending and sys.exc_info()[0] is None:
            if callable(previous):
                previous(*pending[0])
            elif previous == signal.SIG_DFL:
                raise KeyboardInterrupt


@contextmanager
def resource_lock(path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = portalocker.Lock(str(path), mode='a', timeout=0,
                                flags=portalocker.LOCK_EX | portalocker.LOCK_NB)
        lock.acquire()
    except portalocker.exceptions.LockException as exc:
        raise AppError('RESOURCE_LOCKED', f'Tài nguyên đang được sử dụng: {path}', 2) from exc
    except OSError as exc:
        raise AppError('STORAGE_ERROR', f'Không mở được khóa: {path}') from exc
    try:
        yield
    finally:
        lock.release()


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with defer_sigint():
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(stream.name, path)
    except PermissionError as exc:
        raise AppError('OUTPUT_LOCKED', f'OUTPUT_LOCKED: đóng ứng dụng đang giữ {path}; file tạm được giữ lại.') from exc
    except OSError as exc:
        raise AppError('STORAGE_ERROR', f'Không ghi được {path}; kiểm tra dung lượng/quyền ghi.') from exc


def _read_raw_records(path: Path, *, identity_error: str = 'INVALID_CSV') -> Iterator[RawPost]:
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() < 3:
            raise AppError('INVALID_CSV', 'CSV thiếu header hoặc bị cắt dở.', 2)
        stream.seek(-1, os.SEEK_END)
        if stream.read(1) != b'\n':
            raise AppError('INVALID_CSV', 'CSV thiếu dấu kết thúc record; giữ nguyên để khôi phục.', 2)
    try:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream, strict=True)
            if next(reader, None) != RAW_FIELDS:
                raise ValueError('Wrong header')
            for row in reader:
                if len(row) != len(RAW_FIELDS):
                    raise ValueError('Wrong field count')
                if row[0] != make_post_id(row[1]):
                    raise AppError(identity_error, 'ID raw không khớp nội dung; giữ nguyên để kiểm tra.', 2)
                yield RawPost.model_validate(dict(zip(RAW_FIELDS, row)))
    except (ValueError, UnicodeError, csv.Error, ValidationError) as exc:
        raise AppError('INVALID_CSV', 'CSV sai schema hoặc bị cắt dở; giữ nguyên để kiểm tra.', 2) from exc


def iter_raw_posts(path: Path, page_url: str) -> Iterator[RawPost]:
    """Read validated raw records without creating files or deduplicating them."""
    path = Path(path)
    try:
        if not path.exists():
            raise AppError('INPUT_NOT_FOUND', f'Chưa có file raw: {path}', 2)
        expected = {'schema_version': 1, 'page_url': page_url,
                    'normalization_version': NORMALIZATION_VERSION}
        try:
            metadata = json.loads(path.with_suffix('.meta.json').read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError, UnicodeError) as exc:
            raise AppError('RESUME_MISMATCH', 'Metadata raw thiếu hoặc không hợp lệ.', 2) from exc
        if metadata != expected:
            raise AppError('RESUME_MISMATCH', 'Page/schema/normalization raw không khớp.', 2)
        yield from _read_raw_records(path, identity_error='IDENTITY_CONFLICT')
    except OSError as exc:
        raise AppError('STORAGE_ERROR', f'Không đọc được dữ liệu raw: {path}') from exc


class RawPostStore:
    def __init__(self, path: Path, page_url: str):
        self.path = Path(path)
        self.metadata_path = self.path.with_suffix('.meta.json')
        self.metadata = {'schema_version': 1, 'page_url': page_url,
                         'normalization_version': NORMALIZATION_VERSION}
        self.existing: dict[str, str] = {}
        self._stream = None
        self._poisoned = False

    def _read_existing(self):
        if self.metadata_path.exists():
            try:
                value = json.loads(self.metadata_path.read_text(encoding='utf-8'))
            except (ValueError, UnicodeError) as exc:
                raise AppError('RESUME_MISMATCH', 'Metadata raw không hợp lệ; giữ nguyên để kiểm tra.', 2) from exc
            if value != self.metadata:
                raise AppError('RESUME_MISMATCH', 'Page/schema/normalization đã đổi; hãy dùng data_dir mới.', 2)
        elif self.path.exists():
            raise AppError('RESUME_MISMATCH', 'CSV hiện có thiếu metadata; không tự ghi đè.', 2)
        if not self.path.exists():
            return
        for post in _read_raw_records(self.path):
            if post.id in self.existing:
                raise AppError('INVALID_CSV', 'CSV chứa ID trùng; giữ nguyên để kiểm tra.', 2)
            self.existing[post.id] = canonical_text(post.content)

    def __enter__(self):
        try:
            self._read_existing()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.metadata_path.exists():
                atomic_write_json(self.metadata_path, self.metadata)
            if not self.path.exists():
                with defer_sigint(), self.path.open('x', encoding='utf-8-sig', newline='') as stream:
                    csv.writer(stream).writerow(RAW_FIELDS)
                    stream.flush()
                    os.fsync(stream.fileno())
            self._stream = self.path.open('a', encoding='utf-8', newline='')
            self._writer = csv.DictWriter(self._stream, fieldnames=RAW_FIELDS)
            return self
        except OSError as exc:
            raise AppError('STORAGE_ERROR', f'Không mở được CSV: {self.path}') from exc

    def append(self, post: RawPost) -> bool:
        post = RawPost.model_validate(post.model_dump())
        content = canonical_text(post.content)
        if self._poisoned or self._stream is None:
            raise AppError('STORAGE_ERROR', 'Writer đã đóng hoặc lần ghi trước thất bại.')
        if post.id in self.existing:
            if self.existing[post.id] != content:
                raise AppError('IDENTITY_CONFLICT', 'ID trùng nhưng nội dung khác.', 2)
            return False
        try:
            with defer_sigint():
                self._writer.writerow(post.model_dump(mode='json'))
                self._stream.flush()
                os.fsync(self._stream.fileno())
                self.existing[post.id] = content
        except OSError as exc:
            self._poisoned = True
            raise AppError('STORAGE_ERROR', 'Ghi CSV thất bại; dừng để kiểm tra file trước khi chạy lại.') from exc
        return True

    def __exit__(self, exc_type, exc, traceback):
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                if exc_type is None:
                    raise AppError('STORAGE_ERROR', 'Không đóng được CSV.')
            finally:
                self._stream = None
