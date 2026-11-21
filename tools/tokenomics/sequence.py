"""Sequences for the run harness: idle time and resume.

A cache experiment needs more than one prompt in the same session, and a
known idle time between the prompts. A sequence file gives both:

    {
      "title": "What the sequence shows",
      "workload": "status-board-app",
      "variant": "variant",                    optional, relative to this file
      "tool": "claude",
      "model": "claude-sonnet-5-5",
      "effort": "medium",
      "steps": [
        {"label": "Call 1", "prompt": "prompts/call.md"},
        {"label": "Call 2", "prompt": "prompts/call.md", "idle_seconds": 0},
        {"label": "Call 3", "prompt": "prompts/call.md", "idle_seconds": 360}
      ]
    }

`run-baseline --sequence FILE` makes a clean copy of the workload, as for a
baseline run (with the variant files, if any). Then it runs the steps in one
Claude Code session, with the checked-in run settings of the baseline:

    step 1      claude -p "$(cat prompt.md)" --output-format json ...
    step 2..n   wait idle_seconds, then the same command with
                --resume <session ID>, so that the step continues the session

The idle time starts when the step before ends. The harness sends no request
while it waits. `--idle SECONDS` changes the idle time of the last step, for
example to wait longer than the one-hour lifetime of the main conversation.

After the last step, the harness makes a reduced copy of the session (every
lane, no text, no paths) and writes a record to <runs dir>/<run ID>.record/.
Read the session with `tokenomics session --requests`. A sequence adds no row
to the results log: it is a cache experiment, not a baseline attempt with a
pass or a fail.

This version runs Claude Code only.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import time

from . import variant as variant_mod

TOOLS = ("claude",)
SETUP = {"status-board-app": "python-venv", "status-board-infra": "terraform-init"}


def load_sequence(path: str | pathlib.Path, idle: float | None = None) -> dict:
    """Read and check a sequence file. idle: a new idle time for the last step."""
    path = pathlib.Path(path).expanduser()
    data = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    for key in ("title", "workload", "tool", "model", "effort", "steps"):
        if not data.get(key):
            raise ValueError(f"{path}: give {key!r}")
    if data["tool"] not in TOOLS:
        raise ValueError(f"{path}: tool {data['tool']!r} is not supported. "
                         f"Use one of: {', '.join(TOOLS)}")
    if data["workload"] not in SETUP:
        raise ValueError(f"{path}: unknown workload {data['workload']!r}. "
                         f"Use one of: {', '.join(SETUP)}")
    steps = []
    for number, step in enumerate(data["steps"], 1):
        if not step.get("prompt"):
            raise ValueError(f"{path}: step {number} has no prompt")
        prompt = base / step["prompt"]
        if not prompt.is_file():
            raise FileNotFoundError(f"{path}: step {number}: no prompt file {prompt}")
        seconds = step.get("idle_seconds", 0)
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise ValueError(f"{path}: step {number}: idle_seconds must be a number >= 0")
        if number == 1 and seconds:
            raise ValueError(f"{path}: step 1 starts the session. It cannot have idle time.")
        steps.append({"number": number, "label": step.get("label", f"Step {number}"),
                      "prompt_path": prompt, "prompt_file": step["prompt"],
                      "idle_seconds": seconds})
    if idle is not None:
        if idle < 0:
            raise ValueError("--idle must be 0 or more seconds")
        if len(steps) < 2:
            raise ValueError("--idle changes the last step, and step 1 cannot have idle time")
        steps[-1]["idle_seconds"] = idle
    variant = None
    if data.get("variant"):
        variant = variant_mod.load_variant(base / data["variant"])
        if variant.workload != data["workload"]:
            raise ValueError(f"variant {variant.name} changes {variant.workload}, but the "
                             f"sequence uses {data['workload']}")
    return {"path": path, "name": path.stem, "title": data["title"],
            "workload": data["workload"], "setup": SETUP[data["workload"]],
            "tool": data["tool"], "model": data["model"], "effort": data["effort"],
            "variant": variant, "steps": steps}


def step_command(base_argv: list[str], session_id: str | None) -> list[str]:
    """Step 1 starts a session. A later step resumes it."""
    return list(base_argv) + (["--resume", session_id] if session_id else [])


def total_idle(seq: dict) -> float:
    return sum(step["idle_seconds"] for step in seq["steps"])


def run_steps(seq: dict, command, copy_dir: pathlib.Path, env: dict, timeout: float,
              log=print, sleep=time.sleep, run=subprocess.run, clock=time.time) -> dict:
    """Run every step in one session. command(prompt_text) gives the argv of
    step 1 with no --resume. Return the session ID and one result per step."""
    session_id = None
    results = []
    last_end = None
    for step in seq["steps"]:
        if step["number"] > 1 and step["idle_seconds"]:
            log(f"   Idle for {fmt_seconds(step['idle_seconds'])}: no request "
                "(cache experiment)")
            sleep(step["idle_seconds"])
        prompt = step["prompt_path"].read_text(encoding="utf-8")
        argv = step_command(command(prompt), session_id)
        started = clock()
        idle_actual = None if last_end is None else started - last_end
        log(f"   Step {step['number']}: {step['label']}"
            + (" (resume)" if session_id else " (new session)"))
        timed_out = False
        try:
            proc = run(argv, cwd=copy_dir, env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=timeout)
            code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            code, stdout, stderr, timed_out = -1, "", "", True
        last_end = clock()
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            result = {}
        results.append({"step": step, "exit_code": code, "timed_out": timed_out,
                        "started": started, "ended": last_end, "idle_actual": idle_actual,
                        "result": result, "stderr": stderr})
        if code != 0 or not result.get("session_id"):
            raise RuntimeError(f"step {step['number']} failed (exit code {code}). "
                               + (stderr.strip()[:400] or "No JSON result."))
        if session_id and result["session_id"] != session_id:
            raise RuntimeError(f"step {step['number']} did not resume session {session_id}: "
                               f"it reported {result['session_id']}")
        session_id = result["session_id"]
    return {"session_id": session_id, "steps": results}


def fmt_seconds(seconds: float) -> str:
    """360 -> 6m 00s, 30 -> 30s."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def iso(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat(timespec="seconds")
