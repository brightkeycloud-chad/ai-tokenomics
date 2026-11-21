"""Tests for the baseline run harness. Run from the repository root:

    python3 -m unittest discover -s tools/tests

The tests make no model request and no AWS call. Some tests copy a workload
with `git archive` and apply a reference patch with `git apply`.
"""
import csv
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics import baseline  # noqa: E402
from tokenomics.runconfig import LEGACY_CONFIG, current_config  # noqa: E402
from test_costmodel import run_cli  # noqa: E402

REPO = TOOLS.parent
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def workload_with_reference(task_id: str, dest: pathlib.Path) -> pathlib.Path:
    """A git copy of the workload of a task, with the reference patch applied."""
    task = baseline.load_task(task_id)
    baseline.copy_workload(task["workload"], "HEAD", dest)
    baseline.start_repo(dest)
    subprocess.run(["git", "apply", str(task["dir"] / task["reference"])], cwd=dest, check=True)
    baseline.commit_result(dest)
    return dest


class TasksTest(unittest.TestCase):
    def test_both_baseline_tasks_exist(self):
        self.assertEqual(baseline.list_tasks(), ["app-post-incident", "infra-5xx-alarm"])

    def test_task_files_exist(self):
        for task_id in baseline.list_tasks():
            task = baseline.load_task(task_id)
            self.assertTrue((task["dir"] / task["prompt"]).is_file())
            self.assertTrue((task["dir"] / task["reference"]).is_file())
            for src in list(task["acceptance"]["copy"]) + list(task["rules"]["copy"]):
                self.assertTrue((task["dir"] / src).is_file(), src)
            self.assertIn(task["rules"]["static"], baseline.STATIC_RULES)

    def test_unknown_task(self):
        with self.assertRaises(FileNotFoundError):
            baseline.load_task("no-such-task")

    def test_model_alias_is_refused(self):
        for alias in ("sonnet", "opus", "sol", "luna"):
            with self.assertRaises(ValueError):
                baseline.check_model_id(alias)
        baseline.check_model_id("claude-sonnet-5-5")
        baseline.check_model_id("gpt-6.1-sol")


