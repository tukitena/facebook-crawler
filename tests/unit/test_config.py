import pytest

from src.config import load_config
from src.errors import AppError


def write_config(tmp_path, text='{}'):
    path = tmp_path / 'config.yaml'
    path.write_text(text, encoding='utf-8')
    return path


def test_paths_are_relative_to_config(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    config = load_config(path)
    assert config.paths.raw_csv == tmp_path / 'data/uit/raw/raw_posts.csv'
    assert config.chrome.user_data_dir == tmp_path / 'runtime/chrome-profile'
    assert not config.paths.data_dir.exists()
    assert config.crawl.max_posts == 200


@pytest.mark.parametrize('text', [
    'unknown: true', 'crawl: {max_post: 2}',
    'crawl: {max_posts: 1, max_posts: 2}',
    'crawl: {max_posts: 0}', 'crawl: {max_posts: true}',
    'crawl: {feed_wait_timeout_seconds: .inf}',
    'facebook: {page_url: "http://www.facebook.com/UITconfess"}',
    'facebook: {page_url: "https://www.facebook.com.evil.test/UITconfess"}',
    'facebook: {page_url: "https://user@facebook.com/UITconfess"}',
    'facebook: {page_url: "https://www.facebook.com/pages/UITconfess/123"}',
    'facebook: {page_url: "https://www.facebook.com/groups/999"}',
    'paths: {raw_csv: "../outside.csv"}',
    'chrome: {user_data_dir: "data/uit/profile"}',
    'chrome: {user_data_dir: "logs"}',
    'paths: {processed_dir: "data/uit/raw/raw_posts.csv"}',
    'crawl: {blocked_resource_types: [script]}',
    'preprocess: {require_marker: false}',
])
def test_invalid_config_rejected_without_creating_outputs(tmp_path, text):
    with pytest.raises(AppError) as err:
        load_config(write_config(tmp_path, text))
    assert err.value.exit_code == 2
    assert not (tmp_path / 'data').exists()


def test_cli_overrides_defaults(tmp_path):
    config = load_config(write_config(tmp_path, 'crawl: {max_posts: 5}'), {'max_posts': 2})
    assert config.crawl.max_posts == 2
    assert config.crawl.max_scrolls == 200


def test_full_design_config_loads_without_chrome_or_ollama(tmp_path):
    config = load_config(write_config(tmp_path, '''
school: {name: UIT, aliases: [UIT]}
preprocess: {marker_policy: line_start_numeric_v1, separator_min_hyphens: 2}
ollama: {model: 'qwen3:8b'}
classification: {review_threshold: 0.70}
'''))
    assert config.ollama.model == 'qwen3:8b'


def test_missing_config_has_actionable_error(tmp_path):
    with pytest.raises(AppError, match='config.example.yaml'):
        load_config(tmp_path / 'missing.yaml')
