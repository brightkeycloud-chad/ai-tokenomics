"""Tests for tool-result sizes and test runs, variants that extend
other variants, hook logs in the run record, and the filter-test-output hook.
Run from the repository root:

    python3 -m unittest discover -s tools/tests

The tests make no model request and no AWS call.
"""
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics import baseline, cli, toolresults, variant  # noqa: E402
from tokenomics.reduce import leftover_strings, reduce_session  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
HOOK_SWEEP = FIXTURES / "test-filter-hook"
VERBOSE = HOOK_SWEEP / "variants" / "verbose-tests"
HOOKED = HOOK_SWEEP / "variants" / "test-output-hook"
HOOK = HOOKED / "files" / ".claude" / "hooks" / "filter-test-output.py"


def load_hook():
    """Import the hook script. Write no __pycache__ into the variant files."""
    spec = importlib.util.spec_from_file_location("filter_test_output", HOOK)
    module = importlib.util.module_from_spec(spec)
    before, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
    return module


def ts(sec: int) -> str:
    return f"2026-11-16T10:00:{sec:02d}.000Z"


def usage(prompt: int, out: int) -> dict:
    return {"input_tokens": 10, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": prompt - 10, "output_tokens": out}


def assistant(sec, mid, prompt, out, tools=()):
    return {"type": "assistant", "timestamp": ts(sec),
            "message": {"id": mid, "model": "claude-sonnet-5-5", "stop_reason": "tool_use",
                        "usage": usage(prompt, out),
                        "content": [{"type": "tool_use", "id": tid, "name": name,
                                     "input": inp} for tid, name, inp in tools]}}


def results(sec, items, denied=False, source="config"):
    record = {"type": "user", "timestamp": ts(sec),
              "permissionDecision": {"decision": "reject" if denied else "accept",
                                     "source": source, "reasonType": "rule"},
              "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": tid, "content": text,
                   "is_error": denied} for tid, text in items]}}
    if denied:
        record["toolDenialKind"] = "permission-rule"
    return record


