"""Read Claude Code session transcripts.

Claude Code writes one transcript (JSON Lines) for each lane of a session:

    <projects>/<project-key>/<session-id>.jsonl                       main lane
    <projects>/<project-key>/<session-id>/subagents/agent-<id>.jsonl  subagents and
                                                                      forked skills

<projects> is ~/.claude/projects, or $CLAUDE_CONFIG_DIR/projects. The
project key is the absolute project path with each character that is not a
letter or a digit changed to "-".

Three rules give honest numbers. This module obeys them:

1. Keep the token types in separate columns. Each request gives input,
   cache write (5-minute and 1-hour), cache read, and output tokens.
2. Count every lane. The main transcript does not include the tokens of
   subagents and forked skills, so the module reads every lane file.
3. Measure after the work is done. A warning shows when the main lane
   stopped on a tool call, or when the metadata of a subagent or skill says
   that it is not finished.

Claude Code writes one record for each content block of a response. All the
blocks of one response have the same message ID and the same usage, so the
module counts each message ID one time.

Time in the main lane has three parts:

    generating      from the request to the end of the response
    running tools   all other time: tools, subagents (in the foreground or
                    in the background), and permission prompts
    waiting on you  from the end of the last activity to your next prompt
                    or slash command, and from a question that Claude asks
                    (AskUserQuestion) to your answer

When a tool runs while the response still streams, that time counts as
generating. Subagents and forked skills run in parallel with the main lane,
so their generating time is reported for each lane, not added to the wall
clock.

Context is the prompt size of a request: input + cache write + cache read.
Claude Code counts input tokens only, and so does this module.

The requests view (`tokenomics session --requests`) gives every
request of every lane, in time order, with its cache result:

    hit     the request read at least as many tokens from the cache as it
            wrote to it
    write   the request wrote more than it read, and it is the first request
            of the first lane with this name: there was nothing to read yet
    miss    the request wrote more than it read, and an earlier request of a
            lane with the same name ran before it
    none    no cache read and no cache write

"Gap" is the time from the end of the last earlier request of a lane with
the same name to the start of this request: the idle time that the cache
saw. A miss after a gap longer than the cache lifetime is an expired cache.
A request after a large new tool result can also write more than it reads,
with a short gap. "TTL" is the lifetime of the cache write (5 minutes or
1 hour). The view also gives the miss-to-hit ratio of each model: a miss
costs the write rate divided by the read rate, compared with a hit.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
from dataclasses import dataclass, field

from .costmodel import Usage
from .rates import METERS, RateProfile, select_profile


# --------------------------------------------------------------------------
# Find a session
# --------------------------------------------------------------------------

def projects_dir() -> pathlib.Path:
    """The directory where Claude Code keeps its transcripts."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (pathlib.Path(base) if base else pathlib.Path.home() / ".claude") / "projects"


def project_key(project: str | os.PathLike) -> str:
    """/Users/me/my_app -> -Users-me-my-app, as Claude Code names the directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(pathlib.Path(project).expanduser().resolve()))


def find_session(spec: str | None = None, project: str | os.PathLike | None = None,
                 root: pathlib.Path | None = None) -> pathlib.Path:
    """Return the main transcript of a session.

    spec is a path to a .jsonl file, a session ID, or the start of a session
    ID. With no spec, return the most recent session of the project (default:
    the current directory).
    """
    if spec and pathlib.Path(spec).expanduser().is_file():
        return pathlib.Path(spec).expanduser()
    root = root or projects_dir()
    project_dir = root / project_key(project or os.getcwd())

    if spec:
        candidates = sorted(project_dir.glob(f"{spec}*.jsonl"))
        if not candidates:
            candidates = sorted(root.glob(f"*/{spec}*.jsonl"))
        if not candidates:
            raise FileNotFoundError(f"no session {spec!r} in {root}")
        if len(candidates) > 1:
            raise ValueError(f"session {spec!r} is not unique: "
                             + ", ".join(c.stem for c in candidates[:5]))
        return candidates[0]

    sessions = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not sessions:
        raise FileNotFoundError(f"no Claude Code sessions for this project in {project_dir}. "
                                "Use --project DIR, or give the session ID or file.")
    return sessions[-1]


# --------------------------------------------------------------------------
# Read the lanes
# --------------------------------------------------------------------------

def parse_time(value: str) -> float:
    """ISO 8601 time stamp -> seconds since the epoch."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def usage_from_api(usage: dict) -> Usage:
    """The usage object of an API response -> the meters of the cost model."""
    creation = usage.get("cache_creation")
    if creation:
        write_5m = creation.get("ephemeral_5m_input_tokens", 0) or 0
        write_1h = creation.get("ephemeral_1h_input_tokens", 0) or 0
    else:
        write_5m, write_1h = usage.get("cache_creation_input_tokens", 0) or 0, 0
    details = usage.get("output_tokens_details") or {}
    return Usage(input=usage.get("input_tokens", 0) or 0,
                 cache_write_5m=write_5m, cache_write_1h=write_1h,
                 cache_read=usage.get("cache_read_input_tokens", 0) or 0,
                 output=usage.get("output_tokens", 0) or 0,
                 reasoning=details.get("thinking_tokens", 0) or 0)


