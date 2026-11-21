"""Codex on the local model.

Codex sends OpenAI Responses API requests. Ollama accepts them at
http://localhost:11434/v1/responses. The Ollama guide for Codex
(https://docs.ollama.com/integrations/codex) gives three ways to connect:

    ollama launch codex          Ollama writes a profile and a model catalog
                                 in ~/.codex, then starts Codex with the profile
    codex --oss                  the built-in Ollama provider of Codex
    codex --profile ollama-launch
                                 the profile file ~/.codex/ollama-launch.config.toml

`bin/codex-local` gives the same settings as the profile, as `codex -c
key=value` options, for one process only (config/local/codex.json):

    model_provider            ollama-local: a provider for the Ollama
    model_providers.*         Responses API, with no OpenAI sign-in
    model_catalog_json        config/local/codex-models.json: the model
                              metadata (window, thinking levels). Without it,
                              Codex says "Model metadata for `qwen3.5:35b` not
                              found" and uses fallback metadata.
    model_context_window      the window that Ollama serves (65536). The
                              catalog of `ollama launch codex` says 128000.
    web_search                disabled. Through the Ollama profile, Ollama
                              sends web searches to ollama.com.
    model_reasoning_effort    from the thinking setting in ollama.json
                              (thinking on = high). Ollama turns thinking off
                              only for "none".

The launcher also sets CODEX_HOME to a separate Codex home (~/.codex-local).
That home has no ChatGPT sign-in, so no request can go to the plan. Codex
writes its sessions there, and also its own settings (for example the trust
of a project). ~/.codex/config.toml and the sign-in in ~/.codex do not
change, so `codex` in another terminal still uses ChatGPT Plus.

The run harness uses the same settings and the same Codex home
for a Codex run on a local model. The configuration of such a run (the
`config` column) is a hash of four files: baseline/config/codex-overrides.json,
codex.json, codex-models.json, and ollama.json. So it is never the
configuration of a cloud run.

The local meters of a Codex session (`tokenomics ollama task --codex`) join
the Ollama server log (time of each request) and the Codex session file (the
time window of the session, and the tokens written). Codex counts the cached
tokens inside its input count, so the new tokens of a request are input
minus cached.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys

from . import codex as codex_mod
from . import ollama as ollama_mod
from .local import LOCAL_DIR, check_server, think_text
from .rates import REPO_ROOT

CODEX_FILE = LOCAL_DIR / "codex.json"
CATALOG_FILE = LOCAL_DIR / "codex-models.json"

USAGE = """\
codex-local: Codex on the local Ollama model, for this process only.

    codex-local [--model MODEL | --fallback] [CODEX ARGUMENTS ...]
    codex-local --show [--model MODEL | --fallback]

    --model MODEL   a model from `ollama list` and config/local/codex-models.json
                    (default: the model in config/local/ollama.json)
    --fallback      the fallback model in config/local/ollama.json
    --show          show the settings and the command. Start nothing.

