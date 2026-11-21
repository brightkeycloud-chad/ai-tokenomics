"""The `tokenomics` command.

    tokenomics rates                       list the rate profiles
    tokenomics attempt --rates NAME ...    price one attempt, meter by meter
    tokenomics compare SCENARIO.json ...   cost per successful task for each option
    tokenomics session [SESSION]           read a Claude Code session: every lane,
                                           tokens by type, time, context, cache.
                                           --requests: every request in time order,
                                           with its cache result
    tokenomics reduce [SESSION] --out DIR  copy a session with no text, safe to commit
    tokenomics codex [SOURCE]              read Codex usage: `codex exec --json` events or
                                           a Codex session file, priced at API list price
                                           and in Codex credits, with the plan limits
    tokenomics log [LOG.csv]               read the results log of the baseline runs
                                           (`bin/run-baseline` adds the rows)
    tokenomics index [LOG.csv] --sweep F   medians, spread, and index to the baseline of
                                           each setup in the results log.
                                           --cache adds the median cache tokens
    tokenomics index ... --brief           one short table for each task. With a local
                                           setup: passes, token cost, time, and cost per
                                           success attended and unattended. Else: passes,
                                           cost per success, cost index, time index,
                                           verdict, and the baseline spread
    tokenomics tco --hardware N ...        payback months of local hardware
    tokenomics tool-results RUN            the tool calls of one run: the size and the
                                           measured tokens of each result, test runs,
                                           re-runs, and the hook log
    tokenomics tool-results --sweep F      the same for every run of the sweep setups,
                                           with the medians of each setup
    tokenomics extensions --sweep F        four ways to extend the agent: always-on tokens,
                                           tokens when used, cost, time, and the result of
                                           each variant. --always-on: the first meter only
    tokenomics ollama fit [MODEL]          does a local model fit? The sizing with the GPU
                                           memory from the Ollama server log
    tokenomics ollama bench [MODEL]        a cold prompt of about 20k tokens: time to first
                                           token, reading and writing speed
    tokenomics ollama meters [MODEL]       the four local meters of the same prompt sent
                                           two times
    tokenomics ollama task [SESSION]       the four local meters of each model request of a
                                           Claude Code session, from the Ollama server log
    tokenomics ollama task --codex [ID]    the same for a Codex thread on the local model

Add --json to `attempt`, `compare`, `session`, `codex`, `log`, `index`, `tool-results`,
`extensions`, `ollama`, or `tco` for output that other scripts can read.

Add --brief to `compare`, `session`, `codex`, `log`, `index`, `tool-results`, or
`ollama task` for a short view for the screen: at most about 15 lines and 100 columns.

    compare --brief        success rate, cost per success, and the ratio to the best option
    session --brief        tokens and cost by token type, and the cost of each lane group
    session --requests --brief
                           the cache result (hit, write, miss) of the first request of each
                           lane, of each request after an idle gap, and of each miss, then
                           the miss ÷ hit ratio
    codex --brief          tokens and cost by token type, and the plan limits at the end
    log --brief            the newest runs: result, all tokens, list price, wall time
    tool-results --brief   the totals of each run: test runs, re-runs, test tokens, and
                           all tool results
    ollama task --brief    the local meters of a task: request 1, the cold request and the
                           request after it, and the sums of the other requests
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import textwrap

from . import __version__
from . import baseline
from . import codex
from . import codexlocal as codexlocal_mod
from . import extensions as extensions_mod
from . import index as index_mod
from . import local as local_mod
from . import ollama as ollama_mod
from . import runconfig
from . import sweep as sweep_mod
from . import tco as tco_mod
from . import toolresults
from .costmodel import Usage, attempt_cost, cost_per_success
from .rates import METERS, get_profile, load_profiles, rates_file
from .scenario import apply_overrides, load_scenario, parse_number
from .reduce import leftover_strings, reduce_session
from .session import (find_session, load_session, measured_miss_vs_hit, miss_vs_hit,
                      request_rows, summarize)

METER_LABELS = {
    "input": "Input",
    "cache_write_5m": "Cache write (5 min)",
    "cache_write_1h": "Cache write (1 hour)",
    "cache_read": "Cache read",
    "output": "Output",
}


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def fmt_tokens(n: float) -> str:
    """940000 -> 940.0k."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:,.0f}"


def fmt_cost(value: float, unit: str, digits: int = 2) -> str:
    if unit == "USD":
        return f"${value:,.{digits}f}"
    if unit == "input-equivalent tokens":
        return f"{fmt_tokens(value)} in-eq"
    return f"{value:,.{digits}f} {unit}"


