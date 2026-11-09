"""Single-paragraph control files read through the email parser."""

from __future__ import annotations

from email.message import Message
from email.parser import Parser
from typing import NamedTuple


class Stanza(NamedTuple):
    """The first paragraph of a control file, parsed.

    Attributes:
        message: The fields of the first paragraph, in file order, repeats kept.
        more_paragraphs: A field follows an empty line: the file held more than one
            paragraph, which a single-paragraph file must not.
        unreadable: A line of the first paragraph is neither a field, a continuation, nor
            empty, so the email parser stopped there and every field after it is missing.
    """

    message: Message
    more_paragraphs: bool
    unreadable: bool


def parse_stanza(text: str) -> Stanza:
    """Parse the first paragraph of a `.changes`, a `Release`, or one `Sources` stanza.

    Debian Policy 5.1 ends a paragraph at an empty line. CRLF line ends are read as LF and
    leading empty lines are skipped; what a later paragraph or an unreadable line means is
    the caller's rule -> [VALIDATION.md](VALIDATION.md#blank-lines). A line of only spaces
    or tabs is not empty: it continues the field before it, as apt reads it.

    Args:
        text: The decoded file.

    Returns:
        The first paragraph's fields, whether more paragraphs follow, and whether a line
        hid the fields after it.
    """
    lines = text.replace("\r\n", "\n").lstrip("\n").split("\n")
    end = lines.index("") if "" in lines else len(lines)
    message = Parser().parsestr("".join(f"{line}\n" for line in lines[:end]))
    # every line went to the parser as a header; one it cannot read as a field or a
    # continuation ends the headers there and leaves the rest as a payload
    unreadable = bool(message.get_payload()) or bool(message.defects)
    return Stanza(message, any(lines[end:]), unreadable)
