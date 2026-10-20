"""Explicit post eligibility rules and validated operator overrides."""

from collections.abc import Collection
import csv
from pathlib import Path
import re

from src.config import PreprocessConfig
from src.errors import AppError
from src.models import PostKind, RawPost
from src.preprocess.models import CleanedPost, EligibilityDecision, FilterPolicy, PostOverride
from src.preprocess.splitter import has_marker

_KIND_REASONS = {
    PostKind.PROFILE_UPDATE: 'NON_CONFESSION_EVENT',
    PostKind.COVER_UPDATE: 'NON_CONFESSION_EVENT',
    PostKind.SHARED: 'SHARED_POST',
    PostKind.UNKNOWN: 'UNKNOWN_POST_KIND',
}


def build_filter_policy(config: PreprocessConfig) -> FilterPolicy:
    try:
        return FilterPolicy(tuple(re.compile(pattern, re.MULTILINE) for pattern in config.excluded_post_patterns))
    except re.error as exc:
        raise AppError('CONFIG_ERROR', 'Regex loại bài không hợp lệ.', 2) from exc


def load_overrides(path: Path | None, post_ids: Collection[str]) -> dict[str, PostOverride]:
    if path is None:
        return {}
    known = set(post_ids)
    result: dict[str, PostOverride] = {}
    try:
        with Path(path).open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream, strict=True)
            if next(reader, None) != ['post_id', 'decision', 'note']:
                raise ValueError('Wrong header')
            for row in reader:
                if len(row) != 3:
                    raise ValueError('Wrong field count')
                post_id, decision, note = row
                if post_id not in known or post_id in result or decision not in {'accept', 'reject'}:
                    raise ValueError('Invalid or duplicate override')
                result[post_id] = PostOverride(decision, note)
    except (ValueError, UnicodeError, csv.Error) as exc:
        raise AppError('CONFIG_ERROR', 'Override sai schema, ID hoặc quyết định; kiểm tra file override.', 2) from exc
    except OSError as exc:
        raise AppError('STORAGE_ERROR', f'Không đọc được override: {path}') from exc
    return result


def evaluate_post(post: RawPost, cleaned: CleanedPost, policy: FilterPolicy,
                  override: PostOverride | None = None) -> EligibilityDecision:
    if override and override.decision == 'reject':
        return EligibilityDecision(False, 'REJECTED_OVERRIDE')
    manual = override is not None and override.decision == 'accept'
    if not manual:
        if reason := _KIND_REASONS.get(post.post_kind):
            return EligibilityDecision(False, reason)
        if any(pattern.search(post.content) for pattern in policy.excluded_post_patterns):
            return EligibilityDecision(False, 'EXCLUDED_PATTERN')
    if not has_marker(cleaned):
        return EligibilityDecision(False, 'NO_MARKER')
    return EligibilityDecision(True, flags=('MANUAL_ACCEPT',) if manual else ())