def fmt_duration(seconds: float) -> str:
    """25.5 -> 25.5s, 252 -> 4m 12s, 3720 -> 1h 02m."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60):02d}m"


def fmt_share(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def fmt_rate(multiplier: float) -> str:
    return f"{multiplier:g}×"


def table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
    widths = [max(len(str(c)) for c in col) for col in zip(headers, *rows)]

    def line(cells):
        out = []
        for i, cell in enumerate(cells):
            out.append(str(cell).rjust(widths[i]) if i in right else str(cell).ljust(widths[i]))
        return "  ".join(out).rstrip()

    rule = "  ".join("-" * w for w in widths)
    return "\n".join([line(headers), rule] + [line(r) for r in rows])


def table2(headers: list, rows: list[list[str]], right: set[int] = frozenset()) -> str:
    """The same as table(), but a header can be a pair (top line, bottom line).
    Two header lines make a column as narrow as its values (the --brief views)."""
    pairs = [h if isinstance(h, tuple) else ("", h) for h in headers]
    if not any(top for top, _ in pairs):
        return table([b for _, b in pairs], rows, right)
    widths = [max(len(str(c)) for c in col)
              for col in zip([t for t, _ in pairs], [b for _, b in pairs], *rows)]

    def line(cells):
        return "  ".join(str(c).rjust(widths[i]) if i in right else str(c).ljust(widths[i])
                         for i, c in enumerate(cells)).rstrip()

    rule = "  ".join("-" * w for w in widths)
    return "\n".join([line([t for t, _ in pairs]), line([b for _, b in pairs]), rule]
                     + [line(r) for r in rows])


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_rates(args) -> int:
    profiles = load_profiles(args.rates_file)
    print(f"Rate profiles in {rates_file(args.rates_file)}\n")
    rows = []
    for p in profiles.values():
        rows.append([p.name, p.unit] + [fmt_rate(p.relative(m)) for m in METERS]
                    + [p.checked or "-"])
    headers = ["Profile", "Unit", "Input", "Write short", "Write long", "Read", "Output",
               "Checked"]
    print(table(headers, rows, right={2, 3, 4, 5, 6}))
    print("\nRates are relative to each profile's input rate. Look up current prices "
          "before you record a cost.")
    print("Cache write lifetimes: short is 5 minutes (Anthropic) or 30 minutes (OpenAI). "
          "Long is 1 hour or 12 hours.")
    return 0


def _usage_from_args(args) -> Usage:
    data = {}
    if args.usage:
        data.update(json.loads(pathlib.Path(args.usage).read_text()))
    for meter in METERS + ("reasoning",):
        value = getattr(args, meter)
        if value is not None:
            data[meter] = value
    if not data:
        raise ValueError("give token counts with --usage FILE or flags such as --output 5300")
    return Usage.from_dict(data)


def cmd_attempt(args) -> int:
    usage = _usage_from_args(args)
    profile = get_profile(args.rates, args.rates_file)
    result = attempt_cost(usage, profile)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    print(f"Token cost of one attempt · rate profile: {profile.name}\n")
    rows = []
    for line in sorted(result.lines, key=lambda l: l.cost, reverse=True):
        if line.tokens == 0:
            continue
        rows.append([METER_LABELS[line.meter], fmt_tokens(line.tokens),
                     fmt_rate(line.relative_rate), fmt_cost(line.cost, result.unit, 4),
                     f"{line.share:.0%}"])
    rows.append(["Total", fmt_tokens(usage.total), "", fmt_cost(result.total, result.unit, 4),
                 "100%"])
    print(table(["Meter", "Tokens", "Rate", "Cost", "Share"], rows, right={1, 2, 3, 4}))
    if usage.reasoning:
        print(f"\nReasoning: {fmt_tokens(usage.reasoning)} tokens, already counted in "
              "Output (billed as output).")
    if profile.is_currency:
        print(f"\nRates checked {profile.checked or 'on an unknown date'}"
              f"{' at ' + profile.source if profile.source else ''}.")
    else:
        print("\nCost is in input-equivalent tokens: the number of fresh input tokens "
              "that cost the same.")
    return 0


def _print_scenario(scenario, results) -> None:
    unit = scenario.currency

    # Measured runs can cost less than one cent. Then show 4 decimals.
    amounts = [v for r in results for v in (r.token_cost, r.token_cost_per_success,
                                             r.rework_cost, r.waiting_cost, r.cost_per_success)]
    digits = 4 if any(0 < abs(v) < 0.01 for v in amounts) else 2

    def money(value):
        return fmt_cost(value, unit, digits)

    print(scenario.title)
    if scenario.note:
        print(scenario.note)
    print(f"Hourly rate {fmt_cost(scenario.hourly_rate, unit)} · currency {unit}\n")

    best = min(results, key=lambda r: r.cost_per_success)
    rows = []
    for r in results:
        rows.append([
            r.name,
            money(r.token_cost),
            f"{r.success_rate:.0%}",
            money(r.token_cost_per_success),
            f"{r.failures_per_success:.2f}",
            money(r.rework_cost),
            money(r.waiting_cost),
            money(r.cost_per_success),
            f"{r.cost_per_success / best.cost_per_success:.2f}×"
            + ("  best" if r is best else ""),
        ])
    headers = ["Option", "Tokens/attempt", "Success", "Tokens/success", "Failures/success",
               "Rework", "Waiting", "Cost/success", "vs best"]
    print(table(headers, rows, right={1, 2, 3, 4, 5, 6, 7}))

    cheapest_attempt = min(results, key=lambda r: r.token_cost)
    print(f"\nCheapest per attempt: {cheapest_attempt.name}. "
          f"Cheapest per success: {best.name}.")
    if cheapest_attempt is not best:
        price_ratio = cheapest_attempt.token_cost / best.token_cost
        cost_ratio = cheapest_attempt.cost_per_success / best.cost_per_success
        print(f"{cheapest_attempt.name} costs {price_ratio:.0%} of {best.name} per attempt, "
              f"and {cost_ratio:.1f}× per successful task.")


def _print_scenario_brief(scenario, results) -> None:
    """`tokenomics compare --brief`: the option, the success rate, the cost per
    success, and the ratio to the best option. Then the cheapest option per
    attempt and per success. At most about 15 lines and 100 columns: blank
    lines between the parts only when the view stays at 15 lines or fewer."""
    unit = scenario.currency
    amounts = [r.cost_per_success for r in results]
    digits = 4 if any(0 < abs(v) < 0.01 for v in amounts) else 2
    best = min(results, key=lambda r: r.cost_per_success)
    rows = [[r.name, f"{r.success_rate:.0%}", fmt_cost(r.cost_per_success, unit, digits),
             f"{r.cost_per_success / best.cost_per_success:.2f}×"
             + ("  best" if r is best else "")] for r in results]
    cheapest_attempt = min(results, key=lambda r: r.token_cost)
    last = [f"Cheapest per attempt: {cheapest_attempt.name}. "
            f"Cheapest per success: {best.name}."]
    if cheapest_attempt is not best:
        last.append(f"{cheapest_attempt.name} costs "
                    f"{cheapest_attempt.token_cost / best.token_cost:.0%} of {best.name} per "
                    f"attempt, and "
                    f"{cheapest_attempt.cost_per_success / best.cost_per_success:.1f}× "
                    "per successful task.")
    parts = [[scenario.title],
             table(["Option", "Success", "Cost/success", "vs best"], rows,
                   right={1, 2}).splitlines(), last]
    blank = sum(len(part) for part in parts) + 2 <= INDEX_BRIEF_LINES
    print(("\n\n" if blank else "\n").join("\n".join(part) for part in parts))


def cmd_compare(args) -> int:
    reports = []
    for path in args.scenarios:
        scenario = apply_overrides(load_scenario(path, args.rates_file), args.set or [])
        results = [cost_per_success(o, scenario.hourly_rate) for o in scenario.options]
        reports.append((scenario, results))

    if args.json:
        print(json.dumps([{"scenario": s.title, "path": s.path, "currency": s.currency,
                           "hourly_rate": s.hourly_rate,
                           "results": [r.to_dict() for r in rs]} for s, rs in reports],
                         indent=2))
        return 0

    for i, (scenario, results) in enumerate(reports):
        if args.brief:
            if i:
                print()
            _print_scenario_brief(scenario, results)
            continue
        if i:
            print("\n" + "=" * 72 + "\n")
        _print_scenario(scenario, results)
    return 0


def fmt_clock(seconds: float) -> str:
    """75 -> 1:15, 3725 -> 1:02:05: time from the first request."""
    seconds = int(round(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _print_requests(report: dict) -> None:
    """The --requests view: every request in time order, and the
    cost of a cache miss compared with a hit."""
    unit = report["unit"]
    print("\nRequests in time order (all lanes)")
    rows = []
    for r in report["requests"]:
        u = r["usage"]
        rows.append([fmt_clock(r["seconds"]), r["lane"], r["model"],
                     "-" if r["gap_seconds"] is None else fmt_duration(r["gap_seconds"]),
                     fmt_tokens(u["input"]), fmt_tokens(u["cache_write_5m"] + u["cache_write_1h"]),
                     r["ttl"], fmt_tokens(u["cache_read"]), fmt_tokens(u["output"]),
                     "-" if r["prompt_cost"] is None else fmt_cost(r["prompt_cost"], unit, 5),
                     r["cache"]])
    print(table(["Time", "Lane", "Model", "Gap", "Input", "Cache write", "TTL", "Cache read",
                 "Output", "Prompt cost", "Cache"], rows, right={0, 3, 4, 5, 7, 8, 9}))
    print("Gap: the time since the last request of a lane with the same name. "
          "Prompt cost: input and cache tokens, no output.")

    if report["miss_vs_hit"]:
        print("\nA miss compared with a hit on the same prefix (write rate ÷ read rate)")
        rows = [[m["model"], "5-minute write" if m["ttl"] == "5m" else "1-hour write",
                 f"{fmt_rate(round(m['write_rate'], 4))} ÷ {fmt_rate(round(m['read_rate'], 4))}",
                 f"{m['ratio']:.1f}×"] for m in report["miss_vs_hit"]]
        print(table(["Model", "Cache write", "Rates (× input)", "Miss ÷ hit"], rows,
                    right={3}))
    for m in report["measured_miss_vs_hit"]:
        print(f"\nMeasured, first request of each lane: {m['miss']} (miss, after "
              f"{fmt_duration(m['miss_gap_seconds'] or 0)} idle) "
              f"{fmt_cost(m['miss_prompt_cost'], unit, 6)} ÷ {m['hit']} (hit) "
              f"{fmt_cost(m['hit_prompt_cost'], unit, 6)} = {m['ratio']:.1f}×.")


REQUESTS_BRIEF_ROWS = 8   # `session --requests --brief`: at most this many requests
REQUESTS_BRIEF_GAP = 60   # seconds: a gap of this length or more is idle time


def _brief_requests(rows: list[dict]) -> list[int]:
    """The requests that `session --requests --brief` shows, as indexes in
    time order: the first request of each lane, each request after an idle
    gap (REQUESTS_BRIEF_GAP or more), and each miss. If there are more than
    REQUESTS_BRIEF_ROWS, keep the misses first, then the idle gaps, then the
    first requests."""
    def rank(r):
        if r["cache"] == "miss":
            return 0
        if (r["gap_seconds"] or 0) >= REQUESTS_BRIEF_GAP:
            return 1
        return 2 if r["first_in_lane"] else None

    ranked = [(rank(r), i) for i, r in enumerate(rows) if rank(r) is not None]
    return sorted(i for _, i in sorted(ranked)[:REQUESTS_BRIEF_ROWS])


def _print_requests_brief(report: dict) -> None:
    """`tokenomics session --requests --brief`: the cache result of
    the requests that matter, the miss ÷ hit rates, and the measured ratio.
    At most about 15 lines and 100 columns."""
    unit = report["unit"]
    rows = report["requests"]
    lanes = report["lanes"]
    shown = _brief_requests(rows)
    print(f"Session {report['session_id'][:8]} · {len(lanes)} "
          f"lane{'s' if len(lanes) != 1 else ''} (main + {len(lanes) - 1}) · {len(shown)} of "
          f"{len(rows)} requests: first of each lane, idle gaps, misses\n")
    table_rows = []
    for i in shown:
        r = rows[i]
        u = r["usage"]
        table_rows.append([fmt_clock(r["seconds"]), r["lane"],
                           "-" if r["gap_seconds"] is None else fmt_duration(r["gap_seconds"]),
                           fmt_tokens(u["cache_write_5m"] + u["cache_write_1h"]), r["ttl"],
                           fmt_tokens(u["cache_read"]), r["cache"]])
    print(table(["Time", "Lane", "Gap", "Cache write", "TTL", "Cache read", "Cache"],
                table_rows, right={0, 2, 3, 4, 5}))
    notes = []
    for m in report["miss_vs_hit"]:
        notes.append(f"Miss ÷ hit, {m['model']}: {'5-minute' if m['ttl'] == '5m' else '1-hour'} "
                     f"write rate ÷ read rate = {fmt_rate(round(m['write_rate'], 4))} ÷ "
                     f"{fmt_rate(round(m['read_rate'], 4))} = {m['ratio']:.1f}×.")
    for m in report["measured_miss_vs_hit"]:
        notes.append(f"Measured, first request of each lane: {m['miss']} (miss, after "
                     f"{fmt_duration(m['miss_gap_seconds'] or 0)} idle) "
                     f"{fmt_cost(m['miss_prompt_cost'], unit, 6)}")
        notes.append(f"÷ {m['hit']} (hit) {fmt_cost(m['hit_prompt_cost'], unit, 6)} "
                     f"= {m['ratio']:.1f}×.")
    for lane in lanes:
        if not lane["finished"]:
            notes.append(f"WARNING: {lane['name']} is not finished. Collect again later.")
    if notes:
        print()
        for note in notes:
            print(textwrap.fill(note, 100, break_long_words=False, break_on_hyphens=False))


def _print_session(report: dict) -> None:
    unit = report["unit"]
    lanes = report["lanes"]
    others = len(lanes) - 1
    state = "all lanes finished" if report["complete"] else "NOT ALL LANES FINISHED"
    print(f"Claude Code session {report['session_id']} · version {report['version'] or '?'} · "
          f"{len(lanes)} lane{'s' if len(lanes) != 1 else ''} (main + {others}) · {state}")
    if report["rates"] == "per model":
        checked = ", ".join(report["rates_checked"])
        print("Rates: the profile for each model in config/rates.json"
              + (f" (checked {checked})" if checked else "") + "\n")
    else:
        print(f"Rates: profile {report['rates']} for every model\n")

    print("Tokens and cost by lane")
    rows = []
    for lane in lanes:
        u = lane["usage"]
        rows.append([lane["name"], ", ".join(lane["models"]), str(lane["requests"]),
                     fmt_tokens(u["input"]), fmt_tokens(u["cache_write_5m"] + u["cache_write_1h"]),
                     fmt_tokens(u["cache_read"]), fmt_tokens(u["output"]),
                     fmt_cost(lane["cost"], unit, 4), fmt_share(lane["cost_share"])])
    u = report["usage"]
    rows.append(["Total", "", str(sum(l["requests"] for l in lanes)), fmt_tokens(u["input"]),
                 fmt_tokens(u["cache_write_5m"] + u["cache_write_1h"]), fmt_tokens(u["cache_read"]),
                 fmt_tokens(u["output"]), fmt_cost(report["cost"], unit, 4),
                 "100%" if report["cost"] else "-"])
    print(table(["Lane", "Model", "Requests", "Input", "Cache write", "Cache read", "Output",
                 "Cost", "Share"], rows, right={2, 3, 4, 5, 6, 7, 8}))

    if "requests" in report:
        _print_requests(report)
        for lane in lanes:
            if not lane["finished"]:
                print(f"WARNING: {lane['name']} is not finished. Collect again after all work, "
                      "including background work, is done.")
        return

    print("\nCost by token type (all lanes)")
    rows = []
    for meter, line in sorted(report["cost_by_meter"].items(), key=lambda kv: -kv[1]["cost"]):
        if line["tokens"]:
            rows.append([METER_LABELS[meter], fmt_tokens(line["tokens"]),
                         fmt_cost(line["cost"], unit, 4), fmt_share(line["share"])])
    print(table(["Meter", "Tokens", "Cost", "Share"], rows, right={1, 2, 3}))

    time = report["time"]
    print("\nTime in the main lane")
    rows = [[label, fmt_duration(time[key]), fmt_share(time[key] / time["wall"] if time["wall"] else None)]
            for label, key in (("Generating", "generating"), ("Running tools", "tools"),
                               ("Waiting on you", "waiting"))]
    rows.append(["Wall clock", fmt_duration(time["wall"]), "100%"])
    print(table(["Part", "Time", "Share"], rows, right={1, 2}))
    if others:
        print(f"Other lanes generated for {fmt_duration(report['other_lanes_generating_seconds'])}, "
              "in parallel with the main lane.")

    context = report["context"]
    line = (f"\nContext in the main lane: {fmt_tokens(context['last'])} tokens in the last request, "
            f"{fmt_tokens(context['peak'])} at the peak")
    if context["window"]:
        line += f", of a {fmt_tokens(context['window'])} window."
    else:
        line += ". Add --window N to divide by the window size (see /context)."
    print(line)

    ratios = report["ratios"]
    print("\nRatios")
    rows = [
        ["Cache writes ÷ cost", fmt_share(ratios["cache_write_cost_share"])],
        ["Generation ÷ wall time (main lane)", fmt_share(ratios["generation_to_wall"])],
        ["Context used ÷ window (last request)", fmt_share(ratios["context_used"])],
        ["Cache reads ÷ all input (all lanes)", fmt_share(ratios["cache_hit"])],
        ["Cache reads ÷ all input (main lane)", fmt_share(ratios["cache_hit_main"])],
    ]
    if others:
        rows += [["Other lanes ÷ all tokens", fmt_share(ratios["other_lanes_token_share"])],
                 ["Other lanes ÷ cost", fmt_share(ratios["other_lanes_cost_share"])]]
    print(table(["Ratio", "Value"], rows, right={1}))

    print()
    main_cost = lanes[0]["cost"]
    print(f"This report: {fmt_cost(report['cost'], unit, 4)} for all lanes. "
          f"The main lane alone: {fmt_cost(main_cost, unit, 4)}.")
    estimate = report["claude_code_estimate"]
    if estimate:
        print(f"Claude Code estimate in the transcript: ${estimate['total_cost_usd']:.4f} "
              "(list price. It can also include background requests that have no transcript).")
    else:
        print("No Claude Code estimate in the transcript yet. Claude Code writes it when "
              "the session ends.")
    if report["unpriced_models"]:
        print(f"No rate profile for {', '.join(report['unpriced_models'])}: their tokens have "
              "no cost here. Add a profile to config/rates.json, or use --rates.")
    for lane in lanes:
        if not lane["finished"]:
            print(f"WARNING: {lane['name']} is not finished. Collect again after all work, "
                  "including background work, is done.")


def _print_session_brief(report: dict) -> None:
    """`tokenomics session --brief`: the tokens and the cost by token type for
    all lanes, and the cost of the main lane and the other lanes. At most
    about 15 lines and 100 columns."""
    unit = report["unit"]
    lanes = report["lanes"]
    others = len(lanes) - 1
    state = "all lanes finished" if report["complete"] else "NOT ALL LANES FINISHED"
    print(f"Session {report['session_id'][:8]} · {len(lanes)} "
          f"lane{'s' if len(lanes) != 1 else ''} (main + {others}) · {state} · "
          f"wall clock {fmt_duration(report['time']['wall'])}\n")
    rows = []
    for meter, line in sorted(report["cost_by_meter"].items(), key=lambda kv: -kv[1]["cost"]):
        if line["tokens"]:
            rows.append([METER_LABELS[meter], fmt_tokens(line["tokens"]),
                         fmt_cost(line["cost"], unit, 4), fmt_share(line["share"])])
    rows.append(["Total", fmt_tokens(sum(l["tokens"] for l in report["cost_by_meter"].values())),
                 fmt_cost(report["cost"], unit, 4), "100%" if report["cost"] else "-"])
    print(table(["Meter", "Tokens", "Cost", "Share"], rows, right={1, 2, 3}))
    print()
    main = lanes[0]
    line = f"Main lane: {fmt_cost(main['cost'], unit, 4)} ({fmt_share(main['cost_share'])})."
    if others:
        line += (f" Other lanes: {fmt_cost(report['cost'] - main['cost'], unit, 4)} "
                 f"({fmt_share(report['ratios']['other_lanes_cost_share'])}).")
    print(line)
    estimate = report["claude_code_estimate"]
    if estimate:
        print(f"Claude Code estimate in the transcript: ${estimate['total_cost_usd']:.4f} "
              "(list price).")
    if report["unpriced_models"]:
        print(f"No rate profile for {', '.join(report['unpriced_models'])}: no cost here.")
    for lane in lanes:
        if not lane["finished"]:
            print(f"WARNING: {lane['name']} is not finished. Collect again later.")


def cmd_session(args) -> int:
    path = find_session(args.session, args.project)
    profiles = load_profiles(args.rates_file)
    override = get_profile(args.rates, args.rates_file) if args.rates else None
    session = load_session(path)
    report = summarize(session, profiles, override, args.window)
    if args.brief and not args.requests and not args.json:
        _print_session_brief(report)
        return 0
    if args.requests:
        report["requests"] = request_rows(session, profiles, override)
        report["miss_vs_hit"] = miss_vs_hit(report["requests"], profiles, override)
        report["measured_miss_vs_hit"] = measured_miss_vs_hit(report["requests"])
        if args.brief and not args.json:
            _print_requests_brief(report)
            return 0
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_session(report)
    return 0


def cmd_reduce(args) -> int:
    given = pathlib.Path(args.session).expanduser() if args.session else None
    if args.codex or (given and given.is_file() and codex.is_codex_file(given)):
        path = given if given and given.is_file() else codex.find_thread(args.session,
                                                                          args.project)
        written = [codex.reduce_file(path, pathlib.Path(args.out))]
        leftovers = codex.leftover_strings(written)
        print(f"Reduced copy of Codex file {path.name}:")
    else:
        path = find_session(args.session, args.project)
        written = reduce_session(path, pathlib.Path(args.out))
        leftovers = leftover_strings(written)
        print(f"Reduced copy of session {path.stem}:")
    for file in written:
        print(f"  {file}")
    print("String values in the copy that are not IDs or time stamps:")
    for value in leftovers:
        print(f"  {value}")
    print("Read this list. Commit the copy only if it has no text and no paths.")
    return 0


CODEX_METER_LABELS = {
    "input": "Input (fresh)",
    "cache_write_5m": "Cache write",
    "cache_write_1h": "Cache write (12 hours)",
    "cache_read": "Cache read",
    "output": "Output",
}


def fmt_percent(value) -> str:
    return "-" if value is None else f"{value:.1f}%"


def fmt_window(minutes) -> str:
    if not minutes:
        return "?"
    if minutes % 10080 == 0:
        return "Weekly" if minutes == 10080 else f"{minutes // 10080} weeks"
    if minutes % 60 == 0:
        return f"{minutes // 60} hours"
    return f"{minutes} minutes"


def fmt_until(seconds) -> str:
    if seconds is None:
        return "-"
    if seconds < 0:
        return "already reset"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return f"in {days}d {hours:02d}h" if days else f"in {hours}h {rest // 60:02d}m"


def _print_codex(report: dict) -> None:
    raw = report["codex_usage"]
    api, credits = report["api"], report["credits"]
    origin = codex.ORIGINATORS.get(report["originator"], report["originator"] or "")
    parts = [f"Codex thread {report['thread_id'] or '?'}"]
    if report["version"]:
        parts.append(f"version {report['version']}")
    if origin:
        parts.append(origin)
    turns = f"{report['turns']} turn{'s' if report['turns'] != 1 else ''}"
    if report["requests"] is not None:
        turns += f", {report['requests']} request{'s' if report['requests'] != 1 else ''}"
    parts.append(turns)
    parts.append("finished" if report["complete"] else "NOT FINISHED")
    print(" · ".join(parts))
    models = ", ".join(m or "unknown" for m in report["models"]) or "unknown"
    print(f"Model {models} · effort {report['effort'] or 'default'} · read from the "
          f"{report['source']}")
    if report["events_match_session_file"] is not None:
        print("The exec events and the session file give the same token totals."
              if report["events_match_session_file"] else
              "WARNING: the exec events and the session file give different token totals.")

    def card_line(name, card):
        if not card["profiles"]:
            return f"{name}: no rate profile"
        checked = ", ".join(card["checked"])
        return f"{name} ({', '.join(card['profiles'])}" + (f", checked {checked})" if checked
                                                            else ")")
    print("Rates: " + card_line("OpenAI API list price", api) + " and "
          + card_line("Codex credits", credits) + "\n")

    print("Tokens as Codex reports them")
    rows = [["input_tokens", fmt_tokens(raw["input_tokens"])],
            ["  cached_input_tokens", fmt_tokens(raw["cached_input_tokens"])],
            ["  cache_write_input_tokens", fmt_tokens(raw["cache_write_input_tokens"])],
            ["output_tokens", fmt_tokens(raw["output_tokens"])],
            ["  reasoning_output_tokens", fmt_tokens(raw["reasoning_output_tokens"])]]
    print(table(["Field", "Tokens"], rows, right={1}))
    print("Cached tokens and cache writes are inside input_tokens. Reasoning is inside "
          "output_tokens.")

    print("\nTokens and cost by token type (fresh input = input - cached - cache writes)")
    rows = []
    for meter in METERS:
        tokens = api["by_meter"][meter]["tokens"] or credits["by_meter"][meter]["tokens"]
        if meter == "cache_write_1h" and not tokens:
            continue
        a, c = api["by_meter"][meter], credits["by_meter"][meter]
        rows.append([CODEX_METER_LABELS[meter], fmt_tokens(report["usage"][meter]),
                     fmt_cost(a["cost"], api["unit"], 4) if api["unit"] else "-",
                     fmt_share(a["share"]),
                     fmt_cost(c["cost"], credits["unit"], 4) if credits["unit"] else "-",
                     fmt_share(c["share"])])
    total_tokens = sum(report["usage"][m] for m in METERS)
    rows.append(["Total", fmt_tokens(total_tokens),
                 fmt_cost(api["cost"], api["unit"], 4) if api["unit"] else "-",
                 "100%" if api["cost"] else "-",
                 fmt_cost(credits["cost"], credits["unit"], 4) if credits["unit"] else "-",
                 "100%" if credits["cost"] else "-"])
    print(table(["Meter", "Tokens", "API list price", "Share", "Codex credits", "Share"], rows,
                right={1, 2, 3, 4, 5}))
    for card in (api, credits):
        if card["long_prompt_rule_skipped"]:
            print(f"The exec events give one total for each turn, so the long-prompt rates of "
                  f"{card['profiles'][0]} are not applied. The session file gives each request.")
            break
    unpriced = sorted(set(api["unpriced_models"] + credits["unpriced_models"]))
    if "(unknown model)" in unpriced:
        print("The exec events do not name the model, and no session file was found. "
              "Give --model, for example --model gpt-6.1-sol.")
    elif unpriced:
        print(f"No rate profile for {', '.join(unpriced)}: their tokens have no cost here. "
              "Add a profile to config/rates.json.")

    time = report["time"]
    if time:
        print("\nTime")
        rows = [[label, fmt_duration(time[key]),
                 fmt_share(time[key] / time["wall"] if time["wall"] else None)]
                for label, key in (("Generating", "generating"), ("Running tools", "tools"),
                                   ("Waiting on you", "waiting"))]
        rows.append(["Wall clock", fmt_duration(time["wall"]), "100%"])
        print(table(["Part", "Time", "Share"], rows, right={1, 2}))

    context = report["context"]
    if context:
        line = (f"\nContext: {fmt_tokens(context['last'])} tokens in the last request, "
                f"{fmt_tokens(context['peak'])} at the peak")
        line += (f", of a {fmt_tokens(context['window'])} window." if context["window"]
                 else ".")
        print(line)

    limits = report["limits"]
    if limits:
        print(f"\nPlan limits{' (plan: ' + limits['plan'] + ')' if limits['plan'] else ''}, "
              "as Codex recorded them after each request")
        rows = []
        for key in ("primary", "secondary"):
            w = limits[key]
            if w:
                rows.append([fmt_window(w["window_minutes"]),
                             fmt_percent(w["used_percent_first"]),
                             fmt_percent(w["used_percent_last"]),
                             fmt_until(w["resets_in_seconds"])])
        print(table(["Window", "After request 1", "At the end", "Resets"], rows,
                    right={1, 2}))
        c = limits["credits"]
        if c:
            print(f"Credits: balance {c.get('balance') or '0'}"
                  + (", unlimited" if c.get("unlimited") else "") + ".")
        print("This share is the cost on a subscription. Compare it with /status before "
              "and after the run.")
    elif report["source"] == "exec events":
        print("\nNo session file: no time, context, or plan limits. Keep the Codex session "
              "file, or set CODEX_HOME.")

    ratios = report["ratios"]
    print("\nRatios")
    rows = [["Cache reads ÷ all input", fmt_share(ratios["cache_hit"])],
            ["Reasoning ÷ output", fmt_share(ratios["reasoning_share_of_output"])],
            ["Output ÷ cost (API list price)", fmt_share(ratios["output_cost_share_api"])],
            ["Output ÷ cost (Codex credits)", fmt_share(ratios["output_cost_share_credits"])]]
    if context:
        rows.append(["Context used ÷ window (last request)", fmt_share(ratios["context_used"])])
    if time:
        rows.append(["Generation ÷ wall time", fmt_share(ratios["generation_to_wall"])])
    print(table(["Ratio", "Value"], rows, right={1}))

    print()
    figures = []
    if api["unit"]:
        figures.append(f"{fmt_cost(api['cost'], api['unit'], 4)} at API list price")
    if credits["unit"]:
        figures.append(f"{fmt_cost(credits['cost'], credits['unit'], 4)} at Standard speed")
    if figures:
        print(f"This report: {' and '.join(figures)}. Codex shows neither figure.")
        print("OpenAI says: do not use API prices to estimate included usage.")
    if report["failed_turns"]:
        print(f"WARNING: {report['failed_turns']} turn(s) failed or stopped.")
    if not report["complete"]:
        print("WARNING: a turn is not finished. Collect again after all work is done.")


def _print_codex_brief(report: dict) -> None:
    """`tokenomics codex --brief`: the tokens and the cost by token type, at API
    list price and in Codex credits, and the plan limits at the end. At most
    about 15 lines and 100 columns."""
    api, credits = report["api"], report["credits"]
    models = ", ".join(m or "unknown" for m in report["models"]) or "unknown"
    turns = f"{report['turns']} turn{'s' if report['turns'] != 1 else ''}"
    parts = [f"Codex thread {(report['thread_id'] or '?')[:8]}", models,
             f"effort {report['effort'] or 'default'}", turns,
             "finished" if report["complete"] else "NOT FINISHED"]
    if report["time"]:
        parts.append(f"wall clock {fmt_duration(report['time']['wall'])}")
    print(" · ".join(parts) + "\n")
    rows = []
    for meter in METERS:
        tokens = api["by_meter"][meter]["tokens"] or credits["by_meter"][meter]["tokens"]
        if meter == "cache_write_1h" and not tokens:
            continue
        a, c = api["by_meter"][meter], credits["by_meter"][meter]
        rows.append([CODEX_METER_LABELS[meter], fmt_tokens(report["usage"][meter]),
                     fmt_cost(a["cost"], api["unit"], 4) if api["unit"] else "-",
                     fmt_cost(c["cost"], credits["unit"], 4) if credits["unit"] else "-"])
    rows.append(["Total", fmt_tokens(sum(report["usage"][m] for m in METERS)),
                 fmt_cost(api["cost"], api["unit"], 4) if api["unit"] else "-",
                 fmt_cost(credits["cost"], credits["unit"], 4) if credits["unit"] else "-"])
    print(table(["Meter", "Tokens", "API list price", "Codex credits"], rows,
                right={1, 2, 3}))
    print(f"\nReasoning ÷ output: {fmt_share(report['ratios']['reasoning_share_of_output'])}. "
          f"Cache reads ÷ all input: {fmt_share(report['ratios']['cache_hit'])}.")
    limits = report["limits"]
    if limits:
        used = [f"{fmt_window(limits[k]['window_minutes'])} "
                f"{fmt_percent(limits[k]['used_percent_last'])}"
                for k in ("primary", "secondary") if limits[k]]
        print(f"Plan limits used at the end: {', '.join(used)}.")
    unpriced = sorted(set(api["unpriced_models"] + credits["unpriced_models"]))
    if unpriced:
        print(f"No rate profile for {', '.join(unpriced)}: no cost here. Give --model.")
    if report["failed_turns"]:
        print(f"WARNING: {report['failed_turns']} turn(s) failed or stopped.")
    if not report["complete"]:
        print("WARNING: a turn is not finished. Collect again after all work is done.")


def cmd_codex(args) -> int:
    thread, events = codex.load(args.source, args.project, args.model or "")
    report = codex.summarize(thread, load_profiles(args.rates_file), events)
    if args.brief and not args.json:
        _print_codex_brief(report)
        return 0
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_codex(report)
    return 0


def _cell(value: str, kind: str) -> str:
    if value in ("", None):
        return "-"
    if kind == "tokens":
        return fmt_tokens(float(value))
    if kind == "usd":
        return f"${float(value):.4f}"
    if kind == "credits":
        return f"{float(value):.2f}"
    if kind == "share":
        return f"{float(value):.0%}"
    if kind == "seconds":
        return fmt_duration(float(value))
    return value


def cmd_log(args) -> int:
    rows = baseline.read_log(args.log)
    if not rows:
        raise FileNotFoundError(f"no rows in {args.log}. Run bin/run-baseline first.")
    if args.task:
        rows = [r for r in rows if r["task"] == args.task]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    shown = pathlib.Path(args.log).resolve()
    try:
        shown = shown.relative_to(pathlib.Path.cwd())
    except ValueError:
        pass
    if args.brief:
        return _print_log_brief(rows, shown)
    print(f"Results log {shown} · {len(rows)} run{'s' if len(rows) != 1 else ''}\n")
    variants = any(r.get("variant") for r in rows)
    # Mark the rows with an old configuration (see runconfig.py). The column
    # shows only when the log has such rows.
    for r in rows:
        if r.get("tool") in runconfig.CONFIG_FILES:
            config = runconfig.row_config(r)
            current = runconfig.current_config(r["tool"], r.get("task"), r.get("model"))
            r["config_shown"] = "current" if config == current else f"old {config[:6]}"
    configs = any(r.get("config_shown", "current") != "current" for r in rows)
    columns = [("Task", "task", ""), ("Tool", "tool", ""), ("Model", "model", ""),
               ("Effort", "effort", "")] + ([("Variant", "variant", "")] if variants else []) \
        + ([("Config", "config_shown", "")] if configs else []) \
        + [("Rep", "rep", ""), ("Tests", "tests_pass", ""),
               ("Rules", "rules_followed", ""), ("Input", "input", "tokens"),
               ("Cache write", "cache_write", "tokens"), ("Cache read", "cache_read", "tokens"),
               ("Output", "output", "tokens"), ("Cache hit", "cache_hit", "share"),
               ("List price", "cost_list_usd", "usd"), ("Credits", "cost_credits", "credits"),
               ("Wall", "wall_seconds", "seconds")]
    table_rows = [[_cell(r.get(key, ""), kind) for _, key, kind in columns] for r in rows]
    first_number = 7 + variants + configs
    print(table([c[0] for c in columns], table_rows,
                right=set(range(first_number, len(columns)))))
    broken = [(r["run_id"], r["rules_broken"]) for r in rows if r.get("rules_broken")]
    notes = [(r["run_id"], r["notes"]) for r in rows if r.get("notes")]
    if broken:
        print("\nRules broken:")
        for run_id, text in broken:
            print(f"  {run_id}: {text}")
    if notes:
        print("\nNotes:")
        for run_id, text in notes:
            print(f"  {run_id}: {text}")
    if configs:
        print("\nConfig: 'old' rows used another version of the checked-in configuration "
              "(baseline/config/). They are a different setup. `tokenomics index` does not "
              "count them with the current rows, and the repetition numbers start again at 1.")
    print("\nCompare in one unit at a time. Token counts of different vendors are not the "
          "same unit.")
    return 0


LOG_BRIEF_ROWS = 8  # `tokenomics log --brief` shows the newest runs only


def _print_log_brief(rows: list[dict], shown) -> int:
    """`tokenomics log --brief`: one short line for each run, newest last: the
    result, all tokens, the list-price cost, and the wall time. It shows the
    newest LOG_BRIEF_ROWS runs and the file name of the log, not the path. At
    most about 15 lines and 100 columns."""
    tasks = sorted({r["task"] for r in rows})
    one_task = len(tasks) == 1
    print(f"Results log {pathlib.Path(shown).name} · {len(rows)} "
          f"run{'s' if len(rows) != 1 else ''}" + (f" · {tasks[0]}" if one_task else "") + "\n")
    newest = sorted(rows, key=lambda r: (r.get("date", ""), r.get("run_id", "")))
    newest = newest[-LOG_BRIEF_ROWS:]
    old = False
    table_rows = []
    for r in newest:
        setup = " ".join(x for x in (r.get("model"), r.get("effort"), r.get("variant")) if x)
        config = ""
        if r.get("tool") in runconfig.CONFIG_FILES and runconfig.row_config(r) != \
                runconfig.current_config(r["tool"], r.get("task"), r.get("model")):
            config, old = " *", True
        tokens = [float(r[k]) for k in ("input", "cache_write", "cache_read", "output")
                  if r.get(k) not in ("", None)]
        result = "pass" if index_mod.passed(r) else (
            "-" if r.get("tests_pass") in ("", None) else "fail")
        table_rows.append(([] if one_task else [r["task"]])
                          + [r["tool"], setup, f"{r.get('rep', '')}{config}", result,
                             fmt_tokens(sum(tokens)) if tokens else "-",
                             _cell(r.get("cost_list_usd", ""), "usd"),
                             _cell(r.get("wall_seconds", ""), "seconds")])
    headers = ([] if one_task else ["Task"]) + ["Tool", "Setup", "Rep", "Result", "Tokens",
                                                "List price", "Wall"]
    first_number = headers.index("Tokens")
    print(table(headers, table_rows, right=set(range(first_number, len(headers)))))
    notes = ["Result: pass = tests pass and rules followed. Tokens: all token types."]
    if len(rows) > len(newest):
        notes.append(f"The newest {len(newest)} of {len(rows)} runs. Give --task to "
                     "show one task.")
    if old:
        notes.append("* Another configuration than the checked-in one.")
    print("\n" + "\n".join(notes))
    return 0


def _shown_path(path) -> str:
    shown = pathlib.Path(path).resolve()
    for base in (pathlib.Path.cwd(), baseline.REPO_ROOT):
        try:
            return str(shown.relative_to(base))
        except ValueError:
            continue
    return str(shown)


def _index_settings(args, rows):
    """Setups, tasks, hourly rate, and rework minutes, from --sweep and the options."""
    sweep = sweep_mod.load_sweep(args.sweep) if args.sweep else None
    if sweep:
        setups = list(sweep.setups)
        tasks = list(sweep.tasks) or sorted({r["task"] for r in rows})
        hourly_rate = sweep.hourly_rate
        rework = dict(sweep.rework_minutes)
    else:
        if not args.baseline:
            raise ValueError("give --sweep FILE, or --baseline MODEL/EFFORT for each tool")
        bases = set()
        for text in args.baseline:
            model, _, effort = text.partition("/")
            if not effort:
                raise ValueError(f"--baseline {text!r}: give MODEL/EFFORT, for example "
                                 "claude-sonnet-5-5/medium")
            bases.add((model, effort))
        keys = sorted({sweep_mod.row_key(r) for r in rows
                       if r["tool"] in sweep_mod.AGENT_TOOLS})
        setups = [sweep_mod.Setup(t, m, e, baseline=(m, e) in bases and not v, variant=v)
                  for t, m, e, v in keys]
        tasks = sorted({r["task"] for r in rows})
        hourly_rate, rework = 0.0, {}
    if args.hourly_rate is not None:
        hourly_rate = args.hourly_rate
    for text in args.rework_minutes or []:
        task, _, value = text.rpartition("=")
        rework[task or "*"] = float(value)
    if args.task:
        tasks = [t for t in tasks if t in sweep_mod.split_list(args.task)]
    if args.tool:
        setups = [s for s in setups if s.tool == args.tool]

    def rework_for(task):
        return float(rework.get(task, rework.get("*", 0)))

    return sweep, setups, tasks, hourly_rate, rework_for


def _fmt_index(value) -> str:
    """1.234 -> 1.23×. Exactly 0 shows as 0×. Another value below 0.01 shows as <0.01×, not as 0.00×."""
    if value is None:
        return "-"
    if value == 0:
        return "0×"
    return "<0.01×" if value < 0.01 else f"{value:.2f}×"


def _fmt_median_tokens(value) -> str:
    return "-" if value is None else fmt_tokens(value)


def cmd_index(args) -> int:
    rows = baseline.read_log(args.log)
    if not rows:
        raise FileNotFoundError(f"no rows in {args.log}. Run bin/run-baseline first.")
    sweep, setups, tasks, hourly_rate, rework_for = _index_settings(args, rows)
    report, no_usage = index_mod.index_log(rows, setups, tasks, hourly_rate, rework_for)
    if not report:
        raise ValueError("no runs of these setups in the log")
    last_date = max(r["date"] for r in rows)

    written = []
    if args.scenario_dir:
        out_dir = pathlib.Path(args.scenario_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for task_index in report:
            path = out_dir / f"measured-{task_index.task}.json"
            data = index_mod.scenario(task_index, hourly_rate, _shown_path(args.log), last_date)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
            written.append(path)

    if args.brief and not args.json:
        if any(r.local for t in report for ti in t.tools for r in ti.results):
            return _print_index_brief(report, hourly_rate, last_date, args)
        return _print_index_compact(report, hourly_rate, no_usage, args)

    if args.json:
        print(json.dumps({
            "log": _shown_path(args.log), "hourly_rate": hourly_rate,
            "tasks": [{"task": t.task, "rework_minutes": t.rework_minutes,
                       "other_state": t.other_state,
                       "tools": [{"tool": ti.tool, "config": ti.config,
                                  "current_config": ti.current_config,
                                  "local_config": ti.local_config,
                                  "local_current_config": ti.local_current_config,
                                  "other_config": ti.other_config,
                                  "cost_spread": ti.cost_spread,
                                  "time_spread": ti.time_spread,
                                  "results": [r.to_dict() for r in ti.results]}
                                 for ti in t.tools]} for t in report],
            "no_usage": [r["run_id"] for r in no_usage],
            "scenarios": [str(p) for p in written]}, indent=2, ensure_ascii=False))
        return 0

    print(f"Index to the baseline · {_shown_path(args.log)} · runs to {last_date}")
    print(f"Medians of each setup. Cost per success: median list-price cost / pass rate, "
          f"plus rework at {fmt_cost(hourly_rate, 'USD', 0)}/hour for each failure.")
    any_few = False
    for task_index in report:
        print(f"\n{task_index.task} · rework {task_index.rework_minutes:g} min for each "
              "failed run\n")
        table_rows = []
        variants = any(r.variant for ti in task_index.tools for r in ti.results)
        for ti in task_index.tools:
            for r in ti.results:
                any_few = any_few or (r.runs < index_mod.MIN_RUNS and not r.baseline)
                table_rows.append(
                    [r.tool, r.model, r.effort] + ([r.variant or "-"] if variants else [])
                    + [str(r.runs), f"{r.passes} of {r.runs}"]
                    + ([_fmt_median_tokens(r.cache_write), _fmt_median_tokens(r.cache_read)]
                       if args.cache else [])
                    + ["-" if r.token_cost is None else f"${r.token_cost:.4f}",
                       "-" if r.cost_per_success is None else f"${r.cost_per_success:.4f}",
                       _fmt_index(r.cost_index),
                       "-" if r.wall_seconds is None else fmt_duration(r.wall_seconds),
                       _fmt_index(r.time_index), r.verdict or "no baseline runs"])
        headers = (["Tool", "Model", "Effort"] + (["Variant"] if variants else [])
                   + ["Runs", "Passes"] + (["Cache write", "Cache read"] if args.cache else [])
                   + ["Tokens/attempt", "Cost/success", "Cost index", "Wall", "Time index",
                      "Verdict"])
        print(table(headers, table_rows, right=set(range(headers.index("Runs"),
                                                         len(headers) - 1))))
        print()
        for ti in task_index.tools:
            if ti.baseline is None:
                print(f"  {ti.tool}: the baseline setup has no runs of this task.")
                continue
            b = ti.baseline
            cost = "-" if ti.cost_spread is None else f"±{ti.cost_spread:.0%}"
            time_ = "-" if ti.time_spread is None else f"±{ti.time_spread:.0%}"
            print(f"  {ti.tool} baseline {b.model} · {b.effort}"
                  + (f" · {b.variant}" if b.variant else "") + f", {b.passes} passing runs: "
                  f"spread of cost {cost}, of time {time_}")
        for ti in task_index.tools:
            if ti.config and ti.config != ti.current_config:
                print(f"  {ti.tool}: these runs used the old configuration {ti.config}. The "
                      f"checked-in configuration is now {ti.current_config}. Make the runs "
                      "again.")
            if ti.local_config and ti.local_config != ti.local_current_config:
                print(f"  {ti.tool} local: these runs used the old configuration "
                      f"{ti.local_config}. The checked-in configuration is now "
                      f"{ti.local_current_config}. Make the runs again.")
            if ti.other_config:
                print(f"  {ti.tool}: {ti.other_config} run{'' if ti.other_config == 1 else 's'}"
                      " with another configuration not counted.")
        if task_index.other_state:
            print(f"  Warning: {task_index.other_state} runs used another workload tree or "
                  "prompt than the baseline runs. They are not the same test.")
    print()
    if any_few:
        print(f"* Fewer than {index_mod.MIN_RUNS} runs: an anecdote, not a finding.")
    print("Each tool is indexed to its own baseline. Inside the spread, call it a tie.")
    print("The cost is list-price dollars, not what a subscription pays.")
    if no_usage:
        print(f"Not counted (no usage, the agent did not start): "
              + ", ".join(r["run_id"] for r in no_usage))
    for path in written:
        print(f"Wrote {_shown_path(path)}")
    return 0


def _setup_name(r) -> str:
    """The sweep label of a setup, else 'tool model effort'."""
    return r.label or f"{r.tool} {r.model} {r.effort}" + (f" {r.variant}" if r.variant else "")


def _print_index_brief(report, hourly_rate: float, last_date: str, args) -> int:
    """`tokenomics index --brief`: one short table for each task.
    Cost per success attended: you wait for each run. Unattended: batch work,
    nobody waits. At most about 15 lines and 100 columns for one task."""
    for number, task_index in enumerate(report):
        if number:
            print()
        print(f"{task_index.task} · runs to {last_date} · rework "
              f"{task_index.rework_minutes:g} min at {fmt_cost(hourly_rate, 'USD', 0)}/hour\n")
        rows = []
        stars = False
        others = 0
        for ti in task_index.tools:
            others += ti.other_config
            for r in ti.results:
                star = " *" if r.runs < index_mod.MIN_RUNS else ""
                stars = stars or bool(star)
                rows.append([
                    _setup_name(r), f"{r.passes} of {r.runs}{star}",
                    "-" if r.token_cost is None else ("$0 (local)" if r.local and
                                                      not r.token_cost
                                                      else f"${r.token_cost:.4f}"),
                    "-" if r.wall_seconds is None else fmt_duration(r.wall_seconds),
                    "never passed" if r.cost_per_success_attended is None
                    else f"${r.cost_per_success_attended:.2f}",
                    "never passed" if r.cost_per_success is None
                    else f"${r.cost_per_success:.2f}"])
        headers = ["Setup", "Passes", "Tokens/run", "Time/run", "Per success, attended",
                   "unattended"]
        print(table(headers, rows, right=set(range(1, len(headers)))))
        print("\nAttended: you wait, so the time costs your rate. Unattended: nobody waits.")
        notes = []
        if stars:
            notes.append(f"* fewer than {index_mod.MIN_RUNS} runs")
        if others:
            notes.append(f"{others} run{'' if others == 1 else 's'} of another configuration "
                         "not counted")
        if notes:
            print("; ".join(notes)[0].upper() + "; ".join(notes)[1:] + ".")
    return 0


INDEX_BRIEF_WIDTH = 100  # `index --brief` without local setups: the widest table
INDEX_BRIEF_LINES = 15   # blank lines between the parts only up to this length

# The columns of `index --brief` without local setups: (name, one header line,
# two header lines). A two-line header makes the column as narrow as its values.
_COMPACT_COLUMNS = {
    "tool": ("Tool", "Tool"), "model": ("Model", "Model"), "effort": ("Effort", "Effort"),
    "variant": ("Variant", "Variant"), "passes": ("Passes", "Passes"),
    "cache_write": ("Cache write", ("Cache", "write")),
    "cache_read": ("Cache read", ("Cache", "read")),
    "cost": ("Cost/success", ("Cost per", "success")),
    "cost_index": ("Cost index", ("Cost", "index")),
    "time_index": ("Time index", ("Time", "index")),
    "verdict": ("Verdict", "Verdict"),
}
_IDENTITY = ("tool", "model", "effort", "variant")
_LEFT = {"tool", "model", "effort", "variant", "verdict"}


def _compact_cells(r, cache: bool) -> dict:
    cells = {"tool": r.tool, "model": r.model, "effort": r.effort,
             "variant": r.variant or "-", "passes": f"{r.passes} of {r.runs}",
             "cost": "-" if r.cost_per_success is None else f"${r.cost_per_success:.4f}",
             "cost_index": _fmt_index(r.cost_index), "time_index": _fmt_index(r.time_index),
             "verdict": r.verdict or "no baseline runs"}
    if cache:
        cells.update(cache_write=_fmt_median_tokens(r.cache_write),
                     cache_read=_fmt_median_tokens(r.cache_read))
    return cells


def _compact_table(results, cache: bool, variants: bool):
    """The table of one task, and the identity values that every row has in
    common (they go on the task line). The view takes the first layout that
    fits in INDEX_BRIEF_WIDTH columns:
      1. all columns, one header line
      2. all columns, two header lines
      3. a tool, model, effort, or variant that is the same in every row moves
         to the task line, one header line
      4. the same, two header lines
      5. also no Tool column (the model names the tool)
      6. also no Time index column
    """
    names = (["tool", "model", "effort"] + (["variant"] if variants else []) + ["passes"]
             + (["cache_write", "cache_read"] if cache else [])
             + ["cost", "cost_index", "time_index", "verdict"])
    cells = [_compact_cells(r, cache) for r in results]
    same = [n for n in _IDENTITY if n in names and len({c[n] for c in cells}) == 1]
    moved = [n for n in names if n not in same]
    layouts = [(names, False), (names, True), (moved, False), (moved, True),
               ([n for n in moved if n != "tool"], True),
               ([n for n in moved if n not in ("tool", "time_index")], True)]
    for columns, two in layouts:
        headers = [_COMPACT_COLUMNS[n][1 if two else 0] for n in columns]
        text = table2(headers, [[c[n] for n in columns] for c in cells],
                      right={i for i, n in enumerate(columns) if n not in _LEFT})
        if max(len(line) for line in text.splitlines()) <= INDEX_BRIEF_WIDTH:
            break
    common = [cells[0][n] for n in same if n not in columns]
    return text, common


def _print_index_compact(report, hourly_rate: float, no_usage, args) -> int:
    """`tokenomics index --brief` for a sweep with no local setups: for each
    task, a task line, a short table (passes, cost per
    success, cost index, time index, verdict; with --cache also the median
    cache tokens), and the spread of each tool's baseline. No title lines."""
    any_few = False
    for number, task_index in enumerate(report):
        if number:
            print()
        results = [r for ti in task_index.tools for r in ti.results]
        variants = any(r.variant for r in results)
        any_few = any(r.runs < index_mod.MIN_RUNS and not r.baseline for r in results)
        text, common = _compact_table(results, args.cache, variants)
        head = " · ".join([task_index.task] + common + [
            f"rework {task_index.rework_minutes:g} min at {fmt_cost(hourly_rate, 'USD', 0)}"
            "/hour"])
        others = [(ti.tool, ti.other_config) for ti in task_index.tools if ti.other_config]
        notes = []
        if others:
            total = sum(n for _, n in others)
            note = (" and ".join(f"{n} {tool}" for tool, n in others)
                    + f" run{'' if total == 1 else 's'} of another configuration not counted")
            if len(head) + 3 + len(note) <= INDEX_BRIEF_WIDTH:
                head += " · " + note
            else:
                notes.append(f"  {note[0].upper()}{note[1:]}.")
        spreads = []
        for ti in task_index.tools:
            if ti.baseline is None:
                spreads.append(f"  {ti.tool}: the baseline setup has no runs of this task.")
                continue
            cost = "-" if ti.cost_spread is None else f"±{ti.cost_spread:.0%}"
            time_ = "-" if ti.time_spread is None else f"±{ti.time_spread:.0%}"
            spreads.append(f"  {ti.tool} baseline, {ti.baseline.passes} passing run"
                           f"{'' if ti.baseline.passes == 1 else 's'}: spread of cost {cost}, "
                           f"of time {time_}")
            if ti.config and ti.config != ti.current_config:
                notes.append(f"  {ti.tool}: old configuration {ti.config} (now "
                             f"{ti.current_config}). Make the runs again.")
        if task_index.other_state:
            notes.append(f"  Warning: {task_index.other_state} runs used another workload "
                         "tree or prompt.")
        if any_few:
            notes.append(f"* Fewer than {index_mod.MIN_RUNS} runs: an anecdote, not a finding.")
        if number == len(report) - 1 and no_usage:
            notes.append(f"  {len(no_usage)} run{'' if len(no_usage) == 1 else 's'} with no "
                         "usage not counted.")
        parts = [[head], text.splitlines(), spreads + notes]
        blank = sum(len(part) for part in parts) + 2 <= INDEX_BRIEF_LINES
        print(("\n\n" if blank else "\n").join("\n".join(part) for part in parts))
    return 0


