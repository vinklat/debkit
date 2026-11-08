"""Coverage for `debkit.repo.debfile`."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from txstore import StorageException

from debkit.repo.debfile import DebFile, DebFileException


def _mock_deb(control: object) -> MagicMock:
    deb = MagicMock()
    deb.control = {"control": control}
    return deb


def test_init_wraps_storage_exception() -> None:
    storage = MagicMock()
    storage.get_stream.side_effect = StorageException("boom")
    with pytest.raises(DebFileException):
        DebFile(storage, "foo.deb")


def test_get_package_rejects_non_bytes_control() -> None:
    storage = MagicMock()
    storage.get_stream.return_value = MagicMock()
    with patch("debkit.repo.debfile.debfile.DebFile", return_value=_mock_deb("not-bytes")):
        deb = DebFile(storage, "foo.deb")
        with pytest.raises(DebFileException):
            deb.get_package()


def test_get_package_populates_filename_field_with_component() -> None:
    storage = MagicMock()
    storage.get_stream.return_value = MagicMock()
    control = b"Package: foo\nArchitecture: amd64\nVersion: 1.0\n"
    with patch("debkit.repo.debfile.debfile.DebFile", return_value=_mock_deb(control)):
        deb = DebFile(storage, "foo_1.0_amd64.deb", comp="main")
        pkg = deb.get_package()

    assert deb.filename_field == "pool/main/f/foo/foo_1.0_amd64.deb"
    assert pkg.fields["Filename"] == "pool/main/f/foo/foo_1.0_amd64.deb"


def test_get_package_without_component_uses_flat_pool_path() -> None:
    storage = MagicMock()
    storage.get_stream.return_value = MagicMock()
    control = b"Package: bar\nVersion: 1.0\n"
    with patch("debkit.repo.debfile.debfile.DebFile", return_value=_mock_deb(control)):
        deb = DebFile(storage, "bar_1.0_amd64.deb")
        deb.get_package()
    assert deb.filename_field == "pool/b/bar/bar_1.0_amd64.deb"


@pytest.mark.parametrize(
    ("control", "path"),
    [
        (b"Version: 1.0\n", "foo_1.0_amd64.deb"),
        (b"Package: ../../../contrib/h/hello\n", "hello_1.0_amd64.deb"),
        (b"Package: Foo\n", "foo_1.0_amd64.deb"),
        (b"Package: foo\n", "../foo_1.0_amd64.deb"),
    ],
    ids=["missing-package", "traversing-package", "non-policy-name", "traversing-upload-path"],
)
def test_get_package_refuses_a_name_that_steers_the_pool_key(control: bytes, path: str) -> None:
    storage = MagicMock()
    storage.get_stream.return_value = MagicMock()
    with patch("debkit.repo.debfile.debfile.DebFile", return_value=_mock_deb(control)):
        deb = DebFile(storage, path, comp="main")
        with pytest.raises(DebFileException):
            deb.get_package()
