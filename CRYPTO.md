# Cryptography contract

Owns: GPG verification and signing, Debian hash blocks and which digests debkit trusts, checksum
verification order, Release freshness, and verification-failure behavior. Routing →
[AGENTS.md](AGENTS.md#canonical-sources).

Verification is on by default; skipping is explicit through the switches below.

## Verification switches

| Flag                | Effect                                                                              |
| ------------------- | ----------------------------------------------------------------------------------- |
| `--skip-verify` | Disables TLS, GPG, and digest verification; GPG as under `--skip-verify-gpg`. |
| `--skip-verify-gpg` | No `GnuPG` instance is created by action setup; Release signatures are not checked, and a root Release without `Release.gpg` or `InRelease` reads as-is. |
| `--skip-verify-gpg`, writing | Release signing is off too: `insert` and `remove` save the root `Release` unsigned and delete its old `InRelease` and `Release.gpg`, logging each, since both sign the previous content. |
| `--skip-verify` or `--skip-verify-gpg` | Every command that reads a repository logs a warning once per run; `log`, `revert`, and `prune` verify nothing and do not. |
| `--skip-verify-ssl` | `verify=False` on HTTP and S3 clients → [STORAGE.md](STORAGE.md#construction).      |

Flag names and variables → [CONFIG.md](CONFIG.md#global-flags).

## GPG

Implemented by `GnuPG`, `cleartext_body`, and `canonical_text` in `repo/gnupg.py` over
`python-gnupg`; home and keyring → [CONFIG.md](CONFIG.md#global-flags).

| Operation                                                                                                                                                                   | Behavior                                                                                                                                                                                                                                                                     |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cleartext_body(data)`                                                                                                                                                      | The signed body of one cleartext message as gpg canonicalizes it, without checking the signature: trailing spaces, tabs, and CR stripped from every line, each line newline-terminated, armor headers dropped, dash-escaping undone. gpg authenticates nothing else.         |
| `canonical_text(data)`                                                                                                                                                      | `data` with the line canonicalization of `cleartext_body`, so a detached `Release` compares with an `InRelease` body.                                                                                                                                                        |
| `verify(data)`                                                                                                                                                              | Inline `InRelease` verification; returns `cleartext_body(data)`.                                                                                                                                                                                                             |
| `verify(data, detach_sign)`                                                                                                                                                 | Detached `Release` + `Release.gpg` verification; returns `data`. The signature is staged in a `tempfile.NamedTemporaryFile` removed in `finally`, because `python-gnupg` needs a path on disk.                                                                               |
| Root Release load                                                                                                                                                           | Fetches every root `Release`, `Release.gpg`, and `InRelease` present once and records their digests → [Verification order](#verification-order); with GPG verification on, verifies the inline and the detached form alike when both are present, since `sync` mirrors both. |
| Root Release load, `InRelease` and `Release` both present                                                                                                                   | With GPG verification on, the `InRelease` body differs from `canonical_text` of the `Release` → `ChecksumException` (Distrustful): freshness reads one form, `sync` mirrors both, and apt prefers `InRelease`.                                                               |
| Root Release load without `GnuPG` or with GPG verification off | The `sync` target or `--skip-verify-gpg`: no signature file is required, and an `InRelease` it parses is read through `cleartext_body`, unverified. |
| `sign(data, detach=False)`                                                                                                                                                  | Returns the signature (inline or detached) used when writing Release metadata.                                                                                                                                                                                               |
| Keyring cannot be loaded                                                                                                                                                    | → `GPGVerifyException` (Read).                                                                                                                                                                                                                                               |
| Invalid signature or malformed signed message                                                                                                                               | → `GPGVerifyException` (Read).                                                                                                                                                                                                                                               |
| Non-blank text after `-----END PGP SIGNATURE-----`, an unescaped `-` body line, or an armor header that is not `Name: value` with a letters-only name and a non-empty value | → `GPGVerifyException` (Read).                                                                                                                                                                                                                                               |

## Checksums

Digest helpers and the algorithm-name lists are txstore's → [txstore STORAGE.md](https://github.com/vinklat/txstore/blob/v0.1.1/STORAGE.md#digests); the
Debian hash-block layer over them is `repo/hashes.py`.

| Item                                                                   | Rule                                                                                                                                                                                                              |
| ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Algorithms                                                             | `MD5Sum`, `SHA1`, `SHA256`, `SHA512`; `MD5Sum` exists for Debian compatibility and new behavior never relies on it alone.                                                                                         |
| `ChecksumOpen`, `calc_digests`, `compare_digests`, `normalize_digests` | txstore; comparison semantics, including that MD5 alone never makes a match → [txstore STORAGE.md](https://github.com/vinklat/txstore/blob/v0.1.1/STORAGE.md#digests).                                            |
| Non-overlapping, unknown, or mismatched digests                        | Verification failure.                                                                                                                                                                                             |
| Hash blocks                                                            | `parse_hash_block()` in `repo/hashes.py` splits a `Release` hash field, a `Checksums-<algorithm>` field, or a `Sources` `Files` field into `(digest, size, filename)` triples; a trailing partial run is dropped. |
| Index skip during sync                                                 | The target `Release` digest block is compared before the stored object metadata; which entries that settles, and when the source is probed instead → [VALIDATION.md](VALIDATION.md#index-selection).              |

## Verification order

`Repo.verify_checksum(filename, digests)` returns `True` at the first source that verifies:

1. `Release`, `InRelease`, and `Release.gpg` are answered by the digests of the bytes the root
   Release load fetched and verified, and by nothing else: no digest block lists them, a stanza
   cannot vouch for them, and a file the load did not fetch fails.
2. A matching entry in any component package index, whatever the path's suffix, but only for a path
   no `Release` digest block lists: each container keys by the `Filename` field it parsed, so a path
   no index names misses and falls through.
3. Any `Release` digest block, root or per-component: a disjunction over one iterator, so which
   block answers is not observable.

Step 1 binds a `sync` copy of a root Release file to the bytes its signature and freshness checks
read; a source that serves other bytes for the copy fails the transfer.

A signed `Release` owns every key in its digest block, so step 2 is skipped for such a key and the
digest decides. A package index is remote metadata, and a stanza naming a listed index would
otherwise define that path's content in place of the GPG-rooted digest.

Steps 1 and 3 look the path up under its Release key, `RepoParams.release_key()`, whatever its
suffix: a Release-listed `.tar.gz` such as a dep11 icon tarball is an index, not a package →
[VALIDATION.md](VALIDATION.md#repository-index-paths).

Otherwise it returns `False` and the caller raises `ChecksumException` (Distrustful).

## Release freshness

Implemented by `Release.check_fresh_against()` in `repo/release.py`; `sync` runs it before each pass
builds its transfer lists, when GPG verification is on and `--allow-release-rollback` is not set. A
signature proves who signed a Release, not that it is current, and the orphan pass purges what an
older Release no longer lists.

| Condition                                                                               | Result                                                                                                                                                                                                                                                                       |
| --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Source `Valid-Until` earlier than now                                                   | → `ReleaseFreshnessException` (Distrustful)                                                                                                                                                                                                                                  |
| Source `Date` earlier than the loaded target `Date`                                     | → `ReleaseFreshnessException` (Distrustful)                                                                                                                                                                                                                                  |
| Source `Date` missing while the loaded target has a `Date`                              | → `ReleaseFreshnessException` (Distrustful)                                                                                                                                                                                                                                  |
| Source `Date` or `Valid-Until` malformed → [VALIDATION.md](VALIDATION.md#release-dates) | → `ReleaseFreshnessException` (Distrustful)                                                                                                                                                                                                                                  |
| Target compared | The target as read under txstore's `history_lock` → [txstore STORAGE.md](https://github.com/vinklat/txstore/blob/v0.1.1/STORAGE.md#locking), so a peer commit that lands before the lock is granted is the floor; `--verify-existing` checks that same read |
| Target reset as corrupt | Its indices are ignored → [Verification failures](#verification-failures), but the `Date` its Release carried before the reset stays the floor |
| Later pass | The source is compared with the staged target and with the target as the run found it, so a staged target cannot lower the floor |
| Target loaded from `InRelease`                                                          | Its `Date` is read from the signed body → [GPG](#gpg), even though the target is not verified                                                                                                                                                                                |
| Source `Valid-Until` missing, target not loaded, or target without `Date`               | Not compared                                                                                                                                                                                                                                                                 |
| Target `Date` malformed                                                                 | Logged, not compared                                                                                                                                                                                                                                                         |
| `--allow-release-rollback` → [CONFIG.md](CONFIG.md#sync)                                | Checks skipped; a warning is logged once per run                                                                                                                                                                                                                             |

## Verification failures

| Path                                                       | Failure                                                                                                                                                                      |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sync` transfer digest mismatch                            | Target object deleted, then `ChecksumException` (Distrustful); the surrounding transaction discards every staged footmark → [STORAGE.md](STORAGE.md#transactions).           |
| `insert` or `check` finds no index that verifies an object | `ChecksumException` (Distrustful).                                                                                                                                           |
| Corrupted target indices during `sync` | Logged, target reset, sync proceeds and copies every selected index again → [VALIDATION.md](VALIDATION.md#index-selection); a reset target lists no package for `--verify-existing` to check. |
| GPG verification failure                                   | `GPGVerifyException` (Read).                                                                                                                                                 |

## Implementation rules

- Stream payloads; never load a package only to digest it → [STYLE.md](STYLE.md#performance).
- A verification failure stops the operation with a typed exception; never degrade to a warning.
- Use txstore's digest helpers for normalization and comparison and `repo/hashes.py` for hash-block
  parsing; callers do not reimplement them.

## Maintenance

Change verify, sign, or hash-block semantics here and in `repo/gnupg.py` or `repo/hashes.py`
together, with success and failure tests per [TESTING.md](TESTING.md#required-coverage). Digest
semantics change in txstore.
