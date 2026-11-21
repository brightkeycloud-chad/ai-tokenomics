"""Tests for `tokenomics session`. Run from the repository root:

    python3 -m unittest discover -s tools/tests
"""
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics.rates import load_profiles  # noqa: E402
from tokenomics.session import (  # noqa: E402
    find_session, load_session, project_key, summarize, union_length, usage_from_api)
from test_costmodel import run_cli  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "claude-session" / "78fe89b5-226e-4ee5-9785-a7d8ac85470b.jsonl"


def ts(seconds: float) -> str:
    """Seconds after 12:00:00 -> an ISO time stamp, as Claude Code writes it."""
    return f"2026-11-14T12:{int(seconds // 60):02d}:{seconds % 60:06.3f}Z"


def usage(inp=0, w5=0, w1=0, read=0, out=0, thinking=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": w5 + w1,
            "cache_read_input_tokens": read, "output_tokens": out,
            "output_tokens_details": {"thinking_tokens": thinking},
            "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}}


def prompt(t, text="hello"):
    return {"type": "user", "timestamp": ts(t), "promptSource": "typed",
            "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def tool_result(t):
    return {"type": "user", "timestamp": ts(t),
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}}


def request(t, rid):
    return {"type": "api-request", "timestamp": ts(t), "id": rid}


def block(t, rid, mid, use, stop, model="claude-sonnet-5-5", kind="text"):
    return {"type": "assistant", "timestamp": ts(t), "requestRef": rid,
            "message": {"id": mid, "model": model, "stop_reason": stop, "usage": use,
                        "content": [{"type": kind}]}}


def write_jsonl(path: pathlib.Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def make_session(root: pathlib.Path, subagent_finished=True) -> pathlib.Path:
    """A main lane, one subagent, and one forked skill, with known times.

    Main lane, in seconds after 12:00:00:
      0  prompt
      1  request 1 starts; 3 thinking block; 5 tool_use block (same message)
      9  tool result                     -> tools 5..9
      10 request 2 starts; 12 end_turn   -> generating 1..5 and 10..12
      42 you type /usage; 42.5 the next prompt -> waiting 12..42.5
      43 request 3 starts; 46 end_turn   -> generating 43..46
    Wall clock 0..46 = 46 s. Generating 4 + 2 + 3 = 9 s. Waiting 30.5 s.
    Tools (all other time) 6.5 s.
    """
    main = root / "s1.jsonl"
    u1 = usage(inp=2, w1=20_000, out=300, thinking=100)
    write_jsonl(main, [
        prompt(0),
        request(1, "r1"), block(3, "r1", "m1", u1, "tool_use", kind="thinking"),
        block(5, "r1", "m1", u1, "tool_use", kind="tool_use"),
        tool_result(9),
        request(10, "r2"), block(12, "r2", "m2", usage(inp=2, w1=500, read=20_000, out=200),
                                 "end_turn"),
        prompt(42, "<command-name>/usage</command-name>"),
        prompt(42.5),
        {"type": "user", "timestamp": ts(42.6), "isMeta": True,
         "message": {"role": "user", "content": "<system-reminder>"}},
        request(43, "r3"), block(46, "r3", "m3", usage(inp=2, w1=100, read=20_500, out=50),
                                 "end_turn"),
        {"type": "cost-state", "totalCostUSD": 0.5, "totalAPIDuration": 9000,
         "modelUsage": {"claude-sonnet-5-5": {}}},
    ])
    lanes = main.with_suffix("") / "subagents"
    write_jsonl(lanes / "agent-a1.jsonl", [
        {"type": "user", "timestamp": ts(5), "message": {"role": "user", "content": "task"}},
        request(5.1, "s1"), block(8, "s1", "x1", usage(inp=1, w5=10_000, out=400), "end_turn",
                                  model="claude-haiku-5-5"),
        # The same message again: counted one time only.
        block(8, "s1", "x1", usage(inp=1, w5=10_000, out=400), "end_turn",
              model="claude-haiku-5-5"),
    ])
    (lanes / "agent-a1.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "find files", "finished": subagent_finished}))
    write_jsonl(lanes / "agent-b2.jsonl", [
        request(20, "k1"),
        # A prompt of 120,000 tokens: Haiku uses the long-prompt profile.
        block(21, "k1", "y1", usage(inp=1, read=120_000, out=10), "end_turn",
              model="claude-haiku-5-5"),
        # A copy of a main-lane message: counted in the main lane only.
        block(21, "k1", "m2", usage(inp=2, w1=500, read=20_000, out=200), "end_turn"),
    ])
    (lanes / "agent-b2.forked-skill.json").write_text(json.dumps({"skillName": "build-demo"}))
    (lanes / "agent-b2.meta.json").write_text(json.dumps({"agentType": "general-purpose"}))
    return main


class SampleSessionTest(unittest.TestCase):
    """A sample session from an interactive run, with a subagent lane."""

    @classmethod
    def setUpClass(cls):
        cls.report = summarize(load_session(SAMPLE), load_profiles(), window=1_000_000)

    def test_every_lane_is_read(self):
        lanes = self.report["lanes"]
        self.assertEqual([l["name"] for l in lanes], ["main", "subagent: Explore"])
        self.assertEqual([l["requests"] for l in lanes], [3, 4])
        self.assertTrue(self.report["complete"])

    def test_transcripts_are_a_lower_bound(self):
        # Claude Code's own modelUsage for this session: 1,885 input, 38,381 + 52,719
        # cache write, 284,364 cache read, 4,394 output, $0.020570565. Background
        # requests (session title, recap) have no transcript, so the transcripts give less.
        u = self.report["usage"]
        self.assertEqual((u["input"], u["cache_write_5m"], u["cache_write_1h"],
                          u["cache_read"], u["output"]), (16, 38_381, 51_763, 182_396, 3_923))
        for ours, claude in zip((u["input"], u["cache_write_5m"], u["cache_write_1h"],
                                 u["cache_read"], u["output"]),
                                (1_885, 38_381, 52_719, 284_364, 4_394)):
            self.assertLessEqual(ours, claude)
        estimate = self.report["claude_code_estimate"]["total_cost_usd"]
        self.assertAlmostEqual(estimate, 0.020570565, places=9)
        self.assertLess(self.report["cost"], estimate)

    def test_main_lane_alone_undercounts(self):
        main = self.report["lanes"][0]
        self.assertEqual(main["tokens"], 152_703)
        self.assertAlmostEqual(self.report["ratios"]["other_lanes_token_share"], 0.45, places=2)
        self.assertAlmostEqual(self.report["ratios"]["other_lanes_cost_share"], 0.37, places=2)

    def test_ratios(self):
        r = self.report["ratios"]
        self.assertAlmostEqual(r["cache_write_cost_share"], 0.80, places=2)
        self.assertAlmostEqual(r["cache_hit"], 0.67, places=2)
        self.assertAlmostEqual(r["cache_hit_main"], 0.66, places=2)
        self.assertAlmostEqual(r["generation_to_wall"], 0.37, places=2)
        self.assertEqual(self.report["context"]["last"], 51_792)

    def test_background_subagent_wait_is_tool_time(self):
        # The main lane ends its turn and waits for the background subagent report.
        t = self.report["time"]
        self.assertEqual(t["waiting"], 0.0)
        self.assertAlmostEqual(t["tools"], 12.2, places=1)


class SyntheticSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def summary(self, **kwargs):
        return summarize(load_session(make_session(self.root, **kwargs)), load_profiles())

    def test_time_split(self):
        time = self.summary()["time"]
        self.assertAlmostEqual(time["wall"], 46, places=3)
        self.assertAlmostEqual(time["generating"], 9, places=3)
        self.assertAlmostEqual(time["waiting"], 30.5, places=3)
        self.assertAlmostEqual(time["tools"], 6.5, places=3)

    def test_question_counts_as_waiting(self):
        """Claude asks a question (AskUserQuestion). The time to your answer is waiting."""
        def tool_use(t, rid, mid, name, tid):
            record = block(t, rid, mid, usage(inp=1, w1=1_000, out=20), "tool_use")
            record["message"]["content"] = [{"type": "tool_use", "name": name, "id": tid}]
            return record

        def answer(t, tid):
            return {"type": "user", "timestamp": ts(t), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "x"}]}}
        # 0 prompt; 1..3 request; 3..11 question; 12..14 request;
        # 14..16 Read tool; 17..18 request.
        main = self.root / "q.jsonl"
        write_jsonl(main, [
            prompt(0),
            request(1, "r1"), tool_use(3, "r1", "m1", "AskUserQuestion", "t1"),
            answer(11, "t1"),
            request(12, "r2"), tool_use(14, "r2", "m2", "Read", "t2"),
            answer(16, "t2"),
            request(17, "r3"), block(18, "r3", "m3", usage(inp=1, read=1_000, out=5), "end_turn"),
        ])
        time = summarize(load_session(main), load_profiles())["time"]
        self.assertAlmostEqual(time["wall"], 18, places=3)
        self.assertAlmostEqual(time["generating"], 5, places=3)
        self.assertAlmostEqual(time["waiting"], 8, places=3)
        self.assertAlmostEqual(time["tools"], 5, places=3)

    def test_lanes_and_deduplication(self):
        report = self.summary()
        names = [l["name"] for l in report["lanes"]]
        self.assertEqual(names, ["main", "subagent: Explore", "skill: build-demo"])
        self.assertEqual([l["requests"] for l in report["lanes"]], [3, 1, 1])
        self.assertEqual(report["lanes"][1]["usage"]["cache_write_5m"], 10_000)
        self.assertEqual(report["usage"]["reasoning"], 100)
        self.assertAlmostEqual(report["other_lanes_generating_seconds"], 2.9 + 1, places=3)

    def test_long_prompt_uses_its_profile(self):
        skill = self.summary()["lanes"][2]
        # claude-haiku-5-5-over-100k: input 0.50, cache read 0.05, output 2.50 per MTok.
        self.assertAlmostEqual(skill["cost"], (1 * 0.50 + 120_000 * 0.05 + 10 * 2.50) / 1e6)

    def test_unfinished_lane(self):
        report = self.summary(subagent_finished=False)
        self.assertFalse(report["complete"])
        code, out, _ = run_cli("session", str(make_session(self.root, subagent_finished=False)))
        self.assertEqual(code, 0)
        self.assertIn("WARNING: subagent: Explore is not finished", out)

    def test_cli_json_and_relative_rates(self):
        path = make_session(self.root)
        code, out, _ = run_cli("session", str(path), "--json", "--rates", "relative")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["unit"], "input-equivalent tokens")
        self.assertEqual(data["claude_code_estimate"]["total_cost_usd"], 0.5)

    def test_unknown_model_is_reported(self):
        path = self.root / "s2.jsonl"
        write_jsonl(path, [prompt(0), request(1, "r"),
                           block(2, "r", "m", usage(inp=5, out=5), "end_turn", model="other-1")])
        report = summarize(load_session(path), load_profiles())
        self.assertEqual(report["unpriced_models"], ["other-1"])
        self.assertEqual(report["cost"], 0)


