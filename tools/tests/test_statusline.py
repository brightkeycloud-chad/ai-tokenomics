"""Tests for the status line. Run from the repository root:

    python3 -m unittest discover -s tools/tests
"""
from __future__ import annotations
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "statusline"))

import install  # noqa: E402
import statusline  # noqa: E402
from tokenomics.rates import load_profiles, select_profile  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SAMPLES = FIXTURES / "statusline-samples"


def sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text())


def run_main(module, argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = module.main(argv)
            except SystemExit as exc:
                code = exc.code
    finally:
        sys.stdin = old_stdin
    return code, out.getvalue(), err.getvalue()


class SelectProfileTest(unittest.TestCase):
    def test_long_prompt_rule(self):
        profiles = load_profiles()
        self.assertEqual(select_profile(profiles, "claude-haiku-5-5", 100_000).name,
                         "claude-haiku-5-5")
        self.assertEqual(select_profile(profiles, "claude-haiku-5-5", 100_001).name,
                         "claude-haiku-5-5-over-100k")
        self.assertIsNone(select_profile(profiles, "no-such-model", 10))


class ValuesTest(unittest.TestCase):
    def setUp(self):
        self.profiles = load_profiles()

    def test_start_has_no_cost_or_cache(self):
        v = statusline.values(sample("1-start.json"), self.profiles)
        self.assertEqual(v["context"]["percent"], 0)
        self.assertIsNone(v["cache"])
        self.assertIsNone(v["last_request"])
        lines = statusline.render(v, color=False)
        self.assertEqual(len(lines), 3)
        self.assertIn("no cache data yet", lines[1])

    def test_first_request_priced_with_the_cost_model(self):
        v = statusline.values(sample("2-first-request.json"), self.profiles)
        last = v["last_request"]
        self.assertEqual(last["profile"], "claude-haiku-5-5")
        # 1h writes on the main conversation: the 1h meter, not the 5m meter.
        meters = {line["meter"]: line["tokens"] for line in last["lines"]}
        self.assertEqual(meters["cache_write_5m"], 0)
        self.assertEqual(meters["cache_write_1h"], 15_185)
        self.assertAlmostEqual(last["cost"], 0.0035, places=4)
        self.assertAlmostEqual(last["cache_write_share"], 0.87, places=2)

    def test_task_done(self):
        v = statusline.values(sample("3-task-done.json"), self.profiles)
        self.assertAlmostEqual(v["time"]["api_share"], 1.32, places=2)
        self.assertAlmostEqual(v["cache_write_cost"], 25_895 * 0.20 / 1e6)
        cold = v["if_cold"]
        self.assertEqual(cold["tokens"], 54_261)
        self.assertAlmostEqual(cold["times_a_hit"], 20)
        text = "\n".join(statusline.render(v, color=False))
        self.assertIn("hit 91% · 6 requests · 0 misses · 25.9k written ($0.0052)", text)
        self.assertIn("API 34.8s ÷ wall 26.4s = 132%", text)
        self.assertIn("plan 5h 20% · 7d 13%", text)
        self.assertIn("if cold: $0.0109 to re-cache 54.3k (20× a hit)", text)

    def test_cold(self):
        text = "\n".join(statusline.render(
            statusline.values(sample("4-cold.json"), self.profiles), color=False))
        self.assertIn("│ cold", text)
        self.assertIn("next request: $0.0109 to re-cache 54.3k (20× a hit)", text)

    def test_5m_ttl_and_misses(self):
        data = sample("3-task-done.json")
        data["prompt_cache"].update({"ttl": "5m", "misses": 2, "expected_rebuilds": 1,
                                     "last_miss_cause": {"causes": ["ttl_expired_5m"]}})
        v = statusline.values(data, self.profiles)
        meters = {line["meter"]: line["tokens"] for line in v["last_request"]["lines"]}
        self.assertEqual(meters["cache_write_5m"], 1_860)
        self.assertAlmostEqual(v["if_cold"]["times_a_hit"], 12.5)
        text = "\n".join(statusline.render(v, color=False))
        self.assertIn("2 misses", text)
        self.assertIn("1 rebuild after compaction", text)
        self.assertIn("last miss: ttl_expired_5m", text)

    def test_long_prompt_uses_the_long_profile(self):
        data = sample("3-task-done.json")
        data["context_window"]["current_usage"]["cache_read_input_tokens"] = 200_000
        data["prompt_cache"]["recache_tokens_if_cold"] = 201_864
        v = statusline.values(data, self.profiles)
        self.assertEqual(v["last_request"]["profile"], "claude-haiku-5-5-over-100k")
        self.assertAlmostEqual(v["if_cold"]["write_cost"], 201_864 * 1.00 / 1e6)

    def test_model_suffix_and_missing_profile(self):
        data = sample("3-task-done.json")
        data["model"]["id"] = "claude-haiku-5-5[1m]"
        self.assertEqual(statusline.values(data, self.profiles)["last_request"]["profile"],
                         "claude-haiku-5-5")
        data["model"]["id"] = "claude-new-9"
        text = "\n".join(statusline.render(statusline.values(data, self.profiles), color=False))
        self.assertIn("no rate profile for claude-new-9 in config/rates.json", text)

    def test_context_percent_from_tokens_when_null(self):
        data = sample("2-first-request.json")
        data["context_window"]["used_percentage"] = None
        v = statusline.values(data, self.profiles)
        self.assertAlmostEqual(v["context"]["percent"], 4.3334)

    def test_colors(self):
        v = statusline.values(sample("3-task-done.json"), self.profiles)
        self.assertIn("\033[", "\n".join(statusline.render(v, color=True)))
        self.assertNotIn("\033[", "\n".join(statusline.render(v, color=False)))


class WidthTest(unittest.TestCase):
    """Every line fits in 100 columns, on every sample, in color and without
    color, and for a local model: a model ID with no rate profile and a
    5-minute cache."""
    LOCAL = FIXTURES / "claude-local" / "captured" / "statusline.json"

    def setUp(self):
        self.profiles = load_profiles()

    def check(self, data: dict, env: dict | None = None) -> list[str]:
        v = statusline.values(data, self.profiles, env={} if env is None else env)
        plain = statusline.render(v, color=False)
        colored = statusline.render(v, color=True)
        self.assertLessEqual(len(plain), 4, plain)
        for line in plain:
            self.assertLessEqual(len(line), statusline.WIDTH, line)
        self.assertEqual([statusline.visible_width(line) for line in colored],
                         [len(line) for line in plain])
        return plain

    def test_every_sample(self):
        names = sorted(p.name for p in SAMPLES.glob("*.json"))
        self.assertEqual(len(names), 4)
        for name in names:
            with self.subTest(sample=name):
                self.check(sample(name))

    def test_sample_layout(self):
        lines = self.check(sample("3-task-done.json"))
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("Haiku 5.5 medium │ context "))
        self.assertTrue(lines[0].endswith("│ API 34.8s ÷ wall 26.4s = 132%"))
        self.assertTrue(lines[1].startswith("cache (main) │ hit 91% · 6 requests"))
        self.assertEqual(lines[2], "cost │ session $0.0171 (estimate) │ last request $0.0015, "
                                   "cache writes 25% │ plan 5h 20% · 7d 13%")
        self.assertEqual(lines[3], "if cold: $0.0109 to re-cache 54.3k (20× a hit)")
        self.assertEqual(len(self.check(sample("1-start.json"))), 3)

    def test_local_model_with_a_5_minute_cache(self):
        data = json.loads(self.LOCAL.read_text())
        self.assertEqual(data["prompt_cache"]["ttl"], "5m")
        self.assertIsNone(select_profile(self.profiles, data["model"]["id"], 1))
        lines = self.check(data)
        self.assertEqual(len(lines), 4)
        self.assertIn("(5m)", lines[1])
        # With the plan windows too, and with a local base URL in place of a tag.
        data["rate_limits"] = {"five_hour": {"used_percentage": 100},
                               "seven_day": {"used_percentage": 100}}
        lines = self.check(data, env={"ANTHROPIC_BASE_URL": "http://localhost:11434"})
        self.assertIn("plan 5h 100% · 7d 100%", lines[2])

    def test_unknown_cloud_model(self):
        data = sample("3-task-done.json")
        data["model"]["id"] = "claude-new-9"
        lines = self.check(data)
        self.assertEqual(lines[3], "no rate profile for claude-new-9 in config/rates.json")

    def test_long_line_moves_parts_to_a_new_line(self):
        data = sample("4-cold.json")
        data["prompt_cache"].update({"misses": 3, "expected_rebuilds": 2, "last_miss_cause": {
            "causes": ["ttl_expired_1h", "model_changed", "system_prompt_changed"]}})
        v = statusline.values(data, self.profiles, env={})
        lines = statusline.render(v, color=False)
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[1].startswith("cache (main) │ hit 91% · 6 requests · 3 misses"))
        self.assertTrue(lines[2].startswith("2 rebuilds after compaction │ cold │ last miss: "))
        for line in lines:
            self.assertLessEqual(len(line), statusline.WIDTH, line)


