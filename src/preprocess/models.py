"""Text-processing contracts and validated CSV records."""

from dataclasses import dataclass, fields
from datetime import datetime, timezone
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.identity import NORMALIZATION_VERSION, make_confession_id, normalize_display

CONFESSION_FIELDS = ('id', 'content', 'confession_number')
SOURCE_FIELDS = ('confession_id', 'post_id', 'marker', 'start_line', 'end_line', 'source_url', 'flags')
EXCLUDED_FIELDS = ('post_id', 'start_line', 'end_line', 'reason', 'excerpt')
SOURCE_FLAGS = frozenset({'SHORT_CONTENT', 'REPEATED_MARKER', 'MANUAL_ACCEPT'})
EXCLUSION_REASONS = frozenset({
    'NON_CONFESSION_EVENT', 'SHARED_POST', 'UNKNOWN_POST_KIND', 'NO_MARKER',
    'EXCLUDED_PATTERN', 'EMPTY_SEGMENT', 'PREAMBLE', 'AFTER_SEPARATOR',
    'POST_FOOTER', 'UI_CONTROL', 'REJECTED_OVERRIDE',
})
Number = Annotated[str, Field(strict=True, pattern=r'^[0-9]+$')]
PostID = Annotated[str, Field(strict=True, pattern=r'^post_[0-9a-f]{64}$')]
ConfessionID = Annotated[str, Field(strict=True, pattern=r'^cfs_[0-9a-f]{64}$')]
LineNumber = Annotated[int, Field(strict=True, ge=1)]


@dataclass(frozen=True)
class RawLine:
    line_no: int
    text: str


@dataclass(frozen=True)
class ExcludedRegion:
    start_line: int
    end_line: int
    reason: str
    excerpt: str


@dataclass(frozen=True)
class CleanedPost:
    raw_content: str
    lines: tuple[RawLine, ...]
    removed: tuple[ExcludedRegion, ...]


@dataclass(frozen=True)
class Segment:
    content: str
    marker: str
    start_line: int
    end_line: int
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SplitResult:
    segments: tuple[Segment, ...]
    excluded: tuple[ExcludedRegion, ...]
    candidate_segments: int
    empty_segments: int
    separators: int


@dataclass(frozen=True)
class EligibilityDecision:
    accepted: bool
    reason: str | None = None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostOverride:
    decision: Literal['accept', 'reject']
    note: str = ''


@dataclass(frozen=True)
class FilterPolicy:
    excluded_post_patterns: tuple[re.Pattern[str], ...] = ()


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Confession(Record):
    id: ConfessionID
    content: str
    confession_number: Number

    @model_validator(mode='after')
    def validate_body(self):
        if not self.content or self.content != normalize_display(self.content):
            raise ValueError('Content must be nonempty normalized display text')
        if self.id != make_confession_id(self.content):
            raise ValueError('IDENTITY_CONFLICT')
        return self


class SpanRecord(Record):
    start_line: LineNumber
    end_line: LineNumber

    @model_validator(mode='after')
    def validate_span(self):
        if self.end_line < self.start_line:
            raise ValueError('Invalid raw line span')
        return self


class SourceRow(SpanRecord):
    confession_id: ConfessionID
    post_id: PostID
    marker: Number
    source_url: str = ''
    flags: tuple[str, ...] = ()

    @field_validator('flags')
    @classmethod
    def normalize_flags(cls, flags):
        if set(flags) - SOURCE_FLAGS:
            raise ValueError('Unrecognized source flag')
        return tuple(sorted(set(flags)))

    @field_validator('source_url')
    @classmethod
    def validate_source_url(cls, value):
        if value:
            url = urlsplit(value)
            if (url.scheme != 'https' or url.hostname not in {'facebook.com', 'www.facebook.com'}
                    or url.username or url.password):
                raise ValueError('Source must be a Facebook HTTPS URL')
        return value


class ExcludedRow(SpanRecord):
    post_id: PostID
    reason: str
    excerpt: Annotated[str, Field(max_length=200)]

    @field_validator('reason')
    @classmethod
    def validate_reason(cls, reason):
        if reason not in EXCLUSION_REASONS:
            raise ValueError('Unrecognized exclusion reason')
        return reason


@dataclass
class PreprocessStats:
    read_posts: int = 0
    duplicate_posts: int = 0
    accepted_posts: int = 0
    excluded_posts: int = 0
    candidate_segments: int = 0
    empty_segments: int = 0
    unique_confessions: int = 0
    duplicate_confessions: int = 0
    sources: int = 0
    excluded_regions: int = 0
    separators: int = 0
    ui_controls_removed: int = 0
    footers_removed: int = 0


@dataclass
class PreprocessSummary:
    run_id: str
    status: str
    stop_reason: str
    stats: PreprocessStats
    exit_code: int
    generation_id: str | None = None
    published: bool = False


MANIFEST_SCHEMA_VERSION = 1
FILTER_VERSION = 'preprocess_filter_v1'
SPLITTER_VERSION = 'line_start_numeric_v2'
SHA256 = Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{64}$')]
Count = Annotated[int, Field(strict=True, ge=0)]


class OutputInfo(Record):
    sha256: SHA256
    rows: Count


class PreprocessManifest(Record):
    schema_version: Literal[1]
    generation_id: Annotated[str, Field(min_length=1)]
    created_at: str
    page_url: str
    raw_csv_sha256: SHA256
    raw_metadata_sha256: SHA256
    effective_config: dict[str, object]
    effective_config_sha256: SHA256
    overrides_sha256: SHA256 | None
    normalization_version: str
    filter_version: str
    splitter_version: str
    confession_fields: tuple[str, ...]
    outputs: dict[str, OutputInfo]
    counts: dict[str, Count]
    excluded_by_reason: dict[str, Count]

    @field_validator('schema_version', mode='before')
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError('Schema version must be an integer')
        return value

    @model_validator(mode='after')
    def validate_manifest(self):
        if self.confession_fields != CONFESSION_FIELDS:
            raise ValueError('Wrong confession schema')
        if set(self.outputs) != {'confessions.csv', 'sources.csv', 'excluded.csv'}:
            raise ValueError('Wrong generation file set')
        if set(self.counts) != {item.name for item in fields(PreprocessStats)}:
            raise ValueError('Wrong counter set')
        if set(self.excluded_by_reason) - EXCLUSION_REASONS:
            raise ValueError('Unrecognized exclusion reason')
        if not self.created_at.endswith('Z') or datetime.fromisoformat(self.created_at).utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError('Timestamp must be UTC with Z')
        return self
