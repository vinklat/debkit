# Quick how-to

Worked examples for the `debkit` CLI: inspecting, changing, mirroring, checking, and reverting
Debian repositories. Most flags shown have a `DEBKIT_*` environment variable, listed in
[CONFIG.md](../../CONFIG.md); `--verify-existing`, `--fix`, `--dry-run`, and `--orphans` are flags
only.

## Describe a repository

Repositories are addressed the way an APT source line does it:
`https://deb.debian.org/debian bookworm main contrib non-free` names the server, the distribution,
and the components. Wherever the tool asks for a repository, pass this string. The URL scheme picks
the storage backend:

- a filesystem path, or `fs://` with an absolute path, for a local directory,
- `s3://` for an S3-compatible bucket,
- `https://` or `http://` for a served repository, read only.

Details → [VALIDATION.md](../../VALIDATION.md#debian-source-strings) and
[VALIDATION.md](../../VALIDATION.md#storage-urls).

## Inspect a repository

### List packages

From a public HTTP repository. Its `Release` is verified against the trusted keyring, so import
the Debian archive keys first, as in the [quick start](../../README.md#quick-start):

```bash
debkit --target-url="https://deb.debian.org/debian bookworm main contrib non-free" list
```

From a local directory (`debian`, resolved against the current working directory; any absolute or
relative path works, `..` included). The dist and components follow the path as for any source:

```bash
debkit --target-url="debian bookworm main contrib non-free" list
```

From an S3 bucket; the secret key is read only from its variable, never from a flag:

```bash
export DEBKIT_TARGET_URL="s3://s3.example.com/bucket/prefix"
export DEBKIT_S3_ACCESS="XXX"
export DEBKIT_S3_SECRET="YYY"

debkit list
```

Add `-o json` to any inspecting command for machine-readable output:

```bash
debkit list -o json
```

### Repository statistics

```bash
debkit stats
```

```
Packages:
  main:
    amd64: 22.4 GB, 4083 packages
    arm64: 2.9 GB, 1503 packages
Indices read:
  1.0 MB, 5 files
```

```bash
debkit stats -o json
```

```json
{
  "packages": {
    "main": {
      "amd64": {
        "packages_total": 4083,
        "packages_bytes": 22414439636
      },
      "arm64": {
        "packages_total": 1503,
        "packages_bytes": 2889875804
      }
    }
  },
  "indices": {
    "files_total": 5,
    "files_bytes": 1038860
  }
}
```

### Dump the repository structure

Human-readable structure without the package list:

```bash
debkit dump
```

Everything the tool knows, for machine processing or backup (can be large):

```bash
debkit dump -o json > debian.json
```

## Change packages

### Add packages from a `.changes` upload

Place the `.changes` file and its `.deb` files in the incoming storage; the tool moves them into the
repository and rewrites the indices in one transaction.

```bash
export DEBKIT_TARGET_URL="s3://s3.example.com/repo bookworm main"
export DEBKIT_S3_ACCESS="XXX"
export DEBKIT_S3_SECRET="YYY"
export DEBKIT_GNUPG_PASSPHRASE="ZZZ"
export DEBKIT_GNUPG_HOME=/path/to/.gnupg
export DEBKIT_INCOMING_URL="s3://s3.example.com/incoming"
export DEBKIT_CHANGES_FILENAME=nginx.changes
export DEBKIT_CHANGES_SHA256="12345..."
export DEBKIT_COMPONENT=main

debkit insert
```

`DEBKIT_INCOMING_URL` may point at another bucket or prefix than the repository. Every variable
except the secrets `DEBKIT_S3_SECRET` and `DEBKIT_GNUPG_PASSPHRASE` is also a flag; see
`debkit insert -h`, [CONFIG.md](../../CONFIG.md#insert), and
[CONFIG.md](../../CONFIG.md#variable-only-settings).

### Add a single `.deb` file

```bash
export DEBKIT_TARGET_URL="s3://s3.example.com/repo bookworm main"
export DEBKIT_S3_ACCESS="XXX"
export DEBKIT_S3_SECRET="YYY"
export DEBKIT_GNUPG_PASSPHRASE="ZZZ"
export DEBKIT_GNUPG_HOME=/path/to/.gnupg
export DEBKIT_INCOMING_URL="s3://s3.example.com/incoming"
export DEBKIT_DEB_FILENAME=ansible-core_2.16.5-1ppa~jammy_all.deb
export DEBKIT_COMPONENT=main

debkit insert
```

### Remove packages

```bash
export DEBKIT_TARGET_URL="s3://s3.example.com/repo bookworm main"
export DEBKIT_S3_ACCESS="XXX"
export DEBKIT_S3_SECRET="YYY"
export DEBKIT_GNUPG_PASSPHRASE="ZZZ"
export DEBKIT_GNUPG_HOME=/path/to/.gnupg
export DEBKIT_PKG_PATTERN='{"Filename":"some_package1.deb|some_package2.deb"}'

debkit remove
```

Every package matching all regex patterns is removed from every component and architecture. Pattern
rules → [VALIDATION.md](../../VALIDATION.md#package-pattern-matching).

## Mirror a repository

### Into a local directory

Mirrors a public repository into `debian` under the current working directory. The source
`Release` is verified against the trusted keyring, as for [List packages](#list-packages):

```bash
export DEBKIT_SOURCE_URL="https://deb.debian.org/debian bookworm main contrib non-free"
export DEBKIT_TARGET_URL="debian bookworm main contrib non-free"

debkit sync
```

Every index the source `Release` lists is mirrored for the selected components and architectures:
`Packages`, `Contents`, `Translation` files, dep11 metadata, and `Commands`. Trim the set with
exclude globs when the mirror does not need all of it:

```bash
debkit sync --indices-exclude '*/i18n/*,*/dep11/icons-*'
```

Every compression the source publishes is mirrored too, uncompressed forms included. A glob that
names the bare file drops only that form, since matching runs on the whole path:

```bash
debkit sync --indices-exclude '*/Contents-all,*/Contents-amd64,*/Contents-arm64'
```

An excluded index is also removed from the target →
[VALIDATION.md](../../VALIDATION.md#index-exclude-globs).

Source packages are mirrored only when you select the `source` architecture, which brings both the
`Sources` indices and every `.dsc`, `.orig.tar.*`, and `.debian.tar.*` payload they name:

```bash
debkit sync --architectures amd64,arm64,source
```

Without it the source indices are skipped, like any unselected architecture. Selecting `source`
against a repository that publishes none fails the run, as an unavailable architecture does.
`insert` does not accept source packages → [ROADMAP.md](../../ROADMAP.md#phases).

### Into an S3 bucket

```bash
export DEBKIT_SOURCE_URL="https://deb.debian.org/debian bookworm main contrib non-free"
export DEBKIT_TARGET_URL="s3://s3.example.com/debian bookworm main contrib non-free"
export DEBKIT_S3_ACCESS="XXX"
export DEBKIT_S3_SECRET="YYY"
export DEBKIT_GNUPG_HOME=/path/to/.gnupg
# optional: DEBKIT_LOG_LEVEL=DEBUG, DEBKIT_SKIP_VERIFY_SSL=1, DEBKIT_SKIP_VERIFY_GPG=1

debkit sync
```

## Check consistency

### Verify existing packages while syncing

Checks every existing target package before syncing and fixes corrupted files. This reads every
package, so it is far more data intensive than a plain sync.

```bash
debkit sync --verify-existing
```

### Standalone check

Loads the indices, detects missing packages, downloads every package and validates its checksums.
Exit status is 0 when the repository is consistent, otherwise 1; `-o json` gives a machine-readable
report. Pass the selection flags the mirror was synced with, as for [Check and fix](#check-and-fix).

```bash
debkit check
```

### Check and fix

Deletes orphan files not referenced by any index and files with invalid checksums. A check selects
indices and packages the way a sync does, so pass it the same `--architectures` and
`--indices-exclude` the mirror was synced with: anything outside them counts as an orphan and is
deleted → [CONFIG.md](../../CONFIG.md#check). The deletions are recorded, so `revert` can undo
them → [Transaction history](#transaction-history). A mirror synced with `source` needs:

```bash
debkit check --fix --architectures amd64,arm64,source
```

## Transaction history

Every `insert`, `remove`, `sync`, and `check --fix` that changed something is recorded, on `fs://`
and `s3://` targets, so the latest change can be undone. These commands address only the storage
root, so the URL needs no dist or components →
[STORAGE.md](../../STORAGE.md#history-commands).

```bash
debkit --target-url="debian" log
debkit --target-url="debian" log -o json
debkit --target-url="debian" revert --dry-run
debkit --target-url="debian" revert
```

`revert` undoes only the most recent recorded transaction and removes it from the history, so
repeated reverts walk back one change at a time. How history is stored and locked →
[STORAGE.md](../../STORAGE.md#history-commands).

### Pruning old transactions

The history is bounded: a record is dropped once it is older than `--history-max-age` days, unless
it is among the newest `--history-keep` transactions. Pruning happens automatically after each
recorded write, and on demand:

```bash
debkit --target-url="debian" prune --dry-run
debkit --target-url="debian" prune
debkit --target-url="debian" --history-max-age=30 --history-keep=5 prune
debkit --target-url="debian" prune --orphans --dry-run
```

A pruned transaction stays applied and stops being revertable; nothing in the repository itself
changes. `--orphans` additionally removes hidden footmark objects that belong to no recorded
transaction, which is how residue of an interrupted write is reclaimed. Use `--history-max-age=0` to
switch pruning off. The full rule →
[txstore STORAGE.md](https://github.com/vinklat/txstore/blob/v0.1.1/STORAGE.md#retention).

## Further reading

- [CONFIG.md](../../CONFIG.md) for every flag, environment variable, and default.
- [VALIDATION.md](../../VALIDATION.md) for source strings, URL schemes, and pattern rules.
- [STORAGE.md](../../STORAGE.md) for backends, transactions, and history.
- [CRYPTO.md](../../CRYPTO.md) for GPG verification, signing, and checksum policy.
- [EXCEPTIONS.md](../../EXCEPTIONS.md#cli-exit-groups) for the exit messages you will see.
