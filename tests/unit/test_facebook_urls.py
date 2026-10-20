import pytest

from src.crawl.facebook_adapter import page_identity, post_permalink


def test_page_identity_ignores_tracking_not_host():
    assert page_identity('https://www.facebook.com/UITconfess/?foo=x') == 'uitconfess'
    assert page_identity('https://facebook.com/profile.php?id=123') == 'id:123'
    assert page_identity('https://evil.test/UITconfess') is None


@pytest.mark.parametrize('url,expected', [
    ('/UITconfess/posts/123?__cft__=x', 'https://www.facebook.com/UITconfess/posts/123'),
    ('https://www.facebook.com/story.php?story_fbid=abc&id=12&tracking=1',
     'https://www.facebook.com/story.php?story_fbid=abc&id=12'),
    ('https://evil.test/UITconfess/posts/123', ''),
    ('/UITconfess', ''), ('javascript:bad', ''), ('/photo/?fbid=123', ''),
])
def test_permalink_must_be_recognizable(url, expected):
    assert post_permalink(url) == expected
