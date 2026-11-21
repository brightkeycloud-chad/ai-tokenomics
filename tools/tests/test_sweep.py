"""Tests for the sweep options of the harness and `tokenomics index`.
Run from the repository root:

    python3 -m unittest discover -s tools/tests

The tests make no model request and no AWS call.
"""
import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics import baseline, index, sweep  # noqa: E402
from tokenomics.runconfig import LEGACY_CONFIG, current_config  # noqa: E402
from tokenomics.scenario import load_scenario, parse_number  # noqa: E402
from test_costmodel import run_cli  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
MODEL_SWEEP = FIXTURES / "model-effort-sweep"


def row(task="app-post-incident", tool="claude", model="claude-sonnet-5-5", effort="medium",
        cost=0.10, wall=20.0, ok=True, rep=1, tree="t1", prompt="p1", run_id=None, used=True,
        config=None):
    """A results-log row. By default it has the current configuration of its tool."""
    data = {c: "" for c in baseline.COLUMNS}
    data.update({"run_id": run_id or f"r-{task}-{model}-{effort}-{rep}", "date": "2026-11-16",
                 "rep": str(rep), "task": task, "tool": tool, "model": model, "effort": effort,
                 "workload_tree": tree, "prompt_sha": prompt,
                 "config": current_config(tool) if config is None else config,
                 "tests_pass": "yes" if ok else "no", "rules_followed": "yes",
                 "wall_seconds": str(wall)})
    if used:
        data.update({"input": "10", "cost_list_usd": str(cost)})
    return data


def write_sweep(path, setups=None, **extra):
    data = {"title": "test", "tasks": ["app-post-incident"], "repetitions": 3,
            "hourly_rate": 90, "rework_minutes": {"app-post-incident": 10},
            "setups": setups or [
                {"tool": "claude", "model": "claude-sonnet-5-5", "effort": "medium",
                 "baseline": True},
                {"tool": "claude", "model": "claude-haiku-5-5", "effort": "medium"}]}
    data.update(extra)
    path.write_text(json.dumps(data))
    return path


class SweepFileTest(unittest.TestCase):
    def test_sample_sweep_file(self):
        s = sweep.load_sweep(MODEL_SWEEP / "sweep.json")
        self.assertEqual(s.repetitions, 3)
        self.assertEqual(s.hourly_rate, 90)
        self.assertEqual(s.rework_for("app-post-incident"), 10)
        self.assertEqual(s.rework_for("infra-5xx-alarm"), 30)
        self.assertEqual(s.baseline("claude").key, ("claude", "claude-sonnet-5-5", "medium", ""))
        self.assertEqual(s.baseline("codex").key, ("codex", "gpt-6.1-sol", "medium", ""))
        self.assertEqual(len(s.setups), 10)
        for setup in s.setups:
            baseline.check_model_id(setup.model)

    def test_one_baseline_for_each_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_sweep(pathlib.Path(tmp) / "s.json", setups=[
                {"tool": "claude", "model": "claude-sonnet-5-5", "effort": "medium"}])
            with self.assertRaisesRegex(ValueError, "exactly one baseline"):
                sweep.load_sweep(path)

    def test_duplicate_setup(self):
        one = {"tool": "claude", "model": "claude-sonnet-5-5", "effort": "medium",
               "baseline": True}
        with tempfile.TemporaryDirectory() as tmp:
            path = write_sweep(pathlib.Path(tmp) / "s.json", setups=[one, dict(one)])
            with self.assertRaisesRegex(ValueError, "two times"):
                sweep.load_sweep(path)

    def test_split_list(self):
        self.assertEqual(sweep.split_list("a, b,,c"), ["a", "b", "c"])
        self.assertEqual(sweep.split_list(None), [])