def _fmt_months(value) -> str:
    return "never" if value is None else f"{value:.1f} months"


def cmd_tco(args) -> int:
    share = parse_number(args.local_passes)
    result = tco_mod.payback(args.hardware, args.cloud_spend, share, args.power)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    usd = lambda v: fmt_cost(v, "USD", 0 if float(v).is_integer() else 2)  # noqa: E731
    shown_share = f"{share:.0%}" + (f" ({args.local_passes})" if "/" in
                                    str(args.local_passes) else "")
    lines = [("Extra hardware cost", usd(result.hardware)),
             ("Cloud spend on those tasks", f"{usd(result.cloud_spend)} / month"),
             ("Share that local passes", shown_share),
             ("Cloud spend replaced", f"{usd(result.replaced)} / month"),
             ("Power", f"{usd(result.power)} / month"),
             ("Payback", _fmt_months(result.months)),
             ("If local passed every task", _fmt_months(result.months_all_pass))]
    print("Payback of local hardware\n")
    for label, value in lines:
        print(f"{label:<28}{value}")
    print()
    if result.months is None:
        print("The spend replaced does not cover the power: local never pays back.")
    elif result.months_all_pass and share < 1:
        print(f"Local passes {share:.0%} of the tasks: payback takes "
              f"{result.months / result.months_all_pass:.1f}× as long.")
    print("On a subscription, the fee shrinks only if you drop a tier.")
    return 0


