"""Tests for the cache experiments: the sequence option of the harness (idle
time and resume), and `tokenomics session --requests`. Run from the
repository root:

    python3 -m unittest discover -s tools/tests

The tests make no model request and no AWS call.
"""
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics import baseline, sequence  # noqa: E402
from tokenomics.costmodel import Usage  # noqa: E402
from tokenomics.rates import load_profiles  # noqa: E402
from tokenomics.session import (  # noqa: E402
    cache_result, load_session, measured_miss_vs_hit, miss_vs_hit, request_rows, write_ttl)
from test_costmodel import run_cli  # noqa: E402
from test_session import block, prompt, request, tool_result, usage, write_jsonl  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CACHE_IDLE = FIXTURES / "cache-idle"
SEQUENCE = CACHE_IDLE / "cache-sequence.json"
HAIKU = "claude-haiku-5-5"


def subagent(root: pathlib.Path, agent_id: str, start: float, first: dict, second: dict):
    """One subagent lane with two requests, from `start` seconds after 12:00:00."""
    lane = root / "s1" / "subagents" / f"agent-{agent_id}.jsonl"
    write_jsonl(lane, [
        request(start, f"{agent_id}-r1"),
        block(start + 2, f"{agent_id}-r1", f"{agent_id}-m1", first, "tool_use", model=HAIKU),
        tool_result(start + 3),
        request(start + 3, f"{agent_id}-r2"),
        block(start + 5, f"{agent_id}-r2", f"{agent_id}-m2", second, "end_turn", model=HAIKU),
    ])
    lane.with_name(f"agent-{agent_id}.meta.json").write_text(
        json.dumps({"agentType": "cache-probe", "finished": True}))


def make_cache_session(root: pathlib.Path) -> pathlib.Path:
    """The cache-idle shape: a main lane and three calls of one subagent.

    Call 1 at 0:01 writes 20k tokens. Call 2 at 0:20 reads them (a hit).
    Call 3 at 7:00 comes after more than 5 minutes: it writes them again (a miss).
    The main lane writes with a 1-hour lifetime and still reads at 6:58.
    """
    main = root / "s1.jsonl"
    write_jsonl(main, [
        prompt(0),
        request(0.5, "r1"), block(1, "r1", "m1", usage(inp=3, w1=30_000, out=100), "tool_use"),
        tool_result(10),
        request(10.5, "r2"), block(12, "r2", "m2", usage(inp=1, w1=200, read=30_000, out=50),
                                   "end_turn"),
        prompt(19),
        request(19.2, "r3"), block(19.5, "r3", "m3", usage(inp=3, w1=300, read=30_200, out=90),
                                   "tool_use"),
        tool_result(30),
        request(30.5, "r4"), block(32, "r4", "m4", usage(inp=1, w1=200, read=30_500, out=50),
                                   "end_turn"),
        prompt(418),
        request(418.5, "r5"), block(419, "r5", "m5", usage(inp=3, w1=300, read=30_700, out=90),
                                    "tool_use"),
        tool_result(431),
        request(431.5, "r6"), block(433, "r6", "m6", usage(inp=1, w1=200, read=31_000, out=50),
                                    "end_turn"),
    ])
    subagent(root, "a1", 1.5, usage(inp=2, w5=20_000, out=60),
             usage(inp=1, w5=1_500, read=20_000, out=200))
    subagent(root, "a2", 20, usage(inp=2, w5=10, read=20_000, out=60),
             usage(inp=1, w5=1_500, read=20_010, out=200))
    subagent(root, "a3", 420, usage(inp=2, w5=20_000, out=60),
             usage(inp=1, w5=1_500, read=20_000, out=200))
    return main


class CacheResultTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(cache_result(Usage(), True), "none")
        self.assertEqual(cache_result(Usage(cache_read=10, cache_write_5m=10), False), "hit")
        self.assertEqual(cache_result(Usage(cache_write_5m=10), True), "write")
        self.assertEqual(cache_result(Usage(cache_write_1h=10, cache_read=1), False), "miss")

    def test_ttl(self):
        self.assertEqual(write_ttl(Usage()), "-")
        self.assertEqual(write_ttl(Usage(cache_write_5m=5)), "5m")
        self.assertEqual(write_ttl(Usage(cache_write_1h=5, cache_write_5m=1)), "1h")


class RequestRowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.main = make_cache_session(pathlib.Path(self.tmp.name))
        self.profiles = load_profiles()
        self.rows = request_rows(load_session(self.main), self.profiles)

    def tearDown(self):
        self.tmp.cleanup()

    def firsts(self):
        return [r for r in self.rows if r["first_in_lane"] and r["lane"] != "main"]

    def test_lanes_with_one_name_get_numbers_in_time_order(self):
        self.assertEqual([r["lane"] for r in self.firsts()],
                         ["subagent: cache-probe #1", "subagent: cache-probe #2",
                          "subagent: cache-probe #3"])
        self.assertEqual(len(self.rows), 12)
        self.assertEqual([r["seconds"] for r in self.rows],
                         sorted(r["seconds"] for r in self.rows))

    def test_write_hit_miss_and_gap(self):
        first, second, third = self.firsts()
        self.assertEqual((first["cache"], first["gap_seconds"]), ("write", None))
        self.assertEqual(second["cache"], "hit")
        self.assertAlmostEqual(second["gap_seconds"], 20 - 6.5)   # call 1 ended at 0:06.5
        self.assertEqual(third["cache"], "miss")
        self.assertAlmostEqual(third["gap_seconds"], 420 - 25)    # call 2 ended at 0:25
        self.assertEqual({r["ttl"] for r in self.firsts()}, {"5m"})

    def test_main_lane_stays_warm_with_a_one_hour_write(self):
        main = [r for r in self.rows if r["lane"] == "main"]
        self.assertEqual(main[0]["cache"], "write")
        self.assertEqual([r["cache"] for r in main[1:]], ["hit"] * 5)
        self.assertEqual({r["ttl"] for r in main}, {"1h"})
        after_idle = main[4]
        self.assertAlmostEqual(after_idle["gap_seconds"], 418.5 - 32)

    def test_prompt_cost_has_no_output(self):
        third = self.firsts()[2]
        haiku = self.profiles[HAIKU]
        expected = 2 * haiku.price("input") + 20_000 * haiku.price("cache_write_5m")
        self.assertAlmostEqual(third["prompt_cost"], expected)
        self.assertAlmostEqual(third["cost"], expected + 60 * haiku.price("output"))

    def test_rate_ratio_for_each_model_and_lifetime(self):
        ratios = {(m["model"], m["ttl"]): m["ratio"] for m in miss_vs_hit(self.rows,
                                                                          self.profiles)}
        self.assertAlmostEqual(ratios[(HAIKU, "5m")], 12.5)
        self.assertAlmostEqual(ratios[("claude-sonnet-5-5", "1h")], 40.0)

    def test_measured_ratio_compares_the_first_requests(self):
        (m,) = measured_miss_vs_hit(self.rows)
        self.assertEqual((m["hit"], m["miss"]),
                         ("subagent: cache-probe #2", "subagent: cache-probe #3"))
        haiku = self.profiles[HAIKU]
        hit = (2 * haiku.price("input") + 10 * haiku.price("cache_write_5m")
               + 20_000 * haiku.price("cache_read"))
        miss = 2 * haiku.price("input") + 20_000 * haiku.price("cache_write_5m")
        self.assertAlmostEqual(m["ratio"], miss / hit)
        self.assertGreater(m["ratio"], 11)

    def test_cli_requests_view(self):
        code, out, _ = run_cli("session", str(self.main), "--requests")
        self.assertEqual(code, 0)
        self.assertIn("Requests in time order (all lanes)", out)
        self.assertIn("subagent: cache-probe #3", out)
        self.assertIn("6m 35s", out)                       # the gap before call 3
        self.assertIn("1.25× ÷ 0.1×", out)
        self.assertIn("12.5×", out)
        self.assertIn("Measured, first request of each lane: subagent: cache-probe #3 (miss",
                      out)
        self.assertNotIn("Time in the main lane", out)
        code, out, _ = run_cli("session", str(self.main), "--requests", "--json")
        data = json.loads(out)
        self.assertEqual(len(data["requests"]), 12)
        self.assertEqual(data["measured_miss_vs_hit"][0]["miss"], "subagent: cache-probe #3")

    def test_without_requests_the_report_does_not_change(self):
        _, out, _ = run_cli("session", str(self.main), "--json")
        self.assertNotIn("requests", json.loads(out))