class CommandTest(unittest.TestCase):
    def test_claude_command_pins_every_setting(self):
        argv = baseline.agent_command("claude", "claude-sonnet-5-5", "medium", "PROMPT",
                                      pathlib.Path("/copy"))
        self.assertEqual(argv[:3], ["claude", "-p", "PROMPT"])
        for pair in (["--model", "claude-sonnet-5-5"], ["--effort", "medium"],
                     ["--output-format", "json"], ["--setting-sources", "project"],
                     ["--permission-mode", baseline.claude_permission_mode()]):
            i = argv.index(pair[0])
            self.assertEqual(argv[i + 1], pair[1])
        self.assertIn("--strict-mcp-config", argv)
        settings = argv[argv.index("--settings") + 1]
        self.assertTrue(pathlib.Path(settings).is_file())

    def test_claude_permission_mode_comes_from_the_settings_file(self):
        """The mode is in the settings file, so the config hash covers it."""
        import json
        settings = json.loads((baseline.CONFIG_DIR / "claude-settings.json").read_text())
        expected = settings.get("permissions", {}).get("defaultMode", "dontAsk")
        self.assertEqual(baseline.claude_permission_mode(), expected)
        # Deny rules block in every mode, so the dangerous commands stay denied.
        deny = settings["permissions"]["deny"]
        for rule in ("Bash(terraform apply:*)", "Bash(git push:*)", "Bash(aws:*)",
                     "Bash(./venv/bin/python -m pip:*)"):
            self.assertIn(rule, deny)

    def test_codex_command_pins_every_setting(self):
        argv = baseline.agent_command("codex", "gpt-6.1-sol", "medium", "PROMPT",
                                      pathlib.Path("/copy"), tmp="/private/tmp/x")
        self.assertEqual(argv[:3], ["codex", "exec", "--json"])
        self.assertIn("--ignore-user-config", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="medium"', argv)
        self.assertIn('default_permissions="baseline"', argv)
        self.assertIn('permissions.baseline.network.unix_sockets={"/private/tmp/x" = "allow"}',
                      argv)
        self.assertEqual(argv[-3:], ["-C", "/copy", "PROMPT"])

    def test_toml_values(self):
        self.assertEqual(baseline.toml_value(True), "true")
        self.assertEqual(baseline.toml_value("a"), '"a"')
        self.assertEqual(baseline.toml_value({"k": "v", "n": 2}), '{"k" = "v", "n" = 2}')
        self.assertEqual(baseline.toml_value(["a", False]), '["a", false]')

    def test_show_command_hides_the_prompt(self):
        argv = ["claude", "-p", "long prompt text", "--settings", "/repo/baseline/config/x.json"]
        shown = baseline.show_command(argv, "long prompt text",
                                      {"/repo/baseline/config": "baseline/config"})
        self.assertEqual(shown, 'claude -p "$(cat prompt.md)" --settings '
                                'baseline/config/x.json')

    def test_agent_env_removes_keys_and_session_variables(self):
        env = {"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k",
               "AWS_PROFILE": "admin", "AWS_ACCESS_KEY_ID": "k", "CLAUDECODE": "1",
               "CLAUDE_EFFORT": "xhigh", "CLAUDE_CODE_SESSION_ID": "s",
               "CLAUDE_CONFIG_DIR": "/cfg", "CODEX_HOME": "/codex"}
        with mock.patch.dict(os.environ, env, clear=True):
            out = baseline.agent_env(pathlib.Path("/cache"))
        for gone in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_PROFILE", "AWS_ACCESS_KEY_ID",
                     "CLAUDECODE", "CLAUDE_EFFORT", "CLAUDE_CODE_SESSION_ID"):
            self.assertNotIn(gone, out)
        self.assertEqual(out["CLAUDE_CONFIG_DIR"], "/cfg")
        self.assertEqual(out["CODEX_HOME"], "/codex")
        self.assertEqual(out["AWS_SHARED_CREDENTIALS_FILE"], os.devnull)
        self.assertEqual(out["TF_PLUGIN_CACHE_DIR"], "/cache")

    def test_api_key_stops_a_run(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
            with self.assertRaises(ValueError):
                baseline.refuse_api_keys()


class DryRunTest(unittest.TestCase):
    def test_dry_run_shows_the_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_cli_baseline(["app-post-incident", "--tool", "claude", "--model",
                                          "claude-sonnet-5-5", "--effort", "medium",
                                          "--dry-run", "--run-id", "r1", "--runs-dir", tmp,
                                          "--log", f"{tmp}/log.csv"])
            self.assertEqual(code, 0)
            self.assertIn("--model claude-sonnet-5-5 --effort medium", out)
            self.assertIn("(repetition 1)", out)
            self.assertFalse((pathlib.Path(tmp) / "r1").exists())

    def test_model_and_effort_are_required(self):
        code, _ = run_cli_baseline(["app-post-incident", "--tool", "codex", "--dry-run"])
        self.assertEqual(code, 2)


def run_cli_baseline(argv):
    buf = io.StringIO()
    with redirect_stdout(buf), mock.patch("sys.stderr", io.StringIO()):
        code = baseline.main(argv)
    return code, buf.getvalue()


class AppRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = workload_with_reference("app-post-incident", pathlib.Path(self.tmp.name) / "c")
        self.start = {"app/main.py": subprocess.run(
            ["git", "show", f"{baseline.START_TAG}:app/main.py"], cwd=self.root,
            capture_output=True, text=True, check=True).stdout}
        self.changed = ["app/main.py", "app/models.py", "tests/test_api.py"]

    def tearDown(self):
        self.tmp.cleanup()

    def rules(self):
        return {r["rule"][0]: r["ok"] for r in
                baseline.app_house_rules(self.root, self.start, self.changed)}

    def edit(self, old, new):
        path = self.root / "app" / "main.py"
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))

    def test_reference_obeys_every_rule(self):
        self.assertEqual(self.rules(), {"1": True, "2": True, "3": True, "4": True, "5": True})

    def test_boto3_in_main_breaks_rule_2(self):
        self.edit("import logging\n", "import logging\nimport boto3\n")
        self.assertFalse(self.rules()["2"])

    def test_a_docstring_that_names_boto3_is_not_a_call(self):
        self.edit('"""Service Status Board.', '"""Service Status Board. No boto3 here.')
        self.assertTrue(self.rules()["2"])

    def test_inline_validation_breaks_rule_1(self):
        self.edit("        status = check_status(body.status)\n"
                  "        message = check_message(body.message)\n",
                  "        status, message = body.status, body.message\n")
        self.assertFalse(self.rules()["1"])

    def test_a_dict_return_breaks_rule_3(self):
        self.edit("    response_model=Incident,\n", "")
        self.edit("def api_create_incident(service_id: str, body: IncidentCreate) -> Incident:",
                  "def api_create_incident(service_id: str, body: IncidentCreate) -> dict:")
        self.assertFalse(self.rules()["3"])

    def test_no_new_tests_breaks_rule_4(self):
        self.changed = ["app/main.py", "app/models.py"]
        self.assertFalse(self.rules()["4"])

    def test_a_changed_healthz_breaks_rule_5(self):
        self.edit('    return HealthResponse(status="ok")',
                  '    store.list_services()\n    return HealthResponse(status="ok")')
        self.assertFalse(self.rules()["5"])


class InfraRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = workload_with_reference("infra-5xx-alarm",
                                            pathlib.Path(self.tmp.name) / "c")
        self.alarms = self.root / "terraform" / "alarms.tf"

    def tearDown(self):
        self.tmp.cleanup()

    def rules(self):
        baseline.commit_result(self.root)
        return {r["rule"][0]: r for r in
                baseline.infra_house_rules(self.root, {}, ["terraform/alarms.tf"])}

    def test_reference_obeys_the_static_rules(self):
        self.assertTrue(all(r["ok"] for r in self.rules().values()))

    def test_missing_description_breaks_rule_4(self):
        self.alarms.write_text(self.alarms.read_text().replace(
            '  description = "SNS topic that receives the load balancer 5xx alarm."\n', ""))
        rule = self.rules()["4"]
        self.assertFalse(rule["ok"])
        self.assertEqual(rule["failed"], ["output alarm_topic_arn"])

    def test_hard_coded_region_breaks_rule_3(self):
        self.alarms.write_text(self.alarms.read_text().replace(
            '"${local.name}-alarms"\n\n  tags', '"us-east-1-alarms"\n\n  tags'))
        self.assertFalse(self.rules()["3"]["ok"])

    def test_old_hard_coded_lines_do_not_count(self):
        # variables.tf has default = "us-east-1" from the start. Only added lines count.
        path = self.root / "terraform" / "variables.tf"
        path.write_text(path.read_text() + '\n# a comment\n')
        self.assertTrue(self.rules()["3"]["ok"])

    def test_hcl_blocks_skip_braces_in_strings_and_comments(self):
        text = ('variable "a" {\n  # a comment with { a brace\n  default = "${x}}"\n'
                '  description = "d"\n}\n\noutput "b" {\n  value = { k = "}" }\n}\n')
        self.assertIn("description", baseline.hcl_blocks(text, "variable")["a"])
        self.assertIn('k = "}"', baseline.hcl_blocks(text, "output")["b"])


