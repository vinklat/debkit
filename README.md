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

### Installation

Released versions are published to [PyPI](https://pypi.org/project/debkit/). Prerequisites →
[SETUP.md](SETUP.md#prerequisites-and-install). Other examples →
[Quick how-to](docs/guides/examples.md).

```bash
pip install debkit
```

### Example: working with a remote repository (Debian bookworm)

#### GPG setup for repository

debkit verifies the repositories it reads against trusted keys by default. Import every key that
signs it. Bookworm's carries three (its archive key, the trixie archive key, and its stable release
key):

```bash
export DEBKIT_GNUPG_HOME="$HOME/.debkit-gnupg"
mkdir -m 700 -p "$DEBKIT_GNUPG_HOME"
curl -sS https://ftp-master.debian.org/keys/{archive-key-12,archive-key-13,release-12}.asc | \
    gpg --homedir "$DEBKIT_GNUPG_HOME" --no-default-keyring --keyring trustedkeys.gpg --import
```

#### Operating with a remote repository

```bash
# stats of a remote repository
debkit --target-url "https://deb.debian.org/debian bookworm main" stats
# list packages in a repository
debkit --target-url "https://deb.debian.org/debian bookworm main" list
# mirror a remote repository into `/srv/repo`
debkit --target-url "/srv/repo bookworm main" sync \
    --source-url "https://deb.debian.org/debian bookworm main" \
    --indices-only
```

### Example: building a local repository from a `.deb`

#### GPG setup for local repository

```bash
export DEBKIT_GNUPG_SIGN_KEY="repo@example.com"
gpg --homedir "$DEBKIT_GNUPG_HOME" --batch --passphrase '' \
    --quick-gen-key "Repo signing <$DEBKIT_GNUPG_SIGN_KEY>"
gpg --homedir "$DEBKIT_GNUPG_HOME" --export "$DEBKIT_GNUPG_SIGN_KEY" | \
    gpg --homedir "$DEBKIT_GNUPG_HOME" --no-default-keyring --keyring trustedkeys.gpg --import
```

#### Building a local repository

debkit signs the repository with the key `DEBKIT_GNUPG_SIGN_KEY`. The key generated above has no
passphrase, which suits a local demo only; protect a real one and pass `DEBKIT_GNUPG_PASSPHRASE`.

```bash
debkit --target-url "/srv/repo bookworm main" \
    insert --incoming-url ./incoming --deb-filename hello_1.0_amd64.deb
debkit --target-url "/srv/repo bookworm main" list
debkit --target-url /srv/repo log
```

## Documentation

| Location | Audience | Authority |
| -- | -- | -- |
| `/` root contract documents | Developers and AI agents | Canonical |
| [docs/guides/](docs/guides/README.md) | Anyone operating the CLI | Non-canonical how-tos and examples |
| [docs/development/](docs/development/README.md) | Developers and contributors | Non-canonical diagrams, code maps, explanations |

Domain owners and agent navigation → [AGENTS.md](AGENTS.md#canonical-sources).

## Technology

Python and [txstore](https://github.com/vinklat/txstore); runtime dependencies →
[STRUCTURE.md](STRUCTURE.md#dependencies). Tests and gates use pytest, Ruff, and mypy →
[SETUP.md](SETUP.md#quality-gates).

## License

MIT → [LICENSE](LICENSE).
