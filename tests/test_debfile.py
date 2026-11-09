"""Coverage for `debkit.repo.debfile`."""

from __future__ import annotations

import lzma
import subprocess
import tarfile
import zlib
from io import BytesIO, UnsupportedOperation
from pathlib import Path
from typing import IO
from unittest.mock import MagicMock

import pytest
from helpers.binaries import requires_binary
from helpers.factories import make_deb, make_tar_gz
from txstore import StorageFS

from debkit.repo import debfile as debfile_module
from debkit.repo.compress import codec_for
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


@pytest.mark.parametrize(
    ("padding", "cap", "match"),
    # a compressible control file inflates past the cap its small member stays under
    [(4096, 1024, "control file of"), (0, 64, "control.tar.gz of")],
    ids=["control-file", "control-tar-member"],
)
def test_init_refuses_an_oversized_control_and_closes_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, padding: int, cap: int, match: str
) -> None:
    """Both the member and the file are read into memory, so each is capped before reading."""
    monkeypatch.setattr(debfile_module, "MAX_CONTROL_BYTES", cap)
    storage = _storage(tmp_path, make_deb(_CONTROL + b"X-Pad: " + b"a" * padding + b"\n"))

    with pytest.raises(DebFileException, match=match):
        DebFile(storage, _PATH)
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


def _half_of_a_large_control_tar() -> bytes:
    """First half of an uncompressed tar holding a control file well past the 8 KiB that the
    decompressing reader buffers, so its first tar header decodes before the damage is reached."""
    control = _CONTROL + b"".join(b" line %d\n" % n for n in range(4000))
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("./control")
        info.size = len(control)
        tar.addfile(info, BytesIO(control))
    data = buf.getvalue()
    return data[: len(data) // 2]


def _gz_with_a_reserved_block() -> bytes:
    """A gzip member whose deflate data ends in block type 3, which every inflater rejects."""
    deflate = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = deflate.compress(_half_of_a_large_control_tar()) + deflate.flush(zlib.Z_SYNC_FLUSH)
    return b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff" + body + b"\x07"


def _xz_with_a_reserved_chunk() -> bytes:
    """An xz stream whose LZMA2 data ends in the reserved chunk control byte 0x03."""
    stream_header = b"\xfd7zXZ\x00\x00\x00" + zlib.crc32(b"\x00\x00").to_bytes(4, "little")
    # one LZMA2 filter with an 8 MiB dictionary, padded to 12 bytes with its CRC32
    block_header = b"\x02\x00\x21\x01\x16\x00\x00\x00"
    block_header += zlib.crc32(block_header).to_bytes(4, "little")
    lzma2 = lzma.compress(
        _half_of_a_large_control_tar(),
        format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA2}],
    )
    # LZMA2 data ends in the end marker 0x00, replaced here
    return stream_header + block_header + lzma2[:-1] + b"\x03"


@pytest.mark.parametrize(
    ("control_name", "control_tar"),
    [
        ("control.tar.xz", _xz_with_a_reserved_chunk()),
        ("control.tar.gz", _gz_with_a_reserved_block()),
    ],
    ids=["xz", "gz"],
)
def test_init_refuses_a_control_tar_that_does_not_decode(
    tmp_path: Path, control_name: str, control_tar: bytes
) -> None:
    """Damage past the first tar header surfaces as the codec's own error, not as a `TarError`."""
    storage = _storage(tmp_path, make_deb(control_tar=control_tar, control_name=control_name))

    with pytest.raises(DebFileException, match="Invalid .deb"):
        DebFile(storage, _PATH)

    assert storage.all_closed()


def test_get_package_refuses_a_control_member_that_is_not_a_regular_file(tmp_path: Path) -> None:
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        member = tarfile.TarInfo("./control")
        member.type = tarfile.DIRTYPE
        tar.addfile(member)
    deb = DebFile(_storage(tmp_path, make_deb(control_tar=buf.getvalue())), _PATH)

    with pytest.raises(DebFileException, match="not a bytes object"):
        deb.get_package()


