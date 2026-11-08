# Configuration contract

Owns: CLI flag names, their `DEBKIT_*` variables, resolution order, defaults, secrets, and only the
behavior needed to interpret a value. Routing → [AGENTS.md](AGENTS.md#canonical-sources).

`—` in Default means required. Flags are registered in the layer that owns them: global and S3
flags in `argparser.py`, subcommand flags in the owning `Action*` module.

## Resolution

| Rule      | Behavior                                                                                                                                                                                                    |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Order     | The flag, then its `DEBKIT_*` variable, then the default.                                                                                                                                                   |
| Empty     | An empty variable counts as unset.                                                                                                                                                                          |
| Switches  | A boolean flag is a bare switch with a `--no-` form, so `--no-skip-verify` overrides `DEBKIT_SKIP_VERIFY=1`; its variable takes the boolean vocabulary → [VALIDATION.md](VALIDATION.md#environment-values). |
| Values    | A variable passes through the flag's converter and choices; a rejected value exits 2 naming the variable → [VALIDATION.md](VALIDATION.md#principles).                                                       |
| Required  | A required flag with neither flag nor variable exits 2 naming both.                                                                                                                                         |
| Lazy read | A variable is read only when its flag is absent, after argparse parses the command line.                                                                                                                    |
| Reader    | Only `src/debkit/_env.py` reads the environment.                                                                                                                                                            |
| txstore   | debkit passes every storage setting resolved, except S3 credentials left unset: txstore then reads `TXSTORE_S3_ACCESS` / `TXSTORE_S3_SECRET` → [STORAGE.md](STORAGE.md#construction).                       |

## Global flags

| Flag                  | Variable                  | Default           | Interpretation                                                                              |
| --------------------- | ------------------------- | ----------------- | ------------------------------------------------------------------------------------------- |
| `-V`, `--version`     | none                      | none              | Prints `__version__` and exits                                                              |
| `--target-url`        | `DEBKIT_TARGET_URL`       | —                 | Target deb source string → [VALIDATION.md](VALIDATION.md#debian-source-strings)             |
| `-l`, `--log-level`   | `DEBKIT_LOG_LEVEL`        | `INFO`            | Values → [VALIDATION.md](VALIDATION.md#log-levels)                                          |
| `--log-format`        | `DEBKIT_LOG_FORMAT`       | `plain`           | `plain` lines (module and function added at DEBUG) or one `json` object per line            |
| `-n`, `--skip-verify` | `DEBKIT_SKIP_VERIFY`      | off               | Disables TLS, GPG, and digest verification → [CRYPTO.md](CRYPTO.md#verification-switches)   |
| `--skip-verify-ssl`   | `DEBKIT_SKIP_VERIFY_SSL`  | off               | Disables TLS certificate verification                                                       |
| `--skip-verify-gpg` | `DEBKIT_SKIP_VERIFY_GPG` | off | Disables GPG verification and Release signing → [CRYPTO.md](CRYPTO.md#verification-switches) |
| `--gnupg-home`        | `DEBKIT_GNUPG_HOME`       | `.gnupg`          | GnuPG home directory → [CRYPTO.md](CRYPTO.md#gpg)                                           |
| `--gnupg-keyring`     | `DEBKIT_GNUPG_KEYRING`    | `trustedkeys.gpg` | Trusted keyring filename                                                                    |
| `--gnupg-passphrase`  | `DEBKIT_GNUPG_PASSPHRASE` | empty string      | Secret                                                                                      |
| `--history-keep`      | `DEBKIT_HISTORY_KEEP`     | `20`              | Newest transactions never pruned; values → [VALIDATION.md](VALIDATION.md#history-retention) |
| `--history-max-age`   | `DEBKIT_HISTORY_MAX_AGE`  | `90`              | Days after which a transaction outside `--history-keep` is pruned; `0` disables pruning     |

## Storage flags

How they reach txstore → [STORAGE.md](STORAGE.md#construction).

| Flag                | Variable                | Default       | Interpretation                                                                                          |
| ------------------- | ----------------------- | ------------- | ------------------------------------------------------------------------------------------------------- |
| `-u`, `--s3-access` | `DEBKIT_S3_ACCESS`      | none          | Secret; required for `s3://`, from this flag, its variable, or `TXSTORE_S3_ACCESS`                      |
| `-t`, `--s3-secret` | `DEBKIT_S3_SECRET`      | none          | Secret; required for `s3://`, from this flag, its variable, or `TXSTORE_S3_SECRET`                      |
| `--s3-default-acl`  | `DEBKIT_S3_DEFAULT_ACL` | `public-read` | Object ACL for writes, or `none` to send no ACL; values → [VALIDATION.md](VALIDATION.md#s3-canned-acls) |
| `--s3-bucket-acl`   | `DEBKIT_S3_BUCKET_ACL`  | none          | Bucket ACL when a run creates the bucket; unset sends none                                              |

## Output flag

Accepted by `dump`, `list`, `stats`, `insert`, `remove`, `sync`, `check`, `log`, and `prune`.

| Flag             | Variable        | Default | Interpretation    |
| ---------------- | --------------- | ------- | ----------------- |
| `-o`, `--output` | `DEBKIT_OUTPUT` | `brief` | `brief` or `json` |

## Insert

| Flag                 | Variable                  | Default | Interpretation                                                             |
| -------------------- | ------------------------- | ------- | -------------------------------------------------------------------------- |
| `--changes-filename` | `DEBKIT_CHANGES_FILENAME` | none    | `.changes` input                                                           |
| `--changes-sha256`   | `DEBKIT_CHANGES_SHA256`   | none    | Expected SHA256 of the `.changes` file                                     |
| `--deb-filename`     | `DEBKIT_DEB_FILENAME`     | none    | Single `.deb` input                                                        |
| `--incoming-url`     | `DEBKIT_INCOMING_URL`     | —       | Incoming storage URL or path → [VALIDATION.md](VALIDATION.md#storage-urls) |
| `--component`        | `DEBKIT_COMPONENT`        | `main`  | Target component                                                           |

## Remove

| Flag            | Variable             | Default | Interpretation                                                                      |
| --------------- | -------------------- | ------- | ----------------------------------------------------------------------------------- |
| `--pkg-pattern` | `DEBKIT_PKG_PATTERN` | —       | JSON map of field → regex → [VALIDATION.md](VALIDATION.md#package-pattern-matching) |

## Sync

| Flag                       | Variable                        | Default       | Interpretation                                                                                                   |
| -------------------------- | ------------------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------- |
| `--source-url`             | `DEBKIT_SOURCE_URL`             | `.`           | Source storage URL or path → [VALIDATION.md](VALIDATION.md#storage-urls)                                         |
| `--architectures`          | `DEBKIT_ARCHITECTURES`          | `amd64,arm64` | Comma-separated list; `source` selects source packages → [VALIDATION.md](VALIDATION.md#architectures)            |
| `--indices-exclude`        | `DEBKIT_INDICES_EXCLUDE`        | empty string  | Comma-separated globs of Release-listed index paths to skip → [VALIDATION.md](VALIDATION.md#index-exclude-globs) |
| `--allow-release-rollback` | `DEBKIT_ALLOW_RELEASE_ROLLBACK` | off           | Skips the Release freshness checks → [CRYPTO.md](CRYPTO.md#release-freshness)                                    |
| `--verify-existing`        | none                            | off           | Verify target packages before sync                                                                               |
| `--indices-only`           | none                            | off           | Sync index objects only                                                                                          |

## Check

| Flag    | Variable | Default | Interpretation         |
| ------- | -------- | ------- | ---------------------- |
| `--fix` | none     | off     | Delete corrupted files |

Also accepts `--architectures` and `--indices-exclude` with the same variables and defaults as
[Sync](#sync): a check builds its index and package sets the same way a sync does, so other values
report the difference as missing or orphan objects. `--fix` deletes those orphans, so a check
narrower than the sync that built the mirror purges what it leaves out.

## Log

No subcommand flags; uses `--target-url` and `-o`. Root-scoped: the `--target-url` dist and
component fields are optional and ignored → [STORAGE.md](STORAGE.md#history-commands).

## Dry-run flag

Accepted by `revert` and `prune`.

| Flag        | Variable | Default | Interpretation                              |
| ----------- | -------- | ------- | ------------------------------------------- |
| `--dry-run` | none     | off     | Show what would be changed without applying |

## Revert

Uses `--target-url`, whose dist and component fields are optional and ignored; reverts the latest
recorded transaction → [STORAGE.md](STORAGE.md#history-commands). Accepts `--dry-run`.

## Prune

Uses `--target-url`, whose dist and component fields are optional and ignored; applies the
`--history-keep` and `--history-max-age` retention policy →
[STORAGE.md](STORAGE.md#history-commands).
Root-scoped, and accepts `--dry-run` and `-o`.

| Flag        | Variable | Default | Interpretation                                                    |
| ----------- | -------- | ------- | ----------------------------------------------------------------- |
| `--orphans` | none     | off     | Also remove footmark objects belonging to no recorded transaction |

## Secrets

- `--gnupg-passphrase`, `--s3-access`, and `--s3-secret`, their variables, and the
  `TXSTORE_S3_*` fallbacks are secrets.
- `--help` names their variables but never shows a value.
- Pass them through variables in automation; never print, log, or fixture them.

## Maintenance

Register a new flag at the owning layer with `add_env_argument` (or `add_argument` when no variable
is intentional), a safe default, and its row here in the same change; `scripts/check_docs.py`
verifies flag and variable parity with the code. Values that need validation get a section in
[VALIDATION.md](VALIDATION.md).
