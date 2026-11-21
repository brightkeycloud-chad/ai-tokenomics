"""A local model on Ollama.

Memory decides what fits, and locally the meters run in seconds. Filter the
candidates, and size the model to what the GPU gets.

    facts(model)        what this Mac and Ollama report: the memory, the GPU
                        memory and the default window from the server log, and
                        the parameters, file size, and capabilities of the model
    fit(facts)          the sizing:
                            weight budget  = GPU memory - context and headroom
                            max parameters = weight budget / bytes per weight
                        The bytes per weight come from the model file:
                        file size / parameters.
    meters(response)    the four local meters of one response:
                            load       load_duration
                            read       prompt_eval_duration (new tokens only)
                            cache hit  prompt_eval_cached_count
                            write      eval_duration
                        prompt_eval_count includes the cached tokens.
    bench(model)        one cold prompt of about N tokens and a short answer:
                        time to first token, reading speed, and writing speed
                        (is the model fast enough?)

The module also gives the local meters of an agent task. An agent such as Claude
Code talks to Ollama through the Anthropic-compatible endpoint, which returns
token counts but no times. The Ollama server log has the times. For each
model request, the MLX runner writes these lines:

    msg="starting mlx runner subprocess"     a load starts (only when the
    msg="mlx runner is ready"                model was not in memory)
    msg="cache hit" / "cache miss"           the prompt: cached=<cache hit
                                             tokens> left=<new tokens>
    msg="Prompt processing progress"         reading. The last line has
                                             processed = total - 1.
    msg=ServeHTTP path=/v1/completions       the end of the response, and
        took=<duration>                      the time without the load

    log_requests(text)  every model request in a server log:
                            load   ready - starting (0 when no load)
                            read   last progress line - (end - took)
                            write  end - last progress line (thinking and
                                   answer tokens)
    task_meters(...)    the requests of one Claude Code session (its time
                        window), with the tokens written from the transcript
                        (match_transcript). A request that is not in the
                        transcript is a background request.
                        The cold request is the one with the most new tokens.
                        The view compares it with the median of the requests
                        after it, which read from cache.

The llama.cpp runner (for example gpt-oss:20b) does not write these lines, so
`log_requests` finds no request for it.

The settings live in config/local/ollama.json: the model, the thinking
setting, and the context window of the local runs.

The functions that read a response, a log, or a saved file make no request.
Only `gather_facts`, `chat`, `run_meters`, and `run_bench` call the local
Ollama server. No request leaves the machine.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SETTINGS_FILE = REPO_ROOT / "config" / "local" / "ollama.json"
SERVER_LOG = pathlib.Path.home() / ".ollama" / "logs" / "server.log"
GIB = 1024 ** 3

# Source files of the workloads, in this order, for prompts of a given size.
PROMPT_SOURCES = ("workloads/status-board-app", "workloads/status-board-infra")
PROMPT_SUFFIXES = (".md", ".py", ".tf", ".yml", ".html", ".css", ".toml")
CHARS_PER_TOKEN = 3.9  # measured on the workload files with qwen3.5:35b
QUESTION = ("Which file defines the load balancer listener? "
            "Answer in one short sentence.")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def load_settings(path=None) -> dict:
    """The settings for the local model (config/local/ollama.json)."""
    path = pathlib.Path(path) if path else SETTINGS_FILE
    with open(path) as f:
        return json.load(f)


def think_value(text):
    """The `think` value for the API: true, false, or a level such as "low"."""
    if text is None:
        return None
    if isinstance(text, bool):
        return text
    low = str(text).strip().lower()
    if low in ("true", "on", "yes"):
        return True
    if low in ("false", "off", "no"):
        return False
    return low


def think_label(value) -> str:
    if value is None:
        return "default"
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value)


# --------------------------------------------------------------------------
# The local server
# --------------------------------------------------------------------------

def host() -> str:
    value = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    if not value.startswith("http"):
        value = "http://" + value
    return value.rstrip("/")


def _open(request, timeout: float):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace").strip()
        raise ValueError(f"Ollama: {error.code} {detail}") from None
    except urllib.error.URLError:
        raise ValueError(f"cannot connect to Ollama at {host()}: start the Ollama app") from None


def api(path: str, body: dict | None = None, timeout: float = 900):
    """One request to the local Ollama server. Returns the JSON response."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(host() + path, data=data,
                                     headers={"Content-Type": "application/json"})
    with _open(request, timeout) as response:
        return json.load(response)


