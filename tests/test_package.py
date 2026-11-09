"""Package contracts: version, runtime dependencies, the txstore boundary, and import directions.

Source is walked with `ast`, never imported for the boundary checks →
[TESTING.md](../TESTING.md#package-contracts), [STRUCTURE.md](../STRUCTURE.md#dependencies).
"""

from __future__ import annotations

import ast
import importlib.metadata
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
import txstore

import debkit

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src" / "debkit"

# Top-level module names a runtime import may use, beside the standard library and txstore.
_THIRD_PARTY_RUNTIME = {"debian", "gnupg", "humanize", "urllib3"}
_DECLARED = {"txstore", "python-debian", "python-gnupg", "humanize", "urllib3"}

# Modules and the debkit modules each may import at run time → STRUCTURE.md#dependencies.
_ALLOWED_INTERNAL = {
    "_env": set(),
    "_escape": set(),
    "_logging": {"_escape"},
    "_options": {"_env"},
    "argtypes": set(),
}
# Layers and the layers they never import.
_FORBIDDEN_LAYERS = {
    "repo": {"action", "argparser", "argtypes", "cli", "_options", "_logging"},
    "action": {"argparser", "cli", "_logging"},
}


class _Import:
    def __init__(self, module: str, names: tuple[str, ...], deferred: bool, type_only: bool):
        self.module = module
        self.top = module.split(".")[0]
        self.names = names
        self.deferred = deferred
        self.type_only = type_only


def _modules() -> Iterator[tuple[str, ast.Module]]:
    """Yield ``(dotted name under debkit, tree)`` for every source module."""
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).with_suffix("").as_posix().replace("/", ".")
        yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.Module) -> Iterator[_Import]:
    type_only: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            type_only.update(id(child) for child in ast.walk(node))
    top_level = {id(node) for node in tree.body} | type_only
    for node in ast.walk(tree):
        entries: list[tuple[str, tuple[str, ...]]]
        if isinstance(node, ast.Import):
            entries = [(alias.name, ()) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            entries = [(node.module, tuple(alias.name for alias in node.names))]
        else:
            continue
        for module, names in entries:
            yield _Import(module, names, id(node) not in top_level, id(node) in type_only)


def _internal(module: str) -> str | None:
    """Return the debkit module path `module` names (``""`` for the package), else None."""
    if module == "debkit":
        return ""
    if module.startswith("debkit."):
        return module.removeprefix("debkit.")
    return None


def _layer(name: str) -> str:
    return name.split(".")[0]


# Version


def test_version_is_read_from_the_distribution() -> None:
    assert debkit.__version__ == importlib.metadata.version("debkit")


# Dependencies


def test_runtime_imports_are_stdlib_or_declared_dependencies() -> None:
    allowed = set(sys.stdlib_module_names) | _THIRD_PARTY_RUNTIME | {"debkit", "txstore"}
    offenders = [
        f"{name}: {imp.module}"
        for name, tree in _modules()
        for imp in _imports(tree)
        if not imp.type_only and imp.top not in allowed
    ]
    assert offenders == []


def test_declared_dependencies_match_runtime_imports() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {dep.split(">")[0].split("<")[0].split("=")[0] for dep in project["dependencies"]}
    assert declared == _DECLARED


def test_only_env_module_reads_the_environment() -> None:
    readers = {
        name
        for name, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "environb"}
    }
    assert readers == {"_env"}


# txstore boundary


def test_txstore_is_imported_through_its_facade_only() -> None:
    offenders = [
        f"{name}: {imp.module}"
        for name, tree in _modules()
        for imp in _imports(tree)
        if imp.top == "txstore" and imp.module != "txstore"
    ]
    assert offenders == []


def test_txstore_names_are_public() -> None:
    public = set(txstore.__all__)
    offenders = [
        f"{name}: {imported}"
        for name, tree in _modules()
        for imp in _imports(tree)
        if imp.module == "txstore"
        for imported in imp.names
        if imported not in public
    ]
    assert offenders == []


# Import directions


@pytest.mark.parametrize(("module", "allowed"), sorted(_ALLOWED_INTERNAL.items()))
def test_leaf_modules_import_only_their_row(module: str, allowed: set[str]) -> None:
    tree = dict(_modules())[module]
    imported = {
        _internal(imp.module)
        for imp in _imports(tree)
        if not imp.type_only and _internal(imp.module) is not None
    }
    assert imported <= allowed


@pytest.mark.parametrize(("layer", "forbidden"), sorted(_FORBIDDEN_LAYERS.items()))
def test_layers_never_import_upward(layer: str, forbidden: set[str]) -> None:
    offenders = [
        f"{name}: {imp.module}"
        for name, tree in _modules()
        if _layer(name) == layer
        for imp in _imports(tree)
        if not imp.type_only
        and (target := _internal(imp.module)) is not None
        and _layer(target) in forbidden
    ]
    assert offenders == []


def test_no_import_is_deferred() -> None:
    deferred = [
        f"{name}: {imp.module}"
        for name, tree in _modules()
        for imp in _imports(tree)
        if imp.deferred and not imp.type_only
    ]
    assert deferred == []


def test_only_cli_calls_sys_exit() -> None:
    callers = {
        name
        for name, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "exit"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    }
    assert callers <= {"cli"}
