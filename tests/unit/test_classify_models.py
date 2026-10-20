import pytest
from pydantic import ValidationError

from src.classify.models import (
    ClassificationMetadata, FailureRecord, LabeledRecord, ModelPrediction,
)
from src.identity import make_confession_id


def prediction(**changes):
    return dict(label='Neutral', confidence=0.9, reason='Chuyện cá nhân.',
                target_relation='unrelated', mixed=False, needs_review=False) | changes


@pytest.mark.parametrize('field', list(prediction()))
def test_prediction_requires_every_field(field):
    values = prediction()
    del values[field]
    with pytest.raises(ValidationError):
        ModelPrediction(**values)


@pytest.mark.parametrize('changes', [
    {'extra': 1}, {'label': 'neutral'}, {'target_relation': 'unknown'},
    {'reason': ' \n\t'}, {'reason': 'x' * 301}, {'mixed': 'false'},
    {'mixed': 0}, {'needs_review': 'true'}, {'needs_review': 1},
    *({'confidence': value} for value in ['0.9', True, -0.1, 1.1, float('nan'), float('inf'), -float('inf')]),
])
def test_prediction_rejects_invalid_response(changes):
    with pytest.raises(ValidationError):
        ModelPrediction(**prediction(**changes))


@pytest.mark.parametrize('confidence', [0, 1, 0.5])
@pytest.mark.parametrize('reason', [' x ', ' ' + 'x' * 300 + ' '])
def test_prediction_accepts_boundaries_and_strips_reason(confidence, reason):
    result = ModelPrediction(**prediction(confidence=confidence, reason=reason))
    assert result.confidence == confidence
    assert result.reason == reason.strip()


@pytest.mark.parametrize('relation', ['unrelated', 'uncertain'])
@pytest.mark.parametrize('label', ['Positive', 'Negative'])
def test_prediction_rejects_non_neutral_unrelated_or_uncertain(relation, label):
    with pytest.raises(ValidationError):
        ModelPrediction(**prediction(target_relation=relation, label=label))


def label_values(**changes):
    content = 'Phòng học\nrất sạch.'
    return dict(id=make_confession_id(content), content=content, label='Positive',
                confidence=0.8, reason='Cơ sở vật chất tốt.', needs_review=True,
                review_reasons=('MANUAL_ACCEPT', 'SHORT_CONTENT')) | changes


def test_label_csv_round_trip_preserves_unicode_and_exact_booleans():
    record = LabeledRecord(**label_values())
    row = record.to_csv_row()
    assert row['needs_review'] == 'true'
    assert row['review_reasons'] == 'MANUAL_ACCEPT;SHORT_CONTENT'
    assert LabeledRecord.from_csv_row(row) == record
    plain = LabeledRecord(**label_values(needs_review=False, review_reasons=()))
    assert plain.to_csv_row()['needs_review'] == 'false'
    assert LabeledRecord.from_csv_row(plain.to_csv_row()) == plain


@pytest.mark.parametrize('surrogate', ['\ud800', '\udfff'])
@pytest.mark.parametrize('model,values', [(ModelPrediction, prediction), (LabeledRecord, label_values)])
def test_prediction_and_persisted_reason_reject_unpaired_surrogates(surrogate, model, values):
    with pytest.raises(ValidationError):
        model(**values(reason='Invalid ' + surrogate))


def test_label_reason_preserves_non_bmp_unicode_for_utf8_csv():
    reason = 'Cơ sở vật chất tốt. \U0001f642'
    record = LabeledRecord(**label_values(reason=reason))
    row = record.to_csv_row()
    assert row['reason'].encode('utf-8') == reason.encode('utf-8')
    assert LabeledRecord.from_csv_row(row).reason == reason


@pytest.mark.parametrize('changes', [
    {'id': 'cfs_' + '0' * 64}, {'content': ' unnormalized '},
    {'content': ''}, {'review_reasons': ('UNKNOWN',)},
    {'review_reasons': ('SHORT_CONTENT', 'MANUAL_ACCEPT')},
    {'review_reasons': ('SHORT_CONTENT', 'SHORT_CONTENT')},
    {'needs_review': False},
])
def test_label_rejects_identity_or_review_corruption(changes):
    with pytest.raises(ValidationError):
        LabeledRecord(**label_values(**changes))


@pytest.mark.parametrize('boolean', ['True', 'False', '1', '0', '', ' false'])
def test_label_csv_rejects_noncanonical_booleans(boolean):
    row = LabeledRecord(**label_values()).to_csv_row() | {'needs_review': boolean}
    with pytest.raises(ValueError):
        LabeledRecord.from_csv_row(row)


@pytest.mark.parametrize('changes', [
    {'confidence': 'nan'}, {'confidence': 'inf'}, {'confidence': '1.1'},
    {'reason': ' padded '}, {'review_reasons': 'SHORT_CONTENT;MANUAL_ACCEPT'},
    {'review_reasons': 'MANUAL_ACCEPT;SHORT_CONTENT;SHORT_CONTENT'},
    {'unexpected': 'value'},
])
def test_label_csv_rejects_corrupt_values(changes):
    row = LabeledRecord(**label_values()).to_csv_row() | changes
    with pytest.raises(ValueError):
        LabeledRecord.from_csv_row(row)


def test_json_prediction_does_not_use_csv_coercion():
    import json
    with pytest.raises(ValidationError):
        ModelPrediction.model_validate_json(json.dumps(prediction(confidence='0.8')))


def failure_values(**changes):
    return dict(id=make_confession_id('Nội dung.'), error_code='MODEL_INVALID_RESPONSE',
                attempts=0, last_attempt_at='2026-10-20T01:02:03Z') | changes


@pytest.mark.parametrize('changes', [
    {'attempts': -1}, {'attempts': True}, {'attempts': '1'},
    {'last_attempt_at': '2026-10-20T01:02:03'},
    {'last_attempt_at': '2026-10-20T01:02:03+07:00'},
    {'last_attempt_at': 'bad'},
])
def test_failure_rejects_invalid_attempts_or_timestamp(changes):
    with pytest.raises(ValidationError):
        FailureRecord(**failure_values(**changes))


def test_failure_csv_round_trip():
    record = FailureRecord(**failure_values())
    assert record.to_csv_row()['attempts'] == '0'
    assert FailureRecord.from_csv_row(record.to_csv_row()) == record


@pytest.mark.parametrize('attempts', ['01', '-0', '1.0', '+1', ' 1'])
def test_failure_csv_rejects_noncanonical_attempts(attempts):
    row = FailureRecord(**failure_values()).to_csv_row() | {'attempts': attempts}
    with pytest.raises(ValueError):
        FailureRecord.from_csv_row(row)


@pytest.mark.parametrize('changes', [
    {'schema_version': True}, {'schema_version': 2},
    {'classification_fingerprint': 'bad'}, {'input_generation_id': ''},
    {'created_at': '2026-10-20'},
])
def test_metadata_rejects_invalid_identity_and_timestamp(changes):
    values = dict(schema_version=1, classification_fingerprint='a' * 64,
                  fingerprint_payload={}, input_generation_id='generation-1',
                  created_at='2026-10-20T00:00:00Z') | changes
    with pytest.raises(ValidationError):
        ClassificationMetadata(**values)
