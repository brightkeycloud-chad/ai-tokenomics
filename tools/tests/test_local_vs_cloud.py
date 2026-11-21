"""Tests for local and cloud setups in one index, cost per success
attended and unattended, `tokenomics index --brief`, `tokenomics tco`, and
`run-baseline --rebuild --append`. Run from the repository root:

    python3 -m unittest discover -s tools/tests

The tests make no model request and no AWS call.
"""
import json
import pathlib
import sys
import tempfile
import unittest

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics import baseline, index, sweep, tco  # noqa: E402
from tokenomics.runconfig import current_config  # noqa: E402
from test_costmodel import run_cli  # noqa: E402
from test_sweep import row  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LOCAL_VS_CLOUD = FIXTURES / "local-vs-cloud"
LOCAL = "qwen3.5:35b"


def local_row(tool="claude", effort="medium", ok=True, rep=1, wall=200.0, config=None,
              task="app-post-incident"):
    return row(task=task, tool=tool, model=LOCAL, effort=effort, cost=0, wall=wall, ok=ok,
               rep=rep, run_id=f"l-{task}-{tool}-{rep}-{config or 'now'}",
               config=current_config(tool, task, LOCAL) if config is None else config)


def setups():
    return [sweep.Setup("claude", "claude-sonnet-5-5", "medium", "Claude Code, cloud", True),
            sweep.Setup("claude", LOCAL, "medium", "Claude Code, local"),
            sweep.Setup("codex", "gpt-6.1-sol", "medium", "Codex, cloud", True),
            sweep.Setup("codex", LOCAL, "high", "Codex, local")]


def rework(task):
    return {"app-post-incident": 10.0, "infra-5xx-alarm": 30.0}[task]


