"""Claude Code on the local model.

Claude Code sends Anthropic Messages API requests. Ollama accepts them at
http://localhost:11434/v1/messages. The Ollama guide for Claude Code
(https://docs.ollama.com/integrations/claude-code) gives two ways to connect:

    ollama launch claude --model MODEL     Ollama starts Claude Code with the
                                           variables set
    ANTHROPIC_BASE_URL=... claude --model  the manual way: you set the
                                           variables yourself

`bin/claude-local` is the manual way, for one process only. It sets the
variables in config/local/claude-code.json, removes the API keys, and starts
`claude --model MODEL`. It changes no shell profile and no settings file, so
`claude` in another terminal still uses your Claude Pro subscription.

The variables:

    ANTHROPIC_BASE_URL              the local Ollama server
    ANTHROPIC_AUTH_TOKEN            "ollama". Ollama does not check it. It
                                    takes precedence over your claude.ai login,
                                    so no request goes to Anthropic.
    ANTHROPIC_DEFAULT_*_MODEL       the local model for every alias, so that a
    CLAUDE_CODE_SUBAGENT_MODEL      background request or a subagent also stays
                                    local
    CLAUDE_CODE_MAX_CONTEXT_TOKENS  the window that Ollama serves (65536).
                                    Claude Code does not know the model ID, so
                                    it would assume a larger window.
    CLAUDE_CODE_ATTRIBUTION_HEADER  0: no attribution block at the start of the
                                    system prompt (as `ollama launch` sets)

The model, the window, and the fallback come from config/local/ollama.json.

The run harness uses the same variables for a run on a local
model. The configuration of such a run (the `config` column of the results
log) is a hash of three files: the Claude Code settings of the task, this
file, and ollama.json. So it is never the configuration of a cloud run, and
the harness never counts local runs and cloud runs together.

The local meters of a session (`tokenomics ollama task`) join two
sources: the Ollama server log gives the time of each model request (load,
read, write) and its tokens (new and cache hit). The Claude Code transcript
gives the time window of the session and the tokens written. Claude Code also
sends background requests, for example for the session title. They are in
the log, but not in the transcript.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys
import urllib.parse

from . import ollama as ollama_mod
from .rates import REPO_ROOT
from .session import load_session, parse_time

LOCAL_DIR = REPO_ROOT / "config" / "local"
CLAUDE_FILE = LOCAL_DIR / "claude-code.json"

USAGE = """\
claude-local: Claude Code on the local Ollama model, for this process only.

    claude-local [--model MODEL | --fallback] [CLAUDE ARGUMENTS ...]
    claude-local --show [--model MODEL | --fallback]

    --model MODEL   a model from `ollama list` (default: the model in
                    config/local/ollama.json)
    --fallback      the fallback model in config/local/ollama.json
    --show          show the variables and the command. Start nothing.

