#!/usr/bin/env python3
"""Fail when root contract documents drift from code or from each other.

Checks (SETUP.md quality gates; AGENTS.md contract writing rules):

- every ``[text](target)`` in root ``*.md`` and ``docs/**/*.md`` resolves relative to the linking
  document: a ``.md`` target must exist and its ``#anchor`` must be a heading slug; any other
  relative file target must exist. External links, ``]()``, and fenced code blocks are ignored;
- ``CONFIG.md`` flag rows equal the ``add_env_argument`` / ``add_argument`` registrations, flag
  and ``DEBKIT_*`` variable alike, and its variable-only rows equal the ``add_env_variable``
  registrations, in both directions;
- the ``EXCEPTIONS.md`` exception table equals the ``class *Exception`` definitions in the source
  plus the txstore classes ``cli.py`` groups, and every backticked ``*Exception`` name in any
  document is one of them;
- the ``EXCEPTIONS.md`` exit-group table equals the ``except`` handlers of ``cli.py`` and their
  ``err_exit`` messages, and every ``Class (Group)`` notation names the group ``cli.py`` exits
  through, following base classes;
- every exception class defined in the source resolves to a ``cli.py`` exit group, is raised
  somewhere in the source, and is named by a ``pytest.raises`` outside ``tests/test_cli_main.py``,
  whose stubbed exceptions prove only the message mapping (TESTING.md required coverage);
- no document leaves a code fence open, which would hide every later line from every check above;
- no document opens a ``plantuml`` or ``puml`` fence, which GitHub does not render, and every
  ``docs/**/diagrams/*.svg`` is embedded as a Markdown image in a document (SETUP.md diagrams);
- prose lines stay within 100 columns (table rows, fenced blocks, lines carrying a URL, and
  single-token lines are exempt);
- every table row has as many cells as its header;
- root contracts carry no ``TODO``/``FIXME``/``XXX`` outside inline code (``ROADMAP.md`` exempt);
- every ``ROOT.md#anchor`` cited in ``src/`` or ``tests/`` names a root document and a heading;
- every ``docs/<dir>/`` holding pages has a ``README.md`` that links each sibling page;
- the ``README.md`` "Current version" line equals ``[project].version``;
- packages resolve from public PyPI only: ``pyproject.toml`` declares no custom index and every
  ``uv.lock`` registry is ``https://pypi.org/simple``;
- every link into the txstore repository names the tag of the txstore version ``uv.lock`` resolves,
  so a linked contract describes the storage debkit runs on.

Code facts are read from the source tree with ``ast``, never imported, so the checker runs with
only the standard library, as in CI.

Usage:
    uv run python scripts/check_docs.py
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Container
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path("src") / "debkit"
TESTS = Path("tests")
# Raises stubbed exceptions to prove the exit-group messages, so it proves no real raise.
STUB_RAISE_TESTS = frozenset({"test_cli_main.py"})
LINE_LIMIT = 100
PYPI_REGISTRY = "https://pypi.org/simple"
OPTION_CALLS = frozenset({"add_argument", "add_env_argument"})
ENV_ONLY_CALL = "add_env_variable"

# txstore exception classes `cli.py` groups, with their bases → EXCEPTIONS.md#domain-exceptions.
EXTERNAL_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "StorageException": ("Exception",),
    "StorageNotFoundException": ("StorageException",),
}
# Exit groups for failures raised outside `cli.py` handlers: an argparse type converter exits
# through the Usage group → EXCEPTIONS.md#cli-exit-groups.
EXTERNAL_GROUPS: dict[str, str] = {"ArgumentTypeError": "Usage"}

# Root documents allowed to carry TODO-style markers (deviations are recorded there by design).
TODO_EXEMPT: frozenset[str] = frozenset({"ROADMAP.md"})
# Fence info strings of diagrams GitHub does not render; they live as rendered SVG instead.
DIAGRAM_FENCES = frozenset({"plantuml", "puml"})

FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)")
HEADING_RE = re.compile(r"^(#+)\s+(.*?)\s*$")
LINK_RE = re.compile(r"\]\(([^)\s]*)\)")
# An unescaped inline ``![alt](dest)``, with an optional "title", 'title', or (title).
IMAGE_RE = re.compile(
    r"(?<!\\)!\[[^\]]*\]\(\s*([^)\s<>]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# A code span: a backtick run closed by a run of the same length, within one paragraph.
CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(?:(?!\n\s*\n).)+?(?<!`)\1(?!`)", re.DOTALL)
EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)")
TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|?\s*$")
CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
INLINE_CODE_RE = re.compile(r"`[^`]*`")
README_VERSION_RE = re.compile(r"Current version: \*\*([^*]+)\*\*")
TXSTORE_LINK_RE = re.compile(r"https://github\.com/vinklat/txstore/(?:blob|tree)/([^/\s)#]+)")
TODO_RE = re.compile(r"\b(?:TODO|FIXME|XXX)\b")
URL_RE = re.compile(r"https?://")
CODE_REF_RE = re.compile(r"\b([A-Z][A-Z]+\.md)#([a-z0-9-]+)")
FLAG_CELL_RE = re.compile(r"^(?:`-[A-Za-z]`,\s*)?`(--[a-z][a-z0-9-]*)`$")
ENV_CELL_RE = re.compile(r"^(?:`(DEBKIT_[A-Z0-9_]+)`|none)$")
ENV_ONLY_CELL_RE = re.compile(r"^`(DEBKIT_[A-Z0-9_]+)`$")
IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
# ``Class (Group)``, allowing the message form contracts use: `ValueError("...")` (Generic).
GROUP_NOTATION_RE = re.compile(
    r"`([A-Z][A-Za-z0-9]*(?:Exception|Error))(?:\([^`]*\))?`\s*\(([A-Z][A-Za-z]*)\)"
)
EXCEPTION_TOKEN_RE = re.compile(r"`([A-Z][A-Za-z0-9]*Exception)`")
EXCEPTION_CELL_RE = re.compile(r"^`([A-Z][A-Za-z0-9]*Exception)`$")

Lines = list[tuple[int, str]]
Table = tuple[int, list[str], list[tuple[int, list[str]]]]


class Fences(NamedTuple):
    """A document split by its fenced code blocks."""

    prose: Lines
    """``(lineno, line)`` outside fenced blocks."""
    openers: Lines
    """``(lineno, info string)`` of each outermost fence opener."""
    unclosed: int | None
    """Line where a fence opened and never closed."""


def _slug(heading: str) -> str:
    """GitHub-style heading slug: lowercase, drop punctuation, each space to one hyphen."""
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s", "-", text).strip("-")


def _root_markdown() -> dict[str, str]:
    """Root ``*.md`` documents keyed by file name."""
    return {p.name: p.read_text() for p in sorted(REPO_ROOT.glob("*.md"))}


def _docs_markdown() -> dict[str, str]:
    """Non-canonical ``docs/**/*.md``, keyed by repo-relative path."""
    return {
        p.relative_to(REPO_ROOT).as_posix(): p.read_text()
        for p in sorted((REPO_ROOT / "docs").rglob("*.md"))
    }


def _scan_fences(text: str) -> Fences:
    """Prose lines, outermost fence openers, and the line where an unclosed fence opened."""
    lines: Lines = []
    openers: Lines = []
    fence: str | None = None
    opened = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        match = FENCE_RE.match(line)
        if match is None:
            if fence is None:
                lines.append((lineno, line))
            continue
        token = match.group(1)
        if fence is None:
            fence, opened = token, lineno
            openers.append((lineno, match.group(2)))
        elif token[0] == fence[0] and len(token) >= len(fence):
            fence = None
    return Fences(lines, openers, opened if fence is not None else None)


def _prose_lines(text: str) -> Lines:
    """``(lineno, line)`` pairs outside fenced code blocks."""
    return _scan_fences(text).prose


def _target(source: str, target: str, known: Container[str] = ()) -> str | None:
    """Repo-relative path of a link target in ``source``; ``None`` when it does not exist."""
    path = (REPO_ROOT / source).parent / target
    try:
        name = path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:  # resolves outside the repository
        return None
    return name if name in known or (REPO_ROOT / name).exists() else None


def _link_targets(text: str) -> list[tuple[int, str]]:
    """``(lineno, target)`` of each relative link outside fenced blocks; ``]()`` is skipped."""
    return [
        (lineno, match.group(1))
        for lineno, line in _prose_lines(text)
        for match in LINK_RE.finditer(line)
        if match.group(1) and not EXTERNAL_RE.match(match.group(1))
    ]


def _image_targets(text: str) -> list[str]:
    """Target of each relative inline image shown: not in a fence, comment, or code span."""
    prose = "\n".join(line for _, line in _prose_lines(text))
    shown = CODE_SPAN_RE.sub("", HTML_COMMENT_RE.sub("", prose))
    return [
        target
        for match in IMAGE_RE.finditer(shown)
        if not EXTERNAL_RE.match(target := match.group(1))
    ]


def _anchors(text: str) -> set[str]:
    """Heading slugs of a document, ignoring fenced code blocks; duplicates get ``-N`` suffixes."""
    seen: Counter[str] = Counter()
    anchors: set[str] = set()
    for _, line in _prose_lines(text):
        match = HEADING_RE.match(line)
        if match is None:
            continue
        slug = _slug(match.group(2))
        anchors.add(slug if seen[slug] == 0 else f"{slug}-{seen[slug]}")
        seen[slug] += 1
    return anchors


def _cells(row: str) -> list[str]:
    r"""Cells of a ``| a | b |`` table row, stripped; ``\|`` does not split."""
    parts = [c.strip() for c in CELL_SPLIT_RE.split(row.strip())]
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    return parts


def _tables(text: str) -> list[Table]:
    """Markdown tables as ``(header lineno, header cells, [(lineno, cells), ...])``."""
    tables: list[Table] = []
    block: Lines = []

    def flush() -> None:
        if len(block) >= 2 and TABLE_SEP_RE.match(block[1][1].strip()):
            rows = [(n, _cells(row)) for n, row in block[2:]]
            tables.append((block[0][0], _cells(block[0][1]), rows))
        block.clear()

    for lineno, line in _prose_lines(text):
        if line.strip().startswith("|"):
            block.append((lineno, line))
        else:
            flush()
    flush()
    return tables


def _sources() -> list[Path]:
    return sorted((REPO_ROOT / PACKAGE).rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def check_fences(docs: dict[str, str]) -> list[str]:
    """No document leaves a code fence open: everything after one is invisible to every check."""
    errors: list[str] = []
    for name, text in docs.items():
        opened = _scan_fences(text).unclosed
        if opened is not None:
            errors.append(
                f"{name}:{opened}: unterminated code fence hides the rest of the document"
            )
    return errors


def check_anchors(docs: dict[str, str]) -> list[str]:
    """Every link target exists and every ``.md`` ``#anchor`` resolves to a heading."""
    anchors = {name: _anchors(text) for name, text in docs.items()}
    errors: list[str] = []
    for name, text in docs.items():
        for lineno, target in _link_targets(text):
            path, _, anchor = target.partition("#")
            resolved = name if path == "" else _target(name, path, anchors)
            if resolved is None:
                errors.append(f"{name}:{lineno}: link to missing file {path}")
                continue
            if resolved.endswith(".md") and resolved not in anchors:
                anchors[resolved] = _anchors((REPO_ROOT / resolved).read_text())
            if anchor and resolved.endswith(".md") and anchor not in anchors[resolved]:
                errors.append(f"{name}:{lineno}: broken anchor {resolved}#{anchor}")
    return errors


