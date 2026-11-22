# Changelog

Released changes and their upgrade notes. Planned work → [ROADMAP.md](ROADMAP.md); entry format and
release steps → [SETUP.md](SETUP.md#release).

## Unreleased

## 0.1.1 - 2026-11-22

Size-capped storage reads on txstore 0.1.3. Release →
[v0.1.1](https://github.com/vinklat/debkit/releases/tag/v0.1.1).

| Change | Migration |
| -- | -- |
| txstore | Requires txstore 0.1.3 or later → [Dependency policy](SETUP.md#dependency-policy). |
| Unsupported operation | An operation the backend lacks is now a `StorageNotSupportedException` instead of a bare `NotImplementedError`, handled like any storage failure → [EXCEPTIONS.md](EXCEPTIONS.md#domain-exceptions). |
| Oversized objects | A checked copy longer than its recorded `Size` stops → `ChecksumException` (Distrustful); when a cap applies → [VALIDATION.md](VALIDATION.md#object-sizes). |
| Stream read failures | A backend error while an S3 or HTTP body is read is now a `StorageException` instead of escaping unwrapped → [txstore STORAGE.md](https://github.com/vinklat/txstore/blob/v0.1.3/STORAGE.md#storage-abi). |
| Stream read failures in a sync copy | A failed copy is retried, then → `RepoSyncException` (Sync) → [EXCEPTIONS.md](EXCEPTIONS.md#cli-exit-groups). |
| Read metrics | A read refused at its cap counts one read error and no read bytes in the `-o json` metrics → [txstore STORAGE.md](https://github.com/vinklat/txstore/blob/v0.1.3/STORAGE.md#storage-abi). |

## 0.1.0 - 2026-11-12

Initial public release, on the public txstore storage package: insert, remove, inspect, sync, and
check Debian repositories on fs, S3, and HTTP, with transactional writes and history. Release →
[v0.1.0](https://github.com/vinklat/debkit/releases/tag/v0.1.0).

| Change | Migration |
| -- | -- |
| Variables | Every variable gains the `DEBKIT_` prefix: `TARGET_URL` → `DEBKIT_TARGET_URL`, `S3_SECRET` → `DEBKIT_S3_SECRET`, and so on → [CONFIG.md](CONFIG.md). |
| Switches | `--skip-verify 1` → `--skip-verify`; `--skip-verify-gpg false` → omit it or pass `--no-skip-verify-gpg`. The same holds for `--skip-verify-ssl` and `--allow-release-rollback`. |
| Logging | `-l` gains `DEBKIT_LOG_LEVEL` and drops `TRACING`; `--log-format json` is new. |
| Filesystem URLs | `fs://` takes an absolute path only: `fs://debian` → `debian` or `fs:///abs/debian` → [VALIDATION.md](VALIDATION.md#storage-urls). |
| S3 bucket ACL | A created bucket no longer inherits `--s3-default-acl`; pass `--s3-bucket-acl` to set one. |
| S3 credentials | Unset S3 credentials fall back to `TXSTORE_S3_ACCESS` / `TXSTORE_S3_SECRET`. |
| `--history-max-age` | Now has an upper bound → [VALIDATION.md](VALIDATION.md#history-retention). |
| Help | `--help` no longer prints secret values taken from the environment. |
| Secret flags | `-t`/`--s3-secret` and `--gnupg-passphrase` are gone: set `DEBKIT_S3_SECRET` and `DEBKIT_GNUPG_PASSPHRASE` instead → [CONFIG.md](CONFIG.md#secrets). |
| Pool layout | An inserted `.deb` lands in Debian's source pool layout under its source package name, not its upload path's first letter and `Package` name → [VALIDATION.md](VALIDATION.md#filename-safety). Old pool keys stay valid; re-inserting an existing version adds a second stanza under its new key. |
| GnuPG home | `--gnupg-home` defaults to `~/.gnupg`, not `.gnupg` under the working directory, so a checkout cannot plant the trusted keyring → [CONFIG.md](CONFIG.md#global-flags). Keep a per-directory home with `DEBKIT_GNUPG_HOME=$PWD/.gnupg`. |
| Trusted keys | Verification reads no `gpg.conf`, so its `keyring` lines and key retrieval or import no longer authenticate a signer: import the keys into the trusted keyring → [CRYPTO.md](CRYPTO.md#gpg). GnuPG has a minimum version → [Prerequisites](SETUP.md#prerequisites-and-install). |
| Signing without a passphrase | Fixed: with no `DEBKIT_GNUPG_PASSPHRASE`, gpg read the first line of the Release as the passphrase, so `Release.gpg` and `InRelease` signed the Release without it. Re-sign such a repository with any `insert` or `remove`. |
