# Structure and dependency contract

Owns: directories, module ownership, internal import directions, and the txstore boundary. Routing →
[AGENTS.md](AGENTS.md#canonical-sources).

## Layout

| Path | Owns |
| -- | -- |
| `src/debkit/__init__.py` | `__version__`, read from the installed distribution → [SETUP.md](SETUP.md#release). |
| `src/debkit/__main__.py` | `python -m debkit` entry. |
| `src/debkit/cli.py` | Entry point (`debkit` console script); the only `sys.exit` caller → [EXCEPTIONS.md](EXCEPTIONS.md#cli-exit-groups). |
| `src/debkit/argparser.py` | `build_parser()`, `parse_cli()`, the global and S3 flags → [CONFIG.md](CONFIG.md). |
| `src/debkit/argtypes.py` | CLI type converters for flags and their variables → [VALIDATION.md](VALIDATION.md#principles). |
| `src/debkit/_options.py` | `EnvArgumentParser`: options that fall back to a `DEBKIT_*` variable, then a default → [CONFIG.md](CONFIG.md#resolution). |
| `src/debkit/_env.py` | Environment access → [CONFIG.md](CONFIG.md#resolution); value rules → [VALIDATION.md](VALIDATION.md#environment-values). |
| `src/debkit/_logging.py` | `configure_logging()`: root handler, plain or JSON lines → [CONFIG.md](CONFIG.md#global-flags). |
| `src/debkit/action/__init__.py` | `ActionException`, `open_storage()`, `storage_url()`, `history_retention()`, `verify_policy()`, the `attach_storage_*` helpers, `Action` base → [STORAGE.md](STORAGE.md#construction). |
| `src/debkit/action/action_pkg.py` | `ActionPkg`: list (`list_packages()`), dump, stats, insert, remove. |
| `src/debkit/action/action_sync.py` | `ActionSync`: sync; `ActionCheck`: check; the selection both read → [CONFIG.md](CONFIG.md#check). |
| `src/debkit/action/action_history.py` | `ActionHistory`: log, revert, prune → [STORAGE.md](STORAGE.md#history-commands). |
| `src/debkit/repo/__init__.py` | `Repo` base: index loading and checksum verification → [CRYPTO.md](CRYPTO.md#verification-order); the mirror index set and the consistency lists → [VALIDATION.md](VALIDATION.md#index-selection). |
| `src/debkit/repo/params.py` | `RepoParams` value object → [VALIDATION.md](VALIDATION.md#debian-source-strings); index-path layout → [VALIDATION.md](VALIDATION.md#repository-index-paths); pseudo-architectures and `is_valid_arch()` → [VALIDATION.md](VALIDATION.md#architectures). |
| `src/debkit/repo/repo_rw.py` | `RepoRW`: read/write repository mutations, and `check()` of its own storage against its indices → [CONFIG.md](CONFIG.md#check). |
| `src/debkit/repo/repo_sync.py` | `RepoSync`: sync planning into a `SyncPlan` and transfer between repositories. |
| `src/debkit/repo/index_selection.py` | `select_index_entry()`: the pure rules for which Release-listed indices a mirror carries → [VALIDATION.md](VALIDATION.md#index-selection). |
| `src/debkit/repo/repo_diff.py` | Index and file diffing. |
| `src/debkit/repo/release.py` | Release file parse, sign, verify → [CRYPTO.md](CRYPTO.md#gpg). |
| `src/debkit/repo/packages.py`, `package.py` | Packages index and record. |
| `src/debkit/repo/sources.py` | `Sources` index expanded into one read-only record per payload file. |
| `src/debkit/repo/component.py` | Component (suite/component) helpers. |
| `src/debkit/repo/changes.py` | `.changes` parsing. |
| `src/debkit/repo/debfile.py` | `.deb` introspection. |
| `src/debkit/repo/compress.py` | Index codecs, whose decoders stop at a size limit → [VALIDATION.md](VALIDATION.md#index-decoding); `codec_for()` picks one by suffix; discovery order → [VALIDATION.md](VALIDATION.md#repository-index-paths); `read_capped()` reads a stored object and `zstd_decompress()` decodes zstd, each up to a size limit → [VALIDATION.md](VALIDATION.md#object-sizes). |
| `src/debkit/repo/gnupg.py` | GPG sign/verify wrapper → [CRYPTO.md](CRYPTO.md#gpg). |
| `src/debkit/repo/hashes.py` | Debian hash blocks: `parse_hash_block()`, `HexDigests`, `ChecksumException`; re-exports txstore's algorithm-name lists → [CRYPTO.md](CRYPTO.md#checksums). |
| `tests/` | Test suite → [TESTING.md](TESTING.md#file-layout). |
| `scripts/check_docs.py` | Contract-doc gate → [SETUP.md](SETUP.md#quality-gates). |
| `scripts/render_diagrams.py` | PlantUML renderer and freshness checker → [SETUP.md](SETUP.md#diagrams). |
| `docs/guides/` | Non-canonical user how-tos. |
| `docs/development/` | Non-canonical contributor explanations and diagrams. |
| `docs/development/diagrams/` | PlantUML sources (`*.puml`) and their committed SVG renders → [SETUP.md](SETUP.md#diagrams). |
| `.github/` | `workflows/ci.yml` → [SETUP.md](SETUP.md#quality-gates); `debian-test.sh` → [SETUP.md](SETUP.md#dependency-policy); `workflows/release.yml` and `build-constraints.txt` → [SETUP.md](SETUP.md#release). |
| `pyproject.toml`, `uv.lock` | Package metadata, dependencies, tool configuration, version → [SETUP.md](SETUP.md#release). |
| `*.md` at the root | Canonical contracts → [AGENTS.md](AGENTS.md#canonical-sources). |

## Dependencies

```
cli → argparser → action → repo → txstore
```

`tests/test_package.py` asserts each row with `ast` → [TESTING.md](TESTING.md#package-contracts).

| Boundary | Direction |
| -- | -- |
| `cli` | May import any layer for dispatch and exception grouping. |
| Leaves | `_env` and `_logging` import no `debkit` module; `_options` imports only `_env`; `argtypes` imports only the standard library and txstore. |
| `action` | Uses `repo`, `_options` for flag registration types, `argtypes` for flag converters, and txstore; never imports `cli`, `argparser`, or `_logging`. |
| `repo` | Depends on txstore and crypto helpers; never on `action`, `cli`, `argparser`, `argtypes`, `_options`, or `_logging`. |
| `debkit` → `txstore` | Every import is `from txstore import ...` of a name in its `__all__`; a `txstore.<module>` import is a boundary violation. |
| Load-time cycles | None: no import is deferred. |
| Runtime dependencies | Standard library, plus `txstore`, `debian` (`python-debian`), `gnupg` (`python-gnupg`), `humanize`, and `urllib3` → [SETUP.md](SETUP.md#dependency-policy). |

## Naming

| Item | Convention |
| -- | -- |
| Modules | `snake_case`. |
| Layer entry classes | `Action*` and `Repo*` prefixes. |
| Exceptions | `<Owner>Exception`, defined in the module that owns the failure → [EXCEPTIONS.md](EXCEPTIONS.md#rules). |
| Tests | `test_<module>.py` → [TESTING.md](TESTING.md#file-layout). |

## Maintenance

Adding, renaming, or moving a module updates its row here in the same change. An import in a new
direction is a contract change to the dependencies table first, with the matching assertion in
`tests/test_package.py`. A new runtime dependency updates the Runtime dependencies row, the
dependency list in `tests/test_package.py`, and `.github/debian-test.sh` together →
[SETUP.md](SETUP.md#dependency-policy).
