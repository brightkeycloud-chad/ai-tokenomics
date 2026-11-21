#!/usr/bin/env python3
"""filter-test-output: a Claude Code PreToolUse hook for pytest.

Claude Code runs this hook before each Bash command (see .claude/settings.json).
The hook reads the tool call as JSON on stdin.

- If the command is a simple pytest run, the hook rewrites it. The new command
  runs the same pytest command through this script with --run. The script
  keeps only the failures, the error lines, and the summary line, and exits
  with the exit status of pytest. The hook approves the new command, because
  the permission rules apply to the command that the hook returns, and the
  original command is one that the project already allows.
- For all other commands, the hook prints nothing, and Claude Code continues
  as usual.

A simple pytest run is `./venv/bin/python -m pytest [ARGS]` or
`./venv/bin/pytest [ARGS]`, with an optional `cd DIR && ` before it, and an
optional `2>&1` and `| tail -N` or `| head -N` after it. The filter replaces
the tail or head: the agent asked for less output, and it gets the failures
and the summary.

The hook writes one JSON line to .claude/filter-test-output.log for each
rewrite and for each filtered run: the marker that proves that the hook ran.
The log has no command text and no output text, only the tool call ID,
counts, and the exit status. Git ignores *.log in this repository.

If the output has no pytest summary line (for example, pytest could not
start), the script prints the full output. It never hides a result that it
cannot read. Standard library only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys

LOG_NAME = "filter-test-output.log"

SIMPLE_TEST = re.compile(r"""
    ^(?P<cd>cd[ \t]+[^\s;&|<>`$()]+[ \t]*&&[ \t]*)?
    (?P<test>(?:\./)?venv/bin/(?:python3?[ \t]+-m[ \t]+pytest|pytest)
             (?:[ \t]+[^;&|<>`$()\n]*?)?)
    (?:[ \t]+2>&1)?
    (?:[ \t]*\|[ \t]*(?:tail|head)(?:[ \t]+-n)?[ \t]+-?\d+)?
    [ \t]*$
    """, re.X)

SECTION = re.compile(r"^={3,} (.+?) ={3,}$")
SUMMARY = re.compile(r"\b(\d+ (passed|failed|errors?|skipped|deselected|xfailed|xpassed)"
                     r"|no tests ran)\b.* in \d+(\.\d+)?s\b")
KEEP_SECTIONS = ("failures", "errors")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def write_marker(log: pathlib.Path, entry: dict) -> None:
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as out:
            out.write(json.dumps({"time": now(), **entry}) + "\n")
    except OSError:
        pass                     # a marker must never stop the test run


def filter_pytest(text: str) -> tuple[list[str], bool]:
    """Return the lines to keep, and True if the output had a pytest summary line."""
    kept: list[str] = []
    summary = None
    section = None
    short_header = None
    for line in text.splitlines():
        if SUMMARY.search(line) and (SECTION.match(line) or not line.startswith(" ")):
            summary = line
            section = None
            continue
        match = SECTION.match(line)
        if match:
            section = match.group(1).strip().lower()
            if section in KEEP_SECTIONS:
                kept.append(line)
            elif section == "short test summary info":
                short_header = line
            continue
        if section in KEEP_SECTIONS:
            kept.append(line)
        elif line.startswith(("FAILED ", "ERROR ", "ERROR:")):
            if section == "short test summary info" and short_header:
                kept.append(short_header)
                short_header = None
            kept.append(line)
    if summary is None:
        return text.splitlines(), False
    return kept + [summary], True


def run(argv: list[str]) -> int:
    """--run --log FILE --id TOOL_USE_ID -- COMMAND: run, filter, keep the exit status."""
    log = pathlib.Path(argv[argv.index("--log") + 1])
    tool_id = argv[argv.index("--id") + 1]
    command = argv[argv.index("--") + 1]
    proc = subprocess.run(["bash", "-c", command], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace")
    lines, found = filter_pytest(proc.stdout)
    total = len(proc.stdout.splitlines())
    if found:
        head = (f"[filter-test-output hook] Kept the failures, the error lines, and the "
                f"summary line: {len(lines)} of {total} lines. Exit status {proc.returncode}.")
    else:
        head = ("[filter-test-output hook] No pytest summary line found, so the full "
                f"output follows. Exit status {proc.returncode}.")
    out = head + "\n" + "\n".join(lines) + "\n"
    sys.stdout.write(out)
    write_marker(log, {"event": "filtered", "tool_use_id": tool_id,
                       "exit": proc.returncode, "lines_in": total, "lines_out": len(lines),
                       "bytes_in": len(proc.stdout.encode()), "bytes_out": len(out.encode()),
                       "summary_found": found})
    return proc.returncode


def rewrite(event: dict) -> dict | None:
    """The hook output for one PreToolUse event, or None to change nothing."""
    if event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input") or {}
    command = (tool_input.get("command") or "").strip()
    project = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd()
    log = pathlib.Path(project) / ".claude" / LOG_NAME
    tool_id = event.get("tool_use_id", "")
    match = SIMPLE_TEST.match(command)
    if not match:
        if "pytest" in command:
            write_marker(log, {"event": "skip", "tool_use_id": tool_id,
                               "reason": "not a simple pytest command"})
        return None
    inner = (match.group("cd") or "") + match.group("test").strip()
    new_command = shlex.join([sys.executable, str(pathlib.Path(__file__).resolve()),
                              "--run", "--log", str(log), "--id", tool_id, "--", inner])
    write_marker(log, {"event": "rewrite", "tool_use_id": tool_id})
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "filter-test-output: a pytest run that the project "
                                    "allows, with the passing tests filtered out",
        "updatedInput": {**tool_input, "command": new_command},
    }}


def main(argv: list[str]) -> int:
    if argv[:1] == ["--run"]:
        return run(argv)
    try:
        out = rewrite(json.load(sys.stdin))
    except Exception:            # a broken hook must not block the agent
        return 0
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
