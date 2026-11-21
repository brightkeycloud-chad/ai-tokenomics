"""Tests for `tokenomics reduce`. Run from the repository root:

    python3 -m unittest discover -s tools/tests
"""
import json
import pathlib
import sys
import tempfile
import unittest

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics.rates import load_profiles  # noqa: E402
from tokenomics.reduce import leftover_strings, reduce_session  # noqa: E402
from tokenomics.session import load_session, summarize  # noqa: E402
from test_costmodel import run_cli  # noqa: E402
from test_session import SAMPLE, make_session, prompt, request, ts, usage, write_jsonl  # noqa: E402

SECRETS = ("SECRET-PROMPT", "SECRET-ANSWER", "SECRET-THINKING", "SECRET-TOOL-OUTPUT",
           "/Users/someone/project", "SECRET-DESCRIPTION", "SECRET-ORIGIN-BODY", "sk-ant-xyz")


def text_session(root: pathlib.Path) -> pathlib.Path:
    """A session with text in every place where a transcript can hold text."""
    main = root / "src" / "s2.jsonl"
    u = usage(inp=3, w1=2_000, out=40)
    write_jsonl(main, [
        {"type": "permission-mode", "permissionMode": "default"},
        {"type": "attachment", "timestamp": ts(0), "attachment": {"text": "SECRET-PROMPT"}},
        dict(prompt(0, "SECRET-PROMPT in /Users/someone/project"), cwd="/Users/someone/project"),
        request(1, "r1"),
        {"type": "assistant", "timestamp": ts(2), "requestRef": "r1",
         "message": {"id": "m1", "model": "claude-haiku-5-5", "stop_reason": "tool_use",
                     "usage": u, "content": [
                         {"type": "thinking", "thinking": "SECRET-THINKING", "signature": "x"},
                         {"type": "text", "text": "SECRET-ANSWER"},
                         {"type": "tool_use", "name": "AskUserQuestion", "id": "toolu_01",
                          "input": {"question": "sk-ant-xyz"}}]}},
        {"type": "user", "timestamp": ts(6), "toolUseResult": {"answer": "SECRET-TOOL-OUTPUT"},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "toolu_01",
              "content": "SECRET-TOOL-OUTPUT"}]}},
        {"type": "user", "timestamp": ts(7), "isMeta": True, "promptSource": "system",
         "origin": {"kind": "peer", "body": "SECRET-ORIGIN-BODY"},
         "message": {"role": "user", "content": "SECRET-ORIGIN-BODY"}},
        request(8, "r2"),
        {"type": "assistant", "timestamp": ts(9), "requestRef": "r2",
         "message": {"id": "m2", "model": "claude-haiku-5-5", "stop_reason": "end_turn",
                     "usage": usage(inp=2, read=2_000, out=10),
                     "content": [{"type": "text", "text": "SECRET-ANSWER"}]}},
        {"type": "system", "subtype": "turn_duration", "timestamp": ts(9.1), "durationMs": 9100},
        {"type": "cost-state", "totalCostUSD": 0.01, "totalAPIDuration": 2000,
         "modelUsage": {"claude-haiku-5-5": {"inputTokens": 5}}, "cwd": "/Users/someone/project"},
    ])
    lanes = main.with_suffix("") / "subagents"
    write_jsonl(lanes / "agent-a1.jsonl", [
        {"type": "user", "timestamp": ts(1.5),
         "message": {"role": "user", "content": "SECRET-PROMPT"}},
        request(1.6, "s1"),
        {"type": "assistant", "timestamp": ts(3), "requestRef": "s1",
         "message": {"id": "x1", "model": "claude-haiku-5-5", "stop_reason": "end_turn",
                     "usage": usage(inp=1, w5=1_000, out=30),
                     "content": [{"type": "text", "text": "SECRET-ANSWER"}]}},
    ])
    (lanes / "agent-a1.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "SECRET-DESCRIPTION", "finished": True,
         "toolUseId": "toolu_01"}))
    return main


def report(path) -> dict:
    """The report without file paths and lane descriptions: the copy removes those."""
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in ("path", "description")}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return strip(summarize(load_session(path), load_profiles(), window=1_000_000))


class ReduceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_report_and_no_text(self):
        main = text_session(self.root)
        written = reduce_session(main, self.root / "out")
        copy = self.root / "out" / main.name
        self.assertEqual(report(copy), report(main))
        self.assertAlmostEqual(report(copy)["time"]["waiting"], 4, places=3)
        for file in written:
            body = file.read_text()
            for secret in SECRETS:
                self.assertNotIn(secret, body, f"{secret} in {file.name}")
        self.assertEqual(len(written), 3)

    def test_leftover_strings_are_field_values_only(self):
        main = text_session(self.root)
        leftovers = leftover_strings(reduce_session(main, self.root / "out"))
        self.assertIn("AskUserQuestion", leftovers)
        self.assertIn("Explore", leftovers)
        self.assertFalse([s for s in leftovers if any(x in s for x in SECRETS)])

    def test_other_lane_kinds(self):
        # The synthetic session has a subagent and a forked skill.
        main = make_session(self.root / "src")
        reduce_session(main, self.root / "out")
        self.assertEqual(report(self.root / "out" / main.name), report(main))
        skill = self.root / "out" / main.stem / "subagents" / "agent-b2.forked-skill.json"
        self.assertEqual(json.loads(skill.read_text()), {"skillName": "build-demo"})

    def test_sample_session_reduces_to_itself(self):
        reduce_session(SAMPLE, self.root / "out")
        self.assertEqual(report(self.root / "out" / SAMPLE.name), report(SAMPLE))

    def test_no_overwrite(self):
        main = text_session(self.root)
        reduce_session(main, self.root / "out")
        with self.assertRaises(FileExistsError):
            reduce_session(main, self.root / "out")

    def test_cli(self):
        main = text_session(self.root)
        code, out, err = run_cli("reduce", str(main), "--out", str(self.root / "out"))
        self.assertEqual(code, 0, err)
        self.assertIn("Reduced copy of session s2:", out)
        self.assertIn("  AskUserQuestion", out)
        self.assertNotIn("SECRET", out)
        code, out, err = run_cli("reduce", str(main), "--out", str(self.root / "out"))
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)


if __name__ == "__main__":
    unittest.main()