def fmt_bytes(n) -> str:
    """196 -> 196 B, 25671 -> 25.1 KB (1 KB = 1,024 bytes)."""
    if n is None:
        return "-"
    return f"{n:.0f} B" if n < 1024 else f"{n / 1024:.1f} KB"


def _fmt_count(value) -> str:
    if value is None:
        return "-"
    return f"{value:g}" if isinstance(value, float) else str(value)


def _fmt_result_tokens(value) -> str:
    return "-" if value is None else fmt_tokens(round(value))


TOOL_RESULTS_NOTE = ("Tokens: the prompt of the next request, less the prompt and the output "
                     "of this request.\nThese are API counts, not an estimate.")


def _call_note(c) -> str:
    notes = []
    if c.denied:
        notes.append("denied")
    elif c.error:
        notes.append("error")
    if c.rerun:
        notes.append("re-run, no change before it")
    if c.hook:
        notes.append(f"hook: {fmt_bytes(c.hook.get('bytes_in'))} -> "
                     f"{fmt_bytes(c.hook.get('bytes_out'))}, exit {c.hook.get('exit')}")
    if c.approved_by and c.approved_by != "config":
        notes.append(f"approved by {c.approved_by}")
    return "; ".join(notes)


def _print_one_run(run) -> None:
    s = run.summary()
    print(f"Tool results · run {run.run_id}"
          + (f" · variant {run.variant}" if run.variant else "")
          + (f" · tests pass {run.tests_pass}" if run.tests_pass else ""))
    hook = ", ".join(f"{n} {event}" for event, n in sorted(run.hook_log.items())) or "none"
    print(f"{s['requests']} requests in the main lane, {s['tool_calls']} tool calls. "
          f"Hook log: {hook}.\n")
    rows = [[c.lane, str(c.request), c.tool, c.kind or "", fmt_bytes(c.bytes),
             _fmt_result_tokens(c.tokens), _call_note(c)] for c in run.calls]
    print(table(["Lane", "Request", "Tool", "Kind", "Result", "Tokens", "Note"], rows,
                right={1, 4, 5}))
    print()
    if not run.detail:
        print("This reduced copy is from an older version of the toolkit: it has no result sizes and no "
              "tool kinds.")
    else:
        print(f"Test runs: {s['test_runs']} (re-runs with no change before them: "
              f"{s['reruns']}) · test output {fmt_bytes(s['test_bytes'])}, "
              f"{_fmt_result_tokens(s['test_tokens'])} tokens")
    print(f"All tool results: {_fmt_result_tokens(s['result_tokens'])} tokens")
    print(TOOL_RESULTS_NOTE)


