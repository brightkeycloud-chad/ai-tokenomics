"""Tests for the short views (`--brief`) of the wide toolkit commands, and for
the index value below 0.01. Each brief view must fit on the screen: at most
15 lines and 100 columns. The tests read the committed sample data. Run from
the repository root:

    python3 -m unittest discover -s tools/tests

The tests make no model request and no AWS call.
"""
import contextlib
import copy
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics import baseline, cli  # noqa: E402
from test_costmodel import run_cli  # noqa: E402
from test_sweep import row  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SESSION = FIXTURES / "claude-session" / "78fe89b5-226e-4ee5-9785-a7d8ac85470b.jsonl"
CACHE_SESSION = (FIXTURES / "cache-idle" / "sample-run"
                 / "claude-session" / "24985a27-60d2-42d2-81a3-53f3bcba8b3d.jsonl")
CODEX_HOME = FIXTURES / "codex-home"
CODEX_EVENTS = CODEX_HOME / "exec" / "find-incident-files.jsonl"
BASELINE = FIXTURES / "baseline-sample-runs"
SWEEP = FIXTURES / "model-effort-sweep"
HOOK = FIXTURES / "test-filter-hook"
HOOKED = HOOK / "sample-runs" / "20261116-222739-app-post-incident-claude.record"
LOCAL = FIXTURES / "local-vs-cloud"
BLOATED = FIXTURES / "bloated-context"
WORKED = FIXTURES / "cost-scenarios" / "worked-example.json"


class FitsMixin:
    def assertFits(self, out: str):
        lines = out.rstrip("\n").splitlines()
        self.assertLessEqual(len(lines), 15, out)
        self.assertLessEqual(max(len(line) for line in lines), 100, out)


class SessionBriefTest(FitsMixin, unittest.TestCase):
    def test_two_lanes(self):
        code, out, _ = run_cli("session", str(SESSION), "--brief")
        self.assertEqual(code, 0)
        self.assertIn("Session 78fe89b5 · 2 lanes (main + 1) · all lanes finished", out)
        self.assertIn("Cache write (1 hour)   51.8k  $0.0104    55%", out)
        self.assertIn("Total                 276.5k  $0.0189   100%", out)
        self.assertIn("Main lane: $0.0120 (63%). Other lanes: $0.0069 (37%).", out)
        self.assertNotIn("Ratios", out)
        self.assertFits(out)

    def test_four_lanes(self):
        code, out, _ = run_cli("session", str(CACHE_SESSION), "--brief")
        self.assertEqual(code, 0)
        self.assertIn("4 lanes (main + 3)", out)
        self.assertFits(out)

    def test_with_requests_is_the_requests_view(self):
        code, out, _ = run_cli("session", str(SESSION), "--brief", "--requests")
        self.assertEqual(code, 0)
        self.assertIn("Time  Lane", out)
        self.assertNotIn("Meter", out)
        self.assertFits(out)

    def test_json_wins(self):
        code, out, _ = run_cli("session", str(SESSION), "--brief", "--json")
        self.assertEqual(code, 0)
        self.assertIn("lanes", json.loads(out))


