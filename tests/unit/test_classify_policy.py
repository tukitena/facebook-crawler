import pytest

from src.classify.models import ModelPrediction
from src.classify.policy import apply_review_policy


def prediction(**changes):
    values = dict(label='Negative', confidence=0.9, reason='Phàn nàn phòng học.',
                  target_relation='related', mixed=False, needs_review=False)
    return ModelPrediction(**(values | changes))


def test_review_merges_sources_without_changing_label():
    result = apply_review_policy(prediction(confidence=0.69, mixed=True),
                                 ['SHORT_CONTENT', 'SHORT_CONTENT'])
    assert result.label == 'Negative'
    assert result.confidence == 0.69
    assert result.reason == 'Phàn nàn phòng học.'
    assert result.review_reasons == ('LOW_CONFIDENCE', 'MIXED_IMPACT', 'SHORT_CONTENT')
    assert result.needs_review is True


@pytest.mark.parametrize('confidence,reasons', [(0.69, ('LOW_CONFIDENCE',)), (0.70, ())])
def test_review_threshold_is_strict_less_than(confidence, reasons):
    result = apply_review_policy(prediction(confidence=confidence), [])
    assert result.review_reasons == reasons
    assert result.needs_review is bool(reasons)


def test_all_review_sources_are_sorted_and_deduplicated():
    result = apply_review_policy(prediction(label='Neutral', confidence=0.5,
                                            target_relation='uncertain', mixed=True,
                                            needs_review=True),
                                 ['SHORT_CONTENT', 'REPEATED_MARKER', 'MANUAL_ACCEPT'])
    assert result.review_reasons == ('LOW_CONFIDENCE', 'MANUAL_ACCEPT', 'MIXED_IMPACT',
                                     'MODEL_UNCERTAIN', 'REPEATED_MARKER',
                                     'SHORT_CONTENT', 'UNCERTAIN_TARGET')


def test_unrelated_neutral_does_not_automatically_require_review():
    result = apply_review_policy(prediction(label='Neutral', target_relation='unrelated'), [])
    assert result.review_reasons == ()
    assert result.needs_review is False


def test_custom_threshold_is_used():
    assert apply_review_policy(prediction(confidence=0.8), [], threshold=0.85).needs_review


def test_unknown_source_flags_are_rejected():
    with pytest.raises(ValueError):
        apply_review_policy(prediction(), ['UNKNOWN'])


@pytest.mark.parametrize('threshold', [True, '0.7', -0.1, 1.1, float('nan')])
def test_invalid_threshold_is_rejected(threshold):
    with pytest.raises(ValueError):
        apply_review_policy(prediction(), [], threshold=threshold)
