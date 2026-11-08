# Setup and release contract

Owns: install, run, test, quality gates, diagrams, dependency policy, release, and upgrade notes.
Routing → [AGENTS.md](AGENTS.md#canonical-sources).

## Prerequisites and install

- Python 3.13 or newer, and a GnuPG binary on `PATH` for `python-gnupg` →
  [CRYPTO.md](CRYPTO.md#gpg). `fs://` metadata needs a filesystem with extended attributes.
- Users: `pip install debkit` (or `pipx install debkit`).
- Contributors: [uv](https://docs.astral.sh/uv/); dependencies resolve from public PyPI only.

```bash
uv sync
```

## Run

Flags and `DEBKIT_*` variables → [CONFIG.md](CONFIG.md); worked examples →
[docs/guides/examples.md](docs/guides/examples.md).

```bash
uv run debkit --target-url "https://deb.debian.org/debian bookworm main" list
uv run python -m debkit --help
```

## Test

Harness rules → [TESTING.md](TESTING.md#harness-and-gates); required coverage →
[TESTING.md](TESTING.md#required-coverage).

```bash
uv run pytest
uv run pytest --cov
```

Real-gpg tests need the `gpg` binary on `PATH`; skip and CI behavior →
[TESTING.md](TESTING.md#harness-and-gates).

## Quality gates

Run every row before submitting. `.github/workflows/ci.yml` runs the check rows on pushes to `main`,
on pull requests, weekly, and before each release; it checks the lockfile through
`uv sync --locked`.

| Check | Command |
| -- | -- |
| Format | `uv run ruff format` |
| Format check | `uv run ruff format --check .` |
| Lint | `uv run ruff check .` (locally `--fix`) |
| Type check | `uv run mypy`; mode and scope → [STYLE.md](STYLE.md#python-baseline) |
| Tests | `uv run pytest --cov` |
| Lockfile | `uv lock --check` |
| Contract docs | `uv run python scripts/check_docs.py` |
| Diagrams | `uv run python scripts/render_diagrams.py --check` |
| Build | `uv build` |

`check_docs.py` verifies links and anchors in root contracts and `docs/`, contract anchors cited in
code, `CONFIG.md` flag and variable parity with the option registrations, `EXCEPTIONS.md` parity
with the exception classes and the `cli.py` exit groups, an exit group, a raise, and a test for
every exception class, every `Exception (group)` notation, 100-column prose, table rows with as
many cells as their header, no `TODO` markers in contracts, the `docs/*/README.md` indexes, the
README version, PyPI-only package sources, and the [diagram](#diagrams) fence and embed rules.

## Diagrams

GitHub does not render PlantUML, so diagrams are committed as SVG next to their source.

| Rule | Detail |
| -- | -- |
| Source | One diagram per `docs/<dir>/diagrams/*.puml`, wrapped in a bare `@startuml`/`@enduml`. |
| No URL | A source contains no URL: renders run offline. |
| Embed | A document embeds each rendered `.svg` as an inline Markdown image with a bare relative path and an optional title; reference-style images and `<path>` destinations do not count. `check_docs.py` reports an SVG that no document displays that way. |
| Fences | No document opens a `plantuml` or `puml` fence; `check_docs.py` reports one outside another fenced block. |
| Render | `uv run python scripts/render_diagrams.py` renders each stale source through the pinned `plantuml/plantuml` Docker image with no network. |
| Force | `render_diagrams.py --force` renders every source, stale or not. |
| Docker | Rendering needs Docker; `--check` does not. |
| Stamp | Line 1 of each SVG carries the PlantUML version and a SHA-256 of the image, the PlantUML arguments, and the source. |
| Check | `render_diagrams.py --check` reports, offline, a `.puml` under `docs/` outside a `diagrams/` directory, a URL in a source, a missing or stale SVG, and an SVG without its source. |
| Upgrade | Changing `IMAGE` or `RENDER_ARGS` in the script marks every SVG stale; re-render and commit them together. |
| Diff | `.gitattributes` marks rendered SVGs `linguist-generated`, so GitHub collapses their diffs. |

## Dependency policy

debkit's dependencies stay installable from Debian testing, as txstore's do.

| Rule | Detail |
| -- | -- |
| Floors | Every runtime dependency's lower bound is available in Debian testing (forky until its release, then the next testing). |
| txstore | Pinned below its next minor version; a floor raise follows a txstore change debkit needs → [STORAGE.md](STORAGE.md#maintenance). |
| Python | Every Python version Debian testing ships is supported and in CI. |
| Typing | Stub and typing-only packages are imported only under `TYPE_CHECKING` and stay in the `dev` group. |
| Tests | Run offline from the source distribution, with `pytest` and `moto` as the only test-only packages and no dependence on `addopts` or a coverage plugin. |
| Source distribution | Holds `src`, `tests`, `docs`, `scripts`, the root documents, `LICENSE`, and `uv.lock`, so a distribution build and its tests need no git checkout. |
| CI | The `debian-testing` job runs `.github/debian-test.sh`: the suite on Debian's packaged dependencies, pip installing debkit with no index and txstore from PyPI, hash-checked against `uv.lock` → [ROADMAP.md](ROADMAP.md#known-deviations); triggers → [quality gates](#quality-gates). |

A floor raise names the Debian testing version that satisfies it in the change description.

## Release

`.github/workflows/release.yml` publishes to PyPI with Trusted Publishing; no API token is stored.

| Item | Rule |
| -- | -- |
| Version | `pyproject.toml` `[project].version` and the `README.md` "Current version" line. |
| Version parity | `check_docs.py` fails when the two differ. |
| `__version__` | Read from the installed distribution. |
| Version bump | `uv version --bump patch` (or `minor`, `major`), the README line to match, and every `Unreleased:` upgrade note renamed to `<version>:`. |
| Steps | Version bump → [quality gates](#quality-gates) → merge to `main` → `git tag vX.Y.Z` on `main` → `git push origin vX.Y.Z`. |
| Trigger | Pushing a tag `vX.Y.Z`, with or without a suffix (e.g. `v0.2.0rc1`). |
| Gates | `ci.yml` runs in full before the build. |
| Tag checks | The tagged commit is on `main`, and the tag without its `v` equals the `pyproject.toml` version, or the release stops. |
| Build | sdist and wheel, without the uv cache. |
| Build backend | hatchling only from the hash-pinned `.github/build-constraints.txt`; its header holds the refresh command. |
| Build backend floor | `[build-system]` keeps a floor, so a Debian build uses Debian's hatchling. |
| PyPI description | `README.md`, with its relative links rewritten to the tag on GitHub; the repo copy is unchanged. |
| Checks | Run after the artifact upload, so no tool they fetch alters the published files; a failure stops the release. |
| Link check | No relative link is left in the wheel's description. |
| Metadata check | `twine check --strict` passes. |
| Smoke test | The wheel, installed alone with its dependencies, imports with `debkit.__version__` equal to the tag version, and `debkit --version` prints it. |
| Stages | TestPyPI, then PyPI, then a GitHub release with the sdist and wheel and a link to the [upgrade notes](#upgrade-notes). |
| GitHub environments | `testpypi` and `pypi`; `pypi` has a required reviewer, so the PyPI upload waits for approval. |
| Tag ruleset | A repository ruleset limits who creates `v*` tags. |
| Trusted publisher identity | Owner `vinklat`, repo `debkit`, workflow `release.yml`. |
| Trusted publisher on TestPyPI | That identity with environment `testpypi`. |
| Trusted publisher on PyPI | That identity with environment `pypi`. |

## Upgrade notes

### 0.1.0

Initial public release. debkit continues repo-deb-factory 5.1.2 under a new name, on the public
txstore storage package; repositories, footmarks, and `.transactions.json` histories written by
either tool are read by the other.

| Change | Migration |
| -- | -- |
| Command and package | `repo-deb-factory` → `debkit`; `python -m repo_deb_factory` → `python -m debkit`. |
| Variables | Every variable gains the `DEBKIT_` prefix: `TARGET_URL` → `DEBKIT_TARGET_URL`, `S3_SECRET` → `DEBKIT_S3_SECRET`, and so on → [CONFIG.md](CONFIG.md). |
| Switches | `--skip-verify 1` → `--skip-verify`; `--skip-verify-gpg false` → omit it or pass `--no-skip-verify-gpg`. The same holds for `--skip-verify-ssl` and `--allow-release-rollback`. |
| Logging | `-l` gains `DEBKIT_LOG_LEVEL` and drops `TRACING`; `--log-format json` is new. |
| Filesystem URLs | `fs://` takes an absolute path only: `fs://debian` → `debian` or `fs:///abs/debian`; the `sync --source-url` default is `.` → [VALIDATION.md](VALIDATION.md#storage-urls). |
| S3 bucket ACL | A created bucket no longer inherits `--s3-default-acl`; pass `--s3-bucket-acl` to set one. |
| S3 credentials | Unset S3 credentials fall back to `TXSTORE_S3_ACCESS` / `TXSTORE_S3_SECRET`. |
| `--history-max-age` | Capped at 36500 days. |
| Help | `--help` no longer prints secret values taken from the environment. |
| Secret flags | `-t`/`--s3-secret` and `--gnupg-passphrase` are gone: set `DEBKIT_S3_SECRET` and `DEBKIT_GNUPG_PASSPHRASE` instead → [CONFIG.md](CONFIG.md#secrets). |
| Pool layout | An inserted `.deb` lands in Debian's source pool layout under its source package name → [VALIDATION.md](VALIDATION.md#filename-safety). |
| Existing pool keys | Keys written before the change stay valid. |
| Re-inserting a version | Inserting an existing version again stores it under its new key and adds a second stanza. |
| Signing without a passphrase | Fixed: with no `DEBKIT_GNUPG_PASSPHRASE`, gpg read the first line of the Release as the passphrase, so `Release.gpg` and `InRelease` signed the Release without it. Re-sign such a repository with any `insert` or `remove`. |
