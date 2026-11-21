"""Compare four ways to extend the agent.

Each extension has two meters:

    always-on    what it adds to every session, before the first prompt, used
                 or not: tool names and server instructions (MCP server), the
                 name and description (skill), the listing of every part
                 (plugin). A file that you paste adds nothing to the sessions
                 where you do not paste it.
    when used    what lands in the context when it runs: a tool schema and
                 each result (MCP server), the full SKILL.md and the command
                 output (skill), the whole file (direct file).

How the toolkit measures them:

    Always-on    `/context` in a prepared copy of each variant, with the same
                 `claude -p` arguments as a run (`run-baseline --context`). It
                 makes no model request. The always-on tokens are the sum of
                 the rows that an extension adds: "MCP tools" (loaded up
                 front), "MCP server instructions", and every skill whose
                 source is not "Built-in". "MCP tools (deferred)" is shown, but
                 it is not always-on: tool search loads a schema only when the
                 agent needs it. These are Claude Code estimates.

    When used    the largest context of the run (the prompt of its last
                 request) minus the context at its start with the extension
                 idle. For an MCP server, a skill, or a plugin, the start is
                 the prompt of the first request of the run: the system
                 prompt, the tools, the always-on tokens, and the question.
                 A pasted file is in the first request, so for the direct-file
                 variant the start is the median start of the other variants
                 without their always-on tokens. These are API counts.

`tokenomics extensions --sweep F --always-on` shows the first meter for each
variant. `tokenomics extensions --sweep F` shows the comparison: always-on
tokens, tokens when used, the median list-price cost of one run, the median
wall time, and whether the agent found the cause (the acceptance check).

The sweep file names the context directory: "context_dir": "context" (relative
to the sweep file). `run-baseline --sweep F --context` writes one file for each
variant there: <variant>.json.
"""
from __future__ import annotations

import json
import pathlib
import re
import statistics
from dataclasses import dataclass, field

TOKENS = re.compile(r"^~?\s*([\d.,]+)\s*([km]?)$", re.I)
ALWAYS_ON_ROWS = ("MCP tools", "MCP server instructions")
DEFERRED_ROW = "MCP tools (deferred)"


def parse_tokens(text: str) -> int | None:
    """'2.4k' -> 2400, '~70' -> 70, '625' -> 625, '1m' -> 1000000."""
    m = TOKENS.match(text.strip())
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    scale = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return round(value * scale)


def _table(lines: list[str], start: int) -> list[list[str]]:
    """The rows of the Markdown table that starts at or after line `start`."""
    rows, seen = [], False
    for line in lines[start:]:
        if line.startswith("|"):
            seen = True
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and not set(cells[0]) <= set("-: "):
                rows.append(cells)
        elif seen:
            break
    return rows[1:]  # without the header


def parse_context(markdown: str) -> dict:
    """The categories, the MCP tools, and the skills of a `/context` result.
    Built-in skills are counted, not listed. Memory file paths are not kept."""
    lines = markdown.splitlines()
    out = {"total": None, "window": None, "categories": {}, "mcp_tools": [], "skills": [],
           "builtin_skills": 0}
    for line in lines:
        m = re.match(r"\*\*Tokens:\*\*\s*([\d.,]+[km]?)\s*/\s*([\d.,]+[km]?)", line)
        if m:
            out["total"], out["window"] = parse_tokens(m.group(1)), parse_tokens(m.group(2))
    for i, line in enumerate(lines):
        heading = line.strip().lower()
        if heading.startswith("### estimated usage by category"):
            for cells in _table(lines, i + 1):
                if len(cells) >= 2:
                    out["categories"][cells[0]] = parse_tokens(cells[1])
        elif heading == "### mcp tools":
            for cells in _table(lines, i + 1):
                if len(cells) >= 3:
                    out["mcp_tools"].append([cells[0], cells[1], parse_tokens(cells[2])])
        elif heading == "### skills":
            for cells in _table(lines, i + 1):
                if len(cells) >= 3:
                    if cells[1] == "Built-in":
                        out["builtin_skills"] += 1
                    else:
                        out["skills"].append([cells[0], cells[1], parse_tokens(cells[2])])
    return out


def always_on(context: dict) -> dict:
    """The always-on tokens of one variant, and what makes them up."""
    cats = context.get("categories", {})
    parts = []
    for row in ALWAYS_ON_ROWS:
        if cats.get(row):
            parts.append((row, cats[row]))
    for name, _source, tokens in context.get("skills", []):
        parts.append((f"skill {name}", tokens or 0))
    deferred = cats.get(DEFERRED_ROW) or 0
    return {"tokens": sum(t for _, t in parts), "parts": parts,
            "deferred_tokens": deferred, "deferred_tools": len(context.get("mcp_tools", []))}