def check_diagram_fences(docs: dict[str, str]) -> list[str]:
    """No document opens a PlantUML fence; a fence nested in another block is an example."""
    return [
        f"{name}:{lineno}: {info} fence; move it to docs/<dir>/diagrams/*.puml"
        for name, text in docs.items()
        for lineno, info in _scan_fences(text).openers
        if info.lower() in DIAGRAM_FENCES
    ]


def check_diagram_embeds(docs: dict[str, str]) -> list[str]:
    """Every rendered ``docs/**/diagrams/*.svg`` is embedded as a Markdown image in a document."""
    embedded = {
        _target(name, path, docs)
        for name, text in docs.items()
        for target in _image_targets(text)
        if (path := target.partition("#")[0])
    }
    return [
        f"{relative}: diagram is embedded in no document"
        for svg in sorted((REPO_ROOT / "docs").rglob("diagrams/*.svg"))
        if (relative := svg.relative_to(REPO_ROOT).as_posix()) not in embedded
    ]


def _const_str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def parse_cli_flags(sources: list[Path]) -> dict[str, set[str | None]]:
    """Longest option string → the ``env=`` names it is registered with (``None`` = none).

    A variable registered without a flag maps to itself.
    """
    flags: dict[str, set[str | None]] = {}
    for path in sources:
        for node in ast.walk(_parse(path)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr == ENV_ONLY_CALL:
                if node.args and (env_only := _const_str(node.args[0])):
                    flags.setdefault(env_only, set()).add(env_only)
                continue
            if node.func.attr not in OPTION_CALLS:
                continue
            longs = [s for a in node.args if (s := _const_str(a)) and s.startswith("--")]
            if not longs:
                continue
            env = next((_const_str(k.value) for k in node.keywords if k.arg == "env"), None)
            flags.setdefault(max(longs, key=len), set()).add(env)
    if not flags:
        raise SystemExit(f"check_docs: parsed no option registrations under {PACKAGE}")
    return flags


def check_config(docs: dict[str, str], flags: dict[str, set[str | None]]) -> list[str]:
    """``CONFIG.md`` flag and variable-only rows equal the option registrations."""
    errors: list[str] = []
    documented: dict[str, tuple[int, str | None]] = {}
    for _, header, rows in _tables(docs["CONFIG.md"]):
        if header[:1] == ["Variable"]:
            for lineno, cells in rows:
                env_only = ENV_ONLY_CELL_RE.match(cells[0]) if cells else None
                if env_only is None:
                    errors.append(f"CONFIG.md:{lineno}: unparsable variable row")
                    continue
                if env_only.group(1) in documented:
                    errors.append(f"CONFIG.md:{lineno}: {env_only.group(1)} documented twice")
                documented[env_only.group(1)] = (lineno, env_only.group(1))
            continue
        if header[:2] != ["Flag", "Variable"]:
            continue
        for lineno, cells in rows:
            flag_cell = FLAG_CELL_RE.match(cells[0]) if cells else None
            env_cell = ENV_CELL_RE.match(cells[1]) if len(cells) > 1 else None
            if flag_cell is None or env_cell is None:
                errors.append(f"CONFIG.md:{lineno}: unparsable flag row")
                continue
            if flag_cell.group(1) in documented:
                errors.append(f"CONFIG.md:{lineno}: {flag_cell.group(1)} documented twice")
            documented[flag_cell.group(1)] = (lineno, env_cell.group(1))
    for flag, envs in sorted(flags.items()):
        if len(envs) > 1:
            names = ", ".join(sorted(e or "none" for e in envs))
            errors.append(f"src: {flag} registered with conflicting variables: {names}")
        if flag not in documented and not flag.startswith("--"):
            errors.append(f"CONFIG.md: missing variable-only row {flag}")
        elif flag not in documented:
            first = sorted(e or "none" for e in envs)[0]
            errors.append(f"CONFIG.md: missing flag {flag} (variable {first})")
    for flag, (lineno, env) in sorted(documented.items()):
        if flag not in flags:
            errors.append(f"CONFIG.md:{lineno}: {flag} is not registered")
        elif env not in flags[flag]:
            code = ", ".join(sorted(e or "none" for e in flags[flag]))
            errors.append(f"CONFIG.md:{lineno}: {flag} variable documented as {env}, code {code}")
    return errors


def parse_exception_bases(sources: list[Path]) -> dict[str, tuple[str, ...]]:
    """Every ``class *Exception`` in the source mapped to its base names."""
    bases = {
        node.name: tuple(
            base.id if isinstance(base, ast.Name) else base.attr
            for base in node.bases
            if isinstance(base, ast.Name | ast.Attribute)
        )
        for path in sources
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.ClassDef) and node.name.endswith("Exception")
    }
    if not bases:
        raise SystemExit(f"check_docs: parsed no exception classes under {PACKAGE}")
    return bases


