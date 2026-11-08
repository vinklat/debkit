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

Released versions are published to [PyPI](https://pypi.org/project/debkit/). debkit needs Python
3.13 or newer and a GnuPG binary on `PATH`.

debkit verifies every Release it reads against `./.gnupg/trustedkeys.gpg` by default →
[CONFIG.md](CONFIG.md#global-flags), so import the Debian archive keys before reading
deb.debian.org; the keyring file comes from the `debian-archive-keyring` package on Debian and
Ubuntu.

```bash
pip install debkit
mkdir -m 700 -p .gnupg
gpg --homedir .gnupg --no-default-keyring --keyring trustedkeys.gpg \
    --import /usr/share/keyrings/debian-archive-keyring.gpg
debkit --target-url "https://deb.debian.org/debian bookworm main" stats
```

Build a signed local repository from a `.deb`: debkit signs with the key in `./.gnupg` and verifies
what it reads back against `./.gnupg/trustedkeys.gpg`.

```bash
gpg --homedir .gnupg --batch --passphrase '' --quick-gen-key "Repo signing <repo@example.com>"
gpg --homedir .gnupg --export | gpg --homedir .gnupg --no-default-keyring \
    --keyring trustedkeys.gpg --import
debkit --target-url "/srv/repo bookworm main" \
    insert --incoming-url ./incoming --deb-filename hello_1.0_amd64.deb
debkit --target-url "/srv/repo bookworm main" list
debkit --target-url /srv/repo log
```

Prerequisites and details → [SETUP.md](SETUP.md); flags and variables → [CONFIG.md](CONFIG.md);
worked examples → [docs/guides/examples.md](docs/guides/examples.md).

## Documentation

| Location                                        | Audience                    | Authority                                       |
| ----------------------------------------------- | --------------------------- | ----------------------------------------------- |
| `/` root contract documents                     | Developers and AI agents    | Canonical                                       |
| [docs/guides/](docs/guides/README.md)           | Anyone operating the CLI    | Non-canonical how-tos and examples              |
| [docs/development/](docs/development/README.md) | Developers and contributors | Non-canonical diagrams, code maps, explanations |

Domain owners and agent navigation → [AGENTS.md](AGENTS.md#canonical-sources).

## Technology

Python 3.13+, [txstore](https://github.com/vinklat/txstore), python-debian, python-gnupg, humanize,
pytest, Ruff, and mypy.

## License

MIT → [LICENSE](LICENSE).
