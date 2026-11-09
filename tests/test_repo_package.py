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


@pytest.mark.parametrize("filename", ["pool/main/./foo.deb", "pool/main/foo.deb"])
def test_package_parse_normalises_the_filename(filename: str) -> None:
    pkg = Package()
    pkg.parse(f"Package: foo\nFilename: {filename}\nSize: 1\n")
    assert pkg.fields["Filename"] == "pool/main/foo.deb"


def test_package_exception_message_no_self_leak() -> None:
    """Regression: ``__init__(self, ...)`` used to leak the instance into args."""
    exc = PackageException("boom")
    assert str(exc) == "Package: boom"


_STANZA = (
    "Package: foo\n"
    "Maintainer: Jöhn Dœ <j@example.invalid>\n"
    "Conffiles:\n"
    " /etc/foo.conf 0123456789abcdef0123456789abcdef\n"
    " /etc/bar.conf fedcba9876543210fedcba9876543210\n"
    "Description: short synopsis\n"
    " line one.\n"
    " .\n"
    " para two.\n"
    "Filename: pool/main/f/foo/foo_1.0_amd64.deb\n"
    "Size: 1\n"
)


def test_package_parse_serialize_keeps_multiline_and_utf8_values() -> None:
    """Regression: the email-based serializer folded, MIME-encoded, and rewrapped values."""
    pkg = Package()
    pkg.parse(_STANZA)
    assert pkg.fields["Description"] == "short synopsis\n line one.\n .\n para two."
    assert pkg.serialize() == _STANZA + "\n"


def test_package_parse_newline_in_single_line_field_raises() -> None:
    pkg = Package()
    with pytest.raises(PackageException, match="newline in field: Size"):
        pkg.parse("Package: foo\nFilename: pool/main/foo.deb\nSize:\n 1\n 2\n")


def test_package_equality_compares_the_fields() -> None:
    a, b = Package(), Package()
    a.parse(_STANZA)
    b.parse(_STANZA)
    assert a == b
    b.fields["Size"] = "2"
    assert a != b


def test_package_equality_with_another_type_is_false() -> None:
    """Regression: comparing with a non-Package raised, so `pkg in [None]` failed."""
    pkg = Package()
    pkg.parse(_STANZA)
    assert pkg != None  # noqa: E711
    assert pkg not in [None, 1]


def test_package_get_filename_returns_the_filename() -> None:
    pkg = Package()
    pkg.set_filename("pool/main/f/foo/foo_1.0_amd64.deb")
    assert pkg.get_filename() == "pool/main/f/foo/foo_1.0_amd64.deb"


@pytest.mark.parametrize("fields", [{}, {"Filename": ""}], ids=["missing", "empty"])
def test_package_get_filename_without_a_filename_raises(fields: dict[str, str]) -> None:
    with pytest.raises(PackageException, match="filename is empty"):
        Package(fields=fields).get_filename()


def test_set_filename_refuses_an_empty_filename() -> None:
    with pytest.raises(PackageException, match="filename is empty"):
        Package(fields={"Filename": "pool/x.deb", "Size": "1"}).set_filename("")