def add_usage(a: Usage, b: Usage) -> Usage:
    return Usage(**{m: getattr(a, m) + getattr(b, m) for m in METERS + ("reasoning",)})


def prompt_tokens(usage: Usage) -> int:
    """Context of one request: everything the model read. Output is not included."""
    return usage.input + usage.cache_write_5m + usage.cache_write_1h + usage.cache_read


@dataclass
class Request:
    """One API request and its response."""

    message_id: str
    model: str
    start: float
    end: float
    usage: Usage
    stop_reason: str | None


@dataclass
class Lane:
    """One transcript: the main conversation, a subagent, or a forked skill."""

    name: str
    kind: str                       # "main", "subagent", or "skill"
    path: pathlib.Path
    description: str = ""
    finished_flag: bool | None = None
    requests: list = field(default_factory=list)
    human: list = field(default_factory=list)       # prompt and slash-command times
    activity: list = field(default_factory=list)    # response ends and tool results
    questions: list = field(default_factory=list)   # (asked, answered) for AskUserQuestion
    cost_state: dict | None = None

    @property
    def finished(self) -> bool:
        """A subagent or skill: its metadata file says "finished". The main lane:
        the last response ended the turn (it did not stop on a tool call)."""
        if self.kind != "main":
            return self.finished_flag is not False
        last = max(self.requests, key=lambda r: r.end, default=None)
        return last is None or last.stop_reason != "tool_use"

    @property
    def usage(self) -> Usage:
        total = Usage()
        for r in self.requests:
            total = add_usage(total, r.usage)
        return total

    @property
    def models(self) -> list[str]:
        return sorted({r.model for r in self.requests})


QUESTION_TOOL = "AskUserQuestion"   # a tool that waits for your answer


def _is_tool_result(record: dict) -> bool:
    content = record.get("message", {}).get("content")
    return isinstance(content, list) and any(
        isinstance(c, dict) and c.get("type") == "tool_result" for c in content)


def _is_human(record: dict) -> bool:
    """A prompt or a slash command that you typed. Not a tool result or a notification."""
    if record.get("type") != "user" or record.get("isMeta") or _is_tool_result(record):
        return False
    if record.get("promptSource") == "system":
        return False
    origin = record.get("origin")
    return not isinstance(origin, dict) or origin.get("kind") in (None, "human")


def read_lane(path: pathlib.Path, name: str, kind: str, seen: set,
              description: str = "", finished: bool | None = None) -> Lane:
    """Read one transcript. `seen` holds message IDs that other lanes counted."""
    lane = Lane(name=name, kind=kind, path=path, description=description,
                finished_flag=finished)
    request_starts: dict[str, float] = {}
    responses: dict[str, dict] = {}
    last_other = None            # time of the last record that is not a response block
    asked: dict[str, float] = {}  # AskUserQuestion tool_use ID -> time the question showed

    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue         # a partly written last line
            rtype = record.get("type")
            if rtype == "cost-state":
                lane.cost_state = record
                continue
            stamp = record.get("timestamp")
            if not stamp or rtype not in ("user", "assistant", "api-request", "system"):
                continue
            t = parse_time(stamp)

            if rtype == "api-request":
                request_starts[record.get("id", "")] = t
                last_other = t
            elif rtype == "assistant":
                message = record.get("message") or {}
                for block in message.get("content") or []:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("name") == QUESTION_TOOL and block.get("id")):
                        asked[block["id"]] = t
                mid, usage = message.get("id"), message.get("usage")
                model = message.get("model", "")
                if not mid or not usage or model == "<synthetic>":
                    continue
                entry = responses.get(mid)
                if entry is None:
                    start = request_starts.get(record.get("requestRef") or "", last_other)
                    entry = responses[mid] = {"model": model, "start": start if start else t,
                                              "end": t}
                entry["end"] = max(entry["end"], t)
                entry["usage"] = usage
                entry["stop_reason"] = message.get("stop_reason")
            else:
                last_other = t
                if rtype == "user" and _is_tool_result(record):
                    lane.activity.append(t)
                    for block in record["message"]["content"]:
                        shown = asked.pop(block.get("tool_use_id"), None) \
                            if isinstance(block, dict) else None
                        if shown is not None and t > shown:
                            lane.questions.append((shown, t))
                elif _is_human(record):
                    lane.human.append(t)

    for mid, entry in responses.items():
        lane.activity.append(entry["end"])
        if mid in seen:
            continue
        seen.add(mid)
        lane.requests.append(Request(message_id=mid, model=entry["model"],
                                     start=min(entry["start"], entry["end"]), end=entry["end"],
                                     usage=usage_from_api(entry["usage"]),
                                     stop_reason=entry.get("stop_reason")))
    lane.requests.sort(key=lambda r: r.start)
    return lane


