import pytest
from pydantic import ValidationError

from src.identity import make_post_id
from src.models import RawPost


def test_raw_post_validation():
    values = dict(id=make_post_id('#1 Xin chào'), content='#1 Xin chào',
                  crawled_at='2026-10-20T00:00:00Z', source_url='', post_kind='text')
    assert RawPost(**values).content == '#1 Xin chào'
    for field, value in [('id', 'post_fake'), ('content', ' #1 Xin chào '),
                         ('crawled_at', '2026-10-20'), ('post_kind', 'other'),
                         ('content', ''), ('source_url', 'javascript:bad')]:
        with pytest.raises(ValidationError):
            RawPost(**{**values, field: value})
