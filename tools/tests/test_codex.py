"""Tests for `tokenomics codex`. Run from the repository root:

    python3 -m unittest discover -s tools/tests
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics import codex  # noqa: E402
from tokenomics.rates import load_profiles  # noqa: E402
from test_costmodel import run_cli  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SAMPLE_HOME = FIXTURES / "codex-home"
SAMPLE_EVENTS = SAMPLE_HOME / "exec" / "find-incident-files.jsonl"
REASONING_EVENTS = SAMPLE_HOME / "exec" / "reasoning-check.jsonl"
THREAD = "01a1e19a-9782-7e70-a452-805b74bb77a0"

SECRETS = ("SECRET-PROMPT", "SECRET-ANSWER", "SECRET-COMMAND", "SECRET-OUTPUT",
           "/Users/someone/project", "user-SECRET", "SECRET-INSTRUCTIONS", "SECRET-REPO")


def u(inp, cached=0, writes=0, out=0, reasoning=0):
    return {"input_tokens": inp, "cached_input_tokens": cached,
            "cache_write_input_tokens": writes, "output_tokens": out,
            "reasoning_output_tokens": reasoning, "total_tokens": inp + out}


def ts(seconds):
    whole, frac = divmod(seconds, 1)
    return f"2026-11-15T10:{int(whole) // 60:02d}:{int(whole) % 60:02d}.{int(frac * 1000):03d}Z"


def limits(primary, weekly):
    return {"limit_id": "codex", "plan_type": "pro",
            "primary": {"used_percent": primary, "window_minutes": 300, "resets_at": 1794780000},
            "secondary": {"used_percent": weekly, "window_minutes": 10080,
                          "resets_at": 1795300000},
            "credits": {"has_credits": False, "unlimited": False, "balance": "0"}}


def ev(t, ptype, **payload):
    return {"timestamp": ts(t), "type": "event_msg", "payload": dict(type=ptype, **payload)}


def rec(t, rtype, **payload):
    return {"timestamp": ts(t), "type": rtype, "payload": payload}


def session_records(thread="01a1e19a-0000-7000-8000-000000000001", cwd="/work/app",
                    model="gpt-6.1-sol", second_turn=True, finish_last=True, big=False):
    """Two turns. Turn 1: two requests and one command. Turn 2 after a 20-second wait."""
    first = u(300_000 if big else 10_000, cached=8_000, writes=1_000, out=200, reasoning=50)
    rows = [
        rec(0, "session_meta", id=thread, cwd=cwd, cli_version="0.169.0",
            originator="codex_cli_rs", base_instructions={"text": "SECRET-INSTRUCTIONS"},
            creator_user_id="user-SECRET", git={"repository_url": "SECRET-REPO"}),
        ev(0, "task_started", turn_id="t1", model_context_window=258400),
        rec(0.5, "response_item", type="message", role="user",
            content=[{"type": "input_text", "text": "SECRET-PROMPT"}]),
        rec(1, "turn_context", turn_id="t1", model=model, effort="medium", cwd=cwd),
        rec(4, "response_item", type="function_call", name="exec", arguments="SECRET-COMMAND"),
        rec(4, "token_usage_record", turn_id="t1", response_id="resp_1", usage=first),
        ev(4, "token_count", info={"total_token_usage": first, "last_token_usage": first,
                                   "model_context_window": 258400},
           rate_limits=limits(1.0, 2.0)),
        rec(6, "response_item", type="function_call_output", output="SECRET-OUTPUT"),
        rec(9, "response_item", type="message", role="assistant",
            content=[{"type": "output_text", "text": "SECRET-ANSWER"}]),
        rec(9, "token_usage_record", turn_id="t1", response_id="resp_2",
            usage=u(12_000, cached=11_000, out=100)),
        ev(9, "token_count", info={"total_token_usage": u(22_000, 19_000, 1_000, 300, 50),
                                   "last_token_usage": u(12_000, cached=11_000, out=100)},
           rate_limits=limits(1.0, 2.0)),
        ev(9, "task_complete", turn_id="t1", duration_ms=9000),
    ]
    if second_turn:
        rows += [
            ev(29, "task_started", turn_id="t2", model_context_window=258400),
            rec(29, "turn_context", turn_id="t2", model=model, effort="medium"),
            rec(32, "token_usage_record", turn_id="t2", response_id="resp_3",
                usage=u(13_000, cached=12_000, out=50)),
            ev(32, "token_count", info={"total_token_usage": u(35_000, 31_000, 1_000, 350, 50),
                                        "last_token_usage": u(13_000, cached=12_000, out=50)},
               rate_limits=limits(3.0, 2.5)),
        ]
        if finish_last:
            rows.append(ev(33, "task_complete", turn_id="t2", duration_ms=4000))
    return rows


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def rollout_path(home, thread, stamp="2026-11-15T10-00-00"):
    return home / "sessions" / "2026" / "11" / "15" / f"rollout-{stamp}-{thread}.jsonl"


class ToUsageTest(unittest.TestCase):
    def test_cached_and_writes_come_out_of_input(self):
        usage = codex.to_usage(u(16027, cached=12160, out=5))
        self.assertEqual((usage.input, usage.cache_read, usage.cache_write_5m, usage.output),
                         (3867, 12160, 0, 5))
        usage = codex.to_usage(u(10_000, cached=8_000, writes=1_000, out=200, reasoning=50))
        self.assertEqual((usage.input, usage.cache_read, usage.cache_write_5m), (1000, 8000, 1000))
        self.assertEqual((usage.output, usage.reasoning), (200, 50))
        self.assertEqual(usage.total, 10_200)

    def test_bad_counts_are_rejected(self):
        with self.assertRaises(ValueError):
            codex.to_usage(u(100, cached=90, writes=20))
        with self.assertRaises(ValueError):
            codex.to_usage(u(100, out=5, reasoning=6))
        with self.assertRaises(ValueError):
            codex.raw_usage({"input_tokens": -1})


class RatesTest(unittest.TestCase):
    def setUp(self):
        self.profiles = load_profiles()

    def test_credit_cache_writes_use_the_input_rate(self):
        for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"):
            credits = self.profiles[codex.CREDIT_PREFIX + model]
            self.assertEqual(credits.unit, "credits")
            self.assertEqual(credits.relative("cache_write_5m"), 1)
            api = self.profiles[model]
            self.assertAlmostEqual(api.relative("cache_write_5m"), 1.25)
            self.assertAlmostEqual(api.relative("cache_write_1h"), 2.5)
            self.assertEqual(api.long_prompt_over, 272_000)

    def test_cache_read_ratios(self):
        sol = self.profiles["codex-credits/gpt-6.1-sol"]
        self.assertAlmostEqual(sol.relative("cache_read"), 0.05)
        self.assertAlmostEqual(self.profiles["codex-credits/gpt-6-astra"].relative("cache_read"),
                               0.1)
        self.assertAlmostEqual(sol.relative("output"), 5)
        spread = (self.profiles["codex-credits/gpt-6-astra"].price("input")
                  / self.profiles["codex-credits/gpt-6-luna"].price("input"))
        self.assertAlmostEqual(spread, 100)


class SessionFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, rows, model=""):
        return codex.summarize(codex.load_session(rows, model), load_profiles())

    def test_tokens_cost_and_requests(self):
        r = self.report(session_records())
        self.assertEqual(r["requests"], 3)
        self.assertEqual(r["turns"], 2)
        self.assertTrue(r["complete"])
        self.assertEqual(r["codex_usage"]["input_tokens"], 35_000)
        self.assertEqual(r["codex_reported_total"]["input_tokens"], 35_000)
        self.assertEqual(r["usage"], {"input": 3_000, "cache_write_5m": 1_000, "cache_write_1h": 0,
                                      "cache_read": 31_000, "output": 350, "reasoning": 50})
        # gpt-6.1-sol: 2.00 input, 2.50 write, 0.10 read, 10.00 output per million.
        self.assertAlmostEqual(r["api"]["cost"], (3_000 * 2 + 1_000 * 2.5 + 31_000 * 0.1
                                                  + 350 * 10) / 1e6)
        # Credits: 50 input and write, 2.5 read, 250 output per million.
        self.assertAlmostEqual(r["credits"]["cost"], (4_000 * 50 + 31_000 * 2.5 + 350 * 250) / 1e6)
        self.assertEqual(r["models"], ["gpt-6.1-sol"])
        self.assertEqual(r["effort"], "medium")
        self.assertEqual(r["commands"], 1)
        self.assertAlmostEqual(r["ratios"]["cache_hit"], 31_000 / 35_000)
        self.assertAlmostEqual(r["ratios"]["reasoning_share_of_output"], 50 / 350)

    def test_time_split_and_context(self):
        r = self.report(session_records())
        # Generating: 1 -> 4, 6 -> 9, 29 -> 32. Waiting: 9 -> 29. Wall: 0 -> 33.
        self.assertAlmostEqual(r["time"]["generating"], 9)
        self.assertAlmostEqual(r["time"]["waiting"], 20)
        self.assertAlmostEqual(r["time"]["wall"], 33)
        self.assertAlmostEqual(r["time"]["tools"], 4)
        self.assertEqual(r["context"], {"last": 13_000, "peak": 13_000, "window": 258400})

    def test_limits_first_and_last(self):
        limits_ = self.report(session_records())["limits"]
        self.assertEqual(limits_["plan"], "pro")
        self.assertEqual(limits_["primary"]["used_percent_first"], 1.0)
        self.assertEqual(limits_["primary"]["used_percent_last"], 3.0)
        self.assertEqual(limits_["secondary"]["window_minutes"], 10080)

    def test_long_prompt_uses_the_long_profile(self):
        r = self.report(session_records(big=True))
        self.assertIn("gpt-6.1-sol-over-272k", r["api"]["profiles"])
        self.assertEqual(r["credits"]["profiles"], ["codex-credits/gpt-6.1-sol"])

    def test_unfinished_turn(self):
        r = self.report(session_records(finish_last=False))
        self.assertFalse(r["complete"])
        code, out, _ = self._cli(session_records(finish_last=False))
        self.assertIn("NOT FINISHED", out)
        self.assertIn("WARNING: a turn is not finished", out)

    def test_model_override_and_unpriced_model(self):
        r = self.report(session_records(model="gpt-new-9"))
        self.assertEqual(r["api"]["unpriced_models"], ["gpt-new-9"])
        r = self.report(session_records(model="gpt-new-9"), model="gpt-6-luna")
        self.assertEqual(r["models"], ["gpt-6-luna"])
        self.assertFalse(r["api"]["unpriced_models"])

    def test_token_count_only_files_do_not_count_twice(self):
        rows = [r for r in session_records() if r["type"] != "token_usage_record"]
        rows.insert(3, ev(0.6, "token_count", info=None, rate_limits=limits(1.0, 2.0)))
        rows.append(dict(rows[-2]))         # a repeated token_count event
        r = self.report(rows)
        self.assertEqual(r["requests"], 3)
        self.assertEqual(r["codex_usage"]["input_tokens"], 35_000)

    def _cli(self, rows, *extra):
        path = write_jsonl(rollout_path(self.home, "01a1e19a-0000-7000-8000-000000000001"), rows)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            return run_cli("codex", str(path), *extra)

    def test_text_report(self):
        code, out, err = self._cli(session_records())
        self.assertEqual(code, 0, err)
        self.assertIn("· Codex CLI · 2 turns, 3 requests · finished", out)
        self.assertIn("Model gpt-6.1-sol · effort medium · read from the session file", out)
        self.assertIn("Waiting on you  20.0s    61%", out)
        self.assertIn("5 hours             1.0%        3.0%", out)


class ExecEventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def events(self, thread="01a1e19a-0000-7000-8000-000000000001"):
        return write_jsonl(self.home / "run.jsonl", [
            {"type": "thread.started", "thread_id": thread},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "item_0", "type": "command_execution",
                                                "command": "SECRET-COMMAND",
                                                "aggregated_output": "SECRET-OUTPUT"}},
            {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
                                                "text": "SECRET-ANSWER"}},
            {"type": "turn.completed", "usage": u(22_000, 19_000, 1_000, 300, 50)},
        ])

    def test_no_session_file_needs_a_model(self):
        thread, events = codex.load(str(self.events()))
        self.assertIsNone(events)
        r = codex.summarize(thread, load_profiles())
        self.assertEqual(r["source"], "exec events")
        self.assertEqual(r["api"]["unpriced_models"], ["(unknown model)"])
        self.assertIsNone(r["time"])
        code, out, _ = run_cli("codex", str(self.events()))
        self.assertIn("Give --model", out)

    def test_model_flag_prices_the_turn_total(self):
        code, out, _ = run_cli("codex", str(self.events()), "--model", "gpt-6.1-sol", "--json")
        r = json.loads(out)
        self.assertEqual(r["usage"]["input"], 2_000)
        self.assertFalse(r["api"]["long_prompt_rule_skipped"])
        self.assertAlmostEqual(r["credits"]["cost"], (3_000 * 50 + 19_000 * 2.5 + 300 * 250) / 1e6)

    def test_long_turn_total_gives_a_note(self):
        path = write_jsonl(self.home / "long.jsonl", [
            {"type": "thread.started", "thread_id": "01a1e19a-0000-7000-8000-000000000009"},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": u(400_000, 350_000, 0, 900)}])
        code, out, _ = run_cli("codex", str(path), "--model", "gpt-6.1-sol")
        self.assertIn("long-prompt rates of gpt-6.1-sol are not applied", out)

    def test_session_file_is_found_by_thread_id(self):
        write_jsonl(rollout_path(self.home, "01a1e19a-0000-7000-8000-000000000001"),
                    session_records(second_turn=False))
        thread, events = codex.load(str(self.events()))
        r = codex.summarize(thread, load_profiles(), events)
        self.assertEqual(r["source"], "session file")
        self.assertTrue(r["events_match_session_file"])
        self.assertEqual(r["requests"], 2)

    def test_mismatch_is_reported(self):
        write_jsonl(rollout_path(self.home, "01a1e19a-0000-7000-8000-000000000001"),
                    session_records())          # two turns; the events have one
        code, out, _ = run_cli("codex", str(self.events()))
        self.assertIn("WARNING: the exec events and the session file give different", out)

    def test_not_codex(self):
        path = write_jsonl(self.home / "other.jsonl", [{"type": "assistant"}])
        code, _, err = run_cli("codex", str(path))
        self.assertEqual(code, 2)
        self.assertIn("not Codex output", err)


class FindThreadTest(unittest.TestCase):
    def test_by_id_prefix_and_by_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            a = "01a1e19a-0000-7000-8000-00000000000a"
            b = "01a1e19a-0000-7000-8000-00000000000b"
            write_jsonl(rollout_path(home, a, "2026-11-15T10-00-00"),
                        session_records(thread=a, cwd=str(home)))
            write_jsonl(rollout_path(home, b, "2026-11-15T11-00-00"),
                        session_records(thread=b, cwd="/elsewhere"))
            root = home / "sessions"
            self.assertTrue(codex.find_thread(b, root=root).name.endswith(b + ".jsonl"))
            with self.assertRaises(ValueError):
                codex.find_thread("01a1e19a", root=root)
            self.assertTrue(codex.find_thread(project=home, root=root).name.endswith(a + ".jsonl"))
            with self.assertRaises(FileNotFoundError):
                codex.find_thread(project="/nowhere", root=root)


class ReduceTest(unittest.TestCase):
    def test_reduced_copies_have_no_text_and_give_the_same_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            thread = "01a1e19a-0000-7000-8000-000000000001"
            src = write_jsonl(rollout_path(tmp / "src", thread), session_records())
            out = tmp / "out"
            code, printed, err = run_cli("reduce", str(src), "--out", str(out))
            self.assertEqual(code, 0, err)
            copy = out / src.name
            text = copy.read_text()
            for secret in SECRETS:
                self.assertNotIn(secret, text)
            self.assertNotIn('"cwd"', text)
            before = codex.summarize(codex.load_session(codex.read_records(src)), load_profiles())
            after = codex.summarize(codex.load_session(codex.read_records(copy)), load_profiles())
            self.assertEqual(before, after)
            for secret in SECRETS:
                self.assertNotIn(secret, printed)
            self.assertEqual(codex.leftover_strings([copy]),
                             sorted({"codex", "codex_cli_rs", "event_msg", "function_call",
                                     "function_call_output", "exec", "gpt-6.1-sol", "medium",
                                     "pro", "response_item", "session_meta", "task_complete",
                                     "task_started", "token_count", "token_usage_record",
                                     "turn_context", "t1", "t2", "0"}))

    def test_exec_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            src = ExecEventsTest.events(type("T", (), {"home": tmp})())
            copy = codex.reduce_file(src, tmp / "out")
            text = copy.read_text()
            for secret in SECRETS:
                self.assertNotIn(secret, text)
            self.assertEqual(codex.summarize(codex.load_exec(codex.read_records(copy)),
                                             load_profiles()),
                             codex.summarize(codex.load_exec(codex.read_records(src)),
                                             load_profiles()))
            with self.assertRaises(FileExistsError):
                codex.reduce_file(src, tmp / "out")


class SampleTest(unittest.TestCase):
    """The committed samples from short smoke runs."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"CODEX_HOME": str(SAMPLE_HOME)})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_find_incident_files(self):
        thread, events = codex.load(str(SAMPLE_EVENTS))
        r = codex.summarize(thread, load_profiles(), events)
        self.assertEqual(r["thread_id"], THREAD)
        self.assertTrue(r["events_match_session_file"])
        self.assertEqual(r["models"], ["gpt-6-luna"])
        self.assertEqual(r["requests"], 3)
        self.assertEqual(r["codex_usage"]["input_tokens"], 48_744)
        self.assertEqual(r["usage"]["input"], 10_344)
        self.assertEqual(round(r["api"]["cost"], 4), 0.0016)
        self.assertEqual(round(r["credits"]["cost"], 4), 0.0394)

    def test_reasoning_is_inside_output(self):
        thread, events = codex.load(str(REASONING_EVENTS))
        r = codex.summarize(thread, load_profiles(), events)
        self.assertEqual(r["effort"], "high")
        raw = r["codex_usage"]
        self.assertEqual((raw["output_tokens"], raw["reasoning_output_tokens"]), (44, 37))
        self.assertEqual(r["codex_reported_total"]["input_tokens"] + 44, 15_397)
        self.assertEqual(r["usage"]["output"], 44)

    def test_samples_have_no_text(self):
        for path in SAMPLE_HOME.rglob("*.jsonl"):
            text = path.read_text()
            for marker in ('"text":', '"cwd"', '"command"', "/Users/", "/home/",
                           "creator_user_id", "base_instructions"):
                self.assertNotIn(marker, text, path)


if __name__ == "__main__":
    unittest.main()
