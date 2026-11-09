# Required test contract

Owns: required test coverage, harness rules, and gates. Routing →
[AGENTS.md](AGENTS.md#canonical-sources). Tests assert the owning contract directly; definitions are
not copied here.

## Harness and gates

- Run with `uv run pytest` → [SETUP.md](SETUP.md#test).
- Keep tests isolated: no real network, no real cloud account, no real user GPG home; a throwaway
  home from `tests/helpers/gpg.py` is allowed. Tests that run the gpg or zstd binary carry
  `requires_binary` from `tests/helpers/binaries.py`: they skip without it, except under `CI`, where
  they fail instead. S3-backed tests use the session-scoped `moto_server` fixture; CLI and action
  tests use `fake_pars`, both from `tests/conftest.py`.
- `tests/conftest.py` clears every `DEBKIT_*` and `TXSTORE_*` variable before each test; a test sets
  one with `monkeypatch.setenv`.
- Factory helpers and storage stubs live in `tests/helpers/`; prefer them over ad-hoc setup. A real
  repository tree on disk comes from `tests/helpers/trees.py`.
- Import txstore through its façade only; a storage need its public names do not meet goes to
  txstore, not into a private import.
- Test the owning layer directly; do not rely on broader integration tests to cover a local rule.
- A rule that a stubbed `Release` or `Component` decides in a unit test also gets one case on a real
  tree over `StorageFS`, so the stub cannot hide a disagreement with the real class: the sync index
  set, copying, and Release skip rows, the check, and the shared root.
- Name test functions `test_<subject>_<scenario>`; parameterize cases that differ only in input.
- Keep every parameter set non-empty: `empty_parameter_set_mark = "fail_at_collect"` in
  `pyproject.toml` turns an empty set into a collection error, not a skip.

| Gate | Contract |
| -- | -- |
| Validation | Every rule in `VALIDATION.md` has a success and a failure case. |
| Configuration | Every `CONFIG.md` flag resolves in the [resolution order](CONFIG.md#resolution); `tests/test_argparser.py` holds the cases. |
| Exception classes | Every class debkit defines is raised in `src/` and named by a `pytest.raises` outside `tests/test_cli_main.py`, enforced by `check_docs.py`; txstore classes are exempt, since txstore owns their raising. |
| Exit groups | Every exit group is asserted by message in `tests/test_cli_main.py`. |
| Public methods | Every validator and public method has at least one success and one failure case. |
| Coverage | `uv run pytest --cov` measures `debkit` and fails below the branch-coverage `fail_under` in `pyproject.toml`. |
| Scripts | `scripts/` is outside the coverage source; the Contract docs and Diagrams rows hold its tests. |
| Contract docs | `tests/test_check_docs.py` runs `scripts/check_docs.py` against the repository. |
| Diagrams | `tests/test_render_diagrams.py` runs `scripts/render_diagrams.py --check` against the repository → [SETUP.md](SETUP.md#diagrams). |
| Package contracts | `tests/test_package.py` → [Package contracts](#package-contracts). |

## Required coverage

| Area | Required assertion |
| -- | -- |
| CLI | Command dispatch and exit-message mapping for every group. |
| Option precedence | Flag over variable over default; a switch's `--no-` form overrides its variable; an empty variable counts as unset. |
| Option failures | An invalid variable or one outside the choices → usage error naming it (Usage); a required option resolves from its variable or → usage error naming both (Usage). |
| Secrets | A secret resolves from its variable and has no flag; no secret value appears in `--help`; a stand-in `zstd` and `gpg` on `PATH` see no `DEBKIT_*` or `TXSTORE_*` variable → [CONFIG.md](CONFIG.md#secrets). |
| Logging | Plain and JSON lines; the DEBUG format and the quiet third-party loggers. |
| Actions | Orchestration, argument combinations, and error propagation. |
| Action transactions | Begin and commit on success, discard on failure, and no-op on a non-transactional storage. |
| Action retention | The retention policy built by `Action.__init__` reaches `maybe_transaction`. |
| Storage construction | A bare path becomes an absolute `fs://` URL; the resolved flags reach `make_storage` → [VALIDATION.md](VALIDATION.md#storage-urls), [STORAGE.md](STORAGE.md#construction). |
| History commands | `log`, `revert`, and `prune` output in brief and JSON, dry run, the guards → [STORAGE.md](STORAGE.md#history-commands). |
| Repo classes | Parse and serialize round-trips, mutation invariants, checksum decisions, sync/diff behavior; the `Sources` expansion into one record per payload file and its refusal to write. |
| Sync index set | A Release-listed index present at the source is copied, one absent there is skipped, and one already in the target is not purged. |
| Sync index filters | The component, architecture, and exclude-glob filters each drop an entry; the exclude glob reaches a component-discovered index while the root Release files survive it. |
| Sync source indices | Source indices follow the `source` selection, and an index directory nothing parses is dropped. |
| Sync index safety | An unsafe Release-listed path is dropped and logged; the source is never listed or probed. |
| Sync index publication | An unproven index the source lacks is skipped at transfer while a proven one fails the run → [VALIDATION.md](VALIDATION.md#index-selection). |
| Sync flat layouts | The flat layouts round-trip their prefix through `release_key`. |
| Sync index copying | An index whose target digests match is skipped, and a stale one is recopied. |
| Sync root Release copy | The root Release files copy only as the bytes their load verified, and only when both signed forms agree. |
| Sync forced indices | A source Release hash change forces indices with no package diff. |
| Sync index ownership | A stanza naming a Release-listed index is neither copied nor removed as a package, whichever side holds it, and the index set still purges one it drops → [VALIDATION.md](VALIDATION.md#index-selection). |
| Sync index Release skip | The Release-block skip with its metadata fallback → [VALIDATION.md](VALIDATION.md#index-selection) is asserted from both sides. |
| Sync index recovery | The unpublished-entry, absent-entry, and unloaded-target rules → [VALIDATION.md](VALIDATION.md#index-selection) are each asserted, an absent index recovering with nothing else to sync. |
| Sync shared root keep | On a real target root holding another dist, a sync and `check --fix` keep its tree and the payloads it lists → [VALIDATION.md](VALIDATION.md#index-selection). |
| Sync shared root rules | The shared-root corrupt-payload, payload-replace (each refusal cause), held-bytes, and unknown rules → [VALIDATION.md](VALIDATION.md#index-selection) are each asserted on a real target root, success and failure. |
| Sync shared root purge | Only the own tree is purged while another dist's index is corrupt or missing, its only Release form is malformed, or its name is not safe → [VALIDATION.md](VALIDATION.md#index-selection). |
| Insert pool object refusal | On a real root, an insert whose pool key another dist, or another arch's upload of the same file name, holds with different bytes is refused and leaves the held bytes → [VALIDATION.md](VALIDATION.md#filename-safety). |
| Insert pool object pass | Identical bytes into another dist pass, and so does a re-insert after `check --fix` deleted a corrupt object no index records → [VALIDATION.md](VALIDATION.md#filename-safety). |
| Remove shared root | On a real root holding another dist, `remove` keeps a payload it lists, deletes one only the own dist lists, and keeps every payload while its index is missing → [VALIDATION.md](VALIDATION.md#index-selection). |
| Validation | Every documented boundary rule, success and failure. |
| Crypto GPG | GPG verify and sign paths and the skip-verify branches. |
| Crypto real gpg | `tests/test_gnupg_real.py` on throwaway homes from `tests/helpers/gpg.py`: clearsigned and detached, tampered data, a signer outside the keyring and a home whose `gpg.conf` would retrieve or import its key, right and wrong passphrase, and the armor framing `cleartext_body` refuses. |
| Crypto checksums | The checksum source order `Repo.verify_checksum` walks; hash-block parsing and `HexDigests` in `repo/hashes.py`. |
| Crypto freshness | Each Release freshness rule. |

## Package contracts

`tests/test_package.py` walks `src/debkit` with `ast` rather than importing it.

| Area | Required assertion |
| -- | -- |
| Version | `debkit.__version__` equals the installed distribution version. |
| Runtime dependencies | Runtime imports are the standard library or the declared dependencies, and `pyproject.toml` declares exactly those → [STRUCTURE.md](STRUCTURE.md#dependencies). |
| Environment | Only `_env` reads `os.environ` → [CONFIG.md](CONFIG.md#resolution). |
| txstore boundary | Every txstore import is the façade and names a member of `txstore.__all__`. |
| Import directions | Each leaf imports only its row, `repo` and `action` never import upward, no import is deferred, and only `cli` calls `sys.exit`. |

## File layout

One file per source module or contract surface, named `test_<module>.py`. The entry shim
(`__main__.py`) and the version-only `__init__.py` carry no test file. Files that cover a contract
surface rather than a single module:

| File | Covers |
| -- | -- |
| `test_cli_main.py` | CLI dispatch and exit groups. |
| `test_argparser.py` | Parser, every subcommand, and option resolution through `_options`. |
| `test_package.py` | Package contracts → [Package contracts](#package-contracts). |
| `test_repo_package.py` | `repo/package.py`. |
| `test_hashes.py` | `repo/hashes.py`: hash-block parsing and `HexDigests`. |
| `test_repo_rw.py` | `repo/repo_rw.py`, and `repo/shared_root.py`, whose claims and reconciliation lists `RepoRW` exposes. |
| `test_logging.py` | `_logging.py`, and `_escape.py` through the plain log lines. |
| `test_action_history.py` | `log` / `revert` / `prune` wiring: brief and JSON output, dry run, guards. |
| `test_check_docs.py` | Contract-doc checker, loaded from `scripts/check_docs.py` by path. |
| `test_render_diagrams.py` | Diagram renderer and freshness check, loaded from `scripts/render_diagrams.py` by path. |

## Maintenance

For each change: update the owning contract, add the narrowest test that can falsify the change
(success and failure), then update only the distinct assertion here. Extend an existing file when
the behavior belongs to that module; add a file only for a new contract surface. Prefer
representative cases over encyclopedic matrices, and never copy payloads, field limits, or error
explanations from the owning contract.
