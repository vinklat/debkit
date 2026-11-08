# Exception contract

Owns: domain exception classes, their owning modules, and the CLI exit groups, messages, and
statuses. Routing → [AGENTS.md](AGENTS.md#canonical-sources).

## Rules

- Raise typed domain exceptions, never bare `Exception`.
- Define an exception in the module that owns the failure. A txstore class is imported from its
  façade and grouped by `cli.py` like any domain class; its raising conditions are txstore's.
- Preserve the original cause with `raise ... from exc` when wrapping.
- Catch broad exceptions only at the CLI boundary; only `cli.py` groups exceptions and exits.
- Contracts write failures as `condition → ExceptionClass (group)` using the groups below.

## Domain exceptions

| Exception | Module | Meaning |
| -- | -- | -- |
| `ActionException` | `action/__init__.py` | Orchestration failure, including "no package matched" and unsupported history operations |
| `RepoReadException` | `repo/__init__.py` | Repository read or update failure |
| `RepoSyncException` | `repo/repo_sync.py` | Sync or mirror failure |
| `ReleaseReadException` | `repo/release.py` | Release parse or verify failure |
| `ReleaseNotFoundException` | `repo/release.py` | Neither a root `Release` nor an `InRelease` exists; subclass of `ReleaseReadException` |
| `ReleaseWriteException` | `repo/release.py` | Release signing failure, including an empty signature from GnuPG |
| `ReleaseFreshnessException` | `repo/release.py` | Source Release expired, older than the target Release, missing its `Date`, or carrying a malformed date |
| `PackagesException` | `repo/packages.py` | Packages or Sources index parse or serialize failure, or a mutation of the read-only Sources index |
| `PackagesNotFoundException` | `repo/packages.py` | No index file the Release lists for an arch is in storage; subclass of `PackagesException` |
| `PackageException` | `repo/package.py` | Single package parse or serialize failure |
| `ComponentException` | `repo/component.py` | Component load failure, or an index slot refused for an invalid architecture |
| `ChangesException` | `repo/changes.py` | `.changes` parse or validation failure |
| `DebFileException` | `repo/debfile.py` | `.deb` inspection failure |
| `RepoParamsException` | `repo/params.py` | Invalid deb source or params |
| `GPGVerifyException` | `repo/gnupg.py` | GPG verification or keyring failure |
| `ChecksumException` | `repo/hashes.py` | Digest mismatch or distrustful object |
| `StorageException` | `txstore` | Backend failure; at construction, including a missing S3 credential, re-raised as `RepoReadException` for `--target-url` and `--source-url` → [STORAGE.md](STORAGE.md#construction) |
| `StorageNotFoundException` | `txstore` | Object missing on `get` / `get_stream`; subclass of `StorageException` |

## CLI exit groups

`cli.py` logs the original exception, prints the group's exit message, and exits 1. The caught lists
are enumerations and mirror the `except` tuples in `cli.py` exactly.

| Group | Caught exceptions | Exit message | Extra warning |
| -- | -- | -- | -- |
| Read | `RepoReadException`, `GPGVerifyException`, `ComponentException`, `ChangesException`, `DebFileException`, `PackageException`, `PackagesException`, `ReleaseReadException` | `Can't read repository.` | none |
| Distrustful | `ChecksumException`, `ReleaseFreshnessException` | `Distrustful object.` | none |
| Write | `ReleaseWriteException` | `Can't write repository.` | `Some data may have been left unfinished.` |
| Sync | `RepoSyncException` | `Sync failed.` | `Some data may have remained incomplete.` |
| Generic | `ActionException`, `RepoParamsException`, `StorageException` | original message | none |

Exits outside the groups:

| Condition | Exit |
| -- | -- |
| Argparse usage error, including `ArgumentTypeError` from a type converter, an invalid `DEBKIT_*` value, or a missing required option | Usage text, status 2 (group Usage) |
| `check` finds an inconsistent repository | `Repository is not consistent.`, status 1 |
| `NotImplementedError` from a capability stub | Uncaught → [ROADMAP.md](ROADMAP.md#known-deviations) |
| A builtin `ValueError` or other programming error | Uncaught: a traceback, never an input-error message |

## Orchestration vs repo semantics

| Condition | Result |
| -- | -- |
| `remove` matches no package | `RepoRW.remove_pkg` returns `False`; `ActionPkg.remove` raises `ActionException` (Generic). |
| `insert` target without a root `Release` or `InRelease` | `ReleaseNotFoundException` only: a new repository is set up; any other `ReleaseReadException` (Read) stops the run, so an existing repository is never set up again. |
| `sync` or `check` target without a root `Release` or `InRelease` | `ReleaseNotFoundException` only: `sync` treats the target as empty, `check` reports it inconsistent; any other `ReleaseReadException` (Read) stops the run. |
| Release file cannot be signed | `ReleaseWriteException` (Write). |
| Release file cannot be put | `StorageException` (Generic). |
| `log` / `revert` / `prune` failures | → [STORAGE.md](STORAGE.md#history-commands) |
| A storage URL txstore refuses | → [VALIDATION.md](VALIDATION.md#storage-urls) |

## Maintenance

Add the class in the owning module and its row here, and adjust the catch tuple in `cli.py` when
exit behavior changes; `tests/test_cli_main.py` asserts every group message →
[TESTING.md](TESTING.md#required-coverage). `scripts/check_docs.py` verifies both tables against the
code; raise and test coverage per class → [TESTING.md](TESTING.md#required-coverage).
