#!/usr/bin/env python3
"""Show, set, or remove the Codex footer (the status line of the Codex TUI).

    codex-footer.py show   [FILE]
        Print the footer lines. Exit 1 if there are none.
    codex-footer.py --set [ITEM[,ITEM...]] [--no-colors] [--dry-run] [FILE]
        Write the footer. With no items, write the default items.
    codex-footer.py remove [--ours] [--dry-run] [FILE]
        Remove the footer lines. With --ours, remove only the lines that
        --set wrote. Exit 1 if there is nothing to remove.

FILE is ~/.codex/config.toml if you do not give it. The footer is two keys in
the [tui] table: `status_line = [...]` and `status_line_use_colors = ...`.

--set writes the two keys. It makes the [tui] table if it is missing, and it
makes the file if it is missing. It replaces the footer keys that are there.
It writes `status_line_use_colors = true` (--colors, the default) or `false`
(--no-colors). Before it changes a file, it copies the file to
config.toml.before-tokenomics. If that backup is there and the file has
lines that --set wrote, it keeps the backup: the backup then holds the file
before the first --set. --set marks its lines with the comment
"# ai-tokenomics". If you change the footer in Codex with /statusline, the
mark can go away. Then `remove --ours` does not remove your footer.

The default items: model-with-reasoning, current-dir, context-remaining,
context-used, five-hour-limit, weekly-limit. To use other items, give them
with commas and no spaces, for example --set model-with-reasoning,git-branch.
Codex shows the item names in /statusline.

The script changes only the lines of the two footer keys, and a [tui] header
that it adds. All other bytes of the file stay the same: comments, other
[tui] keys, other tables, and the line endings (LF or CRLF). It uses the
standard library only, and no TOML library.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import sys
import tempfile

KEYS = ("status_line", "status_line_use_colors")
DEFAULT_ITEMS = ("model-with-reasoning", "current-dir", "context-remaining", "context-used",
                 "five-hour-limit", "weekly-limit")
MARK = "# ai-tokenomics"
BACKUP_SUFFIX = ".before-tokenomics"
TABLE = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(#.*)?$")
ARRAY_TABLE = re.compile(r"^\s*\[\[")
KEY = re.compile(r"""^\s*(?:"([^"]*)"|'([^']*)'|([A-Za-z0-9_-]+))\s*=""")
ROOT_TUI = re.compile(r"""^\s*(?:tui|"tui"|'tui')\s*[.=]""")
ITEM = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def depth_change(text: str) -> int:
    """The change in bracket depth on one line. Brackets in strings and in a
    comment do not count."""
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c == "#":
            break
        if c in "\"'":
            quote = c
            i += 1
            while i < len(text) and text[i] != quote:
                if quote == '"' and text[i] == "\\":
                    i += 1
                i += 1
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        i += 1
    return depth


def scan(lines: list[str]) -> tuple[int | None, list[list[int]], bool]:
    """Read the file structure.

    Returns the index of the [tui] header (None if there is no header), the
    footer keys of the [tui] table (each one is the list of its line
    indexes), and True if the root table sets `tui` with a dotted key or an
    inline table (this script cannot change that safely)."""
    header: int | None = None
    blocks: list[list[int]] = []
    root_tui = False
    table = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        if ARRAY_TABLE.match(line):
            table = "[[array]]"
        else:
            match = TABLE.match(line)
            if match:
                table = re.sub(r"\s+", "", match.group(1))
                if table == "tui" and header is None:
                    header = i
            elif table == "" and ROOT_TUI.match(line):
                root_tui = True
            elif table == "tui":
                key = KEY.match(line)
                name = key and next(g for g in key.groups() if g is not None)
                if name in KEYS:
                    block = [i]
                    depth = depth_change(line.split("=", 1)[1])
                    while depth > 0 and i + 1 < len(lines):
                        i += 1
                        block.append(i)
                        depth += depth_change(lines[i])
                    blocks.append(block)
        i += 1
    return header, blocks, root_tui


def is_ours(lines: list[str], block: list[int]) -> bool:
    return any(MARK in lines[i] for i in block)


def line_ending(lines: list[str]) -> str:
    """CRLF if the first line ends with CRLF, else LF."""
    return "\r\n" if lines and lines[0].endswith("\r\n") else "\n"


def footer_text(items: list[str], colors: bool) -> list[str]:
    """The two footer lines, without line endings."""
    array = ", ".join(f'"{item}"' for item in items)
    return [f"status_line = [{array}] {MARK}",
            f"status_line_use_colors = {'true' if colors else 'false'} {MARK}"]


def read_lines(path: pathlib.Path) -> list[str]:
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read().splitlines(keepends=True)