class RowTest(unittest.TestCase):
    record = {
        "meta": {"run_id": "r1", "date": "2026-11-16", "rep": 2, "task": "app-post-incident",
                 "tool": "claude", "model": "claude-sonnet-5-5", "effort": "medium",
                 "workload": "status-board-app", "workload_tree": "abc", "prompt_sha": "def",
                 "config": current_config("claude")},
        "agent": {"exit_code": 0, "timed_out": False, "wall_seconds": 61.26},
        "checks": {"acceptance": [{"rule": "pytest", "ok": True, "failed": []}],
                   "rules": [{"rule": "1 validate", "ok": True, "failed": []},
                             {"rule": "4 test every route", "ok": False, "failed": []}]},
    }

    def usage(self, **changes):
        usage = baseline.empty_usage() | {
            "tool_version": "2.1.325", "models": ["claude-sonnet-5-5"], "input": 10,
            "cache_write": 2000, "cache_read": 30000, "output": 900, "context_peak": 25000,
            "cache_hit": 0.937, "cost_list_usd": 0.0313, "vendor_estimate_usd": 0.0312,
            "api_seconds": 40.5, "lanes": 1, "permission_denials": 0}
        return usage | changes

    def test_row_has_every_column(self):
        row = baseline.build_row(self.record, self.usage())
        self.assertEqual(list(row), baseline.COLUMNS)
        self.assertEqual(row["tests_pass"], "yes")
        self.assertEqual(row["rules_followed"], "no")
        self.assertEqual(row["rules_broken"], "4 test every route")
        self.assertEqual(row["wall_seconds"], 61.3)
        self.assertEqual(row["reasoning"], "")
        self.assertEqual(row["notes"], "")

    def test_config(self):
        row = baseline.build_row(self.record, self.usage())
        self.assertEqual(row["config"], current_config("claude"))
        self.assertRegex(row["config"], r"^[0-9a-f]{12}$")
        # A record from before the config column used the legacy configuration.
        old = dict(self.record, meta={k: v for k, v in self.record["meta"].items()
                                      if k != "config"})
        self.assertEqual(baseline.build_row(old, self.usage())["config"],
                         LEGACY_CONFIG["claude"])

    def test_dry_run_shows_the_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                baseline.main(["app-post-incident", "--tool", "claude", "--model",
                               "claude-sonnet-5-5", "--effort", "medium", "--dry-run",
                               "--log", f"{tmp}/log.csv", "--runs-dir", tmp])
            self.assertIn("  Config      baseline/config/claude-settings.json (sha256 "
                          f"{current_config('claude')})", out.getvalue())

    def test_notes(self):
        row = baseline.build_row(self.record, self.usage(
            permission_denials=2, lanes=2, models=["claude-sonnet-5-5", "claude-haiku-5-5"]))
        self.assertEqual(row["notes"], "2 permission denials; also used claude-haiku-5-5; "
                                       "2 lanes")

    def test_log_round_trip_and_repetitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            row = baseline.build_row(self.record, self.usage())
            baseline.append_row(log, row)
            baseline.append_row(log, row)
            rows = baseline.read_log(log)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["cache_read"], "30000")
            self.assertEqual(baseline.next_rep(rows, "app-post-incident", "claude",
                                               "claude-sonnet-5-5", "medium"), 3)
            self.assertEqual(baseline.next_rep(rows, "app-post-incident", "claude",
                                               "claude-sonnet-5-5", "high"), 1)
            code, out, _ = run_cli("log", str(log))
            self.assertEqual(code, 0)
            self.assertIn("2 runs", out)
            self.assertIn("$0.0313", out)
            self.assertIn("4 test every route", out)

    def test_other_columns_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            log.write_text("a,b\n1,2\n")
            with self.assertRaises(ValueError):
                baseline.append_row(log, baseline.build_row(self.record, self.usage()))

    def test_template_has_the_columns(self):
        with open(REPO / "results" / "results-log-template.csv", newline="") as fin:
            self.assertEqual(next(csv.reader(fin)), baseline.COLUMNS)


