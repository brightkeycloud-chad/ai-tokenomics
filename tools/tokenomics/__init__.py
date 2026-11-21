"""Tokenomics toolkit.

Shared tools that measure the token cost of AI coding agents. The tools use
only the Python standard library.

Modules:
    costmodel  - token cost of one attempt, and cost per successful task
    rates      - rate profiles loaded from config/rates.json
    scenario   - scenario files for `tokenomics compare`
    cli        - the `tokenomics` command
    session    - reads Claude Code transcripts: every lane, tokens by type,
                 time split, context, and cache (`tokenomics session`).
                 `tokenomics session --requests` gives every request of every
                 lane in time order, with its cache result (hit, write, miss),
                 the gap, and the cost of a miss compared with a hit
    reduce     - copies a session with no text, as a sample that is safe to
                 commit (`tokenomics reduce`)
    codex      - reads `codex exec --json` events and Codex session files:
                 tokens by type (cached tokens come out of the input count),
                 cost at API list price and in Codex credits, time, context,
                 and plan limits (`tokenomics codex`). Also reduces Codex
                 files for samples (`tokenomics reduce`).
    baseline   - the run harness (`bin/run-baseline`): a clean copy of a
                 workload, one non-interactive agent run with pinned settings,
                 hidden acceptance tests, house-rule checks, and one row in the
                 results log (`tokenomics log` reads it)
    runconfig  - the checked-in run configuration of each tool, and its hash
    sweep      - sweeps for the harness: many setups (tool, model, effort) and
                 a target number of repetitions, from comma-separated options
                 or a sweep file (`run-baseline --sweep FILE`)
    index      - medians, run-to-run spread, cost per success, and the index of
                 each setup to the baseline of its tool (`tokenomics index`).
                 Writes measured scenario files for `tokenomics compare`.
    variant    - variant overlays: files that the harness puts over the copy
                 of the workload (`run-baseline --variant DIR`)
    sequence   - several prompts in one Claude Code session, with idle time
                 between them, for cache experiments (`run-baseline
                 --sequence FILE`, `--idle SECONDS`)
    tasks      - task files: the baseline tasks and extra tasks, with their own
                 Claude Code settings, AWS profile, and answer check
    extensions - four ways to extend the agent: always-on tokens from
                 `/context` (`run-baseline --sweep F --context`), tokens when
                 used, cost, time, and result (`tokenomics extensions`)
    toolresults - the size and the tokens of each tool result, test runs, and
                 re-runs of a run (`tokenomics tool-results`)
    ollama     - a local model on Ollama: the sizing with the GPU memory from
                 the server log (`tokenomics ollama fit`), time to first token
                 and writing speed for a cold prompt (`tokenomics ollama
                 bench`), and the four local meters (`tokenomics ollama
                 meters`). Settings: config/local/ollama.json
    local      - Claude Code on the local model, for one process only
                 (`bin/claude-local`, config/local/claude-code.json), the local
                 configuration hash for the harness, and the local meters of a
                 session from the Ollama server log (`tokenomics ollama task`)
    codexlocal - Codex on the local model, for one process only
                 (`bin/codex-local`, config/local/codex.json)
    tco        - payback months of local hardware (`tokenomics tco`)
"""

__version__ = "0.1.0"