def write_lines(path: pathlib.Path, lines: list[str]) -> None:
    """Write the file in one step: a temporary file, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write("".join(lines))
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def with_ending(line: str, eol: str) -> str:
    return line if line.endswith(("\n", "\r")) else line + eol


def set_footer(lines: list[str], items: list[str], colors: bool) -> list[str]:
    """Return the new lines of the file, with the footer in [tui]."""
    header, blocks, root_tui = scan(lines)
    if root_tui:
        raise SystemExit("error: the file sets `tui` with a dotted key or an inline table. "
                         "The file is not changed. Add the footer with /statusline in Codex.")
    eol = line_ending(lines)
    new = [text + eol for text in footer_text(items, colors)]
    remove = {i for block in blocks for i in block}
    if blocks:
        at = blocks[0][0]  # put the footer where the old footer was
    elif header is not None:
        at = header + 1  # put the footer directly below the [tui] header
    else:
        at = None
    result: list[str] = []
    for i, line in enumerate(lines):
        if i == at:
            if result:
                result[-1] = with_ending(result[-1], eol)
            result += new
        if i not in remove:
            result.append(line)
    if at is not None and at >= len(lines):
        if result:
            result[-1] = with_ending(result[-1], eol)
        result += new
    if at is None:
        if result:
            result[-1] = with_ending(result[-1], eol)
            if result[-1].strip():
                result.append(eol)
        result += [f"[tui] {MARK}{eol}"] + new
    return result


def remove_footer(lines: list[str], ours: bool) -> set[int]:
    """Return the indexes of the lines to remove."""
    header, blocks, _ = scan(lines)
    if ours:
        blocks = [block for block in blocks if is_ours(lines, block)]
    remove = {i for block in blocks for i in block}
    # A [tui] header that --set added: remove it too if the table is now empty.
    if ours and remove and header is not None and MARK in lines[header]:
        end = header + 1
        while end < len(lines) and not TABLE.match(lines[end]) \
                and not ARRAY_TABLE.match(lines[end]):
            end += 1
        rest = [i for i in range(header + 1, end) if i not in remove]
        if all(not lines[i].strip() for i in rest):
            remove.add(header)
            if header > 0 and not lines[header - 1].strip() and end >= len(lines):
                remove.add(header - 1)
    return remove


def backup(path: pathlib.Path, lines: list[str]) -> str:
    """Copy the file before the change. Keep a backup of the earlier state if
    the file already has lines that --set wrote."""
    target = path.with_name(path.name + BACKUP_SUFFIX)
    if target.exists() and any(MARK in line for line in lines):
        return f"Kept the backup {target} (it holds the file before the first --set)."
    shutil.copy2(path, target)
    return f"Backup: {target}"


def parse(argv: list[str]) -> dict:
    opts = {"action": None, "items": None, "colors": True, "dry_run": False, "ours": False,
            "file": None}
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg in ("show", "remove") and opts["action"] is None:
            opts["action"] = arg
        elif arg == "--set" or arg.startswith("--set="):
            opts["action"] = "set"
            value = arg.split("=", 1)[1] if "=" in arg else None
            # The next argument is the item list, unless it looks like a file.
            if value is None and args and not args[0].startswith("-") \
                    and "/" not in args[0] and not args[0].endswith(".toml"):
                value = args.pop(0)
            opts["items"] = value
        elif arg == "--colors":
            opts["colors"] = True
        elif arg == "--no-colors":
            opts["colors"] = False
        elif arg == "--dry-run":
            opts["dry_run"] = True
        elif arg == "--ours":
            opts["ours"] = True
        elif arg in ("-h", "--help"):
            opts["action"] = "help"
        elif not arg.startswith("-") and opts["file"] is None:
            opts["file"] = arg
        else:
            raise ValueError(f"unknown argument: {arg}")
    return opts


def usage() -> str:
    """The first part of the docstring: the command lines."""
    return __doc__.strip().split("\n\nFILE")[0]


def main(argv: list[str]) -> int:
    try:
        opts = parse(argv)
    except ValueError as exc:
        print(f"error: {exc}\n\n{usage()}", file=sys.stderr)
        return 2
    if opts["action"] == "help":
        print(__doc__.strip())
        return 0
    if opts["action"] is None:
        print(usage(), file=sys.stderr)
        return 2
    path = pathlib.Path(opts["file"] or "~/.codex/config.toml").expanduser()
    lines = read_lines(path) if path.is_file() else []

    if opts["action"] == "show":
        _, blocks, _ = scan(lines)
        if not blocks:
            return 1
        for block in blocks:
            for i in block:
                print(lines[i].rstrip("\r\n"))
        return 0

    if opts["action"] == "set":
        items = opts["items"].split(",") if opts["items"] else list(DEFAULT_ITEMS)
        bad = [item for item in items if not ITEM.match(item)]
        if bad:
            print(f"error: not an item name: {', '.join(bad) or '(empty)'}. "
                  "Use names such as five-hour-limit, with commas and no spaces.",
                  file=sys.stderr)
            return 2
        result = set_footer(lines, items, opts["colors"])
        if result == lines:
            print(f"The Codex footer is already set in {path}. Nothing changed.")
            return 0
        if opts["dry_run"]:
            print(f"Dry run: would write these lines under [tui] in {path}:")
            for text in footer_text(items, opts["colors"]):
                print(f"  {text}")
            return 0
        notes = [backup(path, lines)] if path.is_file() else [f"Created {path}."]
        write_lines(path, result)
        print(f"Set the Codex footer ({len(items)} items) in [tui] of {path}.")
        for note in notes:
            print(note)
        return 0

    # remove
    remove = remove_footer(lines, opts["ours"])
    if not remove:
        _, blocks, _ = scan(lines)
        if opts["ours"] and blocks:
            print(f"The footer in {path} was not set by this toolkit. It is not changed.")
        return 1
    count = len(remove)
    plural = "s" if count != 1 else ""
    if opts["dry_run"]:
        print(f"Dry run: would remove {count} line{plural} from {path}:")
        for i in sorted(remove):
            print(f"  {lines[i].rstrip()}")
        return 0
    write_lines(path, [line for i, line in enumerate(lines) if i not in remove])
    print(f"Removed {count} footer line{plural} from [tui] in {path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