def _sweep_runs(args) -> tuple[list[tuple], int]:
    """(task, setup, [RunResults], [missing run IDs]) for each task and setup of the
    sweep, and the number of sweep runs with another configuration. Like
    `tokenomics index`, it counts only one configuration of each tool."""
    sweep = sweep_mod.load_sweep(args.sweep)
    rows = baseline.read_log(args.log)
    configs = index_mod.choose_configs(rows, list(sweep.setups), list(sweep.tasks))
    runs_dir = pathlib.Path(args.runs_dir).expanduser()
    out, other = [], 0
    for task in sweep.tasks:
        for setup in sweep.setups:
            found, missing = [], []
            for row in rows:
                if row["task"] != task or sweep_mod.row_key(row) != setup.key:
                    continue
                if runconfig.row_config(row) != index_mod.config_for(configs, setup.tool, setup.model):
                    other += 1
                    continue
                record = runs_dir / f"{row['run_id']}.record"
                if (record / "run.json").is_file():
                    found.append(toolresults.load_run(record))
                else:
                    missing.append(row["run_id"])
            out.append((task, setup, found, missing))
    return out, other


TOOL_RESULTS_BRIEF_NOTE = "Tokens: measured API counts, not an estimate."
TOOL_RESULTS_BRIEF_HEADERS = ["Test runs", "Re-runs", "Test output", "Test tokens",
                              "All results"]


