"""Tests for demo tasks, variant arguments and pastes, /context, and
`tokenomics extensions`. Run from the repository root:

    python3 -m unittest discover -s tools/tests

The tests make no model request and no AWS call.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics import baseline, extensions, runconfig, tasks, variant  # noqa: E402
from tokenomics.sweep import Setup  # noqa: E402

REPO = TOOLS.parent
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
EXTENSIONS = FIXTURES / "extension-compare"


class FixtureTaskMixin:
    """Find demo tasks in the fixtures, not in the repository: the harness
    then finds the find-5xx-cause task in fixtures/extension-compare/task."""

    def setUp(self):
        super().setUp()
        for name, value in (("REPO_ROOT", FIXTURES), ("DEMO_TASK_GLOB", "*/task/task.json")):
            patcher = mock.patch.object(tasks, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

CONTEXT = """## Context Usage

**Model:** claude-sonnet-5-5
**Tokens:** 10.3k / 1m (1%)

### Estimated usage by category

| Category | Tokens | Percentage |
|----------|--------|------------|
| System prompt | 2.4k | 0.2% |
| MCP server instructions | 221 | 0.0% |
| MCP tools (deferred) | 22.1k | 2.2% |
| Skills | 2.3k | 0.2% |
| Messages | 10 | 0.0% |

### MCP Tools

| Tool | Server | Tokens |
|------|--------|--------|
| mcp__plugin_x_cloudwatch__describe_log_groups | plugin_x_cloudwatch | 1.5k |
| mcp__plugin_x_cloudwatch__get_metric_data | plugin_x_cloudwatch | 4.1k |

### Skills

