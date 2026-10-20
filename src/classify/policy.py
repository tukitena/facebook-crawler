"""Deterministic review signals; never changes a predicted impact label."""

import math
from typing import Sequence

from src.classify.models import LabelDecision, ModelPrediction
from src.preprocess.models import SOURCE_FLAGS

REVIEW_POLICY_VERSION = 'uit_impact_v1'


def apply_review_policy(prediction: ModelPrediction, source_flags: Sequence[str],
                        *, threshold: float = 0.70) -> LabelDecision:
    if (type(threshold) not in {int, float} or not math.isfinite(threshold)
            or not 0 <= threshold <= 1):
        raise ValueError('Threshold must be a finite number between zero and one')
    reasons = set(source_flags)
    if reasons - SOURCE_FLAGS:
        raise ValueError('Unrecognized source flag')
    if prediction.confidence < threshold:
        reasons.add('LOW_CONFIDENCE')
    if prediction.target_relation == 'uncertain':
        reasons.add('UNCERTAIN_TARGET')
    if prediction.mixed:
        reasons.add('MIXED_IMPACT')
    if prediction.needs_review:
        reasons.add('MODEL_UNCERTAIN')
    return LabelDecision(label=prediction.label, confidence=prediction.confidence,
                         reason=prediction.reason, needs_review=bool(reasons),
                         review_reasons=tuple(sorted(reasons)))
