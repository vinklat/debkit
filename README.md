# debkit

Command-line tool for building, inspecting, and mirroring Debian package repositories on a local
filesystem, S3-compatible object storage, or HTTP(S) servers, with GPG-signed Release files,
streamed checksum verification, and transactional writes you can revert.

Current version: **0.1.0**. Planned work → [ROADMAP.md](ROADMAP.md).

## Capabilities

- Inspect a repository: `dump`, `list`, `stats`; verify consistency with `check` and repair with
  `check --fix`.
- Insert packages from a `.changes` upload or a single `.deb`; remove packages by field pattern.
- Mirror a repository with `sync`: architecture and index filters, source packages, indices-only
  mode, Release freshness checks, and optional re-verification of existing packages.
- Transactional writes on `fs://` and `s3://` targets, with `log`, `revert` of the latest change,
  and `prune` of old history.
- GPG verification and signing of `Release` and `InRelease`; checksum verification against every
  index.
- Every option as a flag or a `DEBKIT_*` environment variable, for CI pipelines.
- Storage through [txstore](https://github.com/vinklat/txstore): `fs`, `s3`, and read-only
  `http`/`https`.

## Quick start

Released versions are published to [PyPI](https://pypi.org/project/debkit/). Prerequisites (the
Python floor, the GnuPG and `zstd` binaries, `fs://` extended attributes) →
[SETUP.md](SETUP.md#prerequisites-and-install).

debkit verifies every Release it reads from a repository it trusts (a `sync` source, an `insert` or
`remove` target, the inspecting commands) against `~/.gnupg/trustedkeys.gpg` by default, the keyring
gpgv reads → [CONFIG.md](CONFIG.md#global-flags); a `sync` target is read unverified →
[CRYPTO.md](CRYPTO.md#gpg). The examples keep a dedicated home instead, named by
`DEBKIT_GNUPG_HOME`; import the Debian archive keys into it before reading deb.debian.org. The
keyring file comes from the `debian-archive-keyring` package on Debian and Ubuntu.

```bash
pip install debkit
export DEBKIT_GNUPG_HOME="$HOME/.debkit-gnupg"
mkdir -m 700 -p "$DEBKIT_GNUPG_HOME"
gpg --homedir "$DEBKIT_GNUPG_HOME" --no-default-keyring --keyring trustedkeys.gpg \
    --import /usr/share/keyrings/debian-archive-keyring.gpg
debkit --target-url "https://deb.debian.org/debian bookworm main" stats
```

Build a signed local repository from a `.deb`: debkit signs with the key `DEBKIT_GNUPG_SIGN_KEY`
names and verifies what it reads back against its `trustedkeys.gpg`. The key below has no
passphrase, which suits a local demo only; protect a real one and pass `DEBKIT_GNUPG_PASSPHRASE`.

```bash
export DEBKIT_GNUPG_SIGN_KEY="repo@example.com"
gpg --homedir "$DEBKIT_GNUPG_HOME" --batch --passphrase '' \
    --quick-gen-key "Repo signing <$DEBKIT_GNUPG_SIGN_KEY>"
gpg --homedir "$DEBKIT_GNUPG_HOME" --export "$DEBKIT_GNUPG_SIGN_KEY" | \
    gpg --homedir "$DEBKIT_GNUPG_HOME" --no-default-keyring --keyring trustedkeys.gpg --import
debkit --target-url "/srv/repo bookworm main" \
    insert --incoming-url ./incoming --deb-filename hello_1.0_amd64.deb
debkit --target-url "/srv/repo bookworm main" list
debkit --target-url /srv/repo log
```

Install, test, and release details → [SETUP.md](SETUP.md); flags and variables →
[CONFIG.md](CONFIG.md); worked examples → [docs/guides/examples.md](docs/guides/examples.md).

## Documentation

| Location | Audience | Authority |
| -- | -- | -- |
| `/` root contract documents | Developers and AI agents | Canonical |
| [docs/guides/](docs/guides/README.md) | Anyone operating the CLI | Non-canonical how-tos and examples |
| [docs/development/](docs/development/README.md) | Developers and contributors | Non-canonical diagrams, code maps, explanations |

Domain owners and agent navigation → [AGENTS.md](AGENTS.md#canonical-sources).

## Technology

Python, [txstore](https://github.com/vinklat/txstore), python-debian, python-gnupg, humanize,
pytest, Ruff, and mypy.

## License

MIT → [LICENSE](LICENSE).
