# AI coding agent guide

Root contracts decide behavior; the implementation shows current state. Keep one canonical owner per
fact; link instead of copying.

## Source priority

1. The root contract that owns the domain below; `STYLE.md` prevails on style.
2. Implementation under `src/debkit/` when checking current behavior.
3. `docs/guides/` for user how-tos, `docs/development/` for contributor explanations and diagrams,
   and `ROADMAP.md` for planned work and known deviations. All are non-canonical.

Storage behavior (backends, transactions, footmarks, history, S3 ACLs, digests) is owned by the
[txstore](https://github.com/vinklat/txstore) contracts; debkit's contracts own only how debkit uses
it. Resolve drift by updating the owning contract and implementation together, not by adding a
second definition.

## Canonical sources

| File            | Owns                                                                                                           |
| --------------- | -------------------------------------------------------------------------------------------------------------- |
| `STYLE.md`      | Coding conventions, logging rules, layer responsibilities, performance rules; prevails on style                |
| `STRUCTURE.md`  | Layout, module ownership, internal import directions, the txstore boundary                                     |
| `STORAGE.md`    | How debkit constructs storage, records transactions, and uses history                                          |
| `VALIDATION.md` | Input constraints at the CLI, environment, URL, and `RepoParams` boundaries; path safety; sync index selection |
| `EXCEPTIONS.md` | Domain exception classes, owning modules, CLI exit groups and messages                                         |
| `CONFIG.md`     | CLI flags, `DEBKIT_*` variables, resolution order, defaults, secrets                                           |
| `CRYPTO.md`     | GPG verify and sign, Debian hash blocks, verification order, Release freshness                                 |
| `TESTING.md`    | Required coverage, harness rules, gates                                                                        |
| `SETUP.md`      | Install, run, test, quality gates, diagrams, dependency policy, release, upgrade notes                         |

## Task routing

Read only the relevant sections; locate code from [STRUCTURE.md](STRUCTURE.md#layout).

| Task                                | Primary                               | Also check                                                                     |
| ----------------------------------- | ------------------------------------- | ------------------------------------------------------------------------------ |
| Validator / input rule              | `VALIDATION.md`                       | `EXCEPTIONS.md` failure notation, `TESTING.md`                                 |
| Exception or exit group             | `EXCEPTIONS.md`                       | `src/debkit/cli.py`, `STYLE.md` logging                                        |
| CLI flag / `DEBKIT_*` variable      | `CONFIG.md`                           | `VALIDATION.md` environment values, `src/debkit/_options.py`                   |
| Storage construction / transactions | `STORAGE.md`                          | txstore contracts, `CONFIG.md` storage flags                                   |
| Repo logic (`Repo*`)                | `STYLE.md` layers → `STRUCTURE.md`    | `EXCEPTIONS.md`, `VALIDATION.md`, `CRYPTO.md`                                  |
| Action / subcommand                 | `STYLE.md` layers + `CONFIG.md`       | `EXCEPTIONS.md`, `STORAGE.md`, `TESTING.md`                                    |
| GPG / checksum / freshness          | `CRYPTO.md`                           | `EXCEPTIONS.md` Distrustful group, `VALIDATION.md`                             |
| Import boundary                     | `STRUCTURE.md` dependencies           | `tests/test_package.py`, `STYLE.md` layers                                     |
| Dependency change                   | `SETUP.md` dependency policy          | `STRUCTURE.md` dependencies, `tests/test_package.py`, `.github/debian-test.sh` |
| Bug fix                             | Owning contract + `EXCEPTIONS.md`     | `TESTING.md`                                                                   |
| Tests                               | `TESTING.md`                          | Contract for the behavior under test                                           |
| Contract / doc edit                 | Writing rules below → owning contract | `scripts/check_docs.py`, `SETUP.md` quality gates                              |
| Release                             | `SETUP.md` release                    | `.github/workflows/release.yml`, `ROADMAP.md` phases                           |
| Quality gate / CI change            | `SETUP.md` quality gates              | `.github/workflows/ci.yml`                                                     |
| Developer explanation / diagram     | `docs/development/`                   | Owning root contract, `SETUP.md` diagrams                                      |
| User how-to / future behavior       | `docs/guides/` / `ROADMAP.md`         | Owning root contract before implementing                                       |

## Agent workflow

1. Read only the owning contract sections from the routing table; open the module named in
   [STRUCTURE.md](STRUCTURE.md#layout).
2. Implement through the layer path in [STRUCTURE.md](STRUCTURE.md#dependencies). Write errors as
   `condition → ExceptionClass (group)` and link `EXCEPTIONS.md` instead of copying its table. A new
   flag → [CONFIG.md](CONFIG.md#maintenance); a new module →
   [STRUCTURE.md](STRUCTURE.md#maintenance); a storage need txstore's public names do not meet is
   a txstore change, never a private import.
3. Add the narrowest test that falsifies the change per [TESTING.md](TESTING.md#maintenance).
4. Run the [SETUP.md quality gates](SETUP.md#quality-gates); contract and `docs/` edits also run
   `uv run python scripts/check_docs.py`. CI runs the gates again.

## Contract writing rules

- One owner per fact. Link it as `→ FILE.md#anchor`; never copy its table or values.
- Preamble opens with `Owns: <domain>. Routing → AGENTS.md.`, optionally followed by one short
  orienting paragraph. No cross-document pointer chains.
- Errors: `condition → ExceptionClass (group)`; class and group must match `EXCEPTIONS.md`. Every
  mapped group exits 1, so the group is the discriminator, not the status.
- Prefer tables; one fact per row. Narrative cells stay under ~300 characters: split rows rather
  than pad. Enumerations (flag lists, caught-exception lists) are exempt.
- Tables use `| -- |` separator rows and unpadded cells; do not run a table aligner.
- Bare imperative rules; no RFC 2119 keywords. Headings are sentence case and unique per file.
- No TODOs or "current exception" notes in contracts; record them in
  [ROADMAP.md known deviations](ROADMAP.md#known-deviations) and link.
- Wrap prose at 100 columns; table rows are exempt. Prefer `→` shortcuts to sentences.
- After any contract or `docs/` edit run `uv run python scripts/check_docs.py`.
