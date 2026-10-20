import csv
import json
import signal

import pytest

from src.errors import AppError
from src.identity import make_post_id
from src.models import RawPost
from src.storage import RawPostStore, atomic_write_json

URL = 'https://www.facebook.com/UITconfess'


def post(text='#1 Xin chào, "UIT"\nDòng hai 😊'):
    return RawPost(id=make_post_id(text), content=text, crawled_at='2026-10-20T00:00:00Z', post_kind='text')


def test_roundtrip_and_dedup(tmp_path):
    path = tmp_path / 'raw_posts.csv'
    with RawPostStore(path, URL) as store:
        assert store.append(post()) is True
        assert store.append(post()) is False
    with RawPostStore(path, URL) as store:
        assert len(store.existing) == 1
        assert not store.append(post().model_copy(update={'crawled_at': '2026-10-21T00:00:00Z'}))
        assert store.append(post('#2 Mới'))
    data = path.read_bytes()
    assert data.count(b'\xef\xbb\xbf') == 1
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert list(rows[0]) == ['id', 'content', 'crawled_at', 'source_url', 'post_kind']
    assert rows[0]['content'] == post().content
    assert rows[0]['crawled_at'] == '2026-10-20T00:00:00Z'


def test_header_only_and_metadata_before_csv(tmp_path):
    path = tmp_path / 'raw_posts.csv'
    with RawPostStore(path, URL):
        assert path.with_suffix('.meta.json').exists()
    with RawPostStore(path, URL) as store:
        assert store.existing == {}


@pytest.mark.parametrize('damage', ['tail', 'hash', 'columns', 'empty', 'no_meta', 'wrong_page', 'duplicate'])
def test_corruption_refused_without_changing_file(tmp_path, damage):
    path = tmp_path / 'raw_posts.csv'
    with RawPostStore(path, URL) as store:
        store.append(post('#1 Test'))
    data = path.read_bytes()
    if damage == 'tail':
        path.write_bytes(data[:-2])
    elif damage == 'hash':
        path.write_bytes(data.replace(post('#1 Test').id.encode(), b'post_fake'))
    elif damage == 'columns':
        path.write_bytes(data + b'x,y\r\n')
    elif damage == 'empty':
        path.write_bytes(b'')
    elif damage == 'no_meta':
        path.with_suffix('.meta.json').unlink()
    elif damage == 'wrong_page':
        path.with_suffix('.meta.json').write_text('{}')
    elif damage == 'duplicate':
        path.write_bytes(data + data.split(b'\r\n')[1] + b'\r\n')
    before = path.read_bytes()
    with pytest.raises(AppError):
        with RawPostStore(path, URL):
            pass
    assert path.read_bytes() == before


def test_sigint_waits_for_fsync(tmp_path, monkeypatch):
    import src.storage as module
    path = tmp_path / 'raw_posts.csv'
    with RawPostStore(path, URL) as store:
        fsync = module.os.fsync
        calls = []
        def interrupted_fsync(fd):
            signal.raise_signal(signal.SIGINT)
            fsync(fd)
            calls.append('synced')
        monkeypatch.setattr(module.os, 'fsync', interrupted_fsync)
        with pytest.raises(KeyboardInterrupt):
            store.append(post())
        assert calls == ['synced']
    monkeypatch.setattr(module.os, 'fsync', fsync)
    with RawPostStore(path, URL) as store:
        assert len(store.existing) == 1


def test_fsync_failure_does_not_mark_saved(tmp_path, monkeypatch):
    import src.storage as module
    with RawPostStore(tmp_path / 'raw_posts.csv', URL) as store:
        def fail(fd):
            raise OSError('disk full')
        monkeypatch.setattr(module.os, 'fsync', fail)
        with pytest.raises(AppError):
            store.append(post())
        assert store.existing == {}


def test_atomic_replace_locked_keeps_old_file(tmp_path, monkeypatch):
    import src.storage as module
    path = tmp_path / 'report.json'
    path.write_text('{"old":true}')
    def fail(*args):
        raise PermissionError('held by Excel')
    monkeypatch.setattr(module.os, 'replace', fail)
    with pytest.raises(AppError, match='OUTPUT_LOCKED'):
        atomic_write_json(path, {'new': True})
    assert json.loads(path.read_text()) == {'old': True}
    assert list(tmp_path.glob('*.tmp'))
