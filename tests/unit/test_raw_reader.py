import csv
import json

import pytest

from src import storage
from src.errors import AppError


def read(config):
    return list(storage.iter_raw_posts(config.paths.raw_csv, config.facebook.page_url))


def test_reader_roundtrip_without_writes(raw_dataset):
    config = raw_dataset(['#001 Xin chào, "UIT"\nDòng hai 😊'])
    path = config.paths.raw_csv
    before = {p: p.read_bytes() for p in (path, path.with_suffix('.meta.json'))}
    rows = read(config)
    assert rows[0].content == '#001 Xin chào, "UIT"\nDòng hai 😊'
    assert rows[0].post_kind == 'text'
    assert all(p.read_bytes() == data for p, data in before.items())


def test_reader_does_not_create_missing_input(tmp_path):
    path = tmp_path / 'absent' / 'raw.csv'
    with pytest.raises(AppError) as result:
        list(storage.iter_raw_posts(path, 'https://www.facebook.com/UITconfess'))
    assert (result.value.code, result.value.exit_code) == ('INPUT_NOT_FOUND', 2)
    assert not path.parent.exists()


def test_reader_allows_valid_duplicate_rows_but_writer_refuses(raw_dataset):
    config = raw_dataset(['#1 Test'])
    path = config.paths.raw_csv
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.reader(stream))
    with path.open('a', encoding='utf-8', newline='') as stream:
        csv.writer(stream).writerow(rows[1])
    posts = read(config)
    assert len(posts) == 2 and posts[0] == posts[1]
    with pytest.raises(AppError):
        with storage.RawPostStore(path, config.facebook.page_url):
            pass


def test_reader_header_only(raw_dataset):
    assert read(raw_dataset([])) == []


def test_reader_large_multiline_content(raw_dataset):
    content = '#1 ' + ('Nội dung\n' * 25_000).rstrip('\n')
    assert len(content) > 200_000
    config = raw_dataset([content])
    assert read(config)[0].content == content


@pytest.mark.parametrize('damage,code', [
    ('header', 'INVALID_CSV'), ('extra_header', 'INVALID_CSV'), ('no_header', 'INVALID_CSV'),
    ('fields', 'INVALID_CSV'), ('empty', 'INVALID_CSV'), ('utf8', 'INVALID_CSV'),
    ('quote', 'INVALID_CSV'), ('terminator', 'INVALID_CSV'), ('hash', 'IDENTITY_CONFLICT'),
    ('no_meta', 'RESUME_MISMATCH'), ('bad_meta', 'RESUME_MISMATCH'),
    ('page', 'RESUME_MISMATCH'), ('version', 'RESUME_MISMATCH')])
def test_reader_refuses_damage(raw_dataset, damage, code):
    config = raw_dataset(['#1 Test'])
    path = config.paths.raw_csv
    metadata = path.with_suffix('.meta.json')
    data = path.read_bytes()
    if damage == 'header':
        path.write_bytes(data.replace(b'id,content', b'ID,content', 1))
    elif damage == 'extra_header':
        path.write_bytes(data.replace(b'post_kind\r\n', b'post_kind,extra\r\n', 1))
    elif damage == 'no_header':
        path.write_bytes(data.split(b'\r\n', 1)[1])
    elif damage == 'fields':
        path.write_bytes(data + b'x,y\r\n')
    elif damage == 'empty':
        path.write_bytes(b'')
    elif damage == 'utf8':
        path.write_bytes(data + b'\xff\r\n')
    elif damage == 'quote':
        path.write_bytes(data + b'"unfinished\r\n')
    elif damage == 'terminator':
        path.write_bytes(data[:-2])
    elif damage == 'hash':
        path.write_bytes(data.replace(b'\r\npost_', b'\r\nwrong_', 1))
    elif damage == 'no_meta':
        metadata.unlink()
    elif damage == 'bad_meta':
        metadata.write_text('{')
    else:
        value = json.loads(metadata.read_text(encoding='utf-8'))
        value['page_url' if damage == 'page' else 'normalization_version'] = 'wrong'
        metadata.write_text(json.dumps(value), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(AppError) as result:
        read(config)
    assert (result.value.code, result.value.exit_code) == (code, 2)
    assert path.read_bytes() == before
    assert 'Test' not in str(result.value)


def test_reader_io_error_is_storage_error(raw_dataset, monkeypatch):
    config = raw_dataset(['#1 Test'])
    original = type(config.paths.raw_csv).open

    def fail(path, *args, **kwargs):
        if path == config.paths.raw_csv:
            raise PermissionError('denied')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(type(config.paths.raw_csv), 'open', fail)
    with pytest.raises(AppError) as result:
        read(config)
    assert (result.value.code, result.value.exit_code) == ('STORAGE_ERROR', 1)
