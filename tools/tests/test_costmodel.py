"""Tests for the cost model. Run from the repository root:

    python3 -m unittest discover -s tools/tests
"""
import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics import cli  # noqa: E402
from tokenomics.costmodel import (  # noqa: E402
    Option, Usage, attempt_cost, cost_per_success, failures_per_success)
from tokenomics.rates import get_profile, load_profiles  # noqa: E402
from tokenomics.scenario import apply_overrides, load_scenario, parse_number  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SCENARIOS = FIXTURES / "cost-scenarios"
SAMPLE_SESSION = {"input": 1200, "cache_write_5m": 50000, "cache_read": 940000, "output": 5300}


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class AttemptCostTest(unittest.TestCase):
    """A sample session, priced two ways."""

    def test_sample_session_at_sonnet_5_5(self):
        result = attempt_cost(Usage.from_dict(SAMPLE_SESSION), get_profile("claude-sonnet-5-5"))
        self.assertAlmostEqual(result.total, 0.2744, places=4)
        shares = {line.meter: line.share for line in result.lines}
        self.assertAlmostEqual(shares["cache_write_5m"], 0.46, places=2)
        self.assertAlmostEqual(shares["cache_read"], 0.34, places=2)
        self.assertAlmostEqual(shares["output"], 0.19, places=2)
        self.assertLess(shares["input"], 0.01)

    def test_sample_session_with_relative_multipliers(self):
        result = attempt_cost(Usage.from_dict(SAMPLE_SESSION), get_profile("relative"))
        self.assertAlmostEqual(result.total, 184_200)
        shares = {line.meter: line.share for line in result.lines}
        self.assertAlmostEqual(shares["cache_read"], 0.51, places=2)
        self.assertAlmostEqual(shares["cache_write_5m"], 0.34, places=2)
        self.assertAlmostEqual(shares["output"], 0.14, places=2)
        self.assertLess(shares["input"], 0.01)

    def test_reasoning_is_not_added_to_the_cost(self):
        profile = get_profile("relative")
        without = attempt_cost(Usage(output=1000), profile).total
        with_reasoning = attempt_cost(Usage(output=1000, reasoning=800), profile).total
        self.assertEqual(without, with_reasoning)

    def test_bad_usage_is_rejected(self):
        with self.assertRaises(ValueError):
            Usage.from_dict({"outputs": 5})
        with self.assertRaises(ValueError):
            Usage.from_dict({"output": -1})
        with self.assertRaises(ValueError):
            Usage.from_dict({"output": 1.5})


class CostPerSuccessTest(unittest.TestCase):
    """The worked example: Model A costs $15.80 per success, Model B $2.78."""

    def test_worked_example(self):
        a = cost_per_success(Option("Model A", 0.40, 0.5, rework_minutes=10), 90)
        b = cost_per_success(Option("Model B", 1.00, 0.9, rework_minutes=10), 90)
        self.assertAlmostEqual(a.token_cost_per_success, 0.80)
        self.assertAlmostEqual(a.failures_per_success, 1.00)
        self.assertAlmostEqual(a.rework_cost, 15.00)
        self.assertAlmostEqual(a.cost_per_success, 15.80)
        self.assertAlmostEqual(b.token_cost_per_success, 1.1111, places=4)
        self.assertAlmostEqual(b.rework_cost, 1.6667, places=4)
        self.assertAlmostEqual(b.cost_per_success, 2.7778, places=4)
        self.assertAlmostEqual(a.cost_per_success / b.cost_per_success, 5.69, places=2)

    def test_waiting_is_paid_on_every_attempt(self):
        r = cost_per_success(Option("x", 0, 0.5, wait_minutes=6), 60)
        self.assertAlmostEqual(r.waiting_cost, 12.0)

    def test_failures_per_success_edges(self):
        self.assertEqual(failures_per_success(1.0), 0)
        self.assertAlmostEqual(failures_per_success(0.25), 3.0)
        for bad in (0, -0.1, 1.01):
            with self.assertRaises(ValueError):
                failures_per_success(bad)


class ScenarioTest(unittest.TestCase):
    def test_parse_number(self):
        self.assertEqual(parse_number("90%"), 0.9)
        self.assertEqual(parse_number("0.9"), 0.9)
        self.assertEqual(parse_number(2), 2.0)
        with self.assertRaises(ValueError):
            parse_number("ninety")

    def test_sample_scenarios_load(self):
        for path in SCENARIOS.glob("*.json"):
            if path.name.endswith(".usage.json"):
                continue
            with self.subTest(path=path.name):
                self.assertTrue(load_scenario(path).options)

    def test_overrides(self):
        scenario = load_scenario(SCENARIOS / "worked-example.json")
        changed = apply_overrides(scenario, ["rework_minutes=0", "Model A.success_rate=70%",
                                             "hourly_rate=120"])
        a, b = changed.options
        self.assertEqual((a.rework_minutes, b.rework_minutes), (0, 0))
        self.assertEqual((a.success_rate, b.success_rate), (0.7, 0.9))
        self.assertEqual(changed.hourly_rate, 120)
        with self.assertRaises(ValueError):
            apply_overrides(scenario, ["Model C.success_rate=1"])
        with self.assertRaises(ValueError):
            apply_overrides(scenario, ["colour=1"])

    def test_usage_option_is_priced_with_a_profile(self):
        data = {"currency": "USD", "options": [
            {"name": "x", "rates": "claude-sonnet-5-5", "usage": SAMPLE_SESSION,
             "success_rate": 1}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
        option = load_scenario(f.name).options[0]
        self.assertAlmostEqual(option.token_cost, 0.2744, places=4)

    def test_currency_mismatch_is_rejected(self):
        data = {"currency": "USD", "options": [
            {"name": "x", "rates": "relative", "usage": SAMPLE_SESSION, "success_rate": 1}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
        with self.assertRaises(ValueError):
            load_scenario(f.name)


class RatesFileTest(unittest.TestCase):
    def test_every_profile_is_valid(self):
        profiles = load_profiles()
        self.assertIn("relative", profiles)
        for profile in profiles.values():
            self.assertEqual(profile.relative("input"), 1)


class CliTest(unittest.TestCase):
    def test_compare_prints_both_options(self):
        code, out, _ = run_cli("compare", str(SCENARIOS / "worked-example.json"))
        self.assertEqual(code, 0)
        self.assertIn("$15.80", out)
        self.assertIn("$2.78", out)
        self.assertIn("5.7× per successful task", out)

    def test_compare_json(self):
        code, out, _ = run_cli("compare", str(SCENARIOS / "worked-example.json"), "--json")
        self.assertEqual(code, 0)
        results = json.loads(out)[0]["results"]
        self.assertAlmostEqual(results[0]["cost_per_success"], 15.80)

    def test_attempt_from_flags(self):
        code, out, _ = run_cli("attempt", "--rates", "claude-sonnet-5-5", "--input", "1200",
                               "--cache-write-5m", "50000", "--cache-read", "940000",
                               "--output", "5300")
        self.assertEqual(code, 0)
        self.assertIn("$0.2744", out)

    def test_errors_go_to_stderr(self):
        code, _, err = run_cli("attempt", "--rates", "no-such-model", "--output", "1")
        self.assertEqual(code, 2)
        self.assertIn("no rate profile named", err)


if __name__ == "__main__":
    unittest.main()