def _short_run_id(run_id: str) -> str:
    """20261116-222717-app-post-incident-claude -> 20261116-222717. Another
    run ID (for example from --run-id) stays as it is."""
    match = re.match(r"\d{8}-\d{6}(?=-)", run_id or "")
    return match.group(0) if match else run_id


def _tool_results_totals(s: dict) -> list[str]:
    """The per-run totals of `tool-results --brief`, in the order of
    TOOL_RESULTS_BRIEF_HEADERS."""
    return [_fmt_count(s["test_runs"]), _fmt_count(s["reruns"]), fmt_bytes(s["test_bytes"]),
            _fmt_result_tokens(s["test_tokens"]), _fmt_result_tokens(s["result_tokens"])]


def _print_one_run_brief(run) -> None:
    """`tokenomics tool-results RUN --brief`: the totals of one run, no row for
    each tool call. At most about 15 lines and 100 columns."""
    s = run.summary()
    print(f"Tool results · run {_short_run_id(run.run_id)}"
          + (f" · variant {run.variant}" if run.variant else "")
          + (f" · tests pass {run.tests_pass}" if run.tests_pass else ""))
    hook = ", ".join(f"{n} {event}" for event, n in sorted(run.hook_log.items())) or "none"
    print(f"{s['requests']} requests in the main lane, {s['tool_calls']} tool calls. "
          f"Hook log: {hook}.\n")
    print(table(TOOL_RESULTS_BRIEF_HEADERS, [_tool_results_totals(s)],
                right=set(range(len(TOOL_RESULTS_BRIEF_HEADERS)))))
    print()
    if not run.detail:
        print("This reduced copy is from an older version of the toolkit: it has no test runs.")
    print(TOOL_RESULTS_BRIEF_NOTE)


def _print_sweep_brief(groups, other_config: int) -> None:
    """`tokenomics tool-results --sweep F --brief`: one line of totals for each
    run, with no test output in bytes and no medians table. At most about 15 lines and 100 columns for a few
    runs of each setup."""
    for number, task in enumerate(dict.fromkeys(g[0] for g in groups)):
        print(("\n" if number else "") + f"Tool results of the sweep runs · {task}\n")
        rows = []
        for t, setup, found, missing in groups:
            if t != task:
                continue
            label = setup.label + (" (baseline)" if setup.baseline else "")
            for run in found:
                s = run.summary()
                totals = _tool_results_totals(s)
                del totals[2]  # no test output in bytes: the tokens are the measure
                rows.append([label, _short_run_id(s["run_id"]), s["tests_pass"] or "-"]
                            + totals)
            for run_id in missing:
                rows.append([label, _short_run_id(run_id), "no record"] + [""] * 4)
        headers = [h for h in TOOL_RESULTS_BRIEF_HEADERS if h != "Test output"]
        print(table(["Setup", "Run", "Pass"] + headers, rows, right=set(range(3, 7))))
    print()
    if other_config:
        print(f"{other_config} runs with another configuration not counted.")
    print(TOOL_RESULTS_BRIEF_NOTE)


def cmd_tool_results(args) -> int:
    if not args.sweep:
        if not args.run:
            raise ValueError("give a run record (<run ID>.record), a transcript, or --sweep")
        run = toolresults.load_run(args.run)
        if args.json:
            print(json.dumps({"summary": run.summary(),
                              "calls": [toolresults.call_dict(c) for c in run.calls]},
                             indent=2))
        elif args.brief:
            _print_one_run_brief(run)
        else:
            _print_one_run(run)
        return 0

    groups, other_config = _sweep_runs(args)
    if not any(found for _, _, found, _ in groups):
        raise ValueError(f"no records of the sweep runs in {args.runs_dir}")
    if args.brief and not args.json:
        _print_sweep_brief(groups, other_config)
        return 0
    if args.json:
        print(json.dumps([{"task": task, "setup": setup.label, "variant": setup.variant,
                           "baseline": setup.baseline,
                           "runs": [r.summary() for r in found],
                           "medians": toolresults.medians(found) if found else None,
                           "missing_records": missing}
                          for task, setup, found, missing in groups], indent=2))
        return 0

    home = str(pathlib.Path.home())
    print(f"Tool results of the sweep runs · {_shown_path(args.log)} · records in "
          f"{str(pathlib.Path(args.runs_dir).expanduser()).replace(home, '~')}")
    for task in dict.fromkeys(g[0] for g in groups):
        print(f"\n{task}\n")
        rows, medians = [], []
        for t, setup, found, missing in groups:
            if t != task:
                continue
            label = setup.label + (" (baseline)" if setup.baseline else "")
            for run in found:
                s = run.summary()
                rows.append([label, s["run_id"], s["tests_pass"] or "-", str(s["requests"]),
                             _fmt_count(s["test_runs"]), _fmt_count(s["reruns"]),
                             str(s["hook_filtered"]), fmt_bytes(s["test_bytes"]),
                             _fmt_result_tokens(s["test_tokens"]),
                             _fmt_result_tokens(s["result_tokens"])])
            for run_id in missing:
                rows.append([label, run_id, "no record"] + [""] * 7)
            if found:
                m = toolresults.medians(found)
                medians.append([label, str(m["runs"]), _fmt_count(m["requests"]),
                                _fmt_count(m["test_runs"]),
                                f"{m['runs_with_rerun']} of {m['runs']}",
                                str(m["hook_filtered"]), fmt_bytes(m["test_bytes"]),
                                _fmt_result_tokens(m["test_tokens"]),
                                _fmt_result_tokens(m["result_tokens"])])
        print(table(["Setup", "Run", "Pass", "Requests", "Test runs", "Re-runs", "Hook runs",
                     "Test output", "Test tokens", "All results"], rows,
                    right=set(range(3, 10))))
        print("\nMedians of each setup\n")
        print(table(["Setup", "Runs", "Requests", "Test runs", "Runs with a re-run",
                     "Hook runs", "Test output", "Test tokens", "All results"], medians,
                    right=set(range(1, 9))))
    print()
    if other_config:
        print(f"{other_config} runs with another configuration not counted.")
    print("Hook runs: test runs that the hook filtered (hook-logs/ of the record). "
          "Hook runs in the medians table are a total.")
    print(TOOL_RESULTS_NOTE)
    return 0


# --------------------------------------------------------------------------
# Extensions
# --------------------------------------------------------------------------

def _extension_groups(args, sweep) -> tuple[list[tuple], int, int, list[str]]:
    """(setup, pasted, [(row, RunTokens)]) for each setup of the sweep, the runs
    with another configuration, the runs with an older version of the variant
    files (the sha256 in the record differs), and the run IDs with no record."""
    rows = baseline.read_log(args.log)
    configs = index_mod.choose_configs(rows, list(sweep.setups), list(sweep.tasks))
    runs_dir = pathlib.Path(args.runs_dir).expanduser()
    groups, other, old_variant, missing = [], 0, 0, []
    for setup in sweep.setups:
        variant = (baseline.variant_mod.load_variant(setup.variant_dir)
                   if setup.variant_dir else None)
        found = []
        for row in rows:
            if row["task"] not in sweep.tasks or sweep_mod.row_key(row) != setup.key:
                continue
            if runconfig.row_config(row) != index_mod.config_for(configs, setup.tool, setup.model):
                other += 1
                continue
            record = runs_dir / f"{row['run_id']}.record"
            if not (record / "run.json").is_file():
                missing.append(row["run_id"])
                continue
            meta = json.loads((record / "run.json").read_text(encoding="utf-8"))["meta"]
            if variant and meta.get("variant_sha") not in (None, variant.sha):
                old_variant += 1
                continue
            found.append((row, extensions_mod.run_tokens(record)))
        groups.append((setup, bool(variant and variant.paste), found))
    return groups, other, old_variant, missing


def _always_on_view(sweep, contexts: dict) -> None:
    versions = sorted({c.get("claude_version", "") for c in contexts.values()} - {""})
    print("Always-on: what each variant adds to every session, before the first prompt")
    print(f"(/context in a prepared copy, Claude Code {', '.join(versions) or '?'}: "
          "estimates, no model request)\n")
    rows = []
    for setup in sweep.setups:
        ctx = contexts.get(setup.variant)
        variant = (baseline.variant_mod.load_variant(setup.variant_dir)
                   if setup.variant_dir else None)
        if variant and variant.paste:
            rows.append([setup.label, "nothing (you paste it when needed)", "0",
                         "-"])
            continue
        if ctx is None:
            rows.append([setup.label, "no /context file", "-", "-"])
            continue
        a = extensions_mod.always_on(ctx)
        later = []
        if a["deferred_tokens"]:
            later.append(f"{a['deferred_tools']} tool schemas ({fmt_tokens(a['deferred_tokens'])})")
        if any(name.startswith("skill ") for name, _ in a["parts"]):
            later.append("SKILL.md")
        deferred = " + ".join(later) or "-"
        rows.append([setup.label, extensions_mod.up_front_text(a["parts"]),
                     f"{a['tokens']:,}", deferred])
    print(table(["Variant", "Loads up front", "Tokens", "Deferred until used"], rows,
                right={2}))
    print("\nDeferred: tool search loads a tool schema when the agent needs it. A skill loads "
          "its\nbody when it is invoked.")


def cmd_extensions(args) -> int:
    sweep = sweep_mod.load_sweep(args.sweep)
    contexts = extensions_mod.load_contexts(extensions_mod.context_dir(args.sweep))
    if args.always_on:
        if args.json:
            print(json.dumps({s.label: extensions_mod.always_on(contexts[s.variant])
                              for s in sweep.setups if s.variant in contexts}, indent=2))
            return 0
        _always_on_view(sweep, contexts)
        return 0
    groups, other, old_variant, missing = _extension_groups(args, sweep)
    task = sweep.tasks[0] if sweep.tasks else ""
    results = extensions_mod.compare(groups, contexts, sweep.hourly_rate,
                                     sweep.rework_for(task))
    if args.json:
        print(json.dumps({"results": [r.to_dict() for r in results],
                          "other_config": other, "old_variant": old_variant,
                          "missing_records": missing}, indent=2))
        return 0
    if not any(r.runs for r in results):
        raise ValueError(f"no runs of the sweep setups in {_shown_path(args.log)}")
    runs = sorted({r.runs for r in results if r.runs})
    setup = sweep.setups[0]
    counts = (f"{runs[0]}" if len(runs) == 1 else f"{runs[0]} to {runs[-1]}")
    print(f"{task} · {setup.model} · {setup.effort} · medians of {counts} "
          f"run{'' if runs == [1] else 's'} of each variant\n")
    rows = []
    for r in results:
        rows.append([r.label, "-" if r.always_on is None else f"{r.always_on:,}",
                     _fmt_result_tokens(r.when_used),
                     "-" if r.cost is None else f"${r.cost:.3f}",
                     "-" if r.wall is None else f"{r.wall:.0f} s", r.found_text()])
    print(table(["Variant", "Always-on", "When used", "Cost/run", "Time", "Cause found"],
                rows, right={1, 2, 3, 4}))
    print()
    rework = sweep.rework_for(task)
    miss = rework * sweep.hourly_rate / 60
    if all(r.runs and r.found == r.runs for r in results):
        print(f"Every run found the cause: cost per success = cost per run.\nA miss would add "
              f"{rework:g} min of rework at ${sweep.hourly_rate:g}/h (${miss:.2f}).")
    else:
        parts = [f"{r.label} " + ("-" if not r.runs else "never" if r.cost_per_success is None
                                  else f"${r.cost_per_success:.3f}") for r in results]
        print(f"Cost per success (each miss adds {rework:g} min of rework at "
              f"${sweep.hourly_rate:g}/h):\n  " + " · ".join(parts))
    print("Always-on: /context estimate. When used: the largest context of the run minus its "
          "start\nwith the extension idle (API counts). Cost: list-price estimate.")
    if other:
        print(f"{other} runs with another configuration not counted.")
    no_runs = [r.label for r in results if not r.runs]
    if no_runs:
        print(f"No runs yet: {', '.join(no_runs)}.")
    if old_variant:
        print(f"{old_variant} runs with an older version of their variant files not counted.")
    if missing:
        print(f"No record for {len(missing)} runs: {', '.join(missing)}")
    return 0