def chat(model: str, prompt: str, think=None, max_tokens: int | None = None,
         keep_alive: str | None = None) -> dict:
    """Stream one chat request. Return the final response, plus two client
    times: to the first token, thinking or answer (`first_token_s`), and to
    the first answer token (`first_answer_s`)."""
    body = {"model": model, "stream": True,
            "messages": [{"role": "user", "content": prompt}]}
    if think is not None:
        body["think"] = think
    if max_tokens:
        body["options"] = {"num_predict": max_tokens}
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    request = urllib.request.Request(host() + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    start = time.monotonic()
    first = first_answer = None
    final = {}
    answer = []
    with _open(request, 1800) as response:
        for line in response:
            if not line.strip():
                continue
            chunk = json.loads(line)
            message = chunk.get("message") or {}
            if first is None and (message.get("content") or message.get("thinking")):
                first = time.monotonic() - start
            if first_answer is None and message.get("content"):
                first_answer = time.monotonic() - start
            answer.append(message.get("content") or "")
            if chunk.get("done"):
                final = chunk
    final["first_token_s"] = first
    final["first_answer_s"] = first_answer
    final["wall_s"] = time.monotonic() - start
    final["answer"] = "".join(answer)
    return final


def unload(model: str) -> None:
    """Unload the model from memory, as `ollama stop` does."""
    api("/api/generate", {"model": model, "keep_alive": 0})


# --------------------------------------------------------------------------
# Facts and fit
# --------------------------------------------------------------------------

def _size_gib(text: str) -> float:
    number, unit = re.match(r"\s*([\d.]+)\s*([KMGT]i?B)", text).groups()
    scale = {"KiB": 1 / 1024 ** 2, "MiB": 1 / 1024, "GiB": 1, "TiB": 1024,
             "KB": 1e3 / GIB, "MB": 1e6 / GIB, "GB": 1e9 / GIB, "TB": 1e12 / GIB}[unit]
    return float(number) * scale


def parse_server_log(text: str) -> dict:
    """The GPU memory, the default window, and the context setting from the
    most recent server start in an Ollama server log."""
    out = {}
    for line in text.splitlines():
        if 'msg="inference compute"' in line:
            m = re.search(r'total="([^"]+)"', line)
            if m:
                out["gpu_gib"] = round(_size_gib(m.group(1)), 1)
            m = re.search(r'description="([^"]+)"', line)
            if m:
                out["gpu_name"] = m.group(1)
        elif 'msg="vram-based default context"' in line:
            m = re.search(r"default_num_ctx=(\d+)", line)
            if m:
                out["default_context"] = int(m.group(1))
        elif 'msg="server config"' in line:
            m = re.search(r"OLLAMA_CONTEXT_LENGTH:(\d+)", line)
            if m:
                out["context_setting"] = int(m.group(1))
        elif 'msg="system memory"' in line and "memory_gib" not in out:
            m = re.search(r'total="([^"]+)"', line)
            if m:
                out["memory_gib_log"] = round(_size_gib(m.group(1)), 1)
    return out


def system_memory_gib() -> float | None:
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                             text=True, check=True).stdout
        return round(int(out.strip()) / GIB, 1)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def gather_facts(model: str, log_path=None) -> dict:
    """Read the facts for `fit` from this Mac, the server log, and the API."""
    log_path = pathlib.Path(log_path) if log_path else SERVER_LOG
    facts = {"model": model}
    facts.update(parse_server_log(log_path.read_text(errors="replace")))
    memory = system_memory_gib() or facts.pop("memory_gib_log", None)
    facts.pop("memory_gib_log", None)
    facts["memory_gib"] = memory
    show = api("/api/show", {"model": model})
    details = show.get("details") or {}
    info = show.get("model_info") or {}
    facts["family"] = details.get("family") or info.get("general.architecture", "")
    facts["quantization"] = details.get("quantization_level", "")
    facts["parameters"] = info.get("general.parameter_count")
    facts["capabilities"] = show.get("capabilities") or []
    for entry in api("/api/tags").get("models", []):
        if entry.get("name") == model or entry.get("model") == model:
            facts["file_bytes"] = entry.get("size")
    if not facts.get("file_bytes"):
        raise ValueError(f"model {model} is not in `ollama list`")
    return facts