def _read_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


@dataclass
class Session:
    session_id: str
    path: pathlib.Path
    lanes: list
    version: str = ""

    @property
    def main(self) -> Lane:
        return self.lanes[0]

    @property
    def cost_state(self) -> dict | None:
        return self.main.cost_state


def load_session(path: str | os.PathLike) -> Session:
    """Read the main transcript and every subagent and forked-skill transcript."""
    path = pathlib.Path(path)
    seen: set = set()
    lanes = [read_lane(path, "main", "main", seen)]
    lane_dir = path.with_suffix("") / "subagents"
    for file in sorted(lane_dir.glob("agent-*.jsonl"), key=lambda p: p.stat().st_mtime):
        stem = file.name[: -len(".jsonl")]
        meta = _read_json(file.with_name(stem + ".meta.json"))
        skill = _read_json(file.with_name(stem + ".forked-skill.json"))
        if skill:
            kind, name = "skill", f"skill: {skill.get('skillName', stem)}"
        else:
            kind, name = "subagent", f"subagent: {meta.get('agentType', stem)}"
        lanes.append(read_lane(file, name, kind, seen, description=meta.get("description", ""),
                               finished=meta.get("finished")))
    # Other lanes in the order of their first request (file times change when a
    # session is copied). A lane with no request goes last.
    lanes[1:] = sorted(lanes[1:], key=lambda lane: min((r.start for r in lane.requests),
                                                       default=float("inf")))

    version = ""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if '"version"' in line:
                try:
                    version = json.loads(line).get("version", "")
                except json.JSONDecodeError:
                    continue
                if version:
                    break
    return Session(session_id=path.stem, path=path, lanes=lanes, version=version)


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------

def union_length(intervals: list, lo: float, hi: float) -> float:
    """Total length of the union of the intervals, inside [lo, hi]."""
    clipped = sorted((max(a, lo), min(b, hi)) for a, b in intervals if min(b, hi) > max(a, lo))
    total, cur_a, cur_b = 0.0, None, None
    for a, b in clipped:
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total


def lane_generating(lane: Lane) -> float:
    if not lane.requests:
        return 0.0
    lo = min(r.start for r in lane.requests)
    hi = max(r.end for r in lane.requests)
    return union_length([(r.start, r.end) for r in lane.requests], lo, hi)


def time_split(lane: Lane) -> dict:
    """Wall clock of a lane, split into generating, running tools, and waiting on you.

    The wall clock starts at the first prompt (or request) and stops at the
    last response or tool result.
    """
    if not lane.requests:
        return {"wall": 0.0, "generating": 0.0, "tools": 0.0, "waiting": 0.0}
    first_request = min(r.start for r in lane.requests)
    start = min([first_request] + [t for t in lane.human if t <= first_request])
    end = max(lane.activity + [r.end for r in lane.requests])

    generating = [(r.start, r.end) for r in lane.requests]
    waiting = []
    for t in lane.human:
        before = [a for a in lane.activity if a <= t]
        if before:
            waiting.append((max(before), t))
    waiting.extend(lane.questions)
    gen = union_length(generating, start, end)
    busy_or_waiting = union_length(generating + waiting, start, end)
    wall = end - start
    return {"wall": wall, "generating": gen, "waiting": busy_or_waiting - gen,
            "tools": max(wall - busy_or_waiting, 0.0)}


# --------------------------------------------------------------------------
# Cost and the report
# --------------------------------------------------------------------------

