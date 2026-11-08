# Required test contract

Owns: required test coverage, harness rules, and gates. Routing →
[AGENTS.md](AGENTS.md#canonical-sources). Tests assert the owning contract directly; definitions are
not copied here.

## Harness and gates

- Run with `uv run pytest` → [SETUP.md](SETUP.md#test).
- Keep tests isolated: no real network, no real cloud account, no real user GPG home. S3-backed
  tests use the session-scoped `moto_server` fixture; CLI and action tests use `fake_pars`, both
  from `tests/conftest.py`.
- `tests/conftest.py` clears every `DEBKIT_*` and `TXSTORE_*` variable before each test; a test
  sets one with `monkeypatch.setenv`.
- Factory helpers and storage stubs live in `tests/helpers/`; prefer them over ad-hoc setup.
- Import txstore through its façade only; a storage need its public names do not meet goes to
  txstore, not into a private import.
- Test the owning layer directly; do not rely on broader integration tests to cover a local rule.
- Name test functions `test_<subject>_<scenario>`; parameterize cases that differ only in input.
- Keep every parameter set non-empty: `empty_parameter_set_mark = "fail_at_collect"` in
  `pyproject.toml` turns an empty set into a collection error, not a skip.

| Gate              | Contract                                                                                                                            |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Validation        | Every rule in `VALIDATION.md` has a success and a failure case.                                                                     |
| Configuration     | Every `CONFIG.md` flag resolves in the [resolution order](CONFIG.md#resolution); `tests/test_argparser.py` holds the cases.         |
| Exceptions        | Every class in `EXCEPTIONS.md` is raised by at least one test; every exit group is asserted by message in `tests/test_cli_main.py`. |
| Public methods    | Every validator and public method has at least one success and one failure case.                                                    |
| Coverage          | `uv run pytest --cov` measures `debkit` and fails below the branch-coverage `fail_under` in `pyproject.toml`.                       |
| Scripts           | `scripts/` is outside the coverage source; the Contract docs and Diagrams rows hold its tests.                                      |
| Contract docs     | `tests/test_check_docs.py` runs `scripts/check_docs.py` against the repository.                                                     |
| Diagrams          | `tests/test_render_diagrams.py` runs `scripts/render_diagrams.py --check` against the repository → [SETUP.md](SETUP.md#diagrams).   |
| Package contracts | `tests/test_package.py` → [Package contracts](#package-contracts).                                                                  |

## Required coverage

| Area                    | Required assertion                                                                                                                                                                                                                                                                              |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CLI                     | Command dispatch and exit-message mapping for every group.                                                                                                                                                                                                                                      |
| Options | Flag over variable over default; a switch's `--no-` form overrides its variable; an empty variable counts as unset; an invalid variable or one outside the choices exits 2 naming it; a required option resolves from its variable or exits 2 naming both; a secret resolves from its variable and has no flag; no secret value appears in `--help`. |
| Logging                 | Plain and JSON lines; the DEBUG format and the quiet third-party loggers.                                                                                                                                                                                                                       |
| Actions                 | Orchestration, argument combinations, error propagation; transaction wiring: begin and commit on success, discard on failure, no-op on a non-transactional storage, and the retention policy built by `Action.__init__` reaches `maybe_transaction`.                                            |
| Storage construction    | A bare path becomes an absolute `fs://` URL; the resolved flags reach `make_storage` → [STORAGE.md](STORAGE.md#construction).                                                                                                                                                                   |
| History commands        | `log`, `revert`, and `prune` output in brief and JSON, dry run, the guards → [STORAGE.md](STORAGE.md#history-commands).                                                                                                                                                                         |
| Repo classes            | Parse and serialize round-trips, mutation invariants, checksum decisions, sync/diff behavior; the `Sources` expansion into one record per payload file and its refusal to write.                                                                                                                |
| Sync index set          | A Release-listed index present at the source is copied, one absent there is skipped, and one already in the target is not purged.                                                                                                                                                               |
| Sync index filters      | The component, architecture, and exclude-glob filters each drop an entry, the exclude glob reaching a component-discovered index while the root Release files survive it; source indices follow the `source` selection, and an index directory nothing parses is dropped.                       |
| Sync index safety | An unsafe Release-listed path is dropped and logged, the source is never listed or probed, an unproven index the source lacks is skipped at transfer while a proven one fails the run → [VALIDATION.md](VALIDATION.md#index-selection), and the flat layouts round-trip their prefix through `release_key`. |
| Sync index copying      | An index whose target digests match is skipped, a stale one is recopied, the root Release files copy only as the bytes their load verified and only when both signed forms agree, and a source Release hash change forces indices with no package diff.                                         |
| Sync index ownership    | A stanza naming a Release-listed index is neither copied nor removed as a package, whichever side holds it, and the index set still purges one it drops → [VALIDATION.md](VALIDATION.md#index-selection).                                                                                       |
| Sync index Release skip | The Release-block skip with its metadata fallback → [VALIDATION.md](VALIDATION.md#index-selection) is asserted from both sides.                                                                                                                                                                 |
| Sync index recovery     | The unpublished-entry, absent-entry, and unloaded-target rules → [VALIDATION.md](VALIDATION.md#index-selection) are each asserted, an absent index recovering with nothing else to sync.                                                                                                        |
| Validation              | Every documented boundary rule, success and failure.                                                                                                                                                                                                                                            |
| Crypto                  | GPG verify and sign paths, skip-verify branches; the checksum source order `Repo.verify_checksum` walks; hash-block parsing and `HexDigests` in `repo/hashes.py`; each Release freshness rule.                                                                                                  |

## Package contracts

`tests/test_package.py` walks `src/debkit` with `ast` rather than importing it.

| Area                 | Required assertion                                                                                                                                              |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Version              | `debkit.__version__` equals the installed distribution version.                                                                                                 |
| Runtime dependencies | Runtime imports are the standard library or the declared dependencies, and `pyproject.toml` declares exactly those → [STRUCTURE.md](STRUCTURE.md#dependencies). |
| Environment          | Only `_env` reads `os.environ` → [CONFIG.md](CONFIG.md#resolution).                                                                                             |
| txstore boundary     | Every txstore import is the façade and names a member of `txstore.__all__`.                                                                                     |
| Import directions    | Each leaf imports only its row, `repo` and `action` never import upward, no import is deferred, and only `cli` calls `sys.exit`.                                |

## File layout

One file per source module or contract surface, named `test_<module>.py`. The entry shim
(`__main__.py`) and the version-only `__init__.py` carry no test file. Files that cover a contract
surface rather than a single module:

| File                      | Covers                                                                                  |
| ------------------------- | --------------------------------------------------------------------------------------- |
| `test_cli_main.py`        | CLI dispatch and exit groups.                                                           |
| `test_argparser.py`       | Parser, every subcommand, and option resolution through `_options`.                     |
| `test_package.py`         | Package contracts → [Package contracts](#package-contracts).                            |
| `test_repo_package.py`    | `repo/package.py`.                                                                      |
| `test_hashes.py`          | `repo/hashes.py`: hash-block parsing and `HexDigests`.                                  |
| `test_action_history.py`  | `log` / `revert` / `prune` wiring: brief and JSON output, dry run, guards.              |
| `test_check_docs.py`      | Contract-doc checker, loaded from `scripts/check_docs.py` by path.                      |
| `test_render_diagrams.py` | Diagram renderer and freshness check, loaded from `scripts/render_diagrams.py` by path. |

## Maintenance

For each change: update the owning contract, add the narrowest test that can falsify the change
(success and failure), then update only the distinct assertion here. Extend an existing file when
the behavior belongs to that module; add a file only for a new contract surface. Prefer
representative cases over encyclopedic matrices, and never copy payloads, field limits, or error
explanations from the owning contract.