def fit(facts: dict, reserve_gib: float = 6.0) -> dict:
    """The sizing for one model on this machine."""
    for key in ("gpu_gib", "parameters", "file_bytes"):
        if not facts.get(key):
            raise ValueError(f"no {key} in the facts: start the Ollama app, then try again")
    gpu = facts["gpu_gib"]
    memory = facts.get("memory_gib")
    budget = gpu - reserve_gib
    bytes_per_weight = facts["file_bytes"] / facts["parameters"]
    max_params = budget * GIB / bytes_per_weight
    weights = facts["file_bytes"] / GIB
    return {
        "model": facts["model"],
        "memory_gib": memory,
        "gpu_gib": gpu,
        "gpu_share": gpu / memory if memory else None,
        "reserve_gib": reserve_gib,
        "weight_budget_gib": budget,
        "bytes_per_weight": bytes_per_weight,
        "max_parameters": max_params,
        "parameters": facts["parameters"],
        "weights_gib": weights,
        "spare_gib": budget - weights,
        "fits": weights <= budget,
        "calls_tools": "tools" in facts.get("capabilities", []),
        "default_context": facts.get("default_context"),
    }


# --------------------------------------------------------------------------
# Meters
# --------------------------------------------------------------------------

def meters(response: dict) -> dict:
    """The four local meters of one Ollama response, in seconds and tokens."""
    prompt = response.get("prompt_eval_count", 0) or 0
    cached = response.get("prompt_eval_cached_count", 0) or 0
    written = response.get("eval_count", 0) or 0
    read_s = (response.get("prompt_eval_duration", 0) or 0) / 1e9
    write_s = (response.get("eval_duration", 0) or 0) / 1e9
    new = prompt - cached
    return {
        "load_s": (response.get("load_duration", 0) or 0) / 1e9,
        "prompt_tokens": prompt,
        "new_tokens": new,
        "read_s": read_s,
        "read_tps": new / read_s if read_s else None,
        "cached_tokens": cached,
        "written_tokens": written,
        "write_s": write_s,
        "write_tps": written / write_s if write_s else None,
        "first_token_s": response.get("first_token_s"),
        "first_answer_s": response.get("first_answer_s"),
        "total_s": (response.get("total_duration", 0) or 0) / 1e9,
    }


# --------------------------------------------------------------------------
# Prompts and the bench
# --------------------------------------------------------------------------

def source_files(root=REPO_ROOT) -> list[pathlib.Path]:
    root = pathlib.Path(root)
    files = []
    for base in PROMPT_SOURCES:
        for path in sorted((root / base).rglob("*")):
            if (path.is_file() and path.suffix in PROMPT_SUFFIXES
                    and not any(part.startswith(".") and part not in (".github",)
                                for part in path.relative_to(root).parts)
                    and "__pycache__" not in path.parts):
                files.append(path)
    return files


def build_prompt(tokens: int, root=REPO_ROOT, nonce: str | None = None) -> str:
    """A prompt of about `tokens` tokens: the workload files, then a short
    question. The nonce on the first line makes the prompt cold: no request
    before it has the same start, so Ollama cannot reuse a cached prefix."""
    nonce = nonce or uuid.uuid4().hex[:12]
    budget = int(tokens * CHARS_PER_TOKEN)
    parts = [f"Request {nonce}. Here are the files of two repositories.\n"]
    size = len(parts[0])
    root = pathlib.Path(root)
    for path in source_files(root):
        text = f"\n=== {path.relative_to(root)} ===\n{path.read_text(errors='replace')}"
        if size + len(text) > budget:
            text = text[:budget - size]
        parts.append(text)
        size += len(text)
        if size >= budget:
            break
    if size < budget:
        raise ValueError(f"the workload files give only about "
                         f"{int(size / CHARS_PER_TOKEN):,} tokens")
    parts.append("\n\n" + QUESTION)
    return "".join(parts)


