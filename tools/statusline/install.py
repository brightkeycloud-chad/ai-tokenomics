#!/usr/bin/env python3
"""Install or remove the toolkit status line in a Claude Code settings file.

Usage:
    install.py [--settings FILE] [--dry-run] [--replace]
    install.py --remove [--settings FILE] [--dry-run] [--replace]

    --settings FILE   the settings file to change. The default is the user
                      settings: $CLAUDE_CONFIG_DIR/settings.json, or
                      ~/.claude/settings.json.
    --dry-run         print the new settings. Do not write the file.
    --replace         also replace or remove a status line that is not the
                      toolkit status line.
    --remove          remove the toolkit status line.

The script changes only the "statusLine" key. It keeps all other keys and
their order. If the file has a different status line, the script stops and
shows it, unless you give --replace. Make a backup copy of the file first:
the script does not make one.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import sys
import tempfile

SCRIPT = pathlib.Path(__file__).resolve().parent / "statusline.py"
MARKER = "tools/statusline/statusline.py"


def default_settings() -> pathlib.Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude"
    return pathlib.Path(base) / "settings.json"


def course_status_line(python: str = sys.executable, script: pathlib.Path = SCRIPT) -> dict:
    """The statusLine value: run the status line script with this Python."""
    return {"type": "command", "command": f"{shlex.quote(python)} {shlex.quote(str(script))}",
            "padding": 0}


def is_course(status_line) -> bool:
    return isinstance(status_line, dict) and MARKER in str(status_line.get("command", ""))


def read_settings(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text()
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {path} is not valid JSON ({exc}). The file is not changed.")
    if not isinstance(data, dict):
        raise SystemExit(f"error: {path} does not contain a JSON object. The file is not changed.")
    return data


def write_settings(path: pathlib.Path, data: dict) -> None:
    """Write the file in one step: a temporary file, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".settings.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def change(data: dict, remove: bool, replace: bool, new: dict) -> tuple[dict, str]:
    """Return the new settings and a message. Raise SystemExit to stop."""
    current = data.get("statusLine")
    if current is not None and not is_course(current) and not replace:
        shown = json.dumps(current)
        raise SystemExit("error: the settings already have a different status line:\n"
                         f"  {shown}\n"
                         "The file is not changed. Make a backup, then use --replace.")
    result = dict(data)
    if remove:
        if current is None:
            return result, "No status line in the settings. Nothing to remove."
        del result["statusLine"]
        return result, "Removed the status line."
    if current == new:
        return result, "The tokenomics status line is already installed."
    result["statusLine"] = new
    return result, "Installed the tokenomics status line."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install.py", description="Install or remove the toolkit status line.")
    parser.add_argument("--settings", type=pathlib.Path, default=None,
                        help="settings file (default: ~/.claude/settings.json)")
    parser.add_argument("--remove", action="store_true", help="remove the toolkit status line")
    parser.add_argument("--replace", action="store_true",
                        help="also replace or remove a status line that is not the toolkit one")
    parser.add_argument("--dry-run", action="store_true", help="print the new settings only")
    parser.add_argument("--python", default=sys.executable,
                        help="the Python command that runs the status line. Give a stable "
                             "path, for example /opt/homebrew/bin/python3, so that a Python "
                             "upgrade does not stop the status line (default: this Python)")
    args = parser.parse_args(argv)

    path = args.settings or default_settings()
    data = read_settings(path)
    result, message = change(data, args.remove, args.replace, course_status_line(args.python))
    if args.dry_run:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(f"Dry run: {message} {path} is not changed.", file=sys.stderr)
        return 0
    if result != data:
        write_settings(path, result)
    print(f"{message} Settings file: {path}")
    if not args.remove and "statusLine" in result:
        print(f"Command: {result['statusLine']['command']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