# --------------------------------------------------------------------------
# Local model on Ollama
# --------------------------------------------------------------------------

def _ollama_settings(args) -> dict:
    try:
        return ollama_mod.load_settings(args.settings)
    except FileNotFoundError:
        if args.settings:
            raise
        return {}


def _secs(value) -> str:
    return "-" if value is None else f"{value:.1f} s"


def cmd_ollama_fit(args) -> int:
    settings = _ollama_settings(args)
    if args.from_file:
        facts = json.loads(pathlib.Path(args.from_file).read_text())
    else:
        model = args.model or settings.get("model")
        if not model:
            raise ValueError("give a model, for example qwen3.5:35b")
        facts = ollama_mod.gather_facts(model, args.log)
    if args.save:
        pathlib.Path(args.save).write_text(json.dumps(facts, indent=2) + "\n")
    result = ollama_mod.fit(facts, args.reserve)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    memory = result["memory_gib"]
    share = (f"   {result['gpu_share']:.0%} of memory" if result["gpu_share"] else "")
    print(f"{facts['model']} · {facts.get('family', '')} · {facts.get('quantization', '')} · "
          f"calls tools: {'yes' if result['calls_tools'] else 'NO'}\n")
    rows = [
        ["Memory of this Mac", "-" if memory is None else f"{memory:.1f} GiB", ""],
        ["GPU memory (server log)", f"{result['gpu_gib']:.1f} GiB", share.strip()],
        ["Context and headroom", f"-{result['reserve_gib']:.1f} GiB", ""],
        ["Weight budget", f"{result['weight_budget_gib']:.1f} GiB", ""],
        ["Bytes per weight (file size / parameters)", f"{result['bytes_per_weight']:.2f}", ""],
        ["Most parameters that fit",
         f"≈{result['max_parameters'] / 1e9:.0f}B", ""],
        [f"This model: {result['parameters'] / 1e9:.1f}B parameters",
         f"{result['weights_gib']:.1f} GiB",
         (f"fits, {result['spare_gib']:.1f} GiB to spare" if result["fits"]
          else f"does NOT fit: {-result['spare_gib']:.1f} GiB over")],
    ]
    width = max(len(r[0]) for r in rows)
    vwidth = max(len(r[1]) for r in rows)
    for label, value, note in rows:
        print(f"{label.ljust(width)}  {value.rjust(vwidth)}  {note}".rstrip())
    return 0


def _bench_view(results, max_first, min_write) -> None:
    tokens = sorted({r["target_tokens"] for r in results})
    size = " or ".join(f"{t / 1000:g}k" for t in tokens)
    print(f"Cold prompt of about {size} tokens, short answer, one run each")
    print(f"Fast enough: first token in {max_first:g} s or less, writing {min_write:g} "
          f"tokens/s or more\n")
    rows = []
    for r in results:
        rows.append([r["model"], r["think"], f"{r['prompt_tokens']:,}",
                     _secs(r.get("first_token_s")), _secs(r.get("first_answer_s")),
                     f"{r['read_tps']:.0f} tok/s" if r.get("read_tps") else "-",
                     f"{r['write_tps']:.1f} tok/s" if r.get("write_tps") else "-",
                     "yes" if ollama_mod.fast_enough(r, max_first, min_write) else "NO"])
    print(table(["Model", "Thinking", "Prompt", "First token", "Answer", "Reading", "Writing",
                 "Fast enough"], rows, right={2, 3, 4, 5, 6}))
    print("\nFirst token: thinking or answer. Answer: the first token of the answer.")


def cmd_ollama_bench(args) -> int:
    settings = _ollama_settings(args)
    limits = settings.get("fast_enough", {})
    max_first = args.max_first_token or limits.get("max_first_token_s", 90)
    min_write = args.min_write or limits.get("min_write_tps", 15)
    if args.from_file:
        results = json.loads(pathlib.Path(args.from_file).read_text())
    else:
        models = args.models or [settings.get("model")]
        if not models[0]:
            raise ValueError("give a model, for example qwen3.5:35b")
        think = ollama_mod.think_value(args.think)
        tokens = args.tokens or limits.get("prompt_tokens", 20000)
        results = [ollama_mod.run_bench(m, tokens, think) for m in models]
        if args.save:
            ollama_mod.save_results(args.save, results)
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    _bench_view(results, max_first, min_write)
    return 0


def _task_view(saved: dict, show_all: bool) -> None:
    rows_in = saved["requests"]
    total = saved["total"]
    count = len(rows_in)
    tool = saved.get("tool", "Claude Code")
    unit = "thread" if tool == "Codex" else "session"
    print(f"{tool} on {saved['model'] or 'the local model'} · {unit} {saved['session']} · "
          f"{count} request{'s' if count != 1 else ''} in the Ollama log")
    print("Local meters from the Ollama server log. No token price: the meter is time.\n")

    def cells(label, r):
        written = "-" if r["written_tokens"] is None else f"{r['written_tokens']:,}"
        if r.get("background"):
            label += " bg"
        return [label, f"{r['new_tokens']:,}", f"{r['cached_tokens']:,}", _secs(r["load_s"]),
                _secs(r["read_s"]), _secs(r["write_s"]), written, _secs(r["total_s"])]

    shown = list(enumerate(rows_in, 1))
    rows = []
    if not show_all and count > 8:
        shown = shown[:4] + [None] + shown[-3:]
    for item in shown:
        rows.append(["…"] + [""] * 7 if item is None else cells(str(item[0]), item[1]))
    rows.append(cells("Total", total))
    print(table(["Request", "New tokens", "Cache hit", "Load", "Read", "Write", "Written",
                 "Total"], rows, right={0, 1, 2, 3, 4, 5, 6, 7}))
    notes = []
    if saved.get("cold_s") and saved.get("later_median_s"):
        cold = rows_in[saved["cold_request"] - 1]
        notes.append(f"Request {saved['cold_request']} read {cold['new_tokens']:,} new tokens "
                     f"in {_secs(cold['read_s'])}. The requests after it read from cache:\n"
                     f"median {_secs(saved['later_median_s'])} each, not "
                     f"{_secs(saved['cold_s'])} "
                     f"({saved['cold_s'] / saved['later_median_s']:.0f}× faster).")
    queued = [(n, r["queued_s"]) for n, r in enumerate(rows_in, 1) if r.get("queued_s")]
    for n, wait in queued:
        notes.append(f"Request {n} waited {_secs(wait)} in the queue: Ollama runs one request "
                     f"at a time.")
    if saved.get("background"):
        notes.append("bg: a background request (not in the session file)." if tool == "Codex"
                     else "bg: a background request (not in the transcript), for example the "
                     "session title.")
    if notes:
        print("\n" + "\n".join(notes))


TASK_BRIEF_ROWS = 6  # `ollama task --brief`: show every request up to this count


def _task_brief_rows(saved: dict) -> list[tuple[str, dict]]:
    """The rows of `ollama task --brief`: every request when there are at most
    TASK_BRIEF_ROWS. Else request 1, the cold request, and the request after
    it. Each run of other requests becomes one row with their sums, labeled
    for example "4-7"."""
    rows_in = saved["requests"]
    if len(rows_in) <= TASK_BRIEF_ROWS:
        return [(str(n), r) for n, r in enumerate(rows_in, 1)]
    cold = saved.get("cold_request") or 1
    keep = {1, cold, cold + 1}
    out: list[tuple[str, dict]] = []
    run: list[int] = []

    def close_run():
        if not run:
            return
        if len(run) == 1:
            out.append((str(run[0]), rows_in[run[0] - 1]))
        else:
            part = [rows_in[n - 1] for n in run]
            written = [r["written_tokens"] for r in part if r["written_tokens"] is not None]
            out.append((f"{run[0]}-{run[-1]}", {
                **{key: sum(r[key] for r in part) for key in
                   ("new_tokens", "cached_tokens", "load_s", "read_s", "write_s", "total_s")},
                "written_tokens": sum(written) if written else None,
                "background": False}))
        run.clear()

    for n, r in enumerate(rows_in, 1):
        if n in keep:
            close_run()
            out.append((str(n), r))
        else:
            run.append(n)
    close_run()
    return out


def _task_brief(saved: dict) -> None:
    """`tokenomics ollama task --brief`: the local meters of
    a task in at most about 12 lines and 100 columns."""
    rows_in = saved["requests"]
    total = saved["total"]
    count = len(rows_in)
    tool = saved.get("tool", "Claude Code")
    unit = "thread" if tool == "Codex" else "session"
    print(f"{tool} on {saved['model'] or 'the local model'} · {unit} {saved['session']} · "
          f"{count} request{'s' if count != 1 else ''} · no token price: the meter is time")

    def cells(label, r):
        written = "-" if r["written_tokens"] is None else f"{r['written_tokens']:,}"
        if r.get("background"):
            label += " bg"
        return [label, f"{r['new_tokens']:,}", f"{r['cached_tokens']:,}", _secs(r["load_s"]),
                _secs(r["read_s"]), _secs(r["write_s"]), written, _secs(r["total_s"])]

    shown = _task_brief_rows(saved)
    rows = [cells(label, r) for label, r in shown]
    rows.append(cells("Total", total))
    print(table(["Request", "New tokens", "Cache hit", "Load", "Read", "Write", "Written",
                 "Total"], rows, right={0, 1, 2, 3, 4, 5, 6, 7}))
    notes = []
    if saved.get("cold_s") and saved.get("later_median_s"):
        cold = rows_in[saved["cold_request"] - 1]
        notes.append(f"Request {saved['cold_request']} read {cold['new_tokens']:,} new tokens "
                     f"in {_secs(cold['read_s'])}. After it: median "
                     f"{_secs(saved['later_median_s'])} each, not {_secs(saved['cold_s'])} "
                     f"({saved['cold_s'] / saved['later_median_s']:.0f}× faster).")
    queued = [(n, r["queued_s"]) for n, r in enumerate(rows_in, 1) if r.get("queued_s")]
    if len(queued) == 1:
        notes.append(f"Request {queued[0][0]} waited {_secs(queued[0][1])} in the queue: "
                     "Ollama runs one request at a time.")
    elif queued:
        notes.append(f"{len(queued)} requests waited in the queue, up to "
                     f"{_secs(max(w for _, w in queued))}: Ollama runs one request at a time.")
    last = []
    if any(r.get("background") for _, r in shown):
        last.append("bg: a background request (not in the session file)." if tool == "Codex"
                    else "bg: a background request (not in the transcript), for example the "
                    "session title.")
    ranges = [label for label, _ in shown if "-" in label]
    if ranges:
        last.append(f"{', '.join(ranges)}: the sum of these requests.")
    if last:
        notes.append(" ".join(last))
    if notes:
        print()
        for note in notes:
            print(textwrap.fill(note, 100, break_long_words=False, break_on_hyphens=False))


def cmd_ollama_task(args) -> int:
    if args.from_file:
        saved = json.loads(pathlib.Path(args.from_file).read_text())
    elif args.codex:
        path = codexlocal_mod.find_thread(args.session, args.project, args.codex_home)
        saved = codexlocal_mod.session_meters(path, ollama_mod.read_log_files(args.log))
        if args.save:
            pathlib.Path(args.save).write_text(json.dumps(saved, indent=2) + "\n")
    else:
        path = find_session(args.session, args.project)
        saved = local_mod.session_meters(path, ollama_mod.read_log_files(args.log))
        if args.save:
            pathlib.Path(args.save).write_text(json.dumps(saved, indent=2) + "\n")
    if args.json:
        print(json.dumps(saved, indent=2))
        return 0
    if args.brief:
        _task_brief(saved)
        return 0
    _task_view(saved, args.all)
    return 0


