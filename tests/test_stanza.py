"""Coverage for `debkit.repo.stanza`."""

from __future__ import annotations

import pytest

from debkit.repo.stanza import parse_stanza


@pytest.mark.parametrize(
    "separator",
    ["\n\n", "\n\n\n", "\n\n\n\n", "\r\n\r\n"],
    ids=["one-blank", "two-blank", "three-blank", "crlf-blank"],
)
def test_parse_stanza_reads_the_first_paragraph_and_reports_another(separator: str) -> None:
    """A blank-line run of any length ends the paragraph, as apt and dpkg read it."""
    parsed, more_paragraphs, unreadable = parse_stanza(f"A: 1{separator}B: 2\n")

    assert dict(parsed.items()) == {"A": "1"}
    assert more_paragraphs
    assert not unreadable


def test_parse_stanza_ignores_leading_and_trailing_blank_lines() -> None:
    """A blank line before the first or after the last field is no paragraph break."""
    parsed, more_paragraphs, _ = parse_stanza("\n\nA: 1\nB: 2\n\n\n")

    assert dict(parsed.items()) == {"A": "1", "B": "2"}
    assert not more_paragraphs


@pytest.mark.parametrize("line", [" ", "\t"], ids=["space", "tab"])
def test_parse_stanza_reads_a_whitespace_line_as_a_continuation(line: str) -> None:
    """A line of only spaces or tabs continues the field before it, as apt reads it."""
    parsed, more_paragraphs, unreadable = parse_stanza(f"Description: x\n{line}\n more\nB: 2\n")

    assert parsed["B"] == "2"
    assert "more" in parsed["Description"]
    assert not more_paragraphs
    assert not unreadable


def test_parse_stanza_reads_crlf_line_ends() -> None:
    parsed, more_paragraphs, unreadable = parse_stanza("A: 1\r\nB: 2\r\n")

    assert dict(parsed.items()) == {"A": "1", "B": "2"}
    assert not more_paragraphs
    assert not unreadable


@pytest.mark.parametrize(
    "text",
    ["A: 1\nnocolon\nB: 2\n", "A: 1\nB : 2\nC: 3\n", "Format: 1.8\r\rArchitecture: source\n"],
    ids=["no-colon", "space-before-colon", "bare-cr-blank"],
)
def test_parse_stanza_reports_a_line_that_hides_the_fields_after_it(text: str) -> None:
    """The email parser stops at a line it cannot read as a field and drops the rest."""
    _, more_paragraphs, unreadable = parse_stanza(text)

    assert unreadable
    assert not more_paragraphs


def test_parse_stanza_reads_empty_input_as_no_fields() -> None:
    parsed, more_paragraphs, unreadable = parse_stanza("")

    assert dict(parsed.items()) == {}
    assert not more_paragraphs
    assert not unreadable