def default_think(model: str):
    """The default thinking setting of a model, from `ollama show`."""
    return (api("/api/show", {"model": model}).get("thinking") or {}).get("default")


def run_bench(model: str, tokens: int, think=None, max_tokens: int = 2000) -> dict:
    """Load the model with a short request, then send one cold prompt. With no
    thinking setting, the model uses its default, and the result names it."""
    chat(model, "Say OK.", max_tokens=1)
    response = chat(model, build_prompt(tokens), think=think, max_tokens=max_tokens)
    result = meters(response)
    label = think_label(think if think is not None else default_think(model))
    result.update({"model": model, "think": label,
                   "target_tokens": tokens, "done_reason": response.get("done_reason"),
                   "date": time.strftime("%Y-%m-%d")})
    return result


def fast_enough(result: dict, max_first_token_s: float, min_write_tps: float) -> bool:
    return (result.get("first_token_s") is not None
            and result["first_token_s"] <= max_first_token_s
            and (result.get("write_tps") or 0) >= min_write_tps)


def run_meters(model: str, tokens: int, think=None, cold: bool = False,
               max_tokens: int = 60) -> list[dict]:
    """The same prompt two times: the second request reads it from cache.
    With cold=True, unload the model first, so the first request loads it."""
    if cold:
        unload(model)
    prompt = build_prompt(tokens)
    results = []
    for _ in range(2):
        response = chat(model, prompt, think=think, max_tokens=max_tokens)
        results.append(meters(response))
    return results


def save_results(path, results: list[dict]) -> None:
    """Add bench results to a JSON file. A new result replaces an older one
    with the same model and thinking setting."""
    path = pathlib.Path(path)
    old = json.loads(path.read_text()) if path.exists() else []
    keys = {(r["model"], r["think"]) for r in results}
    kept = [r for r in old if (r["model"], r["think"]) not in keys]
    path.write_text(json.dumps(kept + results, indent=2) + "\n")


# --------------------------------------------------------------------------
# The local meters of an agent task, from the server log
# --------------------------------------------------------------------------

LOG_TIME = re.compile(r"^time=(\S+)")
GO_DURATION = re.compile(r"([\d.]+)(h|ms|µs|us|ns|m|s)")
GO_UNITS = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "µs": 1e-6, "us": 1e-6, "ns": 1e-9}
# A request whose cache line comes more than this after its start waited in
# the queue for another request.
QUEUE_MIN_S = 0.5


def go_duration(text: str) -> float:
    """A Go duration such as 19.04s, 395.9ms, 69.2µs, or 1m2.5s -> seconds."""
    parts = GO_DURATION.findall(text)
    if not parts:
        raise ValueError(f"not a duration: {text!r}")
    return sum(float(number) * GO_UNITS[unit] for number, unit in parts)


def _line_time(line: str) -> float | None:
    m = LOG_TIME.match(line)
    if not m:
        return None
    import datetime as dt
    return dt.datetime.fromisoformat(m.group(1)).timestamp()


def _int_field(line: str, name: str) -> int | None:
    m = re.search(rf"\b{name}=(\d+)", line)
    return int(m.group(1)) if m else None


def log_requests(text: str) -> list[dict]:
    """Every model request in an Ollama server log (MLX runner), in order.
    Times are seconds since the epoch. One request at a time: Ollama runs the
    requests to one model one after the other. A request that waited for
    another request starts at its cache line; `queued_s` is the wait."""
    out = []
    load_start = pending_load = None
    current = None
    for line in text.splitlines():
        if 'msg="starting mlx runner subprocess"' in line:
            load_start = _line_time(line)
        elif 'msg="mlx runner is ready"' in line and load_start is not None:
            pending_load = _line_time(line) - load_start
            load_start = None
        elif 'msg="cache hit"' in line or 'msg="cache miss"' in line:
            current = {"cached_tokens": _int_field(line, "cached") or 0,
                       "new_tokens": _int_field(line, "left") or 0,
                       "load_s": pending_load or 0.0, "read_end": None,
                       "seen": _line_time(line)}
            pending_load = None
        elif 'msg="Prompt processing progress"' in line and current is not None:
            current["read_end"] = _line_time(line)
        elif "path=/v1/completions" in line and "took=" in line and current is not None:
            end = _line_time(line)
            took = go_duration(re.search(r"took=(\S+)", line).group(1))
            start = end - took
            # A request that came while another request ran waits in the
            # queue. `took` includes that wait. The request starts when the
            # runner looks up its prompt (the cache line). For example, a Codex
            # background request and the main request come at the same time.
            seen = current.pop("seen")
            queued = seen - start if seen is not None and seen - start > QUEUE_MIN_S else 0.0
            start += queued
            read_end = current.pop("read_end") or start
            read_end = min(max(read_end, start), end)
            current.update({"start": start, "end": end, "read_s": read_end - start,
                            "write_s": end - read_end, "queued_s": queued,
                            "total_s": current["load_s"] + end - start})
            out.append(current)
            current = None
    return out


