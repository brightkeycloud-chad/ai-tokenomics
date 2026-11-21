"""Tool results and test runs of a Claude Code run.

A hook runs outside the conversation, and only what it returns lands in
context. This module measures what the tool results put in
the context of a run, and how many times the agent ran the tests.

    tokenomics tool-results RECORD_OR_SESSION      one run, every tool call
    tokenomics tool-results --sweep sweep.json      every run of the sweep
                                                    setups, and the medians

For each lane of a session (the main conversation, subagents, and forked
skills), it reads the requests in order. The tool results of one request go
into the prompt of the next request of the same lane. So:

    tokens of the results of request N  =  prompt of request N+1
                                         -  prompt of request N
                                         -  output of request N

The prompt is input + cache write + cache read. These are the counts of the
API, with the real tokenizer: no estimate. The number also includes a few
tokens of framing, and any reminder that Claude Code adds to the next
prompt. When one request makes several tool calls, the tokens are split
between the results by their size in bytes. The results of the last request
of a lane have no next request: they are not counted.

A test run is a Bash call that runs pytest (or `terraform test`, `npm test`,
`go test`, `cargo test`), and that the permissions did not deny. A re-run is
a test run that follows an earlier test run with no change between them:
only reads (Read, Glob, Grep) or nothing. A re-run can show that the agent
did not get the output that it needed, for example because a filter removed
it. A re-run after an edit is a normal check of the fix.

A reduced copy (`tokenomics reduce`) keeps the size of each tool
result and the kind of each tool call, so this module gives the same report
for the copy. Reduced copies from older versions have no sizes and no
kinds: the report shows "-" for them.

The hook log of the record (hook-logs/*.log) proves that a hook
ran: the filter-test-output hook writes one line for each rewrite and for
each filtered run, with the tool call ID, the size before and after, and the
exit status.
"""
from __future__ import annotations

import json
import pathlib
import re
import statistics
from dataclasses import asdict, dataclass, field

READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "LS", "TodoWrite", "NotebookRead", "ToolSearch"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# A command that runs a test suite: the runner at the start of a command (after
# an optional path), not a word inside a heredoc such as "import pytest".
TEST_COMMAND = re.compile(
    r"(?m)(?:^|&&|;|\|\||\()\s*(?:cd\s+\S+\s*&&\s*)?"
    r"(?:\S*/)?(?:python3?\s+-m\s+pytest|pytest|py\.test"
    r"|terraform(?:\s+-chdir=\S+)?\s+test|(?:npm|yarn|pnpm)\s+(?:run\s+)?test"
    r"|go\s+test|cargo\s+test)\b")


def tool_kind(name: str | None, tool_input) -> str:
    """'test' for a Bash call that runs tests, else ''."""
    if name != "Bash" or not isinstance(tool_input, dict):
        return ""
    return "test" if TEST_COMMAND.search(tool_input.get("command") or "") else ""


def result_bytes(block: dict) -> int | None:
    """The size in bytes of the text of a tool result. None for a reduced block
    that has no size."""
    if "bytes" in block:
        return block["bytes"]
    content = block.get("content")
    if content == "[removed]":
        return None
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    total = 0
    for part in content or []:
        if isinstance(part, dict) and part.get("type") == "text":
            total += len((part.get("text") or "").encode("utf-8"))
    return total


@dataclass
class Call:
    lane: str
    request: int                 # number of the request in its lane, from 1
    tool: str
    tool_use_id: str
    kind: str = ""               # "test" or ""
    bytes: int | None = None
    tokens: float | None = None  # measured: this result's share of the prompt growth
    error: bool = False
    denied: bool = False
    approved_by: str = ""        # the source of the permission decision, e.g. "hook"
    rerun: bool = False
    hook: dict | None = None     # the hook log entry of this call ("filtered")


@dataclass
class RunResults:
    run_id: str
    variant: str = ""
    tests_pass: str = ""
    requests: int = 0
    calls: list = field(default_factory=list)
    hook_log: dict = field(default_factory=dict)   # event -> count
    detail: bool = True          # False: a reduced copy with no sizes and no kinds

    @property
    def test_calls(self) -> list:
        return [c for c in self.calls if c.kind == "test" and not c.denied]

    def summary(self) -> dict:
        tests = self.test_calls
        known = self.detail

        def total(values):
            values = [v for v in values if v is not None]
            return sum(values) if values else None

        return {
            "run_id": self.run_id, "variant": self.variant, "tests_pass": self.tests_pass,
            "requests": self.requests, "tool_calls": len(self.calls),
            "test_runs": len(tests) if known else None,
            "reruns": sum(1 for c in tests if c.rerun) if known else None,
            "test_bytes": (total(c.bytes for c in tests) or 0) if known else None,
            "test_tokens": (total(c.tokens for c in tests) or 0) if known else None,
            "result_tokens": total(c.tokens for c in self.calls),
            "hook_filtered": self.hook_log.get("filtered", 0),
            "hook_rewrites": self.hook_log.get("rewrite", 0),
            "hook_skips": self.hook_log.get("skip", 0),
        }


def _prompt(usage: dict) -> int:
    return sum(usage.get(k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                              "cache_read_input_tokens"))