class DiffTest(unittest.TestCase):
    """--diff: what the agent changed in a run."""

    def write_log(self, log: pathlib.Path, rows: list[tuple]) -> None:
        with open(log, "w", newline="") as fout:
            writer = csv.DictWriter(fout, fieldnames=baseline.COLUMNS, restval="")
            writer.writeheader()
            for run_id, task, tool, rep in rows:
                writer.writerow({"run_id": run_id, "task": task, "tool": tool, "rep": rep,
                                 "model": "m-1", "effort": "medium"})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = pathlib.Path(self.tmp.name)
        self.log = self.runs / "log.csv"
        self.write_log(self.log, [
            ("20261116-100000-infra-5xx-alarm-claude", "infra-5xx-alarm", "claude", 1),
            ("20261116-100100-infra-5xx-alarm-codex", "infra-5xx-alarm", "codex", 1),
            ("20261116-100200-app-post-incident-claude", "app-post-incident", "claude", 1),
            ("20261116-100300-infra-5xx-alarm-claude", "infra-5xx-alarm", "claude", 2),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def find(self, selector, task=None, **filters):
        copy_dir, _ = baseline.find_run(selector, task, self.runs, self.log, **filters)
        return copy_dir.name

    def test_task_names_and_short_names(self):
        self.assertEqual(baseline.match_task("app"), "app-post-incident")
        self.assertEqual(baseline.match_task("infra"), "infra-5xx-alarm")
        self.assertEqual(baseline.match_task("infra-5xx-alarm"), "infra-5xx-alarm")
        with self.assertRaises(ValueError):
            baseline.match_task("web")

    def test_latest_run_of_a_task(self):
        self.assertEqual(self.find("infra"), "20261116-100300-infra-5xx-alarm-claude")
        self.assertEqual(self.find(None, "app"), "20261116-100200-app-post-incident-claude")

    def test_filters(self):
        self.assertEqual(self.find("infra", tool="codex"),
                         "20261116-100100-infra-5xx-alarm-codex")
        self.assertEqual(self.find("infra", tool="claude", rep=1),
                         "20261116-100000-infra-5xx-alarm-claude")
        with self.assertRaises(FileNotFoundError):
            self.find("infra", tool="claude", rep=3)
        with self.assertRaises(FileNotFoundError):
            self.find("app", model="m-2")

    def test_run_id_and_directories(self):
        run_id = "20261116-100100-infra-5xx-alarm-codex"
        copy_dir, record_dir = baseline.find_run(run_id, None, self.runs, self.log)
        self.assertEqual((copy_dir, record_dir), (self.runs / run_id,
                                                  self.runs / f"{run_id}.record"))
        record_dir.mkdir()
        copy_dir.mkdir()
        self.assertEqual(baseline.find_run(str(record_dir), None, self.runs, self.log),
                         (copy_dir, record_dir))
        self.assertEqual(baseline.find_run(str(copy_dir), None, self.runs, self.log),
                         (copy_dir, record_dir))

    def test_no_selector(self):
        with self.assertRaises(ValueError):
            baseline.find_run(None, None, self.runs, self.log)

    def test_diff_from_the_copy_then_from_the_patch(self):
        copy_dir = workload_with_reference("app-post-incident", self.runs / "run")
        record_dir = self.runs / "run.record"
        record_dir.mkdir()
        baseline.save_patch(copy_dir, record_dir)
        # The hidden tests come after the result commit. They are not in the diff.
        (copy_dir / "tests" / "test_baseline_acceptance.py").write_text("# hidden\n")
        patch = baseline.agent_diff(copy_dir, record_dir)
        self.assertIn("+++ b/app/main.py", patch)
        self.assertNotIn("test_baseline_acceptance", patch)
        self.assertEqual(patch, (record_dir / "agent.patch").read_text())
        stat = baseline.agent_diff(copy_dir, record_dir, stat=True)
        self.assertIn("app/main.py", stat)
        self.assertIn("changed", stat)
        # After cleanup of the copy, the record keeps the change.
        subprocess.run(["rm", "-rf", str(copy_dir)], check=True)
        self.assertEqual(baseline.agent_diff(copy_dir, record_dir), patch)
        self.assertIn("app/main.py", baseline.agent_diff(copy_dir, record_dir, stat=True))
        (record_dir / "agent.patch").unlink()
        with self.assertRaises(FileNotFoundError):
            baseline.agent_diff(copy_dir, record_dir)

    def test_command_line(self):
        run_id = "20261116-100200-app-post-incident-claude"
        workload_with_reference("app-post-incident", self.runs / run_id)
        out = io.StringIO()
        with redirect_stdout(out):
            code = baseline.main(["--diff", "app", "--stat", "--runs-dir", str(self.runs),
                                  "--log", str(self.log)])
        self.assertEqual(code, 0)
        self.assertIn("app/main.py", out.getvalue())
        self.assertNotIn("Tests pass", out.getvalue())  # this run has no record


class UpToTest(unittest.TestCase):
    """--up-to N: skip the run when the setup already has N runs in the log."""

    def run_main(self, log, up_to):
        out = io.StringIO()
        with redirect_stdout(out):
            code = baseline.main(["app-post-incident", "--tool", "claude", "--model",
                                  "claude-sonnet-5-5", "--effort", "medium", "--dry-run",
                                  "--up-to", str(up_to), "--log", str(log),
                                  "--runs-dir", str(log.parent)])
        return code, out.getvalue()

    def test_skip_when_the_setup_has_enough_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            row = baseline.build_row(RowTest.record, RowTest.usage(RowTest()))
            for _ in range(3):
                baseline.append_row(log, row)
            code, out = self.run_main(log, 3)
            self.assertEqual(code, 0)
            self.assertIn("Skip:", out)
            self.assertIn("has 3 runs of app-post-incident", out)
            code, out = self.run_main(log, 4)
            self.assertNotIn("Skip:", out)
            self.assertIn("(repetition 4)", out)
            # Another effort is another setup.
            other = dict(row, effort="high")
            baseline.append_row(log, other)
            code, out = self.run_main(log, 4)
            self.assertIn("(repetition 4)", out)

    def test_runs_of_an_old_configuration_do_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            row = baseline.build_row(RowTest.record, RowTest.usage(RowTest()))
            for _ in range(3):
                baseline.append_row(log, dict(row, config=LEGACY_CONFIG["claude"]))
            code, out = self.run_main(log, 3)
            self.assertNotIn("Skip:", out)
            self.assertIn("(repetition 1)", out)


class UsageTest(unittest.TestCase):
    def test_codex_window_share_needs_the_5_hour_window(self):
        report = {"usage": {"input": 1, "cache_write_5m": 0, "cache_write_1h": 0,
                            "cache_read": 2, "output": 3, "reasoning": 1},
                  "limits": {"primary": {"window_minutes": 300, "used_percent_first": 4.0,
                                         "used_percent_last": 6.5}},
                  "time": {"generating": 12.34}, "context": {"peak": 9},
                  "ratios": {"cache_hit": 0.5}, "version": "0.169.0", "models": ["m"],
                  "api": {"unit": "USD", "cost": 0.12345, "unpriced_models": []},
                  "credits": {"unit": "credits", "cost": 1.5, "unpriced_models": []},
                  "complete": True, "turns": 1, "failed_turns": 0}
        usage = baseline.codex_usage(report)
        self.assertEqual(usage["window_5h_pct"], 2.5)
        self.assertEqual(usage["cost_list_usd"], 0.1235)
        self.assertEqual(usage["api_seconds"], 12.3)
        report["limits"]["primary"]["window_minutes"] = 10080
        self.assertIsNone(baseline.codex_usage(report)["window_5h_pct"])

    def test_reduced_claude_result_has_no_text(self):
        result = {"type": "result", "result": "SECRET-ANSWER", "session_id": "s",
                  "total_cost_usd": 0.1, "permission_denials": [
                      {"tool_name": "Bash", "tool_input": {"command": "SECRET-COMMAND"}}]}
        reduced = baseline.reduce_claude_result(result)
        self.assertNotIn("SECRET", str(reduced))
        self.assertEqual(reduced["permission_denials"], [{"tool_name": "Bash"}])

    def test_run_lines_of_terraform_test(self):
        out = 'run "a"... pass\nrun "b_rule"... fail\nrun "c"... skip\n'
        self.assertEqual([n for n, s in baseline.RUN_LINE.findall(out) if s == "fail"],
                         ["b_rule"])


if __name__ == "__main__":
    unittest.main()


class ResolveRecordTest(unittest.TestCase):
    """--show and --rebuild accept a record path from the repository root,
    also when the current directory is a subdirectory."""

    RECORD = "20261116-113643-infra-5xx-alarm-codex.record"

    def test_root_relative_path_from_a_subdirectory(self):
        import os
        here = os.getcwd()
        sample = str(FIXTURES.resolve().relative_to(baseline.REPO_ROOT)
                     / "baseline-sample-runs" / self.RECORD)
        try:
            os.chdir(FIXTURES)
            self.assertEqual(baseline.resolve_record(sample), baseline.REPO_ROOT / sample)
            self.assertTrue(baseline.resolve_record(
                "baseline-sample-runs/" + self.RECORD).is_dir())
        finally:
            os.chdir(here)

    def test_missing_record_names_both_places(self):
        with self.assertRaises(FileNotFoundError) as caught:
            baseline.resolve_record("no-such.record")
        self.assertIn("Looked in", str(caught.exception))
        self.assertIn(str(baseline.REPO_ROOT), str(caught.exception))
