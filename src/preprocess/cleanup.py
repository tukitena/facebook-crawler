"""Remove only recognized terminal UI/footer tokens, retaining raw line numbers."""

import re

from src.preprocess.models import CleanedPost, ExcludedRegion, RawLine

_UI_SUFFIX = re.compile(r'(?m)(?:^|(?<=[ \t]))(?:See less|Ẩn bớt)\Z')
_FOOTER_SUFFIX = re.compile(r'(?m)(?:^|(?<=[ \t]))#UITconfessions:?\Z')
_LINKED_FOOTER_SUFFIX = re.compile(r'(?m)^#UITconfessions: bit\.ly/UITConfessions\Z')


def cleanup_tail(content: str) -> CleanedPost:
    work = content.rstrip()
    removed: list[ExcludedRegion] = []

    def remove(match: re.Match[str], reason: str) -> str:
        start = content.count('\n', 0, match.start()) + 1
        end = content.count('\n', 0, match.end()) + 1
        removed.append(ExcludedRegion(start, end, reason, content[match.start():match.end()][:200]))
        return work[:match.start()].rstrip()

    while match := _UI_SUFFIX.search(work):
        work = remove(match, 'UI_CONTROL')
    if match := (_LINKED_FOOTER_SUFFIX.search(work) or _FOOTER_SUFFIX.search(work)):
        work = remove(match, 'POST_FOOTER')
    lines = tuple(RawLine(i, text) for i, text in enumerate(work.split('\n'), 1)) if work else ()
    return CleanedPost(content, lines, tuple(removed))