def parse_cli_handlers(cli_py: Path) -> set[tuple[frozenset[str], str | None]]:
    """``(caught names, err_exit message)`` of every ``except`` handler in ``cli.py``.

    The message is ``None`` when ``err_exit`` receives a non-literal (the original message).
    """
    handlers: set[tuple[frozenset[str], str | None]] = set()
    for node in ast.walk(_parse(cli_py)):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        types = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
        names = frozenset(
            t.id if isinstance(t, ast.Name) else t.attr
            for t in types
            if isinstance(t, ast.Name | ast.Attribute)
        )
        message: str | None = None
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "err_exit":
                message = _const_str(sub.args[0]) if sub.args else None
                break
        handlers.add((names, message))
    if not handlers:
        raise SystemExit(f"check_docs: parsed no except handlers in {cli_py}")
    return handlers


def _message_cell(cell: str) -> str | None:
    match = re.fullmatch(r"`(.*)`", cell)
    return match.group(1) if match else None


def _first_table(text: str, first_header: str) -> Table | None:
    return next((t for t in _tables(text) if t[1] and t[1][0] == first_header), None)


def check_exceptions(docs: dict[str, str], defined: set[str]) -> list[str]:
    """The ``EXCEPTIONS.md`` table lists every class; documents name only known classes."""
    errors: list[str] = []
    known = defined | set(EXTERNAL_EXCEPTIONS)
    rows: dict[str, int] = {}
    table = _first_table(docs["EXCEPTIONS.md"], "Exception")
    for lineno, cells in table[2] if table else []:
        match = EXCEPTION_CELL_RE.match(cells[0]) if cells else None
        if match is not None:
            rows[match.group(1)] = lineno
    if table is None:
        errors.append("EXCEPTIONS.md: no table with header `Exception`")
    errors += [f"EXCEPTIONS.md: missing exception {n}" for n in sorted(known - set(rows))]
    errors += [
        f"EXCEPTIONS.md:{lineno}: {name} is not defined in {PACKAGE.as_posix()} or txstore"
        for name, lineno in sorted(rows.items())
        if name not in known
    ]
    for name, doc in docs.items():
        for lineno, line in _prose_lines(doc):
            for token in EXCEPTION_TOKEN_RE.findall(line):
                if token not in known:
                    errors.append(f"{name}:{lineno}: unknown exception name {token}")
    return errors


