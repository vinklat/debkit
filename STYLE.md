# Coding contract

Owns: coding conventions, logging rules, layer responsibilities, and performance rules; prevails on
style conflicts. Routing → [AGENTS.md](AGENTS.md#canonical-sources).

## Python baseline

- Use the [SETUP.md prerequisites](SETUP.md#prerequisites-and-install) Python floor. Apply PEP 8
  where Ruff is silent, strict type hints, and Google-style docstrings for public symbols.
- Ruff owns formatting, lint, and import order at line length 100: standard library, third-party
  (`txstore` among them), then `debkit`. Commands → [SETUP.md](SETUP.md#quality-gates).
- mypy runs in strict mode with `warn_unreachable` over `src/debkit`, `tests`, and `scripts`;
  `# type: ignore` names its error code.
- No commented-out code (Ruff `ERA`) and no `TODO`-style markers in code (Ruff `FIX`); record
  planned work and deviations in [ROADMAP.md](ROADMAP.md#known-deviations).
- A `# noqa` names an enabled Ruff rule the line breaks (`RUF100` flags any other); no pragmas for
  tools outside the gates.
- In `src/debkit`, import at module top; no import is deferred →
  [STRUCTURE.md](STRUCTURE.md#dependencies). Tests and scripts are exempt.
- Use snake-case modules; a leading underscore marks a module that only debkit imports.
- No side effects at import time: no argv parsing, environment reads, network, or filesystem
  writes. Expose such logic through functions callers and tests invoke, e.g. `argparser.parse_cli`.
- Prefer immutable inputs and results; do not mutate a caller's namespace or dict.

## Layers

| Layer          | Contract                                                                                                                                                                                                                                                     |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `cli.py`       | Parses args, configures logging, dispatches actions, maps exceptions to exit groups → [EXCEPTIONS.md](EXCEPTIONS.md#cli-exit-groups). The only `sys.exit` caller.                                                                                            |
| `argparser.py` | Builds the parser through `_options.EnvArgumentParser`; its type converters live in `argtypes.py` and raise `ArgumentTypeError` for bad values → [VALIDATION.md](VALIDATION.md#principles).                                                                  |
| `Action*`      | Orchestrates `Repo*` objects and output. Reaches storage only through `open_storage` and the `attach_storage_*` helpers; never instantiates a backend or implements repo logic. Wraps writes in `maybe_transaction` → [STORAGE.md](STORAGE.md#transactions). |
| `Repo*`        | Operates on a txstore `Storage` plus `RepoParams`, never on raw URLs or backend SDKs.                                                                                                                                                                        |

Import directions → [STRUCTURE.md](STRUCTURE.md#dependencies).

## Logging

- A module that logs does so through `logger = logging.getLogger(__name__)`, never the root
  logger (Ruff `LOG`), and installs `logging.NullHandler()` on it.
- Use `%` formatting in logger calls, never f-strings or `str.format` (Ruff `G`).
- Only `cli.main` configures handlers, through `_logging.configure_logging` →
  [CONFIG.md](CONFIG.md#global-flags).
- Keep messages factual and low-noise; secrets never appear → [CONFIG.md](CONFIG.md#secrets).
- Exception raising and chaining rules → [EXCEPTIONS.md](EXCEPTIONS.md#rules).

## Performance

| Concern       | Rule                                                                                            |
| ------------- | ----------------------------------------------------------------------------------------------- |
| Large objects | Stream with `get_stream`, `put_stream`, and `transfer`; never load a package just to digest it. |
| Digests       | Reuse txstore's `ChecksumOpen` for in-flight calculation → [CRYPTO.md](CRYPTO.md#checksums).    |
| Lookups       | Avoid repeated full scans when a stable key lookup is available.                                |

## Maintenance

Style rules change here and in `pyproject.toml` together. A change to what a layer may do also
updates the corresponding row in [STRUCTURE.md](STRUCTURE.md#dependencies).
