"""The baseline run harness: `bin/run-baseline`.

The rule: change one thing, and pin everything else. One run
of the harness does these steps:

1. Copy a workload from this repository at a pinned commit (default HEAD) to
   a new directory, <runs dir>/<run ID>, outside the repository.
2. Prepare the copy: a shared Python environment (app) or a Terraform init
   with a shared plugin cache (infra). Make the copy a new git repository,
   and tag the start state `baseline-start`.
3. Run the agent non-interactively, with the task prompt from a file, the
   full model ID, an explicit effort level, and a checked-in configuration:

       claude -p ... --output-format json     (Claude Code)
       codex exec --json ...                  (Codex)
       reference                              (apply the reference patch: no
                                               model, to check the harness)

4. Commit the result of the agent, tag it `agent-result`, and save the
   change as <run ID>.record/agent.patch. Copy in the hidden acceptance
   tests, which the agent did not see, and run them. Then check the house
   rules of the workload (AGENTS.md).
5. Read the usage of the run with the usage readers (`tokenomics session`
   for Claude Code, `tokenomics codex` for Codex). Make a reduced copy of
   the transcripts, with no text and no paths, that is safe to commit.
6. Add one row to the results log (the same columns every time), and write a record
   of the run to <runs dir>/<run ID>.record/.

The agent never sees the repository: the copy is outside it. The harness
removes API keys and AWS credentials from the environment of the agent, so a
run uses the subscription and cannot change an AWS account.

`--brief` gives a short result of one run, a sweep, or `--show`: the run, the
checks, the tokens, the cost, and the time, at most about 15 lines and 100
columns. Only a failed check gets its own line. A run with `--brief` prints
no progress lines. `--dry-run --brief` for one run shows only the pinned
items (workload, prompt, variant, agent command, configuration), with the
agent command wrapped at 100 columns. For a sweep, it shows the plan table
without the lines about the sweep file and the log.

`--rebuild RECORD_DIR` makes the row again from a record and its reduced
transcripts, with no model request. Check scripts use it. Add
`--append` to add the row to the log (for example, a smoke run of a local
setup becomes repetition 1 of that setup).

Sweeps (see sweep.py): give comma-separated models or effort
levels, `--reps N`, or `--sweep FILE`. The harness then makes every missing
run of every setup, one at a time, in rounds. `--dry-run` shows the plan.

Variants (see variant.py): `--variant DIR` puts the files of a
variant over the copy before the start commit, for example a bloated
AGENTS.md. The `variant` column of the log records the name. `--copy-only`
makes and prepares the copy, then stops: no agent, no checks, no log row. Use
it to look at the start state, for example with `/context` in Claude Code.

Sequences (see sequence.py): `--sequence FILE` runs several
prompts in one Claude Code session, with idle time between them (`claude -p`,
then `claude -p --resume`). Use it for cache experiments. `--idle SECONDS`
changes the idle time of the last step. A sequence adds no row to the log.

Extra tasks (see tasks.py): a task can live outside baseline/tasks,
with its own Claude Code settings, an AWS profile for the agent, and an
answer check in place of hidden tests. The harness writes the final answer of
the agent to <record>/answer.md, and the checks read it. A variant can paste
a file into the prompt, or add `claude -p` arguments (--mcp-config,
--plugin-dir). `--context` makes the copy of each setup and runs `/context`
in it, with the same arguments as a run: no model request. It gives the
always-on tokens of each extension (see extensions.py).

`--diff RUN` shows what the agent changed in a run. RUN is a run ID, a copy
or record directory, or a task (`app`, `infra`, or a task ID): then the
latest run of that task in the log, filtered by --tool, --model, --effort,
and --rep. `--stat` shows only the changed files.
"""
from __future__ import annotations

import argparse
import ast
import csv
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from . import codex
from . import codexlocal as codexlocal_mod
from . import extensions as extensions_mod
from . import sequence as sequence_mod
from . import sweep as sweep_mod
from . import variant as variant_mod
from .rates import REPO_ROOT, load_profiles, project_roots
from . import local as local_mod
from .runconfig import (CONFIG_DIR, CONFIG_FILES, LEGACY_CONFIG, config_file, current_config,
                        row_config)
from .tasks import TASKS_DIR, demo_tasks, list_tasks, load_task
from .reduce import leftover_strings, reduce_session
from .session import find_session, load_session, summarize

BASELINE_DIR = REPO_ROOT / "baseline"
DEFAULT_LOG = REPO_ROOT / "results" / "results-log.csv"
TOOLS = ("claude", "codex", "reference")
START_TAG = "baseline-start"
RESULT_TAG = "agent-result"
RUN_ID = re.compile(r"^\d{8}-\d{6}-")
GIT_ID = ["-c", "user.name=tokenomics-baseline", "-c", "user.email=baseline@example.invalid",
          "-c", "commit.gpgsign=false"]

# The results log: one row for each run, the same columns every time.
COLUMNS = [
    # Identity
    "run_id", "date", "rep",
    # Setup
    "task", "tool", "tool_version", "model", "effort", "workload_tree", "prompt_sha",
    "variant", "config",
    # Outcome
    "tests_pass", "rules_followed", "manual_fixes",
    # Usage (tokens by type, all lanes)
    "input", "cache_write", "cache_read", "output", "reasoning", "context_peak", "cache_hit",
    # Cost (record the unit in the column name)
    "cost_list_usd", "cost_credits", "vendor_estimate_usd", "window_5h_pct",
    # Time
    "wall_seconds", "api_seconds", "your_seconds",
    # Detail
    "rules_broken", "notes",
]
# Older logs. The first logs have no `variant` and no `config`. A later version
# added `variant`. The configuration change of 2026-11-16 added `config` (see
# runconfig.py). An old log stays as it is until the first row that needs a
# new column. Then the harness adds the columns, and writes the legacy
# configuration into the old rows.
VARIANT_COLUMNS = [c for c in COLUMNS if c != "config"]
LEGACY_COLUMNS = [c for c in VARIANT_COLUMNS if c != "variant"]

# Environment variables that the agent must not see. API keys change the meter
# from the subscription to API billing. AWS credentials could let an agent
# change an account; the tasks need none. The CLAUDE_* variables of a Claude
# Code session (for example CLAUDE_EFFORT) must not change a run that starts
# from inside that session. CLAUDE_CONFIG_DIR stays: it tells where the
# transcripts are. The backend variables (ANTHROPIC_BASE_URL and the others)
# must not send a cloud run to another server, or a local run to the cloud:
# the harness sets them only for a run on a local model.
REMOVED_ENV = re.compile(r"^(ANTHROPIC_API_KEY|OPENAI_API_KEY|AWS_.*|CLAUDECODE|CLAUDE_CODE_.*"
                         r"|CLAUDE_EFFORT|CLAUDE_PID|ANTHROPIC_BASE_URL|ANTHROPIC_AUTH_TOKEN"
                         r"|ANTHROPIC_MODEL|ANTHROPIC_DEFAULT_\w+_MODEL)$")
NO_AWS = {"AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
          "AWS_EC2_METADATA_DISABLED": "true"}


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

def file_sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def check_model_id(model: str) -> None:
    """Record the full version ID, not an alias. Aliases have no digit."""
    if not re.search(r"\d", model):
        raise ValueError(f"model {model!r} looks like an alias. Give the full model ID, "
                         "for example claude-sonnet-5-5 or gpt-6.1-sol.")


# --------------------------------------------------------------------------
# Agent commands
# --------------------------------------------------------------------------

def temp_dir() -> str:
    """The real path of the temporary directory (Terraform plugins put sockets there)."""
    return os.path.realpath(tempfile.gettempdir())