def check_exit_groups(
    docs: dict[str, str], handlers: set[tuple[frozenset[str], str | None]]
) -> list[str]:
    """The ``EXCEPTIONS.md`` exit-group table equals the ``cli.py`` handlers."""
    table = _first_table(docs["EXCEPTIONS.md"], "Group")
    if table is None:
        return ["EXCEPTIONS.md: no table with header `Group`"]
    errors: list[str] = []
    documented: dict[tuple[frozenset[str], str | None], tuple[int, str]] = {}
    for lineno, cells in table[2]:
        key = (frozenset(IDENT_RE.findall(cells[1])), _message_cell(cells[2]))
        documented[key] = (lineno, cells[0])
    for names, message in sorted(handlers, key=lambda h: (sorted(h[0]), h[1] or "")):
        if (names, message) not in documented:
            shown = ", ".join(sorted(names))
            errors.append(
                f"EXCEPTIONS.md: cli.py handler ({shown}) → {message or 'original message'} "
                "has no row"
            )
    for (names, message), (lineno, group) in sorted(documented.items(), key=lambda i: i[1]):
        if (names, message) not in handlers:
            errors.append(f"EXCEPTIONS.md:{lineno}: exit group {group} matches no cli.py handler")
    return errors


def _resolve_group(
    name: str, groups: dict[str, str], bases: dict[str, tuple[str, ...]]
) -> str | None:
    """Exit group of `name`, following base classes until a caught class is reached."""
    seen: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if current in groups:
            return groups[current]
        if current in EXTERNAL_GROUPS:
            return EXTERNAL_GROUPS[current]
        pending.extend(bases.get(current, ()))
    return None


