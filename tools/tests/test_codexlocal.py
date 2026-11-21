"""Tests for Codex on the local model. The launcher settings, the
separate Codex home, the local configuration of the harness, a request that
waits in the Ollama queue, and the local meters of a Codex thread. Run from
the repository root:

    python3 -m unittest discover -s tools/tests

The tests make no model request and start no Codex process.
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

from tokenomics import baseline, cli, codexlocal, ollama, runconfig  # noqa: E402

REPO = TOOLS.parent
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CODEX_LOCAL = FIXTURES / "codex-local"
SETTINGS = {"model": "qwen3.5:35b", "think": True, "context_length": 65536,
            "fallback": {"model": "gpt-oss:20b", "think": "medium"}}

# Server log lines of the MLX runner (Ollama 0.40.16), shortened. Codex sends a
# background request and the main request at the same time. The main request
# waits in the queue: its `took` includes the wait, and its cache line comes
# when the background request ends. Then one request reads from cache.
LOG = """\
time=2026-11-17T15:00:00.000-08:00 level=INFO source=client.go:508 \
msg="starting mlx runner subprocess" model=qwen3.5:35b port=62159
time=2026-11-17T15:00:08.000-08:00 level=INFO source=client.go:112 msg="mlx runner is ready" \
port=62159
time=2026-11-17T15:00:08.010-08:00 level=INFO source=prefix_cache.go:151 msg="cache miss" \
total=172 matched=0 cached=0 left=172
time=2026-11-17T15:00:58.000-08:00 level=INFO source=server.go:270 msg=ServeHTTP method=POST \
path=/v1/completions took=50s status="200 OK"
time=2026-11-17T15:00:58.010-08:00 level=INFO source=prefix_cache.go:151 msg="cache miss" \
total=7000 matched=1 cached=0 left=7000
time=2026-11-17T15:01:05.000-08:00 level=INFO source=pipeline.go:228 \
msg="Prompt processing progress" processed=6999 total=7000
[GIN] 2026/11/17 - 15:01:06 | 200 |     58s |       127.0.0.1 | POST     "/v1/responses"
time=2026-11-17T15:01:06.000-08:00 level=INFO source=server.go:270 msg=ServeHTTP method=POST \
path=/v1/completions took=58s status="200 OK"
time=2026-11-17T15:01:06.100-08:00 level=INFO source=prefix_cache.go:151 msg="cache hit" \
total=7100 matched=7000 cached=7000 left=100
time=2026-11-17T15:01:06.300-08:00 level=INFO source=pipeline.go:228 \
msg="Prompt processing progress" processed=99 total=100
time=2026-11-17T15:01:08.100-08:00 level=INFO source=server.go:270 msg=ServeHTTP method=POST \
path=/v1/completions took=2s status="200 OK"
"""


def session_file(folder: pathlib.Path) -> pathlib.Path:
    """A Codex session file with two requests. Codex counts the cached tokens
    inside input_tokens."""
    def usage(inp, cached, out):
        return {"input_tokens": inp, "cached_input_tokens": cached,
                "cache_write_input_tokens": 0, "output_tokens": out,
                "reasoning_output_tokens": 0, "total_tokens": inp + out}
    tid = "01a1ebd6-3cd4-79c2-97e4-5daf926f2af9"
    records = [
        {"timestamp": "2026-11-17T23:00:00.000Z", "type": "session_meta",
         "payload": {"id": tid, "cli_version": "0.169.0", "originator": "codex-tui",
                     "cwd": "/work/app", "model_provider": "ollama-local"}},
        {"timestamp": "2026-11-17T23:00:07.900Z", "type": "event_msg",
         "payload": {"type": "task_started", "model_context_window": 62259}},
        {"timestamp": "2026-11-17T23:00:07.900Z", "type": "turn_context",
         "payload": {"model": "qwen3.5:35b", "effort": "high"}},
        {"timestamp": "2026-11-17T23:01:06.010Z", "type": "token_usage_record",
         "payload": {"usage": usage(7000, 0, 80)}},
        {"timestamp": "2026-11-17T23:01:06.050Z", "type": "response_item",
         "payload": {"type": "function_call_output"}},
        {"timestamp": "2026-11-17T23:01:08.110Z", "type": "token_usage_record",
         "payload": {"usage": usage(7100, 7000, 60)}},
        {"timestamp": "2026-11-17T23:01:08.120Z", "type": "event_msg",
         "payload": {"type": "task_complete"}},
    ]
    path = folder / f"rollout-2026-11-17T15-00-00-{tid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.codex = codexlocal.load_codex_settings()

    def test_settings_come_from_the_files(self):
        values = codexlocal.codex_settings(SETTINGS, self.codex, "/repo/models.json")
        self.assertEqual(values["model_provider"], "ollama-local")
        self.assertEqual(values["model_providers.ollama-local.base_url"],
                         "http://localhost:11434/v1/")
        self.assertEqual(values["model_providers.ollama-local.wire_api"], "responses")
        self.assertEqual(values["model_catalog_json"], "/repo/models.json")
        self.assertEqual(values["model_context_window"], 65536)
        self.assertEqual(values["web_search"], "disabled")
        args = codexlocal.codex_args(SETTINGS, self.codex, "/repo/models.json")
        self.assertIn("model_context_window=65536", args)
        self.assertIn('web_search="disabled"', args)
        self.assertEqual(args[0::2], ["-c"] * (len(args) // 2))

    def test_catalog_matches_the_ollama_settings(self):
        settings = ollama.load_settings()
        catalog = json.loads(codexlocal.CATALOG_FILE.read_text())["models"]
        slugs = [m["slug"] for m in catalog]
        self.assertIn(settings["model"], slugs)
        self.assertIn(settings["fallback"]["model"], slugs)
        for model in catalog:
            self.assertEqual(model["context_window"], settings["context_length"])

    def test_thinking_gives_the_effort(self):
        self.assertEqual(codexlocal.think_effort(True), "high")
        self.assertEqual(codexlocal.think_effort(False), "none")
        self.assertEqual(codexlocal.think_effort("medium"), "medium")
        self.assertEqual(codexlocal.model_effort("qwen3.5:35b", SETTINGS), "high")
        self.assertEqual(codexlocal.model_effort("gpt-oss:20b", SETTINGS), "medium")
        self.assertIsNone(codexlocal.model_effort("other:1b", SETTINGS))

    def test_env_is_for_one_process_only(self):
        base = {"PATH": "/bin", "OPENAI_API_KEY": "sk", "CODEX_API_KEY": "k",
                "CODEX_HOME": "/elsewhere"}
        with mock.patch.dict(os.environ, base, clear=True):
            env = codexlocal.codex_env(codex=self.codex)
            self.assertEqual(os.environ["CODEX_HOME"], "/elsewhere")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertEqual(env["CODEX_HOME"], str(pathlib.Path("~/.codex-local").expanduser()))
        self.assertNotEqual(pathlib.Path(env["CODEX_HOME"]),
                            pathlib.Path("~/.codex").expanduser())

    def test_subcommand_comes_first(self):
        argv = codexlocal.launch_args("qwen3.5:35b", ["exec", "--json", "hi"], SETTINGS,
                                      self.codex, "/m.json")
        self.assertEqual(argv[:4], ["codex", "exec", "-m", "qwen3.5:35b"])
        self.assertEqual(argv[4:6], ["-c", 'model_reasoning_effort="high"'])
        self.assertEqual(argv[-2:], ["--json", "hi"])
        argv = codexlocal.launch_args("qwen3.5:35b", ["--disable", "goals"], SETTINGS,
                                      self.codex, "/m.json")
        self.assertEqual(argv[:3], ["codex", "-m", "qwen3.5:35b"])
        self.assertEqual(argv[-2:], ["--disable", "goals"])

    def test_arguments(self):
        self.assertEqual(codexlocal.parse_args(["--fallback", "exec"], SETTINGS),
                         ("gpt-oss:20b", False, ["exec"]))
        self.assertEqual(codexlocal.parse_args(["-m", "x:1b", "--show"], SETTINGS),
                         ("x:1b", True, []))
        with self.assertRaises(ValueError):
            codexlocal.parse_args(["--model"], SETTINGS)

    def test_show_starts_nothing(self):
        out = io.StringIO()
        with mock.patch.object(os, "execve") as execve, redirect_stdout(out):
            self.assertEqual(codexlocal.main(["--show"]), 0)
        execve.assert_not_called()
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "Backend   Ollama at http://localhost:11434/v1/ "
                                   "(Responses API)")
        self.assertIn('          -c model_catalog_json="config/local/codex-models.json"', lines)
        self.assertLessEqual(len(lines), 15)
        self.assertLessEqual(max(len(line) for line in lines), 100)
        self.assertNotIn(str(REPO), out.getvalue())

    def test_launch_uses_the_local_home(self):
        with mock.patch.object(codexlocal, "check_server"), \
                mock.patch.object(codexlocal, "ensure_home"), \
                mock.patch.object(codexlocal.shutil, "which", return_value="/bin/codex"), \
                mock.patch.object(os, "execve") as execve, \
                mock.patch("sys.stderr", io.StringIO()):
            codexlocal.main(["exec", "--json", "hi"])
        exe, argv, env = execve.call_args[0]
        self.assertEqual(exe, "/bin/codex")
        self.assertEqual(argv[:4], ["codex", "exec", "-m", "qwen3.5:35b"])
        self.assertIn(f'model_catalog_json="{codexlocal.CATALOG_FILE}"', argv)
        self.assertEqual(env["CODEX_HOME"], str(codexlocal.local_home()))

    def test_a_model_without_metadata_is_refused(self):
        err = io.StringIO()
        with mock.patch.object(os, "execve") as execve, mock.patch("sys.stderr", err):
            self.assertEqual(codexlocal.main(["--model", "nothing:1b"]), 2)
        execve.assert_not_called()
        self.assertIn("is not in codex-models.json", err.getvalue())


class HarnessTest(unittest.TestCase):
    def test_local_runs_have_their_own_configuration(self):
        cloud = runconfig.current_config("codex", None, "gpt-6.1-sol")
        local = runconfig.current_config("codex", None, "qwen3.5:35b")
        self.assertEqual(cloud, runconfig.LEGACY_CONFIG["codex"])
        self.assertNotEqual(local, cloud)
        self.assertNotEqual(local, runconfig.current_config("claude", None, "qwen3.5:35b"))

    def test_any_file_changes_the_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for name in ("over.json", "codex.json", "models.json", "ollama.json"):
                path = pathlib.Path(tmp) / name
                path.write_text("{}")
                files.append(path)
            first = codexlocal.config_hash(*files)
            for path in files:
                path.write_text('{"x": 1}')
                self.assertNotEqual(codexlocal.config_hash(*files), first)
                path.write_text("{}")

    def test_agent_command(self):
        copy = pathlib.Path("/runs/copy")
        local = baseline.agent_command("codex", "qwen3.5:35b", "high", "PROMPT", copy, "/tmp")
        cloud = baseline.agent_command("codex", "gpt-6.1-sol", "medium", "PROMPT", copy, "/tmp")
        self.assertIn('model_provider="ollama-local"', local)
        self.assertIn("--ignore-user-config", local)
        self.assertEqual(local[-3:], ["-C", str(copy), "PROMPT"])
        self.assertNotIn('model_provider="ollama-local"', cloud)
        self.assertFalse(any("model_catalog_json" in a for a in cloud))

    def test_agent_env(self):
        env = {"PATH": "/bin", "OPENAI_API_KEY": "sk"}
        with mock.patch.dict(os.environ, env, clear=True):
            cloud = baseline.agent_env(pathlib.Path("/cache"), None, None, "gpt-6.1-sol")
            mine = baseline.agent_env(pathlib.Path("/cache"), None, None, "qwen3.5:35b")
        self.assertNotIn("CODEX_HOME", cloud)
        self.assertEqual(mine["CODEX_HOME"], str(codexlocal.local_home()))
        self.assertNotIn("OPENAI_API_KEY", mine)
        self.assertEqual(mine["TF_PLUGIN_CACHE_DIR"], "/cache")

    def test_dry_run_shows_the_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with redirect_stdout(out):
                baseline.main(["app-post-incident", "--tool", "codex", "--model",
                               "qwen3.5:35b", "--effort", "high", "--dry-run",
                               "--log", f"{tmp}/log.csv", "--runs-dir", tmp])
        text = out.getvalue()
        self.assertIn("  Backend     Ollama at http://localhost:11434/v1/ · "
                      "CODEX_HOME=~/.codex-local (no ChatGPT sign-in)", text)
        self.assertIn(runconfig.current_config("codex", None, "qwen3.5:35b"), text)
        self.assertIn('model_catalog_json="config/local/codex-models.json"', text)

    def test_row_of_a_local_run(self):
        record = {
            "meta": {"run_id": "r1", "date": "2026-11-17", "rep": 1,
                     "task": "app-post-incident", "tool": "codex", "model": "qwen3.5:35b",
                     "effort": "high", "workload": "status-board-app",
                     "workload_tree": "abc", "prompt_sha": "def", "config": "d14440079894"},
            "agent": {"exit_code": 0, "timed_out": False, "wall_seconds": 300.0},
            "checks": {"acceptance": [{"rule": "pytest", "ok": True, "failed": []}],
                       "rules": [{"rule": "1 validate", "ok": True, "failed": []}]},
        }
        usage = baseline.empty_usage() | {
            "models": ["qwen3.5:35b"], "input": 30000, "cache_write": 0, "cache_read": 90000,
            "output": 4000, "cost_list_usd": None, "cost_credits": None,
            "unpriced_models": ["qwen3.5:35b"], "lanes": 1}
        row = baseline.build_row(record, usage)
        self.assertEqual(row["cost_list_usd"], 0)
        self.assertEqual(row["cost_credits"], "")
        self.assertEqual(row["notes"], "local model on Ollama: no token price")


class MetersTest(unittest.TestCase):
    def test_a_queued_request_starts_at_its_cache_line(self):
        bg, main, hit = ollama.log_requests(LOG)
        self.assertAlmostEqual(bg["load_s"], 8.0)
        self.assertAlmostEqual(bg["queued_s"], 0.0)
        self.assertAlmostEqual(main["queued_s"], 50.01, places=2)
        self.assertAlmostEqual(main["read_s"], 6.99, places=2)
        self.assertAlmostEqual(main["write_s"], 1.0, places=2)
        self.assertAlmostEqual(main["total_s"], 7.99, places=2)
        self.assertAlmostEqual(hit["queued_s"], 0.0)
        self.assertEqual((hit["new_tokens"], hit["cached_tokens"]), (100, 7000))

    def test_thread_meters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_file(pathlib.Path(tmp))
            meters = codexlocal.session_meters(path, LOG)
            self.assertEqual(codexlocal.find_thread(str(path)), path)
            self.assertEqual(codexlocal.find_thread("01a1ebd6", root=tmp), path)
        self.assertEqual(meters["tool"], "Codex")
        self.assertEqual(meters["session"], "01a1ebd6")
        self.assertEqual(meters["model"], "qwen3.5:35b")
        self.assertEqual([r["written_tokens"] for r in meters["requests"]], [None, 80, 60])
        self.assertEqual(meters["background"], 1)
        self.assertEqual(meters["cold_request"], 2)

    def test_no_request_in_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_file(pathlib.Path(tmp))
            with self.assertRaises(ValueError):
                codexlocal.session_meters(path, "")

    def test_task_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            meters = codexlocal.session_meters(session_file(pathlib.Path(tmp)), LOG)
            saved = pathlib.Path(tmp) / "task.json"
            saved.write_text(json.dumps(meters))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["ollama", "task", "--from", str(saved)]), 0)
        text = out.getvalue()
        self.assertIn("Codex on qwen3.5:35b · thread 01a1ebd6 · 3 requests", text)
        self.assertIn("Request 2 waited 50.0 s in the queue", text)
        self.assertIn("bg: a background request (not in the session file).", text)
        self.assertLessEqual(len(text.splitlines()), 15)
        self.assertLessEqual(max(len(line) for line in text.splitlines()), 100)


class LauncherFilesTest(unittest.TestCase):
    def test_launcher_and_inputs_exist(self):
        self.assertTrue(os.access(REPO / "bin" / "codex-local", os.X_OK))
        self.assertTrue((CODEX_LOCAL / "prompt.md").is_file())
        self.assertEqual((CODEX_LOCAL / "prompt.md").read_text(),
                         (FIXTURES / "claude-local" / "prompt.md").read_text())
        saved = json.loads((CODEX_LOCAL / "captured" / "task.json").read_text())
        self.assertEqual(saved["tool"], "Codex")


if __name__ == "__main__":
    unittest.main()