def write_session(path: pathlib.Path) -> pathlib.Path:
    """Six requests: a read, a denied test run, an edit, a test run, a re-run, an answer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pytest = {"command": "./venv/bin/python -m pytest"}
    records = [
        assistant(1, "m1", 1000, 100, [("t1", "Read", {"file_path": "a"}),
                                       ("t2", "Read", {"file_path": "b"})]),
        results(2, [("t1", "x" * 300), ("t2", "x" * 100)]),
        assistant(3, "m2", 1500, 50, [("t3", "Bash", {
            "command": "python3 - <<'EOF'\nimport pytest\nEOF\n./venv/bin/python -m pytest -q"})]),
        results(4, [("t3", "denied")], denied=True),
        assistant(5, "m3", 1600, 200, [("t4", "Edit", {"file_path": "c"})]),
        results(6, [("t4", "ok")]),
        assistant(7, "m4", 1850, 40, [("t5", "Bash", pytest)]),
        results(8, [("t5", "y" * 9000)], source="hook"),
        assistant(9, "m5", 4890, 40, [("t6", "Bash", pytest)]),
        results(10, [("t6", "y" * 9000)]),
        assistant(11, "m6", 7930, 30),
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


class KindTest(unittest.TestCase):
    def test_test_commands(self):
        for command in ("./venv/bin/python -m pytest", "pytest -q tests/test_api.py",
                        "cd app && ./venv/bin/pytest -x", "ls && venv/bin/python -m pytest",
                        "terraform -chdir=terraform test", "npm test", "go test ./...",
                        "./venv/bin/python -m pytest -q 2>&1 | tail -5"):
            self.assertEqual(toolresults.tool_kind("Bash", {"command": command}), "test",
                             command)

    def test_other_commands(self):
        for command in ("cat tests/test_api.py", "python3 - <<'EOF'\nimport pytest\nEOF",
                        "grep -n pytest README.md", "echo '@pytest.mark'"):
            self.assertEqual(toolresults.tool_kind("Bash", {"command": command}), "", command)
        self.assertEqual(toolresults.tool_kind("Read", {"command": "pytest"}), "")

    def test_result_bytes(self):
        self.assertEqual(toolresults.result_bytes({"content": "abcé"}), 5)
        self.assertEqual(toolresults.result_bytes(
            {"content": [{"type": "text", "text": "ab"}, {"type": "image"}]}), 2)
        self.assertIsNone(toolresults.result_bytes({"content": "[removed]"}))
        self.assertEqual(toolresults.result_bytes({"content": "[removed]", "bytes": 7}), 7)


class MeasureTest(unittest.TestCase):
    def test_tokens_test_runs_and_reruns(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = toolresults.load_run(write_session(pathlib.Path(tmp) / "s.jsonl"))
        tokens = {c.tool_use_id: c.tokens for c in run.calls}
        # m2 prompt 1500 - m1 prompt 1000 - m1 output 100 = 400, split 300:100 by bytes
        self.assertEqual(tokens["t1"], 300)
        self.assertEqual(tokens["t2"], 100)
        self.assertEqual(tokens["t5"], 4890 - 1850 - 40)
        calls = {c.tool_use_id: c for c in run.calls}
        self.assertTrue(calls["t3"].denied)
        self.assertFalse(calls["t5"].rerun)
        self.assertTrue(calls["t6"].rerun)
        self.assertEqual(calls["t5"].approved_by, "hook")
        s = run.summary()
        self.assertEqual((s["requests"], s["tool_calls"], s["test_runs"], s["reruns"]),
                         (6, 6, 2, 1))
        self.assertEqual(s["test_bytes"], 18000)
        self.assertEqual(s["test_tokens"], 3000 + 3000)
        self.assertEqual(s["result_tokens"], 400 + 50 + 50 + 3000 + 3000)

    def test_reduced_copy_gives_the_same_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            main = write_session(root / "src" / "s.jsonl")
            files = reduce_session(main, root / "reduced")
            reduced = toolresults.load_run(root / "reduced" / "s.jsonl")
            original = toolresults.load_run(main)
            self.assertEqual(reduced.summary() | {"run_id": ""},
                             original.summary() | {"run_id": ""})
            text = (root / "reduced" / "s.jsonl").read_text()
            self.assertNotIn("pytest", text)
            self.assertNotIn("yyyy", text)
            self.assertIn('"kind":"test"', text)
            self.assertIn('"source":"hook"', text)
            self.assertNotIn("import", " ".join(leftover_strings(files)))

    def test_old_reduced_copy_has_no_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "old.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in [
                {"type": "assistant", "message": {"id": "m1", "model": "x", "usage": usage(100, 5),
                                                  "content": [{"type": "tool_use", "id": "t1",
                                                               "name": "Bash"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result",
                                                          "tool_use_id": "t1",
                                                          "content": "[removed]"}]}},
                {"type": "assistant", "message": {"id": "m2", "model": "x",
                                                  "usage": usage(300, 5), "content": []}},
            ]) + "\n")
            run = toolresults.load_run(path)
        s = run.summary()
        self.assertFalse(run.detail)
        self.assertIsNone(s["test_runs"])
        self.assertEqual(s["result_tokens"], 195)


class RecordTest(unittest.TestCase):
    def make_record(self, root: pathlib.Path, run_id: str, variant_name: str,
                    hook_lines=()) -> pathlib.Path:
        record = root / f"{run_id}.record"
        session = write_session(root / "src" / f"{run_id}.jsonl")
        reduce_session(session, record / "reduced" / "claude-session")
        (record / "reduced" / "claude-result.json").write_text(
            json.dumps({"session_id": run_id}))
        (record / "run.json").write_text(json.dumps({
            "meta": {"run_id": run_id, "variant": variant_name},
            "row": {"tests_pass": "yes"}}))
        if hook_lines:
            (record / "hook-logs").mkdir()
            (record / "hook-logs" / "filter-test-output.log").write_text(
                "\n".join(json.dumps(x) for x in hook_lines) + "\n")
        return record

    def test_hook_log_links_to_the_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.make_record(pathlib.Path(tmp), "r1", "test-output-hook", [
                {"event": "rewrite", "tool_use_id": "t5"},
                {"event": "filtered", "tool_use_id": "t5", "exit": 0, "bytes_in": 25671,
                 "bytes_out": 196}])
            run = toolresults.load_run(record)
            out = io.StringIO()
            with redirect_stdout(out):
                cli.main(["tool-results", str(record)])
        self.assertEqual(run.hook_log, {"rewrite": 1, "filtered": 1})
        self.assertEqual(run.summary()["hook_filtered"], 1)
        text = out.getvalue()
        self.assertIn("variant test-output-hook · tests pass yes", text)
        self.assertIn("hook: 25.1 KB -> 196 B, exit 0; approved by hook", text)
        self.assertIn("re-run, no change before it", text)
        self.assertIn("Test runs: 2 (re-runs with no change before them: 1)", text)

    def test_sweep_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.make_record(root, "r1", "verbose-tests")
            self.make_record(root, "r2", "test-output-hook",
                             [{"event": "filtered", "tool_use_id": "t5"}])
            log = root / "log.csv"
            cols = ["run_id", "task", "tool", "model", "effort", "variant"]
            log.write_text(",".join(cols) + "\n" + "\n".join(
                f"{rid},app-post-incident,claude,claude-sonnet-5-5,medium,{v}"
                for rid, v in (("r1", "verbose-tests"), ("r2", "test-output-hook"),
                               ("r3", "test-output-hook"))) + "\n")
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["tool-results", "--sweep", str(HOOK_SWEEP / "sweep.json"),
                                 "--log", str(log), "--runs-dir", str(root)])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("No hook (baseline)  r1", text)
        self.assertIn("r3   no record", text)
        self.assertIn("Medians of each setup", text)

    def test_sweep_view_counts_one_configuration(self):
        from tokenomics import runconfig
        current = runconfig.current_config("claude")
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for rid, v in (("old1", "verbose-tests"), ("new1", "verbose-tests"),
                           ("new2", "test-output-hook")):
                self.make_record(root, rid, v)
            log = root / "log.csv"
            cols = ["run_id", "task", "tool", "model", "effort", "variant", "config"]
            log.write_text(",".join(cols) + "\n" + "\n".join(
                f"{rid},app-post-incident,claude,claude-sonnet-5-5,medium,{v},{c}"
                for rid, v, c in (("old1", "verbose-tests", "eb648c47c588"),
                                  ("new1", "verbose-tests", current),
                                  ("new2", "test-output-hook", current))) + "\n")
            out = io.StringIO()
            with redirect_stdout(out):
                cli.main(["tool-results", "--sweep", str(HOOK_SWEEP / "sweep.json"),
                          "--log", str(log), "--runs-dir", str(root)])
        text = out.getvalue()
        self.assertIn("new1", text)
        self.assertNotIn("old1", text)
        self.assertIn("1 runs with another configuration not counted.", text)

    def test_harness_copies_hook_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "copy" / ".claude").mkdir(parents=True)
            (root / "copy" / ".claude" / "x.log").write_text('{"a": 1}\n{"a": 2}\n')
            (root / "copy" / ".claude" / "settings.json").write_text("{}")
            found = baseline.save_hook_logs(root / "copy", root / "rec")
            self.assertEqual(found, {"x.log": 2})
            self.assertTrue((root / "rec" / "hook-logs" / "x.log").is_file())
            self.assertEqual(baseline.save_hook_logs(root / "none", root / "rec2"), {})


class ExtendsTest(unittest.TestCase):
    def test_sample_variants(self):
        v = variant.load_variant(VERBOSE)
        h = variant.load_variant(HOOKED)
        self.assertEqual(v.files, ("AGENTS.md", "pytest.ini", "tests/test_validation_table.py"))
        self.assertEqual(h.base, "verbose-tests")
        self.assertEqual(h.files, (".claude/hooks/filter-test-output.py",
                                   ".claude/settings.json") + v.files)
        self.assertEqual(h.source("pytest.ini").resolve(),
                         (VERBOSE / "files" / "pytest.ini").resolve())
        settings = json.loads((HOOKED / "files" / ".claude" / "settings.json").read_text())
        hook = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "Bash")
        self.assertIn("filter-test-output.py", hook["hooks"][0]["command"])

    def test_override_hash_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name, meta, files in (
                    ("base", {}, {"a.txt": "base a", "b.txt": "base b"}),
                    ("child", {"extends": "../base"}, {"b.txt": "child b", "c.txt": "c"}),
                    ("other", {"extends": "../base", "workload": "status-board-infra"},
                     {"x": "x"}),
                    ("loop1", {"extends": "../loop2"}, {"x": "x"}),
                    ("loop2", {"extends": "../loop1"}, {"y": "y"})):
                (root / name / "files").mkdir(parents=True)
                (root / name / "variant.json").write_text(json.dumps(
                    {"name": name, "workload": "status-board-app"} | meta))
                for rel, text in files.items():
                    (root / name / "files" / rel).write_text(text)
            child = variant.load_variant(root / "child")
            self.assertEqual(child.files, ("a.txt", "b.txt", "c.txt"))
            copy = root / "copy"
            variant.apply_variant(child, copy)
            self.assertEqual((copy / "a.txt").read_text(), "base a")
            self.assertEqual((copy / "b.txt").read_text(), "child b")
            self.assertNotEqual(child.sha, variant.load_variant(root / "base").sha)
            with self.assertRaisesRegex(ValueError, "it extends changes status-board-app"):
                variant.load_variant(root / "other")
            with self.assertRaisesRegex(ValueError, "in a loop"):
                variant.load_variant(root / "loop1")

    def test_hash_of_a_plain_variant_did_not_change(self):
        v = variant.load_variant(FIXTURES / "bloated-context" / "variants"
                                 / "bloated-agents-md")
        self.assertEqual(v.sha, "fe095a6b8a75")
        self.assertEqual(v.base, "")


PASSING = """============================= test session starts ==============================
collected 3 items

