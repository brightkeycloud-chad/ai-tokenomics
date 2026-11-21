"""Tests for the local model on Ollama. The fit math, the server
log, the four local meters, the prompts for the bench, and the CLI views.
Run from the repository root:

    python3 -m unittest discover -s tools/tests

The tests make no model request. One test connects to a closed local port.
"""
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics import cli, ollama  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CAPTURED = FIXTURES / "ollama-captured"

LOG = """\
time=2026-11-17T13:28:50.100-08:00 level=INFO source=routes.go:2474 msg="server config" \
env="map[HTTPS_PROXY: OLLAMA_CONTEXT_LENGTH:65536 OLLAMA_DEBUG:INFO]"
time=2026-11-17T13:28:53.400-08:00 level=INFO source=types.go:32 msg="inference compute" \
id=0 library=Metal name=MTL0 description="Apple M4 Pro" type=iGPU total="37.4 GiB" \
available="37.4 GiB"
time=2026-11-17T13:28:53.400-08:00 level=INFO source=routes.go:2586 \
msg="vram-based default context" total_vram="37.4 GiB" default_num_ctx=32768
time=2026-11-17T13:29:33.804-08:00 level=INFO source=sched.go:651 msg="system memory" \
total="48.0 GiB" free="34.6 GiB" free_swap="0 B"
"""

# A real /api/chat response: the second of two requests with the same prompt.
CACHED_RESPONSE = {
    "model": "qwen3.5:35b", "done": True, "done_reason": "stop",
    "total_duration": 770331584, "load_duration": 412875,
    "prompt_eval_count": 1111, "prompt_eval_cached_count": 1107,
    "prompt_eval_duration": 65849875, "eval_count": 48, "eval_duration": 690826750,
}


def run_cli(*argv) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    if code:
        raise AssertionError(f"tokenomics {' '.join(argv)} exited with {code}")
    return out.getvalue()


class ServerLogTest(unittest.TestCase):
    def test_reads_gpu_memory_default_window_and_setting(self):
        facts = ollama.parse_server_log(LOG)
        self.assertEqual(facts["gpu_gib"], 37.4)
        self.assertEqual(facts["gpu_name"], "Apple M4 Pro")
        self.assertEqual(facts["default_context"], 32768)
        self.assertEqual(facts["context_setting"], 65536)
        self.assertEqual(facts["memory_gib_log"], 48.0)

    def test_the_last_server_start_wins(self):
        later = LOG.replace("37.4 GiB", "28.0 GiB").replace("32768", "4096")
        facts = ollama.parse_server_log(LOG + later)
        self.assertEqual(facts["gpu_gib"], 28.0)
        self.assertEqual(facts["default_context"], 4096)

    def test_size_units(self):
        self.assertAlmostEqual(ollama._size_gib("512.0 MiB"), 0.5)
        self.assertAlmostEqual(ollama._size_gib("1 TiB"), 1024)


class FitTest(unittest.TestCase):
    def facts(self, **change):
        facts = {"model": "m", "gpu_gib": 37.4, "memory_gib": 48.0,
                 "parameters": 35_106_839_040, "file_bytes": 21_909_194_238,
                 "capabilities": ["completion", "tools"]}
        facts.update(change)
        return facts

    def test_fit_math(self):
        result = ollama.fit(self.facts())
        self.assertAlmostEqual(result["weight_budget_gib"], 31.4)
        self.assertAlmostEqual(result["gpu_share"], 37.4 / 48)
        self.assertAlmostEqual(result["bytes_per_weight"], 0.624, places=3)
        self.assertEqual(round(result["max_parameters"] / 1e9), 54)
        self.assertTrue(result["fits"])
        self.assertTrue(result["calls_tools"])
        self.assertAlmostEqual(result["spare_gib"], 31.4 - 21_909_194_238 / 2 ** 30)

    def test_hypothetical_laptop(self):
        # 36 GB for the GPU, 6 GB reserved, a bit over half a byte per weight:
        # about 50B parameters.
        result = ollama.fit(self.facts(gpu_gib=36.0, file_bytes=int(0.6 * 35e9),
                                       parameters=int(35e9)))
        self.assertEqual(result["weight_budget_gib"], 30.0)
        self.assertEqual(round(result["max_parameters"] / 1e9, -1), 50.0)

    def test_too_big_and_no_tools(self):
        result = ollama.fit(self.facts(file_bytes=40 * 2 ** 30, capabilities=["completion"]))
        self.assertFalse(result["fits"])
        self.assertFalse(result["calls_tools"])

    def test_missing_gpu_memory(self):
        with self.assertRaises(ValueError):
            ollama.fit(self.facts(gpu_gib=None))


