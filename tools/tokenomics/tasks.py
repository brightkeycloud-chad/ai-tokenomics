"""Task files for the run harness.

A task is a directory with a task.json file. The harness finds tasks in two
places:

    baseline/tasks/<id>/task.json            the two baseline tasks
    <NN>_<name>/<dir>/task/task.json         an extra task in a numbered
                                             directory of the repository

`list_tasks()` gives only the baseline tasks: "all" in a sweep means the
baseline tasks. An extra task has its own ID, and a sweep file or the command
line names it.

task.json can have optional fields, for a task that reads a live
system and gives an answer, not a code change:

    "claude_settings"    a Claude Code settings file in the task directory, in
                         place of baseline/config/claude-settings.json. The
                         `config` column of the log then records its hash.
    "skills": true       do not pass --disable-slash-commands, so that project
                         and plugin skills load
    "strict_mcp_config": false
                         do not pass --strict-mcp-config, so that the MCP
                         servers of a plugin load (the settings file should
                         then set disableClaudeAiConnectors)
    "aws_profile"        the AWS profile of the agent. The harness keeps the
                         AWS configuration files and sets AWS_PROFILE. It
                         still removes AWS key variables from the environment.
    "setup": "none"      no Python environment and no Terraform init

The acceptance and rule commands can use two names: {task} is the task
directory, and {answer} is <record>/answer.md, the final answer of the agent.
"""
from __future__ import annotations

import json
import pathlib

from .rates import REPO_ROOT, project_roots

TASKS_DIR = REPO_ROOT / "baseline" / "tasks"
DEMO_TASK_GLOB = "[0-9][0-9]_*/*/task/task.json"


def list_tasks() -> list[str]:
    """The baseline tasks."""
    return sorted(p.parent.name for p in TASKS_DIR.glob("*/task.json"))


def demo_tasks() -> dict[str, pathlib.Path]:
    """Task ID -> task.json of each extra task."""
    out = {}
    for root in project_roots(REPO_ROOT):
        for path in sorted(root.glob(DEMO_TASK_GLOB)):
            try:
                task_id = json.loads(path.read_text(encoding="utf-8")).get("id")
            except json.JSONDecodeError:
                continue
            if task_id and task_id not in out:
                out[task_id] = path
    return out


def task_file(name: str) -> pathlib.Path | None:
    """The task.json of a task ID, or of a task directory."""
    path = TASKS_DIR / name / "task.json"
    if path.is_file():
        return path
    found = demo_tasks().get(name)
    if found:
        return found
    given = pathlib.Path(name).expanduser()
    if (given / "task.json").is_file():
        return given / "task.json"
    return None


def load_task(name: str) -> dict:
    path = task_file(name)
    if path is None:
        names = list_tasks() + sorted(demo_tasks())
        raise FileNotFoundError(f"no task {name!r}. Tasks: {', '.join(names)}")
    task = json.loads(path.read_text(encoding="utf-8"))
    task["dir"] = path.parent
    return task


def claude_settings_file(task: dict | str | None) -> pathlib.Path | None:
    """The Claude Code settings file of a task, or None for the baseline file."""
    if task is None:
        return None
    if isinstance(task, str):
        path = task_file(task)
        if path is None:
            return None
        task = json.loads(path.read_text(encoding="utf-8")) | {"dir": path.parent}
    name = task.get("claude_settings")
    return task["dir"] / name if name else None