class PlanTest(unittest.TestCase):
    setups = [sweep.Setup("claude", "claude-sonnet-5-5", "medium", baseline=True),
              sweep.Setup("claude", "claude-haiku-5-5", "medium")]

    def test_fill_to_the_target(self):
        rows = [row(rep=i) for i in range(1, 6)] + [row(model="claude-haiku-5-5")]
        plan = sweep.plan_sweep(["app-post-incident"], self.setups, rows, 3)
        self.assertEqual([(p.in_log, p.to_run) for p in plan], [(5, 0), (1, 2)])

    def test_one_more_with_no_target(self):
        plan = sweep.plan_sweep(["app-post-incident"], self.setups, [], None)
        self.assertEqual([p.to_run for p in plan], [1, 1])

    def test_rounds(self):
        plan = [sweep.PlanLine("t", self.setups[0], 0, 2), sweep.PlanLine("t", self.setups[1], 0, 1)]
        order = sweep.run_order(plan)
        self.assertEqual([p.setup.model for p in order],
                         ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-sonnet-5-5"])

    def test_cross_and_filter(self):
        setups = sweep.cross_setups("codex", ["gpt-6-luna", "gpt-6.1-sol"], ["low", "high"])
        self.assertEqual(len(setups), 4)
        self.assertEqual(len(sweep.filter_setups(setups, efforts=["low"])), 2)
        self.assertEqual(len(sweep.filter_setups(setups, tool="claude")), 0)


def run_baseline(argv):
    out = io.StringIO()
    with redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()) as err:
        code = baseline.main(argv)
    return code, out.getvalue(), err.getvalue()


