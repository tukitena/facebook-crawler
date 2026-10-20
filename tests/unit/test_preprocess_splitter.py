from importlib import import_module

import pytest


def split(text, **kwargs):
    cleanup = import_module('src.preprocess.cleanup')
    splitter = import_module('src.preprocess.splitter')
    return splitter.split_confessions(cleanup.cleanup_tail(text), **kwargs)


@pytest.mark.parametrize('text,expected', [
    ('#1\nCảm ơn UIT.\n#2 Mình đang tìm bạn học.', [('1', 'Cảm ơn UIT.'), ('2', 'Mình đang tìm bạn học.')]),
    ('#1 I have 3 cats #1, #2, and #3.', [('1', 'I have 3 cats #1, #2, and #3.')]),
    ('#1 A\n--------\nLời admin\n#2 B', [('1', 'A'), ('2', 'B')]),
    ('Lời dẫn\n#1: A\n#2. B', [('1', 'A'), ('2', 'B')]),
    ('#1\n#2 B', [('2', 'B')]),
    ('Admin thông báo đổi ảnh đại diện.', []),
    ('#1 Admin ơi, thư viện UIT hỗ trợ rất tốt.', [('1', 'Admin ơi, thư viện UIT hỗ trợ rất tốt.')]),
    ('#1 A\n-\nB', [('1', 'A\n-\nB')]),
    ('#1 A\n--\nB', [('1', 'A')]),
    ('#1 A\n  --  \n#2 B', [('1', 'A'), ('2', 'B')]),
    ('#1 A\n-------\nB', [('1', 'A')]),
    ('#1 A -------- B', [('1', 'A -------- B')]),
    ('#1 -10 điểm vì nộp muộn.', [('1', '-10 điểm vì nộp muộn.')]),
    ('#10 Xin chào\n#11 Xin chào', [('10', 'Xin chào'), ('11', 'Xin chào')]),
    ('#123 ABC xyz\n#123 FHG GBC', [('123', 'ABC xyz'), ('123', 'FHG GBC')]),
    ('#001 A\n#2 B', [('001', 'A'), ('2', 'B')]),
    ('#123 ABC xyz\n#123 ABC xyz', [('123', 'ABC xyz'), ('123', 'ABC xyz')]),
    ('#1 Tôi trích lại: > #2 chuyện cũ', [('1', 'Tôi trích lại: > #2 chuyện cũ')]),
    ('#1 A\n#2 B\n#UITconfessions', [('1', 'A'), ('2', 'B')]),
    ('#1 Tôi nhắc #UITconfessions trong câu.\nNút See less hoặc Ẩn bớt nằm trong lời kể.',
     [('1', 'Tôi nhắc #UITconfessions trong câu.\nNút See less hoặc Ẩn bớt nằm trong lời kể.')]),
    ('#1 :)', [('1', ':)')]), ('#1:abc', [('1', ':abc')]),
    ('#1 .abc', [('1', '.abc')]), ('#1 – Text', [('1', 'Text')]),
    ('#1 — Text', [('1', 'Text')]), ('#1 - Text', [('1', 'Text')]),
    ('#1 –10', [('1', '–10')]), ('#1 —10', [('1', '—10')]),
    ('#2 B\n#1 A', [('2', 'B'), ('1', 'A')]),
])
def test_marker_and_body_examples(text, expected):
    assert [(s.marker, s.content) for s in split(text).segments] == expected


@pytest.mark.parametrize('text', ['abc #1', '#123abc', '#UIT123', '##12', '> #12', '#١ A'])
def test_non_markers_do_not_open_confessions(text):
    cleanup = import_module('src.preprocess.cleanup')
    splitter = import_module('src.preprocess.splitter')
    assert not splitter.has_marker(cleanup.cleanup_tail(text))
    assert split(text).segments == ()


@pytest.mark.parametrize('separator', ['--', '---', '--------', '  --', '--  ', '\t--\t'])
def test_separator_is_an_entire_ascii_hyphen_line(separator):
    result = split('#1 A\n' + separator + '\nB\n#2 C')
    assert [s.content for s in result.segments] == ['A', 'C']
    assert result.separators == 1
    assert [(r.reason, r.start_line, r.end_line, r.excerpt) for r in result.excluded] == [('AFTER_SEPARATOR', 3, 3, 'B')]


@pytest.mark.parametrize('line', ['-', '- -', '———', 'A -- B', '-- ghi chú'])
def test_non_separator_lines_remain_in_body(line):
    result = split('#1 A\n' + line + '\nB')
    assert result.segments[0].content == 'A\n' + line + '\nB'
    assert result.separators == 0


def test_preamble_audit_spans_and_blank_regions():
    result = split('Lời dẫn\n\n#1\nNội dung\n\n--\n\nLời ngoài\n#2 B')
    assert [(s.start_line, s.end_line) for s in result.segments] == [(3, 4), (9, 9)]
    assert [(r.reason, r.start_line, r.end_line) for r in result.excluded] == [
        ('PREAMBLE', 1, 1), ('AFTER_SEPARATOR', 8, 8)]
    assert split('\n\n#1 A\n--\n\n').excluded == ()


def test_repeated_marker_flags_all_occurrences_without_dedup():
    result = split('#001 A\n#1 B\n#001 A')
    assert len(result.segments) == 3
    assert [s.flags for s in result.segments] == [
        ('REPEATED_MARKER', 'SHORT_CONTENT'), ('SHORT_CONTENT',), ('REPEATED_MARKER', 'SHORT_CONTENT')]
    assert result.candidate_segments == 3


def test_short_content_boundary_and_empty_segments():
    result = split('#1 123456789\n#2 1234567890\n#3 😊\n#4\n#5 .')
    assert [s.flags for s in result.segments] == [('SHORT_CONTENT',), (), ('SHORT_CONTENT',)]
    assert result.candidate_segments == 5 and result.empty_segments == 2
    assert [r.reason for r in result.excluded] == ['EMPTY_SEGMENT', 'EMPTY_SEGMENT']
    assert split('#1 A', short_content_chars=1).segments[0].flags == ()


def test_audit_excerpt_is_bounded_and_body_blank_lines_preserved():
    result = split('x' * 250 + '\n#1 First\n\nSecond')
    assert result.excluded[0].excerpt == 'x' * 200
    assert result.segments[0].content == 'First\n\nSecond'
