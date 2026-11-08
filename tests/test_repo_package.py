"""Unit tests for ``Package`` (TESTING.md § 7a).

Covers: mandatory-field validation, newline detection, Filename normalisation,
and the PackageException string-representation regression fix.
"""

from __future__ import annotations

import pytest

from debkit.repo.package import Package, PackageException


def test_package_parse_missing_mandatory_raises() -> None:
    pkg = Package()
    with pytest.raises(PackageException, match="mandatory field"):
        pkg.parse("Package: foo\nSize: 1\n")


def test_package_parse_normalises_relative_filename_sets_modified() -> None:
    pkg = Package()
    pkg.parse("Package: foo\nFilename: pool/main/./foo.deb\nSize: 1\n")
    assert pkg.fields["Filename"] == "pool/main/foo.deb"
    assert pkg.modified is True


def test_package_parse_already_normalised_filename_does_not_set_modified() -> None:
    pkg = Package()
    pkg.parse("Package: foo\nFilename: pool/main/foo.deb\nSize: 1\n")
    assert pkg.fields["Filename"] == "pool/main/foo.deb"
    assert pkg.modified is False


def test_package_exception_message_no_self_leak() -> None:
    """Regression: ``__init__(self, ...)`` used to leak the instance into args."""
    exc = PackageException("boom")
    assert str(exc) == "Package: boom"