def _profile_for(request: Request, profiles: dict, override: RateProfile | None):
    if override:
        return select_profile({**profiles, override.name: override}, override.name,
                              prompt_tokens(request.usage))
    return select_profile(profiles, request.model, prompt_tokens(request.usage))


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def summarize(session: Session, profiles: dict, override: RateProfile | None = None,
              window: int | None = None) -> dict:
    """Everything that `tokenomics session` shows, as plain data (for --json)."""
    meter_cost = {m: 0.0 for m in METERS}
    unpriced: set = set()
    units: set = set()
    checked: set = set()
    lanes_out = []

    for lane in session.lanes:
        lane_cost = 0.0
        for request in lane.requests:
            profile = _profile_for(request, profiles, override)
            if profile is None:
                unpriced.add(request.model)
                continue
            units.add(profile.unit)
            if profile.checked:
                checked.add(profile.checked)
            for m in METERS:
                cost = getattr(request.usage, m) * profile.price(m)
                meter_cost[m] += cost
                lane_cost += cost
        usage = lane.usage
        lanes_out.append({
            "name": lane.name, "kind": lane.kind, "description": lane.description,
            "models": lane.models, "requests": len(lane.requests), "finished": lane.finished,
            "usage": {m: getattr(usage, m) for m in METERS + ("reasoning",)},
            "tokens": usage.total, "cost": lane_cost,
            "generating_seconds": lane_generating(lane),
        })

    if len(units) > 1:
        raise ValueError("the models in this session use rate profiles with different units "
                         f"({', '.join(sorted(units))}); use --rates to choose one profile")
    total_cost = sum(meter_cost.values())
    for lane in lanes_out:
        lane["cost_share"] = _ratio(lane["cost"], total_cost)

    total = Usage()
    for lane in session.lanes:
        total = add_usage(total, lane.usage)
    all_input = prompt_tokens(total)
    main_usage = session.main.usage
    other_tokens = total.total - main_usage.total
    other_cost = total_cost - lanes_out[0]["cost"]

    time = time_split(session.main)
    contexts = [prompt_tokens(r.usage) for r in session.main.requests]
    context = {"last": contexts[-1] if contexts else 0, "peak": max(contexts, default=0),
               "window": window}
    context["used"] = _ratio(context["last"], window) if window else None

    cost_state = session.cost_state
    estimate = None
    if cost_state and "totalCostUSD" in cost_state:
        estimate = {"total_cost_usd": cost_state["totalCostUSD"],
                    "api_seconds": (cost_state.get("totalAPIDuration") or 0) / 1000,
                    "models": sorted(cost_state.get("modelUsage", {}))}

    return {
        "session_id": session.session_id,
        "path": str(session.path),
        "version": session.version,
        "unit": units.pop() if units else (override.unit if override else "USD"),
        "rates": override.name if override else "per model",
        "rates_checked": sorted(checked),
        "unpriced_models": sorted(unpriced),
        "complete": all(lane["finished"] for lane in lanes_out),
        "lanes": lanes_out,
        "usage": {m: getattr(total, m) for m in METERS + ("reasoning",)},
        "tokens": total.total,
        "cost": total_cost,
        "cost_by_meter": {m: {"tokens": getattr(total, m), "cost": meter_cost[m],
                              "share": _ratio(meter_cost[m], total_cost)} for m in METERS},
        "time": time,
        "other_lanes_generating_seconds": sum(l["generating_seconds"] for l in lanes_out[1:]),
        "context": context,
        "ratios": {
            "cache_write_cost_share": _ratio(meter_cost["cache_write_5m"]
                                             + meter_cost["cache_write_1h"], total_cost),
            "generation_to_wall": _ratio(time["generating"], time["wall"]),
            "cache_hit": _ratio(total.cache_read, all_input),
            "cache_hit_main": _ratio(main_usage.cache_read, prompt_tokens(main_usage)),
            "context_used": context["used"],
            "other_lanes_token_share": _ratio(other_tokens, total.total),
            "other_lanes_cost_share": _ratio(other_cost, total_cost),
        },
        "claude_code_estimate": estimate,
    }


# --------------------------------------------------------------------------
# Requests in time order, with the cache result of each
# --------------------------------------------------------------------------

WRITE_METERS = {"5m": "cache_write_5m", "1h": "cache_write_1h"}


def cache_result(usage: Usage, first_of_name: bool) -> str:
    """hit, write, miss, or none (see the module docstring)."""
    reads, writes = usage.cache_read, usage.cache_write_5m + usage.cache_write_1h
    if not reads and not writes:
        return "none"
    if reads >= writes:
        return "hit"
    return "write" if first_of_name else "miss"


