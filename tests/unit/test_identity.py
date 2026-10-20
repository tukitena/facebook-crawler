import hashlib

from src.identity import canonical_text, make_post_id, normalize_display


def test_normalization_preserves_lines_unicode_and_emoji():
    text = '\ufeff \t#1\r\n Ca\u0309m\xa0ơn UIT 😊 \r\n\r\n'
    assert normalize_display(text) == '#1\nCảm ơn UIT 😊'
    assert canonical_text(text) == '#1 Cảm ơn UIT 😊'


def test_hash_exact_and_whitespace_dedup():
    expected = 'post_' + hashlib.sha256('post:v1\n#1 Xin chào'.encode()).hexdigest()
    assert make_post_id('#1\nXin chào') == expected
    assert make_post_id(' #1 Xin\tchào ') == expected
    assert make_post_id('#2 Xin chào') != expected
    assert make_post_id('#1 xin chào') != expected
