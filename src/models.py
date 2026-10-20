from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, model_validator

from src.identity import make_post_id, normalize_display


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


class PostKind(str, Enum):
    TEXT = 'text'
    PROFILE_UPDATE = 'profile_update'
    COVER_UPDATE = 'cover_update'
    SHARED = 'shared'
    UNKNOWN = 'unknown'


class SessionState(str, Enum):
    AUTHENTICATED = 'AUTHENTICATED'
    LOGIN_REQUIRED = 'LOGIN_REQUIRED'
    CHECKPOINT = 'CHECKPOINT'
    UNKNOWN = 'UNKNOWN'


class RawPost(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    id: str
    content: str
    crawled_at: str
    source_url: str = ''
    post_kind: PostKind = PostKind.UNKNOWN

    @model_validator(mode='after')
    def validate_record(self):
        if not self.content or self.content != normalize_display(self.content):
            raise ValueError('Content must be nonempty normalized display text')
        if self.id != make_post_id(self.content):
            raise ValueError('IDENTITY_CONFLICT')
        if not self.crawled_at.endswith('Z'):
            raise ValueError('Timestamp must be UTC with Z')
        stamp = datetime.fromisoformat(self.crawled_at)
        if stamp.utcoffset() != timezone.utc.utcoffset(stamp):
            raise ValueError('Timestamp must be UTC')
        if self.source_url:
            url = urlsplit(self.source_url)
            if url.scheme != 'https' or url.hostname not in {'facebook.com', 'www.facebook.com'} or url.username or url.password:
                raise ValueError('Source must be a Facebook HTTPS URL')
        return self


@dataclass(frozen=True)
class ExtractedPost:
    content: str
    source_url: str = ''
    post_kind: PostKind = PostKind.UNKNOWN


@dataclass(frozen=True)
class ExtractionResult:
    signature: str
    post: ExtractedPost | None = None
    error_code: str | None = None
    # A retry correlation token, never a post identity or a seen-post counter.
    observation_id: str = ''

    def __post_init__(self):
        if self.post is not None and self.error_code is not None:
            raise ValueError('Extraction cannot be both successful and failed')


@dataclass(frozen=True)
class FeedProgress:
    signatures: frozenset[str]
    advanced: bool = False


@dataclass
class CrawlStats:
    seen_posts: int = 0
    saved_posts: int = 0
    duplicate_posts: int = 0
    candidate_segments: int = 0
    extraction_errors: int = 0
    media_only_posts: int = 0
    scrolls: int = 0
    no_progress_rounds: int = 0


@dataclass
class CrawlSummary:
    run_id: str
    status: str
    stop_reason: str
    stats: CrawlStats
    exit_code: int