def read_lane(path: pathlib.Path, name: str, seen: set) -> tuple[list[Call], int, bool]:
    """The tool calls of one lane, with the measured tokens of each result.
    Return (calls, requests, detail)."""
    order: list[str] = []
    usage: dict[str, dict] = {}
    uses: dict[str, list] = {}          # message ID -> [(tool_use_id, name, kind)]
    results: dict[str, dict] = {}       # tool_use_id -> result facts
    detail = True
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = record.get("message") or {}
            content = message.get("content")
            if record.get("type") == "assistant":
                mid = message.get("id")
                if not mid or message.get("model") == "<synthetic>" or mid in seen:
                    continue
                if mid not in usage:
                    order.append(mid)
                usage[mid] = message.get("usage") or {}
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        kind = block.get("kind")      # a reduced copy: "test" or "other"
                        if kind is None:
                            if "input" in block:
                                kind = tool_kind(block.get("name"), block.get("input"))
                            else:
                                detail = False        # a reduced copy from an older version
                        uses.setdefault(mid, []).append(
                            (block.get("id"), block.get("name"),
                             "test" if kind == "test" else ""))
            elif record.get("type") == "user" and isinstance(content, list):
                decision = record.get("permissionDecision") or {}
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        size = result_bytes(block)
                        if size is None:
                            detail = False
                        results[block.get("tool_use_id")] = {
                            "bytes": size, "error": bool(block.get("is_error")),
                            "denied": bool(record.get("toolDenialKind"))
                            or decision.get("decision") == "reject",
                            "approved_by": decision.get("source", "")
                            if decision.get("decision") == "accept" else ""}
    seen.update(order)

    calls: list[Call] = []
    for n, mid in enumerate(order):
        batch = []
        for tool_use_id, tool, kind in uses.get(mid, []):
            facts = results.get(tool_use_id, {})
            batch.append(Call(name, n + 1, tool or "", tool_use_id or "", kind,
                              facts.get("bytes"), None, facts.get("error", False),
                              facts.get("denied", False), facts.get("approved_by", "")))
        if batch and n + 1 < len(order):
            growth = (_prompt(usage[order[n + 1]]) - _prompt(usage[mid])
                      - (usage[mid].get("output_tokens", 0) or 0))
            growth = max(growth, 0)
            sizes = [c.bytes for c in batch]
            if all(s is not None for s in sizes) and sum(sizes) > 0:
                for c in batch:
                    c.tokens = growth * c.bytes / sum(sizes)
            else:
                for c in batch:
                    c.tokens = growth / len(batch)
        calls += batch
    _mark_reruns(calls)
    return calls, len(order), detail


def _mark_reruns(calls: list[Call]) -> None:
    """A test run with no change since the last test run is a re-run."""
    earlier_test, changed = False, False
    for c in calls:
        if c.denied:
            continue
        if c.kind == "test":
            c.rerun = earlier_test and not changed
            earlier_test, changed = True, False
        elif c.tool not in READ_ONLY_TOOLS:
            changed = True


def read_hook_logs(record_dir: pathlib.Path) -> tuple[dict, dict]:
    """(event -> count, tool_use_id -> "filtered" entry) from <record>/hook-logs/."""
    counts: dict[str, int] = {}
    filtered: dict[str, dict] = {}
    for log in sorted((record_dir / "hook-logs").glob("*.log")):
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = entry.get("event", "")
            counts[event] = counts.get(event, 0) + 1
            if event == "filtered" and entry.get("tool_use_id"):
                filtered[entry["tool_use_id"]] = entry
    return counts, filtered


def session_of_record(record_dir: pathlib.Path) -> pathlib.Path:
    reduced = record_dir / "reduced"
    result = json.loads((reduced / "claude-result.json").read_text(encoding="utf-8"))
    return reduced / "claude-session" / f"{result['session_id']}.jsonl"


def load_run(source: str | pathlib.Path) -> RunResults:
    """Read one run: a run record directory (<run ID>.record) or a transcript file."""
    path = pathlib.Path(source).expanduser()
    meta, record_dir = {}, None
    if path.is_dir():
        record_dir = path
        run = json.loads((path / "run.json").read_text(encoding="utf-8"))
        meta = run["meta"] | {"tests_pass": run.get("row", {}).get("tests_pass", "")}
        main = session_of_record(path)
    else:
        main = path
    seen: set = set()
    calls, requests, detail = read_lane(main, "main", seen)
    lane_dir = main.with_suffix("") / "subagents"
    for file in sorted(lane_dir.glob("agent-*.jsonl")):
        more, _, more_detail = read_lane(file, file.stem, seen)
        calls += more
        detail = detail and more_detail
    counts, filtered = read_hook_logs(record_dir) if record_dir else ({}, {})
    for c in calls:
        c.hook = filtered.get(c.tool_use_id)
    return RunResults(run_id=meta.get("run_id", main.stem), variant=meta.get("variant", ""),
                      tests_pass=meta.get("tests_pass", ""), requests=requests, calls=calls,
                      hook_log=counts, detail=detail)


def median(values) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def medians(runs: list[RunResults]) -> dict:
    """The median of each summary number, over the runs of one setup."""
    sums = [r.summary() for r in runs]
    keys = ("requests", "test_runs", "reruns", "test_bytes", "test_tokens", "result_tokens")
    out = {k: median(s[k] for s in sums) for k in keys}
    out["runs"] = len(runs)
    out["runs_with_rerun"] = sum(1 for s in sums if s["reruns"])
    out["hook_filtered"] = sum(s["hook_filtered"] for s in sums)
    return out


def call_dict(call: Call) -> dict:
    return asdict(call)