def _exit_groups(docs: dict[str, str]) -> dict[str, str]:
    """Caught class name → exit group, from the ``EXCEPTIONS.md`` exit-group table."""
    table = _first_table(docs["EXCEPTIONS.md"], "Group")
    return {
        name: cells[0]
        for _, cells in (table[2] if table else [])
        for name in IDENT_RE.findall(cells[1])
    }


def check_group_notation(docs: dict[str, str], bases: dict[str, tuple[str, ...]]) -> list[str]:
    """Every ``Class (Group)`` notation names the group ``cli.py`` exits through.

    A parenthesised word that is not a known group is ordinary prose and left alone.
    """
    groups = _exit_groups(docs)
    known = set(groups.values()) | set(EXTERNAL_GROUPS.values())
    all_bases = {**EXTERNAL_EXCEPTIONS, **bases}
    errors: list[str] = []
    for name, text in docs.items():
        for lineno, line in _prose_lines(text):
            for cls, group in GROUP_NOTATION_RE.findall(line):
                if group not in known:
                    continue
                actual = _resolve_group(cls, groups, all_bases)
                if actual is None:
                    errors.append(f"{name}:{lineno}: {cls} ({group}) resolves to no exit group")
                elif actual != group:
                    errors.append(
                        f"{name}:{lineno}: {cls} is documented as {group}, cli.py groups it as "
                        f"{actual}"
                    )
    return errors