class SweepCommandTest(unittest.TestCase):
    def test_dry_run_of_the_sample_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            for i in range(1, 6):
                baseline.append_row(log, row(rep=i))
            code, out, _ = run_baseline(["--sweep", str(MODEL_SWEEP / "sweep.json"), "--dry-run",
                                         "--log", str(log), "--runs-dir", tmp])
            self.assertEqual(code, 0, out)
            self.assertIn("Runs to make: 57 (claude 27, codex 30).", out)
            self.assertIn("Workhorse (baseline)", out)
            self.assertIn("Dry run: no copy and no model request.", out)
            self.assertEqual(list(pathlib.Path(tmp).iterdir()), [log])

    def test_filters_and_reps(self):
        code, out, _ = run_baseline(["--sweep", str(MODEL_SWEEP / "sweep.json"), "app", "--tool",
                                     "codex", "--effort", "low,high", "--reps", "2",
                                     "--dry-run", "--json", "--log", "/nonexistent/log.csv"])
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual([(p["model"], p["effort"], p["to_run"]) for p in plan],
                         [("gpt-6.1-sol", "low", 2), ("gpt-6.1-sol", "high", 2)])

    def test_comma_lists_make_a_sweep(self):
        code, out, _ = run_baseline(["app-post-incident,infra-5xx-alarm", "--tool", "claude",
                                     "--model", "claude-haiku-5-5,claude-opus-5-5", "--effort",
                                     "medium", "--dry-run", "--log", "/nonexistent/log.csv"])
        self.assertEqual(code, 0)
        self.assertIn("Runs to make: 4 (claude 4).", out)

    def test_sweep_refuses_aliases_and_fixed_ids(self):
        code, _, err = run_baseline(["app", "--tool", "claude", "--model", "haiku,opus",
                                     "--effort", "medium", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("alias", err)
        code, _, err = run_baseline(["--sweep", str(MODEL_SWEEP / "sweep.json"), "--run-id", "x",
                                     "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("Do not give", err)

    def test_single_run_is_not_a_sweep(self):
        code, out, _ = run_baseline(["app-post-incident", "--tool", "claude", "--model",
                                     "claude-sonnet-5-5", "--effort", "medium", "--dry-run",
                                     "--log", "/nonexistent/log.csv"])
        self.assertEqual(code, 0)
        self.assertIn("Agent       claude -p", out)
        self.assertNotIn("Runs to make", out)


class IndexMathTest(unittest.TestCase):
    def test_median_and_spread(self):
        self.assertEqual(index.median([3, 1, 2]), 2)
        self.assertAlmostEqual(index.spread([0.9, 1.0, 1.15]), 0.15)
        self.assertIsNone(index.spread([1.0]))

    def test_cost_per_success_uses_the_cost_model_formula(self):
        rows = [row(cost=0.10), row(cost=0.20, ok=False), row(cost=0.30)]
        r = index.summarize_setup("t", sweep.Setup("claude", "m-1", "medium"), rows, 90, 10)
        self.assertEqual((r.runs, r.passes), (3, 2))
        self.assertAlmostEqual(r.token_cost, 0.20)
        # 0.20 / (2/3) + (1/3)/(2/3) * 10 min * $1.50/min = 0.30 + 7.50
        self.assertAlmostEqual(r.cost_per_success, 7.80)

    def setups(self):
        return [sweep.Setup("claude", "claude-sonnet-5-5", "medium", baseline=True),
                sweep.Setup("claude", "claude-haiku-5-5", "medium"),
                sweep.Setup("claude", "claude-opus-5-5", "medium"),
                sweep.Setup("claude", "claude-sonnet-5-5", "low")]

    def test_index_and_verdicts(self):
        rows = [row(cost=c, wall=w, rep=i) for i, (c, w) in
                enumerate([(0.10, 20), (0.11, 21), (0.09, 19), (0.40, 50)], 1)]
        rows[-1]["tests_pass"] = "no"  # a failed baseline run: not in the spread
        rows += [row(model="claude-haiku-5-5", cost=0.02, wall=10, rep=i) for i in (1, 2, 3)]
        rows += [row(model="claude-opus-5-5", cost=0.105, wall=20.5, rep=i) for i in (1, 2, 3)]
        rows += [row(effort="low", cost=0.05, wall=30, ok=False)]
        report, no_usage = index.index_log(rows, self.setups(), ["app-post-incident"], 0,
                                           lambda task: 0)
        self.assertEqual(no_usage, [])
        tool = report[0].tools[0]
        self.assertAlmostEqual(tool.cost_spread, 0.10)  # passing runs 0.09, 0.10, 0.11
        self.assertAlmostEqual(tool.time_spread, 0.05)
        results = {(r.model, r.effort): r for r in tool.results}
        base = results[("claude-sonnet-5-5", "medium")]
        self.assertEqual(base.verdict, "baseline")
        self.assertEqual(base.cost_index, 1.0)
        haiku = results[("claude-haiku-5-5", "medium")]
        self.assertEqual(haiku.verdict, "Cheaper, faster, more reliable")
        # Opus: the same median cost and time, but the baseline failed 1 of 4 runs.
        self.assertEqual(results[("claude-opus-5-5", "medium")].verdict,
                         "Cheaper, more reliable")
        self.assertEqual(results[("claude-sonnet-5-5", "low")].verdict, "Never passed")

    def test_few_runs_get_a_star_and_rows_with_no_usage_do_not_count(self):
        rows = [row(rep=i) for i in (1, 2, 3)]
        rows += [row(model="claude-haiku-5-5", cost=0.10, wall=20)]
        rows += [row(model="claude-opus-5-5", used=False, ok=False, run_id="dead")]
        report, no_usage = index.index_log(rows, self.setups(), ["app-post-incident"], 0,
                                           lambda task: 0)
        haiku = [r for r in report[0].tools[0].results if r.model == "claude-haiku-5-5"][0]
        self.assertEqual(haiku.verdict, "Tie *")
        self.assertEqual([r["run_id"] for r in no_usage], ["dead"])
        self.assertNotIn("claude-opus-5-5", [r.model for r in report[0].tools[0].results])

    def test_other_workload_state_is_counted(self):
        rows = [row(rep=i) for i in (1, 2, 3)] + [row(model="claude-haiku-5-5", tree="t2")]
        report, _ = index.index_log(rows, self.setups(), ["app-post-incident"], 0,
                                    lambda task: 0)
        self.assertEqual(report[0].other_state, 1)

    def test_never_mixes_two_configurations(self):
        old = LEGACY_CONFIG["claude"]
        self.assertNotEqual(old, current_config("claude"))
        # Old runs of every setup, and two current runs of the baseline.
        rows = [row(rep=i, cost=0.50, config=old, run_id=f"old-{i}") for i in (1, 2, 3)]
        rows += [row(model="claude-haiku-5-5", rep=i, config=old) for i in (1, 2, 3)]
        rows += [row(rep=i, cost=0.10, run_id=f"new-{i}") for i in (1, 2)]
        report, _ = index.index_log(rows, self.setups(), ["app-post-incident"], 0,
                                    lambda task: 0)
        tool = report[0].tools[0]
        self.assertEqual(tool.config, current_config("claude"))
        self.assertEqual(tool.other_config, 6)
        self.assertEqual([(r.model, r.runs) for r in tool.results],
                         [("claude-sonnet-5-5", 2)])
        self.assertAlmostEqual(tool.baseline.token_cost, 0.10)

    def test_a_log_with_only_old_runs_indexes_the_old_configuration(self):
        old = LEGACY_CONFIG["claude"]
        rows = [row(rep=i, config=old) for i in (1, 2, 3)]
        rows += [{k: v for k, v in row(model="claude-haiku-5-5").items() if k != "config"}]
        report, _ = index.index_log(rows, self.setups(), ["app-post-incident"], 0,
                                    lambda task: 0)
        tool = report[0].tools[0]
        self.assertEqual(tool.config, old)
        self.assertEqual(tool.current_config, current_config("claude"))
        self.assertEqual(tool.other_config, 0)
        self.assertEqual(sum(r.runs for r in tool.results), 4)

    def test_index_and_log_commands_mark_the_old_configuration(self):
        old = LEGACY_CONFIG["claude"]
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            for i in (1, 2, 3):
                baseline.append_row(log, row(rep=i, config=old, run_id=f"old-{i}"))
            baseline.append_row(log, row(rep=1, run_id="new-1"))
            code, out, _ = run_cli("index", str(log), "--baseline", "claude-sonnet-5-5/medium")
            self.assertEqual(code, 0, out)
            self.assertIn("claude: 3 runs with another configuration not counted.", out)
            self.assertIn("claude-sonnet-5-5  medium     1  1 of 1", out)
            code, out, _ = run_cli("log", str(log))
            self.assertIn(f"old {old[:6]}", out)
            self.assertIn("current", out)
            self.assertIn("Config: 'old' rows used another version", out)


class ScenarioTest(unittest.TestCase):
    def test_fraction_success_rate(self):
        self.assertAlmostEqual(parse_number("5/6"), 5 / 6)
        with self.assertRaises(ValueError):
            parse_number("1/0")

    def test_measured_scenario_gives_the_same_cost_per_success(self):
        rows = [row(rep=i, cost=0.1 + i / 1000) for i in (1, 2, 3)]
        rows += [row(model="claude-haiku-5-5", cost=0.02, rep=i, ok=i != 2) for i in (1, 2, 3)]
        rows += [row(effort="low", ok=False)]
        setups = IndexMathTest.setups(None)
        report, _ = index.index_log(rows, setups, ["app-post-incident"], 90, lambda t: 10)
        data = index.scenario(report[0], 90, "log.csv", "2026-11-16")
        self.assertIn("Never passed, so not here: claude-sonnet-5-5 · low.", data["note"])
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "s.json"
            path.write_text(json.dumps(data))
            scenario = load_scenario(path)
        haiku = [o for o in scenario.options if o.name.startswith("claude-haiku")][0]
        self.assertAlmostEqual(haiku.success_rate, 2 / 3)
        self.assertEqual(haiku.rework_minutes, 10)


class IndexCommandTest(unittest.TestCase):
    def test_text_json_and_scenarios(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            for i in (1, 2, 3):
                baseline.append_row(log, row(rep=i, cost=0.10 + i / 100))
                baseline.append_row(log, row(rep=i, model="claude-haiku-5-5", cost=0.01))
            sweep_file = write_sweep(pathlib.Path(tmp) / "s.json")
            code, out, err = run_cli("index", str(log), "--sweep", str(sweep_file),
                                     "--scenario-dir", tmp)
            self.assertEqual(code, 0, err)
            self.assertIn("Cheaper", out)
            self.assertIn("spread of cost ±8%, of time ±0%", out)
            self.assertIn("rework 10 min", out)
            self.assertTrue((pathlib.Path(tmp) / "measured-app-post-incident.json").is_file())
            code, out, _ = run_cli("compare",
                                   str(pathlib.Path(tmp) / "measured-app-post-incident.json"))
            self.assertIn("Cheapest per success: claude-haiku-5-5 · medium.", out)
            code, out, _ = run_cli("index", str(log), "--sweep", str(sweep_file), "--json")
            data = json.loads(out)
            results = data["tasks"][0]["tools"][0]["results"]
            self.assertEqual([r["verdict"] for r in results], ["baseline", "Cheaper"])

    def test_baseline_option_with_no_sweep_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            for i in (1, 2, 3):
                baseline.append_row(log, row(rep=i))
            code, _, err = run_cli("index", str(log))
            self.assertEqual(code, 2)
            self.assertIn("--baseline", err)
            code, out, err = run_cli("index", str(log), "--baseline",
                                     "claude-sonnet-5-5/medium", "--hourly-rate", "60",
                                     "--rework-minutes", "5")
            self.assertEqual(code, 0, err)
            self.assertIn("rework 5 min", out)
            self.assertIn("$60/hour", out)



class CompareDigitsTest(unittest.TestCase):
    def test_sub_cent_costs_use_4_decimals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "s.json"
            path.write_text(json.dumps({"title": "t", "hourly_rate": 90, "options": [
                {"name": "A", "token_cost": 0.0029, "success_rate": "1/1"},
                {"name": "B", "token_cost": 0.0599, "success_rate": "4/4"}]}))
            code, out, _ = run_cli("compare", str(path))
            self.assertEqual(code, 0)
            self.assertIn("$0.0029", out)
            self.assertIn("$0.0599", out)


if __name__ == "__main__":
    unittest.main()