def read_log_files(path=None) -> str:
    """The text of the server log. With no path: server.log, after the older
    files server-1.log ... server-5.log (Ollama starts a new file at each start
    of the app), oldest first."""
    if path:
        return pathlib.Path(path).read_text(errors="replace")
    logs = sorted(SERVER_LOG.parent.glob("server-*.log"),
                  key=lambda p: int(re.sub(r"\D", "", p.stem) or 0), reverse=True)
    return "".join(p.read_text(errors="replace") for p in logs + [SERVER_LOG] if p.is_file())


def requests_between(requests: list[dict], start: float, end: float,
                     margin: float = 2.0) -> list[dict]:
    """The requests that ended in [start - margin, end + margin]."""
    return [r for r in requests if start - margin <= r["end"] <= end + margin]


def match_transcript(requests: list[dict], transcript: list[tuple[int, int, int]]
                     ) -> list[int | None]:
    """The tokens written in each log request, from the transcript requests
    (input, cache read, output), matched in order by the prompt tokens: new
    tokens = input, cache hit = cache read. None for a request that is not in
    the transcript (a background request)."""
    out, start = [], 0
    for r in requests:
        written = None
        for n in range(start, len(transcript)):
            new, cached, output = transcript[n]
            if (new, cached) == (r["new_tokens"], r["cached_tokens"]):
                written, start = output, n + 1
                break
        out.append(written)
    return out


def task_meters(requests: list[dict], written: list[int | None] | None = None,
                model: str = "", session: str = "", transcript_requests: int | None = None
                ) -> dict:
    """The local meters of one task. `written` gives the tokens written in each
    request (see match_transcript), or None for a background request."""
    rows = []
    for n, r in enumerate(requests):
        row = {k: r[k] for k in ("start", "end", "load_s", "read_s", "write_s", "total_s",
                                 "new_tokens", "cached_tokens")}
        row["queued_s"] = r.get("queued_s", 0.0)
        row["written_tokens"] = written[n] if written else None
        row["background"] = bool(written) and written[n] is None
        rows.append(row)
    total = {k: sum(r[k] for r in rows) for k in ("load_s", "read_s", "write_s", "total_s",
                                                  "new_tokens", "cached_tokens", "queued_s")}
    total["written_tokens"] = (sum(r["written_tokens"] or 0 for r in rows)
                               if any(r["written_tokens"] is not None for r in rows) else None)
    # The cold request: the one that read the most new tokens (the first
    # prompt of the task, with the system prompt and the tools). The requests
    # after it read most of their prompt from cache.
    cold = max(range(len(rows)), key=lambda n: rows[n]["new_tokens"]) if rows else None
    later = sorted(r["total_s"] for r in rows[cold + 1:]) if rows else []
    median_later = None
    if later:
        mid = len(later) // 2
        median_later = later[mid] if len(later) % 2 else (later[mid - 1] + later[mid]) / 2
    prompt = total["new_tokens"] + total["cached_tokens"]
    return {"model": model, "session": session, "transcript_requests": transcript_requests,
            "requests": rows, "total": total,
            "cold_request": None if cold is None else cold + 1,
            "cold_s": None if cold is None else rows[cold]["total_s"],
            "later_median_s": median_later,
            "background": sum(1 for r in rows if r["background"]),
            "cache_hit": total["cached_tokens"] / prompt if prompt else None}