def up_front_text(parts: list[tuple[str, int]]) -> str:
    """'server instructions + skill description' from the always-on parts."""
    words = []
    for name, _ in parts:
        if name == "MCP server instructions":
            words.append("server instructions")
        elif name == "MCP tools":
            words.append("tool definitions")
        elif name.startswith("skill "):
            words.append("skill description")
    return " + ".join(words) if words else "nothing"


def context_dir(sweep_path: str | pathlib.Path) -> pathlib.Path:
    """The context directory of a sweep file: "context_dir" (default "context"),
    relative to the sweep file."""
    path = pathlib.Path(sweep_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return path.parent / data.get("context_dir", "context")


def load_contexts(directory: pathlib.Path) -> dict[str, dict]:
    """variant name -> the saved /context summary (<variant>.json)."""
    out = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out[data.get("variant") or path.stem] = data
    return out


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

@dataclass
class RunTokens:
    run_id: str
    first_prompt: int        # the prompt of the first request of the main lane
    peak: int                # the largest prompt of the main lane
    requests: int
    tools: dict = field(default_factory=dict)   # tool name -> calls


def run_tokens(record_dir: pathlib.Path) -> RunTokens:
    """The first and the largest context of a run, from its reduced session."""
    from .session import load_session, prompt_tokens

    reduced = record_dir / "reduced"
    result = json.loads((reduced / "claude-result.json").read_text(encoding="utf-8"))
    session = load_session(reduced / "claude-session" / f"{result['session_id']}.jsonl")
    requests = sorted(session.main.requests, key=lambda r: r.start)
    if not requests:
        raise ValueError(f"{record_dir}: the main lane has no requests")
    prompts = [prompt_tokens(r.usage) for r in requests]
    tools = {}
    path = session.main.path
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "assistant":
            continue
        for block in (record.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tools[block.get("name", "?")] = tools.get(block.get("name", "?"), 0) + 1
    run = json.loads((record_dir / "run.json").read_text(encoding="utf-8"))
    return RunTokens(run["meta"]["run_id"], prompts[0], max(prompts), len(requests), tools)


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


@dataclass
class VariantResult:
    label: str
    variant: str
    pasted: bool
    always_on: int | None
    runs: int
    found: int
    when_used: float | None
    cost: float | None
    wall: float | None
    cost_per_success: float | None
    tools: dict = field(default_factory=dict)
    up_front: str = ""
    deferred_tokens: int = 0
    deferred_tools: int = 0

    def found_text(self) -> str:
        if not self.runs:
            return "-"
        if self.found == self.runs:
            return "yes"
        if not self.found:
            return "no"
        return f"{self.found} of {self.runs}"

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def compare(groups: list[tuple], contexts: dict[str, dict], hourly_rate: float,
            rework_minutes: float) -> list[VariantResult]:
    """groups: (setup, pasted, [(row, RunTokens)]) for each setup of the sweep."""
    from .costmodel import Option, cost_per_success

    def num(value):
        return None if value in ("", None) else float(value)

    always = {}
    for setup, _pasted, _runs in groups:
        ctx = contexts.get(setup.variant)
        always[setup.variant] = always_on(ctx) if ctx else None
    # The start context with no extension: the first prompt of each run of a
    # variant with no paste, minus its always-on tokens.
    bases = []
    for setup, pasted, runs in groups:
        a = always[setup.variant]
        if pasted or a is None:
            continue
        bases += [t.first_prompt - a["tokens"] for _, t in runs]
    base = median(bases)

    out = []
    for setup, pasted, runs in groups:
        a = always[setup.variant]
        used = []
        for _row, t in runs:
            if pasted:
                used.append(None if base is None else t.peak - base)
            else:
                used.append(t.peak - t.first_prompt)
        rows = [row for row, _ in runs]
        found = sum(1 for r in rows if r.get("tests_pass") == "yes"
                    and r.get("rules_followed") == "yes")
        cost = median([num(r.get("cost_list_usd")) for r in rows])
        cps = None
        if found and cost is not None:
            option = Option(name=setup.label, token_cost=cost, success_rate=found / len(rows),
                            rework_minutes=rework_minutes)
            cps = cost_per_success(option, hourly_rate).cost_per_success
        tools = {}
        for _, t in runs:
            for name, n in t.tools.items():
                tools[name] = tools.get(name, 0) + n
        out.append(VariantResult(
            label=setup.label or setup.variant, variant=setup.variant, pasted=pasted,
            always_on=0 if pasted else (a["tokens"] if a else None),
            runs=len(rows), found=found, when_used=median(used), cost=cost,
            wall=median([num(r.get("wall_seconds")) for r in rows]),
            cost_per_success=cps, tools=tools,
            up_front="nothing" if pasted else (up_front_text(a["parts"]) if a else "-"),
            deferred_tokens=0 if pasted or not a else a["deferred_tokens"],
            deferred_tools=0 if pasted or not a else a["deferred_tools"]))
    return out
