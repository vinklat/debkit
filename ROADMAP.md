# debkit roadmap

## Phases

| Phase | Status | Feature                     | Notes                                                                                                                                                                                |
| ----- | ------ | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 0.1.0 | ✅     | Initial public release      | Insert, remove, inspect, sync, and check Debian repositories on fs, S3, and HTTP via txstore; transactional writes with history; options by flag or `DEBKIT_*` variable              |
| 0.2.0 | ⏳     | `insert` of source packages | `ActionPkg.insert` refuses a `.changes` naming `source` among its `Architecture` tokens, and skips the source architecture when a binary package fans out over the repository arches |
| 0.3.0 | ⏳     | `remove` of source packages | `Component.remove_pkg` skips the read-only `Sources` index, so a pattern never removes a source payload                                                                              |

## Ideas

Unscheduled improvements, each waiting on a trigger. Promote a row to a phase when its trigger fires
or the work is planned; drop it when it no longer applies.

| Idea                                                                        | Revisit when                                        | Area                                                  |
| --------------------------------------------------------------------------- | --------------------------------------------------- | ----------------------------------------------------- |
| Typed settings object passed to actions instead of the argparse `Namespace` | An action is called from Python rather than the CLI | [STRUCTURE.md](STRUCTURE.md#dependencies)             |
| Debian package `debkit`                                                     | txstore is packaged for Debian (txstore 0.2.0)      | [SETUP.md](SETUP.md#dependency-policy)                |
| Mirror `by-hash/` indices                                                   | A mirror's clients rely on `Acquire-By-Hash`        | [VALIDATION.md](VALIDATION.md#repository-index-paths) |
| `ReleaseKey`/`StoragePath` NewTypes and a coordinate-aware `Packages` owning its component, arch, and codec | The next change to Release or Packages loading | [STRUCTURE.md](STRUCTURE.md#layout) |
| `IndexKind` value object owning the arch directory, index name, container class, and writability, replacing the `SOURCE_ARCH` comparisons | A second index kind is written, or 0.2.0 starts | [STRUCTURE.md](STRUCTURE.md#dependencies) |
| Signer fingerprint pinning per source | One keyring must serve several sources | [CRYPTO.md](CRYPTO.md#gpg) |
| `Layout` value object (componented, flat, rootless flat) owning the index prefix, component slot, component Release, `Components` field, and pool-key rules, replacing the `is_flat`/`is_real_dist` branches | A new layout is added, or the `IndexKind` work starts | [VALIDATION.md](VALIDATION.md#repository-index-paths) |

## Known deviations

Implementation that does not yet meet a root contract. Contracts link here instead of carrying
TODOs; remove a row when the code meets the rule.

| Contract                                              | Deviation                                                                                                                                                                                                                                                                                                                                                  | Source                                           |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| [VALIDATION.md](VALIDATION.md#sources-stanzas) | Read-only-ness on the insert path is inferred per call site: `_binary_archs` filters on the source architecture, and the `.changes` and control-field checks refuse it before the transfer. `remove` and `save` already rely on `Sources` returning the base contract's "nothing changed"; a read-only base shared by `Packages` and `Sources` would own insert too. | `action/action_pkg.py` `_binary_archs`, `_target_archs` |
| [STYLE.md](STYLE.md#layers) | `ActionPkg.insert` places each `.deb` in the pool and checks it against its `.changes` digest, repo logic an action does not own; it moves into `RepoRW` with the `IndexKind` [idea](#ideas). | `action/action_pkg.py` `insert` |
| [EXCEPTIONS.md](EXCEPTIONS.md#cli-exit-groups)        | `NotImplementedError` from a read-only, write-only, or non-transactional txstore backend is in no exit group and surfaces as a traceback.                                                                                                                                                                                                                  | `cli.py`                                         |
| [VALIDATION.md](VALIDATION.md#repository-index-paths) | `by-hash/` objects are not mirrored because the root `Release` does not list them; an apt client of a mirror whose `Release` advertises `Acquire-By-Hash: yes` falls back to the canonical index path after a 404.                                                                                                                                         | `repo/__init__.py` `Repo._get_indices`           |
| [STORAGE.md](STORAGE.md#construction)                 | `RepoRW._verify_file` instantiates txstore's `StorageNull` directly as a digest sink instead of obtaining it through `open_storage()`, so `open_storage()` is not the only place debkit builds a storage.                                                                                                                                                | `repo/repo_rw.py` `_verify_file`                 |
| [VALIDATION.md](VALIDATION.md#object-sizes) | A `.zst` `control.tar` member is decompressed whole by python-debian through `unzstd`, so only its compressed size is capped, not the tar it inflates to. | `repo/debfile.py` `_control_of` |
| [SETUP.md](SETUP.md#dependency-policy)                | The `debian-testing` CI job installs txstore from PyPI, since Debian does not package it yet; every other dependency comes from apt.                                                                                                                                                                                                                       | `.github/debian-test.sh`                         |
