from importlib import import_module
import hashlib

import pytest
from pydantic import ValidationError


def confession_id(body):
    return 'cfs_' + hashlib.sha256(('confession:v1\n' + body).encode()).hexdigest()


def test_confession_preserves_number_and_allows_same_number_for_different_bodies():
    models = import_module('src.preprocess.models')
    first = models.Confession(id=confession_id('Xin chào'), content='Xin chào', confession_number='001')
    second = models.Confession(id=confession_id('Tạm biệt'), content='Tạm biệt', confession_number='001')
    assert first.confession_number == second.confession_number == '001'
    assert first.id != second.id
    with pytest.raises(ValidationError):
        first.content = 'changed'


@pytest.mark.parametrize('number', [1, '', '١', '1a', '-1'])
def test_confession_rejects_non_ascii_string_numbers(number):
    models = import_module('src.preprocess.models')
    with pytest.raises(ValidationError):
        models.Confession(id=confession_id('Xin chào'), content='Xin chào', confession_number=number)


@pytest.mark.parametrize('changes', [
    {'id': 'cfs_' + '0' * 64}, {'content': ''}, {'content': ' Xin chào'}, {'unexpected': 'field'}])
def test_confession_rejects_invalid_body_identity_and_extra_fields(changes):
    models = import_module('src.preprocess.models')
    values = dict(id=confession_id('Xin chào'), content='Xin chào', confession_number='1')
    with pytest.raises(ValidationError):
        models.Confession(**(values | changes))


def test_source_flags_are_sorted_unique_and_number_keeps_zeroes():
    models = import_module('src.preprocess.models')
    row = models.SourceRow(confession_id=confession_id('A'), post_id='post_' + 'a' * 64,
                           marker='001', start_line=1, end_line=2,
                           flags=('SHORT_CONTENT', 'MANUAL_ACCEPT', 'SHORT_CONTENT'))
    assert row.marker == '001'
    assert row.flags == ('MANUAL_ACCEPT', 'SHORT_CONTENT')


@pytest.mark.parametrize('changes', [
    {'start_line': 0}, {'start_line': True}, {'start_line': 1.0}, {'end_line': 0},
    {'start_line': 3}, {'marker': '١'}, {'marker': 1}, {'flags': ('UNRECOGNIZED',)},
    {'confession_id': 'bad'}, {'post_id': 'bad'}, {'source_url': 'https://example.com/post'},
    {'extra': 'field'}])
def test_source_rejects_invalid_spans_and_fields(changes):
    models = import_module('src.preprocess.models')
    values = dict(confession_id=confession_id('A'), post_id='post_' + 'a' * 64,
                  marker='1', start_line=1, end_line=2)
    with pytest.raises(ValidationError):
        models.SourceRow(**(values | changes))


@pytest.mark.parametrize('changes', [
    {'reason': 'BAD_REASON'}, {'excerpt': 'x' * 201}, {'start_line': 0}, {'end_line': 0}])
def test_excluded_row_rejects_invalid_audit_data(changes):
    models = import_module('src.preprocess.models')
    values = dict(post_id='post_' + 'a' * 64, start_line=1, end_line=2, reason='PREAMBLE', excerpt='x' * 200)
    with pytest.raises(ValidationError):
        models.ExcludedRow(**(values | changes))
