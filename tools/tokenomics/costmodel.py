"""The cost model.

Two calculations:

1. Token cost of one attempt. Multiply the tokens on each meter by the rate
   of that meter, then add the results.

2. Cost per successful task:

       cost per success = token cost per attempt / success rate
                        + failures per success * rework cost
                        + waiting cost

       failures per success = (1 - success rate) / success rate

   Rework cost is your time to review, diagnose, and redo one failed attempt.
   Waiting cost is time lost to limit resets, slow local runs, and long agent
   loops. Every attempt waits, so waiting per success is the waiting per
   attempt divided by the success rate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .rates import METERS, RateProfile


# --------------------------------------------------------------------------
# 1. Token cost of one attempt
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Usage:
    """Tokens on each meter for one attempt.

    `reasoning` is for information only. Both vendors bill reasoning tokens as
    output, and both include them in the output count, so the cost model does
    not add them again.
    """

    input: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0
    reasoning: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "Usage":
        allowed = set(METERS) | {"reasoning"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ValueError(f"unknown usage field(s) {', '.join(unknown)}; "
                             f"expected some of {', '.join(sorted(allowed))}")
        values = {}
        for key, value in data.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"usage field {key} must be a whole number >= 0, "
                                 f"got {value!r}")
            values[key] = value
        return cls(**values)

    @property
    def total(self) -> int:
        return sum(getattr(self, m) for m in METERS)


@dataclass(frozen=True)
class MeterLine:
    meter: str
    tokens: int
    relative_rate: float
    cost: float
    share: float


@dataclass(frozen=True)
class AttemptCost:
    profile: str
    unit: str
    total: float
    lines: tuple

    def to_dict(self) -> dict:
        return {"profile": self.profile, "unit": self.unit, "total": self.total,
                "lines": [asdict(line) for line in self.lines]}


def attempt_cost(usage: Usage, profile: RateProfile) -> AttemptCost:
    """Price one attempt, meter by meter."""
    costs = {m: getattr(usage, m) * profile.price(m) for m in METERS}
    total = sum(costs.values())
    lines = tuple(
        MeterLine(meter=m, tokens=getattr(usage, m), relative_rate=profile.relative(m),
                  cost=costs[m], share=(costs[m] / total) if total else 0.0)
        for m in METERS
    )
    return AttemptCost(profile=profile.name, unit=profile.unit, total=total, lines=lines)


# --------------------------------------------------------------------------
# 2. Cost per successful task
# --------------------------------------------------------------------------

def failures_per_success(success_rate: float) -> float:
    """(1 - p) / p. On average, the failed attempts you make for each success."""
    check_success_rate(success_rate)
    return (1 - success_rate) / success_rate


def check_success_rate(success_rate: float) -> None:
    if not 0 < success_rate <= 1:
        raise ValueError(f"success rate must be more than 0 and not more than 1, "
                         f"got {success_rate!r}; an option that never succeeds has "
                         "no cost per success")


@dataclass(frozen=True)
class Option:
    """One way to do the task: a tool, a model, and its settings."""

    name: str
    token_cost: float          # token cost of one attempt, in the scenario currency
    success_rate: float        # share of attempts that pass, more than 0 and not more than 1
    rework_minutes: float = 0  # your time for each failed attempt
    wait_minutes: float = 0    # time lost for each attempt: limits, slow runs

    def __post_init__(self):
        check_success_rate(self.success_rate)
        for name in ("token_cost", "rework_minutes", "wait_minutes"):
            if getattr(self, name) < 0:
                raise ValueError(f"{self.name}: {name} must be 0 or more, "
                                 f"got {getattr(self, name)!r}")


@dataclass(frozen=True)
class OptionResult:
    name: str
    token_cost: float
    success_rate: float
    token_cost_per_success: float
    failures_per_success: float
    rework_cost: float
    waiting_cost: float
    cost_per_success: float

    def to_dict(self) -> dict:
        return asdict(self)


def cost_per_success(option: Option, hourly_rate: float) -> OptionResult:
    """Apply the cost-per-success formula to one option."""
    if hourly_rate < 0:
        raise ValueError(f"hourly rate must be 0 or more, got {hourly_rate!r}")
    per_minute = hourly_rate / 60
    p = option.success_rate

    tokens = option.token_cost / p
    failures = failures_per_success(p)
    rework = failures * option.rework_minutes * per_minute
    waiting = option.wait_minutes / p * per_minute

    return OptionResult(
        name=option.name,
        token_cost=option.token_cost,
        success_rate=p,
        token_cost_per_success=tokens,
        failures_per_success=failures,
        rework_cost=rework,
        waiting_cost=waiting,
        cost_per_success=tokens + rework + waiting,
    )
