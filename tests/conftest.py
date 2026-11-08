"""Pytest fixtures for debkit.

See TESTING.md for the test matrix and conventions.
"""

from __future__ import annotations

import os
from argparse import Namespace
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every ``DEBKIT_*`` and ``TXSTORE_*`` variable, so no test reads the caller's shell."""
    for name in list(os.environ):
        if name.startswith(("DEBKIT_", "TXSTORE_")):
            monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def moto_server() -> Iterator[str]:
    """Spin up a single in-process ``moto`` server for the whole session.

    Yields:
        Endpoint URL (``http://127.0.0.1:<port>``) suitable for passing to
        ``StorageS3`` via its ``entrypoint`` argument.
    """
    moto_server_module = pytest.importorskip("moto.server")

    # Proxy env vars in the sandbox would otherwise force boto3 through
    # an outbound proxy and never reach the in-process server.
    proxy_keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    saved = {k: os.environ.pop(k, None) for k in proxy_keys}
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

    server = moto_server_module.ThreadedMotoServer(port=0, verbose=False)
    server.start()
    port = server._server.socket.getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


@pytest.fixture
def fake_pars() -> Namespace:
    """Build a minimal argparse.Namespace mimicking the parsed CLI.

    Provides the global flags `cli.main()` reads unconditionally before dispatch.
    Tests override `.command` and any per-command attributes as needed.
    """
    return Namespace(
        command=None,
        log_level="INFO",
        log_format="plain",
        skip_verify=True,
        skip_verify_ssl=True,
        skip_verify_gpg=True,
    )
