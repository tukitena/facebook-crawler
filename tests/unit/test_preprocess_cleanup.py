from importlib import import_module

import pytest


@pytest.mark.parametrize('raw,want,reasons', [
    ('#1 A', '#1 A', []),
    ('#1 A\nSee less', '#1 A', ['UI_CONTROL']),
    ('#1 A Ẩn bớt', '#1 A', ['UI_CONTROL']),
    ('#1 A\n#UITconfessions', '#1 A', ['POST_FOOTER']),
    ('#1 A #UITconfessions:', '#1 A', ['POST_FOOTER']),
    ('#1 A\n#UITconfessions:\nSee less', '#1 A', ['UI_CONTROL', 'POST_FOOTER']),
    ('#1 A #UITconfessions Ẩn bớt', '#1 A', ['UI_CONTROL', 'POST_FOOTER']),
    ('#1 A\n#UITconfessions\nSee less Ẩn bớt\n \t', '#1 A', ['UI_CONTROL', 'UI_CONTROL', 'POST_FOOTER']),
    ('#1 A\tSee less', '#1 A', ['UI_CONTROL']),
    ('#1 See less trong câu.\nNút Ẩn bớt ở đây.', '#1 See less trong câu.\nNút Ẩn bớt ở đây.', []),
    ('#1 A #UITconfessions ở giữa.', '#1 A #UITconfessions ở giữa.', []),
    ('#1 A #UITConfessions', '#1 A #UITConfessions', []),
    ('#1 A #Ngmay', '#1 A #Ngmay', []),
    ('#1 A #goctimdo', '#1 A #goctimdo', []),
    ('#1 ASee less', '#1 ASee less', []),
    ('#1 A#UITconfessions', '#1 A#UITconfessions', []),
    ('#1 "See less"', '#1 "See less"', []),
    ('#1 "#UITconfessions"', '#1 "#UITconfessions"', []),
])
def test_cleanup_only_exact_terminal_suffixes(raw, want, reasons):
    cleanup = import_module('src.preprocess.cleanup')
    result = cleanup.cleanup_tail(raw)
    assert '\n'.join(line.text for line in result.lines) == want
    assert result.raw_content == raw
    assert [item.reason for item in result.removed] == reasons


def test_cleanup_keeps_original_line_positions_and_excerpts():
    cleanup = import_module('src.preprocess.cleanup')
    raw = '#001 Xin chào\n#2 Tạm biệt\n#UITconfessions:\nSee less'
    result = cleanup.cleanup_tail(raw)
    assert [(line.line_no, line.text) for line in result.lines] == [(1, '#001 Xin chào'), (2, '#2 Tạm biệt')]
    assert [(r.reason, r.start_line, r.end_line, r.excerpt) for r in result.removed] == [
        ('UI_CONTROL', 4, 4, 'See less'), ('POST_FOOTER', 3, 3, '#UITconfessions:')]


def test_cleanup_same_line_and_empty_segment():
    cleanup = import_module('src.preprocess.cleanup')
    splitter = import_module('src.preprocess.splitter')
    cleaned = cleanup.cleanup_tail('#1 A #UITconfessions Ẩn bớt')
    segment = splitter.split_confessions(cleaned).segments[0]
    assert (segment.content, segment.start_line, segment.end_line) == ('A', 1, 1)
    assert [(r.start_line, r.end_line) for r in cleaned.removed] == [(1, 1), (1, 1)]
    empty = splitter.split_confessions(cleanup.cleanup_tail('#1\n#UITconfessions\nẨn bớt'))
    assert empty.segments == () and empty.empty_segments == 1
    assert [(r.reason, r.start_line, r.end_line) for r in empty.excluded] == [('EMPTY_SEGMENT', 1, 1)]


@pytest.mark.parametrize('ui', ['', ' See less', '\nẨn bớt'])
def test_exact_terminal_linked_footer(ui):
    cleanup = import_module('src.preprocess.cleanup')
    splitter = import_module('src.preprocess.splitter')
    footer = '#UITconfessions: bit.ly/UITConfessions'
    raw = '#001 Nội dung tổng hợp\n' + footer + ui
    result = cleanup.cleanup_tail(raw)
    assert result.raw_content == raw
    assert [(r.reason, r.start_line, r.end_line, r.excerpt) for r in result.removed if r.reason == 'POST_FOOTER'] == [
        ('POST_FOOTER', 2, 2, footer)]
    segment = splitter.split_confessions(result).segments[0]
    assert (segment.content, segment.start_line, segment.end_line) == ('Nội dung tổng hợp', 1, 1)
    empty = splitter.split_confessions(cleanup.cleanup_tail('#1\n' + footer + ui))
    assert not empty.segments and empty.empty_segments == 1


@pytest.mark.parametrize('body', [
    'Nhắc tới #UITconfessions: bit.ly/UITConfessions',
    '#UITconfessions: bit.ly/UITConfessions\nCòn nội dung',
    '"#UITconfessions: bit.ly/UITConfessions"',
    '#UITconfessions: bit.ly/UITConfessions/other',
    '#UITconfessions: https://bit.ly/UITConfessions',
    '#UITConfessions: bit.ly/UITConfessions',
    '#UITconfessions: bit.ly/OtherForm',
])
def test_linked_footer_cleanup_does_not_expand_to_other_text(body):
    cleanup = import_module('src.preprocess.cleanup')
    raw = '#1 Nội dung\n' + body
    cleaned = cleanup.cleanup_tail(raw)
    assert '\n'.join(line.text for line in cleaned.lines) == raw and not cleaned.removed
