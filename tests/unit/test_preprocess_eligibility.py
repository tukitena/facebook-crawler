import csv
from importlib import import_module

import pytest

from src.config import PreprocessConfig
from src.errors import AppError
from src.identity import make_post_id
from src.models import RawPost
from src.preprocess.cleanup import cleanup_tail
from src.preprocess.models import PostOverride


def post(content='#1 Admin ơi, thư viện UIT hỗ trợ rất tốt.', kind='text'):
    return RawPost(id=make_post_id(content), content=content, post_kind=kind, crawled_at='2026-10-20T00:00:00Z')


@pytest.mark.parametrize('kind,reason', [
    ('text', None), ('profile_update', 'NON_CONFESSION_EVENT'), ('cover_update', 'NON_CONFESSION_EVENT'),
    ('shared', 'SHARED_POST'), ('unknown', 'UNKNOWN_POST_KIND')])
def test_post_kind_and_admin_word_filter(kind, reason):
    module = import_module('src.preprocess.eligibility')
    value = post(kind=kind)
    policy = module.build_filter_policy(PreprocessConfig())
    result = module.evaluate_post(value, cleanup_tail(value.content), policy)
    assert result.reason == reason
    assert result.accepted is (reason is None)


@pytest.mark.parametrize('kind', ['text', 'profile_update', 'cover_update', 'shared', 'unknown'])
def test_overrides_take_priority_but_accept_still_requires_marker(kind):
    module = import_module('src.preprocess.eligibility')
    policy = module.build_filter_policy(PreprocessConfig(excluded_post_patterns=['.*']))
    value = post(kind=kind)
    cleaned = cleanup_tail(value.content)
    reject = module.evaluate_post(value, cleaned, policy, PostOverride('reject'))
    assert not reject.accepted and reject.reason == 'REJECTED_OVERRIDE'
    accept = module.evaluate_post(value, cleaned, policy, PostOverride('accept'))
    assert accept.accepted and accept.reason is None and accept.flags == ('MANUAL_ACCEPT',)
    no_marker = post('No marker See less', kind)
    result = module.evaluate_post(no_marker, cleanup_tail(no_marker.content), policy, PostOverride('accept'))
    assert not result.accepted and result.reason == 'NO_MARKER'


def test_patterns_use_multiline_raw_and_case_sensitive_matching():
    module = import_module('src.preprocess.eligibility')
    value = post('#1 A\nAdmin message\nSee less')
    for pattern, expected in [('^Admin', 'EXCLUDED_PATTERN'), ('^admin', None), ('^See less$', 'EXCLUDED_PATTERN')]:
        policy = module.build_filter_policy(PreprocessConfig(excluded_post_patterns=[pattern]))
        assert module.evaluate_post(value, cleanup_tail(value.content), policy).reason == expected
    plain = post('Admin ơi')
    assert module.evaluate_post(plain, cleanup_tail(plain.content), module.build_filter_policy(PreprocessConfig())).reason == 'NO_MARKER'


def test_invalid_regex_is_configuration_error():
    module = import_module('src.preprocess.eligibility')
    with pytest.raises(AppError) as error:
        module.build_filter_policy(PreprocessConfig(excluded_post_patterns=['[']))
    assert (error.value.code, error.value.exit_code) == ('CONFIG_ERROR', 2)


def test_overrides_roundtrip_bom_quotes_newlines_and_empty_notes(tmp_path):
    module = import_module('src.preprocess.eligibility')
    first, second = post(), post('#2 B')
    path = tmp_path / 'overrides.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        csv.writer(stream).writerows([
            ['post_id', 'decision', 'note'], [first.id, 'accept', ''],
            [second.id, 'reject', 'Lý do, "quoted"\nhai dòng']])
    rows = module.load_overrides(path, {first.id, second.id})
    assert rows == {first.id: PostOverride('accept', ''), second.id: PostOverride('reject', 'Lý do, "quoted"\nhai dòng')}
    assert module.load_overrides(None, {first.id}) == {}


@pytest.mark.parametrize('damage', ['empty', 'header', 'extra', 'missing', 'unknown', 'duplicate', 'decision', 'utf8', 'quote'])
def test_overrides_validate_all_rows(tmp_path, damage):
    module = import_module('src.preprocess.eligibility')
    value = post()
    rows = [['post_id', 'decision', 'note'], [value.id, 'accept', '']]
    if damage == 'header':
        rows[0][0] = 'ID'
    elif damage == 'extra':
        rows[1].append('extra')
    elif damage == 'missing':
        rows[1].pop()
    elif damage == 'unknown':
        rows[1][0] = 'post_' + '0' * 64
    elif damage == 'duplicate':
        rows.append(rows[1].copy())
    elif damage == 'decision':
        rows[1][1] = 'ACCEPT'
    path = tmp_path / 'overrides.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        csv.writer(stream).writerows(rows)
    if damage in {'empty', 'utf8', 'quote'}:
        path.write_bytes({'empty': b'', 'utf8': b'\xff', 'quote': b'"unfinished'}[damage])
    with pytest.raises(AppError) as error:
        module.load_overrides(path, {value.id})
    assert (error.value.code, error.value.exit_code) == ('CONFIG_ERROR', 2)


def test_missing_override_file_is_storage_error(tmp_path):
    module = import_module('src.preprocess.eligibility')
    with pytest.raises(AppError) as error:
        module.load_overrides(tmp_path / 'absent.csv', set())
    assert (error.value.code, error.value.exit_code) == ('STORAGE_ERROR', 1)
