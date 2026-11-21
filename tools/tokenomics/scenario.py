"""Scenario files: the options to compare for one task.

A scenario is a JSON file:

    {
      "title": "What the comparison is about",
      "note": "Where the numbers come from",
      "currency": "USD",
      "hourly_rate": 90,
      "defaults": {"rework_minutes": 10, "wait_minutes": 0},
      "options": [
        {"name": "Model A", "token_cost": 0.40, "success_rate": 0.5},
        {"name": "Model B", "rates": "claude-sonnet-5-5",
         "usage": {"input": 1200, "cache_read": 940000, "output": 5300},
         "success_rate": "90%", "rework_minutes": 5}
      ]
    }

Each option gives its token cost per attempt in one of two ways:
    "token_cost"           a number in the scenario currency, or
    "usage" and "rates"    tokens by meter, priced with a profile from config/rates.json.

Values in "defaults" apply to every option that does not set its own value.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, replace

from .costmodel import Option, Usage, attempt_cost
from .rates import get_profile

OPTION_FIELDS = ("token_cost", "success_rate", "rework_minutes", "wait_minutes")
SCENARIO_FIELDS = ("hourly_rate",)


@dataclass(frozen=True)
class Scenario:
    title: str
    note: str
    currency: str
    hourly_rate: float
    options: tuple
    path: str = ""


def parse_number(value) -> float:
    """Accept 0.9, "0.9", "90%", or "9/10" (passes of runs)."""
    if isinstance(value, bool):
        raise ValueError(f"expected a number, got {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        if text.endswith("%"):
            return float(text[:-1]) / 100
        if "/" in text:
            top, bottom = text.split("/", 1)
            return float(top) / float(bottom)
        return float(text)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"expected a number such as 0.9, 90%, or 9/10, got {value!r}") from None


def _option_from_dict(data: dict, defaults: dict, currency: str, rates_path) -> Option:
    name = data.get("name")
    if not name:
        raise ValueError(f"every option needs a name: {data!r}")
    merged = {**defaults, **data}

    if "token_cost" in merged and "usage" in merged:
        raise ValueError(f"{name}: give token_cost or usage, not both")
    if "usage" in merged:
        if "rates" not in merged:
            raise ValueError(f"{name}: usage needs a rates profile name")
        profile = get_profile(merged["rates"], rates_path)
        if profile.unit != currency:
            raise ValueError(f"{name}: rates profile {profile.name!r} is in {profile.unit}, "
                             f"but the scenario currency is {currency}")
        token_cost = attempt_cost(Usage.from_dict(merged["usage"]), profile).total
    elif "token_cost" in merged:
        token_cost = parse_number(merged["token_cost"])
    else:
        raise ValueError(f"{name}: give token_cost, or usage and rates")

    if "success_rate" not in merged:
        raise ValueError(f"{name}: success_rate is missing")

    return Option(
        name=name,
        token_cost=token_cost,
        success_rate=parse_number(merged["success_rate"]),
        rework_minutes=parse_number(merged.get("rework_minutes", 0)),
        wait_minutes=parse_number(merged.get("wait_minutes", 0)),
    )


def load_scenario(path, rates_path=None) -> Scenario:
    file = pathlib.Path(path)
    data = json.loads(file.read_text())
    currency = data.get("currency", "USD")
    defaults = data.get("defaults", {})
    unknown = sorted(set(defaults) - set(OPTION_FIELDS))
    if unknown:
        raise ValueError(f"{file}: unknown default(s) {', '.join(unknown)}")
    options = tuple(_option_from_dict(o, defaults, currency, rates_path)
                    for o in data.get("options", []))
    if not options:
        raise ValueError(f"{file}: no options to compare")
    names = [o.name.lower() for o in options]
    if len(set(names)) != len(names):
        raise ValueError(f"{file}: option names must be different")
    return Scenario(
        title=data.get("title", file.stem),
        note=data.get("note", ""),
        currency=currency,
        hourly_rate=parse_number(data.get("hourly_rate", 0)),
        options=options,
        path=str(file),
    )


def apply_overrides(scenario: Scenario, overrides: list[str]) -> Scenario:
    """Apply what-if changes from the command line.

    "key=value"              changes every option (or hourly_rate)
    "Option name.key=value"  changes one option
    """
    for text in overrides:
        if "=" not in text:
            raise ValueError(f"--set needs key=value, got {text!r}")
        target, value = text.split("=", 1)
        target = target.strip()
        option_name, _, key = target.rpartition(".")
        number = parse_number(value)

        if key in SCENARIO_FIELDS and not option_name:
            scenario = replace(scenario, hourly_rate=number)
            continue
        if key not in OPTION_FIELDS:
            raise ValueError(f"--set {text!r}: unknown field {key!r}; use one of "
                             f"{', '.join(SCENARIO_FIELDS + OPTION_FIELDS)}")

        matched = False
        options = []
        for option in scenario.options:
            if not option_name or option.name.lower() == option_name.lower():
                option = replace(option, **{key: number})
                matched = True
            options.append(option)
        if not matched:
            raise ValueError(f"--set {text!r}: no option named {option_name!r}; options are "
                             f"{', '.join(o.name for o in scenario.options)}")
        scenario = replace(scenario, options=tuple(options))
    return scenario