class LoadSequenceTest(unittest.TestCase):
    def test_sample_sequence(self):
        seq = sequence.load_sequence(SEQUENCE)
        self.assertEqual((seq["tool"], seq["model"], seq["effort"]),
                         ("claude", "claude-sonnet-5-5", "medium"))
        self.assertEqual([s["idle_seconds"] for s in seq["steps"]], [0, 0, 360])
        self.assertEqual({s["prompt_file"] for s in seq["steps"]},
                         {"prompts/call-cache-probe.md"})
        self.assertEqual(seq["variant"].name, "cache-probe-subagent")
        self.assertEqual(seq["variant"].files, (".claude/agents/cache-probe.md",))
        self.assertEqual(seq["setup"], "python-venv")

    def test_sample_subagent_pins_a_model_and_is_read_only(self):
        text = (CACHE_IDLE / "variant" / "files" / ".claude" / "agents" / "cache-probe.md").read_text()
        self.assertIn("\nmodel: claude-haiku-5-5\n", text)
        self.assertIn("\ntools: Read, Grep, Glob\n", text)

    def test_idle_option_changes_the_last_step(self):
        seq = sequence.load_sequence(SEQUENCE, idle=3900)
        self.assertEqual([s["idle_seconds"] for s in seq["steps"]], [0, 0, 3900])
        self.assertEqual(sequence.total_idle(seq), 3900)

    def write(self, root, data):
        (root / "p.md").write_text("hello\n")
        path = root / "seq.json"
        path.write_text(json.dumps(data))
        return path

    def test_errors(self):
        good = {"title": "t", "workload": "status-board-app", "tool": "claude",
                "model": "claude-haiku-5-5", "effort": "low", "steps": [{"prompt": "p.md"}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for change, message in (
                    ({"tool": "codex"}, "not supported"),
                    ({"workload": "other"}, "unknown workload"),
                    ({"steps": [{"prompt": "p.md", "idle_seconds": 5}]}, "cannot have idle"),
                    ({"steps": [{"prompt": "missing.md"}]}, "no prompt file"),
                    ({"model": ""}, "give 'model'")):
                with self.subTest(change=change):
                    with self.assertRaises((ValueError, FileNotFoundError)) as ctx:
                        sequence.load_sequence(self.write(root, good | change))
                    self.assertIn(message, str(ctx.exception))
            with self.assertRaises(ValueError):
                sequence.load_sequence(self.write(root, good), idle=60)   # one step only

    def test_fmt_seconds(self):
        self.assertEqual(sequence.fmt_seconds(30), "30s")
        self.assertEqual(sequence.fmt_seconds(360), "6m 00s")
        self.assertEqual(sequence.fmt_seconds(3900), "1h 05m")


class RunStepsTest(unittest.TestCase):
    def fake(self, session_ids):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            sid = session_ids[len(calls) - 1]
            return subprocess.CompletedProcess(argv, 0, json.dumps({"session_id": sid,
                                                                    "num_turns": 2}), "")
        return run, calls

    def test_resume_and_idle(self):
        seq = sequence.load_sequence(SEQUENCE)
        run, calls = self.fake(["s-1", "s-1", "s-1"])
        slept = []
        clock = iter(range(0, 1000, 10))
        out = sequence.run_steps(seq, lambda p: ["claude", "-p", p], pathlib.Path("."), {},
                                 60, log=lambda *a: None, sleep=slept.append, run=run,
                                 clock=lambda: next(clock))
        self.assertEqual(out["session_id"], "s-1")
        self.assertNotIn("--resume", calls[0])
        self.assertEqual(calls[1][-2:], ["--resume", "s-1"])
        self.assertEqual(calls[2][-2:], ["--resume", "s-1"])
        self.assertIn("List the incident functions.", calls[0][2])
        self.assertEqual(slept, [360])
        self.assertIsNone(out["steps"][0]["idle_actual"])
        self.assertEqual(out["steps"][1]["idle_actual"], 10)

    def test_a_new_session_id_stops_the_sequence(self):
        seq = sequence.load_sequence(SEQUENCE)
        run, _ = self.fake(["s-1", "s-2", "s-2"])
        with self.assertRaises(RuntimeError) as ctx:
            sequence.run_steps(seq, lambda p: ["claude"], pathlib.Path("."), {}, 60,
                               log=lambda *a: None, sleep=lambda s: None, run=run)
        self.assertIn("did not resume", str(ctx.exception))


class HarnessOptionTest(unittest.TestCase):
    def run_baseline(self, argv):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()) as err:
            code = baseline.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_dry_run(self):
        code, out = self.run_baseline(["--sequence", str(SEQUENCE), "--dry-run",
                                       "--run-id", "test-sequence"])
        self.assertEqual(code, 0)
        self.assertIn("cache-probe-subagent", out)
        self.assertIn("idle 6m 00s first", out)
        self.assertIn("--model claude-sonnet-5-5 --effort medium", out)
        self.assertIn("Dry run: no copy and no model request.", out)

    def test_idle_needs_a_sequence(self):
        code, out = self.run_baseline(["app-post-incident", "--tool", "reference",
                                       "--idle", "60"])
        self.assertEqual(code, 2)
        self.assertIn("--idle needs --sequence", out)


if __name__ == "__main__":
    unittest.main()
