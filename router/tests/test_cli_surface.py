"""Characterization test for the whole CLI surface.

``build_parser`` is the single declaration of every command, flag, default and
handler binding. Splitting it into registrars must not change one observable
detail, so the full surface is snapshotted and compared byte for byte.

Regenerate the snapshot only for an intentional CLI change:

    python router/tests/test_cli_surface.py

This reads a few argparse internals (``_actions``, ``_defaults``) on purpose:
they are the only place where handler bindings and subparser structure can be
inspected without invoking every command.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SNAPSHOT = Path(__file__).with_name("data") / "cli-surface.json"


def _type_name(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "__name__", None) or repr(value)


def _default_name(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_default_name(item) for item in value]
    return _type_name(value)


def _action_surface(action: argparse.Action) -> dict[str, Any]:
    return {
        "class": type(action).__name__,
        "option_strings": list(action.option_strings),
        "dest": action.dest,
        "nargs": action.nargs,
        "const": _default_name(action.const),
        "default": _default_name(action.default),
        "type": _type_name(action.type),
        "choices": (
            [_default_name(item) for item in action.choices]
            if action.choices is not None
            and not isinstance(action.choices, dict)
            else None
        ),
        "required": action.required,
        "help": action.help,
        "metavar": action.metavar,
    }


def _parser_surface(parser: argparse.ArgumentParser) -> dict[str, Any]:
    surface: dict[str, Any] = {
        "prog": parser.prog,
        "defaults": {
            key: getattr(value, "__name__", _default_name(value))
            for key, value in sorted(parser._defaults.items())
        },
        "arguments": [],
        "subcommands": {},
    }
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            surface["subcommands"] = {
                "dest": action.dest,
                "required": action.required,
                # Declaration order is part of the help output.
                "order": [choice.dest for choice in action._choices_actions],
                "help": {
                    choice.dest: choice.help for choice in action._choices_actions
                },
                "parsers": {
                    name: _parser_surface(child)
                    for name, child in action.choices.items()
                },
            }
            continue
        surface["arguments"].append(_action_surface(action))
    return surface


def cli_surface() -> dict[str, Any]:
    from baldr_router.cli import build_parser

    return _parser_surface(build_parser())


def test_cli_surface_is_unchanged() -> None:
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert cli_surface() == expected


def test_snapshot_covers_the_documented_command_set() -> None:
    surface = cli_surface()
    commands = surface["subcommands"]["order"]

    # Guards against a snapshot regenerated from an accidentally empty parser.
    assert len(commands) >= 39
    assert "facade" in commands
    assert surface["subcommands"]["parsers"]["facade"]["subcommands"]["order"] == [
        "contract",
        "setup",
        "status",
        "run",
    ]


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(
        json.dumps(cli_surface(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {SNAPSHOT}")
