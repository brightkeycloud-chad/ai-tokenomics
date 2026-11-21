"""Payback of local hardware: `tokenomics tco`.

The formula:

    payback months  = extra hardware cost
                      / (cloud spend replaced - power per month)

    cloud spend replaced = cloud spend on those tasks, per month
                           * share that local passes

Two words in the formula matter:

- Extra: count only what you pay beyond the machine you buy anyway, for
  example more memory.
- Replaced: only the tasks that the local model passes come off the cloud
  bill. A failed local attempt still ends in a cloud run. If local passes a
  third of the tasks, payback takes three times as long.

On a subscription, the fee does not shrink when you run work locally. Local
saves money there only if you drop to a lower tier: then the cloud spend
replaced is the difference between the two tiers. Otherwise local buys
headroom, not savings. On pay-as-you-go, every task that local passes is a
saving.

All inputs are your own numbers. The toolkit keeps no hardware prices.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Payback:
    hardware: float          # extra hardware cost
    cloud_spend: float       # cloud spend on those tasks, per month
    local_share: float       # share of those tasks that local passes, 0 to 1
    power: float             # power per month
    replaced: float          # cloud spend replaced, per month
    net: float               # replaced - power, per month
    months: float | None     # None: it never pays back
    months_all_pass: float | None  # the same if local passed every task

    def to_dict(self) -> dict:
        return asdict(self)


def _months(hardware: float, net: float) -> float | None:
    if hardware == 0:
        return 0.0
    return hardware / net if net > 0 else None


def payback(hardware: float, cloud_spend: float, local_share: float,
            power: float = 0.0) -> Payback:
    """Apply the payback formula."""
    for name, value in (("hardware", hardware), ("cloud spend", cloud_spend),
                        ("power", power)):
        if value < 0:
            raise ValueError(f"{name} must be 0 or more, got {value!r}")
    if not 0 <= local_share <= 1:
        raise ValueError(f"the share that local passes must be from 0 to 1, "
                         f"got {local_share!r}")
    replaced = cloud_spend * local_share
    net = replaced - power
    return Payback(hardware, cloud_spend, local_share, power, replaced, net,
                   _months(hardware, net), _months(hardware, cloud_spend - power))
