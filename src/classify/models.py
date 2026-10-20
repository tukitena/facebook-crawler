"""Strict model responses and persisted classification contracts."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.identity import make_confession_id, normalize_display
from src.preprocess.models import Confession, ConfessionID, PreprocessManifest, SHA256, SOURCE_FLAGS

LABELED_FIELDS = ('id', 'content', 'label', 'confidence', 'reason', 'needs_review', 'review_reasons')
FAILURE_FIELDS = ('id', 'error_code', 'attempts', 'last_attempt_at')
METADATA_SCHEMA_VERSION = 1
REVIEW_REASONS = SOURCE_FLAGS | {'LOW_CONFIDENCE', 'UNCERTAIN_TARGET', 'MIXED_IMPACT', 'MODEL_UNCERTAIN'}
Label = Literal['Positive', 'Neutral', 'Negative']
Confidence = Annotated[float, Field(ge=0, le=1)]
Nonempty = Annotated[str, Field(min_length=1)]


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False, frozen=True)


class PredictionFields(StrictRecord):
    label: Label
    confidence: Confidence
    reason: str

    @field_validator('reason')
    @classmethod
    def clean_reason(cls, reason: str) -> str:
        reason = reason.strip()
        if not 1 <= len(reason) <= 300:
            raise ValueError('Reason must contain 1 to 300 characters after stripping')
        return reason


class ModelPrediction(PredictionFields):
    target_relation: Literal['related', 'unrelated', 'uncertain']
    mixed: bool
    needs_review: bool

    @model_validator(mode='after')
    def validate_relation(self):
        if self.target_relation != 'related' and self.label != 'Neutral':
            raise ValueError('Unrelated or uncertain targets require a Neutral label')
        return self


class LabelDecision(PredictionFields):
    needs_review: bool
    review_reasons: tuple[str, ...]

    @field_validator('review_reasons')
    @classmethod
    def validate_review_reasons(cls, reasons):
        if set(reasons) - REVIEW_REASONS or reasons != tuple(sorted(set(reasons))):
            raise ValueError('Review reasons must be recognized, sorted and unique')
        return reasons

    @model_validator(mode='after')
    def validate_review_state(self):
        if self.needs_review != bool(self.review_reasons):
            raise ValueError('Review state must agree with review reasons')
        return self


class LabeledRecord(LabelDecision):
    id: ConfessionID
    content: str

    @model_validator(mode='after')
    def validate_identity(self):
        if not self.content or self.content != normalize_display(self.content):
            raise ValueError('Content must be nonempty normalized display text')
        if self.id != make_confession_id(self.content):
            raise ValueError('IDENTITY_CONFLICT')
        return self

    def to_csv_row(self) -> dict[str, str]:
        return dict(id=self.id, content=self.content, label=self.label,
                    confidence=str(self.confidence), reason=self.reason,
                    needs_review='true' if self.needs_review else 'false',
                    review_reasons=';'.join(self.review_reasons))

    @classmethod
    def from_csv_row(cls, row: Mapping[str, str]) -> 'LabeledRecord':
        _validate_csv_fields(row, LABELED_FIELDS)
        if row['needs_review'] not in {'true', 'false'}:
            raise ValueError('CSV boolean must be true or false')
        values = dict(row)
        values['confidence'] = float(row['confidence'])
        values['needs_review'] = row['needs_review'] == 'true'
        values['review_reasons'] = tuple(row['review_reasons'].split(';')) if row['review_reasons'] else ()
        result = cls.model_validate(values)
        if result.to_csv_row() != row:
            raise ValueError('Noncanonical labeled CSV value')
        return result


def _validate_csv_fields(row: Mapping[str, str], fields: tuple[str, ...]) -> None:
    if set(row) != set(fields) or any(not isinstance(value, str) for value in row.values()):
        raise ValueError('Wrong CSV fields')


def _validate_utc(value: str) -> str:
    if (not value.endswith('Z') or 'T' not in value
            or datetime.fromisoformat(value).utcoffset() != timezone.utc.utcoffset(None)):
        raise ValueError('Timestamp must be UTC ISO with Z')
    return value


class FailureRecord(StrictRecord):
    id: ConfessionID
    error_code: Annotated[str, Field(pattern=r'^[A-Z][A-Z0-9_]*$')]
    attempts: Annotated[int, Field(ge=0)]
    last_attempt_at: str

    @field_validator('last_attempt_at')
    @classmethod
    def validate_timestamp(cls, value):
        return _validate_utc(value)

    def to_csv_row(self) -> dict[str, str]:
        return dict(id=self.id, error_code=self.error_code, attempts=str(self.attempts),
                    last_attempt_at=self.last_attempt_at)

    @classmethod
    def from_csv_row(cls, row: Mapping[str, str]) -> 'FailureRecord':
        _validate_csv_fields(row, FAILURE_FIELDS)
        result = cls.model_validate(dict(row) | {'attempts': int(row['attempts'])})
        if result.to_csv_row() != row:
            raise ValueError('Noncanonical failure CSV value')
        return result


class ModelIdentity(StrictRecord):
    tag: Nonempty
    digest: Nonempty
    server_version: Nonempty


@dataclass(frozen=True)
class InputSnapshot:
    manifest: PreprocessManifest
    confessions: tuple[Confession, ...]
    flags_by_id: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class PromptBundle:
    system: str
    schema: dict
    prompt_sha256: str
    schema_sha256: str


class ClassificationMetadata(StrictRecord):
    schema_version: Literal[1]
    classification_fingerprint: SHA256
    fingerprint_payload: dict[str, object]
    input_generation_id: Nonempty
    created_at: str

    @field_validator('schema_version', mode='before')
    @classmethod
    def validate_version(cls, value):
        if type(value) is not int:
            raise ValueError('Schema version must be an integer')
        return value

    @field_validator('created_at')
    @classmethod
    def validate_timestamp(cls, value):
        return _validate_utc(value)


@dataclass
class ClassificationStats:
    total: int = 0
    already_done: int = 0
    attempted: int = 0
    newly_saved: int = 0
    failed: int = 0
    remaining: int = 0
    reviewed_new: int = 0
    reviewed_total: int = 0


@dataclass
class ClassificationSummary:
    run_id: str
    stats: ClassificationStats
    status: str
    stop_reason: str
    exit_code: int
