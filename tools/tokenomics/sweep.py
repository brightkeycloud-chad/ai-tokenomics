"""Sweeps for the run harness: many setups, many repetitions.

A setup is one tool, one model, and one effort level. A sweep runs one or more
tasks with each setup, until the results log has a number of runs of each.

You give the setups in one of two ways:

    run-baseline app-post-incident --tool claude \\
        --model claude-haiku-5-5,claude-opus-5-5 --effort medium --reps 3
                                         every model with every effort level

    run-baseline --sweep sweep.json      the setups of a checked-in sweep file

A sweep file is JSON:

    {
      "title": "What the sweep compares",
      "tasks": ["app-post-incident", "infra-5xx-alarm"],
      "repetitions": 3,
      "hourly_rate": 90,
      "rework_minutes": {"app-post-incident": 10, "infra-5xx-alarm": 30},
      "setups": [
        {"tool": "claude", "model": "claude-sonnet-5-5", "effort": "medium",
         "label": "Workhorse", "baseline": true},
        {"tool": "claude", "model": "claude-haiku-5-5", "effort": "medium",
         "label": "Small"}
      ]
    }

A setup can also name a variant (see variant.py): files that the
harness puts over the copy of the workload, for example a bloated AGENTS.md.
The path is relative to the sweep file:

        {"tool": "claude", "model": "claude-sonnet-5-5", "effort": "medium",
         "variant": "variants/bloated-agents-md", "label": "Bloated AGENTS.md"}

A setup with no variant uses the workload as it is.

Each tool has exactly one baseline setup. `tokenomics index` divides the
medians of every setup by the medians of the baseline of the same tool and
task. "hourly_rate" and "rework_minutes" are the inputs of cost per success
(see costmodel.py).

"repetitions" is a target, not a count to add: a sweep makes only the runs
that the log does not have yet. Only the runs with the current configuration
of the tool count (see runconfig.py): after a change of
baseline/config/claude-settings.json, the sweep makes every Claude Code run
again. You can stop a sweep and start it again. The
sweep runs in rounds: repetition 1 of every setup, then repetition 2, and so
on, so that a slow drift in the service affects every setup the same way.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

from .runconfig import current_config, row_config
from .scenario import parse_number
from .variant import load_variant

AGENT_TOOLS = ("claude", "codex")


@dataclass(frozen=True)
class Setup:
    tool: str
    model: str
    effort: str
    label: str = ""
    baseline: bool = False
    variant: str = ""        # the variant name in the results log; "" for none
    variant_dir: str = ""    # the variant directory, for the harness

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.tool, self.model, self.effort, self.variant)


@dataclass(frozen=True)
class Sweep:
    title: str
    tasks: tuple[str, ...]
    repetitions: int
    hourly_rate: float
    rework_minutes: dict
    setups: tuple[Setup, ...]
    path: str = ""

    def baseline(self, tool: str) -> Setup | None:
        return next((s for s in self.setups if s.tool == tool and s.baseline), None)

    def rework_for(self, task: str) -> float:
        return float(self.rework_minutes.get(task, self.rework_minutes.get("*", 0)))


def split_list(text: str | None) -> list[str]:
    """'a,b, c' -> ['a', 'b', 'c']. None or '' -> []."""
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def load_sweep(path: str | pathlib.Path) -> Sweep:
    file = pathlib.Path(path)
    data = json.loads(file.read_text(encoding="utf-8"))
    setups = []
    for item in data.get("setups", []):
        missing = [k for k in ("tool", "model", "effort") if not item.get(k)]
        if missing:
            raise ValueError(f"{file}: a setup has no {', '.join(missing)}: {item!r}")
        if item["tool"] not in AGENT_TOOLS:
            raise ValueError(f"{file}: tool {item['tool']!r} is not one of "
                             f"{', '.join(AGENT_TOOLS)}")
        variant_name = variant_dir = ""
        if item.get("variant"):
            variant = load_variant(file.parent / item["variant"])
            variant_name, variant_dir = variant.name, str(variant.dir)
        setups.append(Setup(item["tool"], item["model"], item["effort"],
                            item.get("label", ""), bool(item.get("baseline")),
                            variant_name, variant_dir))
    if not setups:
        raise ValueError(f"{file}: no setups")
    keys = [s.key for s in setups]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{file}: a setup is in the file two times")
    for tool in sorted({s.tool for s in setups}):
        count = sum(1 for s in setups if s.tool == tool and s.baseline)
        if count != 1:
            raise ValueError(f"{file}: give exactly one baseline setup for {tool} "
                             f"(found {count})")
    reps = int(data.get("repetitions", 1))
    if reps < 1:
        raise ValueError(f"{file}: repetitions must be 1 or more")
    rework = data.get("rework_minutes", 0)
    if not isinstance(rework, dict):
        rework = {"*": rework}
    return Sweep(
        title=data.get("title", file.stem),
        tasks=tuple(data.get("tasks", [])),
        repetitions=reps,
        hourly_rate=parse_number(data.get("hourly_rate", 0)),
        rework_minutes={k: parse_number(v) for k, v in rework.items()},
        setups=tuple(setups),
        path=str(file),
    )


def cross_setups(tool: str, models: list[str], efforts: list[str],
                 variant: str = "", variant_dir: str = "") -> list[Setup]:
    """Every model with every effort level, in the order given."""
    return [Setup(tool, m, e, variant=variant, variant_dir=variant_dir)
            for m in models for e in efforts]


def row_key(row: dict) -> tuple[str, str, str, str]:
    """The setup of a results-log row. Older logs have no
    variant column: their runs have no variant."""
    return (row["tool"], row["model"], row["effort"], row.get("variant") or "")


def filter_setups(setups, tool: str | None = None, models: list[str] | None = None,
                  efforts: list[str] | None = None) -> list[Setup]:
    return [s for s in setups
            if (not tool or s.tool == tool) and (not models or s.model in models)
            and (not efforts or s.effort in efforts)]


def count_runs(log_rows: list[dict], task: str, setup: Setup) -> int:
    """The runs of a setup in the log. Only the runs with the current
    configuration of the tool count (see runconfig.py)."""
    config = current_config(setup.tool, task, setup.model)
    return sum(1 for r in log_rows if r["task"] == task and row_key(r) == setup.key
               and row_config(r) == config)


@dataclass(frozen=True)
class PlanLine:
    task: str
    setup: Setup
    in_log: int
    to_run: int


def plan_sweep(tasks: list[str], setups: list[Setup], log_rows: list[dict],
               reps: int | None) -> list[PlanLine]:
    """What a sweep will do. With reps, fill each setup to that many runs in the
    log. With no reps, make one more run of each setup."""
    lines = []
    for task in tasks:
        for setup in setups:
            done = count_runs(log_rows, task, setup)
            to_run = 1 if reps is None else max(0, reps - done)
            lines.append(PlanLine(task, setup, done, to_run))
    return lines


def run_order(plan: list[PlanLine]) -> list[PlanLine]:
    """The runs one at a time, in rounds: the first missing run of every line,
    then the second, and so on."""
    order = []
    for round_ in range(max((p.to_run for p in plan), default=0)):
        order += [p for p in plan if p.to_run > round_]
    return order