class ConfigGroupTest(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(index.config_group("claude", "claude-sonnet-5-5"), "claude")
        self.assertEqual(index.config_group("claude", LOCAL), "claude:local")
        self.assertEqual(index.config_group("codex", LOCAL), "codex:local")

    def test_cloud_and_local_have_their_own_configuration(self):
        rows = [row(rep=1), local_row(rep=1)]
        configs = index.choose_configs(rows, setups(), ["app-post-incident"])
        self.assertEqual(configs["claude"], current_config("claude", "app-post-incident"))
        self.assertEqual(configs["claude:local"],
                         current_config("claude", "app-post-incident", LOCAL))
        self.assertNotEqual(configs["claude"], configs["claude:local"])

    def test_one_configuration_in_one_setup(self):
        """An old local configuration never mixes with the current one."""
        rows = [row(rep=r) for r in (1, 2, 3)] + [local_row(rep=1),
                                                  local_row(rep=2, config="0ldl0ca1c0f1")]
        report, _ = index.index_log(rows, setups()[:2], ["app-post-incident"], 90, rework)
        tool = report[0].tools[0]
        local = next(r for r in tool.results if r.local)
        self.assertEqual(local.runs, 1)
        self.assertEqual(tool.other_config, 1)
        self.assertEqual(tool.local_config,
                         current_config("claude", "app-post-incident", LOCAL))


class LocalIndexTest(unittest.TestCase):
    def report(self, rows):
        report, _ = index.index_log(rows, setups(), ["app-post-incident"], 90, rework)
        return {r.label: r for ti in report[0].tools for r in ti.results}

    def test_local_runs_are_counted_next_to_the_cloud_runs(self):
        rows = [row(rep=r, cost=0.10, wall=20) for r in (1, 2, 3)] + \
            [local_row(rep=r, wall=240) for r in (1, 2, 3)]
        results = self.report(rows)
        cloud, local = results["Claude Code, cloud"], results["Claude Code, local"]
        self.assertEqual((cloud.runs, local.runs), (3, 3))
        self.assertTrue(local.local)
        self.assertFalse(cloud.local)
        self.assertEqual(local.token_cost, 0)
        # Unattended: no token cost and no failure, so nothing.
        self.assertEqual(local.cost_per_success, 0)
        # Attended: 4 minutes of waiting at $90/hour.
        self.assertAlmostEqual(local.cost_per_success_attended, 6.0)
        # Cloud attended: $0.10 + 20 s at $90/hour = $0.10 + $0.50.
        self.assertAlmostEqual(cloud.cost_per_success_attended, 0.60)
        self.assertAlmostEqual(local.time_index, 12.0)

    def test_a_failure_is_data(self):
        """A failed local run costs rework, and the waiting of
        every attempt is shared by fewer successes."""
        rows = [row(rep=1)] + [local_row(rep=1, wall=120), local_row(rep=2, wall=120),
                               local_row(rep=3, wall=120, ok=False)]
        local = self.report(rows)["Claude Code, local"]
        self.assertEqual((local.passes, local.runs), (2, 3))
        # 0.5 failures per success * 10 min * $1.50/min = $7.50.
        self.assertAlmostEqual(local.cost_per_success, 7.5)
        # Plus 2 min / (2/3) * $1.50/min = $4.50.
        self.assertAlmostEqual(local.cost_per_success_attended, 12.0)

    def test_never_passed(self):
        rows = [row(rep=1), local_row(rep=1, ok=False)]
        local = self.report(rows)["Claude Code, local"]
        self.assertIsNone(local.cost_per_success)
        self.assertIsNone(local.cost_per_success_attended)
        self.assertEqual(local.verdict, "Never passed")


class BriefTest(unittest.TestCase):
    def test_brief_table(self):
        rows = [row(rep=r, cost=0.10, wall=20) for r in (1, 2, 3)] + \
            [local_row(rep=r, wall=240) for r in (1, 2, 3)] + \
            [local_row(tool="codex", effort="high", rep=1, ok=False)]
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            for r in rows:
                baseline.append_row(log, r)
            code, out, _ = run_cli("index", str(log), "--sweep", str(LOCAL_VS_CLOUD / "sweep.json"),
                                   "--task", "app-post-incident", "--brief")
        self.assertEqual(code, 0)
        self.assertIn("Claude Code, cloud    3 of 3", out)
        self.assertIn("$0 (local)", out)
        self.assertIn("$6.00", out)
        self.assertIn("never passed", out)
        self.assertIn("* fewer than 3 runs", out)
        lines = out.splitlines()
        self.assertLessEqual(len(lines), 15)
        self.assertLessEqual(max(len(line) for line in lines), 100)


class SampleSweepTest(unittest.TestCase):
    def test_sample_sweep_file(self):
        s = sweep.load_sweep(LOCAL_VS_CLOUD / "sweep.json")
        self.assertEqual(s.hourly_rate, 90)
        self.assertEqual(s.rework_for("app-post-incident"), 10)
        self.assertEqual(s.rework_for("infra-5xx-alarm"), 30)
        self.assertEqual(s.baseline("claude").model, "claude-sonnet-5-5")
        self.assertEqual(s.baseline("codex").model, "gpt-6.1-sol")
        local = [x for x in s.setups if x.model == LOCAL]
        self.assertEqual({(x.tool, x.effort) for x in local},
                         {("claude", "medium"), ("codex", "high")})


class TcoTest(unittest.TestCase):
    def test_payback_formula(self):
        result = tco.payback(800, 60, 1 / 3, 3)
        self.assertAlmostEqual(result.replaced, 20)
        self.assertAlmostEqual(result.months, 800 / 17)
        self.assertAlmostEqual(result.months_all_pass, 800 / 57)

    def test_a_third_takes_three_times_as_long_with_no_power(self):
        result = tco.payback(900, 30, 1 / 3)
        self.assertAlmostEqual(result.months / result.months_all_pass, 3)

    def test_never_pays_back(self):
        self.assertIsNone(tco.payback(800, 6, 1 / 3, 3).months)

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            tco.payback(800, 60, 1.5)
        with self.assertRaises(ValueError):
            tco.payback(-1, 60, 0.5)

    def test_command(self):
        code, out, _ = run_cli("tco", "--hardware", "800", "--cloud-spend", "60",
                               "--local-passes", "1/3", "--power", "3")
        self.assertEqual(code, 0)
        self.assertIn("Cloud spend replaced        $20 / month", out)
        self.assertIn("Payback                     47.1 months", out)
        self.assertIn("3.4× as long", out)
        code, out, _ = run_cli("tco", "--hardware", "800", "--cloud-spend", "60",
                               "--local-passes", "50%", "--json")
        self.assertEqual(json.loads(out)["months"], 800 / 30)


class RebuildAppendTest(unittest.TestCase):
    def test_append_refuses_a_run_that_is_in_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            r = local_row()
            baseline.add_rebuilt_row(log, r)
            self.assertEqual(len(baseline.read_log(log)), 1)
            with self.assertRaisesRegex(ValueError, "already has the run"):
                baseline.add_rebuilt_row(log, r)
            self.assertEqual(len(baseline.read_log(log)), 1)


if __name__ == "__main__":
    unittest.main()
