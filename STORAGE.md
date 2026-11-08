# Storage integration contract

Owns: how debkit constructs storage, records its writes as transactions, and serves the history
commands. Routing → [AGENTS.md](AGENTS.md#canonical-sources).

Storage is the [txstore](https://github.com/vinklat/txstore) package; its contracts own the
`Storage` interface, backends, transactions, footmarks, history files, retention rules, locking, S3
ACL handling, and digests → [txstore STORAGE.md](https://github.com/vinklat/txstore/blob/v0.1.1/STORAGE.md).
This file states only what debkit chooses.

## Construction

`open_storage()` in `action/__init__.py` is debkit's only `make_storage` call; actions reach storage
through it or the `attach_storage_*` helpers built on it →
[STRUCTURE.md](STRUCTURE.md#dependencies).

| Input | Rule |
| -- | -- |
| URL | `open_storage()` passes `storage_url()` of the value to `make_storage` → [VALIDATION.md](VALIDATION.md#storage-urls). |
| `create_path` | Set only on the write target of `insert`, `remove`, and `sync`: creates the S3 bucket or FS directory when missing. |
| `--incoming-url` | Opened without `create_path`; a missing upload → `DebFileException` or `ChangesException` (Read). |
| `verify_ssl` | `not (skip_verify or skip_verify_ssl)` → [CRYPTO.md](CRYPTO.md#verification-switches). |
| `s3_access`, `s3_secret` | The resolved `--s3-access` and `DEBKIT_S3_SECRET` values, unset ones left to txstore → [CONFIG.md](CONFIG.md#resolution); an `s3://` URL missing either key is a construction failure below. |
| `s3_default_acl` | `--s3-default-acl` → [CONFIG.md](CONFIG.md#storage-flags); its default is public because an apt repository serves its objects anonymously. |
| `s3_bucket_acl` | `--s3-bucket-acl`, else none; never derived from the object ACL, since a public bucket ACL lists every key. |
| Rejected URL | → [VALIDATION.md](VALIDATION.md#storage-urls) |
| Construction failure | `make_storage` raising `StorageException` (empty URL, no host, unresolved S3 key, uncreatable path): `--target-url` or `--source-url` → `RepoReadException` (Read). |
| Construction failure, plain URL | The same on `--incoming-url` or a `log`, `revert`, or `prune` target → `StorageException` (Generic). |
| Digest sink | `RepoRW.check` builds a `StorageNull` directly to digest a stored object without storing it again → [ROADMAP.md](ROADMAP.md#known-deviations). |

## Storage errors

| Condition | Result |
| -- | -- |
| Backend failure | → `StorageException` (Generic) |
| Object missing on `get` or `get_stream` | → `StorageNotFoundException` (Generic), unless debkit re-raises it; a construction failure on `--target-url` or `--source-url` is re-raised → [Construction](#construction) |

## Transactions

| Rule | Behavior |
| -- | -- |
| Scope | `insert`, `remove`, `sync`, and `check --fix` run their writes inside one `maybe_transaction`; `sync --verify-existing` deletes inside the sync's transaction. The read-only commands record nothing. |
| Retention | `Action.__init__` builds `HistoryRetention.from_env(keep=..., max_age_days=...)` from `--history-keep` and `--history-max-age` and passes it to `maybe_transaction`, so a recorded write prunes expired history → [CONFIG.md](CONFIG.md#global-flags). |
| Non-transactional target | An HTTP target runs the body without a transaction or history. |
| Failure | The body's exception propagates after txstore discards the staged footmarks; a partial commit on an unreachable backend is repaired by re-running `sync --verify-existing`. |

## History commands

| Command | Rule |
| -- | -- |
| Scope | `log`, `revert`, and `prune` address the storage root: the `--target-url` dist and component fields are optional and ignored → [CONFIG.md](CONFIG.md#log). |
| Guard | A non-transactional backend → `ActionException` (Generic). |
| `log` | Lists `{id, timestamp}` records in file order; `-o json` prints the array as the history file stores it. |
| `revert` | Reverts the latest recorded transaction; empty history → `ActionException` (Generic). `--dry-run` names it without reverting. |
| `prune` | Applies the retention policy above; `--orphans` also reclaims footmarks of no recorded transaction; `--dry-run` reports the same paths without deleting. |
| Storage failure | `StorageException`, including a malformed history file, is re-raised as `ActionException` (Generic). |

## Maintenance

Change how debkit calls txstore here and in `action/__init__.py` or `action/action_history.py`
together. A behavior txstore owns is changed in txstore; debkit raises its `txstore` floor in
`pyproject.toml` when it needs the change → [SETUP.md](SETUP.md#dependency-policy).
