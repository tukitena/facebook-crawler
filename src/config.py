"""Load the shared design schema; runtime dependencies are checked by each command."""

import os
import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.errors import AppError

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
PositiveFloat = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]


class Schema(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_default=True)


class SchoolConfig(Schema):
    name: str = 'Trường Đại học Công nghệ Thông tin'
    aliases: list[str] = ['UIT', 'Đại học Công nghệ Thông tin', 'ĐH CNTT']


class FacebookConfig(Schema):
    page_name: str = 'UIT Confessions'
    page_url: str = 'https://www.facebook.com/UITconfess'

    @model_validator(mode='after')
    def validate_url(self):
        url = urlsplit(self.page_url)
        if (url.scheme != 'https' or url.hostname not in {'facebook.com', 'www.facebook.com'}
                or url.username or url.password or url.port not in {None, 443}
                or not url.path.strip('/') or url.fragment):
            raise ValueError('page_url must identify a Facebook page over HTTPS')
        if not self.page_name.strip():
            raise ValueError('page_name must not be empty')
        path = url.path.strip('/')
        numeric_profile = path == 'profile.php' and parse_qs(url.query).get('id', [''])[0].isdigit()
        vanity_page = bool(path) and '/' not in path and '.' not in path
        if not numeric_profile and not vanity_page:
            raise ValueError('Use a supported page URL: /PageName or /profile.php?id=123')
        return self


class ChromeConfig(Schema):
    executable_path: Path | None = None
    user_data_dir: Path = Path('runtime/chrome-profile')
    headless: bool = Field(default=False, strict=True)
    login_attempts: Annotated[int, Field(strict=True, ge=1, le=3)] = 3


class PathsConfig(Schema):
    data_dir: Path = Path('data/uit')
    raw_csv: Path = Path('data/uit/raw/raw_posts.csv')
    processed_dir: Path = Path('data/uit/processed')
    labeled_dir: Path = Path('data/uit/labeled')
    reports_dir: Path = Path('data/uit/reports')
    log_dir: Path = Path('logs')


class CrawlConfig(Schema):
    max_posts: PositiveInt = 200
    max_scrolls: PositiveInt = 200
    no_progress_rounds: PositiveInt = 8
    scroll_delay_seconds: Annotated[float, Field(strict=True, ge=2, allow_inf_nan=False)] = 2
    feed_wait_timeout_seconds: PositiveFloat = 10
    navigation_timeout_seconds: PositiveFloat = 30
    action_timeout_seconds: PositiveFloat = 10
    blocked_resource_types: list[Literal['image', 'media', 'font']] = ['image', 'media', 'font']
    service_workers: Literal['block'] = 'block'


class PreprocessConfig(Schema):
    marker_policy: Literal['line_start_numeric_v1'] = 'line_start_numeric_v1'
    separator_min_hyphens: Literal[2] = 2
    require_marker: Literal[True] = True
    short_content_chars: PositiveInt = 10
    excluded_post_patterns: list[str] = []
    overrides_csv: Path | None = None


class OllamaConfig(Schema):
    base_url: str = 'http://127.0.0.1:11434'
    model: str = 'qwen3:8b'
    temperature: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)] = 0
    seed: Annotated[int, Field(strict=True)] = 42
    num_ctx: PositiveInt = 8192
    num_predict: PositiveInt = 512
    think: bool = Field(default=False, strict=True)
    keep_alive: str = '5m'
    connect_timeout_seconds: PositiveFloat = 5
    read_timeout_seconds: PositiveFloat = 120
    write_timeout_seconds: PositiveFloat = 10
    pool_timeout_seconds: PositiveFloat = 5


class ClassificationConfig(Schema):
    prompt_path: Path = Path('src/classify/prompts/uit-impact-v1.txt')
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=3)] = 3
    review_threshold: Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)] = 0.70
    max_content_utf8_bytes: PositiveInt = 6000
    context_margin: PositiveInt = 1024
    consecutive_service_failures: PositiveInt = 3