class MainTest(unittest.TestCase):
    def test_bad_input_still_prints_a_line(self):
        code, out, _ = run_main(statusline, ["--plain"], "not json")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("tokenomics status line:"))

    def test_json(self):
        code, out, _ = run_main(statusline, ["--json"],
                                (SAMPLES / "3-task-done.json").read_text())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["cache"]["requests"], 6)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "settings.json"
        self.original = {"model": "opusplan", "permissions": {"allow": ["Bash(ls:*)"]}}
        self.path.write_text(json.dumps(self.original, indent=2) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self):
        return json.loads(self.path.read_text())

    def test_install_then_remove_keeps_other_keys(self):
        code, out, _ = run_main(install, ["--settings", str(self.path)])
        self.assertEqual(code, 0)
        self.assertIn("Installed the tokenomics status line.", out)
        data = self.settings()
        self.assertEqual(list(data), ["model", "permissions", "statusLine"])
        self.assertEqual(data["statusLine"]["type"], "command")
        self.assertIn("tools/statusline/statusline.py", data["statusLine"]["command"])

        code, out, _ = run_main(install, ["--settings", str(self.path)])
        self.assertIn("already installed", out)

        code, out, _ = run_main(install, ["--remove", "--settings", str(self.path)])
        self.assertIn("Removed the status line.", out)
        self.assertEqual(self.settings(), self.original)

    def test_dry_run_writes_nothing(self):
        before = self.path.read_text()
        code, out, err = run_main(install, ["--dry-run", "--settings", str(self.path)])
        self.assertEqual(code, 0)
        self.assertIn('"statusLine"', out)
        self.assertIn("is not changed", err)
        self.assertEqual(self.path.read_text(), before)

    def test_other_status_line_is_kept_without_replace(self):
        mine = {"type": "command", "command": "~/.claude/statusline.sh"}
        self.path.write_text(json.dumps({"statusLine": mine}))
        for argv in ([], ["--remove"]):
            code, _, _ = run_main(install, argv + ["--settings", str(self.path)])
            self.assertNotEqual(code, 0)
            self.assertEqual(self.settings()["statusLine"], mine)
        run_main(install, ["--replace", "--settings", str(self.path)])
        self.assertIn("tools/statusline/statusline.py", self.settings()["statusLine"]["command"])

    def test_missing_file_is_made(self):
        path = pathlib.Path(self.tmp.name) / "new" / "settings.json"
        run_main(install, ["--settings", str(path)])
        self.assertIn("statusLine", json.loads(path.read_text()))

    def test_invalid_json_is_not_changed(self):
        self.path.write_text("{ not json")
        code, _, _ = run_main(install, ["--settings", str(self.path)])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.path.read_text(), "{ not json")

    def test_default_path(self):
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp.name
        try:
            self.assertEqual(install.default_settings(), self.path)
        finally:
            if old is None:
                del os.environ["CLAUDE_CONFIG_DIR"]
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old


if __name__ == "__main__":
    unittest.main()