tests/test_a.py::test_one PASSED                                         [ 33%]
tests/test_a.py::test_two PASSED                                         [ 66%]
tests/test_a.py::test_three PASSED                                       [100%]

=========================== short test summary info ============================
PASSED tests/test_a.py::test_one
PASSED tests/test_a.py::test_two
PASSED tests/test_a.py::test_three
============================== 3 passed in 0.10s ===============================
"""

FAILING = """============================= test session starts ==============================
tests/test_a.py::test_one PASSED                                         [ 50%]
tests/test_a.py::test_two FAILED                                         [100%]

=================================== FAILURES ===================================
___________________________________ test_two ___________________________________

>       assert 1 == 2
E       assert 1 == 2

tests/test_a.py:5: AssertionError
=============================== warnings summary ===============================
some warning text
=========================== short test summary info ============================
PASSED tests/test_a.py::test_one
FAILED tests/test_a.py::test_two - assert 1 == 2
========================= 1 failed, 1 passed in 0.10s ==========================
"""


class HookTest(unittest.TestCase):
    def setUp(self):
        self.hook = load_hook()

    def event(self, command, tmp):
        return {"tool_name": "Bash", "tool_use_id": "toolu_x", "cwd": tmp,
                "tool_input": {"command": command, "description": "Run tests"}}

    def test_filter_keeps_failures_errors_and_the_summary(self):
        kept, found = self.hook.filter_pytest(PASSING)
        self.assertTrue(found)
        self.assertEqual(kept, ["============================== 3 passed in 0.10s "
                                "==============================="])
        kept, found = self.hook.filter_pytest(FAILING)
        text = "\n".join(kept)
        self.assertIn("E       assert 1 == 2", text)
        self.assertIn("FAILED tests/test_a.py::test_two - assert 1 == 2", text)
        self.assertIn("short test summary info", text)
        self.assertTrue(kept[-1].startswith("=") and "1 failed, 1 passed" in kept[-1])
        self.assertNotIn("PASSED", text)
        self.assertNotIn("some warning text", text)
        self.assertEqual(self.hook.filter_pytest("152 passed in 1.01s\n")[0],
                         ["152 passed in 1.01s"])

    def test_no_summary_gives_the_full_output(self):
        text = "ERROR: usage: pytest [options]\nmore\n"
        kept, found = self.hook.filter_pytest(text)
        self.assertFalse(found)
        self.assertEqual(kept, text.splitlines())

    def test_rewrite_simple_commands(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": tmp}):
            for command, inner in (
                    ("./venv/bin/python -m pytest", "./venv/bin/python -m pytest"),
                    ("./venv/bin/python -m pytest -q 2>&1 | tail -5",
                     "./venv/bin/python -m pytest -q"),
                    ("venv/bin/pytest tests/test_api.py -k post | head -n 40",
                     "venv/bin/pytest tests/test_api.py -k post"),
                    ("cd /x/y && ./venv/bin/python -m pytest -x",
                     "cd /x/y && ./venv/bin/python -m pytest -x")):
                out = self.hook.rewrite(self.event(command, tmp))
                spec = out["hookSpecificOutput"]
                self.assertEqual(spec["permissionDecision"], "allow")
                self.assertEqual(spec["updatedInput"]["description"], "Run tests")
                argv = __import__("shlex").split(spec["updatedInput"]["command"])
                self.assertEqual(argv[2:5], ["--run", "--log",
                                             str(pathlib.Path(tmp) / ".claude"
                                                 / "filter-test-output.log")])
                self.assertEqual(argv[-1], inner, command)
            log = (pathlib.Path(tmp) / ".claude" / "filter-test-output.log").read_text()
            self.assertEqual(log.count('"event": "rewrite"'), 4)

    def test_other_commands_are_not_changed(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": tmp}):
            for command in ("ls tests", "cat x && ./venv/bin/python -m pytest",
                            "./venv/bin/python -m pytest; rm -rf /",
                            "./venv/bin/python -m pytest $(echo x)",
                            "python3 - <<'EOF'\nx\nEOF\n./venv/bin/python -m pytest -q"):
                self.assertIsNone(self.hook.rewrite(self.event(command, tmp)), command)
            self.assertIsNone(self.hook.rewrite({"tool_name": "Read", "tool_input": {}}))
            log = (pathlib.Path(tmp) / ".claude" / "filter-test-output.log").read_text()
            self.assertEqual(log.count('"event": "skip"'), 4)

    def test_run_keeps_the_exit_status_and_writes_the_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "m.log"
            script = pathlib.Path(tmp) / "fake.sh"
            script.write_text("printf '%s' \"$1\"; echo oops >&2; exit 3\n")
            proc = subprocess.run(
                [sys.executable, str(HOOK), "--run", "--log", str(log), "--id", "toolu_y",
                 "--", f"bash {script} " + __import__("shlex").quote(FAILING)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 3)
            self.assertIn("Exit status 3.", proc.stdout)
            self.assertIn("FAILED tests/test_a.py::test_two", proc.stdout)
            self.assertNotIn("PASSED", proc.stdout)
            entry = json.loads(log.read_text())
            self.assertEqual((entry["event"], entry["tool_use_id"], entry["exit"]),
                             ("filtered", "toolu_y", 3))
            self.assertGreater(entry["bytes_in"], entry["bytes_out"])

    def test_hook_never_blocks_on_bad_input(self):
        proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                              capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


class VerboseTestsTest(unittest.TestCase):
    def test_agents_md_adds_only_the_test_rule(self):
        lean = baseline.committed_file("status-board-app", "HEAD", "AGENTS.md").decode()
        new = (VERBOSE / "files" / "AGENTS.md").read_text()
        rule = ("Run the full suite as its own Bash command, after you finish your edits. Do "
                "not\nput the test run in the same command as an edit, and do not pipe the "
                "output to\n`tail` or `head`. Read the whole report, as CI does: `pytest.ini` "
                "turns on the\nverbose report.\n\n")
        self.assertIn(rule, new)
        self.assertEqual(new.replace(rule, ""), lean)

    def test_pytest_ini_and_the_table(self):
        ini = (VERBOSE / "files" / "pytest.ini").read_text()
        self.assertIn("addopts = -v -rA", ini)
        source = (VERBOSE / "files" / "tests" / "test_validation_table.py").read_text()
        self.assertEqual(source.count("pytest.param("), 85)

if __name__ == "__main__":
    unittest.main()
