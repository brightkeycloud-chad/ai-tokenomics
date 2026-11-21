"""Index every setup to the baseline.

`tokenomics index` reads the results log and groups the runs by task and
setup (tool, model, effort). For each group it gives:

    runs, passes      a pass is a run with tests_pass = yes and rules_followed = yes
    tokens/attempt    the median list-price cost of one run, in dollars
    cost/success      the cost-per-success formula, with the median cost and the pass rate:
                          tokens/attempt / pass rate
                        + failures per success * rework minutes * hourly rate / 60
                      This is the unattended view: nobody waits for the run.
    attended          the same, plus waiting: you wait for every
                      run, so the median wall time of one run is waiting time:
                        + wall minutes / pass rate * hourly rate / 60
    wall              the median wall-clock time of one run
    cache write/read  the median cache write and cache read tokens of one run
                      (`tokenomics index --cache`)

Then it divides the cost per success and the wall time of each setup by the
values of the baseline setup of the same tool and task. The index
of the baseline is 1.00.

The spread is the run-to-run spread of the baseline: the largest distance of
one passing baseline run from the median of the passing runs, as a share of
that median. The spread of the cost uses the list-price cost of each run; the
spread of the time uses the wall time of each run. A failed run counts in the
pass rate, not in the spread. An index inside 1 ± spread is a tie on that
measure.

A setup is a tool, a model, an effort level, and a variant. The
runs of a variant are a different setup from the runs with no variant. A different pass rate shows as "more reliable" or "less reliable".

Each tool is indexed to its own baseline. The cost is list-price dollars for
both tools: Claude Code gives an estimate at list price, and the toolkit
prices Codex tokens with the rate card. That is one currency, but it is not
what a subscription pays.

Rows with no usage (the agent did not start, for example a model that the
plan does not include) are not counted. The report lists them.

One configuration for each configuration group (see runconfig.py): the
index never puts runs of two configurations of a group together. A group is
a tool on the cloud, or the same tool on a local model. A local
run always has another configuration than a cloud run, so the two are
separate groups: the index compares them as separate setups, and each setup
has the runs of one configuration only. For each group, the index uses the
current configuration (the checked-in files now) when the log has runs with
it. Otherwise it uses the configuration of the newest run of that group, and
the report says that it is old. The runs with another configuration are not
counted. The report gives their number.

A local setup is indexed to the cloud baseline of its tool. Its
token cost is 0 (the results log records no token price for a local model),
so the rework and the waiting decide its cost per success.

`--brief` shows one short table for each task. The data chooses the view.
When a setup runs on a local model: the setup, the passes, the
token cost of one run, the time, and the cost per success attended and
unattended. Else: the passes, the cost per success,
the cost index, the time index, the verdict, and the spread of each
baseline (see `_print_index_compact` in cli.py).
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass

from .costmodel import Option, cost_per_success
from .local import is_local_model
from .runconfig import current_config, row_config
from .sweep import Setup, row_key

MIN_RUNS = 3  # fewer runs than this: an anecdote, not a finding


def _num(value) -> float | None:
    return None if value in ("", None) else float(value)


def passed(row: dict) -> bool:
    return row.get("tests_pass") == "yes" and row.get("rules_followed") == "yes"


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def spread(values: list[float]) -> float | None:
    """The largest distance of one value from the median, as a share of the median."""
    mid = median(values)
    if mid in (None, 0) or len(values) < 2:
        return None
    return max(abs(v - mid) for v in values) / mid


@dataclass
class SetupResult:
    task: str
    tool: str
    model: str
    effort: str
    label: str
    baseline: bool
    runs: int
    passes: int
    token_cost: float | None          # median list-price dollars of one run
    credits: float | None             # median Codex credits of one run
    wall_seconds: float | None        # median wall time of one run
    cost_per_success: float | None    # None when no run passed. No waiting: unattended
    cache_write: float | None = None  # median cache write tokens of one run
    cache_read: float | None = None   # median cache read tokens of one run
    variant: str = ""
    cost_per_success_attended: float | None = None  # plus the wall time as waiting
    local: bool = False               # a model on Ollama
    cost_index: float | None = None
    time_index: float | None = None
    verdict: str = ""

    @property
    def success_rate(self) -> float:
        return self.passes / self.runs if self.runs else 0.0

    def to_dict(self) -> dict:
        return asdict(self) | {"success_rate": self.success_rate}


def summarize_setup(task: str, setup: Setup, rows: list[dict], hourly_rate: float,
                    rework_minutes: float) -> SetupResult:
    costs = [c for c in (_num(r.get("cost_list_usd")) for r in rows) if c is not None]
    credits = [c for c in (_num(r.get("cost_credits")) for r in rows) if c is not None]
    walls = [w for w in (_num(r.get("wall_seconds")) for r in rows) if w is not None]
    passes = sum(1 for r in rows if passed(r))
    token_cost = median(costs)
    wall = median(walls)
    cps = attended = None
    if passes and token_cost is not None:
        option = Option(name=setup.model, token_cost=token_cost,
                        success_rate=passes / len(rows), rework_minutes=rework_minutes)
        cps = cost_per_success(option, hourly_rate).cost_per_success
        if wall is not None:
            waiting = Option(name=setup.model, token_cost=token_cost,
                             success_rate=passes / len(rows), rework_minutes=rework_minutes,
                             wait_minutes=wall / 60)
            attended = cost_per_success(waiting, hourly_rate).cost_per_success
    writes = [w for w in (_num(r.get("cache_write")) for r in rows) if w is not None]
    reads = [w for w in (_num(r.get("cache_read")) for r in rows) if w is not None]
    return SetupResult(task, setup.tool, setup.model, setup.effort, setup.label,
                       setup.baseline, len(rows), passes, token_cost, median(credits),
                       wall, cps, median(writes), median(reads), setup.variant,
                       attended, is_local_model(setup.model))


def verdict(result: SetupResult, base: SetupResult, cost_spread: float | None,
            time_spread: float | None) -> str:
    """Inside the spread of the baseline, call it a tie."""
    if result.baseline:
        return "baseline"
    if not result.passes:
        return "Never passed"
    parts = []
    if result.cost_index is not None and cost_spread is not None:
        if result.cost_index < 1 - cost_spread:
            parts.append("cheaper")
        elif result.cost_index > 1 + cost_spread:
            parts.append("costlier")
    if result.time_index is not None and time_spread is not None:
        if result.time_index < 1 - time_spread:
            parts.append("faster")
        elif result.time_index > 1 + time_spread:
            parts.append("slower")
    if result.success_rate > base.success_rate:
        parts.append("more reliable")
    elif result.success_rate < base.success_rate:
        parts.append("less reliable")
    text = ", ".join(parts) if parts else "tie"
    text = text[0].upper() + text[1:]
    return text + (" *" if result.runs < MIN_RUNS else "")


@dataclass
class ToolIndex:
    task: str
    tool: str
    baseline: SetupResult | None
    cost_spread: float | None
    time_spread: float | None
    results: list[SetupResult]
    config: str = ""          # the configuration of the cloud runs in this index
    current_config: str = ""  # the configuration that a cloud run uses now
    other_config: int = 0     # runs of these setups with another configuration: not counted
    local_config: str = ""          # the same for the local runs; "" for none
    local_current_config: str = ""


def config_group(tool: str, model: str | None) -> str:
    """The configuration group of a setup: 'claude' or 'codex' on the cloud,
    'claude:local' or 'codex:local' on a local model."""
    return f"{tool}:local" if is_local_model(model) else tool


def config_for(configs: dict[str, str], tool: str, model: str | None) -> str | None:
    """The chosen configuration of the group of this tool and model."""
    return configs.get(config_group(tool, model))


def choose_configs(rows: list[dict], setups: list[Setup], tasks: list[str]) -> dict[str, str]:
    """The one configuration to index for each configuration group (see
    config_group): the current one if the log has runs of these setups with
    it, else the one of the newest run. Two configurations never mix in one
    group, so they never mix in one setup."""
    keys = {s.key for s in setups}
    out = {}
    groups = sorted({config_group(s.tool, s.model) for s in setups})
    for group in groups:
        group_setups = [s for s in setups if config_group(s.tool, s.model) == group]
        group_keys = {s.key for s in group_setups} & keys
        group_rows = [r for r in rows if r["task"] in tasks and row_key(r) in group_keys]
        if not group_rows:
            continue
        tool = group_setups[0].tool
        models = [s.model for s in group_setups if s.baseline] + \
            [s.model for s in group_setups if not s.baseline]
        currents = [current_config(tool, t, m) for m in models for t in tasks]
        current = next((c for c in currents if any(row_config(r) == c for r in group_rows)),
                       None)
        if current is not None:
            out[group] = current
        else:
            newest = max(group_rows, key=lambda r: (r.get("date", ""), r.get("run_id", "")))
            out[group] = row_config(newest)
    return out


@dataclass
class TaskIndex:
    task: str
    rework_minutes: float
    tools: list[ToolIndex]
    other_state: int  # rows of this task with another workload tree or prompt than the baseline


def index_log(rows: list[dict], setups: list[Setup], tasks: list[str], hourly_rate: float,
              rework_for) -> tuple[list[TaskIndex], list[dict]]:
    """Index each setup to the baseline setup of its tool. `setups` has one
    baseline for each tool. Returns the task indexes and the rows with no usage."""
    no_usage = [r for r in rows if r.get("tool") in ("claude", "codex")
                and r.get("input") in ("", None)]
    used = [r for r in rows if r not in no_usage]
    configs = choose_configs(used, setups, tasks)
    out = []
    for task in tasks:
        all_task_rows = [r for r in used if r["task"] == task]
        task_rows = [r for r in all_task_rows
                     if config_for(configs, r["tool"], r["model"]) in (None, row_config(r))]
        tools = []
        other_state = 0
        for tool in [t for t in ("claude", "codex") if any(s.tool == t for s in setups)]:
            tool_setups = [s for s in setups if s.tool == tool]
            base_setup = next((s for s in tool_setups if s.baseline), None)
            results = []
            groups = {}
            for setup in tool_setups:
                group = [r for r in task_rows if row_key(r) == setup.key]
                if group:
                    groups[setup.key] = group
                    results.append(summarize_setup(task, setup, group, hourly_rate,
                                                   rework_for(task)))
            if not results:
                continue
            base = next((r for r in results if r.baseline), None)
            cost_spread = time_spread = None
            if base is not None:
                base_rows = groups[base_setup.key]
                passing = [r for r in base_rows if passed(r)]
                cost_spread = spread([c for c in (_num(r.get("cost_list_usd"))
                                                  for r in passing) if c is not None])
                time_spread = spread([w for w in (_num(r.get("wall_seconds"))
                                                  for r in passing) if w is not None])
                state = {(r["workload_tree"], r["prompt_sha"]) for r in base_rows}
                other_state += sum(1 for g in groups.values() for r in g
                                   if (r["workload_tree"], r["prompt_sha"]) not in state)
                for r in results:
                    if r.cost_per_success is not None and base.cost_per_success:
                        r.cost_index = r.cost_per_success / base.cost_per_success
                    if r.wall_seconds is not None and base.wall_seconds:
                        r.time_index = r.wall_seconds / base.wall_seconds
                    r.verdict = verdict(r, base, cost_spread, time_spread)
            keys = {s.key for s in tool_setups}
            other = sum(1 for r in all_task_rows if r["tool"] == tool and row_key(r) in keys
                        and row_config(r) != config_for(configs, tool, r["model"]))
            local_setup = next((s for s in tool_setups if is_local_model(s.model)), None)
            local_config = local_current = ""
            if local_setup is not None and configs.get(f"{tool}:local"):
                local_config = configs[f"{tool}:local"]
                local_current = current_config(tool, task, local_setup.model)
            tools.append(ToolIndex(task, tool, base, cost_spread, time_spread, results,
                                   configs.get(tool, ""),
                                   current_config(tool, task,
                                                  base_setup.model if base_setup else None),
                                   other, local_config, local_current))
        if tools:
            out.append(TaskIndex(task, rework_for(task), tools, other_state))
    return out, no_usage


def scenario(task_index: TaskIndex, hourly_rate: float, log_name: str, last_date: str) -> dict:
    """A scenario file for `tokenomics compare` with the measured
    medians. A setup that never passed has no cost per success, so the
    scenario leaves it out and names it in the note."""
    options, never = [], []
    for tool in task_index.tools:
        for r in tool.results:
            if not r.passes or r.token_cost is None:
                never.append(f"{r.model} · {r.effort}" + (f" · {r.variant}" if r.variant else ""))
                continue
            options.append({"name": f"{r.model} · {r.effort}"
                            + (f" · {r.variant}" if r.variant else "")
                            + (" (baseline)" if r.baseline else ""),
                            "token_cost": round(r.token_cost, 4),
                            "success_rate": f"{r.passes}/{r.runs}",
                            "runs": r.runs})
    note = (f"Measured: median list-price cost of one run and the pass rate, from {log_name} "
            f"(runs to {last_date}). List-price dollars, not what a subscription pays.")
    if never:
        note += " Never passed, so not here: " + ", ".join(never) + "."
    return {
        "title": f"{task_index.task}: measured setups",
        "note": note,
        "currency": "USD",
        "hourly_rate": hourly_rate,
        "defaults": {"rework_minutes": task_index.rework_minutes, "wait_minutes": 0},
        "options": options,
    }
