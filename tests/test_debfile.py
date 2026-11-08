"""Coverage for `debkit.repo.debfile`."""

from __future__ import annotations

from io import BytesIO, UnsupportedOperation
from pathlib import Path
from typing import IO
from unittest.mock import MagicMock, patch

import pytest
from helpers.factories import make_deb, make_tar_gz
from txstore import StorageFS

from debkit.repo.debfile import DebFile, DebFileException

_PATH = "foo_1.0_amd64.deb"
_CONTROL = b"Package: foo\nVersion: 1.0\nArchitecture: amd64\n"
_CONTROL_TAR = make_tar_gz({"./control": _CONTROL})


class _TrackingFS(StorageFS):
    """Real fs storage that keeps every handle `get_stream` returns and counts `get`."""

    def __init__(self, base_dir: str) -> None:
        super().__init__(base_dir)
        self.handles: list[IO[bytes]] = []
        self.gets = 0

    def get_stream(self, path: str) -> IO[bytes]:
        handle = super().get_stream(path)
        self.handles.append(handle)
        return handle

    def get(self, path: str) -> bytes:
        self.gets += 1
        return super().get(path)

    def all_closed(self) -> bool:
        """At least one stream was opened, and every one of them is closed."""
        return bool(self.handles) and all(handle.closed for handle in self.handles)


class _Unseekable(BytesIO):
    """A stream such as an HTTP body: readable, but neither `seek` nor `tell` work."""

    def seekable(self) -> bool:
        return False

    def seek(self, *_args: object) -> int:
        raise UnsupportedOperation("seek")

    def tell(self) -> int:
        raise UnsupportedOperation("tell")


class _UnseekableFS(_TrackingFS):
    def get_stream(self, path: str) -> IO[bytes]:
        with super().get_stream(path) as fp:
            handle = _Unseekable(fp.read())
        self.handles.append(handle)
        return handle


def _storage(tmp_path: Path, data: bytes, cls: type[_TrackingFS] = _TrackingFS) -> _TrackingFS:
    storage = cls(str(tmp_path))
    storage.put(_PATH, data)
    return storage


def _streaming(control: bytes) -> MagicMock:
    """Storage that serves a real `.deb` holding *control* under any path."""
    storage = MagicMock()
    storage.get_stream.side_effect = lambda _path: BytesIO(make_deb(control))
    return storage


def test_init_reads_the_control_file_and_closes_the_stream(tmp_path: Path) -> None:
    storage = _storage(tmp_path, make_deb(_CONTROL))

    deb = DebFile(storage, _PATH)

    assert deb.control == _CONTROL
    assert storage.all_closed()


def test_init_falls_back_to_get_when_the_stream_cannot_seek(tmp_path: Path) -> None:
    storage = _storage(tmp_path, make_deb(_CONTROL), _UnseekableFS)

    deb = DebFile(storage, _PATH)

    assert deb.control == _CONTROL
    assert storage.gets == 1
    assert storage.all_closed()


def test_init_wraps_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DebFileException, match="Cannot read"):
        DebFile(StorageFS(str(tmp_path)), _PATH)


@pytest.mark.parametrize(
    "data",
    [b"garbage", b"!<arch>\nnotamember", b"!<arch>\n"],
    ids=["no-ar-header", "truncated-member-header", "no-members"],
)
def test_init_refuses_a_broken_deb_and_closes_the_stream(tmp_path: Path, data: bytes) -> None:
    storage = _storage(tmp_path, data)

    with pytest.raises(DebFileException, match="Invalid .deb"):
        DebFile(storage, _PATH)

    assert storage.all_closed()


@pytest.mark.parametrize(
    "control_tar",
    [_CONTROL_TAR[: len(_CONTROL_TAR) // 2], b"garbage", make_tar_gz({"./other": b""})],
    ids=["truncated", "not-a-tar", "no-control-file"],
)
def test_init_refuses_a_broken_control_tar(tmp_path: Path, control_tar: bytes) -> None:
    """A broken control member fails while the control file is read, inside the constructor."""
    storage = _storage(tmp_path, make_deb(control_tar=control_tar))

    with pytest.raises(DebFileException, match="Invalid .deb"):
        DebFile(storage, _PATH)

    assert storage.all_closed()


def test_get_package_rejects_non_bytes_control() -> None:
    with patch("debkit.repo.debfile._control_of", return_value="not-bytes"):
        deb = DebFile(MagicMock(), _PATH)
    with pytest.raises(DebFileException):
        deb.get_package()


def test_get_package_names_the_pool_path_with_component() -> None:
    pkg = DebFile(_streaming(_CONTROL), _PATH, comp="main").get_package()
    assert pkg.get_filename() == "pool/main/f/foo/foo_1.0_amd64.deb"


def test_get_package_without_component_uses_flat_pool_path() -> None:
    deb = DebFile(_streaming(b"Package: bar\nVersion: 1.0\n"), "bar_1.0_amd64.deb")
    assert deb.get_package().get_filename() == "pool/b/bar/bar_1.0_amd64.deb"


@pytest.mark.parametrize(
    ("control", "path"),
    [
        (b"Version: 1.0\n", "foo_1.0_amd64.deb"),
        (b"Package: ../../../contrib/h/hello\n", "hello_1.0_amd64.deb"),
        (b"Package: Foo\n", "foo_1.0_amd64.deb"),
        (b"Package: foo\n", "incoming/.."),
        (b"Package: foo\nSource: ../../../contrib/h/hello\n", "foo_1.0_amd64.deb"),
        (b"Package: foo\nSource: Foo (1.0)\n", "foo_1.0_amd64.deb"),
        (b"Package: foo\nSource: foo (1.0\n", "foo_1.0_amd64.deb"),
        (b"Package: foo\nSource:\n", "foo_1.0_amd64.deb"),
    ],
    ids=[
        "missing-package",
        "traversing-package",
        "non-policy-name",
        "traversing-upload-name",
        "traversing-source",
        "non-policy-source",
        "unclosed-source-version",
        "empty-source",
    ],
)
def test_get_package_refuses_a_name_that_steers_the_pool_key(control: bytes, path: str) -> None:
    deb = DebFile(_streaming(control), path, comp="main")
    with pytest.raises(DebFileException):
        deb.get_package()


@pytest.mark.parametrize(
    ("control", "path", "expected"),
    [
        (b"Package: foo\n", "incoming/foo_1.0_amd64.deb", "pool/main/f/foo/foo_1.0_amd64.deb"),
        (
            b"Package: libfoo1\nSource: foo\n",
            "libfoo1_1.0_amd64.deb",
            "pool/main/f/foo/libfoo1_1.0_amd64.deb",
        ),
        (
            b"Package: libbar1\nSource: libbar\n",
            "libbar1_1.0_amd64.deb",
            "pool/main/libb/libbar/libbar1_1.0_amd64.deb",
        ),
        (
            b"Package: libfoo1\nSource: foo (1.2-1)\n",
            "libfoo1_1.0_amd64.deb",
            "pool/main/f/foo/libfoo1_1.0_amd64.deb",
        ),
    ],
    ids=["package-as-source", "source-field", "lib-prefix", "source-version-stripped"],
)
def test_get_package_files_a_real_deb_under_its_source_package(
    tmp_path: Path, control: bytes, path: str, expected: str
) -> None:
    storage = StorageFS(str(tmp_path))
    storage.put(path, make_deb(control + b"Version: 1.0\nArchitecture: amd64\n"))

    pkg = DebFile(storage, path, comp="main").get_package()

    assert pkg.fields["Filename"] == expected
