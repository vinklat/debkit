# Validation contract

Owns: input constraints at the CLI, environment, storage-URL, and `RepoParams` boundaries,
path-safety rules, and the sync index selection, including which selected entries a sync copies,
skips, or recopies. Routing → [AGENTS.md](AGENTS.md#canonical-sources).

## Principles

- Validate at the boundary that first receives the value: CLI parser, `DEBKIT_*` variable, storage
  URL, or `RepoParams`.
- Reject unknown schemes and malformed source strings early.
- Failures use `EXCEPTIONS.md` classes, written below as `Exception (group)`; CLI type converters
  raise `ArgumentTypeError` (Usage) → [EXCEPTIONS.md](EXCEPTIONS.md#cli-exit-groups).
- Variable resolution and rejected values → [CONFIG.md](CONFIG.md#resolution).

## Storage URLs

Implemented by `storage_url()` in `action/__init__.py` for `--target-url`, `--source-url`, and
`--incoming-url`; txstore validates what it receives →
[txstore VALIDATION.md](https://github.com/vinklat/txstore/blob/v0.1.2/VALIDATION.md#storage-urls),
including HTTP joins and the S3 path split.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Bare path | A value without `://` is a filesystem path, made absolute against the working directory and prefixed with `fs://`; any other value passes to txstore unchanged. | — |
| `fs://` | The path is absolute: `fs:///srv/repo`. | `fs://relative` → `RepoParamsException` (Generic) |
| Schemes | `fs`, `s3`, `http`, `https`, from txstore's `get_supported_schemes()`. | Other scheme → `RepoParamsException` (Generic) |
| Other refusal | Any other URL txstore refuses fails the same way: `open_storage()` wraps txstore's `ValueError`. | → `RepoParamsException` (Generic) |

## Debian source strings

Implemented by `RepoParams.from_deb_source()` in `repo/params.py`. Format is
`<url> <dist> [<component> ...]`; repeated whitespace is collapsed and surrounding whitespace is
stripped before parsing.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Field count | At least two fields. | → `RepoParamsException` (Generic) |
| Flat repository | Two fields: `url`, `dist`; no components; `is_flat=True`; `indices_prefix=""`. | — |
| Componented repository | Three or more fields: `url`, `dist`, components; `indices_prefix="dists/<dist>"`. | — |
| Input type | A string. | Non-string → `RepoParamsException` (Generic) |
| Names | A componented source's dist and every component pass `is_safe_name()`: non-empty, not `.`, and the [unsafe-key rule](#filename-safety); `/` is allowed, as in `updates/main`. A flat dist names a directory only when it is a real dist. | → `RepoParamsException` (Generic) |
| `--component` | `component_type()` in `argtypes.py` applies the same rule. | → `ArgumentTypeError` (Usage) |
| Release `Components` token | A root Release token follows the same rule. | Dropped and logged |

## Distribution names

Implemented by `RepoParams.is_real_dist()`.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Real dist | Matches `^[A-Za-z0-9]*$`; the empty string matches. | — |
| Not real | Contains `/`, `-`, `_`, whitespace, or non-ASCII characters. | Treated as a path-like dist, not an error |

## Repository index paths

Built by `RepoParams.indices_path()` from the layout prefix, component, and inner path.

| Layout | Prefix applied to an index path |
| -- | -- |
| Componented | `dists/<dist>/<component>/`, as in `dists/<dist>/main/binary-<arch>/Packages[.<compression>]` or `dists/<dist>/main/source/Sources[.<compression>]` |
| Flat | `<dist>/`, as in `<dist>/Packages[.<compression>]` |
| Rootless flat | None, as in `Packages[.<compression>]` |
| Componented, no component | `dists/<dist>/`, as in `dists/<dist>/Contents-<arch>[.<compression>]` at the dists root |
| No dist | None, as in `<component>/binary-<arch>/Packages[.<compression>]` |
| Architecture directory | `binary-<arch>/` for a binary architecture, `source/` for the source one; built by `RepoParams.arch_dir()` |
| Package index inner path | `Packages` and `Sources` compose `arch_dir()` and `index_name()` into one inner path, read and write alike |
| Compression preference | `Packages` and `Sources` discovery follows the codec order in `repo/compress.py`, the uncompressed index last; a save rewrites every form discovery found |
| Release files | Root `Release`, `Release.gpg`, and `InRelease`, and the per-component `Release` under the architecture directory (`binary-<arch>/` or `source/`), come from `indices_path()`, read and write alike |
| Component Release | Only a componented repo reads or writes a per-component `Release`; its component and architecture are non-empty; an empty one is a programming error, an uncaught `ValueError`. |
| Flat component | A flat layout has one component slot: insert joins the component it loaded, `/` when its Release names none, whatever `--component` says. |
| Flat Components field | A new flat repository is set up without `Components`; a save never rewrites the field, so a loaded one is kept. |
| Flat pool key | `--component` still names the pool key of a flat insert → [Filename safety](#filename-safety). |
| Flat index | Every arch slot of a flat component shares one `Packages` container, and one `Sources` container, since the layout names one file whatever the arch; it is saved once. |
| Release key | `RepoParams.release_key()` inverts the layout prefix: it strips exactly what `indices_path()` prepends |

Every other index, `Contents`, `Translation`, dep11, and `Commands` among them, is reached through
its Release-listed path rather than a built one.

## Index selection

Implemented by `select_index_entry()` in `repo/index_selection.py`. The sync index set is the
Release-listed paths, filtered by these rules and by the exclude globs below. Publication is settled
per entry by its transfer; the source is never probed.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Component | The segment before the first `/`; a key with no `/` belongs to no component and passes | — |
| Known architectures | The selected architectures plus every root Release token the Release token rule keeps, whatever `--architectures` selects → [Architectures](#architectures) | — |
| Architecture token | A `-<arch>` run ending at `.`, `/`, or the key end, matched only against the known architectures; where several end at one token the longest owns it | — |
| Structural rule scope | The architecture-token, source, and unparsed rules read the key below its component segment, so a component name such as `extra-armhf` or `installer-extras` never trips them | — |
| Source indices | A key under `<component>/source/`, a `Sources*` file name, or a `-source` token is selected only when `source` is a selected architecture | — |
| Unparsed index | A key under `debian-installer/` or an `installer-<arch>/` image tree whose `<arch>` is a known architecture, or one with a segment starting `Contents-udeb`, is dropped: nothing parses these, so a mirror would carry metadata for `.udeb` payloads it lacks | — |
| Unparsed index scope | The unparsed-index rule is structural, so an exclude glob cannot switch it off | — |
| Compression forms | Every compression of an index the Release lists is its own entry, uncompressed included; the rules above never prefer one form, so the mirror serves every form its signed Release names | — |
| Unpublished entry | A selected path the source does not publish stays in the set, so it is never purged from the target | — |
| Payload deletion | A payload dropped from one source index is deleted from the target only when no selected source index still lists it, so a shared `all` payload survives its removal from one architecture | — |
| Shared root | A componented root may hold other dists beside the own one, sharing `pool/`: a sync, `check --fix`, or `remove` never deletes a path under another `dists/<dist>/`, nor a payload another dist's index lists | — |
| Shared root claims | Each other dist loads exactly the binary and source indices its root Release lists, without signature or digest checks, since its claims only keep paths; a flat layout has no `dists/` tree, so the shared root rules do not apply | — |
| Shared root unknown | Another dist whose name is not safe, or whose Release or a listed index does not load or is missing, leaves its payloads unknown; so does a storage that cannot list | → nothing outside the own `dists/<dist>/` is deleted, logged as a warning |
| Absent entry | A selected path the target lacks and no parse proved is copied by a forced pass without a source probe, even when both root Releases record its digest alike: the last sync copies the Release whether or not it copied the object | Missing at the source → skipped at transfer, logged at debug, never retried |
| Absent entry recovery | An absent entry never forces the index pass, so a run with nothing else to sync copies nothing; one the source 404ed once is recovered by the next forced pass or `--indices-only` | — |
| Parse-proven entry | A selected path a parse found, a root Release file or a component `Packages`, is copied without a source probe when the target lacks it: the parse already fetched it from the source | A source that stops publishing it fails the transfer → `RepoSyncException` (Sync) |
| Held entry | A selected path the target already holds is transferred without a source probe: an earlier transfer proved the source publishes it, so a changed digest is a republish. Digests both root Releases record alike settle it without a digest round trip | A source that stops publishing it fails the transfer → `RepoSyncException` (Sync) |
| Unloaded target | A target whose Release did not load, empty or reset after a checksum failure, settles no entry: agreement and stored metadata are both skipped, so every selected path it holds is copied again | — |
| Unreadable target index | A target index skipped as unreadable forces the index pass even when both root Releases are the same bytes: the plan compares the loaded packages, never the Release hashes alone → [Index loading](#index-loading) | — |
| Later pass | A pass after the source changed mid-sync reloads the target through the transaction first, so agreement compares against the Release the previous pass staged | — |
| Unsettled source | A source whose Release hash still differs after `MAX_UPDATED_REPO_RETRIES` (4) passes is not read again: the run commits the last pass, which matches the Release it loaded | Logged as a warning |
| Payload naming an index | A stanza `Filename` a Release digest block also lists is an index, not a payload: it leaves the package set, so the rules above alone decide whether it is copied or purged and selection cannot disagree with the payload copy → [CRYPTO.md](CRYPTO.md#verification-order) | Dropped from the package set and logged |

## Index exclude globs

| Rule | Constraint | Failure |
| -- | -- | -- |
| Value | Comma-separated `fnmatch` globs; the empty string excludes nothing | — |
| Empty token | Dropped, so `a,,b` excludes two globs | — |
| Match target | The path as the root Release lists it, relative to the layout prefix | — |
| Matching | Case-sensitive; `*` crosses `/` | — |
| Scope | Every candidate index, discovered while parsing as well as listed in the Release; the root `Release`, `Release.gpg`, and `InRelease` are never filtered | — |
| Effect | An excluded index is not mirrored, and is purged from the target as an orphan | — |

## Object sizes

Release files and stored indices are read by `read_capped()` in `repo/compress.py`, which reads at
most one byte past a cap, so each is refused before it is verified or buffered in full. Upload
control data is capped by `_control_of()` in `repo/debfile.py`, which decodes the `control.tar`
member under a limit and walks it as a stream. "Checked" means digest verification is on.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Root Release files | `Release`, `InRelease`, and `Release.gpg` are at most `MAX_RELEASE_BYTES` (16 MiB): no signed file records their size. | → `ReleaseReadException` (Read) |
| Per-arch Release | At most the `Size` the checked root Release records, else `MAX_RELEASE_BYTES`. | Larger than recorded → `ChecksumException` (Distrustful); past the cap → `ReleaseReadException` (Read) |
| Stored index | At most the largest `Size` a checked root or per-arch Release records, else `MAX_INDICES_BYTES` (256 MiB), the decoded cap → [Index decoding](#index-decoding). | Larger than recorded → `ChecksumException` (Distrustful); past the cap → `PackagesException` (Read) |
| Upload control data | The compressed `control.tar` member and its `control` file are each at most `MAX_CONTROL_BYTES` (4 MiB). | → `DebFileException` (Read) |
| Upload control tar | The `control.tar` decodes to at most `MAX_CONTROL_TAR_BYTES` (64 MiB), which also bounds its member count; it is uncompressed, `.gz`, `.xz`, `.bz2`, `.lzma`, or `.zst`, the last decoded by the `zstd` binary. | Past the limit, another compression, or a stream that does not decode → `DebFileException` (Read) |

## Index decoding

Implemented by `Packages.load()` and `Packages._stanzas()` in `repo/packages.py`, which `Sources`
inherits, through the decoders in `repo/compress.py`.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Decompressed size | Decoding stops as soon as the output passes `MAX_INDICES_BYTES` (256 MiB), so a compression bomb is never inflated in full. | → `PackagesException` (Read) |
| Stream integrity | A compressed index decodes to its end-of-stream marker; truncated or corrupt data is refused. | → `PackagesException` (Read) |
| LZMA dictionary | An `.xz` or `.lzma` stream asking for a dictionary over 128 MiB is refused before it is allocated. | → `PackagesException` (Read) |
| Stream padding | Any run of NUL bytes between or after compressed streams is skipped. | — |
| Encoding | The decoded index is UTF-8, as Debian Policy 5.1 requires of control data. | → `PackagesException` (Read) |
| Stanza separation | A blank or whitespace-only line ends a stanza, as Debian Policy 5.1 allows. | — |
| Line ends | CRLF line ends are read as LF. | — |

## Index loading

Implemented by `Component.load()` in `repo/component.py`, called through `Repo.update()`.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Missing index | An arch whose Release lists no `Packages*` file present in storage is skipped, so `insert` starts its index; only the strict `sync` source refuses it. | Strict → `ComponentException` (Read) |
| Unreadable index | An index that exists but does not decode, passes the size cap, or holds a bad `Sources` stanza, or a per-arch Release storage fails to read, stops `insert`, `remove`, and `check --fix`, which would replace that index or delete what it lists. | → `ComponentException` (Read) |
| Skipped unreadable index | `list`, `dump`, `stats`, `check` without `--fix`, and the `sync` target, which plans from the source, skip an unreadable index with a warning. | Logged |
| Sources index | A mirrored `Sources` index is never written, so every non-strict load skips an unreadable one. | Logged |
| Index digest | A digest mismatch is never skipped; the `sync` target resets instead → [Index selection](#index-selection). | → `ChecksumException` (Distrustful) |

## Package stanzas

Parsed and written by `Package.parse()` and `Package.serialize()` through `debian.deb822`.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Mandatory | `Package`, `Filename`, and `Size` are present. | → `PackageException` (Read) |
| Multi-line value | Continuation lines are kept, so a rewritten index carries `Description` and `Conffiles` unchanged. | — |
| Encoding | Values are written verbatim as UTF-8; no MIME encoding or rewrapping. | — |
| Single-line field | `Package`, `Filename`, `Size`, and every digest field hold no line break. | → `PackageException` (Read) |

## Release fields

Parsed by `Release.parse()` through the email parser and written by `Release.serialize()` through
`debian.deb822`; the hash blocks → [CRYPTO.md](CRYPTO.md#checksums).

| Rule | Constraint | Failure |
| -- | -- | -- |
| Multi-line value | Continuation lines are kept, so a folded `Description` or a wrapped `Architectures` survives a rewrite. | — |
| Encoding | Values are written verbatim as UTF-8; no MIME encoding, date reformatting, or rewrapping. | — |

## Sources stanzas

Parsed by `Sources._expand_stanza()`. Each stanza names several payload files under one `Directory`;
every file becomes one record keyed by `<directory>/<name>`.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Directory | Present, not `.`, and passes the unsafe-key rule below. | → `PackagesException` (Read) |
| File name | A bare name: no `/` segment, and the joined key passes the unsafe-key rule. | → `PackagesException` (Read) |
| Size | Agrees across every hash block that lists the file. | → `PackagesException` (Read) |
| Digests | At least one algorithm normalization keeps; `Files` alone is MD5, which it drops → [CRYPTO.md](CRYPTO.md#checksums). | → `PackagesException` (Read) |
| Shared payload | A file named by several stanzas is kept once, last stanza wins; disagreeing digests are logged. | — |
| Mutation | `insert` is refused; `remove` matches nothing; `save` writes nothing. | `insert` → `PackagesException` (Read) |
| Container | The source architecture resolves to `Sources` on the read and the write path alike, through `container_for()` in `repo/component.py`: `Packages.setup` names its file from the layout, so a binary container would write a binary index into the canonical `Sources` path. | → `PackagesException` (Read) on the refused insert |
| Insert architecture | A package whose control field names the source architecture is refused before the transfer, as a `.changes` is when any of its `Architecture` tokens names it. | → `ActionException` (Generic) |

## Filename safety

Package metadata names storage objects, so a `Filename:` field copied from a source repo picks the
key a write lands on, and a root `Release` digest block names the index keys a mirror fetches. The
checks below constrain both; unsafe names fail before any storage mutation.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Unsafe key | txstore's `is_unsafe_path()`, shared by every caller below → [txstore VALIDATION.md](https://github.com/vinklat/txstore/blob/v0.1.2/VALIDATION.md#filename-safety). | — |
| `Packages.remove()` | Matched package filename passes the unsafe-key rule. | → `PackagesException` (Read) |
| `RepoSync` index set | Release-listed index key and path pass the unsafe-key rule. | Entry dropped from the index set and logged |
| `Packages` index parse | Stanza `Filename`, POSIX-normalized, is not `.` and passes the unsafe-key rule. | Stanza dropped from the container and logged |
| `Sources` payload | Stanza `Directory` and the joined payload key pass the unsafe-key rule. | → `PackagesException` (Read) |
| Insert `Package` name | The `.deb` control `Package` field is present and a Debian Policy name, `^[a-z0-9][a-z0-9+.-]+$`, checked before the transfer. | → `DebFileException` (Read) |
| Insert source name | The control `Source` field, a trailing ` (<version>)` stripped, else the `Package` field, is a Debian Policy name as above, checked before the transfer. | → `DebFileException` (Read) |
| Insert pool path | The Debian source pool key `pool/[<component>/]<prefix>/<source>/<upload file name>` from `pool_path()` in `repo/params.py` passes the unsafe-key rule, checked before the transfer. | → `DebFileException` (Read) |
| Insert pool object | A pool key the target already holds is replaced only by identical bytes: every dist of the root and every arch index may list it, and their records name the bytes it holds. An identical upload passes. | Different bytes → `ActionException` (Generic), the staged transfer discarded |
| Insert pool prefix | `lib` and the next letter for a source name longer than 3 starting with `lib`, else its first letter. | — |
| `.changes` file list | Every `Checksums-Sha256` entry is a bare file name: no `/` segment, and it passes the unsafe-key rule. | → `ChangesException` (Read) |
| Storage paths | Confinement, reserved names, and footmark reclaim → [txstore VALIDATION.md](https://github.com/vinklat/txstore/blob/v0.1.2/VALIDATION.md#filename-safety). | → `StorageException` (Generic) |

## Control file encoding

Implemented by `DebFile.get_package()`, `Changes.parse()`, and `Release.parse()`. Debian Policy 5.1
requires UTF-8 control data; indices → [Index decoding](#index-decoding).

| Rule | Constraint | Failure |
| -- | -- | -- |
| `.deb` control file | UTF-8, checked before the transfer. | → `DebFileException` (Read) |
| `.changes` file | UTF-8; a clearsigned one is checked on its signed body. | → `ChangesException` (Read) |
| `Release` | UTF-8, else read as latin-1, as some repositories publish it; an `InRelease` body is decoded the same way. | — |

## Log levels

Flag and variable → [CONFIG.md](CONFIG.md#global-flags).

| Rule | Constraint | Failure |
| -- | -- | -- |
| Accepted values | `CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG`; case-insensitive. | Other → usage error (Usage) |

## History retention

Implemented by `positive_int_type()` and `history_max_age_type()` in `argtypes.py`; flags →
[CONFIG.md](CONFIG.md#global-flags).

| Rule | Constraint | Failure |
| -- | -- | -- |
| Integer | Plain ASCII decimal, optional sign, surrounding whitespace ignored; `1_0` and non-ASCII digits are refused. | → `ArgumentTypeError` (Usage) |
| `--history-keep` | 1 or greater. | Below 1 → `ArgumentTypeError` (Usage) |
| `--history-max-age` | 0 to 36500 days, txstore's bound; meaning of `0` → [CONFIG.md](CONFIG.md#global-flags). | Outside → `ArgumentTypeError` (Usage) |

## Environment values

Implemented by `_env.py` and `_options.py`; resolution → [CONFIG.md](CONFIG.md#resolution).

| Rule | Constraint | Failure |
| -- | -- | -- |
| Switch true | `1`, `true`, `yes`, `on`; case-insensitive, surrounding whitespace ignored. | — |
| Switch false | `0`, `false`, `no`, `off`; case-insensitive, surrounding whitespace ignored. | — |
| Switch other | Never read as true, so a typo cannot disable verification. | → usage error naming the variable (Usage) |
| Valued | Converted and checked as the flag's value would be. | → usage error naming the variable (Usage) |
| Prefix | Only `DEBKIT_` names are read; another name is a programming error. | Other name → uncaught `ValueError` |

## Release dates

Implemented by `parse_release_date()` and `Release.date()` in `repo/release.py` for the root Release
`Date` and `Valid-Until` fields. Both raise `ValueError`, which never reaches the CLI: the freshness
check maps it → [CRYPTO.md](CRYPTO.md#release-freshness).

| Rule | Constraint | Failure |
| -- | -- | -- |
| Format | RFC 2822 date as `email.utils.parsedate_to_datetime` reads it, e.g. `Sat, 10 Jun 2023 08:47:46 UTC`. | Rejected by the parser → [CRYPTO.md](CRYPTO.md#release-freshness) |
| Zone | Last token `UTC`, `GMT`, `UT`, `Z`, or a numeric `+hhmm` / `-hhmm` offset; `-0000` is read as UTC. A named zone such as `CEST`, a missing zone, or trailing text is refused, since it would otherwise read silently as UTC. | Rejected by the parser → [CRYPTO.md](CRYPTO.md#release-freshness) |

## Package pattern matching

Implemented by `pkg_pattern_type()` in `argtypes.py` for `--pkg-pattern` and `DEBKIT_PKG_PATTERN`,
before the target is read; `Packages.remove()` matches the compiled patterns; flag →
[CONFIG.md](CONFIG.md#remove).

| Rule | Constraint | Failure |
| -- | -- | -- |
| Shape | Non-empty JSON object mapping package field names to regex strings; any field, no allow-list. | Invalid JSON, another JSON type, `{}`, or a non-string value → `ArgumentTypeError` (Usage) |
| Match | Every supplied field pattern matches, using `re.search` without anchoring. | — |
| Length | At most 500 characters per pattern; checked before compilation. | Longer → `ArgumentTypeError` (Usage) |
| Backtracking | Patterns are trusted operator input and are not checked for catastrophic backtracking; anyone who sets the pattern can already remove every package. | — |
| Compiles | Every pattern compiles with `re.compile`. | `re.error` → `ArgumentTypeError` (Usage) |

## Architectures

Used by `ActionSync` and `insert`; flag → [CONFIG.md](CONFIG.md#sync). The token rule is
`is_valid_arch()` in `repo/params.py`: a token names the `binary-<arch>/` directory of a storage
path.

| Rule | Constraint | Failure |
| -- | -- | -- |
| Value | Comma-separated string; split on commas, with no trimming or case folding; default → [CONFIG.md](CONFIG.md#sync). | — |
| Empty token | Dropped, so `amd64,,arm64` selects two architectures; a value of commas alone selects none, and the Release tokens decide. | — |
| Empty string | No override is applied. | — |
| Token | Lower-case letters, digits, and `-`, not starting with `-`, e.g. `amd64`, `hurd-i386`; a flag token is checked before any storage is opened. | Flag token → `ActionException` (Generic) |
| Release token | A root Release `Architectures` or `Architecture` token follows the token rule. | Dropped and logged |
| No Release token | A root Release with neither `Architectures` nor a valid `Architecture` token selects the empty arch in a flat repo; a componented one selects each arch its digest block lists `<component>/binary-<arch>/Packages*` for, under the token rule, `all` skipped. | Bad token dropped and logged |
| Upload token | Every `.changes` `Architecture` token and the `.deb` control `Architecture` follow the token rule, checked before the transfer. | → `ActionException` (Generic) |
| Index slot | `Component.insert_pkg` refuses an arch that is neither empty nor a token, whatever its caller checked. | → `ComponentException` (Read) |
| Insert target | A `.deb` goes to its control `Architecture` alone; an `all` or missing one goes to every root Release architecture but `source`. The `.changes` field and its file order play no part. | — |
| No binary target | An `all` or arch-less `.deb` while the root Release lists no architecture but `source`, refused before its transfer. | → `ActionException` (Generic) |
| `source` | Selects the source pseudo-architecture: `<component>/source/Sources*` is parsed and every payload it lists joins the package set. The index is mirrored, never regenerated. | — |

## S3 canned ACLs

Implemented by `object_acl_type()` and `bucket_acl_type()` in `argtypes.py` over txstore's
`parse_object_acl()` and `parse_bucket_acl()`; values →
[txstore VALIDATION.md](https://github.com/vinklat/txstore/blob/v0.1.2/VALIDATION.md#s3-canned-acls).

| Rule | Constraint | Failure |
| -- | -- | -- |
| `--s3-default-acl` | An object canned ACL, or `none` to send no ACL. | Other → `ArgumentTypeError` (Usage) |
| `--s3-bucket-acl` | A bucket canned ACL. | Other → `ArgumentTypeError` (Usage) |

## Maintenance

Add the rule here with its failure notation, add success and failure tests per
[TESTING.md](TESTING.md#required-coverage), and add a new class to
[EXCEPTIONS.md](EXCEPTIONS.md#domain-exceptions) when one is introduced. A rule txstore owns is
linked, never restated.