class MetersTest(unittest.TestCase):
    def test_new_tokens_exclude_the_cached_tokens(self):
        m = ollama.meters(CACHED_RESPONSE)
        self.assertEqual(m["prompt_tokens"], 1111)
        self.assertEqual(m["cached_tokens"], 1107)
        self.assertEqual(m["new_tokens"], 4)
        self.assertAlmostEqual(m["read_s"], 0.065849875)
        self.assertAlmostEqual(m["write_tps"], 48 / 0.690826750)
        self.assertAlmostEqual(m["load_s"], 0.000412875)

    def test_missing_fields_are_zero(self):
        m = ollama.meters({"done": True})
        self.assertEqual(m["new_tokens"], 0)
        self.assertIsNone(m["read_tps"])
        self.assertIsNone(m["write_tps"])


class PromptTest(unittest.TestCase):
    def test_size_nonce_and_question(self):
        prompt = ollama.build_prompt(5000, nonce="abc123")
        self.assertTrue(prompt.startswith("Request abc123."))
        self.assertTrue(prompt.endswith(ollama.QUESTION))
        body = len(prompt) - len(ollama.QUESTION) - 2
        self.assertEqual(body, int(5000 * ollama.CHARS_PER_TOKEN))
        self.assertIn("=== workloads/status-board-app/", prompt)

    def test_two_prompts_start_differently(self):
        self.assertNotEqual(ollama.build_prompt(100)[:30], ollama.build_prompt(100)[:30])

    def test_too_many_tokens(self):
        with self.assertRaises(ValueError):
            ollama.build_prompt(10_000_000)


class SettingsTest(unittest.TestCase):
    def test_think_values(self):
        self.assertIs(ollama.think_value("on"), True)
        self.assertIs(ollama.think_value("false"), False)
        self.assertEqual(ollama.think_value("Low"), "low")
        self.assertIsNone(ollama.think_value(None))
        self.assertEqual(ollama.think_label(True), "on")
        self.assertEqual(ollama.think_label("medium"), "medium")

    def test_default_settings(self):
        settings = ollama.load_settings()
        self.assertGreaterEqual(settings["context_length"], 65536)
        self.assertIn(settings["model"], {"qwen3.5:35b", "gpt-oss:20b"})

    def test_save_replaces_the_same_model_and_thinking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "bench.json"
            ollama.save_results(path, [{"model": "a", "think": "on", "x": 1},
                                       {"model": "a", "think": "off", "x": 2}])
            ollama.save_results(path, [{"model": "a", "think": "on", "x": 3}])
            saved = json.loads(path.read_text())
            self.assertEqual(sorted((r["think"], r["x"]) for r in saved),
                             [("off", 2), ("on", 3)])

    def test_no_server(self):
        with unittest.mock.patch.dict(os.environ, {"OLLAMA_HOST": "127.0.0.1:9"}):
            with self.assertRaisesRegex(ValueError, "start the Ollama app"):
                ollama.api("/api/tags")


class CliTest(unittest.TestCase):
    def test_fit_view(self):
        out = run_cli("ollama", "fit", "--from", str(CAPTURED / "fit-qwen3.5-35b.json"))
        self.assertIn("calls tools: yes", out)
        self.assertIn("37.4 GiB  78% of memory", out)
        self.assertIn("≈54B", out)
        self.assertIn("fits, 11.0 GiB to spare", out)

    def test_fit_reserve(self):
        out = run_cli("ollama", "fit", "--from", str(CAPTURED / "fit-qwen3.5-35b.json"),
                      "--reserve", "20", "--json")
        self.assertFalse(json.loads(out)["fits"])

    def test_bench_view(self):
        out = run_cli("ollama", "bench", "--from", str(CAPTURED / "bench.json"))
        self.assertIn("qwen3.5:35b  on", out)
        self.assertIn("gpt-oss:20b  medium", out)
        self.assertEqual(len(out.splitlines()), 10)
        out = run_cli("ollama", "bench", "--from", str(CAPTURED / "bench.json"),
                      "--min-write", "61")
        self.assertIn("NO", out)

    def test_meters_view(self):
        out = run_cli("ollama", "meters", "--from", str(CAPTURED / "meters.json"))
        self.assertIn("prompt_eval_cached_count", out)
        self.assertLessEqual(len(out.splitlines()), 15)
        self.assertLessEqual(max(len(line) for line in out.splitlines()), 100)


if __name__ == "__main__":
    unittest.main()