class RequestsBriefTest(FitsMixin, unittest.TestCase):
    """`tokenomics session --requests --brief` on the cache-idle sample."""

    def test_cache_idle_sample(self):
        code, out, _ = run_cli("session", str(CACHE_SESSION), "--requests", "--brief")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "Session 24985a27 · 4 lanes (main + 3) · 5 of 12 "
                         "requests: first of each lane, idle gaps, misses")
        for line in (
                "Time  Lane                         Gap  Cache write  TTL  Cache read  Cache",
                "0:00  main                           -         5.2k   1h        8.4k  hit",
                "0:01  subagent: cache-probe #1       -         5.5k   5m           0  write",
                "0:07  subagent: cache-probe #2    3.4s            0    -        5.5k  hit",
                "6:12  main                      6m 00s          236   1h       15.0k  hit",
                "6:13  subagent: cache-probe #3  6m 03s         5.5k   5m           0  miss",
                "Miss ÷ hit, claude-haiku-5-5: 5-minute write rate ÷ read rate = 1.25× ÷ 0.1× "
                "= 12.5×.",
                "Measured, first request of each lane: subagent: cache-probe #3 (miss, after "
                "6m 03s idle) $0.000693",
                "÷ subagent: cache-probe #2 (hit) $0.000056 = 12.5×."):
            self.assertIn(line + "\n", out)
        # The second request of a call is a hit with a short gap: not shown.
        self.assertNotIn("0:02", out)
        self.assertNotIn("Prompt cost", out)
        self.assertFits(out)

    def test_every_committed_session_fits(self):
        sessions = sorted(FIXTURES.glob("**/*.jsonl"))
        self.assertGreater(len(sessions), 5)
        for path in sessions:
            if "subagents" in path.parts or "codex" in str(path):
                continue
            with self.subTest(session=path.name):
                code, out, _ = run_cli("session", str(path), "--requests", "--brief")
                self.assertEqual(code, 0)
                self.assertFits(out)

    def test_selection_keeps_misses_and_idle_gaps_first(self):
        def r(cache, gap=None, first=False):
            return {"cache": cache, "gap_seconds": gap, "first_in_lane": first}
        rows = ([r("write", first=True)] + [r("hit", 1.0, True) for _ in range(10)]
                + [r("hit", 400.0), r("miss", 2.0), r("hit", 0.5)])
        shown = cli._brief_requests(rows)
        self.assertEqual(len(shown), cli.REQUESTS_BRIEF_ROWS)
        self.assertIn(11, shown)   # the idle gap
        self.assertIn(12, shown)   # the miss
        self.assertNotIn(13, shown)
        self.assertEqual(shown, sorted(shown))

    def test_json_is_not_changed(self):
        code, out, _ = run_cli("session", str(CACHE_SESSION), "--requests", "--brief", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["requests"]), 12)

    def test_full_view_did_not_change(self):
        code, out, _ = run_cli("session", str(CACHE_SESSION), "--requests")
        self.assertIn("6:13  subagent: cache-probe #3  claude-haiku-5-5   6m 03s      2         "
                      "5.5k  5m            0     160     $0.00069  miss", out)
        self.assertIn("Measured, first request of each lane: subagent: cache-probe #3 (miss, "
                      "after 6m 03s idle) $0.000693 ÷ subagent: cache-probe #2 (hit) $0.000056 "
                      "= 12.5×.", out)


class OllamaTaskBriefTest(unittest.TestCase):
    """`tokenomics ollama task --brief` for Claude Code and Codex on the local
    model: at most about 12 lines and 100 columns."""
    CLAUDE = FIXTURES / "claude-local" / "captured" / "task.json"
    CODEX = FIXTURES / "codex-local" / "captured" / "task.json"

    def brief(self, path) -> str:
        code, out, _ = run_cli("ollama", "task", "--from", str(path), "--brief")
        self.assertEqual(code, 0)
        lines = out.rstrip("\n").splitlines()
        self.assertLessEqual(len(lines), 12, out)
        self.assertLessEqual(max(len(line) for line in lines), 100, out)
        return out

    def test_claude_code_shows_every_request(self):
        out = self.brief(self.CLAUDE)
        self.assertEqual(out.splitlines()[0], "Claude Code on qwen3.5:35b · session b236b357 · "
                         "4 requests · no token price: the meter is time")
        self.assertIn("   1 bg         758          0  9.2 s   1.1 s  7.0 s        -  17.3 s", out)
        self.assertIn("   4 bg       1,266     24,115  0.0 s   2.0 s  0.7 s        -   2.7 s", out)
        self.assertIn("  Total      27,010     48,303  9.2 s  42.4 s  9.6 s      115  61.2 s", out)
        self.assertIn("Request 2 read 24,119 new tokens in 37.8 s. After it: median 2.5 s each, "
                      "not 38.9 s (16× faster).", out)

    def test_codex_adds_the_later_requests(self):
        out = self.brief(self.CODEX)
        self.assertIn("      3          21      7,344  0.0 s  0.1 s    1.3 s       85    1.4 s",
                      out)
        self.assertIn("    4-7         936     31,063  0.0 s  1.6 s    6.9 s      457    8.4 s",
                      out)
        self.assertIn("  Total       8,388     38,407  7.6 s  9.3 s  101.9 s      626  118.8 s",
                      out)
        self.assertIn("Request 2 waited 92.8 s in the queue: Ollama runs one request at a time.",
                      out)
        self.assertIn("4-7: the sum of these requests.", out)
        self.assertNotIn("      5  ", out)

    def test_long_task(self):
        saved = json.loads(self.CODEX.read_text())
        later = dict(saved["requests"][3], queued_s=4.0)
        saved["requests"] += [later] * 21
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "task.json"
            path.write_text(json.dumps(saved))
            out = self.brief(path)
        self.assertIn("28 requests", out)
        self.assertIn("   4-28 ", out)
        self.assertIn("22 requests waited in the queue, up to 92.8 s: Ollama runs one request "
                      "at a time.", out)

    def test_full_view_did_not_change(self):
        code, out, _ = run_cli("ollama", "task", "--from", str(self.CLAUDE))
        self.assertIn("Local meters from the Ollama server log. No token price: the meter is "
                      "time.", out)
        self.assertIn("median 2.5 s each, not 38.9 s (16× faster).", out)


