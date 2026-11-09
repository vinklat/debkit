"""Argparse options backed by ``DEBKIT_*`` environment variables.

`EnvArgumentParser.add_env_argument` registers an option with ``default=None`` and records its
variable, default, and converter; `EnvArgumentParser.add_env_variable` records a variable with no
flag. After argparse has parsed the command line, each value still None resolves from its
variable, then from its default → [CONFIG.md](CONFIG.md#resolution). Subparsers inherit the
class, so each parser resolves its own options.

A leaf over `_env`: it imports no other `debkit` module → [STRUCTURE.md](STRUCTURE.md#dependencies).
"""

from __future__ import annotations

from argparse import Action, ArgumentParser, ArgumentTypeError, BooleanOptionalAction, Namespace
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from debkit._env import env_bool, env_str


@dataclass(frozen=True)
class EnvOption:
    """One option resolved from the command line, then a variable, then a default.

    Attributes:
        dest: Namespace attribute.
        flag: Longest option string, named in error messages; None for a variable without a flag.
        env: ``DEBKIT_*`` variable read when the flag is absent.
        default: Value when neither flag nor variable is given.
        convert: Converter applied to a variable's string value; None keeps the string.
        choices: Allowed converted values; None allows any.
        switch: Boolean switch: the variable is read with `env_bool`.
        required: Fail when no value resolves.
    """

    dest: str
    flag: str | None
    env: str
    default: Any
    convert: Callable[[str], Any] | None
    choices: Sequence[Any] | None
    switch: bool
    required: bool


class EnvArgumentParser(ArgumentParser):
    """`ArgumentParser` whose options can fall back to ``DEBKIT_*`` variables."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._env_options: list[EnvOption] = []
        self._env_only_help: dict[str, str] = {}

    def add_env_argument(
        self,
        *flags: str,
        env: str,
        help: str,
        default: Any = None,
        required: bool = False,
        **kwargs: Any,
    ) -> Action:
        """Register an option that falls back to `env`, then to `default`.

        ``action=BooleanOptionalAction`` makes a switch with a ``--no-`` form; any other option
        takes a value converted by ``type``. Help names the variable and the default.

        Args:
            *flags: Option strings, as for `add_argument`.
            env: ``DEBKIT_*`` variable read when the flag is absent.
            help: Help text without the variable or default.
            default: Value when neither flag nor variable is given.
            required: Fail after resolution when no value resolves.
            **kwargs: Passed to `add_argument` (``type``, ``choices``, ``action``, ``dest``).

        Returns:
            The registered argparse action.
        """
        details = [f"env: {env}"]
        if default not in (None, ""):
            details.append(f"default: {default}")
        text = f"{help} [{'; '.join(details)}]".replace("%", "%%")
        action = self.add_argument(*flags, default=None, help=text, **kwargs)
        self._env_options.append(
            EnvOption(
                dest=action.dest,
                flag=max(action.option_strings, key=len),
                env=env,
                default=default,
                convert=kwargs.get("type"),
                choices=kwargs.get("choices"),
                switch=kwargs.get("action") is BooleanOptionalAction,
                required=required,
            )
        )
        return action

    def add_env_variable(self, env: str, *, dest: str, help: str, default: Any = None) -> None:
        """Register a value read only from `env`, then `default`; no flag sets it.

        For secrets: a command-line value is visible to other local users →
        [CONFIG.md](CONFIG.md#secrets). Help names the variable, never its value or default.

        Args:
            env: ``DEBKIT_*`` variable holding the value.
            dest: Namespace attribute.
            help: Help text without the variable.
            default: Value when the variable is unset or empty.
        """
        self._env_options.append(
            EnvOption(
                dest=dest,
                flag=None,
                env=env,
                default=default,
                convert=None,
                choices=None,
                switch=False,
                required=False,
            )
        )
        self._env_only_help[env] = help

    def format_help(self) -> str:
        """Return argparse help followed by the variables registered without a flag."""
        text = super().format_help()
        if not self._env_only_help:
            return text
        width = max(map(len, self._env_only_help))
        rows = [f"  {env:<{width}}  {line}" for env, line in self._env_only_help.items()]
        return "\n".join([text, "environment-only variables:", *rows, ""])

    def parse_known_args(  # type: ignore[override]
        self, args: Sequence[str] | None = None, namespace: Namespace | None = None
    ) -> tuple[Namespace, list[str]]:
        """Parse `args`, then resolve every option of this parser still None.

        Raises:
            SystemExit: Status 2 when a variable holds an invalid value or a required option
                resolves no value.
        """
        parsed, extras = super().parse_known_args(args, namespace)
        for option in self._env_options:
            if getattr(parsed, option.dest, None) is None:
                setattr(parsed, option.dest, self._resolve(option))
        return parsed, extras

    def _resolve(self, option: EnvOption) -> Any:
        """Return the variable's converted value, else the default."""
        try:
            value = self._read_env(option)
        except (ArgumentTypeError, ValueError, TypeError) as exc:
            self.error(f"{option.env}: {exc}")
        if value is not None:
            return value
        if option.required:
            self.error(f"{option.flag} or {option.env} is required")
        return option.default

    @staticmethod
    def _read_env(option: EnvOption) -> Any:
        """Read and convert `option`'s variable; None when unset or empty.

        Raises:
            ValueError: If the value is not in the option's choices, or the converter rejects it.
        """
        if option.switch:
            return env_bool(option.env)
        raw = env_str(option.env)
        if raw is None:
            return None
        value = option.convert(raw) if option.convert else raw
        if option.choices is not None and value not in option.choices:
            allowed = ", ".join(str(choice) for choice in option.choices)
            raise ValueError(f"invalid choice: {raw!r} (choose from {allowed})")
        return value
