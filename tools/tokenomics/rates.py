"""Rate profiles: what one token on each meter costs.

The toolkit is price-agnostic. A rate profile is data that you look up and
record, not a fact that the code knows. Profiles live in config/rates.json.

A profile uses one of two forms:

    "per_mtok"     Price per million tokens for each meter, in a currency
                   such as USD. Copy these values from the vendor price list.

    "multipliers"  Rate relative to the base input rate (input = 1). The unit
                   of cost is then "input-equivalent tokens": the number of
                   fresh input tokens that cost the same. Use this form to
                   study the shape of a bill without a price list.
"""
from __future__ import annotations

import json
import os
import pathlib
from dataclasses import dataclass, field

# The four meters. A cache write has two lifetimes with
# different prices, so it uses two meters.
METERS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def project_roots(primary: pathlib.Path | None = None) -> list[pathlib.Path]:
    """The roots to search for extra tasks and for run records: the toolkit
    root first, then the git root of the current directory when it is another
    repository (for example a repository that has this toolkit as a git
    submodule). The order matters: the first match wins."""
    import subprocess
    roots = [primary or REPO_ROOT]
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        top = ""
    if top:
        path = pathlib.Path(top).resolve()
        if path not in [p.resolve() for p in roots]:
            roots.append(path)
    return roots
DEFAULT_RATES_FILE = REPO_ROOT / "config" / "rates.json"

MULTIPLIER_UNIT = "input-equivalent tokens"


@dataclass(frozen=True)
class RateProfile:
    """The price of each meter for one model."""

    name: str
    unit: str
    per_token: dict = field(repr=False)
    form: str = "per_mtok"
    source: str = ""
    checked: str = ""
    note: str = ""
    # Optional: a prompt longer than long_prompt_over tokens (input, cache
    # writes, and cache reads together) uses the profile long_prompt_profile.
    long_prompt_over: int = 0
    long_prompt_profile: str = ""

    def price(self, meter: str) -> float:
        """Return the cost of one token on this meter, in self.unit."""
        if meter not in self.per_token:
            raise KeyError(f"rate profile {self.name!r} has no rate for meter "
                           f"{meter!r}; expected all of {', '.join(METERS)}")
        return self.per_token[meter]

    def relative(self, meter: str) -> float:
        """Return the rate of this meter divided by the input rate."""
        return self.price(meter) / self.price("input")

    @property
    def is_currency(self) -> bool:
        return self.form == "per_mtok"


def _profile_from_dict(name: str, data: dict) -> RateProfile:
    if "per_mtok" in data and "multipliers" in data:
        raise ValueError(f"rate profile {name!r} has both per_mtok and multipliers; "
                         "use one of them")
    if "per_mtok" in data:
        form, values, scale = "per_mtok", data["per_mtok"], 1_000_000
        unit = data.get("unit", "USD")
    elif "multipliers" in data:
        form, values, scale = "multipliers", data["multipliers"], 1
        unit = MULTIPLIER_UNIT
    else:
        raise ValueError(f"rate profile {name!r} needs per_mtok or multipliers")

    missing = [m for m in METERS if m not in values]
    if missing:
        raise ValueError(f"rate profile {name!r} has no rate for {', '.join(missing)}")
    per_token = {}
    for meter in METERS:
        value = values[meter]
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"rate profile {name!r}: {meter} must be a number >= 0, "
                             f"got {value!r}")
        per_token[meter] = value / scale
    if per_token["input"] == 0:
        raise ValueError(f"rate profile {name!r}: the input rate must be more than 0")

    long_prompt = data.get("long_prompt") or {}
    over = long_prompt.get("over_tokens", 0)
    if long_prompt and (not isinstance(over, int) or over <= 0 or not long_prompt.get("profile")):
        raise ValueError(f"rate profile {name!r}: long_prompt needs over_tokens (> 0) and profile")

    return RateProfile(name=name, unit=unit, per_token=per_token, form=form,
                       source=data.get("source", ""), checked=data.get("checked", ""),
                       note=data.get("note", ""), long_prompt_over=over,
                       long_prompt_profile=long_prompt.get("profile", ""))


def rates_file(path: str | os.PathLike | None = None) -> pathlib.Path:
    """Resolve the rates file: an explicit path, then $TOKENOMICS_RATES, then the default."""
    if path:
        return pathlib.Path(path)
    if os.environ.get("TOKENOMICS_RATES"):
        return pathlib.Path(os.environ["TOKENOMICS_RATES"])
    return DEFAULT_RATES_FILE


def load_profiles(path: str | os.PathLike | None = None) -> dict[str, RateProfile]:
    """Load every profile in the rates file, keyed by name."""
    file = rates_file(path)
    try:
        data = json.loads(file.read_text())
    except FileNotFoundError:
        raise FileNotFoundError(f"rates file not found: {file}") from None
    profiles = data.get("profiles", {})
    if not profiles:
        raise ValueError(f"rates file {file} has no profiles")
    return {name: _profile_from_dict(name, body) for name, body in profiles.items()}


def get_profile(name: str, path: str | os.PathLike | None = None) -> RateProfile:
    """Load one profile by name."""
    profiles = load_profiles(path)
    if name not in profiles:
        raise KeyError(f"no rate profile named {name!r}; available: "
                       f"{', '.join(sorted(profiles))}")
    return profiles[name]


def select_profile(profiles: dict, model: str, prompt_tokens: int) -> RateProfile | None:
    """Return the profile for one request of `model`, or None.

    `prompt_tokens` is input, cache writes, and cache reads together. When the
    profile has a long-prompt rule and the prompt is longer, the function
    returns the long-prompt profile.
    """
    profile = profiles.get(model)
    if profile and profile.long_prompt_over and prompt_tokens > profile.long_prompt_over:
        profile = profiles.get(profile.long_prompt_profile, profile)
    return profile
