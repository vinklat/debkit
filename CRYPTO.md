# Cryptography contract

Owns: GPG verification and signing, Debian hash blocks and which digests debkit trusts, checksum
verification order, Release freshness, and verification-failure behavior. Routing →
[AGENTS.md](AGENTS.md#canonical-sources).

Verification is on by default; skipping is explicit through the switches below.

## Verification switches

| Flag | Effect |
| -- | -- |
| `--skip-verify` | Disables TLS, GPG, and index and payload digest verification; GPG as under `--skip-verify-gpg`. An explicit `--changes-sha256` is still checked → [Verification failures](#verification-failures). |
| `--skip-verify-gpg` | No `GnuPG` instance is created by action setup; Release signatures are not checked, and a root Release without `Release.gpg` or `InRelease` reads as-is. |
| `--skip-verify-gpg`, writing | Release signing is off too: `insert` and `remove` save the root `Release` unsigned and delete its old `InRelease` and `Release.gpg`, logging each, since both sign the previous content. |
| `--skip-verify` or `--skip-verify-gpg` | Every command that reads a repository logs a warning once per run; `log`, `revert`, and `prune` verify nothing and do not. |
| `--skip-verify-ssl` | `verify=False` on HTTP and S3 clients → [STORAGE.md](STORAGE.md#construction). |

Flag names and variables → [CONFIG.md](CONFIG.md#global-flags).

## GPG

Implemented by `GnuPG`, `cleartext_body`, and `canonical_text` in `repo/gnupg.py` over
`python-gnupg`; home and keyring → [CONFIG.md](CONFIG.md#global-flags).

| Operation | Behavior |
| -- | -- |
| `cleartext_body(data)` | The signed body of one cleartext message as gpg canonicalizes it, without checking the signature: trailing spaces, tabs, and CR stripped from every line, each line newline-terminated, armor headers dropped, dash-escaping undone. gpg authenticates nothing else. |
| `cleartext_body` encoding | Works on bytes, so the body keeps its encoding. |
| `canonical_text(data)` | `data` with the line canonicalization of `cleartext_body`, so a detached `Release` compares with an `InRelease` body. |
| `verify(data)` | Inline `InRelease` verification; returns `cleartext_body(data)`. |
| `verify(data, detach_sign)` | Detached `Release` + `Release.gpg` verification; returns `data`. The signature is staged in a `tempfile.NamedTemporaryFile` removed when its context exits, even when staging it fails, because `python-gnupg` needs a path on disk. |
| Root Release load | Fetches every root `Release`, `Release.gpg`, and `InRelease` present once and records their digests → [Verification order](#verification-order). |
| Root Release load, GPG verification on | Verifies the inline and the detached form alike when both are present, since `sync` mirrors both. |
| Root Release load, `Release.gpg` without `Release` | Signs nothing: skipped with a warning, so it is neither fetched nor mirrored. |
| Root Release load, `InRelease` and `Release` both present | With GPG verification on, the `InRelease` body differs from `canonical_text` of the `Release` → `ChecksumException` (Distrustful): freshness reads one form, `sync` mirrors both, and apt prefers `InRelease`. |
| Signature mode | `RootRelease.signature_mode` is the one place the GPG switch and the `GnuPG` combine: `VERIFY_AND_SIGN` (switch on, `GnuPG` set), `READ_SIGNED_UNVERIFIED` (switch on, no `GnuPG`: the `sync` target), `OFF` (`--skip-verify-gpg`). |
| Root Release load in `READ_SIGNED_UNVERIFIED` or `OFF` | No signature file is required, and an `InRelease` it parses is read through `cleartext_body`, unverified; `READ_SIGNED_UNVERIFIED` parses `InRelease` over a `Release` without `Release.gpg`, as the verified source does. |
| Root Release save | Writes the layout's root `Release` from `indices_path()` whichever form the load read, then signs it into `InRelease` and `Release.gpg` or removes both; a per-component Release is never signed. |
| Root Release save in `READ_SIGNED_UNVERIFIED` | Nothing signs it, and an unsigned save would drop the signatures it was read with → `ReleaseWriteException` (Write), before anything is written. |
| Signing key | `--gnupg-sign-key` names it; unset, gpg picks `default-key` from the home's `gpg.conf`, else the first key in the trusted keyring whose secret the home holds. A key gpg cannot use creates no signature, refused as below. |
| `sign(data, detach=False)` | Returns the signature (inline or detached) used when writing Release metadata; empty when gpg reports none created, as on a wrong passphrase, even though a failed clearsign prints its armor, and `Release.save` refuses an empty one → `ReleaseWriteException` (Write). |
| Key retrieval | gpg runs with `--no-auto-key-retrieve`, `--no-auto-key-locate`, and `--no-auto-key-import`, so no gpg run fetches, locates, or imports a key, whatever a config file says; for verify they back up `--no-options` below. |
| Verify and `gpg.conf` | Verify runs with `--no-options`, so a `keyring` or `primary-keyring` line in the home's `gpg.conf` adds no key that authenticates; only the trusted keyring does. |
| gpg version | `--no-auto-key-import` needs the GnuPG minimum → [SETUP.md](SETUP.md#prerequisites-and-install); an older gpg rejects it when `GnuPG` opens, before any verify or sign → `GPGVerifyException` (Read). |
| Keyring cannot be loaded | → `GPGVerifyException` (Read). |
| The `gpg` binary cannot be run | python-gnupg raises `OSError` → `GPGVerifyException` (Read). |
| Invalid signature or malformed signed message | → `GPGVerifyException` (Read). |
| Non-blank text after `-----END PGP SIGNATURE-----`, an unescaped `-` body line, or an armor header that is not `Name: value` with a letters-only name and a non-empty value | → `GPGVerifyException` (Read). |

## Checksums

Digest helpers and the algorithm-name lists are txstore's →
[txstore STORAGE.md](https://github.com/vinklat/txstore/blob/v0.1.2/STORAGE.md#digests); the Debian
hash-block layer over them is `repo/hashes.py`.

| Item | Rule |
| -- | -- |
| Algorithms | `MD5Sum`, `SHA1`, `SHA256`, `SHA512`; `MD5Sum` exists for Debian compatibility and new behavior never relies on it alone. |
| `SHA1` | A `SHA1`-only record verifies: debkit filters nothing from txstore's comparison list; how algorithms combine → [txstore STORAGE.md](https://github.com/vinklat/txstore/blob/v0.1.2/STORAGE.md#digests); requiring `SHA256` or `SHA512` → [ROADMAP.md](ROADMAP.md#ideas). |
| `ChecksumOpen`, `calc_digests`, `compare_digests`, `normalize_digests` | txstore; comparison semantics, including that MD5 alone never makes a match → [txstore STORAGE.md](https://github.com/vinklat/txstore/blob/v0.1.2/STORAGE.md#digests). |
| Non-overlapping, unknown, or mismatched digests | `verify_checksum` returns `False`; what each path raises or logs → [Verification failures](#verification-failures). |
| Hash blocks | `parse_hash_block()` in `repo/hashes.py` splits a `Release` hash field, a `Checksums-<algorithm>` field, or a `Sources` `Files` field into `(digest, size, filename)` triples; a trailing partial run is dropped. |
| Index skip during sync | The target `Release` digest block is compared before the stored object metadata; which entries that settles → [VALIDATION.md](VALIDATION.md#index-selection). |

## Verification order

`Repo.verify_checksum(filename, digests)` returns `True` at the first source that verifies:

1. `Release`, `InRelease`, and `Release.gpg` are answered by the digests of the bytes the root
   Release load fetched and verified, and by nothing else: no digest block lists them, a stanza
   cannot vouch for them, and a file the load did not fetch fails; a failed `sync` transfer →
   `ChecksumException` (Distrustful).
2. A matching entry in any component package index, whatever the path's suffix, but only for a path
   no `Release` digest block lists: each container keys by the `Filename` field it parsed, so a path
   no index names misses and falls through.
3. Any `Release` digest block, root or per-component: a disjunction over one iterator, so which
   block answers is not observable.

Step 1 binds a `sync` copy of a root Release file to the bytes its signature and freshness checks
read; a source that serves other bytes for the copy fails the transfer → `ChecksumException`
(Distrustful).

A signed `Release` owns every key in its digest block, so step 2 is skipped for such a key and the
digest decides. A package index is remote metadata, and a stanza naming a listed index would
otherwise define that path's content in place of the GPG-rooted digest.

Steps 1 and 3 look the path up under its Release key, `RepoParams.release_key()`, whatever its
suffix: a Release-listed `.tar.gz` such as a dep11 icon tarball is an index, not a package →
[VALIDATION.md](VALIDATION.md#repository-index-paths).

Otherwise it returns `False`; callers differ → [Verification failures](#verification-failures).

## Release freshness

Implemented by `RootRelease.check_fresh_against()` in `repo/release.py`; `sync` runs it before each
pass builds its transfer lists, when GPG verification is on and `--allow-release-rollback` is not
set. A signature proves who signed a Release, not that it is current, and the orphan pass purges
what an older Release no longer lists.

| Condition | Result |
| -- | -- |
| Source `Valid-Until` earlier than now | → `ReleaseFreshnessException` (Distrustful) |
| Source `Date` earlier than the loaded target `Date` | → `ReleaseFreshnessException` (Distrustful) |
| Source `Date` missing while the loaded target has a `Date` | → `ReleaseFreshnessException` (Distrustful) |
| Source `Date` or `Valid-Until` malformed → [VALIDATION.md](VALIDATION.md#release-dates) | → `ReleaseFreshnessException` (Distrustful) |
| Target trust | The operator owns the target, so its Release sets the floor unverified: whoever can write the target can roll it back directly, and `--allow-release-rollback` recovers from a bad floor. |
| Target compared | The target as read under txstore's `history_lock` → [txstore STORAGE.md](https://github.com/vinklat/txstore/blob/v0.1.2/STORAGE.md#locking), so a peer commit that lands before the lock is granted is the floor; `--verify-existing` checks that same read |
| Target reset as corrupt | Its indices are ignored → [Verification failures](#verification-failures), but the `Date` its Release carried before the reset stays the floor |
| Later pass | The source is compared with the staged target and with the target as the run found it, so a staged target cannot lower the floor |
| Target loaded from `InRelease` | Its `Date` is read from the signed body → [GPG](#gpg), even though the target is not verified |
| Source `Valid-Until` missing, target not loaded, or target without `Date` | Not compared |
| Target `Date` malformed | Logged, not compared |
| `--allow-release-rollback` → [CONFIG.md](CONFIG.md#sync) | Checks skipped; a warning is logged once per run |

## Verification failures

| Path | Failure |
| -- | -- |
| `sync` transfer digest mismatch | Target object deleted, then `ChecksumException` (Distrustful); the surrounding transaction discards every staged footmark → [STORAGE.md](STORAGE.md#transactions). |
| `check` payload digest mismatch | Logged and counted as an error; `--fix` deletes the object and counts it fixed unless another dist sharing the root may record its bytes → [VALIDATION.md](VALIDATION.md#index-selection). An unfixed error fails the check → [EXCEPTIONS.md](EXCEPTIONS.md#cli-exit-groups). |
| `sync --verify-existing` payload digest mismatch | Logged and, under the same shared-root rule, deleted inside the sync's transaction, so the sync copies it again. |
| `sync --verify-existing` kept payload | A corrupt payload the shared-root rule keeps is logged as not deleted; overwriting it follows the shared-root replace rule → [VALIDATION.md](VALIDATION.md#index-selection). |
| `insert` `.changes` file | A `.changes` file whose SHA256 differs from `--changes-sha256` → `ChecksumException` (Distrustful). |
| `insert --changes-filename` payload | Each transferred `.deb` is compared with its `Checksums-Sha256` entry unless digest verification is off, before the pool object rule → [VALIDATION.md](VALIDATION.md#filename-safety); a mismatch → `ChecksumException` (Distrustful). |
| `insert --changes-filename` payload mismatch | The transaction discards the staged transfer → [STORAGE.md](STORAGE.md#transactions). |
| `insert --deb-filename` payload | No manifest lists it, so it is never verified. |
| `insert` signed `.changes` | Parsed from its `cleartext_body`; its signature is not verified, so `--changes-sha256` is the only check on the file. |
| `insert` trust in the incoming storage | Trusted like `--deb-filename` input: whoever can write it can publish, since an unpinned `.changes` only shows the upload is consistent with itself. Pin `--changes-sha256` from a channel the uploader cannot write; uploader signatures → [ROADMAP.md](ROADMAP.md#ideas). |
| `insert` `.changes` with malformed armor | → `ChangesException` (Read). |
| Corrupted target indices during `sync` | Logged, target reset, sync proceeds and copies every selected index again → [VALIDATION.md](VALIDATION.md#index-selection); a reset target lists no package for `--verify-existing` to check. |
| GPG verification failure | `GPGVerifyException` (Read). |

## Implementation rules

- Stream payloads; never load a package only to digest it → [STYLE.md](STYLE.md#performance).
- A verification failure stops the operation with a typed exception; never degrade to a warning.
- Use txstore's digest helpers for normalization and comparison and `repo/hashes.py` for hash-block
  parsing; callers do not reimplement them.

## Maintenance

Change verify, sign, or hash-block semantics here and in `repo/gnupg.py` or `repo/hashes.py`
together, with success and failure tests per [TESTING.md](TESTING.md#required-coverage). Digest
semantics change in txstore.
