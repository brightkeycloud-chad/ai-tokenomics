"""Read Codex usage.

Codex writes usage in two places:

    codex exec --json            One JSON event on each line, on standard output.
                                 Each `turn.completed` event has the token usage
                                 of one turn. The events do not name the model.
    <codex home>/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl
                                 The session file of each thread, interactive or
                                 `codex exec`. It has the model, the effort, one
                                 usage record for each request, time stamps, the
                                 context window, and the plan limits.

<codex home> is ~/.codex, or $CODEX_HOME.

Codex reports five token fields. Two of them are inside other fields:

    input_tokens                all prompt tokens of the request
      cached_input_tokens       part of input_tokens: read from the cache
      cache_write_input_tokens  part of input_tokens: written to the cache
    output_tokens               all generated tokens
      reasoning_output_tokens   part of output_tokens

The OpenAI prompt-caching guide gives fresh input as input_tokens minus
cached_input_tokens minus cache_write_input_tokens. This module does that
subtraction, so the toolkit meters do not count a token two times:

    input           = input_tokens - cached_input_tokens - cache_write_input_tokens
    cache_read      = cached_input_tokens
    cache_write_5m  = cache_write_input_tokens  (the short cache lifetime: 30
                      minutes, the OpenAI default; see config/rates.json)
    output          = output_tokens  (reasoning is inside it, for information)

The module prices every request two times: at OpenAI API list price (the
profile named for the model) and in Codex credits (the profile
"codex-credits/<model>"). Codex shows neither figure. On a subscription, the
real cost is the share of the plan limits, which the session file records
after each request.

Time has the same three parts as in `tokenomics session`:

    generating      from the start of a turn, or from the last tool output,
                    to the end of the next response
    running tools   all other time in a turn: commands and approval prompts
    waiting on you  from the end of one turn to the start of the next turn

The module reads one thread. It does not read threads that a thread starts.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field

from .costmodel import Usage
from .rates import METERS, RateProfile, select_profile
from .reduce import leftover_strings as _leftover_strings

FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
          "reasoning_output_tokens")
CREDIT_PREFIX = "codex-credits/"
EXEC_TYPES = {"thread.started", "turn.started", "turn.completed", "turn.failed",
              "item.started", "item.updated", "item.completed", "error"}
ROLLOUT_NAME = re.compile(r"^rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
                          r"-[0-9a-f]{12})\.jsonl$")
ORIGINATORS = {"codex_exec": "codex exec", "codex_cli_rs": "Codex CLI"}


# --------------------------------------------------------------------------
# Token fields
# --------------------------------------------------------------------------

def raw_usage(data: dict | None) -> dict:
    """The five Codex token fields, with 0 for a missing field."""
    data = data or {}
    out = {}
    for name in FIELDS:
        value = data.get(name) or 0
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"Codex usage field {name} must be a whole number >= 0, "
                             f"got {value!r}")
        out[name] = value
    return out


def to_usage(raw: dict) -> Usage:
    """Convert Codex token fields to the toolkit meters. Cached tokens and cache
    writes are inside input_tokens, so subtract them."""
    raw = raw_usage(raw)
    fresh = raw["input_tokens"] - raw["cached_input_tokens"] - raw["cache_write_input_tokens"]
    if fresh < 0:
        raise ValueError(f"cached and cache-write tokens ({raw['cached_input_tokens']} + "
                         f"{raw['cache_write_input_tokens']}) are more than input_tokens "
                         f"({raw['input_tokens']})")
    if raw["reasoning_output_tokens"] > raw["output_tokens"]:
        raise ValueError("reasoning_output_tokens is more than output_tokens; Codex counts "
                         "reasoning inside output")
    return Usage(input=fresh, cache_write_5m=raw["cache_write_input_tokens"],
                 cache_read=raw["cached_input_tokens"], output=raw["output_tokens"],
                 reasoning=raw["reasoning_output_tokens"])


def add_raw(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in FIELDS}


# --------------------------------------------------------------------------
# Find a source
# --------------------------------------------------------------------------

def codex_home() -> pathlib.Path:
    base = os.environ.get("CODEX_HOME")
    return pathlib.Path(base) if base else pathlib.Path.home() / ".codex"


def sessions_dir() -> pathlib.Path:
    return codex_home() / "sessions"


def _first_record(path: pathlib.Path) -> dict:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    return {}
    return {}


def thread_files(root: pathlib.Path | None = None) -> list[pathlib.Path]:
    root = root or sessions_dir()
    return sorted((p for p in root.rglob("rollout-*.jsonl") if ROLLOUT_NAME.match(p.name)),
                  key=lambda p: p.name)


def find_thread(spec: str | None = None, project: str | os.PathLike | None = None,
                root: pathlib.Path | None = None) -> pathlib.Path:
    """Return the session file of a thread.

    spec is a thread ID or the start of one. With no spec, return the most
    recent thread that started in the project directory (default: the
    current directory).
    """
    root = root or sessions_dir()
    files = thread_files(root)
    if spec:
        found = [p for p in files if ROLLOUT_NAME.match(p.name).group(1).startswith(spec)]
        if not found:
            raise FileNotFoundError(f"no Codex session file for thread {spec!r} in {root}")
        if len(found) > 1:
            raise ValueError(f"{spec!r} matches {len(found)} Codex threads; give more of the ID")
        return found[0]
    cwd = str(pathlib.Path(project or ".").expanduser().resolve())
    for path in reversed(files):
        meta = _first_record(path)
        where = meta.get("payload", {}).get("cwd") if meta.get("type") == "session_meta" else None
        if where and (where == cwd or str(pathlib.Path(where).resolve()) == cwd):
            return path
    raise FileNotFoundError(f"no Codex threads for project {cwd} in {root}")


def read_records(source: str | os.PathLike) -> list[dict]:
    """Read JSON Lines from a file, or from standard input when source is '-'."""
    if str(source) == "-":
        lines = sys.stdin.read().splitlines()
    else:
        lines = pathlib.Path(source).read_text(encoding="utf-8").splitlines()
    records = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue                       # a status line, or a partly written last line
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def kind_of(records: list[dict]) -> str:
    """'exec' for `codex exec --json` events, 'session' for a session file."""
    types = {r.get("type") for r in records}
    if types & {"session_meta", "turn_context", "token_usage_record", "event_msg"}:
        return "session"
    if types & EXEC_TYPES:
        return "exec"
    raise ValueError("this is not Codex output: expected `codex exec --json` events "
                     "or a Codex session file")


def is_codex_file(path: str | os.PathLike) -> bool:
    try:
        kind_of(read_records(path)[:20])
        return True
    except (ValueError, OSError):
        return False


# --------------------------------------------------------------------------
# Parse
# --------------------------------------------------------------------------

@dataclass
class Request:
    model: str
    raw: dict
    per_request: bool = True    # False: one turn total from `codex exec --json`


@dataclass
class Thread:
    source: str                          # "exec events" or "session file"
    thread_id: str = ""
    version: str = ""
    originator: str = ""
    effort: str = ""
    requests: list = field(default_factory=list)
    turns_started: int = 0
    turns_finished: int = 0
    failed_turns: int = 0
    commands: int = 0
    window: int = 0
    last_prompt: int = 0
    peak_prompt: int = 0
    generating: float = 0.0
    waiting: float = 0.0
    wall: float = 0.0
    timed: bool = False
    limits_first: dict | None = None
    limits_last: dict | None = None
    last_time: float = 0.0
    reported_total: dict | None = None   # Codex's own running total, to cross-check


def _ts(value: str) -> float:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def load_exec(records: list[dict], model: str = "") -> Thread:
    """Read `codex exec --json` events. Each turn.completed is one priced unit."""
    t = Thread(source="exec events")
    for r in records:
        rtype = r.get("type")
        if rtype == "thread.started":
            t.thread_id = r.get("thread_id", "")
        elif rtype == "turn.started":
            t.turns_started += 1
        elif rtype == "turn.completed":
            t.turns_finished += 1
            t.requests.append(Request(model=model, raw=raw_usage(r.get("usage")),
                                      per_request=False))
        elif rtype == "turn.failed":
            t.turns_finished += 1
            t.failed_turns += 1
        elif rtype == "item.completed" and (r.get("item") or {}).get("type") == "command_execution":
            t.commands += 1
    return t


def load_session(records: list[dict], model: str = "") -> Thread:
    """Read a Codex session file. `model` replaces the model of every request."""
    t = Thread(source="session file", timed=True)
    current_model = model
    usage_records = [r for r in records if r.get("type") == "token_usage_record"]
    start = cursor = turn_end = None
    open_turn = False
    previous_total = None
    for r in records:
        rtype = r.get("type")
        payload = r.get("payload") or {}
        ptype = payload.get("type")
        when = _ts(r["timestamp"]) if r.get("timestamp") else None
        if when is not None:
            t.last_time = when
        if rtype == "session_meta":
            t.thread_id = payload.get("id", "")
            t.version = payload.get("cli_version", "")
            t.originator = payload.get("originator", "")
        elif rtype == "turn_context":
            if not model:
                current_model = payload.get("model", current_model)
            t.effort = payload.get("effort") or t.effort
            cursor = max(cursor or when, when) if when else cursor
        elif ptype == "task_started":
            t.turns_started += 1
            open_turn = True
            t.window = payload.get("model_context_window") or t.window
            if start is None:
                start = when
            elif turn_end is not None:
                t.waiting += when - turn_end
            cursor = when
        elif ptype in ("task_complete", "turn_aborted"):
            t.turns_finished += 1
            open_turn = False
            if ptype == "turn_aborted":
                t.failed_turns += 1
            turn_end = when
        elif rtype == "response_item" and str(ptype).endswith("_output"):
            cursor = when                       # a tool result goes back to the model
            t.commands += 1
        elif rtype == "token_usage_record" or (ptype == "token_count" and not usage_records):
            if rtype == "token_usage_record":
                raw = raw_usage(payload.get("usage"))
            else:
                info = payload.get("info") or {}
                total = info.get("total_token_usage")
                if not total or total == previous_total:
                    continue                    # a limits-only event, or a repeat
                previous_total = total
                raw = raw_usage(info.get("last_token_usage"))
            t.requests.append(Request(model=current_model, raw=raw))
            t.last_prompt = raw["input_tokens"]
            t.peak_prompt = max(t.peak_prompt, raw["input_tokens"])
            if when is not None and cursor is not None and when > cursor:
                t.generating += when - cursor
            cursor = when
        if ptype == "token_count":
            info = payload.get("info") or {}
            t.window = info.get("model_context_window") or t.window
            if info.get("total_token_usage"):
                t.reported_total = raw_usage(info["total_token_usage"])
            limits = payload.get("rate_limits")
            if limits:
                if t.limits_first is None:
                    t.limits_first = limits
                t.limits_last = limits
    end = turn_end if not open_turn else t.last_time
    if start is not None and end is not None:
        t.wall = max(end - start, 0.0)
    return t


def load(source: str, project: str | None = None, model: str = "",
         root: pathlib.Path | None = None) -> tuple[Thread, Thread | None]:
    """Read a source: a file of exec events, '-' (standard input), a session file,
    or a thread ID. Return (thread, events). For exec events with a session file
    in the Codex home, `thread` is the session file and `events` is the exec
    stream, so the report can compare them."""
    if source and (source == "-" or pathlib.Path(source).expanduser().is_file()):
        records = read_records(pathlib.Path(source).expanduser() if source != "-" else "-")
    else:
        records = read_records(find_thread(source, project, root))
    if kind_of(records) == "session":
        return load_session(records, model), None
    events = load_exec(records, model)
    if events.thread_id:
        try:
            path = find_thread(events.thread_id, root=root)
        except (FileNotFoundError, ValueError):
            path = None
        if path:
            return load_session(read_records(path), model), events
    return events, None


# --------------------------------------------------------------------------
# Summarize
# --------------------------------------------------------------------------

def _card(requests: list[Request], profiles: dict, name_for) -> dict:
    """Price every request with the profile that name_for(model) gives."""
    costs = {m: 0.0 for m in METERS}
    tokens = {m: 0 for m in METERS}
    used, unpriced, unit = [], [], ""
    long_rule_skipped = False
    for req in requests:
        usage = to_usage(req.raw)
        name = name_for(req.model)
        base = profiles.get(name)
        if base is None:
            label = name if req.model else "(unknown model)"
            if label not in unpriced:
                unpriced.append(label)
            continue
        if req.per_request:
            profile = select_profile(profiles, name, req.raw["input_tokens"])
        else:
            # One total for each turn. If the total is over the limit, a request
            # in it may be too, but the events do not say which.
            profile = base
            long_rule_skipped = long_rule_skipped or bool(
                base.long_prompt_over and req.raw["input_tokens"] > base.long_prompt_over)
        if unit and profile.unit != unit:
            raise ValueError(f"rate profiles {used[0]} and {profile.name} have different units")
        unit = profile.unit
        if profile.name not in used:
            used.append(profile.name)
        for m in METERS:
            tokens[m] += getattr(usage, m)
            costs[m] += getattr(usage, m) * profile.price(m)
    total = sum(costs.values())
    checked = sorted({profiles[n].checked for n in used if profiles[n].checked})
    return {
        "unit": unit, "cost": total, "profiles": used, "checked": checked,
        "unpriced_models": unpriced, "long_prompt_rule_skipped": long_rule_skipped,
        "by_meter": {m: {"tokens": tokens[m], "cost": costs[m],
                         "share": (costs[m] / total) if total else None} for m in METERS},
    }


def _limits(t: Thread) -> dict | None:
    if not t.limits_last:
        return None

    def window(name):
        last = t.limits_last.get(name) or {}
        first = (t.limits_first or {}).get(name) or {}
        if not last:
            return None
        resets = last.get("resets_at")
        return {"window_minutes": last.get("window_minutes"),
                "used_percent_first": first.get("used_percent"),
                "used_percent_last": last.get("used_percent"),
                "resets_in_seconds": (resets - t.last_time) if resets and t.last_time else None}

    credits = t.limits_last.get("credits") or {}
    return {"plan": t.limits_last.get("plan_type") or "",
            "primary": window("primary"), "secondary": window("secondary"),
            "credits": {k: credits.get(k) for k in ("has_credits", "unlimited", "balance")}
            if credits else None}


def summarize(t: Thread, profiles: dict[str, RateProfile], events: Thread | None = None) -> dict:
    total_raw = {k: 0 for k in FIELDS}
    for req in t.requests:
        total_raw = add_raw(total_raw, req.raw)
    usage = to_usage(total_raw)
    models = []
    for req in t.requests:
        if req.model not in models:
            models.append(req.model)

    api = _card(t.requests, profiles, lambda m: m)
    credits = _card(t.requests, profiles, lambda m: CREDIT_PREFIX + m)

    events_match = None
    if events is not None:
        events_raw = {k: 0 for k in FIELDS}
        for req in events.requests:
            events_raw = add_raw(events_raw, req.raw)
        events_match = events_raw == total_raw

    all_input = total_raw["input_tokens"]
    time = None
    if t.timed:
        tools = max(t.wall - t.generating - t.waiting, 0.0)
        time = {"generating": t.generating, "tools": tools, "waiting": t.waiting, "wall": t.wall}
    context = None
    if t.requests and t.source == "session file":
        context = {"last": t.last_prompt, "peak": t.peak_prompt, "window": t.window}

    ratios = {
        "cache_hit": (total_raw["cached_input_tokens"] / all_input) if all_input else None,
        "reasoning_share_of_output": (total_raw["reasoning_output_tokens"]
                                      / total_raw["output_tokens"])
        if total_raw["output_tokens"] else None,
        "output_cost_share_api": api["by_meter"]["output"]["share"],
        "output_cost_share_credits": credits["by_meter"]["output"]["share"],
        "context_used": (context["last"] / context["window"])
        if context and context["window"] else None,
        "generation_to_wall": (time["generating"] / time["wall"])
        if time and time["wall"] else None,
    }

    return {
        "tool": "codex",
        "source": t.source,
        "thread_id": t.thread_id,
        "version": t.version,
        "originator": t.originator,
        "models": models,
        "effort": t.effort,
        "turns": t.turns_finished,
        "failed_turns": t.failed_turns,
        "requests": len(t.requests) if t.source == "session file" else None,
        "commands": t.commands,
        "complete": t.turns_started <= t.turns_finished,
        "codex_usage": dict(total_raw, total_tokens=total_raw["input_tokens"]
                            + total_raw["output_tokens"]),
        "codex_reported_total": t.reported_total,
        "usage": {m: getattr(usage, m) for m in METERS} | {"reasoning": usage.reasoning},
        "api": api,
        "credits": credits,
        "time": time,
        "context": context,
        "limits": _limits(t),
        "ratios": ratios,
        "events_match_session_file": events_match,
    }


# --------------------------------------------------------------------------
# Reduce: a copy with no text, safe to commit
# --------------------------------------------------------------------------

_LIMIT_FIELDS = ("limit_id", "plan_type")
_WINDOW_FIELDS = ("used_percent", "window_minutes", "resets_at")
_EVENT_FIELDS = {
    "task_started": ("type", "turn_id", "started_at", "model_context_window"),
    "task_complete": ("type", "turn_id", "started_at", "completed_at", "duration_ms",
                      "time_to_first_token_ms"),
    "turn_aborted": ("type", "turn_id", "reason"),
}


def _reduce_limits(limits: dict) -> dict:
    out = {k: limits[k] for k in _LIMIT_FIELDS if k in limits}
    for name in ("primary", "secondary"):
        if isinstance(limits.get(name), dict):
            out[name] = {k: limits[name][k] for k in _WINDOW_FIELDS if k in limits[name]}
    if isinstance(limits.get("credits"), dict):
        out["credits"] = {k: limits["credits"][k] for k in ("has_credits", "unlimited", "balance")
                          if k in limits["credits"]}
    return out


def _reduce_info(info: dict | None) -> dict | None:
    if not isinstance(info, dict):
        return None
    out = {}
    for key in ("total_token_usage", "last_token_usage"):
        if isinstance(info.get(key), dict):
            out[key] = dict(raw_usage(info[key]), total_tokens=info[key].get("total_tokens", 0))
    for key in ("last_usage_includes_reasoning", "model_context_window"):
        if key in info:
            out[key] = info[key]
    return out


def reduce_record(r: dict) -> dict | None:
    """Return the reduced record, or None if `tokenomics codex` does not read it."""
    rtype = r.get("type")
    base = {k: r[k] for k in ("timestamp", "ordinal", "type") if k in r}
    payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
    ptype = payload.get("type")
    # Exec events
    if rtype == "thread.started":
        return {"type": rtype, "thread_id": r.get("thread_id")}
    if rtype in ("turn.started", "turn.failed", "error"):
        return {"type": rtype}
    if rtype == "turn.completed":
        return {"type": rtype, "usage": raw_usage(r.get("usage"))}
    if rtype in ("item.started", "item.completed"):
        item = r.get("item") or {}
        return {"type": rtype, "item": {k: item[k] for k in ("id", "type", "status", "exit_code")
                                        if k in item}}
    # Session file records
    if rtype == "session_meta":
        keep = ("id", "session_id", "timestamp", "cli_version", "originator", "source",
                "model_provider")
        return base | {"payload": {k: payload[k] for k in keep if k in payload}}
    if rtype == "turn_context":
        keep = ("turn_id", "model", "effort")
        return base | {"payload": {k: payload[k] for k in keep if k in payload}}
    if rtype == "token_usage_record":
        keep = ("thread_id", "turn_id", "response_id")
        return base | {"payload": {k: payload[k] for k in keep if k in payload}
                       | {"usage": raw_usage(payload.get("usage"))}}
    if rtype == "event_msg" and ptype in _EVENT_FIELDS:
        return base | {"payload": {k: payload[k] for k in _EVENT_FIELDS[ptype] if k in payload}}
    if rtype == "event_msg" and ptype == "token_count":
        out = {"type": ptype, "info": _reduce_info(payload.get("info"))}
        if isinstance(payload.get("rate_limits"), dict):
            out["rate_limits"] = _reduce_limits(payload["rate_limits"])
        return base | {"payload": out}
    if rtype == "response_item" and str(ptype).endswith("_output"):
        return base | {"payload": {"type": ptype}}
    if rtype == "response_item" and str(ptype).endswith("_call"):
        return base | {"payload": {"type": ptype, "name": payload.get("name")}}
    return None


def reduce_file(source: pathlib.Path, out_dir: pathlib.Path) -> pathlib.Path:
    """Write a reduced copy of a Codex file (exec events or a session file) to
    out_dir, with the same file name. Do not overwrite an existing copy."""
    source, out_dir = pathlib.Path(source), pathlib.Path(out_dir)
    dest = out_dir / source.name
    if dest.exists():
        raise FileExistsError(f"{dest} already exists. Remove it, or use another directory.")
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fout:
        for record in read_records(source):
            reduced = reduce_record(record)
            if reduced is not None:
                fout.write(json.dumps(reduced, separators=(",", ":"), ensure_ascii=False) + "\n")
    return dest


_KNOWN = re.compile(r"^(resp_[0-9a-f]+|item_\d+)$")


def leftover_strings(files: list[pathlib.Path]) -> list[str]:
    """Every string value in the files that is not an ID, a time stamp, or a version."""
    return [s for s in _leftover_strings(files) if not _KNOWN.match(s)]
