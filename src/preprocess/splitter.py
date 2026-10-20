"""Line-based confession grammar; no I/O, identity or dataset deduplication."""

from collections import Counter
import re

from src.identity import normalize_display
from src.preprocess.models import CleanedPost, ExcludedRegion, RawLine, Segment, SplitResult

_MARKER = re.compile(r'^[ \t]*#(?P<number>[0-9]+)(?=$|[ \t]|[:.\-–—])[ \t]*(?P<body>.*)$')
_SEPARATOR = re.compile(r'^[ \t]*-{2,}[ \t]*$')


def has_marker(cleaned: CleanedPost) -> bool:
    return any(_MARKER.match(line.text) for line in cleaned.lines)


def split_confessions(cleaned: CleanedPost, *, short_content_chars: int = 10) -> SplitResult:
    marker_counts = Counter(match['number'] for line in cleaned.lines if (match := _MARKER.match(line.text)))
    raw_lines = cleaned.raw_content.split('\n')
    segments: list[Segment] = []
    excluded: list[ExcludedRegion] = []
    buffer: list[RawLine] = []
    outside: list[RawLine] = []
    number: str | None = None
    start_line = 0
    outside_reason = 'PREAMBLE'
    empty_segments = 0
    separators = 0

    def region(start: int, end: int, reason: str) -> ExcludedRegion:
        return ExcludedRegion(start, end, reason, '\n'.join(raw_lines[start - 1:end])[:200])

    def flush_outside() -> None:
        meaningful = [line for line in outside if line.text.strip()]
        if meaningful:
            excluded.append(region(meaningful[0].line_no, meaningful[-1].line_no, outside_reason))
        outside.clear()

    def flush_segment() -> None:
        nonlocal number, empty_segments
        if number is None:
            return
        raw_body = '\n'.join(line.text for line in buffer)
        # Removing a marker can expose leading BOMs that were internal in raw.
        # Strip the leading BOM/whitespace run in one pass, including alternating
        # BOMs and spaces, then apply the shared display normalization to the body.
        body = normalize_display(re.sub(r'^[\s\ufeff]+', '', raw_body)).strip()
        end_line = next((line.line_no for line in reversed(buffer) if line.text.strip()), start_line)
        if not body:
            excluded.append(region(start_line, end_line, 'EMPTY_SEGMENT'))
            empty_segments += 1
        else:
            flags = []
            if len(body) < short_content_chars:
                flags.append('SHORT_CONTENT')
            if marker_counts[number] > 1:
                flags.append('REPEATED_MARKER')
            segments.append(Segment(body, number, start_line, end_line, tuple(sorted(flags))))
        number = None
        buffer.clear()

    for line in cleaned.lines:
        match = _MARKER.match(line.text)
        if match:
            flush_outside()
            flush_segment()
            number, start_line = match['number'], line.line_no
            body = match['body']
            if body and body[0] in ':.\u002d–—' and (len(body) == 1 or body[1].isspace()):
                body = body[1:].lstrip()
            buffer.append(RawLine(line.line_no, body))
        elif _SEPARATOR.match(line.text):
            flush_segment()
            flush_outside()
            separators += 1
            outside_reason = 'AFTER_SEPARATOR'
        elif number is not None:
            buffer.append(line)
        else:
            outside.append(line)
    flush_segment()
    flush_outside()
    return SplitResult(tuple(segments), tuple(excluded), sum(marker_counts.values()), empty_segments, separators)