class CodexBriefTest(FitsMixin, unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"CODEX_HOME": str(CODEX_HOME)})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_exec_events(self):
        code, out, _ = run_cli("codex", str(CODEX_EVENTS), "--brief")
        self.assertEqual(code, 0)
        self.assertIn("Codex thread 01a1e19a · gpt-6-luna · effort default · 1 turn", out)
        self.assertIn("Input (fresh)   10.3k         $0.0010  0.0259 credits", out)
        self.assertIn("Total           49.1k         $0.0016  0.0394 credits", out)
        self.assertIn("Plan limits used at the end: 5 hours 0.0%, Weekly 0.0%.", out)
        self.assertFits(out)

    def test_interactive_thread(self):
        code, out, _ = run_cli("codex", "01a1e1aa-d24b-7232-99a5-1e15465970eb", "--brief")
        self.assertEqual(code, 0)
        self.assertIn("Total           53.2k         $0.0015  0.0374 credits", out)
        self.assertFits(out)


class LogBriefTest(FitsMixin, unittest.TestCase):
    def test_baseline_log(self):
        code, out, _ = run_cli("log", str(BASELINE / "results-log.csv"), "--brief")
        self.assertEqual(code, 0)
        self.assertIn("Results log results-log.csv · 4 runs", out)
        self.assertIn("app-post-incident  codex   gpt-6.1-sol medium        1    pass    "
                      "111.5k     $0.0800  57.0s", out)
        self.assertFits(out)

    def test_long_log_shows_the_newest_runs(self):
        code, out, _ = run_cli("log", str(SWEEP / "sample-runs" / "results-log.csv"),
                               "--brief")
        self.assertEqual(code, 0)
        self.assertIn(f"The newest {cli.LOG_BRIEF_ROWS} of 87 runs.", out)
        self.assertFits(out)

    def test_one_task_with_variants(self):
        code, out, _ = run_cli("log", str(HOOK / "sample-runs" / "results-log.csv"), "--brief")
        self.assertEqual(code, 0)
        self.assertIn("4 runs · app-post-incident", out)
        self.assertIn("claude  claude-sonnet-5-5 medium test-output-hook  1    pass", out)
        self.assertFits(out)

    def test_local_runs_and_failures(self):
        code, out, _ = run_cli("log", str(LOCAL / "sample-runs" / "results-log.csv"),
                               "--brief", "--task", "infra-5xx-alarm")
        self.assertEqual(code, 0)
        self.assertIn("fail", out)
        self.assertFits(out)

    def test_old_configuration_has_a_star(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            baseline.append_row(log, row(rep=1))
            baseline.append_row(log, row(rep=2, config="0ldc0nf1g000", ok=False))
            code, out, _ = run_cli("log", str(log), "--brief")
        self.assertEqual(code, 0)
        self.assertIn("2 *  fail", out)
        self.assertIn("* Another configuration than the checked-in one.", out)
        self.assertFits(out)


class ToolResultsBriefTest(FitsMixin, unittest.TestCase):
    def test_one_run(self):
        code, out, _ = run_cli("tool-results", str(HOOKED), "--brief")
        self.assertEqual(code, 0)
        self.assertIn("Tool results · run 20261116-222739 · variant test-output-hook", out)
        self.assertIn("        1        0        195 B           74         6.9k", out)
        self.assertNotIn("Read", out)
        self.assertFits(out)

    def test_run_without_hook_logs(self):
        record = BASELINE / "20261116-221840-app-post-incident-claude.record"
        code, out, _ = run_cli("tool-results", str(record), "--brief")
        self.assertEqual(code, 0)
        self.assertFits(out)

    def test_sweep(self):
        code, out, _ = run_cli("tool-results", "--sweep", str(HOOK / "sweep.json"),
                               "--log", str(HOOK / "sample-runs" / "results-log.csv"),
                               "--runs-dir", str(HOOK / "sample-runs"), "--brief")
        self.assertEqual(code, 0)
        self.assertIn("No hook (baseline)  20261116-222717  yes           1        0"
                      "        12.3k        18.0k", out)
        self.assertIn("Hook                20261116-222739  yes           1        0"
                      "           74         6.9k", out)
        self.assertNotIn("Medians", out)
        self.assertFits(out)

    def test_short_run_id(self):
        self.assertEqual(cli._short_run_id("20261116-222739-app-post-incident-claude"),
                         "20261116-222739")
        self.assertEqual(cli._short_run_id("cache-idle-headless"), "cache-idle-headless")


def run_baseline(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = baseline.main(list(argv))
    return code, out.getvalue()


class RunBaselineBriefTest(FitsMixin, unittest.TestCase):
    def test_show(self):
        for record in sorted(BASELINE.glob("*.record")) + [HOOKED]:
            code, out = run_baseline("--show", str(record), "--brief")
            self.assertEqual(code, 0)
            self.assertIn("6 of 6 checks ok", out)
            self.assertNotIn("  ok ", out)
            self.assertFits(out)
        self.assertIn("Run 20261116-222739-app-post-incident-claude · repetition 1 · "
                      "variant test-output-hook", out)
        self.assertIn("Cost: $0.1147 at list price", out)

    def test_failed_check_gets_a_line(self):
        record = json.loads((HOOKED / "run.json").read_text(encoding="utf-8"))
        failed = copy.deepcopy(record)
        failed["checks"]["rules"][0].update(ok=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baseline.print_result_brief(failed)
        self.assertIn("5 of 6 checks ok", out.getvalue())
        self.assertIn("  FAIL rules      ", out.getvalue())
        self.assertFits(out.getvalue())


class IndexBelowOneHundredthTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(cli._fmt_index(None), "-")
        self.assertEqual(cli._fmt_index(0.0), "0×")
        self.assertEqual(cli._fmt_index(0.0024), "<0.01×")
        self.assertEqual(cli._fmt_index(0.01), "0.01×")
        self.assertEqual(cli._fmt_index(1.234), "1.23×")

    def test_sweep_sample(self):
        code, out, _ = run_cli("index", str(SWEEP / "sample-runs" / "results-log.csv"),
                               "--sweep", str(SWEEP / "sweep.json"),
                               "--task", "infra-5xx-alarm")
        self.assertEqual(code, 0)
        self.assertIn("$0.0024      <0.01×  28.8s", out)
        self.assertNotIn("0.00×", out)

    def test_brief_has_no_zero_index(self):
        code, out, _ = run_cli("index", str(LOCAL / "sample-runs" / "results-log.csv"),
                               "--sweep", str(LOCAL / "sweep.json"),
                               "--task", "app-post-incident", "--brief")
        self.assertEqual(code, 0)
        self.assertNotIn("0.00×", out)


def index_brief(sample: pathlib.Path, *extra):
    code, out, _ = run_cli("index", str(sample / "sample-runs" / "results-log.csv"),
                           "--sweep", str(sample / "sweep.json"), *extra)
    return code, out


class IndexCompactBriefTest(FitsMixin, unittest.TestCase):
    """`index --brief` for a sweep with no local setups (the model sweep, the
    bloated context, and the test-filter hook)."""

    def test_app_task_of_the_model_sweep(self):
        code, out = index_brief(SWEEP, "--task", "app-post-incident", "--brief")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "app-post-incident · rework 10 min at $90/hour · "
                                   "18 claude runs of another configuration not counted")
        self.assertEqual(lines[1], "Tool    Model              Effort  Passes  Cost/success  "
                                   "Cost index  Time index  Verdict")
        self.assertIn("claude  claude-haiku-5-5   medium  3 of 3       $0.0097       0.10×"
                      "       1.53×  Cheaper, slower", out)
        self.assertIn("  codex baseline, 5 passing runs: spread of cost ±22%, of time ±7%", out)
        self.assertNotIn("Index to the baseline", out)
        self.assertNotIn("Variant", out)
        self.assertEqual(len(lines), 15)
        self.assertFits(out)

    def test_infra_task_has_two_header_lines(self):
        code, out = index_brief(SWEEP, "--task", "infra-5xx-alarm", "--brief")
        self.assertEqual(code, 0)
        self.assertIn("codex   gpt-6.1-sol        medium  4 of 5  $11.2950   1.00×  1.00×  "
                      "baseline", out)
        self.assertIn("codex   gpt-6-luna         medium  3 of 3   $0.0024  <0.01×  0.89×  "
                      "Cheaper, faster, more reliable", out)
        self.assertIn("  claude baseline, 3 passing runs: spread of cost ±7%, of time ±9%", out)
        self.assertFits(out)

    def test_variants_and_cache(self):
        code, out = index_brief(BLOATED, "--brief")
        self.assertEqual(code, 0)
        self.assertIn("claude  claude-sonnet-5-5  medium  bloated-agents-md  3 of 3   $0.1459"
                      "  1.44×  0.98×  Costlier", out)
        self.assertFits(out)
        code, out = index_brief(BLOATED, "--brief", "--cache")
        self.assertEqual(code, 0)
        # Tool, model, and effort are the same in each row: they go on the task line.
        self.assertIn("app-post-incident · claude · claude-sonnet-5-5 · medium · rework 10 min "
                      "at $90/hour", out)
        self.assertIn("bloated-agents-md  3 of 3        27.6k      104.1k       $0.1459       "
                      "1.44×       0.98×  Costlier", out)
        self.assertFits(out)

    def test_star_and_cache_of_the_hook_sweep(self):
        code, out = index_brief(HOOK, "--brief", "--cache")
        self.assertEqual(code, 0)
        self.assertIn("test-output-hook  2 of 2        17.3k       97.9k       $0.1073       "
                      "0.68×       0.97×  Cheaper *", out)
        self.assertIn("* Fewer than 3 runs: an anecdote, not a finding.", out)
        self.assertFits(out)

    def test_cache_on_the_model_sweep_stays_under_100_columns(self):
        for task in ("app-post-incident", "infra-5xx-alarm"):
            code, out = index_brief(SWEEP, "--task", task, "--brief", "--cache")
            self.assertEqual(code, 0)
            self.assertLessEqual(max(len(line) for line in out.splitlines()), 100, out)
            self.assertIn("Cache", out)

    def test_local_setups_keep_the_local_vs_cloud_view(self):
        code, out = index_brief(LOCAL, "--task", "app-post-incident", "--brief")
        self.assertEqual(code, 0)
        self.assertIn("Per success, attended", out)
        self.assertIn("Claude Code, local  3 of 3  $0 (local)    3m 30s", out)
        self.assertNotIn("Cost index", out)

    def test_normal_view_did_not_change(self):
        code, out = index_brief(SWEEP, "--task", "app-post-incident")
        self.assertEqual(code, 0)
        self.assertIn("Index to the baseline · ", out)
        self.assertIn("claude  claude-haiku-5-5   medium     3  3 of 3         $0.0097       "
                      "$0.0097       0.10×   29.4s       1.53×  Cheaper, slower", out)
        self.assertIn("  claude: 18 runs with another configuration not counted.", out)
        code, out = index_brief(BLOATED, "--cache")
        self.assertIn("Tool    Model              Effort  Variant            Runs  Passes  "
                      "Cache write  Cache read  Tokens/attempt", out)


class CompareBriefTest(FitsMixin, unittest.TestCase):
    def test_worked_example(self):
        code, out, _ = run_cli("compare", str(WORKED), "--brief")
        self.assertEqual(code, 0)
        self.assertIn("Option   Success  Cost/success  vs best", out)
        self.assertIn("Model A      50%        $15.80  5.69×", out)
        self.assertIn("Model B      90%         $2.78  1.00×  best", out)
        self.assertIn("Cheapest per attempt: Model A. Cheapest per success: Model B.", out)
        self.assertIn("Model A costs 40% of Model B per attempt, and 5.7× per successful task.",
                      out)
        self.assertNotIn("Rework", out)
        self.assertFits(out)

    def test_measured_scenario_has_four_decimals(self):
        code, out, _ = run_cli("compare", str(SWEEP / "scenarios"
                                              / "measured-app-post-incident.json"), "--brief")
        self.assertEqual(code, 0)
        self.assertIn("gpt-6-luna · medium                       100%       $0.0030  1.00×  best",
                      out)
        self.assertFits(out)

    def test_normal_view_did_not_change(self):
        code, out, _ = run_cli("compare", str(WORKED))
        self.assertEqual(code, 0)
        self.assertIn("Model A           $0.40      50%           $0.80              1.00  "
                      "$15.00    $0.00        $15.80  5.69×", out)
        self.assertIn("Hourly rate $90.00 · currency USD", out)


class DryRunBriefTest(FitsMixin, unittest.TestCase):
    def dry_run(self, *argv):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_baseline(*argv, "--dry-run", "--runs-dir", tmp,
                                     "--log", str(pathlib.Path(tmp) / "log.csv"))
            self.assertEqual(os.listdir(tmp), [])
        self.assertEqual(code, 0)
        return out

    def test_one_run_shows_the_pinned_items(self):
        for tool, model in (("claude", "claude-sonnet-5-5"), ("codex", "gpt-6.1-sol")):
            argv = ("app-post-incident", "--tool", tool, "--model", model, "--effort", "medium")
            brief = self.dry_run(*argv, "--brief")
            full = self.dry_run(*argv)
            self.assertFits(brief)
            names = [line.split()[0] for line in brief.splitlines()
                     if line.startswith("  ") and not line.startswith("   ")]
            self.assertEqual(names, ["Workload", "Prompt", "Agent", "Config"])
            self.assertTrue(brief.endswith("Dry run: no copy and no model request.\n"))
            # The wrapped command is the same command as in the full plan.
            agent = [line for line in full.splitlines() if line.startswith("  Agent")][0]
            lines = brief.splitlines()
            start = next(i for i, line in enumerate(lines) if line.startswith("  Agent"))
            end = next(i for i, line in enumerate(lines) if line.startswith("  Config"))
            self.assertEqual(" ".join(line.strip() for line in lines[start:end]),
                             agent.strip())
            for line in lines[start + 1:end]:
                self.assertTrue(line.startswith(" " * 14), line)

    def test_one_run_with_a_variant(self):
        brief = self.dry_run("app-post-incident", "--tool", "claude", "--model",
                             "claude-sonnet-5-5", "--effort", "medium", "--variant",
                             str(BLOATED / "variants" / "bloated-agents-md"), "--brief")
        self.assertIn("  Variant     bloated-agents-md (sha256 ", brief)
        self.assertIn("              AGENTS.md: 1,511 lines, 28.7 KB", brief)
        self.assertFits(brief)

    def test_wrap_keeps_an_option_with_its_value(self):
        lines = baseline.wrap_command(["codex", "-c", "x" * 30, "-c", "y" * 30, "--json"],
                                      "  Agent       ", 14, width=60)
        self.assertEqual(lines, ["  Agent       codex -c " + "x" * 30,
                                 " " * 14 + "-c " + "y" * 30 + " --json"])

    def test_sweep_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_baseline("infra-5xx-alarm", "--sweep", str(SWEEP / "sweep.json"),
                                     "--dry-run", "--brief", "--runs-dir", tmp, "--log",
                                     str(SWEEP / "sample-runs" / "results-log.csv"))
        self.assertEqual(code, 0)
        self.assertNotIn("Sweep ", out)
        self.assertNotIn("Target:", out)
        self.assertIn("infra-5xx-alarm  codex   gpt-6.1-sol        medium  Workhorse (baseline)"
                      "         5       0", out)
        self.assertIn("Runs to make: 12 (claude 12, codex 0).", out)
        self.assertEqual(out.splitlines()[0].split(), ["Task", "Tool", "Model", "Effort",
                                                       "Setup", "In", "log", "To", "run"])
        self.assertFits(out)
        full = self.dry_run("infra-5xx-alarm", "--sweep", str(SWEEP / "sweep.json"))
        self.assertIn("Target: 3 runs of each task and setup · log ", full)


if __name__ == "__main__":
    unittest.main()