def cmd_ollama_meters(args) -> int:
    settings = _ollama_settings(args)
    if args.from_file:
        saved = json.loads(pathlib.Path(args.from_file).read_text())
    else:
        model = args.model or settings.get("model")
        if not model:
            raise ValueError("give a model, for example qwen3.5:35b")
        think = (ollama_mod.think_value(args.think) if args.think is not None
                 else settings.get("think"))
        runs = ollama_mod.run_meters(model, args.tokens, think, cold=args.cold)
        saved = {"model": model, "think": ollama_mod.think_label(think), "runs": runs}
        if args.save:
            pathlib.Path(args.save).write_text(json.dumps(saved, indent=2) + "\n")
    if args.json:
        print(json.dumps(saved, indent=2))
        return 0
    first, second = saved["runs"]
    print(f"{saved['model']} · thinking {saved['think']} · the same "
          f"{first['prompt_tokens']:,}-token prompt, two times\n")
    rows = [
        ["Load", "load_duration", _secs(first["load_s"]), _secs(second["load_s"])],
        ["Read", "prompt_eval_duration", _secs(first["read_s"]), _secs(second["read_s"])],
        ["", "  new prompt tokens", f"{first['new_tokens']:,}", f"{second['new_tokens']:,}"],
        ["Cache hit", "prompt_eval_cached_count", f"{first['cached_tokens']:,}",
         f"{second['cached_tokens']:,}"],
        ["Write", "eval_duration", _secs(first["write_s"]), _secs(second["write_s"])],
        ["", "  tokens written", f"{first['written_tokens']:,}",
         f"{second['written_tokens']:,}"],
        ["Total", "total_duration", _secs(first["total_s"]), _secs(second["total_s"])],
    ]
    print(table(["Meter", "Ollama field", "1st request", "2nd request"], rows,
                right={2, 3}))
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokenomics",
        description="Cost tools for AI coding agents: tokens, time, and cost per success.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--rates-file", help="rates file (default: config/rates.json, "
                        "or $TOKENOMICS_RATES)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("rates", help="list the rate profiles")
    p.set_defaults(func=cmd_rates)

    p = sub.add_parser("attempt", help="price one attempt, meter by meter")
    p.add_argument("--rates", required=True, help="rate profile name (see `tokenomics rates`)")
    p.add_argument("--usage", help="JSON file with token counts by meter")
    for meter in METERS + ("reasoning",):
        p.add_argument(f"--{meter.replace('_', '-')}", dest=meter, type=int,
                       help=f"{meter} tokens (overrides --usage)")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_attempt)

    p = sub.add_parser("compare", help="cost per successful task for each option")
    p.add_argument("scenarios", nargs="+", help="scenario JSON file(s)")
    p.add_argument("--set", action="append", metavar="[OPTION.]KEY=VALUE",
                   help="what-if change, for example rework_minutes=0 or "
                        "'Model A.success_rate=70%%' (repeatable)")
    p.add_argument("--brief", action="store_true", help="a short table: the success rate, "
                   "the cost per success, and the ratio to the best option, then the "
                   "cheapest option per attempt and per success")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("session", help="read a Claude Code session: every lane, tokens by "
                       "type, time, context, and cache")
    p.add_argument("session", nargs="?", help="transcript file, session ID, or the start of "
                   "an ID (default: the most recent session of the project)")
    p.add_argument("--project", help="project directory (default: the current directory)")
    p.add_argument("--rates", help="one rate profile for every model, for example relative "
                   "(default: the profile named for each model)")
    p.add_argument("--window", type=int, help="context window size in tokens, from /context")
    p.add_argument("--requests", action="store_true", help="show every request of every lane "
                   "in time order, with its cache result (hit, write, miss) and the cost of "
                   "a miss compared with a hit. Replaces the time, context, and "
                   "ratio sections.")
    p.add_argument("--brief", action="store_true", help="a short table: tokens and cost by "
                   "token type for all lanes, and the cost of the main lane and the other "
                   "lanes. With --requests: the cache result of the first request of each "
                   "lane, of each request after an idle gap, and of each miss, then the "
                   "miss ÷ hit ratio")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("reduce", help="copy a Claude Code session or a Codex file with no "
                       "text, so that you can commit it as a sample")
    p.add_argument("session", nargs="?", help="transcript file, session ID, or the start of "
                   "an ID (default: the most recent session of the project). A Codex file "
                   "(exec events or a session file) is found from its content.")
    p.add_argument("--project", help="project directory (default: the current directory)")
    p.add_argument("--codex", action="store_true", help="find a Codex thread, not a Claude "
                   "Code session")
    p.add_argument("--out", required=True, help="directory for the reduced copy")
    p.set_defaults(func=cmd_reduce)

    p = sub.add_parser("codex", help="read Codex usage: tokens by type, API and credit cost, "
                       "time, context, and plan limits")
    p.add_argument("source", nargs="?", help="a file of `codex exec --json` events, - for "
                   "standard input, a Codex session file, or a thread ID or the start of one "
                   "(default: the most recent Codex thread of the project)")
    p.add_argument("--project", help="project directory (default: the current directory)")
    p.add_argument("--model", help="the model ID, for exec events with no session file "
                   "(replaces the model of every request)")
    p.add_argument("--brief", action="store_true", help="a short table: tokens and cost by "
                   "token type, at API list price and in Codex credits, and the plan limits "
                   "at the end")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_codex)

    p = sub.add_parser("log", help="read the results log of the baseline runs")
    p.add_argument("log", nargs="?", default=str(baseline.DEFAULT_LOG),
                   help="results log CSV (default: results/results-log.csv)")
    p.add_argument("--task", help="show only the runs of one task")
    p.add_argument("--brief", action="store_true", help="a short table of the newest "
                   f"{LOG_BRIEF_ROWS} runs: result, all tokens, list-price cost, and wall time")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("index", help="medians, spread, and index to the baseline of each "
                       "setup in the results log")
    p.add_argument("log", nargs="?", default=str(baseline.DEFAULT_LOG),
                   help="results log CSV (default: results/results-log.csv)")
    p.add_argument("--sweep", help="sweep file: the setups, the baseline of each tool, the "
                   "hourly rate, and the rework minutes of each task")
    p.add_argument("--baseline", action="append", metavar="MODEL/EFFORT",
                   help="with no --sweep: the baseline setup of a tool, for example "
                        "claude-sonnet-5-5/medium (repeatable)")
    p.add_argument("--hourly-rate", type=float, help="your hourly rate (default: from the "
                   "sweep file, or 0)")
    p.add_argument("--rework-minutes", action="append", metavar="[TASK=]MINUTES",
                   help="your time for each failed run (repeatable; default: from the "
                        "sweep file, or 0)")
    p.add_argument("--task", help="only these tasks (comma-separated)")
    p.add_argument("--tool", choices=sweep_mod.AGENT_TOOLS, help="only one tool")
    p.add_argument("--cache", action="store_true", help="also show the median cache write "
                   "and cache read tokens of one run")
    p.add_argument("--scenario-dir", metavar="DIR", help="also write one scenario file for "
                   "each task, measured-<task>.json, for `tokenomics compare`")
    p.add_argument("--brief", action="store_true", help="a short table for each task. "
                   "The view comes from the data. When a setup runs on a local model: "
                   "passes, token cost and time of one run, and cost per "
                   "success attended (the wall time is waiting) and unattended. Else: "
                   "passes, cost per success, cost index, time "
                   "index, verdict, and the spread of each baseline, in at most 100 "
                   "columns. With --cache, also the median cache tokens; then a tool, "
                   "model, effort, or variant that every row has moves to the task line, "
                   "and if necessary the Tool and Time index columns go")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("tco", help="payback months of local hardware")
    p.add_argument("--hardware", type=float, required=True, metavar="USD",
                   help="extra hardware cost: only what you pay beyond the machine you buy "
                        "anyway")
    p.add_argument("--cloud-spend", type=float, required=True, metavar="USD",
                   help="cloud spend on the tasks that local can take, per month")
    p.add_argument("--local-passes", required=True, metavar="SHARE",
                   help="share of those tasks that local passes: 0.67, 67%%, or 2/3")
    p.add_argument("--power", type=float, default=0.0, metavar="USD",
                   help="power per month (default: 0)")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_tco)

    p = sub.add_parser("tool-results", help="tool results and test runs of a Claude Code "
                       "run: size and measured tokens of each result, re-runs, hook log")
    p.add_argument("run", nargs="?", help="a run record directory (<run ID>.record) or a "
                   "transcript file")
    p.add_argument("--sweep", help="sweep file: every run of its setups in the log, and the "
                   "medians of each setup")
    p.add_argument("--log", default=str(baseline.DEFAULT_LOG),
                   help="with --sweep: results log (default: results/results-log.csv)")
    p.add_argument("--runs-dir", default=baseline.os.environ.get("TOKENOMICS_RUNS",
                                                                 "~/tokenomics-runs"),
                   help="with --sweep: where the run records are (default: $TOKENOMICS_RUNS "
                        "or ~/tokenomics-runs)")
    p.add_argument("--brief", action="store_true", help="a short table: the totals of each "
                   "run (test runs, re-runs, test output, test tokens, all results), no row "
                   "for each tool call and no medians table")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_tool_results)

    p = sub.add_parser("extensions", help="compare ways to extend the agent: always-on "
                       "tokens, tokens when used, cost, time, and result")
    p.add_argument("--sweep", required=True, help="sweep file: the variants, the task, and "
                   "the context directory (\"context_dir\")")
    p.add_argument("--always-on", action="store_true", help="only the always-on tokens of "
                   "each variant, from the /context files (run-baseline --sweep F --context)")
    p.add_argument("--log", default=str(baseline.DEFAULT_LOG),
                   help="results log (default: results/results-log.csv)")
    p.add_argument("--runs-dir", default=baseline.os.environ.get("TOKENOMICS_RUNS",
                                                                 "~/tokenomics-runs"),
                   help="where the run records are (default: $TOKENOMICS_RUNS or "
                        "~/tokenomics-runs)")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_extensions)

    p = sub.add_parser("ollama", help="a local model on Ollama: fit, speed, and the local "
                       "meters")
    osub = p.add_subparsers(dest="ollama_command", required=True)

    def ollama_common(q):
        q.add_argument("--settings", help="local model settings (default: "
                       "config/local/ollama.json)")
        q.add_argument("--from", dest="from_file", metavar="FILE",
                       help="read saved results, and make no request")
        q.add_argument("--save", metavar="FILE", help="save the results to a JSON file")
        q.add_argument("--json", action="store_true", help="print JSON")

    q = osub.add_parser("fit", help="does the model fit? The sizing with the GPU "
                        "memory from the server log")
    q.add_argument("model", nargs="?", help="model name (default: from the settings)")
    q.add_argument("--reserve", type=float, default=6.0, metavar="GIB",
                   help="GiB for the context and headroom (default: 6)")
    q.add_argument("--log", help="Ollama server log (default: ~/.ollama/logs/server.log)")
    ollama_common(q)
    q.set_defaults(func=cmd_ollama_fit)

    q = osub.add_parser("bench", help="a cold prompt and a short answer: time to first "
                        "token, reading and writing speed")
    q.add_argument("models", nargs="*", help="model names (default: from the settings)")
    q.add_argument("--tokens", type=int, help="prompt size (default: 20000)")
    q.add_argument("--think", help="thinking: on, off, or a level such as low (default: the "
                   "model default)")
    q.add_argument("--max-first-token", type=float, metavar="SECONDS",
                   help="fast enough: the longest wait for the first token (default: 90)")
    q.add_argument("--min-write", type=float, metavar="TOK/S",
                   help="fast enough: the lowest writing speed (default: 15)")
    ollama_common(q)
    q.set_defaults(func=cmd_ollama_bench)

    q = osub.add_parser("meters", help="the four local meters of the same prompt sent two "
                        "times")
    q.add_argument("model", nargs="?", help="model name (default: from the settings)")
    q.add_argument("--tokens", type=int, default=2000, help="prompt size (default: 2000)")
    q.add_argument("--think", help="thinking: on, off, or a level (default: from the "
                   "settings)")
    q.add_argument("--cold", action="store_true", help="unload the model first, so that "
                   "the first request loads it")
    ollama_common(q)
    q.set_defaults(func=cmd_ollama_meters)

    q = osub.add_parser("task", help="the local meters of each model request of a Claude "
                        "Code session or a Codex thread (--codex), from "
                        "the Ollama server log")
    q.add_argument("session", nargs="?", help="session ID, start of an ID, or .jsonl file "
                   "(default: the latest session of the project)")
    q.add_argument("--codex", action="store_true", help="a Codex thread on the local model, "
                   "in the Codex home of bin/codex-local (config/local/codex.json)")
    q.add_argument("--codex-home", help="with --codex: the sessions directory (default: "
                   "<codex_home>/sessions from config/local/codex.json)")
    q.add_argument("--project", help="project directory (default: the current directory)")
    q.add_argument("--log", help="Ollama server log (default: ~/.ollama/logs/server.log "
                   "and the older server-N.log files)")
    q.add_argument("--all", action="store_true", help="show every request (default: at "
                   "most 8 rows)")
    q.add_argument("--brief", action="store_true", help="a short view: at most about 12 "
                   "lines and 100 columns. With more than 6 requests: request 1, the cold "
                   "request and the request after it, and rows with the sums of the other "
                   "requests")
    ollama_common(q)
    q.set_defaults(func=cmd_ollama_task)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError, FileNotFoundError, FileExistsError,
            json.JSONDecodeError) as error:
        message = error.args[0] if isinstance(error, KeyError) and error.args else error
        print(f"tokenomics: error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