def test_get_package_refuses_a_control_file_that_is_not_utf_8() -> None:
    """Debian Policy 5.1: a latin-1 control file is a typed refusal, not a traceback."""
    deb = DebFile(_streaming(_CONTROL + b"Maintainer: Jos\xe9\n"), _PATH)

    with pytest.raises(DebFileException, match="not UTF-8"):
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
        (b"Package: ../../../contrib/h/hello\nVersion: 1.0\n", "hello_1.0_amd64.deb"),
        (b"Package: Foo\nVersion: 1.0\n", "foo_1.0_amd64.deb"),
        (b"Package: foo\nVersion: 1.0\n", "incoming/.."),
        (b"Package: foo\nSource: ../../../contrib/h/hello\nVersion: 1.0\n", "foo_1.0_amd64.deb"),
        (b"Package: foo\nSource: Foo (1.0)\nVersion: 1.0\n", "foo_1.0_amd64.deb"),
        (b"Package: foo\nSource: foo (1.0\nVersion: 1.0\n", "foo_1.0_amd64.deb"),
        (b"Package: foo\nSource:\nVersion: 1.0\n", "foo_1.0_amd64.deb"),
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
    "version",
    [None, "", "!!", "1.0-", "1:", "1.0\n second", ":1.0"],
    ids=["missing", "empty", "garbage", "empty-revision", "epoch-only", "multi-line", "bare-colon"],
)
def test_get_package_refuses_a_control_without_a_debian_version(version: str | None) -> None:
    control = b"Package: foo\n" + (b"" if version is None else f"Version: {version}\n".encode())
    deb = DebFile(_streaming(control), _PATH, comp="main")

    with pytest.raises(DebFileException, match="names no valid Version"):
        deb.get_package()


@pytest.mark.parametrize("version", ["1.0", "2:1.2~rc1-3+b1", "0.9.8a-1", "a1.0-1"])
def test_get_package_accepts_a_debian_version(version: str) -> None:
    control = f"Package: foo\nVersion: {version}\n".encode()

    assert DebFile(_streaming(control), _PATH, comp="main").get_package().fields["Version"] == (
        version
    )


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


# ---------------------------------------------------------------------------
# control.tar decoded under a size limit
# ---------------------------------------------------------------------------


def _tar(members: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
    return buf.getvalue()


def _compress(suffix: str, data: bytes) -> bytes:
    if suffix == ".zst":
        return subprocess.run(
            ["zstd", "-c", "-q"], input=data, capture_output=True, check=True
        ).stdout
    return codec_for(suffix).compress(data)


# the compressed forms, each of which a small member can inflate from
_SUFFIXES = [
    ".gz",
    ".xz",
    ".bz2",
    ".lzma",
    pytest.param(".zst", marks=requires_binary("zstd")),
]


@pytest.mark.parametrize("suffix", ["", *_SUFFIXES])
def test_init_reads_a_control_tar_in_each_dpkg_compression(tmp_path: Path, suffix: str) -> None:
    tar = _compress(suffix, _tar({"./control": _CONTROL}))

    deb = DebFile(
        _storage(tmp_path, make_deb(control_tar=tar, control_name=f"control.tar{suffix}")), _PATH
    )

    assert deb.control == _CONTROL


@pytest.mark.parametrize("suffix", _SUFFIXES)
@pytest.mark.parametrize(
    "members",
    [
        {"./pad": b"\0" * (1 << 20), "./control": _CONTROL},
        {**{f"./e{i}": b"" for i in range(4096)}, "./control": _CONTROL},
    ],
    ids=["one-large-member", "many-empty-members"],
)
def test_init_refuses_a_control_tar_bomb_and_closes_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, members: dict[str, bytes]
) -> None:
    """A small member that inflates past the decoded cap is refused before the tar is walked."""
    monkeypatch.setattr(debfile_module, "MAX_CONTROL_TAR_BYTES", 64 * 1024)
    tar = _compress(suffix, _tar(members))
    assert len(tar) < 64 * 1024
    storage = _storage(tmp_path, make_deb(control_tar=tar, control_name=f"control.tar{suffix}"))

    with pytest.raises(DebFileException, match="decodes past"):
        DebFile(storage, _PATH)
    assert storage.all_closed()


@pytest.mark.parametrize(
    "deb",
    [
        make_deb(control_tar=b"x", control_name="control.tar.lz4"),
        make_deb(control_name=None),
    ],
    ids=["unknown-compression-alone", "no-control-tar"],
)
def test_init_refuses_a_deb_without_a_control_tar_dpkg_reads(tmp_path: Path, deb: bytes) -> None:
    """python-debian refuses it before debkit picks a member."""
    with pytest.raises(DebFileException, match="missing required part"):
        DebFile(_storage(tmp_path, deb), _PATH)


def test_init_refuses_an_unknown_control_tar_ahead_of_a_known_one(tmp_path: Path) -> None:
    """The first `control.tar*` member is read, so its compression must be one debkit decodes."""
    deb = make_deb(before_control={"control.tar.lz4": b"x"})

    with pytest.raises(DebFileException, match="unknown compression$"):
        DebFile(_storage(tmp_path, deb), _PATH)
