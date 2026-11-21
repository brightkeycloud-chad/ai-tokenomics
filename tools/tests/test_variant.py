"""Tests for the variant overlays of the harness. Run from the repository
root:

    python3 -m unittest discover -s tools/tests

The tests make no model request and no AWS call.
"""
import csv
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from tokenomics import baseline, index, sweep, variant  # noqa: E402
from tokenomics.runconfig import LEGACY_CONFIG, current_config  # noqa: E402
from test_costmodel import run_cli  # noqa: E402
from test_sweep import row  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
BLOATED_CONTEXT = FIXTURES / "bloated-context"
BLOATED = BLOATED_CONTEXT / "variants" / "bloated-agents-md"


def make_variant(root: pathlib.Path, name="test-variant", workload="status-board-app",
                 files=None) -> pathlib.Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "variant.json").write_text(json.dumps({"name": name, "workload": workload}))
    for rel, text in (files or {"AGENTS.md": "one\ntwo\n"}).items():
        path = root / "files" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def run_baseline(argv):
    out = io.StringIO()
    with redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()) as err:
        code = baseline.main(argv)
    return code, out.getvalue() + err.getvalue()


class SampleVariantTest(unittest.TestCase):
    def test_bloated_file_keeps_the_house_rules_and_stays_under_the_cap(self):
        v = variant.load_variant(BLOATED)
        self.assertEqual(v.name, "bloated-agents-md")
        self.assertEqual(v.workload, "status-board-app")
        self.assertEqual(v.files, ("AGENTS.md",))
        data = (BLOATED / "files" / "AGENTS.md").read_bytes()
        lines, size = variant.file_size(data)
        self.assertGreaterEqual(lines, 1400)
        self.assertLessEqual(lines, 1600)
        self.assertLess(size, 32_000)  # under 32 KB, and under the Codex cap of 32 KiB
        lean = baseline.committed_file("status-board-app", "HEAD", "AGENTS.md")
        self.assertTrue(data.startswith(lean), "the house rules must stay the same")

    def test_sample_sweep_file(self):
        s = sweep.load_sweep(BLOATED_CONTEXT / "sweep.json")
        self.assertEqual(s.tasks, ("app-post-incident",))
        self.assertEqual(s.baseline("claude").key,
                         ("claude", "claude-sonnet-5-5", "medium", ""))
        bloated = [x for x in s.setups if x.variant]
        self.assertEqual(len(bloated), 1)
        self.assertEqual(bloated[0].key,
                         ("claude", "claude-sonnet-5-5", "medium", "bloated-agents-md"))
        self.assertEqual(pathlib.Path(bloated[0].variant_dir).resolve(), BLOATED.resolve())


class LoadTest(unittest.TestCase):
    def test_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            with self.assertRaisesRegex(FileNotFoundError, "no variant.json"):
                variant.load_variant(root)
            make_variant(root / "bad", name="Bad Name")
            with self.assertRaisesRegex(ValueError, "lower case"):
                variant.load_variant(root / "bad")
            (root / "empty").mkdir()
            (root / "empty" / "variant.json").write_text('{"workload": "w"}')
            with self.assertRaisesRegex(ValueError, "no files"):
                variant.load_variant(root / "empty")

    def test_hash_changes_with_the_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = variant.load_variant(make_variant(pathlib.Path(tmp) / "a"))
            b = variant.load_variant(make_variant(pathlib.Path(tmp) / "b",
                                                  files={"AGENTS.md": "one\nthree\n"}))
            self.assertNotEqual(a.sha, b.sha)

    def test_sizes_and_apply(self):
        self.assertEqual(variant.file_size(b"a\nb"), (2, 3))
        self.assertEqual(variant.file_size(b""), (0, 0))
        self.assertEqual(variant.fmt_size(1511, 29357), "1,511 lines, 28.7 KB")
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            v = variant.load_variant(make_variant(root / "v", files={
                "AGENTS.md": "x\n" * 10, "docs/new.md": "new\n"}))
            compared = variant.compare_files(
                v, lambda rel: b"x\n" * 5 if rel == "AGENTS.md" else None)
            text = variant.describe_files(compared)
            self.assertEqual(text[0], "AGENTS.md: 10 lines, 0.0 KB (replaces 5 lines, "
                                      "0.0 KB: 2.0× the bytes)")
            self.assertEqual(text[1], "docs/new.md: 1 line, 0.0 KB (a new file)")
            copy = root / "copy"
            copy.mkdir()
            (copy / "AGENTS.md").write_text("old\n")
            variant.apply_variant(v, copy)
            self.assertEqual((copy / "AGENTS.md").read_text(), "x\n" * 10)
            self.assertEqual((copy / "docs" / "new.md").read_text(), "new\n")