class LoggingConfig(Schema):
    level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR'] = 'INFO'
    max_bytes: PositiveInt = 10485760
    backup_count: PositiveInt = 3


def validate_ollama_base_url(value: str) -> str:
    """Validate at the classify boundary, without affecting offline commands."""
    try:
        if (not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value)
                or any(char in value for char in ('?', '#', '\\'))):
            raise ValueError('Invalid endpoint characters')
        url = urlsplit(value)
        if (url.scheme not in {'http', 'https'} or url.hostname not in {'localhost', '127.0.0.1', '::1'}
                or re.fullmatch(r'(localhost|127\.0\.0\.1|\[::1\])(:[0-9]+)?', url.netloc.lower()) is None
                or url.username is not None or url.password is not None or url.path not in {'', '/'}
                or (url.port is not None and not 1 <= url.port <= 65535)):
            raise ValueError('Invalid loopback endpoint')
        return value.rstrip('/')
    except (TypeError, ValueError) as exc:
        raise AppError('CONFIG_ERROR', 'Ollama URL must be an HTTP(S) loopback endpoint without credentials, query or fragment.', 2) from exc


class AppConfig(Schema):
    schema_version: Literal[1] = 1
    school: SchoolConfig = Field(default_factory=SchoolConfig)
    facebook: FacebookConfig = Field(default_factory=FacebookConfig)
    chrome: ChromeConfig = Field(default_factory=ChromeConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    preprocess: PreprocessConfig = Field(default_factory=PreprocessConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    classification: ClassificationConfig = Field(default_factory=ClassificationConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f'Duplicate YAML key: {key}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _resolve_paths(config: AppConfig, base: Path) -> None:
    for section in (config.paths, config.chrome, config.preprocess, config.classification):
        for key in type(section).model_fields:
            value = getattr(section, key)
            if isinstance(value, Path):
                setattr(section, key, (base / value).resolve())
    p = config.paths
    outputs = [p.raw_csv, p.processed_dir, p.labeled_dir, p.reports_dir]
    if any(p.data_dir not in output.parents for output in outputs):
        raise ValueError('All outputs must be inside data_dir')
    if any(_overlap(a, b) for i, a in enumerate(outputs) for b in outputs[i + 1:]):
        raise ValueError('Output paths must not overlap')
    profile = config.chrome.user_data_dir
    if _overlap(profile, p.data_dir) or _overlap(profile, p.log_dir) or _overlap(p.log_dir, p.data_dir):
        raise ValueError('Profile, data and log directories must not overlap')
    local_app = os.environ.get('LOCALAPPDATA')
    if local_app and _overlap(profile, (Path(local_app) / 'Google/Chrome/User Data').resolve()):
        raise ValueError('Use a dedicated Chrome profile, not the personal default')
    if config.ollama.num_ctx <= config.ollama.num_predict + config.classification.context_margin:
        raise ValueError('num_ctx must exceed num_predict + context_margin')


def load_config(path: Path, overrides: dict[str, object] | None = None) -> AppConfig:
    path = Path(path).resolve()
    try:
        with path.open(encoding='utf-8-sig') as stream:
            values = yaml.load(stream, Loader=UniqueKeyLoader)
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise ValueError('Config must be a YAML mapping')
        if overrides:
            if set(overrides) - {'max_posts', 'max_scrolls'}:
                raise ValueError('Unsupported CLI override')
            values['crawl'] = {**values.get('crawl', {}), **overrides}
        config = AppConfig.model_validate(values)
        _resolve_paths(config, path.parent)
        return config
    except FileNotFoundError as exc:
        raise AppError('CONFIG_ERROR', f'Không tìm thấy {path}. Sao chép config.example.yaml thành config.yaml.', 2) from exc
    except (OSError, ValueError, TypeError, yaml.YAMLError, ValidationError) as exc:
        raise AppError('CONFIG_ERROR', f'Cấu hình không hợp lệ: {exc}', 2) from exc