def toml_value(value) -> str:
    """A JSON value as a TOML value, for `codex exec -c key=value`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {toml_value(v)}"
                               for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(value)


def codex_overrides(tmp: str) -> list[str]:
    """`-c` options from baseline/config/codex-overrides.json, in order. The text
    {tmpdir} changes to the real path of the temporary directory."""
    data = json.loads((CONFIG_DIR / "codex-overrides.json").read_text(encoding="utf-8"))
    out = []
    for key, value in data["overrides"].items():
        out += ["-c", f"{key}={toml_value(value)}".replace("{tmpdir}", tmp)]
    return out


def claude_permission_mode(task: dict | None = None) -> str:
    """The permission mode of Claude Code runs. It comes from
    permissions.defaultMode in the settings file (of the task), so
    the config hash of the file covers it. The harness also passes it with
    --permission-mode, so the mode applies even where a settings file cannot
    set it. Default: dontAsk."""
    settings = json.loads(config_file("claude", task).read_text())
    return (settings.get("permissions") or {}).get("defaultMode", "dontAsk")


def agent_command(tool: str, model: str, effort: str, prompt: str, copy_dir: pathlib.Path,
                  tmp: str | None = None, task: dict | None = None,
                  variant=None) -> list[str]:
    """The command line of one run. The prompt is the full text of the prompt
    (with a pasted file, if the variant has one). An extra task can
    give its own settings file, keep skills, and allow plugin MCP servers. A
    variant can add arguments."""
    if tool == "claude":
        task = task or {}
        argv = ["claude", "-p", prompt, "--output-format", "json",
                "--model", model, "--effort", effort,
                "--setting-sources", "project",
                "--settings", str(config_file("claude", task or None))]
        if task.get("strict_mcp_config", True):
            argv.append("--strict-mcp-config")
        if not task.get("skills"):
            argv.append("--disable-slash-commands")
        argv += ["--permission-mode", claude_permission_mode(task or None)]
        return argv + variant_mod.expand_args(variant, copy_dir)
    if tool == "codex":
        local = (codexlocal_mod.codex_args() if local_mod.is_local_model(model) else [])
        return (["codex", "exec", "--json", "--ignore-user-config", "--ignore-rules",
                 "-m", model, "-c", f"model_reasoning_effort={json.dumps(effort)}"]
                + codex_overrides(tmp or temp_dir()) + local + ["-C", str(copy_dir), prompt])
    raise ValueError(f"no agent command for tool {tool!r}")


def show_args(argv: list[str], prompt: str, replace: dict[str, str]) -> list[str]:
    """The arguments as text, with the prompt and long paths replaced by names."""
    out = []
    for arg in argv:
        if arg == prompt:
            out.append('"$(cat prompt.md)"')
            continue
        for old, new in replace.items():
            arg = arg.replace(old, new)
        out.append(arg if arg.startswith("<") else shlex.quote(arg))
    return out


def show_command(argv: list[str], prompt: str, replace: dict[str, str]) -> str:
    """The command as text, with the prompt and long paths replaced by names."""
    return " ".join(show_args(argv, prompt, replace))


def wrap_command(args: list[str], first: str, indent: int, width: int = 100) -> list[str]:
    """The command in lines of at most `width` columns (--dry-run --brief). A
    line breaks only between arguments, and never between an option and its
    value. The first line starts with `first`; the next lines start with
    `indent` spaces. An argument longer than one line gets a line of its own."""
    groups = []
    for arg in args:
        if groups and groups[-1].startswith("-") and " " not in groups[-1] \
                and not arg.startswith("-"):
            groups[-1] += " " + arg
        else:
            groups.append(arg)
    lines, line = [], first
    for group in groups:
        if line.strip() and len(line) + 1 + len(group) > width:
            lines.append(line)
            line = " " * indent + group
        else:
            line += ("" if not line.strip() or line.endswith(" ") else " ") + group
    lines.append(line)
    return lines


def agent_env(plugin_cache: pathlib.Path, aws_profile: str | None = None,
              claude_model: str | None = None,
              codex_model: str | None = None) -> dict[str, str]:
    """The environment of the agent. With aws_profile (an extra task),
    the AWS configuration files stay and AWS_PROFILE names the profile of the
    agent. AWS key variables are removed in both cases. With claude_model on a
    local model, the variables of config/local/claude-code.json
    send Claude Code to Ollama. With codex_model on a local model,
    CODEX_HOME is the separate Codex home of config/local/codex.json,
    which has no ChatGPT sign-in."""
    env = {k: v for k, v in os.environ.items() if not REMOVED_ENV.match(k)}
    if claude_model and local_mod.is_local_model(claude_model):
        env = local_mod.claude_env(claude_model, base=env)
    if codex_model and local_mod.is_local_model(codex_model):
        env = codexlocal_mod.codex_env(base=env)
    if aws_profile:
        env["AWS_PROFILE"] = aws_profile
    else:
        env.update(NO_AWS)
    env["TF_PLUGIN_CACHE_DIR"] = str(plugin_cache)
    env["CHECKPOINT_DISABLE"] = "1"
    return env


def refuse_api_keys() -> None:
    found = [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY") if os.environ.get(k)]
    if found:
        raise ValueError(f"{' and '.join(found)} is set. The baseline uses the subscription. "
                         f"Unset it (unset {' '.join(found)}), then run again.")


# --------------------------------------------------------------------------
# Copy and setup
# --------------------------------------------------------------------------

def git(args: list[str], cwd: pathlib.Path, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True,
                          **kw)


def workload_tree(workload: str, ref: str) -> str:
    return git(["rev-parse", "--short=12", f"{ref}:workloads/{workload}"], REPO_ROOT).stdout.strip()


def copy_workload(workload: str, ref: str, dest: pathlib.Path) -> None:
    """Extract the committed workload at ref into dest (not the working tree)."""
    data = subprocess.run(["git", "archive", "--format=tar", f"{ref}:workloads/{workload}"],
                          cwd=REPO_ROOT, check=True, capture_output=True).stdout
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError:  # Python before 3.10.12 has no filter argument
            tar.extractall(dest)


def committed_file(workload: str, ref: str, rel: str) -> bytes | None:
    """The bytes of one workload file at ref, or None if it does not exist."""
    proc = subprocess.run(["git", "show", f"{ref}:workloads/{workload}/{rel}"], cwd=REPO_ROOT,
                          capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def requirements_hash(copy_dir: pathlib.Path) -> str:
    h = hashlib.sha256()
    for name in ("requirements.txt", "requirements-dev.txt"):
        p = copy_dir / name
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


def shared_venv(runs_dir: pathlib.Path, workload: str, copy_dir: pathlib.Path) -> pathlib.Path:
    return runs_dir / ".cache" / f"venv-{workload}-{requirements_hash(copy_dir)}"


def setup_copy(task: dict, copy_dir: pathlib.Path, runs_dir: pathlib.Path,
               env: dict, log) -> str:
    """Prepare the copy for the agent. Return one line that tells what was done."""
    kind = task["setup"]
    if kind == "python-venv":
        venv = shared_venv(runs_dir, task["workload"], copy_dir)
        if not (venv / "bin" / "python").exists():
            log(f"Make the shared Python environment {venv} (one time)")
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
            subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "-q",
                            "--disable-pip-version-check", "-r", "requirements-dev.txt"],
                           cwd=copy_dir, check=True)
        (copy_dir / "venv").symlink_to(venv)
        return f"shared Python environment {venv.name}, linked as venv"
    if kind == "none":
        return "no setup (the agent only reads the copy)"
    if kind == "terraform-init":
        pathlib.Path(env["TF_PLUGIN_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
        subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                       cwd=copy_dir / "terraform", env=env, check=True, capture_output=True)
        return "terraform init -backend=false, shared plugin cache"
    raise ValueError(f"unknown setup {kind!r} in task {task['id']}")


def start_repo(copy_dir: pathlib.Path) -> None:
    git(["init", "-q", "-b", "main"], copy_dir)
    exclude = copy_dir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    # A venv symbolic link is a file, so the workload pattern "venv/" does not match it.
    exclude.write_text("venv\n.terraform/\n.pytest_cache/\n__pycache__/\n", encoding="utf-8")
    git(["add", "-A"], copy_dir)
    git([*GIT_ID, "commit", "-q", "-m", "baseline start"], copy_dir)
    git(["tag", START_TAG], copy_dir)


def commit_result(copy_dir: pathlib.Path) -> list[str]:
    """Commit what the agent changed. Return the changed paths."""
    git(["add", "-A"], copy_dir)
    git([*GIT_ID, "commit", "-q", "--allow-empty", "-m", "agent result"], copy_dir)
    git(["tag", "-f", RESULT_TAG], copy_dir)
    out = git(["diff", "--name-only", START_TAG, "HEAD"], copy_dir).stdout
    return [line for line in out.splitlines() if line]


def save_hook_logs(copy_dir: pathlib.Path, record_dir: pathlib.Path) -> dict[str, int]:
    """Copy the hook logs of the copy (.claude/*.log) to <record>/hook-logs/.

    A hook that a variant adds can write a marker line to a log in .claude/,
    to prove that it ran in the headless run. Return the number of lines of
    each log."""
    found = {}
    for log in sorted((copy_dir / ".claude").glob("*.log")):
        dest = record_dir / "hook-logs" / log.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(log, dest)
        found[log.name] = sum(1 for line in log.read_text(encoding="utf-8").splitlines()
                              if line.strip())
    return found


def result_ref(copy_dir: pathlib.Path) -> str:
    """The commit of the agent result. Copies from before the tag: HEAD."""
    found = git(["rev-parse", "-q", "--verify", f"refs/tags/{RESULT_TAG}"], copy_dir,
                check=False)
    return RESULT_TAG if found.returncode == 0 else "HEAD"


def save_patch(copy_dir: pathlib.Path, record_dir: pathlib.Path) -> pathlib.Path:
    """Save the change of the agent in the record, so that it stays after cleanup."""
    patch = git(["diff", "--binary", START_TAG, result_ref(copy_dir)], copy_dir).stdout
    path = record_dir / "agent.patch"
    path.write_text(patch, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# What the agent changed: --diff
# --------------------------------------------------------------------------

def match_task(name: str) -> str:
    """A task ID from a task ID or the start of one (for example app or infra)."""
    tasks = list_tasks() + sorted(demo_tasks())
    if name in tasks:
        return name
    found = [t for t in tasks if t.startswith(name)]
    if len(found) != 1:
        raise ValueError(f"no run, run directory, or single task matches {name!r}. "
                         f"Tasks: {', '.join(tasks)}")
    return found[0]


def find_run(selector: str | None, task: str | None, runs_dir: pathlib.Path,
             log_path: pathlib.Path, tool: str | None = None, model: str | None = None,
             effort: str | None = None, rep: int | None = None, variant: str | None = None
             ) -> tuple[pathlib.Path, pathlib.Path]:
    """(copy directory, record directory) of one run."""
    if selector:
        path = pathlib.Path(selector).expanduser()
        if path.is_dir():
            if path.name.endswith(".record"):
                return path.with_name(path.name[: -len(".record")]), path
            return path, path.with_name(path.name + ".record")
        if RUN_ID.match(selector):
            return runs_dir / selector, runs_dir / f"{selector}.record"
        task = selector
    if not task:
        raise ValueError("give a run ID, a run directory, or a task (app, infra, or a task ID)")
    task = match_task(task)
    want = {"task": task, "tool": tool, "model": model, "effort": effort,
            "rep": None if rep is None else str(rep), "variant": variant}
    rows = [r for r in read_log(log_path)
            if all(v is None or (r.get(k) or "") == v for k, v in want.items())]
    if not rows:
        given = ", ".join(f"{k} {v}" for k, v in want.items() if v is not None)
        raise FileNotFoundError(f"no run with {given} in {repo_path(log_path)}")
    run_id = rows[-1]["run_id"]
    return runs_dir / run_id, runs_dir / f"{run_id}.record"


def agent_diff(copy_dir: pathlib.Path, record_dir: pathlib.Path, stat: bool = False,
               color: bool = False) -> str:
    """The change of the agent: from the copy, or from agent.patch in the record."""
    if (copy_dir / ".git").is_dir():
        return git(["diff", f"--color={'always' if color else 'never'}",
                    "--stat" if stat else "--patch", START_TAG, result_ref(copy_dir)],
                   copy_dir).stdout
    patch = record_dir / "agent.patch"
    if patch.is_file():
        if stat:
            return subprocess.run(["git", "apply", "--stat", str(patch)], check=True,
                                  capture_output=True, text=True).stdout
        return patch.read_text(encoding="utf-8")
    raise FileNotFoundError(f"{copy_dir} is gone, and {record_dir} has no agent.patch.")


def resolve_record(path: str) -> pathlib.Path:
    """The record directory for --show and --rebuild. A relative path that does
    not exist from the current directory is also tried from the repository
    root, because the runbooks give paths from the root."""
    given = pathlib.Path(path).expanduser()
    tried = [given]
    if not given.is_absolute():
        tried += [root / given for root in project_roots(REPO_ROOT)]
    for candidate in tried:
        if (candidate / "run.json").is_file():
            return candidate
    raise FileNotFoundError(
        f"no run record (run.json) at {path}. Looked in: "
        + ", ".join(str(c) for c in tried)
        + ". Give the path from the current directory or from the repository root.")


def print_diff(copy_dir: pathlib.Path, record_dir: pathlib.Path, stat: bool) -> None:
    run_json = record_dir / "run.json"
    if run_json.is_file():
        record = json.loads(run_json.read_text(encoding="utf-8"))
        row = record["row"]
        print(describe_record(record))
        print(f"Tests pass: {row['tests_pass']} · rules followed: {row['rules_followed']}"
              + (f" (broken: {row['rules_broken']})" if row["rules_broken"] else ""))
        changed = record.get("changed", [])
        print(f"Changed: {len(changed)} file{'' if len(changed) == 1 else 's'}" + (f" ({', '.join(changed)})" if changed else ""))
        print()
    diff = agent_diff(copy_dir, record_dir, stat, sys.stdout.isatty())
    print(diff if diff else "The agent changed no files.", end="" if diff else "\n")


# --------------------------------------------------------------------------
# Checks: hidden acceptance tests and house rules
# --------------------------------------------------------------------------

RUN_LINE = re.compile(r'run "([^"]+)"\.\.\. (pass|fail|skip)')
PYTEST_FAILED = re.compile(r"^FAILED (\S+)", re.M)


def copy_hidden(task: dict, mapping: dict, copy_dir: pathlib.Path) -> None:
    for src, dest in mapping.items():
        target = copy_dir / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(task["dir"] / src, target)


def run_commands(commands: list[dict], copy_dir: pathlib.Path, env: dict,
                 log_file: pathlib.Path, names: dict[str, str] | None = None) -> list[dict]:
    """Run the check commands. names: {task} and {answer} in a command."""
    results = []
    with open(log_file, "a", encoding="utf-8") as fout:
        for c in commands:
            argv = [arg.format(**names) if names and "{" in arg else arg for arg in c["run"]]
            proc = subprocess.run(argv, cwd=copy_dir / c["cwd"], env=env,
                                  capture_output=True, text=True)
            output = proc.stdout + proc.stderr
            fout.write(f"$ {shlex.join(argv)}   (in {c['cwd']})\n{output}\n"
                       f"exit {proc.returncode}\n\n")
            failed = [name for name, state in RUN_LINE.findall(output) if state == "fail"]
            failed += PYTEST_FAILED.findall(output)
            results.append({"rule": c["name"], "ok": proc.returncode == 0,
                            "failed": failed})
    return results


def _functions(source: str) -> list[ast.FunctionDef]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _decorator(fn, method: str):
    for d in fn.decorator_list:
        if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and d.func.attr == method):
            return d
    return None


def _called_names(fn) -> set[str]:
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute)
                      else "")
    return names


def _code_names(source: str) -> set[str]:
    """Names, attributes, and imported modules in Python code (not in text or comments)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _healthz(source: str | None) -> str | None:
    for fn in _functions(source or ""):
        if fn.name == "healthz":
            return ast.dump(fn)
    return None


def app_house_rules(root: pathlib.Path, start: dict, changed: list[str]) -> list[dict]:
    """The five house rules in the AGENTS.md of the application, checked on the files."""
    def read(rel):
        p = root / rel
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    main = read("app/main.py")
    validators = {fn.name for fn in _functions(read("app/validators.py"))
                  if fn.name.startswith("check_")}
    posts = [(fn, _decorator(fn, "post")) for fn in _functions(main)]
    posts = [(fn, d) for fn, d in posts if d is not None]
    rules = []

    used = set()
    for fn, _ in posts:
        used |= _called_names(fn) & validators
    rules.append({"rule": "1 validate in validators.py",
                  "ok": bool(posts) and bool(used - {"check_service_id"}),
                  "failed": [] if posts else ["no POST route in app/main.py"]})

    partiql = [p.relative_to(root).as_posix() for p in sorted((root / "app").glob("*.py"))
               if "execute_statement" in _code_names(p.read_text(encoding="utf-8"))]
    boto = "boto3" in _code_names(main)
    rules.append({"rule": "2 DynamoDB only through store.py",
                  "ok": not boto and not partiql,
                  "failed": (["boto3 in app/main.py"] if boto else []) + partiql})

    def returns_model(fn, d):
        if any(k.arg == "response_model" for k in d.keywords):
            return True
        return isinstance(fn.returns, ast.Name) and fn.returns.id not in ("dict", "Any")
    rules.append({"rule": "3 return a model",
                  "ok": bool(posts) and all(returns_model(fn, d) for fn, d in posts),
                  "failed": []})

    tests = read("tests/test_api.py")
    rules.append({"rule": "4 test every route",
                  "ok": "tests/test_api.py" in changed and ".post(" in tests
                  and "incidents" in tests, "failed": []})

    rules.append({"rule": "5 /healthz free of dependencies",
                  "ok": _healthz(main) is not None
                  and _healthz(main) == _healthz(start.get("app/main.py")),
                  "failed": []})
    return rules


def hcl_blocks(text: str, kind: str) -> dict[str, str]:
    """Bodies of the top-level blocks `kind "name" { ... }` in Terraform text.
    The scanner skips strings and comments, so braces in "${...}" do not count."""
    blocks = {}
    for m in re.finditer(rf'(?m)^{kind}\s+"([^"]+)"\s*\{{', text):
        i, depth = m.end(), 1
        while i < len(text) and depth:
            ch = text[i]
            if ch == '"':
                i += 1
                while i < len(text) and text[i] != '"':
                    i += 2 if text[i] == "\\" else 1
            elif ch == "#" or text.startswith("//", i):
                while i < len(text) and text[i] != "\n":
                    i += 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        blocks[m.group(1)] = text[m.end():i - 1]
    return blocks


def added_lines(root: pathlib.Path, suffix: str) -> list[tuple[str, str]]:
    """(path, line) for each line that the agent added since the start tag."""
    out = git(["diff", "--unified=0", "--no-color", START_TAG, "HEAD"], root).stdout
    lines, current = [], ""
    for line in out.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and current.endswith(suffix):
            lines.append((current, line[1:]))
    return lines


HARD_CODED = re.compile(r'"[^"\n]*(arn:aws[^"\n]*|\b\d{12}\b|\b[a-z]{2}-[a-z]+-\d[a-z]?\b)[^"\n]*"')


def infra_house_rules(root: pathlib.Path, start: dict, changed: list[str]) -> list[dict]:
    """The static parts of the AGENTS.md conventions of the infrastructure. The
    hidden rules test checks names, tags, and the variable validation."""
    missing = []
    for path in sorted((root / "terraform").glob("*.tf")):
        text = path.read_text(encoding="utf-8")
        for kind in ("variable", "output"):
            for name, body in hcl_blocks(text, kind).items():
                if not re.search(r"(?m)^\s*description\s*=", body):
                    missing.append(f"{kind} {name}")
    hard = []
    for rel, line in added_lines(root, ".tf"):
        code = line.split("#", 1)[0]
        if HARD_CODED.search(code):
            hard.append(f"{rel}: {code.strip()[:60]}")
    return [{"rule": "3 look values up, do not hard-code them", "ok": not hard, "failed": hard},
            {"rule": "4 describe every variable and output", "ok": not missing,
             "failed": missing}]


def read_only_rule(root: pathlib.Path, start: dict, changed: list[str]) -> list[dict]:
    """A task that only reads: the agent changed no file."""
    return [{"rule": "read only: no file changed", "ok": not changed, "failed": changed[:4]}]


STATIC_RULES = {"app-house-rules": app_house_rules, "infra-house-rules": infra_house_rules,
                "read-only": read_only_rule}


# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------

def _round(value, digits):
    return None if value is None else round(value, digits)


def claude_usage(result: dict, report: dict) -> dict:
    """Usage of one Claude Code run: the `claude -p` result and `tokenomics session`."""
    u = report["usage"]
    models = sorted({m for lane in report["lanes"] for m in lane["models"]})
    return {
        "tool_version": report["version"], "models": models,
        "input": u["input"], "cache_write": u["cache_write_5m"] + u["cache_write_1h"],
        "cache_read": u["cache_read"], "output": u["output"], "reasoning": None,
        "context_peak": report["context"]["peak"],
        "cache_hit": _round(report["ratios"]["cache_hit"], 3),
        "cost_list_usd": _round(report["cost"], 4) if report["unit"] == "USD" else None,
        "cost_credits": None,
        "vendor_estimate_usd": _round(result.get("total_cost_usd"), 4),
        "window_5h_pct": None,
        "api_seconds": _round((result.get("duration_api_ms") or 0) / 1000, 1),
        "lanes": len(report["lanes"]), "complete": report["complete"],
        "turns": result.get("num_turns"), "is_error": bool(result.get("is_error")),
        "permission_denials": len(result.get("permission_denials") or []),
        "unpriced_models": report["unpriced_models"],
    }


def codex_usage(report: dict) -> dict:
    """Usage of one Codex run, from `tokenomics codex`."""
    u = report["usage"]
    limits = report["limits"] or {}
    primary = limits.get("primary") or {}
    window = None
    if primary.get("window_minutes") == 300 and primary.get("used_percent_last") is not None:
        window = round(primary["used_percent_last"] - (primary.get("used_percent_first") or 0), 1)
    time_ = report["time"] or {}
    return {
        "tool_version": report["version"], "models": report["models"],
        "input": u["input"], "cache_write": u["cache_write_5m"] + u["cache_write_1h"],
        "cache_read": u["cache_read"], "output": u["output"], "reasoning": u["reasoning"],
        "context_peak": (report["context"] or {}).get("peak"),
        "cache_hit": _round(report["ratios"]["cache_hit"], 3),
        "cost_list_usd": _round(report["api"]["cost"], 4) if report["api"]["unit"] == "USD"
        else None,
        "cost_credits": _round(report["credits"]["cost"], 4) if report["credits"]["unit"]
        else None,
        "vendor_estimate_usd": None,
        "window_5h_pct": window,
        "api_seconds": _round(time_.get("generating"), 1),
        "lanes": 1, "complete": report["complete"],
        "turns": report["turns"], "is_error": bool(report["failed_turns"]),
        "permission_denials": None,
        "unpriced_models": sorted(set(report["api"]["unpriced_models"]
                                      + report["credits"]["unpriced_models"])),
    }


def reduce_claude_result(result: dict) -> dict:
    """The `claude -p` result with no answer text and no tool input."""
    keep = ("type", "subtype", "is_error", "duration_ms", "duration_api_ms", "num_turns",
            "session_id", "total_cost_usd", "usage", "modelUsage")
    out = {k: result[k] for k in keep if k in result}
    out["permission_denials"] = [{"tool_name": d.get("tool_name")}
                                 for d in result.get("permission_denials") or []]
    return out


def usage_from_record(record_dir: pathlib.Path, tool: str) -> dict:
    """Read the usage from the reduced files of a record (no model request)."""
    reduced = record_dir / "reduced"
    profiles = load_profiles()
    if tool == "claude":
        result = json.loads((reduced / "claude-result.json").read_text(encoding="utf-8"))
        main = reduced / "claude-session" / f"{result['session_id']}.jsonl"
        return claude_usage(result, summarize(load_session(main), profiles))
    if tool == "codex":
        thread, events = codex.load(str(reduced / "codex-events.jsonl"),
                                    root=reduced / "codex-sessions")
        return codex_usage(codex.summarize(thread, profiles, events))
    return empty_usage()


def empty_usage() -> dict:
    return {k: None for k in ("tool_version", "input", "cache_write", "cache_read", "output",
                              "reasoning", "context_peak", "cache_hit", "cost_list_usd",
                              "cost_credits", "vendor_estimate_usd", "window_5h_pct",
                              "api_seconds", "turns", "permission_denials")} | {
        "models": [], "lanes": 0, "complete": True, "is_error": False, "unpriced_models": []}


# --------------------------------------------------------------------------
# The results log
# --------------------------------------------------------------------------

def is_local_run(tool: str | None, model: str | None) -> bool:
    """A Claude Code or Codex run on a local Ollama model."""
    return tool in ("claude", "codex") and local_mod.is_local_model(model)


def build_row(record: dict, usage: dict) -> dict:
    """One row of the results log, from the record of a run and its usage."""
    meta, agent, checks = record["meta"], record["agent"], record["checks"]
    rules = checks["rules"]
    notes = []
    if agent.get("timed_out"):
        notes.append("timed out")
    elif agent.get("exit_code"):
        notes.append(f"agent exit {agent['exit_code']}")
    if usage.get("is_error"):
        notes.append("agent reported an error")
    if usage.get("permission_denials"):
        notes.append(f"{usage['permission_denials']} permission denials")
    if not usage.get("complete", True):
        notes.append("not all lanes finished")
    other = [m for m in usage.get("models", []) if m and m != meta["model"]]
    if other:
        notes.append("also used " + " ".join(other))
    if usage.get("lanes", 0) > 1:
        notes.append(f"{usage['lanes']} lanes")
    local = is_local_run(meta["tool"], meta["model"])
    if local:
        # Local tokens have no price. Claude Code still gives an
        # estimate for a model that it does not know (cloud prices), so the
        # row does not record it. Codex gives no price for it.
        notes.append("local model on Ollama: no token price")
    unpriced = [m for m in usage.get("unpriced_models", []) if not local_mod.is_local_model(m)]
    if unpriced:
        notes.append("no rate profile for " + " ".join(unpriced))
    row = {
        "run_id": meta["run_id"], "date": meta["date"], "rep": meta["rep"],
        "task": meta["task"], "tool": meta["tool"],
        "tool_version": usage.get("tool_version") or meta.get("tool_version") or "",
        "model": meta["model"], "effort": meta["effort"],
        "workload_tree": meta["workload_tree"], "prompt_sha": meta["prompt_sha"],
        "variant": meta.get("variant") or "",
        "config": meta.get("config") or LEGACY_CONFIG.get(meta["tool"], ""),
        "tests_pass": "yes" if all(c["ok"] for c in checks["acceptance"]) else "no",
        "rules_followed": "yes" if all(r["ok"] for r in rules) else "no",
        "manual_fixes": 0,
        "wall_seconds": round(agent["wall_seconds"], 1),
        "your_seconds": 0,
        "rules_broken": "; ".join(r["rule"] for r in rules if not r["ok"]),
        "notes": "; ".join(notes),
    }
    for key in ("input", "cache_write", "cache_read", "output", "reasoning", "context_peak",
                "cache_hit", "cost_list_usd", "cost_credits", "vendor_estimate_usd",
                "window_5h_pct", "api_seconds"):
        row[key] = usage.get(key)
    if local:
        row["vendor_estimate_usd"] = None
        if row["input"] is not None:
            row["cost_list_usd"] = 0
    return {c: ("" if row.get(c) is None else row[c]) for c in COLUMNS}


def read_log(path: str | os.PathLike) -> list[dict]:
    path = pathlib.Path(path)
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as fin:
        return list(csv.DictReader(fin))


def row_columns(row: dict) -> list[str]:
    """The columns of one row. A Claude Code or Codex row has a configuration,
    so it has all the columns. A reference row with no variant has the
    columns of the first log version (LEGACY_COLUMNS)."""
    if row.get("config"):
        return COLUMNS
    return VARIANT_COLUMNS if row.get("variant") else LEGACY_COLUMNS


def upgrade_log(path: pathlib.Path) -> None:
    """Add the new columns to an old log. Old rows get an empty variant (they
    used the workload as it is) and the legacy configuration of their tool."""
    rows = read_log(path)
    with open(path, "w", newline="", encoding="utf-8") as fout:
        writer = csv.DictWriter(fout, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in COLUMNS} | {"config": row_config(r)})


def append_row(path: pathlib.Path, row: dict) -> None:
    new = not path.is_file() or path.stat().st_size == 0
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = COLUMNS
    if not new:
        with open(path, newline="", encoding="utf-8") as fin:
            header = next(csv.reader(fin), [])
        if header in (LEGACY_COLUMNS, VARIANT_COLUMNS):
            if set(row_columns(row)) <= set(header):
                columns = header
            else:
                upgrade_log(path)
        elif header != COLUMNS:
            raise ValueError(f"{path} has other columns. Use another --log file.")
    with open(path, "a", newline="", encoding="utf-8") as fout:
        writer = csv.DictWriter(fout, fieldnames=columns, lineterminator="\n",
                                extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)


def add_rebuilt_row(path: pathlib.Path, row: dict) -> None:
    """Add a row made again from a record (`--rebuild --append`),
    for example a smoke run that was made outside a sweep. The row keeps the
    repetition number of its record. A run ID that the log has already is an
    error, so the same run never counts two times."""
    if any(r.get("run_id") == row["run_id"] for r in read_log(path)):
        raise ValueError(f"{path} already has the run {row['run_id']}")
    append_row(path, row)


def next_rep(log_rows: list[dict], task: str, tool: str, model: str, effort: str,
             variant: str = "") -> int:
    """The next repetition of a setup. Only the runs with the current
    configuration of the tool (and of a local model) count."""
    config = current_config(tool, task, model)
    same = [r for r in log_rows
            if r["task"] == task and sweep_mod.row_key(r) == (tool, model, effort, variant)
            and row_config(r) == config]
    return len(same) + 1


def row_csv(row: dict, header: bool = True) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=row_columns(row), lineterminator="\n",
                            extrasaction="ignore")
    if header:
        writer.writeheader()
    writer.writerow(row)
    return buf.getvalue()


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------

def plan_run(args) -> dict:
    task = load_task(args.task)
    tool = args.tool
    if tool in ("claude", "codex"):
        if not args.model or not args.effort:
            raise ValueError("give --model and --effort. Set both explicitly, "
                             "never use the default.")
        check_model_id(args.model)
        model, effort = args.model, args.effort
    else:
        model, effort = "-", "-"
    runs_dir = pathlib.Path(args.runs_dir).expanduser()
    log_path = pathlib.Path(args.log).expanduser()
    now = dt.datetime.now()
    run_id = args.run_id or f"{now:%Y%m%d-%H%M%S}-{task['id']}-{tool}"
    prompt_path = task["dir"] / task["prompt"]
    variant = None
    if getattr(args, "variant", None):
        variant = variant_mod.load_variant(args.variant)
        if variant.workload != task["workload"]:
            raise ValueError(f"variant {variant.name} changes {variant.workload}, but task "
                             f"{task['id']} uses {task['workload']}")
    rep = args.rep or next_rep(read_log(log_path), task["id"], tool, model, effort,
                               variant.name if variant else "")
    return {
        "task": task, "tool": tool, "model": model, "effort": effort, "rep": rep,
        "variant": variant,
        "run_id": run_id, "date": now.date().isoformat(), "ref": args.ref,
        "runs_dir": runs_dir, "copy_dir": runs_dir / run_id,
        "record_dir": runs_dir / f"{run_id}.record", "log": log_path,
        "prompt": variant_mod.pasted_prompt(prompt_path.read_text(encoding="utf-8"), variant),
        "prompt_path": prompt_path,
        "prompt_sha": file_sha(prompt_path),
        "config": current_config(tool, task, model),
        "workload_tree": workload_tree(task["workload"], args.ref),
        "timeout": args.timeout * 60,
    }


def repo_path(path: pathlib.Path) -> str:
    """A path inside a project root (see project_roots) as a path relative to
    that root; any other path as it is."""
    resolved = pathlib.Path(path).resolve()
    for root in project_roots(REPO_ROOT):
        try:
            return str(resolved.relative_to(root.resolve()))
        except ValueError:
            continue
    return str(path)


# The items of a run plan that `--dry-run --brief` shows: what the harness pins.
PINNED_ITEMS = ("Workload", "Prompt", "Variant", "Agent", "Backend", "Config", "AWS")


def plan_items(p: dict, brief: bool = False) -> list[tuple[str, str]]:
    """The items of a run plan as (name, text). A name of "" continues the item
    before it. The agent command is the list of its shown arguments. `brief`:
    the variant without its directory."""
    task = p["task"]
    home = str(pathlib.Path.home())
    lines = [("Task", f"{task['id']}: {task['title']}"),
             ("Workload", f"workloads/{task['workload']} at {p['ref']} "
                          f"(tree {p['workload_tree']})"),
             ("Prompt", f"{repo_path(p['prompt_path'])} "
                        f"(sha256 {p['prompt_sha']})"),
             ("Copy", str(p['copy_dir']).replace(home, '~')),
             ("Setup", task['setup'])]
    variant = p.get("variant")
    if variant:
        lines.append(("Variant", f"{variant.name}" + ("" if brief else
                                                       f": {repo_path(variant.dir)}")
                      + f" (sha256 {variant.sha})"))
        if variant.base:
            lines.append(("", f"extends {variant.base}"))
        compared = variant_mod.compare_files(
            variant, lambda rel: committed_file(task["workload"], p["ref"], rel))
        lines += [("", text) for text in variant_mod.describe_files(compared)]
        if variant.paste is not None:
            data = variant.paste.read_bytes()
            lines.append(("", f"pasted after the prompt: {variant.paste.name}: "
                          + variant_mod.fmt_size(*variant_mod.file_size(data))))
    if p["tool"] == "reference":
        lines.append(("Agent", f"none: git apply {task['reference']} (no model request)"))
    else:
        argv = agent_command(p["tool"], p["model"], p["effort"], p["prompt"], p["copy_dir"],
                             "<tmpdir>", task, variant)
        settings = config_file(p["tool"], task)
        replace = {str(settings.parent): repo_path(settings.parent),
                   str(codexlocal_mod.CATALOG_FILE): repo_path(codexlocal_mod.CATALOG_FILE),
                   str(p["copy_dir"]): "<copy>"}
        lines.append(("Agent", show_args(argv, p['prompt'], replace)))
        if p["tool"] == "codex" and local_mod.is_local_model(p["model"]):
            codex_settings = codexlocal_mod.load_codex_settings()
            lines.append(("Backend", f"Ollama at {codex_settings['base_url']} · CODEX_HOME="
                                     f"{codex_settings['codex_home']} (no ChatGPT sign-in)"))
            names = [settings, codexlocal_mod.CODEX_FILE, codexlocal_mod.CATALOG_FILE,
                     local_mod.ollama_mod.SETTINGS_FILE]
            lines.append(("Config", f"local: {', '.join(n.stem for n in names)} (.json) "
                                    f"· sha256 {p['config']}"))
        elif p["tool"] == "claude" and local_mod.is_local_model(p["model"]):
            claude = local_mod.load_claude_settings()
            lines.append(("Backend", f"Ollama at {claude['base_url']}: the variables of "
                                     f"{repo_path(local_mod.CLAUDE_FILE)}"))
            lines.append(("Config", f"local: {settings.name}, {local_mod.CLAUDE_FILE.name}, "
                                    f"{local_mod.ollama_mod.SETTINGS_FILE.name} "
                                    f"(sha256 {p['config']})"))
        else:
            lines.append(("Config", f"{repo_path(settings)} (sha256 {p['config']})"))
        if task.get("aws_profile"):
            lines.append(("AWS", f"AWS_PROFILE={task['aws_profile']} (AWS key variables "
                                 "removed)"))
    acc = task["acceptance"]
    lines.append(("Acceptance", (", ".join(acc["copy"]) + " then " if acc["copy"] else "")
                  + "; ".join(c["name"] for c in acc["commands"])))
    rules = task["rules"]
    lines.append(("Rules", "; ".join([c["name"] for c in rules["commands"]]
                                     + [rules["static"]])))
    lines.append(("Log", f"{repo_path(p['log'])} (repetition {p['rep']})"))
    return lines


def _item_line(name: str, text) -> str:
    return f"  {name:<12}{' '.join(text) if isinstance(text, list) else text}"


def describe(p: dict) -> str:
    return "\n".join([f"Run {p['run_id']}"] + [_item_line(n, t) for n, t in plan_items(p)])


def describe_brief(p: dict, width: int = 100) -> str:
    """`--dry-run --brief` for one run: only the pinned items (the workload, the
    prompt, the variant, the agent command, and the configuration), with the
    agent command wrapped at `width` columns. No run ID, copy, or log, so the
    view is the same in each dry run. The variant has no directory."""
    lines, keep = [], False
    for name, text in plan_items(p, brief=True):
        if name:
            keep = name in PINNED_ITEMS
        if not keep:
            continue
        if isinstance(text, list):
            lines += wrap_command(text, f"  {name:<12}", 14, width)
        else:
            lines.append(_item_line(name, text))
    return "\n".join(lines)


def execute(p: dict, log=print) -> dict:
    task, tool = p["task"], p["tool"]
    copy_dir, record_dir = p["copy_dir"], p["record_dir"]
    if copy_dir.exists() or record_dir.exists():
        raise FileExistsError(f"{copy_dir} already exists. Use another --run-id.")
    if tool != "reference":
        refuse_api_keys()
    local = is_local_run(tool, p["model"])
    if local:
        local_mod.check_server(p["model"])
    if local and tool == "codex":
        if p["model"] not in codexlocal_mod.catalog_models():
            raise ValueError(f"model {p['model']} is not in "
                             f"{repo_path(codexlocal_mod.CATALOG_FILE)}")
        codexlocal_mod.ensure_home()
    env = agent_env(p["runs_dir"] / ".cache" / "terraform-plugins", task.get("aws_profile"),
                    p["model"] if tool == "claude" else None,
                    p["model"] if tool == "codex" else None)

    variant = p.get("variant")
    if variant and variant.files:
        log(f"1. Copy the workload, then put the variant {variant.name} over it: "
            + ", ".join(variant.files))
    elif variant:
        log(f"1. Copy the workload (the variant {variant.name} puts no file in the copy)")
    else:
        log("1. Copy the workload")
    copy_workload(task["workload"], p["ref"], copy_dir)
    if variant:
        variant_mod.apply_variant(variant, copy_dir)
    if not p.get("copy_only"):
        record_dir.mkdir(parents=True)
    log("2. Prepare the copy: " + setup_copy(task, copy_dir, p["runs_dir"], env, log))
    start_repo(copy_dir)
    if p.get("copy_only"):
        return {}
    start = {rel: (copy_dir / rel).read_text(encoding="utf-8")
             for rel in ("app/main.py",) if (copy_dir / rel).is_file()}

    log(f"3. Run the agent ({tool})")
    agent = {"exit_code": 0, "timed_out": False}
    began = time.monotonic()
    if tool == "reference":
        proc = git(["apply", str(task["dir"] / task["reference"])], copy_dir, check=False)
        agent["exit_code"] = proc.returncode
        stdout = ""
    else:
        argv = agent_command(tool, p["model"], p["effort"], p["prompt"], copy_dir,
                             task=task, variant=variant)
        try:
            proc = subprocess.run(argv, cwd=copy_dir, env=env, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=p["timeout"])
            agent["exit_code"], stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as error:
            agent["timed_out"], agent["exit_code"] = True, -1
            stdout = (error.stdout or b"").decode() if isinstance(error.stdout, bytes) \
                else (error.stdout or "")
            stderr = ""
        (record_dir / "agent-stderr.log").write_text(stderr, encoding="utf-8")
    agent["wall_seconds"] = time.monotonic() - began
    out_name = {"claude": "claude-result.json", "codex": "codex-events.jsonl"}.get(tool)
    if out_name:
        (record_dir / out_name).write_text(stdout, encoding="utf-8")

    hook_logs = save_hook_logs(copy_dir, record_dir)
    answer = record_dir / "answer.md"
    answer.write_text(final_answer(tool, stdout), encoding="utf-8")

    log("4. Check the result: hidden acceptance tests, then the house rules")
    changed = commit_result(copy_dir)
    save_patch(copy_dir, record_dir)
    check_env = agent_env(p["runs_dir"] / ".cache" / "terraform-plugins")
    names = {"task": str(task["dir"]), "answer": str(answer)}
    copy_hidden(task, task["acceptance"]["copy"], copy_dir)
    acceptance = run_commands(task["acceptance"]["commands"], copy_dir, check_env,
                              record_dir / "checks.log", names)
    copy_hidden(task, task["rules"]["copy"], copy_dir)
    rules = run_commands(task["rules"]["commands"], copy_dir, check_env,
                         record_dir / "checks.log", names)
    rules += STATIC_RULES[task["rules"]["static"]](copy_dir, start, changed)

    log("5. Read the usage and make a reduced copy")
    usage, reduced_files = collect_usage(tool, copy_dir, record_dir, stdout,
                                         local and tool == "codex")
    if local:
        save_local_meters(copy_dir, record_dir, stdout, log, tool)

    record = {
        "meta": {"run_id": p["run_id"], "date": p["date"], "rep": p["rep"],
                 "task": task["id"], "tool": tool, "model": p["model"], "effort": p["effort"],
                 "workload": task["workload"], "workload_tree": p["workload_tree"],
                 "prompt_sha": p["prompt_sha"]}
        | ({"config": p["config"]} if p["config"] else {})
        | ({"variant": variant.name, "variant_sha": variant.sha} if variant else {}),
        "agent": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in agent.items()},
        "changed": changed,
        "checks": {"acceptance": acceptance, "rules": rules},
    } | ({"hook_logs": hook_logs} if hook_logs else {})
    row = build_row(record, usage)
    record["row"] = row
    (record_dir / "run.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if reduced_files:
        leftovers = leftover_strings(reduced_files) if tool == "claude" \
            else codex.leftover_strings(reduced_files)
        (record_dir / "leftover-strings.txt").write_text("\n".join(leftovers) + "\n",
                                                          encoding="utf-8")
    log(f"6. Add one row to {repo_path(p['log'])}")
    append_row(p["log"], row)
    return record


def final_answer(tool: str, stdout: str) -> str:
    """The final answer text of the agent: the `result` of `claude -p`, or the
    last agent message of `codex exec --json`. "" when there is none."""
    if tool == "claude":
        try:
            return str(json.loads(stdout).get("result") or "")
        except (json.JSONDecodeError, AttributeError):
            return ""
    if tool == "codex":
        text = ""
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text") or text
        return text
    return ""


def collect_usage(tool: str, copy_dir: pathlib.Path, record_dir: pathlib.Path,
                  stdout: str, local_codex: bool = False) -> tuple[dict, list]:
    """The usage of a run and its reduced files. A Codex run on a local model
    has its session file in the local Codex home."""
    reduced = record_dir / "reduced"
    profiles = load_profiles()
    if tool == "claude":
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            return empty_usage() | {"is_error": True}, []
        main = find_session(result["session_id"], copy_dir)
        usage = claude_usage(result, summarize(load_session(main), profiles))
        reduced.mkdir(parents=True, exist_ok=True)
        (reduced / "claude-result.json").write_text(
            json.dumps(reduce_claude_result(result), indent=2) + "\n", encoding="utf-8")
        files = reduce_session(main, reduced / "claude-session")
        return usage, files
    if tool == "codex":
        events_path = record_dir / "codex-events.jsonl"
        root = codexlocal_mod.local_home() / "sessions" if local_codex else None
        thread, events = codex.load(str(events_path), root=root)
        usage = codex_usage(codex.summarize(thread, profiles, events))
        files = [codex.reduce_file(events_path, reduced)]
        if events is not None and events.thread_id:
            session_file = codex.find_thread(events.thread_id, root=root)
            files.append(codex.reduce_file(session_file, reduced / "codex-sessions"))
        return usage, files
    return empty_usage(), []


def save_local_meters(copy_dir: pathlib.Path, record_dir: pathlib.Path, stdout: str,
                      log=print, tool: str = "claude") -> dict | None:
    """A run on a local model (Claude Code or Codex): save the local
    meters of the session from the Ollama server log to
    <record>/ollama-meters.json (times and token counts only).
    Skip with a message when the log has no request lines (for
    example the llama.cpp runner)."""
    try:
        log_text = local_mod.ollama_mod.read_log_files()
        if tool == "codex":
            records = []
            for line in stdout.splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            thread_id = codex.load_exec(records).thread_id
            if not thread_id:
                raise ValueError("no thread ID in the codex exec events")
            meters = codexlocal_mod.session_meters(codexlocal_mod.find_thread(thread_id),
                                                   log_text)
        else:
            session_id = json.loads(stdout)["session_id"]
            meters = local_mod.session_meters(find_session(session_id, copy_dir), log_text)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        log(f"   No local meters: {error}")
        return None
    (record_dir / "ollama-meters.json").write_text(json.dumps(meters, indent=2) + "\n",
                                                   encoding="utf-8")
    return meters


def describe_record(record: dict) -> str:
    m = record["meta"]
    return (f"Run {m['run_id']} · repetition {m['rep']} · {m['date']}\n"
            f"  {m['task']} · {m['tool']} {record['row']['tool_version']} · model {m['model']}"
            f" · effort {m['effort']} · workload tree {m['workload_tree']}"
            f" · prompt {m['prompt_sha']}"
            + (f" · config {row_config(record['row'])}" if row_config(record["row"]) else "")
            + (f"\n  variant {m['variant']} (sha256 {m.get('variant_sha', '-')})"
               if m.get("variant") else ""))


def print_result(record: dict, record_dir: pathlib.Path) -> None:
    row = record["row"]
    home = str(pathlib.Path.home())
    print(f"Tests pass: {row['tests_pass']} · rules followed: {row['rules_followed']}"
          + (f" (broken: {row['rules_broken']})" if row["rules_broken"] else ""))
    for group in ("acceptance", "rules"):
        for c in record["checks"][group]:
            mark = "ok  " if c["ok"] else "FAIL"
            extra = f": {', '.join(c['failed'][:4])}" if c["failed"] else ""
            print(f"  {mark} {group:10} {c['rule']}{extra}")
    if row["input"] != "":
        print(f"Tokens: input {row['input']}, cache write {row['cache_write']}, cache read "
              f"{row['cache_read']}, output {row['output']}"
              + (f" (reasoning {row['reasoning']})" if row["reasoning"] != "" else ""))
        cost = [f"${row['cost_list_usd']:.4f} at list price" if row["cost_list_usd"] != ""
                else "",
                f"{row['cost_credits']:.4f} credits" if row["cost_credits"] != "" else ""]
        if is_local_run(row.get("tool"), row.get("model")):
            cost = ["no token price (local model). The meter is time."]
        print("Cost: " + " and ".join(c for c in cost if c))
        if row["window_5h_pct"] != "":
            print(f"Plan: {row['window_5h_pct']} percentage points of the 5-hour window "
                  "(after request 1 to the end)")
    print(f"Time: {row['wall_seconds']} s wall clock"
          + (f", {row['api_seconds']} s API" if row["api_seconds"] != "" else ""))
    if row["notes"]:
        print(f"Notes: {row['notes']}")
    for name, count in (record.get("hook_logs") or {}).items():
        print(f"Hook log: {name}, {count} line{'' if count == 1 else 's'} "
              f"(copied to hook-logs/)")
    copy_dir = record_dir.with_name(record_dir.name[: -len(".record")])
    if copy_dir.is_dir():
        print(f"Copy: {str(copy_dir).replace(home, '~')}")
    note = ("  (reduced/ has no text; read leftover-strings.txt before you commit it)"
            if (record_dir / "leftover-strings.txt").is_file() else "")
    print(f"Record: {str(record_dir).replace(home, '~')}{note}")


def print_result_brief(record: dict) -> None:
    """The short view of the result of a run (--brief): the run, the checks,
    the tokens, the cost, and the time. Only the checks that failed get a
    line. At most about 15 lines and 100 columns."""
    row, m = record["row"], record["meta"]
    print(f"Run {m['run_id']} · repetition {m['rep']}"
          + (f" · variant {m['variant']}" if m.get("variant") else ""))
    print(f"{m['task']} · {m['tool']} · {m['model']} · {m['effort']}")
    checks = record["checks"]["acceptance"] + record["checks"]["rules"]
    passed = sum(1 for c in checks if c["ok"])
    print(f"Tests pass: {row['tests_pass']} · rules followed: {row['rules_followed']} · "
          f"{passed} of {len(checks)} checks ok")
    for group in ("acceptance", "rules"):
        for c in record["checks"][group]:
            if not c["ok"]:
                print(f"  FAIL {group:10} {c['rule'][:60]}")
    if row["input"] != "":
        print(f"Tokens: input {row['input']}, cache write {row['cache_write']}, cache read "
              f"{row['cache_read']}, output {row['output']}")
        cost = [f"${row['cost_list_usd']:.4f} at list price" if row["cost_list_usd"] != ""
                else "",
                f"{row['cost_credits']:.4f} credits" if row["cost_credits"] != "" else ""]
        if is_local_run(row.get("tool"), row.get("model")):
            cost = ["no token price (local model). The meter is time."]
        print("Cost: " + " and ".join(c for c in cost if c))
    print(f"Time: {row['wall_seconds']} s wall clock"
          + (f", {row['api_seconds']} s API" if row["api_seconds"] != "" else ""))


def print_answer(record: dict, record_dir: pathlib.Path) -> None:
    """The short view of a run (--answer): the final answer of the
    agent, then one line with the check, the cost, and the time."""
    path = record_dir / "answer.md"
    text = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    print(text or "(no answer)")
    row = record["row"]
    check = record["checks"]["acceptance"][0]["rule"] if record["checks"]["acceptance"] \
        else "acceptance"
    cost = f"${row['cost_list_usd']:.4f} at list price" if row["cost_list_usd"] != "" else "-"
    if is_local_run(row.get("tool"), row.get("model")):
        cost = "local model, no token price"
    print(f"\n{check}: {row['tests_pass']} · {cost} · {row['wall_seconds']} s")


# --------------------------------------------------------------------------
# /context in a prepared copy
# --------------------------------------------------------------------------

def claude_version(env: dict | None = None) -> str:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                             env=env, stdin=subprocess.DEVNULL, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.split()[0] if out.split() else ""


def capture_context(p: dict, log=print, run=subprocess.run) -> dict:
    """Make the copy of one setup (as --copy-only), then run `/context` in it
    with the same `claude -p` arguments as a run. No model request: Claude
    Code answers /context itself. Return the parsed result (extensions.py)."""
    task, variant = p["task"], p.get("variant")
    if p["tool"] != "claude":
        raise ValueError("--context needs --tool claude")
    if not task.get("skills"):
        raise ValueError(f"task {task['id']} disables slash commands, so /context cannot run. "
                         "Use a task with \"skills\": true.")
    refuse_api_keys()
    execute(dict(p, copy_only=True), log)
    env = agent_env(p["runs_dir"] / ".cache" / "terraform-plugins", task.get("aws_profile"),
                    p["model"])
    argv = agent_command("claude", p["model"], p["effort"], "/context", p["copy_dir"],
                         task=task, variant=variant)
    log("3. Run /context in the copy (no model request)")
    proc = run(argv, cwd=p["copy_dir"], env=env, capture_output=True, text=True,
               stdin=subprocess.DEVNULL, timeout=300)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"/context gave no JSON (exit {proc.returncode}): "
                         f"{(proc.stderr or proc.stdout)[:300]}") from error
    if result.get("num_turns") or result.get("total_cost_usd"):
        raise ValueError("/context made a model request. Check the Claude Code version.")
    parsed = extensions_mod.parse_context(result.get("result") or "")
    return {"variant": variant.name if variant else "", "task": task["id"],
            "model": p["model"], "claude_version": claude_version(env),
            "date": p["date"]} | parsed


def run_contexts(args) -> int:
    """`run-baseline --sweep F --context`: /context for each setup of the sweep.
    Write <context dir>/<variant>.json (the sweep file names the directory)."""
    sweep, tasks, setups, _ = sweep_setup(args)
    if not sweep:
        raise ValueError("--context needs --sweep (the sweep file names the context directory)")
    out_dir = extensions_mod.context_dir(sweep.path)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    for task_id in tasks:
        for s in setups:
            name = s.variant or "none"
            run_args = argparse.Namespace(**{**vars(args), "task": task_id, "tool": s.tool,
                                             "model": s.model, "effort": s.effort, "rep": 1,
                                             "variant": s.variant_dir or None,
                                             "run_id": f"{now:%Y%m%d-%H%M%S}-{task_id}-"
                                                       f"context-{name}"})
            print(f"\n== /context: {task_id} · {s.label or name}")
            p = plan_run(run_args)
            data = capture_context(p)
            path = out_dir / f"{name}.json"
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            a = extensions_mod.always_on(data)
            print(f"Always-on: {a['tokens']} tokens ({extensions_mod.up_front_text(a['parts'])})"
                  f" · wrote {repo_path(path)}")
    return 0


# --------------------------------------------------------------------------
# Sweeps
# --------------------------------------------------------------------------

def is_sweep(args) -> bool:
    lists = [args.task or "", args.model or "", args.effort or ""]
    return bool(args.sweep or args.reps is not None or args.task == "all"
                or any("," in text for text in lists))


def sweep_setup(args) -> tuple[object, list[str], list, int | None]:
    """The sweep file (or None), the tasks, the setups, and the target repetitions."""
    if args.run_id or args.rep or args.up_to is not None:
        raise ValueError("a sweep chooses the run IDs and the repetitions. Do not give "
                         "--run-id, --rep, or --up-to.")
    if args.copy_only:
        raise ValueError("--copy-only makes one copy. Give one task, one model, and one "
                         "effort level.")
    if args.reps is not None and args.reps < 1:
        raise ValueError("--reps must be 1 or more")
    models = sweep_mod.split_list(args.model)
    efforts = sweep_mod.split_list(args.effort)
    task_names = sweep_mod.split_list(args.task)
    sweep = sweep_mod.load_sweep(args.sweep) if args.sweep else None
    if sweep:
        if args.variant:
            raise ValueError("with --sweep, the sweep file gives the variants. Do not give "
                             "--variant.")
        setups = sweep_mod.filter_setups(sweep.setups, args.tool, models, efforts)
        if not setups:
            raise ValueError(f"no setup in {args.sweep} matches --tool, --model, and --effort")
        tasks = list(sweep.tasks) or list_tasks()
        reps = args.reps if args.reps is not None else sweep.repetitions
    else:
        if args.tool not in sweep_mod.AGENT_TOOLS:
            raise ValueError("a sweep needs --tool claude or --tool codex")
        if not models or not efforts:
            raise ValueError("give --model and --effort. Set both explicitly, "
                             "never use the default.")
        variant = variant_mod.load_variant(args.variant) if args.variant else None
        setups = sweep_mod.cross_setups(args.tool, models, efforts,
                                        variant.name if variant else "",
                                        str(variant.dir) if variant else "")
        if not task_names:
            raise ValueError("give the task IDs, comma-separated, or all")
        tasks = list_tasks()
        reps = args.reps
    if task_names and task_names != ["all"]:
        tasks = [match_task(name) for name in task_names]
    for setup in setups:
        check_model_id(setup.model)
    return sweep, tasks, setups, reps


def print_plan(plan, sweep, reps, log_path, brief: bool = False) -> None:
    """The plan of a sweep. `brief` (--dry-run --brief): no lines about the sweep
    file, the target, and the log path before the table."""
    from .cli import table  # the table format of the tokenomics command

    if not brief:
        if sweep:
            print(f"Sweep {repo_path(pathlib.Path(sweep.path))}: {sweep.title}")
        target = (f"{reps} run{'' if reps == 1 else 's'} of each task and setup" if reps
                  else "one more run of each task and setup")
        print(f"Target: {target} · log {repo_path(log_path)}\n")
    rows = []
    variants = any(line.setup.variant for line in plan)
    for line in plan:
        s = line.setup
        label = s.label + (" (baseline)" if s.baseline else "")
        rows.append([line.task, s.tool, s.model, s.effort]
                    + ([s.variant or "-"] if variants else [])
                    + [label, str(line.in_log), str(line.to_run)])
    headers = ["Task", "Tool", "Model", "Effort"] + (["Variant"] if variants else []) \
        + ["Setup", "In log", "To run"]
    print(table(headers, rows, right={len(headers) - 2, len(headers) - 1}))
    total = sum(line.to_run for line in plan)
    by_tool = {}
    for line in plan:
        by_tool[line.setup.tool] = by_tool.get(line.setup.tool, 0) + line.to_run
    split = ", ".join(f"{tool} {n}" for tool, n in by_tool.items())
    print(f"\nRuns to make: {total} ({split}).")


def run_sweep(args) -> int:
    sweep, tasks, setups, reps = sweep_setup(args)
    log_path = pathlib.Path(args.log).expanduser()
    plan = sweep_mod.plan_sweep(tasks, setups, read_log(log_path), reps)
    order = sweep_mod.run_order(plan)
    if args.json and args.dry_run:
        print(json.dumps([{"task": p.task, "tool": p.setup.tool, "model": p.setup.model,
                           "effort": p.setup.effort, "variant": p.setup.variant,
                           "label": p.setup.label,
                           "baseline": p.setup.baseline, "in_log": p.in_log,
                           "to_run": p.to_run} for p in plan], indent=2))
        return 0
    print_plan(plan, sweep, reps, log_path, brief=args.brief and args.dry_run)
    if args.dry_run:
        print("Dry run: no copy and no model request.")
        return 0
    if not order:
        print("Nothing to run.")
        return 0
    refuse_api_keys()
    done, failed = [], []
    for number, line in enumerate(order, 1):
        s = line.setup
        run_args = argparse.Namespace(**{**vars(args), "task": line.task, "tool": s.tool,
                                         "model": s.model, "effort": s.effort, "rep": None,
                                         "run_id": None, "variant": s.variant_dir or None})
        print(f"\n== Run {number} of {len(order)}: {line.task} · {s.tool} · {s.model} · "
              f"{s.effort}" + (f" · variant {s.variant}" if s.variant else "") + "\n")
        try:
            p = plan_run(run_args)
            if args.brief:
                record = execute(p, log=lambda *_: None)
            else:
                print(describe(p) + "\n")
                record = execute(p)
        except (ValueError, KeyError, FileNotFoundError, FileExistsError,
                json.JSONDecodeError, subprocess.CalledProcessError) as error:
            print(f"run-baseline: error in this run: {error}", file=sys.stderr)
            failed.append((line, str(error)))
            continue
        if args.brief:
            print_result_brief(record)
        else:
            print()
            print_result(record, p["record_dir"])
        done.append(record["row"])
    print(f"\n== Sweep finished: {len(done)} run{'' if len(done) == 1 else 's'} added to "
          f"{repo_path(log_path)}")
    for row in done:
        cost = f"${row['cost_list_usd']:.4f}" if row["cost_list_usd"] != "" else "-"
        print(f"  {row['run_id']}  {row['model']} · {row['effort']}"
              + (f" · {row['variant']}" if row.get("variant") else "") + f" · rep {row['rep']}  "
              f"tests {row['tests_pass']} · rules {row['rules_followed']}  {cost}  "
              f"{row['wall_seconds']} s" + (f"  ({row['notes']})" if row["notes"] else ""))
    for line, error in failed:
        print(f"  FAILED {line.task} · {line.setup.model} · {line.setup.effort}"
              + (f" · {line.setup.variant}" if line.setup.variant else "") + f": {error}")
    if any("agent exit" in row["notes"] for row in done):
        print("A run with 'agent exit' in the notes can mean that the plan does not include "
              "the model. Read agent-stderr.log in the record.")
    return 1 if failed else 0


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Sequences
# --------------------------------------------------------------------------

def plan_sequence(args) -> dict:
    seq = sequence_mod.load_sequence(args.sequence, args.idle)
    check_model_id(seq["model"])
    runs_dir = pathlib.Path(args.runs_dir).expanduser()
    now = dt.datetime.now()
    run_id = args.run_id or f"{now:%Y%m%d-%H%M%S}-{seq['name']}"
    return seq | {"run_id": run_id, "date": now.date().isoformat(), "ref": args.ref,
                  "runs_dir": runs_dir, "copy_dir": runs_dir / run_id,
                  "record_dir": runs_dir / f"{run_id}.record",
                  "sequence_sha": file_sha(seq["path"]),
                  "workload_tree": workload_tree(seq["workload"], args.ref),
                  "timeout": args.timeout * 60}


def describe_sequence(q: dict) -> str:
    home = str(pathlib.Path.home())
    lines = [f"Sequence {q['run_id']}: {q['title']}",
             f"  File        {repo_path(q['path'])} (sha256 {q['sequence_sha']})",
             f"  Workload    workloads/{q['workload']} at {q['ref']} "
             f"(tree {q['workload_tree']})"]
    variant = q["variant"]
    if variant:
        lines.append(f"  Variant     {variant.name}: {repo_path(variant.dir)} "
                     f"(sha256 {variant.sha})")
        if variant.base:
            lines.append(f"              extends {variant.base}")
        compared = variant_mod.compare_files(
            variant, lambda rel: committed_file(q["workload"], q["ref"], rel))
        lines += [f"              {text}" for text in variant_mod.describe_files(compared)]
    lines += [f"  Copy        {str(q['copy_dir']).replace(home, '~')}",
              f"  Setup       {q['setup']}"]
    argv = agent_command(q["tool"], q["model"], q["effort"], "<prompt>", q["copy_dir"])
    replace = {str(CONFIG_DIR): "baseline/config", str(q["copy_dir"]): "<copy>"}
    lines.append(f"  Agent       {show_command(argv, '<prompt>', replace)}")
    lines.append("  Steps       (one session: step 1 starts it, each later step adds "
                 "--resume <session ID>)")
    rows = []
    for step in q["steps"]:
        idle = ("starts the session" if step["number"] == 1 else
                "no idle time" if not step["idle_seconds"] else
                f"idle {sequence_mod.fmt_seconds(step['idle_seconds'])} first")
        rows.append((str(step["number"]), step["label"],
                     f"{step['prompt_file']} (sha256 {file_sha(step['prompt_path'])})", idle))
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        lines.append("              " + "  ".join(r[i].ljust(widths[i]) for i in range(3))
                     + "  " + r[3])
    lines.append(f"  Idle time   {sequence_mod.fmt_seconds(sequence_mod.total_idle(q))} in "
                 "total. The harness sends no request while it waits.")
    lines.append("  Log         no row: a sequence is a cache experiment, not a baseline "
                 "attempt")
    return "\n".join(lines)


def execute_sequence(q: dict, log=print, sleep=time.sleep, run=subprocess.run) -> dict:
    copy_dir, record_dir = q["copy_dir"], q["record_dir"]
    if copy_dir.exists() or record_dir.exists():
        raise FileExistsError(f"{copy_dir} already exists. Use another --run-id.")
    refuse_api_keys()
    env = agent_env(q["runs_dir"] / ".cache" / "terraform-plugins", None, q["model"])
    variant = q["variant"]
    log("1. Copy the workload" + (f", then put the variant {variant.name} over it: "
                                  + ", ".join(variant.files) if variant else ""))
    copy_workload(q["workload"], q["ref"], copy_dir)
    if variant:
        variant_mod.apply_variant(variant, copy_dir)
    record_dir.mkdir(parents=True)
    task = {"id": q["name"], "workload": q["workload"], "setup": q["setup"]}
    log("2. Prepare the copy: " + setup_copy(task, copy_dir, q["runs_dir"], env, log))
    start_repo(copy_dir)

    log(f"3. Run the steps in one session ({q['tool']})")
    try:
        out = sequence_mod.run_steps(
            q, lambda prompt: agent_command(q["tool"], q["model"], q["effort"], prompt,
                                            copy_dir),
            copy_dir, env, q["timeout"], log=log, sleep=sleep, run=run)
        error = None
    except RuntimeError as failure:
        out, error = {"session_id": None, "steps": []}, str(failure)
    steps = []
    reduced = record_dir / "reduced"
    reduced.mkdir()
    for r in out["steps"]:
        n = r["step"]["number"]
        (record_dir / f"step-{n}-result.json").write_text(
            json.dumps(r["result"], indent=2) + "\n", encoding="utf-8")
        (record_dir / f"step-{n}-stderr.log").write_text(r["stderr"], encoding="utf-8")
        (reduced / f"step-{n}-result.json").write_text(
            json.dumps(reduce_claude_result(r["result"]), indent=2) + "\n", encoding="utf-8")
        steps.append({"number": n, "label": r["step"]["label"],
                      "prompt": r["step"]["prompt_file"],
                      "prompt_sha": file_sha(r["step"]["prompt_path"]),
                      "idle_seconds": r["step"]["idle_seconds"],
                      "idle_actual_seconds": _round(r["idle_actual"], 1),
                      "started": sequence_mod.iso(r["started"]),
                      "wall_seconds": round(r["ended"] - r["started"], 1),
                      "exit_code": r["exit_code"], "timed_out": r["timed_out"],
                      "num_turns": r["result"].get("num_turns"),
                      "api_seconds": _round((r["result"].get("duration_api_ms") or 0) / 1000, 1),
                      "vendor_estimate_usd": _round(r["result"].get("total_cost_usd"), 4)})

    session_id = out["session_id"]
    files = []
    if session_id:
        log("4. Make a reduced copy of the session (every lane, no text)")
        main = find_session(session_id, copy_dir)
        files = reduce_session(main, reduced / "claude-session")
        (record_dir / "leftover-strings.txt").write_text(
            "\n".join(leftover_strings(files)) + "\n", encoding="utf-8")
    record = {
        "meta": {"run_id": q["run_id"], "date": q["date"], "title": q["title"],
                 "sequence": repo_path(q["path"]), "sequence_sha": q["sequence_sha"],
                 "tool": q["tool"], "model": q["model"], "effort": q["effort"],
                 "workload": q["workload"], "workload_tree": q["workload_tree"]}
        | ({"variant": variant.name, "variant_sha": variant.sha} if variant else {}),
        "session_id": session_id, "error": error, "steps": steps,
    }
    (record_dir / "sequence.json").write_text(json.dumps(record, indent=2) + "\n",
                                              encoding="utf-8")
    (reduced / "sequence.json").write_text(json.dumps(record, indent=2) + "\n",
                                           encoding="utf-8")
    return record


def print_sequence_result(record: dict, copy_dir: pathlib.Path,
                          record_dir: pathlib.Path) -> None:
    home = str(pathlib.Path.home())
    for step in record["steps"]:
        idle = ("" if step["idle_actual_seconds"] is None else
                f" · {sequence_mod.fmt_seconds(step['idle_actual_seconds'])} after the step "
                "before")
        print(f"  Step {step['number']}: {step['label']} · {step['wall_seconds']} s"
              f" · {step['num_turns']} turns{idle}")
    if record["error"]:
        print(f"Stopped: {record['error']}")
    if record["session_id"]:
        print(f"Session {record['session_id']}. Read every request and its cache result:")
        print(f"  tokenomics session {record['session_id']} --project "
              f"{str(copy_dir).replace(home, '~')} --requests")
    print(f"Copy: {str(copy_dir).replace(home, '~')}")
    print(f"Record: {str(record_dir).replace(home, '~')}  (reduced/ has no text; read "
          "leftover-strings.txt before you commit it)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run-baseline",
        description="Run one baseline task in a clean copy of a workload, check it, and add "
                    "one row to the results log.")
    parser.add_argument("task", nargs="?", help="task ID (see --list). For a sweep: task IDs, "
                        "comma-separated, or all")
    parser.add_argument("--list", action="store_true", help="list the tasks")
    parser.add_argument("--tool", choices=TOOLS, help="claude, codex, or reference (apply "
                        "the reference solution: no model request)")
    parser.add_argument("--model", help="full model ID, for example claude-sonnet-5-5. "
                        "Comma-separated IDs make a sweep.")
    parser.add_argument("--effort", help="effort level, for example medium. Comma-separated "
                        "levels make a sweep.")
    parser.add_argument("--reps", type=int, metavar="N", help="sweep: make runs until the log "
                        "has N runs of each task and setup (default: one more run of each, "
                        "or the repetitions of the sweep file)")
    parser.add_argument("--sweep", metavar="FILE", help="sweep file: the setups, the tasks, "
                        "and the repetitions. --tool, --model, and --effort then select "
                        "setups from the file.")
    parser.add_argument("--variant", metavar="DIR", help="variant directory: "
                        "put its files over the copy before the start commit, for example "
                        "a bloated AGENTS.md. The log records its name. With --diff: the "
                        "variant name, or none.")
    parser.add_argument("--copy-only", action="store_true", help="make and prepare the "
                        "copy, then stop: no agent, no checks, no log row. For a look at "
                        "the start state, for example with /context.")
    parser.add_argument("--context", action="store_true", help="with --sweep: "
                        "make a copy of each setup and run /context in it, with the same "
                        "claude -p arguments as a run. No model request. Writes "
                        "<variant>.json to the context directory of the sweep file.")
    parser.add_argument("--sequence", metavar="FILE", help="sequence file: "
                        "run several prompts in one Claude Code session, with idle time "
                        "between them (claude -p, then claude -p --resume). For cache "
                        "experiments. Adds no log row.")
    parser.add_argument("--idle", type=float, metavar="SECONDS", help="with --sequence: the "
                        "idle time before the last step, in place of the time in the file")
    parser.add_argument("--rep", type=int, help="repetition number (default: the next one "
                        "for this task, tool, model, and effort in the log)")
    parser.add_argument("--up-to", type=int, metavar="N", help="skip the run when the log "
                        "already has N or more runs of this task, tool, model, and effort. "
                        "A loop with --up-to fills only the missing repetitions, so you can "
                        "run it again.")
    parser.add_argument("--runs-dir", default=os.environ.get("TOKENOMICS_RUNS",
                                                             "~/tokenomics-runs"),
                        help="where to make the copies (default: $TOKENOMICS_RUNS or "
                             "~/tokenomics-runs)")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="results log CSV "
                        "(default: results/results-log.csv)")
    parser.add_argument("--ref", default="HEAD", help="git commit of the workload "
                        "(default: HEAD)")
    parser.add_argument("--run-id", help="run ID (default: date, time, task, and tool)")
    parser.add_argument("--timeout", type=float, default=30, help="minutes before the "
                        "harness stops the agent (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="show the plan only: no "
                        "copy, no model request")
    parser.add_argument("--show", metavar="RECORD_DIR", help="show the result of a "
                        "finished run again, from its record")
    parser.add_argument("--rebuild", metavar="RECORD_DIR", help="make the row again from a "
                        "run record and its reduced transcripts (no model request)")
    parser.add_argument("--append", action="store_true", help="with --rebuild: add the "
                        "row to the results log (--log), for example a smoke run. "
                        "Refuses a run ID that the log already has.")
    parser.add_argument("--diff", nargs="?", const="", metavar="RUN", help="show what the "
                        "agent changed in a run. RUN is a run ID, a run directory, or a task "
                        "(app, infra, or a task ID: the latest run of that task in the log, "
                        "filtered by --tool, --model, --effort, and --rep). With no RUN, "
                        "give the task.")
    parser.add_argument("--stat", action="store_true", help="with --diff: show only the "
                        "changed files and the number of changed lines")
    parser.add_argument("--answer", action="store_true", help="one run, or --show: print only "
                        "the final answer of the agent and one line with the check, the cost, "
                        "and the time")
    parser.add_argument("--brief", action="store_true", help="one run, a sweep, or --show: "
                        "a short result: the run, the checks (a line only for a failed "
                        "check), the tokens, the cost, and the time. No progress lines. "
                        "With --dry-run: one run shows only the pinned items, with the "
                        "agent command wrapped at 100 columns; a sweep shows the plan "
                        "table with no lines about the sweep file and the log before it.")
    parser.add_argument("--json", action="store_true", help="print JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.list:
            for task_id in list_tasks():
                task = load_task(task_id)
                print(f"{task_id:20} {task['workload']:20} {task['title']}")
            return 0
        if args.diff is not None:
            variant = None
            if args.variant:
                variant = "" if args.variant == "none" else (
                    variant_mod.load_variant(args.variant).name
                    if pathlib.Path(args.variant).is_dir() else args.variant)
            copy_dir, record_dir = find_run(
                args.diff or None, args.task, pathlib.Path(args.runs_dir).expanduser(),
                pathlib.Path(args.log).expanduser(), args.tool, args.model, args.effort,
                args.rep, variant)
            print_diff(copy_dir, record_dir, args.stat)
            return 0
        if args.show:
            record_dir = resolve_record(args.show)
            record = json.loads((record_dir / "run.json").read_text(encoding="utf-8"))
            if args.answer:
                print_answer(record, record_dir)
                return 0
            if args.brief and not args.json:
                print_result_brief(record)
                return 0
            print(json.dumps(record, indent=2) if args.json else describe_record(record))
            if not args.json:
                print_result(record, record_dir)
            return 0
        if args.rebuild:
            record_dir = resolve_record(args.rebuild)
            record = json.loads((record_dir / "run.json").read_text(encoding="utf-8"))
            row = build_row(record, usage_from_record(record_dir, record["meta"]["tool"]))
            if args.append:
                log_path = pathlib.Path(args.log).expanduser()
                add_rebuilt_row(log_path, row)
                print(f"Added {row['run_id']} to {repo_path(log_path)}: {row['task']} · "
                      f"{row['tool']} · {row['model']} · {row['effort']} · rep {row['rep']}")
                return 0
            print(json.dumps(row, indent=2) if args.json else row_csv(row), end="" if not
                  args.json else "\n")
            return 0
        if args.sequence:
            q = plan_sequence(args)
            if args.dry_run:
                print(describe_sequence(q) + "\n\nDry run: no copy and no model request.")
                return 0
            print(describe_sequence(q) + "\n")
            record = execute_sequence(q)
            if args.json:
                print(json.dumps(record, indent=2))
            else:
                print()
                print_sequence_result(record, q["copy_dir"], q["record_dir"])
            return 0 if not record["error"] else 1
        if args.idle is not None:
            raise ValueError("--idle needs --sequence")
        if args.context:
            return run_contexts(args)
        if is_sweep(args):
            return run_sweep(args)
        if not args.task or not args.tool:
            raise ValueError("give a task and --tool (or --list, --show, --diff, or --rebuild)")
        p = plan_run(args)
        variant_name = p["variant"].name if p["variant"] else ""
        if args.up_to is not None:
            done = next_rep(read_log(p["log"]), p["task"]["id"], p["tool"], p["model"],
                            p["effort"], variant_name) - 1
            if done >= args.up_to:
                print(f"Skip: {repo_path(p['log'])} has {done} run{'' if done == 1 else 's'} of {p['task']['id']} · "
                      f"{p['tool']} · {p['model']} · {p['effort']}"
                      + (f" · {variant_name}" if variant_name else "")
                      + f" (--up-to {args.up_to}).")
                return 0
        if args.copy_only:
            p["copy_only"] = True
            execute(p)
            home = str(pathlib.Path.home())
            print(f"\nCopy ready: {str(p['copy_dir']).replace(home, '~')}\n"
                  "No agent run, no checks, and no log row.")
            return 0
        if args.dry_run:
            if args.brief:
                print(describe_brief(p) + "\nDry run: no copy and no model request.")
                return 0
            print(describe(p) + "\n\nDry run: no copy and no model request.")
            return 0
        if args.answer:
            record = execute(p, log=lambda *_: None)
            print_answer(record, p["record_dir"])
            return 0
        if args.brief and not args.json:
            print(f"Run {p['run_id']}: no progress lines (--brief). Wait for the result.\n")
            print_result_brief(execute(p, log=lambda *_: None))
            return 0
        print(describe(p) + "\n")
        record = execute(p)
        if args.json:
            print(json.dumps(record, indent=2))
        else:
            print()
            print_result(record, p["record_dir"])
        return 0
    except (ValueError, KeyError, FileNotFoundError, FileExistsError,
            json.JSONDecodeError, subprocess.CalledProcessError) as error:
        message = error.args[0] if isinstance(error, KeyError) and error.args else error
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            message = f"{error}\n{error.stderr if isinstance(error.stderr, str) else error.stderr.decode()}"
        print(f"run-baseline: error: {message}", file=sys.stderr)
        return 2