def write_ttl(usage: Usage) -> str:
    """The lifetime of the cache write of a request: 5m, 1h, or - (no write)."""
    if not usage.cache_write_5m and not usage.cache_write_1h:
        return "-"
    return "1h" if usage.cache_write_1h > usage.cache_write_5m else "5m"


def request_rows(session: Session, profiles: dict,
                 override: RateProfile | None = None) -> list[dict]:
    """Every request of every lane, in time order (`tokenomics session --requests`).

    Lanes with the same name (for example three calls of one subagent) get
    the numbers #1, #2, ... in the order of their first request. "prompt_cost"
    is the cost of the prompt only (input, cache writes, cache reads), so the
    output of a request does not hide the cache result.
    """
    lanes = sorted((lane for lane in session.lanes if lane.requests),
                   key=lambda lane: (lane.kind != "main", min(r.start for r in lane.requests)))
    per_name: dict[str, int] = {}
    for lane in lanes:
        per_name[lane.name] = per_name.get(lane.name, 0) + 1
    numbers: dict[str, int] = {}
    items = []
    for lane in lanes:
        numbers[lane.name] = numbers.get(lane.name, 0) + 1
        label = lane.name if per_name[lane.name] == 1 else f"{lane.name} #{numbers[lane.name]}"
        for index, request in enumerate(lane.requests):
            items.append((request.start, label, lane.name, index == 0, request))
    items.sort(key=lambda item: item[0])

    t0 = items[0][0] if items else 0.0
    last_end: dict[str, float] = {}
    rows = []
    for start, label, name, first_in_lane, request in items:
        u = request.usage
        profile = _profile_for(request, profiles, override)
        gap = None if name not in last_end else max(start - last_end[name], 0.0)
        cost = prompt_cost = None
        if profile is not None:
            prompt_cost = sum(getattr(u, m) * profile.price(m) for m in METERS if m != "output")
            cost = prompt_cost + u.output * profile.price("output")
        rows.append({
            "seconds": start - t0, "lane": label, "lane_name": name,
            "first_in_lane": first_in_lane, "model": request.model,
            "gap_seconds": gap,
            "usage": {m: getattr(u, m) for m in METERS},
            "prompt_tokens": prompt_tokens(u),
            "ttl": write_ttl(u),
            "cache": cache_result(u, name not in last_end),
            "prompt_cost": prompt_cost, "cost": cost,
        })
        last_end[name] = max(last_end.get(name, request.end), request.end)
    return rows


def miss_vs_hit(rows: list[dict], profiles: dict,
                override: RateProfile | None = None) -> list[dict]:
    """The miss-to-hit ratio for each model and cache lifetime in the rows: a miss costs
    the write rate divided by the read rate, compared with a hit."""
    out, seen = [], set()
    for row in rows:
        if row["ttl"] == "-" or (row["model"], row["ttl"]) in seen:
            continue
        seen.add((row["model"], row["ttl"]))
        profile = override or profiles.get(row["model"])
        if profile is None or not profile.price("cache_read"):
            continue
        write = profile.relative(WRITE_METERS[row["ttl"]])
        read = profile.relative("cache_read")
        out.append({"model": row["model"], "ttl": row["ttl"], "write_rate": write,
                    "read_rate": read, "ratio": write / read})
    return sorted(out, key=lambda r: (r["model"], r["ttl"] != "5m"))


def measured_miss_vs_hit(rows: list[dict]) -> list[dict]:
    """For each lane name with a hit and a later miss on the first request of
    a lane: the prompt cost of the miss divided by the prompt cost of the hit."""
    out = []
    firsts: dict[str, list] = {}
    for row in rows:
        if row["first_in_lane"] and row["prompt_cost"] is not None:
            firsts.setdefault(row["lane_name"], []).append(row)
    for name, items in firsts.items():
        hits = [r for r in items if r["cache"] == "hit"]
        misses = [r for r in items if r["cache"] == "miss"]
        if not hits or not misses:
            continue
        hit, miss = hits[-1], misses[-1]
        out.append({"lane_name": name, "hit": hit["lane"], "miss": miss["lane"],
                    "hit_prompt_cost": hit["prompt_cost"],
                    "miss_prompt_cost": miss["prompt_cost"],
                    "miss_gap_seconds": miss["gap_seconds"],
                    "ratio": _ratio(miss["prompt_cost"], hit["prompt_cost"])})
    return out
