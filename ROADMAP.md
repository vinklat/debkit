# debkit roadmap

Planned releases, deferred work, and places the code does not yet meet a contract. Shipped work →
[CHANGELOG.md](CHANGELOG.md). Nothing here is dated or committed.

## Releases

| Version | Goal | Blocked by |
| -- | -- | -- |
| 0.2.0 | Debian package `debkit`; the `debian-testing` CI job then installs txstore from apt → [known deviations](#known-deviations) | txstore 0.2.0 and its Debian package → [SETUP.md](SETUP.md#dependency-policy) |

## Backlog

Unscheduled and unordered work, each row waiting on a trigger. Promote a row to a release when its
trigger fires or the work is planned; drop it when it no longer applies.

### Features

Changes a user of the CLI or of a published repository would see.

| Feature | Revisit when | Area |
| -- | -- | -- |
| `insert` and `remove` of source packages; `insert` refuses them and `remove` keeps their payloads today | A user publishes source packages, or the `IndexKind` work starts | [VALIDATION.md](VALIDATION.md#sources-stanzas) |
| Case-insensitive `Release` field names, as Debian Policy 5.1 requires; the email parser matches case-sensitively, so a lowercase `sha256:` block is ignored | A source publishes lowercase field names, or the deb822 reader work starts | [VALIDATION.md](VALIDATION.md#release-fields) |
| Mirror `by-hash/` indices | A mirror's clients rely on `Acquire-By-Hash` | [VALIDATION.md](VALIDATION.md#repository-index-paths) |
| Decode a `.zst` `control.tar` in process with `compression.zstd`, capped by `ZstdDecompressor.decompress(max_length=…)`, replacing the `zstd` subprocess and the `zstd` binary prerequisite | `requires-python` rises to 3.14, once Debian testing no longer ships Python 3.13 | [VALIDATION.md](VALIDATION.md#object-sizes) |
| Require `SHA256` or `SHA512` for payload, index, and shared-root replace decisions, so a `SHA1`-only record no longer verifies | A mirrored source publishes `SHA1`-only records, or a publisher is not trusted with its own payloads | [CRYPTO.md](CRYPTO.md#checksums) |
| Signer fingerprint pinning per source | One keyring must serve several sources | [CRYPTO.md](CRYPTO.md#gpg) |
| Opt-in uploader keyring (`--changes-keyring`) that `insert` verifies a clearsigned `.changes` against, separate from the keyring that verifies and signs Releases | An incoming storage is writable by anyone the operator does not trust to publish | [CRYPTO.md](CRYPTO.md#verification-failures) |

### Internal design

Restructuring a user would not see.

| Change | Revisit when | Area |
| -- | -- | -- |
| Typed settings object passed to actions instead of the argparse `Namespace`; it also carries the parsed `--architectures` and `--indices-exclude` selection `_selection()` returns as a tuple | An action is called from Python rather than the CLI | [STRUCTURE.md](STRUCTURE.md#dependencies) |
| Immutable `RepoParams` holding location only, with the run's `VerifyPolicy` passed to `Repo*` beside it instead of the `verify_gpg` and `verify_digests` fields | The typed settings work starts, or a second caller builds `RepoParams` | [CRYPTO.md](CRYPTO.md#verification-switches) |
| One deb822 stanza reader and writer for `Release`, `.changes`, and `Sources`, which still read through the email parser, with the repeated-field and blank-line rules as its parameters | A fourth stanza rule diverges between the email parser and `debian.deb822` | [VALIDATION.md](VALIDATION.md#release-fields) |
| `ReleaseKey`/`StoragePath` NewTypes and a coordinate-aware `Packages` owning its component, arch, and codec | A fifth raw `component.packages` walk outside `Component`, or `IndexKind` or `Layout` starts | [STRUCTURE.md](STRUCTURE.md#layout) |
| `Repo.slots()` iterator over the coordinate-aware `Packages`, replacing the hand-written component and arch walks in `repo/__init__.py`, `repo/shared_root.py`, and `ActionPkg`, resolving the component and arch selection once, and renaming `Packages.packages` | The coordinate-aware `Packages` lands | [STRUCTURE.md](STRUCTURE.md#layout) |
| Split `ActionPkg` into its read-only views, `ActionInsert`, and `ActionRemove`, and `ActionHistory` per subcommand, as `ActionSync` and `ActionCheck` are, so no action reads its subcommand's flags as optional | The typed settings work starts, or the `ActionPkg.insert` placement moves into `RepoRW` | [STRUCTURE.md](STRUCTURE.md#layout) |
| `SyncPlanner(source, target, listing)` returning the `SyncPlan`, so `RepoSync` keeps only the transfer, with an optional listing replacing the `existing_files`/`can_list` pair | The `ActionSync` sync policy moves into a repo-level use case, or `RepoSync` gains a second caller | [STRUCTURE.md](STRUCTURE.md#layout) |
| `ComponentRelease` split out of `Release`, so `load_component` and `setup_component` cannot be called on a `RootRelease` | A third Release role, or a caller of either method on the root | [STRUCTURE.md](STRUCTURE.md#layout) |
| `IndexKind` value object owning the arch directory, index name, container class, and writability, replacing the `SOURCE_ARCH` comparisons | A second index kind is written, or `insert` of source packages starts | [STRUCTURE.md](STRUCTURE.md#dependencies) |
| `insert` reads each `.deb` control from the stored copy after its `.changes` digest check, so the index stanza and the verified payload come from one read | The pool key no longer needs the control before the transfer | [CRYPTO.md](CRYPTO.md#verification-failures) |
| `Layout` value object (componented, flat, rootless flat) owning the index prefix, component slot, component Release, `Components` field, pool-key rules, and a flat layout's one index shared by every arch slot under the empty arch, replacing the `is_flat`/`is_real_dist` branches and the identity dedup in `Component.containers()` | A new layout is added, or the `IndexKind` work starts | [VALIDATION.md](VALIDATION.md#repository-index-paths) |

## Known deviations

Implementation that does not yet meet a root contract. Contracts link here instead of carrying
TODOs; remove a row when the code meets the rule. `Fix` links the release or backlog row that
closes the deviation; `—` means none is recorded.

| Contract | Deviation | Source | Fix |
| -- | -- | -- | -- |
| [VALIDATION.md](VALIDATION.md#sources-stanzas) | Read-only-ness on the insert path is inferred per call site: `RepoRW.binary_archs` filters on the source architecture, and the `.changes` and control-field checks refuse it before the transfer. `remove` and `save` already rely on `Sources` returning the base contract's "nothing changed"; a read-only base shared by `Packages` and `Sources` would own insert too. | `repo/repo_rw.py` `RepoRW.binary_archs`, `action/action_pkg.py` `_target_archs` | — |
| [STYLE.md](STYLE.md#layers) | `ActionPkg.insert` places each `.deb` in the pool, caps its reads at its `.changes` `Size`, checks it against its `.changes` digest, and refuses other bytes under a held pool key through `RepoRW.held_digests`, repo logic an action does not own. | `action/action_pkg.py` `insert` | Moves into `RepoRW` with `IndexKind` → [internal design](#internal-design) |
| [CRYPTO.md](CRYPTO.md#verification-failures) | `insert` builds the index stanza from a first read of each `.deb` and checks its `.changes` digest on the transfer, a second read, so an incoming-storage writer racing the two could pair other control fields with the verified payload. | `action/action_pkg.py` `insert` | One read from the stored copy → [internal design](#internal-design) |
| [STYLE.md](STYLE.md#layers) | `ActionSync` decides the sync policy itself: the target reset after a checksum failure, the freshness floors of each pass, and the verify-before-sync order. | `action/action_sync.py` `sync`, `_load_target`, `_sync_until_settled` | Repo-level use case with typed settings → [internal design](#internal-design) |
| [VALIDATION.md](VALIDATION.md#index-selection) | A component nested below a selected one, as `main/debug` below `--components main`, keeps its indices, since selection splits a key at the longest selected component, while its payloads are never loaded, so the mirror lists packages it lacks. | `repo/index_selection.py` `_split_component` | — |
| [VALIDATION.md](VALIDATION.md#repository-index-paths) | `by-hash/` objects are not mirrored because the root `Release` does not list them; an apt client of a mirror whose `Release` advertises `Acquire-By-Hash: yes` falls back to the canonical index path after a 404. | `repo/__init__.py` `Repo._get_indices` | Mirror `by-hash/` indices → [features](#features) |
| [VALIDATION.md](VALIDATION.md#object-sizes) | An upload inserted with `--deb-filename`, without digest verification, or listed in its `.changes` with no usable `Size`, on a backend that cannot seek, is read whole with `storage.get` before the control caps apply, since no checked `Size` bounds it and python-debian's `ArFile` seeks; only the backend bounds its size. | `repo/debfile.py` `DebFile._read_control` | — |
| [SETUP.md](SETUP.md#dependency-policy) | The `debian-testing` CI job installs txstore from PyPI, since Debian does not package it yet; every other dependency comes from apt. | `.github/debian-test.sh` | Release 0.2.0 → [releases](#releases) |