All other arguments go to `claude`. The variables apply to this process only.
No shell profile and no settings file change.
"""


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def load_claude_settings(path=None) -> dict:
    with open(path or CLAUDE_FILE) as f:
        return json.load(f)


def local_models(settings: dict | None = None) -> list[str]:
    """The local model and the fallback, from config/local/ollama.json."""
    settings = settings if settings is not None else ollama_mod.load_settings()
    models = [settings.get("model")]
    models.append((settings.get("fallback") or {}).get("model"))
    return [m for m in models if m]


def is_local_model(model: str | None, settings: dict | None = None) -> bool:
    """True for a model of config/local/ollama.json, or any Ollama tag
    (name:tag) that is not an Anthropic model ID."""
    if not model:
        return False
    try:
        if model in local_models(settings):
            return True
    except OSError:
        pass
    return ":" in model and "claude" not in model and "anthropic" not in model


def is_local_url(url: str | None) -> bool:
    """True when ANTHROPIC_BASE_URL points at this machine."""
    if not url:
        return False
    host = urllib.parse.urlparse(url if "//" in url else "//" + url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def claude_vars(model: str | None = None, settings: dict | None = None,
                claude: dict | None = None) -> tuple[dict[str, str], list[str]]:
    """The variables to set and the variables to remove, for one model."""
    settings = settings if settings is not None else ollama_mod.load_settings()
    claude = claude if claude is not None else load_claude_settings()
    names = {"model": model or settings["model"],
             "context_length": str(settings["context_length"]),
             "base_url": claude["base_url"]}
    values = {key: value.format(**names) for key, value in claude["set"].items()}
    return values, list(claude.get("remove", []))


def claude_env(model: str | None = None, base: dict | None = None,
               settings: dict | None = None, claude: dict | None = None) -> dict[str, str]:
    """The environment of Claude Code on the local model: `base` (default: this
    process) without the removed variables, with the local variables."""
    values, remove = claude_vars(model, settings, claude)
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in remove}
    env.update(values)
    return env


def config_hash(task_settings: pathlib.Path, claude_file=None, settings_file=None) -> str:
    """The configuration of a Claude Code run on the local model: the first 12
    hex digits of the sha256 of the task settings file, claude-code.json, and
    ollama.json, in that order."""
    digest = hashlib.sha256()
    for path in (task_settings, claude_file or CLAUDE_FILE,
                 settings_file or ollama_mod.SETTINGS_FILE):
        digest.update(pathlib.Path(path).read_bytes())
    return digest.hexdigest()[:12]


# --------------------------------------------------------------------------
# The local meters of one Claude Code session
# --------------------------------------------------------------------------

def transcript_span(path: pathlib.Path) -> tuple[float, float]:
    """The first and the last time stamp in the transcripts of a session (the
    main lane and every subagent and forked-skill lane)."""
    path = pathlib.Path(path)
    files = [path] + sorted(path.with_suffix("").glob("subagents/agent-*.jsonl"))
    times = []
    for file in files:
        with open(file, encoding="utf-8") as handle:
            for line in handle:
                try:
                    stamp = json.loads(line).get("timestamp")
                except json.JSONDecodeError:
                    continue
                if stamp:
                    times.append(parse_time(stamp))
    if not times:
        raise ValueError(f"no time stamps in {path.name}")
    return min(times), max(times)


def session_meters(path, log_text: str) -> dict:
    """The local meters of a Claude Code session: the requests in the Ollama
    server log during the session, with the tokens written from the transcript."""
    session = load_session(path)
    start, end = transcript_span(path)
    requests = ollama_mod.requests_between(ollama_mod.log_requests(log_text), start, end)
    if not requests:
        raise ValueError("no model request in the Ollama server log during this session. "
                         "Ollama starts a new log file when the app starts: give the "
                         "older file with --log. The llama.cpp runner (gpt-oss) writes no "
                         "request lines.")
    done = sorted((r for lane in session.lanes for r in lane.requests), key=lambda r: r.end)
    models = sorted({r.model for r in done if r.model})
    written = ollama_mod.match_transcript(
        requests, [(r.usage.input, r.usage.cache_read, r.usage.output) for r in done])
    return ollama_mod.task_meters(requests, written,
                                  model=", ".join(models), session=session.session_id[:8],
                                  transcript_requests=len(done))


# --------------------------------------------------------------------------
# The launcher: bin/claude-local
# --------------------------------------------------------------------------

def parse_args(argv: list[str], settings: dict) -> tuple[str, bool, list[str]]:
    """(model, show, arguments for claude)."""
    model, show, rest = settings["model"], False, []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--show":
            show = True
        elif arg == "--fallback":
            model = settings["fallback"]["model"]
        elif arg == "--model":
            if i + 1 >= len(argv):
                raise ValueError("--model needs a model name")
            model = argv[i + 1]
            i += 1
        elif arg.startswith("--model="):
            model = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        i += 1
    return model, show, rest


def think_text(model: str, settings: dict) -> str:
    if model == settings.get("model"):
        return f"thinking {ollama_mod.think_label(settings.get('think'))}"
    fallback = settings.get("fallback") or {}
    if model == fallback.get("model"):
        return f"thinking {ollama_mod.think_label(fallback.get('think'))}"
    return "thinking: model default"


def show_text(model: str, rest: list[str], settings: dict, claude: dict) -> str:
    values, remove = claude_vars(model, settings, claude)
    window = settings["context_length"]
    lines = [f"Backend   Ollama at {claude['base_url']}",
             f"Model     {model} · {think_text(model, settings)} · window {window:,}"]
    for n, (key, value) in enumerate(values.items()):
        lines.append(("Sets      " if n == 0 else " " * 10) + f"{key}={value}")
    lines.append("Removes   " + " ".join(k for k in remove if k.endswith("API_KEY"))
                 + " (and provider switches)")
    lines.append("Starts    " + " ".join(["claude", "--model", model] + rest))
    lines.append("Changes   this process only: no shell profile, no settings file")
    return "\n".join(lines)


def check_server(model: str) -> None:
    """Stop with a clear message when Ollama does not run or lacks the model."""
    tags = ollama_mod.api("/api/tags", timeout=5)
    names = {m.get("name") for m in tags.get("models", [])} | \
            {m.get("model") for m in tags.get("models", [])}
    if model not in names:
        raise ValueError(f"model {model} is not on this Mac (ollama list). Pull it first: "
                         f"ollama pull {model}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    try:
        settings = ollama_mod.load_settings()
        claude = load_claude_settings()
        model, show, rest = parse_args(argv, settings)
        if show:
            print(show_text(model, rest, settings, claude))
            return 0
        check_server(model)
    except (OSError, ValueError, KeyError) as error:
        print(f"claude-local: {error}", file=sys.stderr)
        return 2
    exe = shutil.which("claude")
    if not exe:
        print("claude-local: `claude` is not on PATH. Install Claude Code first.",
              file=sys.stderr)
        return 2
    print(f"claude-local: Claude Code on {model} at {claude['base_url']} "
          "(this process only)", file=sys.stderr)
    os.execve(exe, ["claude", "--model", model] + rest, claude_env(model, None, settings, claude))
    return 0  # not reached