def check_tables(docs: dict[str, str]) -> list[str]:
    """Every table row has as many cells as its header; an escaped ``\\|`` stays in its cell."""
    return [
        f"{name}:{lineno}: row has {len(cells)} cells, header at line {header_lineno} has "
        f"{len(header)}"
        for name, text in docs.items()
        for header_lineno, header, rows in _tables(text)
        for lineno, cells in rows
        if len(cells) != len(header)
    ]


def check_exit_coverage(docs: dict[str, str], bases: dict[str, tuple[str, ...]]) -> list[str]:
    """Every exception class defined in the source reaches a ``cli.py`` exit group.

    A class the handlers miss, directly or through a base, exits with a traceback.
    """
    groups = _exit_groups(docs)
    all_bases = {**EXTERNAL_EXCEPTIONS, **bases}
    return [
        f"{PACKAGE.as_posix()}: {name} resolves to no cli.py exit group"
        for name in sorted(bases)
        if _resolve_group(name, groups, all_bases) is None
    ]


def _raised_names(sources: list[Path]) -> set[str]:
    """Class names in every ``raise Name(...)`` / ``raise Name`` of the source."""
    names: set[str] = set()
    for path in sources:
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(target, ast.Name | ast.Attribute):
                names.add(target.id if isinstance(target, ast.Name) else target.attr)
    return names


def _pytest_raises_names(tests: list[Path]) -> set[str]:
    """Class names passed to ``pytest.raises``, a tuple of them included."""
    names: set[str] = set()
    for path in tests:
        for node in ast.walk(_parse(path)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "raises"
                and node.args
            ):
                continue
            arg = node.args[0]
            for item in arg.elts if isinstance(arg, ast.Tuple) else [arg]:
                if isinstance(item, ast.Name | ast.Attribute):
                    names.add(item.id if isinstance(item, ast.Name) else item.attr)
    return names


def check_exception_tests(bases: dict[str, tuple[str, ...]]) -> list[str]:
    """Every exception class defined in the source is raised there and proven by a test.

    txstore classes are exempt: txstore owns their raising conditions → EXCEPTIONS.md.
    """
    raised = _raised_names(_sources())
    tests = sorted(
        path for path in (REPO_ROOT / TESTS).rglob("test_*.py") if path.name not in STUB_RAISE_TESTS
    )
    tested = _pytest_raises_names(tests)
    errors = [
        f"{PACKAGE.as_posix()}: {name} is defined but never raised"
        for name in sorted(bases)
        if name not in raised
    ]
    errors += [
        f"{TESTS.as_posix()}: no pytest.raises names {name} outside test_cli_main.py"
        for name in sorted(bases)
        if name not in tested
    ]
    return errors


def check_line_length(docs: dict[str, str]) -> list[str]:
    """Prose stays within ``LINE_LIMIT`` columns; tables, fences, URLs, bare tokens are exempt."""
    errors: list[str] = []
    for name, text in docs.items():
        for lineno, line in _prose_lines(text):
            stripped = line.strip()
            if len(line) <= LINE_LIMIT or stripped.startswith("|") or URL_RE.search(line):
                continue
            if " " not in stripped:
                continue
            errors.append(f"{name}:{lineno}: line is {len(line)} columns (limit {LINE_LIMIT})")
    return errors


def check_todos(docs: dict[str, str]) -> list[str]:
    """Root contracts carry no TODO-style markers outside inline code."""
    return [
        f"{name}:{lineno}: TODO marker in a contract"
        for name, text in docs.items()
        if name not in TODO_EXEMPT
        for lineno, line in _prose_lines(text)
        if TODO_RE.search(INLINE_CODE_RE.sub("", line))
    ]


def check_code_references(docs: dict[str, str]) -> list[str]:
    """Every ``ROOT.md#anchor`` cited in source or test code resolves to a root heading."""
    anchors = {name: _anchors(text) for name, text in docs.items()}
    errors: list[str] = []
    for root in (REPO_ROOT / PACKAGE, REPO_ROOT / "tests"):
        for path in sorted(root.rglob("*.py")):
            where = path.relative_to(REPO_ROOT).as_posix()
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                for doc, anchor in CODE_REF_RE.findall(line):
                    if doc not in anchors:
                        errors.append(f"{where}:{lineno}: cites missing document {doc}")
                    elif anchor not in anchors[doc]:
                        errors.append(f"{where}:{lineno}: broken anchor {doc}#{anchor}")
    return errors