class LogTest(unittest.TestCase):
    def legacy_log(self, path: pathlib.Path) -> None:
        with open(path, "w", newline="") as fout:
            writer = csv.DictWriter(fout, fieldnames=baseline.LEGACY_COLUMNS,
                                    lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            writer.writerow(row(rep=1))

    def test_legacy_log_stays_legacy_until_a_row_needs_more_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            self.legacy_log(log)
            # A reference run has no variant and no configuration.
            baseline.append_row(log, row(rep=1, tool="reference", config=""))
            with open(log, newline="") as fin:
                self.assertEqual(next(csv.reader(fin)), baseline.LEGACY_COLUMNS)
            baseline.append_row(log, row(rep=1) | {"variant": "bloated"})
            with open(log, newline="") as fin:
                self.assertEqual(next(csv.reader(fin)), baseline.COLUMNS)
            rows = baseline.read_log(log)
            self.assertEqual([r["variant"] for r in rows], ["", "", "bloated"])
            # The old rows are marked with the legacy configuration of their tool.
            self.assertEqual([r["config"] for r in rows],
                             [LEGACY_CONFIG["claude"], "", current_config("claude")])
            self.assertEqual(rows[0]["cost_list_usd"], "0.1")
            # The legacy row is another configuration: the repetitions start again.
            self.assertEqual(baseline.next_rep(rows, "app-post-incident", "claude",
                                               "claude-sonnet-5-5", "medium"), 1)
            self.assertEqual(baseline.next_rep(rows, "app-post-incident", "claude",
                                               "claude-sonnet-5-5", "medium", "bloated"), 2)

    def test_variant_log_gets_the_config_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            with open(log, "w", newline="") as fout:
                writer = csv.DictWriter(fout, fieldnames=baseline.VARIANT_COLUMNS,
                                        lineterminator="\n", extrasaction="ignore")
                writer.writeheader()
                writer.writerow(row(tool="codex", model="gpt-6.1-sol") | {"variant": "v"})
            baseline.append_row(log, row(rep=2))
            rows = baseline.read_log(log)
            self.assertEqual(list(rows[0]), baseline.COLUMNS)
            self.assertEqual([r["config"] for r in rows],
                             [LEGACY_CONFIG["codex"], current_config("claude")])

    def test_row_csv_shape(self):
        plain = baseline.row_csv(row(tool="reference", config="")).splitlines()[0].split(",")
        self.assertEqual(plain, baseline.LEGACY_COLUMNS)
        variant_only = baseline.row_csv(row(tool="reference", config="") | {"variant": "v"})
        self.assertEqual(variant_only.splitlines()[0].split(","), baseline.VARIANT_COLUMNS)
        full = baseline.row_csv(row()).splitlines()[0].split(",")
        self.assertEqual(full, baseline.COLUMNS)

    def test_count_runs_reads_legacy_rows(self):
        legacy = {k: v for k, v in row().items() if k not in ("variant", "config")}
        s = sweep.Setup("claude", "claude-sonnet-5-5", "medium")
        # The legacy row has the legacy configuration, not the current one.
        self.assertEqual(sweep.count_runs([legacy], "app-post-incident", s), 0)
        self.assertEqual(sweep.count_runs([row()], "app-post-incident", s), 1)
        sv = sweep.Setup("claude", "claude-sonnet-5-5", "medium", variant="v")
        self.assertEqual(sweep.count_runs([row()], "app-post-incident", sv), 0)


class IndexTest(unittest.TestCase):
    def rows(self):
        out = []
        for i, (cost, write) in enumerate([(0.10, 15000), (0.11, 16000), (0.12, 17000)], 1):
            out.append(row(rep=i, cost=cost) | {"cache_write": str(write),
                                                "cache_read": "80000"})
        for i, (cost, write) in enumerate([(0.15, 26000), (0.16, 27000), (0.17, 28000)], 1):
            out.append(row(rep=i, cost=cost) | {"variant": "bloated",
                                                "cache_write": str(write),
                                                "cache_read": "120000"})
        return out

    def test_variant_is_its_own_setup(self):
        setups = [sweep.Setup("claude", "claude-sonnet-5-5", "medium", baseline=True),
                  sweep.Setup("claude", "claude-sonnet-5-5", "medium", variant="bloated")]
        report, _ = index.index_log(self.rows(), setups, ["app-post-incident"], 90,
                                    lambda task: 10)
        lean, bloated = report[0].tools[0].results
        self.assertEqual((lean.runs, bloated.runs), (3, 3))
        self.assertEqual(lean.cache_write, 16000)
        self.assertEqual(bloated.cache_write, 27000)
        self.assertEqual(bloated.cache_read, 120000)
        self.assertAlmostEqual(bloated.cost_index, 0.16 / 0.11)
        self.assertEqual(bloated.verdict, "Costlier")

    def test_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "log.csv"
            for r in self.rows():
                baseline.append_row(log, r)
            code, out, _ = run_cli("index", str(log), "--baseline",
                                   "claude-sonnet-5-5/medium", "--cache")
            self.assertEqual(code, 0)
            self.assertIn("Variant", out)
            self.assertIn("Cache write  Cache read", out)
            self.assertIn("bloated", out)
            self.assertIn("27.0k", out)
            code, out, _ = run_cli("log", str(log))
            self.assertIn("Variant", out)
            code, out, _ = run_cli("index", str(log), "--baseline",
                                   "claude-sonnet-5-5/medium")
            self.assertNotIn("Cache write", out)


class HarnessTest(unittest.TestCase):
    def test_dry_run_shows_the_variant_and_the_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_baseline(["app-post-incident", "--tool", "claude", "--model",
                                      "claude-sonnet-5-5", "--effort", "medium",
                                      "--variant", str(BLOATED), "--dry-run",
                                      "--runs-dir", tmp, "--log", f"{tmp}/log.csv"])
            self.assertEqual(code, 0)
            self.assertIn("Variant     bloated-agents-md", out)
            self.assertIn("AGENTS.md: 1,511 lines", out)
            self.assertIn("replaces 127 lines, 4.1 KB", out)
            self.assertEqual(list(pathlib.Path(tmp).iterdir()), [])

    def test_variant_must_match_the_workload(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_baseline(["infra-5xx-alarm", "--tool", "claude", "--model",
                                      "claude-sonnet-5-5", "--effort", "medium",
                                      "--variant", str(BLOATED), "--dry-run",
                                      "--runs-dir", tmp, "--log", f"{tmp}/log.csv"])
            self.assertEqual(code, 2)
            self.assertIn("changes status-board-app, but task infra-5xx-alarm", out)

    def test_copy_only_puts_the_variant_in_the_start_commit(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(baseline, "setup_copy", return_value="no setup (test)"):
            v = make_variant(pathlib.Path(tmp) / "v", files={"AGENTS.md": "short\n"})
            runs = pathlib.Path(tmp) / "runs"
            code, out = run_baseline(["app-post-incident", "--tool", "claude", "--model",
                                      "claude-sonnet-5-5", "--effort", "medium",
                                      "--variant", str(v), "--copy-only", "--run-id", "c1",
                                      "--runs-dir", str(runs), "--log", f"{tmp}/log.csv"])
            self.assertEqual(code, 0, out)
            self.assertIn("Copy ready", out)
            copy = runs / "c1"
            shown = subprocess.run(["git", "show", f"{baseline.START_TAG}:AGENTS.md"],
                                   cwd=copy, capture_output=True, text=True).stdout
            self.assertEqual(shown, "short\n")
            self.assertFalse((runs / "c1.record").exists())
            self.assertFalse((pathlib.Path(tmp) / "log.csv").exists())

    def test_sweep_plan_shows_the_variant_column_only_when_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_baseline(["--sweep", str(BLOATED_CONTEXT / "sweep.json"), "--dry-run",
                                      "--log", f"{tmp}/log.csv"])
            self.assertEqual(code, 0)
            self.assertIn("Variant", out)
            self.assertIn("bloated-agents-md  Bloated AGENTS.md", out)
            code, out = run_baseline(["--sweep", str(BLOATED_CONTEXT / "sweep.json"), "--dry-run",
                                      "--variant", str(BLOATED), "--log", f"{tmp}/log.csv"])
            self.assertEqual(code, 2)
            code, out = run_baseline(["app-post-incident", "--tool", "claude", "--model",
                                      "claude-sonnet-5-5,claude-haiku-5-5", "--effort",
                                      "medium", "--variant", str(BLOATED), "--dry-run",
                                      "--log", f"{tmp}/log.csv"])
            self.assertEqual(code, 0)
            self.assertIn("bloated-agents-md", out)


if __name__ == "__main__":
    unittest.main()
