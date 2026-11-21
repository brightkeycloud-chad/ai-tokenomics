"""Tests for Claude Code on the local model. The launcher variables,
the local configuration of the harness, the local meters from the Ollama
server log, and the status line for a local model. Run from the repository
root:

    python3 -m unittest discover -s tools/tests

The tests make no model request and start no Claude Code process.
"""
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "statusline"))

import statusline  # noqa: E402
from tokenomics import baseline, cli, local, ollama, runconfig  # noqa: E402
from tokenomics.rates import load_profiles  # noqa: E402

REPO = TOOLS.parent
SETTINGS = {"model": "qwen3.5:35b", "think": True, "context_length": 65536,
            "fallback": {"model": "gpt-oss:20b", "think": "medium"}}

# Server log lines of the MLX runner (Ollama 0.40.16), shortened: a load,
# a cold request with progress lines, and a request that reads from cache.
LOG = """\
time=2026-11-17T14:00:00.000-08:00 level=INFO source=client.go:508 \
msg="starting mlx runner subprocess" model=qwen3.5:35b port=59810
time=2026-11-17T14:00:09.500-08:00 level=INFO source=client.go:112 msg="mlx runner is ready" \
port=59810
time=2026-11-17T14:00:09.510-08:00 level=INFO source=prefix_cache.go:151 msg="cache miss" \
total=4000 matched=1 cached=0 left=4000
time=2026-11-17T14:00:11.000-08:00 level=INFO source=pipeline.go:228 \
msg="Prompt processing progress" processed=2048 total=4000
time=2026-11-17T14:00:13.500-08:00 level=INFO source=pipeline.go:228 \
msg="Prompt processing progress" processed=3999 total=4000
[GIN] 2026/11/17 - 14:00:15 | 200 |    15.5s |       127.0.0.1 | POST     "/v1/messages?beta=true"
time=2026-11-17T14:00:15.500-08:00 level=INFO source=server.go:270 msg=ServeHTTP method=POST \
path=/v1/completions took=6s status="200 OK"
time=2026-11-17T14:00:15.600-08:00 level=INFO source=prefix_cache.go:151 msg="cache hit" \
total=4300 matched=4100 cached=4100 left=200
time=2026-11-17T14:00:15.900-08:00 level=INFO source=pipeline.go:228 \
msg="Prompt processing progress" processed=199 total=200
time=2026-11-17T14:00:17.100-08:00 level=INFO source=server.go:270 msg=ServeHTTP method=POST \
path=/v1/completions took=1.5s status="200 OK"
time=2026-11-17T14:00:17.200-08:00 level=INFO source=prefix_cache.go:151 msg="cache hit" \
total=4500 matched=4300 cached=4300 left=200
time=2026-11-17T14:00:17.500-08:00 level=INFO source=pipeline.go:228 \
msg="Prompt processing progress" processed=199 total=200
time=2026-11-17T14:00:18.200-08:00 level=INFO source=server.go:270 msg=ServeHTTP method=POST \
path=/v1/completions took=1000ms status="200 OK"
"""


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.claude = local.load_claude_settings()

    def test_variables_come_from_the_settings(self):
        values, remove = local.claude_vars(None, SETTINGS, self.claude)
        self.assertEqual(values["ANTHROPIC_BASE_URL"], "http://localhost:11434")
        self.assertEqual(values["ANTHROPIC_AUTH_TOKEN"], "ollama")
        self.assertEqual(values["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "65536")
        for alias in ("FABLE", "OPUS", "SONNET", "HAIKU"):
            self.assertEqual(values[f"ANTHROPIC_DEFAULT_{alias}_MODEL"], "qwen3.5:35b")
        self.assertEqual(values["CLAUDE_CODE_SUBAGENT_MODEL"], "qwen3.5:35b")
        self.assertIn("ANTHROPIC_API_KEY", remove)
        self.assertIn("OPENAI_API_KEY", remove)
        values, _ = local.claude_vars("gpt-oss:20b", SETTINGS, self.claude)
        self.assertEqual(values["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "gpt-oss:20b")

    def test_env_is_for_one_process_only(self):
        base = {"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k",
                "CLAUDE_CODE_USE_BEDROCK": "1", "HOME": "/home/me"}
        env = local.claude_env(None, base, SETTINGS, self.claude)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://localhost:11434")
        # The base environment does not change.
        self.assertEqual(base["ANTHROPIC_API_KEY"], "k")
        self.assertNotIn("ANTHROPIC_BASE_URL", base)

    def test_local_models_and_urls(self):
        for model in ("qwen3.5:35b", "gpt-oss:20b", "llama3.3:70b"):
            self.assertTrue(local.is_local_model(model, SETTINGS), model)
        for model in ("claude-sonnet-5-5", "gpt-6.1-sol", "", None,
                      "us.anthropic.claude-opus-4-8-v1:0"):
            self.assertFalse(local.is_local_model(model, SETTINGS), model)
        self.assertTrue(local.is_local_url("http://localhost:11434"))
        self.assertTrue(local.is_local_url("http://127.0.0.1:11434/"))
        self.assertFalse(local.is_local_url("https://api.anthropic.com"))
        self.assertFalse(local.is_local_url(None))

    def test_arguments(self):
        self.assertEqual(local.parse_args([], SETTINGS), ("qwen3.5:35b", False, []))
        self.assertEqual(local.parse_args(["--fallback", "-c"], SETTINGS),
                         ("gpt-oss:20b", False, ["-c"]))
        self.assertEqual(local.parse_args(["--model", "x:1", "--show"], SETTINGS),
                         ("x:1", True, []))
        self.assertEqual(local.parse_args(["--model=x:2", "-p", "hi"], SETTINGS),
                         ("x:2", False, ["-p", "hi"]))
        with self.assertRaises(ValueError):
            local.parse_args(["--model"], SETTINGS)

    def test_show_starts_nothing(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("os.execve") as execve:
            self.assertEqual(local.main(["--show"]), 0)
        execve.assert_not_called()
        text = out.getvalue()
        self.assertIn("Backend   Ollama at http://localhost:11434", text)
        self.assertIn("Starts    claude --model qwen3.5:35b", text)
        self.assertIn("no shell profile, no settings file", text)
        self.assertLessEqual(len(text.splitlines()), 15)
        self.assertLessEqual(max(len(line) for line in text.splitlines()), 100)

    def test_launch_sets_the_variables_and_keeps_the_arguments(self):
        with mock.patch.object(local, "check_server"), \
                mock.patch("shutil.which", return_value="/bin/claude"), \
                mock.patch("os.execve") as execve, \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            local.main(["--fallback", "--resume"])
        exe, argv, env = execve.call_args.args
        self.assertEqual(argv, ["claude", "--model", "gpt-oss:20b", "--resume"])
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "ollama")
        # This process keeps its own environment.
        self.assertNotIn("ANTHROPIC_BASE_URL", os.environ)


class ConfigTest(unittest.TestCase):
    def test_local_runs_have_their_own_configuration(self):
        cloud = runconfig.current_config("claude", None, "claude-sonnet-5-5")
        self.assertEqual(cloud, runconfig.current_config("claude"))
        mine = runconfig.current_config("claude", None, "qwen3.5:35b")
        self.assertRegex(mine, r"^[0-9a-f]{12}$")
        self.assertNotEqual(mine, cloud)
        self.assertEqual(mine, local.config_hash(runconfig.config_file("claude")))
        # The fallback model uses the same files, so the same configuration.
        self.assertEqual(mine, runconfig.current_config("claude", None, "gpt-oss:20b"))
        # A Codex cloud run is not changed. Codex on the local model has its
        # own configuration (see test_codexlocal.py).
        self.assertEqual(runconfig.current_config("codex", None, "gpt-6.1-sol"),
                         runconfig.current_config("codex"))
        self.assertNotEqual(runconfig.current_config("codex", None, "qwen3.5:35b"), mine)

    def test_any_file_changes_the_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for name in ("settings.json", "claude-code.json", "ollama.json"):
                path = pathlib.Path(tmp) / name
                path.write_text("{}")
                files.append(path)
            first = local.config_hash(*files)
            files[2].write_text('{"model": "other"}')
            self.assertNotEqual(first, local.config_hash(*files))

    def test_repetitions_count_one_configuration(self):
        mine = runconfig.current_config("claude", "app-post-incident", "qwen3.5:35b")
        row = {"task": "app-post-incident", "tool": "claude", "model": "qwen3.5:35b",
               "effort": "medium", "variant": ""}
        rows = [dict(row, config=mine), dict(row, config="0af40a62a105")]
        self.assertEqual(baseline.next_rep(rows, "app-post-incident", "claude",
                                           "qwen3.5:35b", "medium"), 2)

    def test_agent_env(self):
        env = {"PATH": "/bin", "ANTHROPIC_BASE_URL": "http://elsewhere",
               "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "m"}
        with mock.patch.dict(os.environ, env, clear=True):
            cloud = baseline.agent_env(pathlib.Path("/cache"), None, "claude-sonnet-5-5")
            mine = baseline.agent_env(pathlib.Path("/cache"), None, "qwen3.5:35b")
            codex = baseline.agent_env(pathlib.Path("/cache"))
        # A cloud run never goes to another server.
        for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            self.assertNotIn(name, cloud)
            self.assertNotIn(name, codex)
        self.assertEqual(mine["ANTHROPIC_BASE_URL"], "http://localhost:11434")
        self.assertEqual(mine["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "65536")
        self.assertEqual(mine["TF_PLUGIN_CACHE_DIR"], "/cache")

    def test_dry_run_shows_the_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                baseline.main(["app-post-incident", "--tool", "claude", "--model",
                               "qwen3.5:35b", "--effort", "medium", "--dry-run",
                               "--log", f"{tmp}/log.csv", "--runs-dir", tmp])
        text = out.getvalue()
        self.assertIn("  Backend     Ollama at http://localhost:11434", text)
        self.assertIn(runconfig.current_config("claude", None, "qwen3.5:35b"), text)

    def test_row_of_a_local_run(self):
        record = {
            "meta": {"run_id": "r1", "date": "2026-11-17", "rep": 1,
                     "task": "app-post-incident", "tool": "claude", "model": "qwen3.5:35b",
                     "effort": "medium", "workload": "status-board-app",
                     "workload_tree": "abc", "prompt_sha": "def", "config": "72573c95f82d"},
            "agent": {"exit_code": 0, "timed_out": False, "wall_seconds": 300.0},
            "checks": {"acceptance": [{"rule": "pytest", "ok": True, "failed": []}],
                       "rules": [{"rule": "1 validate", "ok": True, "failed": []}]},
        }
        usage = baseline.empty_usage() | {
            "models": ["qwen3.5:35b"], "input": 30000, "cache_write": 0, "cache_read": 90000,
            "output": 4000, "cost_list_usd": 0.0, "vendor_estimate_usd": 0.4,
            "unpriced_models": ["qwen3.5:35b"], "lanes": 1}
        row = baseline.build_row(record, usage)
        self.assertEqual(row["vendor_estimate_usd"], "")
        self.assertEqual(row["cost_list_usd"], 0)
        self.assertEqual(row["notes"], "local model on Ollama: no token price")


class LogMetersTest(unittest.TestCase):
    def test_go_durations(self):
        self.assertAlmostEqual(ollama.go_duration("19.039902416s"), 19.039902416)
        self.assertAlmostEqual(ollama.go_duration("395.95275ms"), 0.39595275)
        self.assertAlmostEqual(ollama.go_duration("69.25µs"), 0.00006925)
        self.assertAlmostEqual(ollama.go_duration("1m2.5s"), 62.5)
        with self.assertRaises(ValueError):
            ollama.go_duration("soon")

    def test_requests_from_the_log(self):
        first, second, third = ollama.log_requests(LOG)
        self.assertAlmostEqual(first["load_s"], 9.5)
        self.assertEqual((first["new_tokens"], first["cached_tokens"]), (4000, 0))
        self.assertAlmostEqual(first["read_s"], 4.0)    # 13.5 - (15.5 - 6)
        self.assertAlmostEqual(first["write_s"], 2.0)   # 15.5 - 13.5
        self.assertAlmostEqual(first["total_s"], 15.5)  # load + took
        self.assertEqual((second["new_tokens"], second["cached_tokens"]), (200, 4100))
        self.assertAlmostEqual(second["load_s"], 0.0)
        self.assertAlmostEqual(second["read_s"], 0.3, places=3)
        self.assertAlmostEqual(second["write_s"], 1.2, places=3)
        self.assertAlmostEqual(third["total_s"], 1.0)

    def test_a_request_with_no_progress_line_is_all_writing(self):
        text = "\n".join(line for line in LOG.splitlines() if "progress" not in line)
        first = ollama.log_requests(text)[0]
        self.assertAlmostEqual(first["read_s"], 0.0)
        self.assertAlmostEqual(first["write_s"], 6.0)

    def test_window_match_and_meters(self):
        requests = ollama.log_requests(LOG)
        start = requests[0]["end"] - 1
        self.assertEqual(len(ollama.requests_between(requests, start, start + 2, margin=0)), 1)
        # The second request is a background request: not in the transcript.
        written = ollama.match_transcript(requests, [(4000, 0, 300), (200, 4300, 50)])
        self.assertEqual(written, [300, None, 50])
        meters = ollama.task_meters(requests, written, "qwen3.5:35b", "abcd1234", 2)
        self.assertEqual(meters["cold_request"], 1)
        self.assertAlmostEqual(meters["cold_s"], 15.5)
        self.assertAlmostEqual(meters["later_median_s"], 1.25)
        self.assertEqual(meters["background"], 1)
        self.assertEqual(meters["total"]["new_tokens"], 4400)
        self.assertEqual(meters["total"]["written_tokens"], 350)
        self.assertTrue(meters["requests"][1]["background"])

    def test_session_meters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "abcd1234-0000.jsonl"
            records = [
                {"type": "user", "timestamp": "2026-11-17T22:00:00.000Z",
                 "message": {"role": "user", "content": "List the routes."}},
                {"type": "assistant", "timestamp": "2026-11-17T22:00:15.500Z",
                 "message": {"id": "m1", "model": "qwen3.5:35b", "role": "assistant",
                             "content": [{"type": "text", "text": "x"}],
                             "usage": {"input_tokens": 4000, "cache_read_input_tokens": 0,
                                       "output_tokens": 300}}},
                {"type": "assistant", "timestamp": "2026-11-17T22:00:18.200Z",
                 "message": {"id": "m2", "model": "qwen3.5:35b", "role": "assistant",
                             "content": [{"type": "text", "text": "y"}],
                             "usage": {"input_tokens": 200, "cache_read_input_tokens": 4300,
                                       "output_tokens": 50}}},
            ]
            path.write_text("".join(json.dumps(r) + "\n" for r in records))
            meters = local.session_meters(path, LOG)
            self.assertEqual(meters["model"], "qwen3.5:35b")
            self.assertEqual(meters["session"], "abcd1234")
            self.assertEqual(len(meters["requests"]), 3)
            self.assertEqual(meters["background"], 1)
            with self.assertRaises(ValueError):
                local.session_meters(path, "")

    def test_task_view(self):
        requests = ollama.log_requests(LOG)
        meters = ollama.task_meters(requests, [300, None, 50], "qwen3.5:35b", "abcd1234", 2)
        with tempfile.TemporaryDirectory() as tmp:
            saved = pathlib.Path(tmp) / "task.json"
            saved.write_text(json.dumps(meters))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["ollama", "task", "--from", str(saved)]), 0)
        text = out.getvalue()
        self.assertIn("Claude Code on qwen3.5:35b · session abcd1234 · 3 requests", text)
        self.assertIn("2 bg", text)
        self.assertIn("No token price: the meter is time.", text)
        self.assertIn("(12× faster)", text)
        self.assertLessEqual(len(text.splitlines()), 15)
        self.assertLessEqual(max(len(line) for line in text.splitlines()), 100)


class StatusLineTest(unittest.TestCase):
    def data(self, model="qwen3.5:35b"):
        return {"model": {"id": model, "display_name": model}, "effort": {"level": "xhigh"},
                "cost": {"total_cost_usd": 0.1725755, "total_duration_ms": 103686,
                         "total_api_duration_ms": 47121},
                "context_window": {"total_input_tokens": 25172, "context_window_size": 65536,
                                   "used_percentage": 38,
                                   "current_usage": {"input_tokens": 867, "output_tokens": 60,
                                                     "cache_creation_input_tokens": 0,
                                                     "cache_read_input_tokens": 24305}}}

    def test_local_model_shows_no_dollar_figure(self):
        v = statusline.values(self.data(), load_profiles(), env={})
        self.assertTrue(v["local"])
        self.assertIsNone(v["session_estimate_usd"])
        lines = statusline.render(v, color=False)
        self.assertIn("qwen3.5:35b xhigh · local", lines[0])
        self.assertIn("context ▓▓▓░░░░░░░ 38% · 25.2k of 65.5k", lines[0])
        self.assertEqual(lines[2], "cost │ local model: no token price, the meter is time")
        self.assertEqual(lines[3], "Claude Code $ estimate not shown (unknown model)")
        self.assertNotIn("$0", "\n".join(lines))
        self.assertLessEqual(max(len(line) for line in lines), statusline.WIDTH)

    def test_local_base_url(self):
        v = statusline.values(self.data("some-model"), load_profiles(),
                              env={"ANTHROPIC_BASE_URL": "http://localhost:11434"})
        self.assertTrue(v["local"])
        v = statusline.values(self.data("claude-sonnet-5-5"), load_profiles(), env={})
        self.assertFalse(v["local"])
        self.assertAlmostEqual(v["session_estimate_usd"], 0.1725755)


class LauncherFilesTest(unittest.TestCase):
    def test_launcher_and_inputs_exist(self):
        self.assertTrue(os.access(REPO / "bin" / "claude-local", os.X_OK))
        self.assertTrue((REPO / "config" / "local" / "claude-code.json").is_file())


if __name__ == "__main__":
    unittest.main()
