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


@pytest.mark.parametrize('text', [
    'ollama: {seed: true}', 'ollama: {seed: "42"}',
    'ollama: {think: "false"}', 'ollama: {think: 0}',
    'ollama: {temperature: true}', 'classification: {review_threshold: true}',
])
def test_classification_config_rejects_coerced_scalars(tmp_path, text):
    with pytest.raises(AppError) as error:
        load_config(write_config(tmp_path, text))
    assert error.value.code == 'CONFIG_ERROR'


@pytest.mark.parametrize('url', [
    'http://localhost', 'https://localhost:11434/',
    'http://127.0.0.1:11434', 'https://[::1]:11434',
])
def test_classify_accepts_loopback_ollama_endpoint(url):
    from src.config import validate_ollama_base_url
    assert validate_ollama_base_url(url) == url.rstrip('/')


@pytest.mark.parametrize('url', [
    'localhost:11434', 'ftp://localhost', 'http://remote.test',
    'http://127.0.0.2', 'http://localhost.evil.test', 'http://user@localhost',
    'http://user:secret@localhost', 'http://localhost?', 'http://localhost#',
    'http://localhost?next=remote', 'http://localhost#fragment',
    'http://localhost/api', 'http://localhost:bad', 'http://localhost:65536',
    'http://localhost:0', 'http://[::1', ' http://localhost',
    'http://local\nhost', 'http://localhost\\@remote.test',
    'http://localhost:', 'http://[::1]:', 'http://[::1]evil:11434',
])
def test_classify_rejects_invalid_or_remote_ollama_endpoint(url):
    from src.config import validate_ollama_base_url
    with pytest.raises(AppError) as error:
        validate_ollama_base_url(url)
    assert error.value.code == 'CONFIG_ERROR'
    assert error.value.exit_code == 2


def test_offline_config_loading_does_not_validate_ollama_endpoint(tmp_path):
    config = load_config(write_config(tmp_path, 'ollama: {base_url: "http://remote.test"}'))
    assert config.ollama.base_url == 'http://remote.test'