All other arguments go to `codex`, for example `exec`. The settings apply to
this process only. No shell profile and no file in ~/.codex change.
"""


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def load_codex_settings(path=None) -> dict:
    with open(path or CODEX_FILE) as f:
        return json.load(f)


def local_home(codex: dict | None = None) -> pathlib.Path:
    """The separate Codex home of the local model."""
    codex = codex if codex is not None else load_codex_settings()
    return pathlib.Path(codex["codex_home"]).expanduser()


def catalog_models(path=None) -> list[str]:
    with open(path or CATALOG_FILE) as f:
        return [m["slug"] for m in json.load(f)["models"]]


def think_effort(value) -> str:
    """The Codex reasoning effort for a thinking setting of ollama.json:
    true = high, false = none, a level = that level."""
    if value is True:
        return "high"
    if value is False or value is None:
        return "none"
    return str(value)


def model_effort(model: str, settings: dict) -> str | None:
    if model == settings.get("model"):
        return think_effort(settings.get("think"))
    fallback = settings.get("fallback") or {}
    if model == fallback.get("model"):
        return think_effort(fallback.get("think"))
    return None


def codex_settings(settings: dict | None = None, codex: dict | None = None,
                   catalog: str | None = None) -> dict:
    """The `-c` settings, in order, with the values filled in. The window is a
    number. The catalog is an absolute path (default: codex-models.json)."""
    settings = settings if settings is not None else ollama_mod.load_settings()
    codex = codex if codex is not None else load_codex_settings()
    names = {"base_url": codex["base_url"], "catalog": catalog or str(CATALOG_FILE),
             "context_length": str(settings["context_length"])}
    out = {}
    for key, value in codex["set"].items():
        text = value.format(**names) if isinstance(value, str) else value
        out[key] = int(text) if value == "{context_length}" else text
    return out


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if isinstance(value, int) else json.dumps(value)


def codex_args(settings: dict | None = None, codex: dict | None = None,
               catalog: str | None = None) -> list[str]:
    """`-c key=value` options for Codex on the local model."""
    out = []
    for key, value in codex_settings(settings, codex, catalog).items():
        out += ["-c", f"{key}={toml_value(value)}"]
    return out


def codex_env(base: dict | None = None, codex: dict | None = None) -> dict[str, str]:
    """The environment of Codex on the local model: `base` (default: this
    process) without the API keys, with CODEX_HOME set to the local home."""
    codex = codex if codex is not None else load_codex_settings()
    remove = set(codex.get("remove", []))
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in remove}
    env["CODEX_HOME"] = str(local_home(codex))
    return env


def ensure_home(codex: dict | None = None) -> pathlib.Path:
    """Make the local Codex home if it does not exist. Codex needs it."""
    home = local_home(codex)
    home.mkdir(parents=True, exist_ok=True)
    return home


def config_hash(overrides_file: pathlib.Path, codex_file=None, catalog_file=None,
                settings_file=None) -> str:
    """The configuration of a Codex run on the local model: the first 12 hex
    digits of the sha256 of the harness overrides, codex.json,
    codex-models.json, and ollama.json, in that order."""
    digest = hashlib.sha256()
    for path in (overrides_file, codex_file or CODEX_FILE, catalog_file or CATALOG_FILE,
                 settings_file or ollama_mod.SETTINGS_FILE):
        digest.update(pathlib.Path(path).read_bytes())
    return digest.hexdigest()[:12]


# --------------------------------------------------------------------------
# The local meters of one Codex session
# --------------------------------------------------------------------------

def find_thread(spec: str | None = None, project=None, root=None) -> pathlib.Path:
    """A Codex session file: a file path, a thread ID, or the latest thread of
    the project, in the local Codex home (default)."""
    if spec and pathlib.Path(spec).expanduser().is_file():
        return pathlib.Path(spec).expanduser()
    root = pathlib.Path(root) if root else local_home() / "sessions"
    return codex_mod.find_thread(spec, project, root)


def thread_span(records: list[dict]) -> tuple[float, float]:
    times = [codex_mod._ts(r["timestamp"]) for r in records if r.get("timestamp")]
    if not times:
        raise ValueError("no time stamps in the Codex session file")
    return min(times), max(times)


def session_meters(path, log_text: str) -> dict:
    """The local meters of a Codex session: the requests in the Ollama server
    log during the session, with the tokens written from the session file."""
    records = codex_mod.read_records(path)
    thread = codex_mod.load_session(records)
    start, end = thread_span(records)
    requests = ollama_mod.requests_between(ollama_mod.log_requests(log_text), start, end)
    if not requests:
        raise ValueError("no model request in the Ollama server log during this session. "
                         "Ollama starts a new log file when the app starts: give the "
                         "older file with --log. The llama.cpp runner (gpt-oss) writes no "
                         "request lines.")
    done = [(r.raw["input_tokens"] - r.raw["cached_input_tokens"], r.raw["cached_input_tokens"],
             r.raw["output_tokens"]) for r in thread.requests]
    models = sorted({r.model for r in thread.requests if r.model})
    written = ollama_mod.match_transcript(requests, done)
    meters = ollama_mod.task_meters(requests, written, model=", ".join(models),
                                    session=thread.thread_id[:8],
                                    transcript_requests=len(done))
    return {"tool": "Codex"} | meters


# --------------------------------------------------------------------------
# The launcher: bin/codex-local
# --------------------------------------------------------------------------

def parse_args(argv: list[str], settings: dict) -> tuple[str, bool, list[str]]:
    """(model, show, arguments for codex)."""
    model, show, rest = settings["model"], False, []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--show":
            show = True
        elif arg == "--fallback":
            model = settings["fallback"]["model"]
        elif arg in ("--model", "-m"):
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


def launch_args(model: str, rest: list[str], settings: dict, codex: dict,
                catalog: str | None = None) -> list[str]:
    """The codex command line: the subcommand first (for example `exec`), then
    the model, the effort, and the local settings, then the other arguments."""
    head, tail = [], list(rest)
    if tail and not tail[0].startswith("-"):
        head, tail = [tail[0]], tail[1:]
    effort = model_effort(model, settings)
    argv = ["codex"] + head + ["-m", model]
    if effort:
        argv += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
    return argv + codex_args(settings, codex, catalog) + tail


def show_text(model: str, rest: list[str], settings: dict, codex: dict) -> str:
    catalog = os.path.relpath(CATALOG_FILE, REPO_ROOT)
    values = codex_settings(settings, codex, catalog)
    window = settings["context_length"]
    effort = model_effort(model, settings)
    home = codex["codex_home"]
    lines = [f"Backend   Ollama at {codex['base_url']} (Responses API)",
             f"Model     {model} · {think_text(model, settings)} · window {window:,}"]
    sets = ([f'model_reasoning_effort="{effort}"'] if effort else []) + \
        [f"{key}={toml_value(value)}" for key, value in values.items()]
    for n, text in enumerate(sets):
        lines.append(("Sets      -c " if n == 0 else " " * 10 + "-c ") + text)
    lines.append(f"Home      CODEX_HOME={home}: no ChatGPT sign-in, so no request to the plan")
    lines.append("Removes   " + " ".join(k for k in codex.get("remove", []) if k.endswith("KEY")))
    head = [a for a in rest[:1] if not a.startswith("-")]
    lines.append("Starts    " + " ".join(["codex"] + head + ["-m", model, "-c ..."]
                                         + rest[len(head):]))
    lines.append("Changes   this process only: no shell profile, no file in ~/.codex")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    try:
        settings = ollama_mod.load_settings()
        codex = load_codex_settings()
        model, show, rest = parse_args(argv, settings)
        if show:
            print(show_text(model, rest, settings, codex))
            return 0
        if model not in catalog_models():
            raise ValueError(f"model {model} is not in {CATALOG_FILE.name}. Add it there "
                             f"first, or Codex uses fallback metadata.")
        check_server(model)
        ensure_home(codex)
    except (OSError, ValueError, KeyError) as error:
        print(f"codex-local: {error}", file=sys.stderr)
        return 2
    exe = shutil.which("codex")
    if not exe:
        print("codex-local: `codex` is not on PATH. Install Codex first.", file=sys.stderr)
        return 2
    print(f"codex-local: Codex on {model} at {codex['base_url']}, "
          f"CODEX_HOME={codex['codex_home']} (this process only)", file=sys.stderr)
    os.execve(exe, launch_args(model, rest, settings, codex), codex_env(None, codex))
    return 0  # not reached
