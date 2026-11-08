# Validation contract

Owns: input constraints at the CLI, environment, storage-URL, and `RepoParams` boundaries,
path-safety rules, and the sync index selection, including which selected entries a sync probes,
skips, or recopies. Routing → [AGENTS.md](AGENTS.md#canonical-sources).

## Principles

- Validate at the boundary that first receives the value: CLI parser, `DEBKIT_*` variable, storage
  URL, or `RepoParams`.
- Reject unknown schemes and malformed source strings early.
- Failures use `EXCEPTIONS.md` classes, written below as `Exception (group)`; CLI type converters
  raise `ArgumentTypeError` (Usage) → [EXCEPTIONS.md](EXCEPTIONS.md#cli-exit-groups).
- A variable passes through its flag's converter and choices; a rejected value exits 2 naming the
  variable → [CONFIG.md](CONFIG.md#resolution).

## Storage URLs

Implemented by `storage_url()` in `action/__init__.py` for `--target-url`, `--source-url`, and
`--incoming-url`; txstore validates what it receives → [txstore VALIDATION.md](https://github.com/vinklat/txstore/blob/v0.1.1/VALIDATION.md#storage-urls),
including HTTP joins and the S3 path split.

| Rule      | Constraint                                                                                                         | Failure                                  |
| --------- | ------------------------------------------------------------------------------------------------------------------ | ---------------------------------------- |
| Bare path | A value without `://` is a filesystem path, made absolute against the working directory and prefixed with `fs://`. | —                                        |
| `fs://`   | The path is absolute: `fs:///srv/repo`.                                                                            | `fs://relative` → `ValueError` (Generic) |
| Schemes   | `fs`, `s3`, `http`, `https`, from txstore's `get_supported_schemes()`.                                             | Other scheme → `ValueError` (Generic)    |

## Debian source strings

Implemented by `RepoParams.from_deb_source()` in `repo/params.py`. Format is
`<url> <dist> [<component> ...]`; repeated whitespace is collapsed and surrounding whitespace is
stripped before parsing.

| Rule                   | Constraint                                                                        | Failure                                      |
| ---------------------- | --------------------------------------------------------------------------------- | -------------------------------------------- |
| Field count            | At least two fields.                                                              | → `RepoParamsException` (Generic)            |
| Flat repository        | Two fields: `url`, `dist`; no components; `is_flat=True`; `indices_prefix=""`.    | —                                            |
| Componented repository | Three or more fields: `url`, `dist`, components; `indices_prefix="dists/<dist>"`. | —                                            |
| Input type             | A string.                                                                         | Non-string → `RepoParamsException` (Generic) |

## Distribution names

Implemented by `RepoParams.is_real_dist()`.

| Rule      | Constraint                                                   | Failure                                   |
| --------- | ------------------------------------------------------------ | ----------------------------------------- |
| Real dist | Matches `^[A-Za-z0-9]*$`; the empty string matches.          | —                                         |
| Not real  | Contains `/`, `-`, `_`, whitespace, or non-ASCII characters. | Treated as a path-like dist, not an error |

## Repository index paths

Built by `RepoParams.indices_path()` from the layout prefix, component, and inner path.

| Layout                    | Prefix applied to an index path                                                                                                                                                                    |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Componented               | `dists/<dist>/<component>/`, as in `dists/<dist>/main/binary-<arch>/Packages[.<compression>]` or `dists/<dist>/main/source/Sources[.<compression>]`                                                |
| Flat                      | `<dist>/`, as in `<dist>/Packages[.<compression>]`                                                                                                                                                 |
| Rootless flat             | None, as in `Packages[.<compression>]`                                                                                                                                                             |
| Componented, no component | `dists/<dist>/`, as in `dists/<dist>/Contents-<arch>[.<compression>]` at the dists root                                                                                                            |
| No dist                   | None, as in `<component>/binary-<arch>/Packages[.<compression>]`                                                                                                                                   |
| Architecture directory    | `binary-<arch>/` for a binary architecture, `source/` for the source one; built by `RepoParams.arch_dir()`                                                                                         |
| Package index inner path  | `Packages` and `Sources` compose `arch_dir()` and `index_name()` into one inner path, read and write alike                                                                                         |
| Compression preference    | `Packages` and `Sources` discovery follows the decompression map order in `repo/compress.py`.                                                                                                      |
| Release files             | Root `Release`, `Release.gpg`, and `InRelease`, and the per-component `Release` under the architecture directory (`binary-<arch>/` or `source/`), come from `indices_path()`, read and write alike |
| Release key               | `RepoParams.release_key()` inverts the layout prefix: it strips exactly what `indices_path()` prepends                                                                                             |

Every other index, `Contents`, `Translation`, dep11, and `Commands` among them, is reached through
its Release-listed path rather than a built one.

## Index selection

Implemented by `select_index_entry()` in `repo/repo_sync.py`. The sync index set is the
Release-listed paths, filtered by these rules and by the exclude globs below. Publication is settled
per entry only when it would be transferred, not when the set is built.

| Rule                    | Constraint                                                                                                                                                                                                                                                                  | Failure                                                                           |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| Component               | The segment before the first `/`; a key with no `/` belongs to no component and passes                                                                                                                                                                                      | —                                                                                 |
| Known architectures     | The selected architectures plus every root Release token the Release token rule keeps, whatever `--architectures` selects → [Architectures](#architectures)                                                                                                                 | —                                                                                 |
| Architecture token      | A `-<arch>` run ending at `.`, `/`, or the key end, matched only against the known architectures; where several end at one token the longest owns it                                                                                                                        | —                                                                                 |
| Structural rule scope   | The architecture-token, source, and unparsed rules read the key below its component segment, so a component name such as `extra-armhf` or `installer-extras` never trips them                                                                                               | —                                                                                 |
| Source indices          | A key under `<component>/source/`, a `Sources*` file name, or a `-source` token is selected only when `source` is a selected architecture                                                                                                                                   | —                                                                                 |
| Unparsed index          | A key under `debian-installer/` or an `installer-<arch>/` image tree whose `<arch>` is a known architecture, or one with a segment starting `Contents-udeb`, is dropped: nothing parses these, so a mirror would carry metadata for `.udeb` payloads it lacks               | —                                                                                 |
| Unparsed index scope    | The unparsed-index rule is structural, so an exclude glob cannot switch it off                                                                                                                                                                                              | —                                                                                 |
| Compression forms       | Every compression of an index the Release lists is its own entry, uncompressed included; the rules above never prefer one form, so the mirror serves every form its signed Release names                                                                                    | —                                                                                 |
| Unpublished entry       | A selected path the source does not publish and the target lacks is dropped before transfer, not from the set: it is never copied and never purged from the target                                                                                                          | —                                                                                 |
| Absent entry            | A selected path the target lacks and no parse proved is probed on every run, whatever forced the pass and even when both root Releases record its digest alike: the last sync copies the Release whether or not it copied the object                                        | —                                                                                 |
| Absent entry probe      | Only the probe tells a key the source never publishes from one it 404ed once; the probe alone never adds the root Release files to the copy list                                                                                                                            | —                                                                                 |
| Parse-proven entry      | A selected path a parse found, a root Release file or a component `Packages`, is copied without a source probe when the target lacks it: the parse already fetched it from the source                                                                                       | —                                                                                 |
| Held entry              | A selected path the target already holds is transferred without a source probe: an earlier transfer proved the source publishes it, so a changed digest is a republish. Digests both root Releases record alike settle it without a digest round trip                       | A source that stops publishing it fails the transfer → `RepoSyncException` (Sync) |
| Unloaded target         | A target whose Release did not load, empty or reset after a checksum failure, settles no entry: agreement and stored metadata are both skipped, so every selected path it holds is copied again                                                                             | —                                                                                 |
| Later pass              | A pass after the source changed mid-sync reloads the target through the transaction first, so agreement compares against the Release the previous pass staged                                                                                                               | —                                                                                 |
| Payload naming an index | A stanza `Filename` a Release digest block also lists is an index, not a payload: it leaves the package set, so the rules above alone decide whether it is copied or purged and selection cannot disagree with the payload copy → [CRYPTO.md](CRYPTO.md#verification-order) | Dropped from the package set and logged                                           |

## Index exclude globs

| Rule         | Constraint                                                                                                                                              | Failure |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| Value        | Comma-separated `fnmatch` globs; the empty string excludes nothing                                                                                      | —       |
| Empty token  | Dropped, so `a,,b` excludes two globs                                                                                                                   | —       |
| Match target | The path as the root Release lists it, relative to the layout prefix                                                                                    | —       |
| Matching     | Case-sensitive; `*` crosses `/`                                                                                                                         | —       |
| Scope        | Every candidate index, discovered while parsing as well as listed in the Release; the root `Release`, `Release.gpg`, and `InRelease` are never filtered | —       |
| Effect       | An excluded index is not mirrored, and is purged from the target as an orphan                                                                           | —       |

## Sources stanzas

Parsed by `Sources._expand_stanza()`. Each stanza names several payload files under one `Directory`;
every file becomes one record keyed by `<directory>/<name>`.

| Rule                | Constraint                                                                                                                                                                                                                                                                 | Failure                                            |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| Directory           | Present, not `.`, and passes the unsafe-key rule below.                                                                                                                                                                                                                    | → `PackagesException` (Read)                       |
| File name           | A bare name: no `/` segment, and the joined key passes the unsafe-key rule.                                                                                                                                                                                                | → `PackagesException` (Read)                       |
| Size                | Agrees across every hash block that lists the file.                                                                                                                                                                                                                        | → `PackagesException` (Read)                       |
| Digests             | At least one algorithm normalization keeps; `Files` alone is MD5, which it drops → [CRYPTO.md](CRYPTO.md#checksums).                                                                                                                                                       | → `PackagesException` (Read)                       |
| Shared payload      | A file named by several stanzas is kept once, last stanza wins; disagreeing digests are logged.                                                                                                                                                                            | —                                                  |
| Mutation            | `insert` is refused; `remove` matches nothing; `save` writes nothing.                                                                                                                                                                                                      | `insert` → `PackagesException` (Read)              |
| Container           | The source architecture resolves to `Sources` on the read and the write path alike, through `container_for()` in `repo/component.py`: `Packages.setup` names its file from the layout, so a binary container would write a binary index into the canonical `Sources` path. | → `PackagesException` (Read) on the refused insert |
| Insert architecture | A package whose control field names the source architecture is refused before the transfer, as a `.changes` is when any of its `Architecture` tokens names it.                                                                                                             | → `ActionException` (Generic)                      |

## Filename safety

Package metadata names storage objects, so a `Filename:` field copied from a source repo picks the
key a write lands on, and a root `Release` digest block names the index keys a mirror fetches. The
checks below constrain both; unsafe names fail before any storage mutation.

| Rule                   | Constraint                                                                                                                                                          | Failure                                      |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------- |
| Unsafe key             | txstore's `is_unsafe_path()`, shared by every caller below → [txstore VALIDATION.md](https://github.com/vinklat/txstore/blob/v0.1.1/VALIDATION.md#filename-safety). | —                                            |
| `Packages.remove()`    | Matched package filename passes the unsafe-key rule.                                                                                                                | → `PackagesException` (Read)                 |
| `RepoSync` index set   | Release-listed index key and path pass the unsafe-key rule.                                                                                                         | Entry dropped from the index set and logged  |
| `Packages` index parse | Stanza `Filename` passes the unsafe-key rule.                                                                                                                       | Stanza dropped from the container and logged |
| `Sources` payload      | Stanza `Directory` and the joined payload key pass the unsafe-key rule.                                                                                             | → `PackagesException` (Read)                 |
| Storage paths          | Confinement, reserved names, and footmark reclaim → [txstore VALIDATION.md](https://github.com/vinklat/txstore/blob/v0.1.1/VALIDATION.md#filename-safety).          | → `StorageException` (Generic)               |

## Log levels

Flag and variable → [CONFIG.md](CONFIG.md#global-flags).

| Rule            | Constraint                                                         | Failure                     |
| --------------- | ------------------------------------------------------------------ | --------------------------- |
| Accepted values | `CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG`; case-insensitive. | Other → usage error (Usage) |

## History retention

Implemented by `positive_int_type()` and `history_max_age_type()` in `argtypes.py`; flags →
[CONFIG.md](CONFIG.md#global-flags).

| Rule                | Constraint                                                                                                  | Failure                               |
| ------------------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------- |
| Integer             | Plain ASCII decimal, optional sign, surrounding whitespace ignored; `1_0` and non-ASCII digits are refused. | → `ArgumentTypeError` (Usage)         |
| `--history-keep`    | 1 or greater.                                                                                               | Below 1 → `ArgumentTypeError` (Usage) |
| `--history-max-age` | 0 to 36500 days, txstore's bound; `0` disables pruning.                                                     | Outside → `ArgumentTypeError` (Usage) |

## Environment values

Implemented by `_env.py` and `_options.py`; resolution → [CONFIG.md](CONFIG.md#resolution).

| Rule         | Constraint                                                                   | Failure                                         |
| ------------ | ---------------------------------------------------------------------------- | ----------------------------------------------- |
| Empty        | An empty variable counts as unset.                                           | —                                               |
| Switch true  | `1`, `true`, `yes`, `on`; case-insensitive, surrounding whitespace ignored.  | —                                               |
| Switch false | `0`, `false`, `no`, `off`; case-insensitive, surrounding whitespace ignored. | —                                               |
| Switch other | Never read as true, so a typo cannot disable verification.                   | → usage error naming the variable (Usage)       |
| Valued       | Converted and checked as the flag's value would be.                          | → usage error naming the variable (Usage)       |
| Prefix       | Only `DEBKIT_` names are read.                                               | Other name → `ValueError` (a programming error) |

## Release dates

Implemented by `parse_release_date()` and `Release.date()` in `repo/release.py` for the root Release
`Date` and `Valid-Until` fields. Both raise `ValueError`, which never reaches the CLI: the freshness
check maps it → [CRYPTO.md](CRYPTO.md#release-freshness).

| Rule   | Constraint                                                                                                                                                                                                                  | Failure                                                           |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| Format | RFC 2822 date as `email.utils.parsedate_to_datetime` reads it, e.g. `Sat, 10 Jun 2023 08:47:46 UTC`.                                                                                                                        | Rejected by the parser → [CRYPTO.md](CRYPTO.md#release-freshness) |
| Zone   | Last token `UTC`, `GMT`, `UT`, `Z`, or a numeric `+hhmm` / `-hhmm` offset; `-0000` is read as UTC. A named zone such as `CEST`, a missing zone, or trailing text is refused, since it would otherwise read silently as UTC. | Rejected by the parser → [CRYPTO.md](CRYPTO.md#release-freshness) |

## Package pattern matching

Used by `ActionPkg.remove()` and `Packages.remove()`; flag → [CONFIG.md](CONFIG.md#remove).

| Rule               | Constraint                                                                                                                                                                 | Failure                                                                       |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Shape              | JSON object mapping package field names to regex patterns; any field, no allow-list.                                                                                       | —                                                                             |
| Match              | Every supplied field pattern matches, using `re.search` without anchoring.                                                                                                 | —                                                                             |
| Length             | At most 500 characters; checked before compilation.                                                                                                                        | Longer → `ValueError("Regex pattern too long (N chars, max 500).")` (Generic) |
| Nested quantifiers | No quantifier (`+`, `*`, `?`, or an unbounded `{n,}`) inside a quantified group, e.g. `(a+)+`; a bounded `{n}` or `{n,m}` never counts as one. Checked before compilation. | → `ValueError("Regex pattern rejected: nested quantifiers ...")` (Generic)    |
| Compiles           | Every supplied pattern compiles with `re.compile`.                                                                                                                         | `re.error` → `ValueError("Invalid regex pattern: ...")` (Generic)             |

## Architectures

Used by `ActionSync` and `insert`; flag → [CONFIG.md](CONFIG.md#sync). The token rule is
`is_valid_arch()` in `repo/params.py`: a token names the `binary-<arch>/` directory of a storage
path.

| Rule          | Constraint                                                                                                                                                                  | Failure                                  |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| Value         | Comma-separated string, default `amd64,arm64`; split on commas, with no trimming or case folding.                                                                           | —                                        |
| Empty token   | Dropped, so `amd64,,arm64` selects two architectures; a value of commas alone selects none, and the Release tokens decide.                                                  | —                                        |
| Empty string  | No override is applied.                                                                                                                                                     | —                                        |
| Token         | Lower-case letters, digits, and `-`, not starting with `-`, e.g. `amd64`, `hurd-i386`.                                                                                      | Flag token → `ActionException` (Generic) |
| Release token | A root Release `Architectures` or `Architecture` token follows the token rule.                                                                                              | Dropped and logged                       |
| Upload token  | Every `.changes` `Architecture` token and the `.deb` control `Architecture` follow the token rule, checked before the transfer.                                             | → `ActionException` (Generic)            |
| Insert target | A `.deb` goes to its control `Architecture` alone; an `all` or missing one goes to every root Release architecture but `source`. The `.changes` field and its file order play no part. | — |
| No binary target | An `all` or arch-less `.deb` while the root Release lists no architecture but `source`, refused before its transfer. | → `ActionException` (Generic) |
| `source`      | Selects the source pseudo-architecture: `<component>/source/Sources*` is parsed and every payload it lists joins the package set. The index is mirrored, never regenerated. | —                                        |

## S3 canned ACLs

Implemented by `object_acl_type()` and `bucket_acl_type()` in `argtypes.py` over txstore's
`parse_object_acl()` and `parse_bucket_acl()`; values → [txstore VALIDATION.md](https://github.com/vinklat/txstore/blob/v0.1.1/VALIDATION.md#s3-canned-acls).

| Rule               | Constraint                                      | Failure                             |
| ------------------ | ----------------------------------------------- | ----------------------------------- |
| `--s3-default-acl` | An object canned ACL, or `none` to send no ACL. | Other → `ArgumentTypeError` (Usage) |
| `--s3-bucket-acl`  | A bucket canned ACL.                            | Other → `ArgumentTypeError` (Usage) |

## Maintenance

Add the rule here with its failure notation, add success and failure tests per
[TESTING.md](TESTING.md#required-coverage), and add a new class to
[EXCEPTIONS.md](EXCEPTIONS.md#domain-exceptions) when one is introduced. A rule txstore owns is
linked, never restated.