class FindSessionTest(unittest.TestCase):
    def test_project_key(self):
        self.assertEqual(project_key("/Users/me/my_app.v2"), "-Users-me-my-app-v2")

    def test_latest_id_prefix_and_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            project = root / "work" / "app"
            project.mkdir(parents=True)
            folder = root / "projects" / project_key(project)
            folder.mkdir(parents=True)
            old, new = folder / "aaaa-1.jsonl", folder / "bbbb-2.jsonl"
            old.write_text("")
            new.write_text("")
            now = time.time()
            os.utime(old, (now - 60, now - 60))
            self.assertEqual(find_session(None, project, root / "projects"), new)
            self.assertEqual(find_session("aaaa", project, root / "projects"), old)
            self.assertEqual(find_session(str(old)), old)
            with self.assertRaises(FileNotFoundError):
                find_session("cccc", project, root / "projects")


class HelperTest(unittest.TestCase):
    def test_union_length(self):
        self.assertEqual(union_length([(0, 2), (1, 3), (5, 6)], 0, 10), 4)
        self.assertEqual(union_length([(0, 10)], 2, 4), 2)
        self.assertEqual(union_length([], 0, 1), 0)

    def test_long_prompt_needs_a_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            rates = pathlib.Path(tmp) / "rates.json"
            body = {"multipliers": {"input": 1, "cache_write_5m": 1.25, "cache_write_1h": 2,
                                    "cache_read": 0.1, "output": 5},
                    "long_prompt": {"over_tokens": 100}}
            rates.write_text(json.dumps({"profiles": {"x": body}}))
            with self.assertRaises(ValueError):
                load_profiles(rates)

    def test_usage_without_cache_split(self):
        u = usage_from_api({"input_tokens": 1, "cache_creation_input_tokens": 7,
                            "cache_read_input_tokens": 3, "output_tokens": 2})
        self.assertEqual((u.cache_write_5m, u.cache_write_1h, u.reasoning), (7, 0, 0))


if __name__ == "__main__":
    unittest.main()
