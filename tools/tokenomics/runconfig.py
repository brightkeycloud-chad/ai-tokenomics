"""The checked-in run configuration of each tool.

Pin the configuration. The harness gives each agent a checked-in
configuration file:

    claude    baseline/config/claude-settings.json   (claude --settings)
    codex     baseline/config/codex-overrides.json   (codex exec -c key=value)

The Claude Code file also sets the permission mode (permissions.defaultMode).
The harness reads it and passes it with --permission-mode, so the hash of the
file covers the mode too. The current file uses bypassPermissions inside the
Claude Code sandbox: no approvals, Bash commands write only in the run copy,
and the deny rules (terraform init/plan/apply/destroy, git commit/push, pip
and python -m pip, aws, WebFetch, WebSearch) still apply in bypass mode.

The `config` column of the results log records the configuration of each
run: the first 12 hex digits of the sha256 of that file. When the file
changes, the configuration changes. Then the runs before the change are a
different setup:

- `run-baseline --up-to`, the sweep plan, and the repetition number count
  only the runs with the current configuration.
- `tokenomics index` never puts runs of two configurations in one index.

Rows from before the `config` column have no value. All of them used the
first version of each file (commit 070c60f: an allow list in dontAsk mode).
The change of 2026-11-16 gave the Claude Code settings bypass mode in the
sandbox, allowed `python3`, and denied `python -m pip` and `aws`
(configuration 0af40a62a105). The Codex file did not change.
`LEGACY_CONFIG` gives the configuration of the first version. When the harness adds the column
to an old log, it writes these values into the old rows.

A Claude Code run on the local model (a model of
config/local/ollama.json, or an Ollama tag) has its own configuration: the
hash of the settings file of the task, config/local/claude-code.json, and
config/local/ollama.json (see local.py). Give the model to
`current_config(tool, task, model)`. A local run and a cloud run never have
the same configuration, so the harness never counts them together.

A Codex run on the local model has its own configuration too:
the hash of baseline/config/codex-overrides.json, config/local/codex.json,
config/local/codex-models.json, and config/local/ollama.json (see
codexlocal.py).

An extra task can have its own Claude Code settings file
("claude_settings" in task.json, see tasks.py). Then the configuration of a
Claude Code run of that task is the hash of that file. Give the task to
`current_config(tool, task)`.
"""
from __future__ import annotations

import hashlib

from .local import config_hash as local_config_hash, is_local_model
from .rates import REPO_ROOT
from .tasks import claude_settings_file

CONFIG_DIR = REPO_ROOT / "baseline" / "config"
CONFIG_FILES = {"claude": "claude-settings.json", "codex": "codex-overrides.json"}

# The configuration of every row from before the `config` column.
LEGACY_CONFIG = {"claude": "eb648c47c588", "codex": "a88994d06c01"}


def config_file(tool: str, task=None):
    """The configuration file of a run of this tool and task, or None (reference).
    task is a task ID, a loaded task, or None for the baseline file."""
    name = CONFIG_FILES.get(tool)
    if not name:
        return None
    if tool == "claude":
        own = claude_settings_file(task)
        if own is not None:
            return own
    return CONFIG_DIR / name


def current_config(tool: str, task=None, model: str | None = None) -> str:
    """The configuration that a run of this tool (and task, and model) uses
    now. "" for a tool with no configuration file (reference). A Claude Code
    run on a local model has the local configuration."""
    path = config_file(tool, task)
    if path is None:
        return ""
    if tool == "claude" and is_local_model(model):
        return local_config_hash(path)
    if tool == "codex" and is_local_model(model):
        from .codexlocal import config_hash as codex_local_hash
        return codex_local_hash(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def row_config(row: dict) -> str:
    """The configuration of one results-log row. A row from before the
    `config` column used the legacy configuration of its tool."""
    return row.get("config") or LEGACY_CONFIG.get(row.get("tool", ""), "")


def config_label(tool: str, config: str, task=None, model: str | None = None) -> str:
    """'current' or 'old', for the configuration of one row."""
    return "current" if config == current_config(tool, task, model) else "old"