def check_docs_index(docs: dict[str, str]) -> list[str]:
    """Every ``docs/<dir>/`` with pages has a ``README.md`` linking each sibling page."""
    errors: list[str] = []
    by_dir: dict[str, list[str]] = {}
    for name in docs:
        parts = name.split("/")
        if len(parts) == 3 and parts[0] == "docs":
            by_dir.setdefault(parts[1], []).append(parts[2])
    for directory, pages in sorted(by_dir.items()):
        index = f"docs/{directory}/README.md"
        if index not in docs:
            errors.append(f"docs/{directory}/: no README.md index")
            continue
        linked = {
            m.group(1).partition("#")[0]
            for _, line in _prose_lines(docs[index])
            for m in LINK_RE.finditer(line)
        }
        errors += [
            f"{index}: {page} is not linked from the index"
            for page in sorted(pages)
            if page != "README.md" and page not in linked
        ]
    return errors


def check_version(docs: dict[str, str]) -> list[str]:
    """The ``README.md`` "Current version" line carries ``[project].version``."""
    expected = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    readme = README_VERSION_RE.search(docs.get("README.md", ""))
    found = readme.group(1) if readme else None
    if found == expected:
        return []
    return [f"README.md: version {found or 'missing'}, pyproject.toml has {expected}"]


def check_txstore_links(docs: dict[str, str]) -> list[str]:
    """Every link into the txstore repository names the tag of the locked txstore version."""
    lock = REPO_ROOT / "uv.lock"
    if not lock.is_file():
        return []
    locked = next(
        (
            package.get("version")
            for package in tomllib.loads(lock.read_text()).get("package", [])
            if package.get("name") == "txstore"
        ),
        None,
    )
    if locked is None:
        return ["uv.lock: no txstore package to pin the txstore links to"]
    expected = f"v{locked}"
    return [
        f"{name}:{number}: txstore link names {ref}, uv.lock resolves {expected}"
        for name, text in docs.items()
        for number, line in enumerate(text.splitlines(), start=1)
        for ref in TXSTORE_LINK_RE.findall(line)
        if ref != expected
    ]


def check_package_sources() -> list[str]:
    """Dependencies resolve from public PyPI only."""
    errors: list[str] = []
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    tool = pyproject.get("tool", {})
    if tool.get("uv", {}).get("index"):
        errors.append("pyproject.toml: declares [[tool.uv.index]]; packages resolve from PyPI")
    if tool.get("poetry", {}).get("source"):
        errors.append("pyproject.toml: declares [[tool.poetry.source]]; packages resolve from PyPI")
    lock = REPO_ROOT / "uv.lock"
    if lock.is_file():
        for package in tomllib.loads(lock.read_text()).get("package", []):
            registry = package.get("source", {}).get("registry")
            if registry is not None and registry != PYPI_REGISTRY:
                errors.append(f"uv.lock: {package.get('name')} resolves from {registry}")
    return errors


def main() -> int:
    """Run every check; print failures; exit 1 on any failure."""
    docs = _root_markdown()
    for required in ("CONFIG.md", "EXCEPTIONS.md"):
        if required not in docs:
            raise SystemExit(f"check_docs: {required} not found in {REPO_ROOT}")
    all_docs = {**docs, **_docs_markdown()}
    sources = _sources()
    bases = parse_exception_bases(sources)
    errors = [
        *check_fences(all_docs),
        *check_anchors(all_docs),
        *check_diagram_fences(all_docs),
        *check_diagram_embeds(all_docs),
        *check_config(docs, parse_cli_flags(sources)),
        *check_exceptions(all_docs, set(bases)),
        *check_exit_groups(docs, parse_cli_handlers(REPO_ROOT / PACKAGE / "cli.py")),
        *check_group_notation(all_docs, bases),
        *check_exit_coverage(docs, bases),
        *check_exception_tests(bases),
        *check_line_length(all_docs),
        *check_tables(all_docs),
        *check_todos(docs),
        *check_code_references(docs),
        *check_docs_index(all_docs),
        *check_version(docs),
        *check_package_sources(),
        *check_txstore_links(all_docs),
    ]
    for error in errors:
        print(error)
    if errors:
        print(f"check_docs: {len(errors)} problem(s)")
        return 1
    print(f"check_docs: OK ({len(all_docs)} documents)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
