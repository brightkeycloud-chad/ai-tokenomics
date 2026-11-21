#!/usr/bin/env python3
"""A status line for Claude Code.

Claude Code runs this script after each response and sends the session data
as JSON on stdin. The script prints three or four lines, each at most 100
columns wide (WIDTH), for these groups of signals:

    1. Model and effort. Context fill (input tokens only). API time divided
       by the wall clock: API time adds all lanes, and subagents run in
       parallel, so the ratio can be more than 100%.
    2. The prompt cache of the main conversation: hit ratio, requests,
       misses, the tokens written to the cache and their cost, and warm or
       cold. The write cost uses the rate for the size of the last prompt.
    3. Cost. The Claude Code session estimate (all lanes, list price). The
       cost of the last request, priced with the toolkit cost model, and
       the share of that cost that is cache writes. The share of the plan
       windows that you used.
    4. The cost of the next request if the cache is cold ("if cold" while
       the cache is warm, "next request" when it is cold). The line is not
       there before the first response.

If a line is wider than WIDTH (for example a long list of miss causes), the
script moves its last parts to a new line.

A local model: the model is in config/local/ollama.json or is an
Ollama tag, or ANTHROPIC_BASE_URL points at this machine. Line 1 then says
"local". Lines 3 and 4 show no dollar figure: local tokens have no price.
Claude Code still sends an estimate for a model that it does not know (it
prices the tokens as a cloud model), so the status line does not show it.

The script reads stdin only. It does not read transcripts, so it is fast.
Claude Code does not count subagents in the prompt cache statistics. For
every lane, use `tokenomics session` after the work is done.

Usage:
    statusline.py [--plain] [--json] < status.json

    --plain   no colors (for tests and logs)
    --json    print the values as JSON, not the status line

Install it with install.py in this directory. The rates come from
config/rates.json, or from the file in $TOKENOMICS_RATES.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tokenomics.cli import fmt_cost, fmt_duration, fmt_tokens  # noqa: E402
from tokenomics.costmodel import Usage, attempt_cost  # noqa: E402
from tokenomics.local import is_local_model, is_local_url  # noqa: E402
from tokenomics.rates import load_profiles, select_profile  # noqa: E402

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
BAR_CELLS = 10
WIDTH = 100  # the widest line, in columns
ANSI = re.compile(r"\033\[[0-9;]*m")
METER_NAMES = {"input": "input", "cache_write_5m": "cache writes", "cache_write_1h": "cache writes",
               "cache_read": "cache reads", "output": "output"}


def _get(data: dict, *keys, default=None):
    """data["a"]["b"], or default when a key is missing or a value is null."""
    for key in keys:
        if not isinstance(data, dict) or data.get(key) is None:
            return default
        data = data[key]
    return data


def model_id(data: dict) -> str:
    """The model ID without a suffix such as [1m]."""
    return re.sub(r"\[.*\]$", "", _get(data, "model", "id", default="") or "")


def money(value: float, unit: str) -> str:
    return fmt_cost(value, unit, digits=4 if unit == "USD" and value < 1 else 2)


def percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------

def last_request_usage(data: dict, ttl: str) -> Usage | None:
    """Tokens of the most recent request, on the toolkit meters.

    Claude Code gives one cache write count. The main conversation uses one
    cache lifetime (prompt_cache.ttl), so the writes go on that meter.
    """
    current = _get(data, "context_window", "current_usage")
    if not current:
        return None
    write_meter = "cache_write_1h" if ttl == "1h" else "cache_write_5m"
    return Usage.from_dict({
        "input": int(current.get("input_tokens") or 0),
        write_meter: int(current.get("cache_creation_input_tokens") or 0),
        "cache_read": int(current.get("cache_read_input_tokens") or 0),
        "output": int(current.get("output_tokens") or 0),
    })


def values(data: dict, profiles: dict, env: dict | None = None) -> dict:
    """Everything that the status line shows, as plain data. `env` (default:
    this process) gives ANTHROPIC_BASE_URL."""
    env = os.environ if env is None else env
    window = _get(data, "context_window", "context_window_size", default=0)
    used = _get(data, "context_window", "total_input_tokens", default=0)
    pct = _get(data, "context_window", "used_percentage")
    if pct is None:
        pct = 100 * used / window if window else 0
    api_ms = _get(data, "cost", "total_api_duration_ms", default=0)
    wall_ms = _get(data, "cost", "total_duration_ms", default=0)

    cache = data.get("prompt_cache") or None
    ttl = _get(cache or {}, "ttl", default="5m")
    out = {
        "model": model_id(data),
        "model_name": _get(data, "model", "display_name", default=model_id(data)) or "unknown model",
        "effort": _get(data, "effort", "level"),
        "context": {"used": used, "window": window, "percent": pct},
        "time": {"api_seconds": api_ms / 1000, "wall_seconds": wall_ms / 1000,
                 "api_share": api_ms / wall_ms if wall_ms else None},
        "plan": {name: _get(data, "rate_limits", name, "used_percentage")
                 for name in ("five_hour", "seven_day")},
        "session_estimate_usd": _get(data, "cost", "total_cost_usd", default=0),
        "cache": cache,
        "last_request": None,
        "if_cold": None,
        "cache_write_cost": None,
        "rate_problem": None,
        "local": is_local_model(model_id(data)) or is_local_url(env.get("ANTHROPIC_BASE_URL")),
    }
    if out["local"]:
        out["session_estimate_usd"] = None  # not a price: see the docstring

    usage = last_request_usage(data, ttl)
    if usage is None or out["local"]:
        return out
    prompt = usage.input + usage.cache_write_5m + usage.cache_write_1h + usage.cache_read
    profile = select_profile(profiles, out["model"], prompt)
    if profile is None:
        out["rate_problem"] = f"no rate profile for {out['model'] or 'this model'} in config/rates.json"
        return out
    cost = attempt_cost(usage, profile)
    writes = sum(line.cost for line in cost.lines if line.meter.startswith("cache_write"))
    out["last_request"] = {"profile": profile.name, "unit": profile.unit, "cost": cost.total,
                           "cache_write_share": writes / cost.total if cost.total else None,
                           "lines": cost.to_dict()["lines"]}

    write_meter = "cache_write_1h" if ttl == "1h" else "cache_write_5m"
    written = _get(cache or {}, "cache_write_tokens")
    if written:
        out["cache_write_cost"] = written * profile.price(write_meter)

    recache = _get(cache or {}, "recache_tokens_if_cold")
    if recache:
        cold_profile = select_profile(profiles, out["model"], recache)
        write = recache * cold_profile.price(write_meter)
        read = recache * cold_profile.price("cache_read")
        out["if_cold"] = {"tokens": recache, "write_cost": write, "read_cost": read,
                          "times_a_hit": write / read if read else None,
                          "unit": cold_profile.unit}
    return out


# --------------------------------------------------------------------------
# The lines
# --------------------------------------------------------------------------

def visible_width(text: str) -> int:
    """The width of a line on the screen, without the color codes."""
    return len(ANSI.sub("", text))


def fit(parts: list[str], sep: str, width: int = WIDTH) -> list[str]:
    """Join the parts with the separator. If the line is wider than `width`,
    start a new line at a separator. A single part that is too wide stays
    on its own line."""
    lines: list[str] = []
    for part in parts:
        if lines and visible_width(lines[-1] + sep + part) <= width:
            lines[-1] += sep + part
        else:
            lines.append(part)
    return lines


def render(v: dict, color: bool = True) -> list[str]:
    def paint(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if color else text

    sep = paint(" │ ", DIM)
    lines: list[str] = []

    # Line 1: model, context, time.
    pct = v["context"]["percent"]
    filled = min(BAR_CELLS, int(pct * BAR_CELLS // 100))
    bar_color = RED if pct >= 90 else YELLOW if pct >= 70 else GREEN
    bar = paint("▓" * filled + "░" * (BAR_CELLS - filled), bar_color)
    model = v["model_name"] + (f" {v['effort']}" if v["effort"] else "") \
        + (" · local" if v.get("local") else "")
    t = v["time"]
    lines += fit([model,
                  f"context {bar} {pct:.0f}% · {fmt_tokens(v['context']['used'])} of "
                  f"{fmt_tokens(v['context']['window'])}",
                  f"API {fmt_duration(t['api_seconds'])} ÷ wall {fmt_duration(t['wall_seconds'])} "
                  f"= {percent(t['api_share'])}"], sep)

    # Line 2: the prompt cache of the main conversation.
    cache = v["cache"]
    if not cache:
        lines += fit(["cache (main)", "no cache data yet"], sep)
    else:
        requests = cache.get("requests") or 0
        misses = cache.get("misses") or 0
        stats = (f"hit {percent(cache.get('hit_ratio'))} · {requests} "
                 f"request{'s' if requests != 1 else ''} · "
                 + paint(f"{misses} miss{'es' if misses != 1 else ''}", YELLOW if misses else GREEN)
                 + f" · {fmt_tokens(cache.get('cache_write_tokens') or 0)} written")
        if v["cache_write_cost"] is not None:
            stats += f" ({money(v['cache_write_cost'], v['last_request']['unit'])})"
        parts = ["cache (main)", stats]
        rebuilds = cache.get("expected_rebuilds") or 0
        if rebuilds:
            parts.append(f"{rebuilds} rebuild{'s' if rebuilds != 1 else ''} after compaction")
        if cache.get("warm"):
            until = time.strftime("%H:%M", time.localtime(cache["expires_at"])) \
                if cache.get("expires_at") else "?"
            parts.append(paint(f"warm until {until} ({cache.get('ttl', '?')})", GREEN))
        else:
            parts.append(paint("cold", YELLOW))
        causes = _get(cache, "last_miss_cause", "causes")
        if causes:
            parts.append("last miss: " + ", ".join(causes))
        lines += fit(parts, sep)

    # Line 3: cost and the plan windows. Line 4: the cost of a cold cache.
    plan = v["plan"]
    windows = []
    if plan["five_hour"] is not None:
        windows.append(f"5h {plan['five_hour']:.0f}%")
    if plan["seven_day"] is not None:
        windows.append(f"7d {plan['seven_day']:.0f}%")
    plan_part = ["plan " + " · ".join(windows)] if windows else []
    if v.get("local"):
        lines += fit(["cost", "local model: no token price, the meter is time"] + plan_part, sep)
        lines.append("Claude Code $ estimate not shown (unknown model)")
        return lines
    parts = ["cost", f"session {money(v['session_estimate_usd'], 'USD')} (estimate)"]
    last = v["last_request"]
    if last:
        parts.append(f"last request {money(last['cost'], last['unit'])}, "
                     f"cache writes {percent(last['cache_write_share'])}")
    lines += fit(parts + plan_part, sep)
    cold = v["if_cold"]
    if v["rate_problem"]:
        lines.append(v["rate_problem"])
    elif last and cold:
        times = f" ({cold['times_a_hit']:.0f}× a hit)" if cold["times_a_hit"] else ""
        text = (f"{money(cold['write_cost'], cold['unit'])} to re-cache "
                f"{fmt_tokens(cold['tokens'])}{times}")
        if cache and cache.get("warm"):
            lines.append("if cold: " + text)
        else:
            lines.append(paint("next request: " + text, YELLOW))
    return lines


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    unknown = [a for a in argv if a not in ("--plain", "--json", "-h", "--help")]
    if "-h" in argv or "--help" in argv or unknown:
        print(__doc__.strip())
        return 0 if not unknown else 2
    try:
        data = json.loads(sys.stdin.read() or "{}")
        v = values(data, load_profiles())
        if "--json" in argv:
            text = json.dumps(v, indent=2)
        else:
            text = "\n".join(render(v, color="--plain" not in argv))
    except Exception as exc:  # The status line must never go blank.
        text = f"tokenomics status line: {exc}"
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