| Skill | Source | Tokens |
|-------|--------|--------|
| dataviz | Built-in | ~480 |
| x:cloudwatch-5xx | Plugin (x) | ~70 |
"""


class ParseContextTest(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual(extensions.parse_tokens("2.4k"), 2400)
        self.assertEqual(extensions.parse_tokens("~70"), 70)
        self.assertEqual(extensions.parse_tokens("625"), 625)
        self.assertEqual(extensions.parse_tokens("1m"), 1_000_000)
        self.assertIsNone(extensions.parse_tokens("n/a"))

    def test_context(self):
        ctx = extensions.parse_context(CONTEXT)
        self.assertEqual(ctx["total"], 10300)
        self.assertEqual(ctx["window"], 1_000_000)
        self.assertEqual(ctx["categories"]["MCP server instructions"], 221)
        self.assertEqual(ctx["categories"]["MCP tools (deferred)"], 22100)
        self.assertEqual(len(ctx["mcp_tools"]), 2)
        self.assertEqual(ctx["skills"], [["x:cloudwatch-5xx", "Plugin (x)", 70]])
        self.assertEqual(ctx["builtin_skills"], 1)

    def test_always_on(self):
        a = extensions.always_on(extensions.parse_context(CONTEXT))
        self.assertEqual(a["tokens"], 291)
        self.assertEqual(a["deferred_tokens"], 22100)
        self.assertEqual(a["deferred_tools"], 2)
        self.assertEqual(extensions.up_front_text(a["parts"]),
                         "server instructions + skill description")
        self.assertEqual(extensions.up_front_text([]), "nothing")


class CompareTest(unittest.TestCase):
    def test_when_used_and_cost_per_success(self):
        contexts = {"mcp": {"categories": {"MCP server instructions": 200}},
                    "skill": {"categories": {}, "skills": [["s", "Project", 60]]}}
        mcp, skill, direct = (Setup("claude", "m-1", "medium", "MCP", variant="mcp"),
                              Setup("claude", "m-1", "medium", "Skill", variant="skill"),
                              Setup("claude", "m-1", "medium", "Direct", variant="direct"))
        row = {"tests_pass": "yes", "rules_followed": "yes", "cost_list_usd": "0.10",
               "wall_seconds": "30"}
        miss = dict(row, tests_pass="no", cost_list_usd="0.20")
        groups = [
            (mcp, False, [(row, extensions.RunTokens("a", 10200, 15200, 5))]),
            (skill, False, [(row, extensions.RunTokens("b", 10060, 12060, 4)),
                            (miss, extensions.RunTokens("c", 10060, 13060, 4))]),
            (direct, True, [(row, extensions.RunTokens("d", 15000, 16000, 2))]),
        ]
        out = extensions.compare(groups, contexts, hourly_rate=90, rework_minutes=15)
        by = {r.label: r for r in out}
        self.assertEqual(by["MCP"].always_on, 200)
        self.assertEqual(by["MCP"].when_used, 5000)
        self.assertEqual(by["Skill"].when_used, 2500)
        self.assertEqual(by["Skill"].found_text(), "1 of 2")
        # Start with no extension: 10200 - 200 and 10060 - 60 -> 10000.
        self.assertEqual(by["Direct"].always_on, 0)
        self.assertEqual(by["Direct"].when_used, 6000)
        self.assertEqual(by["MCP"].cost_per_success, 0.10)
        # Skill: median cost 0.15, pass rate 1/2, one miss of 15 min at $90/h.
        self.assertAlmostEqual(by["Skill"].cost_per_success, 0.15 / 0.5 + 22.5)


class DemoTaskTest(FixtureTaskMixin, unittest.TestCase):
    def test_demo_task_is_found_but_not_in_all(self):
        self.assertNotIn("find-5xx-cause", tasks.list_tasks())
        self.assertIn("find-5xx-cause", tasks.demo_tasks())
        task = baseline.load_task("find-5xx-cause")
        self.assertEqual(task["dir"], EXTENSIONS / "task")
        self.assertEqual(baseline.match_task("find-5xx"), "find-5xx-cause")
        self.assertIn(task["rules"]["static"], baseline.STATIC_RULES)

    def test_task_settings_give_the_config(self):
        own = runconfig.config_file("claude", "find-5xx-cause")
        self.assertEqual(own, EXTENSIONS / "task" / "claude-settings.json")
        self.assertNotEqual(runconfig.current_config("claude", "find-5xx-cause"),
                            runconfig.current_config("claude"))
        self.assertEqual(runconfig.current_config("claude", "app-post-incident"),
                         runconfig.current_config("claude"))
        self.assertEqual(runconfig.config_file("codex", "find-5xx-cause"),
                         runconfig.CONFIG_DIR / "codex-overrides.json")

    def test_settings_are_read_only_for_aws(self):
        settings = json.loads((EXTENSIONS / "task" / "claude-settings.json").read_text())
        allow = settings["permissions"]["allow"]
        aws = [r for r in allow if r.startswith("Bash(aws ")]
        self.assertTrue(aws)
        for rule in aws:
            verb = rule.split()[2]
            self.assertTrue(verb.startswith(("describe-", "filter-", "get-", "list-",
                                             "start-query", "tail")), rule)
        self.assertEqual(settings["env"]["AWS_PROFILE"], "tokenomics-agent")
        self.assertIn("Bash(aws * --profile *)", settings["permissions"]["deny"])
        self.assertTrue(settings["disableClaudeAiConnectors"])

    def test_agent_command_for_the_demo_task(self):
        task = baseline.load_task("find-5xx-cause")
        plugin = variant.load_variant(EXTENSIONS / "variants" / "plugin")
        argv = baseline.agent_command("claude", "claude-sonnet-5-5", "medium", "Q",
                                      pathlib.Path("/copy"), task=task, variant=plugin)
        self.assertNotIn("--strict-mcp-config", argv)
        self.assertNotIn("--disable-slash-commands", argv)
        self.assertEqual(argv[argv.index("--settings") + 1],
                         str(EXTENSIONS / "task" / "claude-settings.json"))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[-2:], ["--plugin-dir", "/copy/plugins/cloudwatch-logs"])
        base = baseline.agent_command("claude", "claude-sonnet-5-5", "medium", "Q",
                                      pathlib.Path("/copy"))
        self.assertIn("--strict-mcp-config", base)
        self.assertIn("--disable-slash-commands", base)

    def test_agent_env_with_a_profile(self):
        env = {"PATH": "/bin", "AWS_PROFILE": "admin", "AWS_ACCESS_KEY_ID": "k",
               "AWS_SECRET_ACCESS_KEY": "k", "AWS_SESSION_TOKEN": "t"}
        with mock.patch.dict(os.environ, env, clear=True):
            out = baseline.agent_env(pathlib.Path("/cache"), "tokenomics-agent")
        self.assertEqual(out["AWS_PROFILE"], "tokenomics-agent")
        for gone in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            self.assertNotIn(gone, out)
        self.assertNotIn("AWS_CONFIG_FILE", out)
        self.assertNotIn("AWS_SHARED_CREDENTIALS_FILE", out)

    def test_read_only_rule(self):
        self.assertTrue(baseline.read_only_rule(REPO, {}, [])[0]["ok"])
        self.assertFalse(baseline.read_only_rule(REPO, {}, ["app/main.py"])[0]["ok"])

    def test_final_answer(self):
        self.assertEqual(baseline.final_answer("claude", json.dumps({"result": "Cause: x"})),
                         "Cause: x")
        self.assertEqual(baseline.final_answer("claude", "not json"), "")
        events = "\n".join(json.dumps(e) for e in [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "one"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "two"}}])
        self.assertEqual(baseline.final_answer("codex", events), "two")

    def test_check_commands_get_the_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            answer = tmp / "answer.md"
            answer.write_text("Cause: the ECS task role lost its DynamoDB policy, so "
                              "dynamodb:Scan gets AccessDeniedException.\n")
            task = baseline.load_task("find-5xx-cause")
            out = baseline.run_commands(task["acceptance"]["commands"], tmp, dict(os.environ),
                                        tmp / "checks.log",
                                        {"task": str(task["dir"]), "answer": str(answer)})
            self.assertTrue(out[0]["ok"])
            answer.write_text("Cause: the DynamoDB table is gone.\n")
            out = baseline.run_commands(task["acceptance"]["commands"], tmp, dict(os.environ),
                                        tmp / "checks.log",
                                        {"task": str(task["dir"]), "answer": str(answer)})
            self.assertFalse(out[0]["ok"])


class CheckAnswerTest(unittest.TestCase):
    def check(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(text)
        try:
            proc = subprocess.run([sys.executable, str(EXTENSIONS / "task" / "check-answer.py"),
                                   f.name, "--json"], capture_output=True, text=True)
        finally:
            os.unlink(f.name)
        return json.loads(proc.stdout)["cause_found"], proc.returncode

    def test_answers(self):
        self.assertEqual(self.check("The task role tokenomics-status-board-task is not "
                                    "authorized to perform dynamodb:Scan."), (True, 0))
        self.assertEqual(self.check("Cause: IAM permission missing on the ECS task role for "
                                    "DynamoDB."), (True, 0))
        self.assertEqual(self.check("The DynamoDB table does not exist."), (False, 1))
        self.assertEqual(self.check("AccessDenied on DynamoDB for the task execution role "
                                    "tokenomics-status-board-task-execution."), (False, 1))
        self.assertEqual(self.check(""), (False, 1))


class VariantArgsTest(unittest.TestCase):
    def make(self, tmp, meta, paste=None):
        root = pathlib.Path(tmp) / meta["name"]
        root.mkdir()
        if paste is not None:
            (root / "paste.txt").write_text(paste)
            meta = dict(meta, paste="paste.txt")
        (root / "variant.json").write_text(json.dumps(meta))
        return root

    def test_paste_only_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp, {"name": "p", "workload": "w"}, "line 1\nline 2\n")
            v = variant.load_variant(root)
            self.assertEqual(v.files, ())
            prompt = variant.pasted_prompt("Question?\n", v)
            self.assertEqual(prompt, "Question?\n\nPasted file paste.txt:\n\n```text\n"
                                     "line 1\nline 2\n```\n")
            self.assertEqual(variant.pasted_prompt("Q", None), "Q")
            sha = v.sha
            (root / "paste.txt").write_text("other\n")
            self.assertNotEqual(variant.load_variant(root).sha, sha)

    def test_args_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp, {"name": "a", "workload": "w",
                                   "claude_args": ["--mcp-config", "{copy}/.mcp.json"]})
            v = variant.load_variant(root)
            self.assertEqual(variant.expand_args(v, pathlib.Path("/c")),
                             ["--mcp-config", "/c/.mcp.json"])
            self.assertEqual(variant.expand_args(None, pathlib.Path("/c")), [])

    def test_empty_variant_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(tmp, {"name": "e", "workload": "w"})
            with self.assertRaises(ValueError):
                variant.load_variant(root)

    def test_sample_variants_load(self):
        for name in ("direct-file", "mcp-server", "skill", "plugin"):
            v = variant.load_variant(EXTENSIONS / "variants" / name)
            self.assertEqual(v.workload, "status-board-app")
        direct = variant.load_variant(EXTENSIONS / "variants" / "direct-file")
        self.assertIsNotNone(direct.paste)
        self.assertEqual(direct.files, ())


class AnswerViewTest(unittest.TestCase):
    def test_print_answer(self):
        import io
        from contextlib import redirect_stdout
        record = {"row": {"tests_pass": "yes", "cost_list_usd": 0.0429, "wall_seconds": 5.5},
                  "checks": {"acceptance": [{"rule": "cause found", "ok": True}]}}
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "answer.md").write_text("Cause: x\n")
            out = io.StringIO()
            with redirect_stdout(out):
                baseline.print_answer(record, pathlib.Path(tmp))
        self.assertEqual(out.getvalue(),
                         "Cause: x\n\ncause found: yes · $0.0429 at list price · 5.5 s\n")


class CaptureContextTest(FixtureTaskMixin, unittest.TestCase):
    def test_capture_makes_the_copy_and_runs_context(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"num_turns": 0, "total_cost_usd": 0, "result": CONTEXT}), "")

        with tempfile.TemporaryDirectory() as tmp:
            args = baseline.build_parser().parse_args([
                "find-5xx-cause", "--tool", "claude", "--model", "claude-sonnet-5-5",
                "--effort", "medium", "--variant", str(EXTENSIONS / "variants" / "skill"),
                "--runs-dir", tmp, "--log", str(pathlib.Path(tmp) / "log.csv"),
                "--run-id", "20260101-000000-ctx"])
            p = baseline.plan_run(args)
            with mock.patch.dict(os.environ, {}, clear=False), \
                    mock.patch.object(baseline, "claude_version", return_value="9.9.9"), \
                    mock.patch.object(baseline, "refuse_api_keys"):
                data = baseline.capture_context(p, log=lambda *_: None, run=fake_run)
            copy = pathlib.Path(tmp) / "20260101-000000-ctx"
            self.assertTrue((copy / ".claude/skills/cloudwatch-5xx/SKILL.md").is_file())
            self.assertFalse((pathlib.Path(tmp) / "20260101-000000-ctx.record").exists())
        argv, kw = calls[0]
        self.assertEqual(argv[2], "/context")
        self.assertEqual(kw["env"]["AWS_PROFILE"], "tokenomics-agent")
        self.assertEqual(data["variant"], "skill")
        self.assertEqual(data["claude_version"], "9.9.9")
        self.assertEqual(extensions.always_on(data)["tokens"], 291)

    def test_capture_refuses_a_model_request(self):
        def fake_run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"num_turns": 1, "total_cost_usd": 0.01, "result": ""}), "")

        with tempfile.TemporaryDirectory() as tmp:
            args = baseline.build_parser().parse_args([
                "find-5xx-cause", "--tool", "claude", "--model", "claude-sonnet-5-5",
                "--effort", "medium", "--runs-dir", tmp,
                "--log", str(pathlib.Path(tmp) / "log.csv"), "--run-id", "20260101-000001-c"])
            p = baseline.plan_run(args)
            with mock.patch.object(baseline, "refuse_api_keys"), \
                    self.assertRaises(ValueError):
                baseline.capture_context(p, log=lambda *_: None, run=fake_run)

    def test_baseline_task_cannot_run_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = baseline.build_parser().parse_args([
                "app-post-incident", "--tool", "claude", "--model", "claude-sonnet-5-5",
                "--effort", "medium", "--runs-dir", tmp,
                "--log", str(pathlib.Path(tmp) / "log.csv")])
            with self.assertRaises(ValueError):
                baseline.capture_context(baseline.plan_run(args))


if __name__ == "__main__":
    unittest.main()
