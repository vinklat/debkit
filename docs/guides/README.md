# Guides

These guides explain what `debkit` does and how to operate it. They are written for anyone running
the CLI: repository maintainers, operators, and pipeline authors. No knowledge of the code or of the
contract documents is assumed.

The guides are explanatory and non-canonical. When a guide and a contract document disagree, the
contract is right and the guide needs a fix. Each guide ends with a "Further reading" section that
names the contracts behind it.

## Conventions

- **Repositories** are named by an APT-style source string, `<url> <dist> [<component> ...]`, whose
  URL scheme (`fs://`, `s3://`, `https://`) selects the storage backend.
- **Flags and variables.** Every flag with an environment fallback is shown in its variable form in
  multi-line examples and its flag form in one-liners; the mapping is in
  [CONFIG.md](../../CONFIG.md).
- **Placeholders.** `XXX`, `YYY`, `ZZZ`, and `/path/to/.gnupg` stand for credentials and paths you
  supply. Hostnames are illustrative.

## Pages

| Guide | What you will learn |
| -- | -- |
| [examples.md](examples.md) | Inspecting, changing, mirroring, checking, and reverting a repository from the command line. |

## The contract documents

The repository root holds the contract documents. They are the single source of truth for every rule
and are written for implementers and automation, so they are terse and table-heavy. You do not need
them to use the tool; each guide links the ones behind it.

The full list of contracts and what each owns is
[AGENTS.md#canonical-sources](../../AGENTS.md#canonical-sources), the routing table that says which
contract owns which subject.

Two root documents are not contracts: [ROADMAP.md](../../ROADMAP.md) records planned work and known
deviations, and `AGENTS.md` itself routes rather than rules. Contributor-facing diagrams and
explanations are in [docs/development/](../development/README.md).
