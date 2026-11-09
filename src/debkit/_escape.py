"""Control-character escaping for untrusted text bound for a terminal.

Repository content (Release fields, storage keys) could carry an ANSI escape that rewrites the
terminal, or a newline that forges a line (CWE-117); log lines and brief output escape it the
same way → [CONFIG.md](CONFIG.md#output-flag). A leaf: it imports no `debkit` module.
"""

from __future__ import annotations

# C0 and C1 control characters and DEL, except tab, each become a visible ``\xNN``.
CONTROL_ESCAPES = {
    code: f"\\x{code:02x}" for code in (*range(0x20), *range(0x7F, 0xA0)) if code != ord("\t")
}


def escape_control(text: str) -> str:
    r"""Replace each C0/C1 control character but tab in `text` with ``\xNN``.

    Args:
        text: Untrusted text.

    Returns:
        The text, safe to write to a terminal on one line.
    """
    return text.translate(CONTROL_ESCAPES)
