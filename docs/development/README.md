# Developer guidance

Non-canonical explanations for contributors: behavior diagrams of the actions and the reasoning
behind the rules. Storage internals are explained in txstore's
[developer guidance](https://github.com/vinklat/txstore/blob/v0.1.1/docs/development/README.md).
Root contract documents remain authoritative; when a page here conflicts with a root contract,
update or remove the page. The reader-facing guides are in [docs/guides/](../guides/README.md).

## Page template

Every page opens with one paragraph: what the page explains, which contract sections own the
behavior, and the guide it complements. Sections follow in this order: `Code map`
(`Concern | Module | Notes`, only where a page spans several modules), `Activity diagram`,
`Sequence diagram`, `Details that matter` (bullets that link the owning contract instead of
restating it), `Testing notes` (which test files prove the behavior), and `Related contracts` ending
with a `Sibling pages:` bullet.

Diagram conventions: PlantUML only; sources, rendering, and checks →
[SETUP.md](../../SETUP.md#diagrams). A source is named `diagrams/<page>-<name>.puml` after its page,
and the page embeds it in its section with the heading as alt text. An activity diagram shows the
decision tree of one method; a sequence diagram shows one subcommand path from the action through
the repo and storage layers.

## Pages

| Page | Explains |
| -- | -- |
| [action_pkg_insert.md](action_pkg_insert.md) | `ActionPkg.insert`: moving `.deb` packages from the incoming storage into the repository in one transaction. |
| [action_pkg_remove.md](action_pkg_remove.md) | `ActionPkg.remove`: pattern-matched removal and the in-memory index rewrite. |
| [action_sync.md](action_sync.md) | `ActionSync.sync`: mirroring with the source-stability retry loop and checksum abort. |
| [repo_sync_indices.md](repo_sync_indices.md) | `Repo` index selection: the root Release as the manifest a sync copies and a check verifies against, its filters, and the digest skip. |
