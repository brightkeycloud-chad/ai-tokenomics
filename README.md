# ai-tokenomics

The toolkit from the video course **Tokenomics: Managing AI Agent Costs**.

Use it to see and measure the cost of AI coding agents: Claude Code, OpenAI Codex, and a local model on Ollama. The main tools are two status lines. They show the context, the prompt cache, the cost, and the plan limits while you work.

"Tokenomics" here has the FinOps meaning. It does not refer to cryptocurrency.

## Quick start

```bash
git clone https://github.com/brightkeycloud-chad/ai-tokenomics.git
cd ai-tokenomics
./install.sh
```

The installer adds a status line only for the tools that are on your PATH (`claude`, `codex`). It asks no questions, and you can run it again. To see what it will do before it changes a file, run `./install.sh --dry-run`.

**Keep the cloned directory where it is.** The Claude Code status line runs `tools/statusline/statusline.py` from this directory. If you move or delete the directory, the status line stops.

## The two status lines

### Claude Code

Claude Code sends the session data to `tools/statusline/statusline.py` after each response. The script prints three or four lines. This is the output for the sample session in `tools/statusline/sample-status.json`:

```text
Haiku 5.5 medium │ context ░░░░░░░░░░ 5% · 54.3k of 1.00M │ API 34.8s ÷ wall 26.4s = 132%
cache (main) │ hit 91% · 6 requests · 0 misses · 25.9k written ($0.0052) │ warm until 16:10 (1h)
cost │ session $0.0171 (estimate) │ last request $0.0015, cache writes 25% │ plan 5h 20% · 7d 13%
if cold: $0.0109 to re-cache 54.3k (20× a hit)
```

| Line | What it shows |
|---|---|
| 1 | The model and the effort level. The context fill. The API time divided by the wall-clock time (subagents run in parallel, so the ratio can be more than 100%). |
| 2 | The prompt cache of the main conversation: hit ratio, requests, misses, the tokens written to the cache, and warm or cold. The time is in your time zone. |
| 3 | The Claude Code session estimate, the cost of the last request, and the share of your 5-hour and 7-day plan windows that you used. |
| 4 | The cost of the next request if the cache is cold. |

To test the script with the sample, run:

```bash
python3 tools/statusline/statusline.py --plain < tools/statusline/sample-status.json
```

The dollar figures use the list prices in `config/rates.json`. On a subscription, they are estimates, not your bill. Prices change: check the `source` and the `checked` date of each profile.

### Codex

Codex has a footer at the bottom of its screen. The installer sets these items in the `[tui]` table of `~/.codex/config.toml`: `model-with-reasoning`, `current-dir`, `context-remaining`, `context-used`, `five-hour-limit`, and `weekly-limit`.

A real footer, from a session that also had the `thread-title` item:

```text
GPT-6-Luna default · ~/tokenomics/tokenomics-status-board-app · Trace incident read/write paths · Context 98% left · Context 2% used · 5h 100% left · weekly 100% left
```

To select other items, use `/statusline` in Codex, or run `python3 setup/codex-footer.py --set ITEM,ITEM,...`.

## Requirements

| Item | Requirement |
|---|---|
| Operating system | macOS or Linux, with bash |
| Python | 3.9 or later for the status lines (the `python3` of macOS is sufficient). 3.10 or later for the other tools. Standard library only: no packages to install. |
| Claude Code | For the Claude Code status line. The plan windows show only on a Claude subscription. |
| Codex | For the Codex footer. The limit items need a ChatGPT sign-in. |

## Install options

| Command | What it does |
|---|---|
| `./install.sh` | Installs the status line for each tool that is on your PATH, and shows the result. |
| `./install.sh --dry-run` | Shows what the installer would do. Changes nothing. |
| `./install.sh --replace` | Replaces a status line that you had before. Without this option, the installer keeps it and tells you. |
| `./install.sh --claude-only` | Does only Claude Code. |
| `./install.sh --codex-only` | Does only Codex. |

Before the installer changes a file, it copies the file to `<file>.before-tokenomics`. The files are `~/.claude/settings.json` (only the `statusLine` key changes) and `~/.codex/config.toml` (only the two footer keys in `[tui]` change).

## Uninstall

```bash
./install.sh --uninstall
```

This removes only the status lines that the installer added. It does not touch a status line that you set yourself, and it does not restore a backup. Add `--dry-run` to see what it will remove. After you uninstall, you can delete the directory.

## Other tools

Put `bin/` on your PATH to use the commands below: `export PATH="$PWD/bin:$PATH"`. Each command has `--help`.

### The `tokenomics` command

| Command | What it does |
|---|---|
| `tokenomics compare`, `tokenomics attempt` | Calculator: the cost of one attempt, meter by meter, and the cost per successful task of each option. |
| `tokenomics session` | Reads a Claude Code session: every lane, tokens by type, time, context, and cache. |
| `tokenomics codex` | Reads Codex usage: tokens by type, API and credit cost, time, context, and plan limits. |
| `tokenomics log`, `tokenomics index` | Read the results log of the baseline runs: medians, spread, and the index to the baseline. |
| `tokenomics rates` | Lists the rate profiles in `config/rates.json`. |

The code is in `tools/tokenomics/`. The unit tests are in `tools/tests/`.

### Other commands and files

| Item | What it does |
|---|---|
| `bin/run-baseline` | Runs a baseline task in Claude Code or Codex in a clean copy, checks the result, and adds one row to the results log. See `baseline/`. |
| `bin/claude-local`, `bin/codex-local` | Start Claude Code or Codex on a local Ollama model, for one process only. See `config/local/` and [setup/03-ollama.md](setup/03-ollama.md). |
| `workloads/` | The Status Board app (FastAPI) and its infrastructure (Terraform on AWS): the test workloads for the baseline tasks. |
| [`setup/`](setup/README.md) | Install guides for Claude Code, Codex, and Ollama, and the scripts that publish and deploy the workloads. |

## Safety

- The AWS stack in `workloads/status-board-infra` costs money while it exists. Tear it down after use (see [setup/04-workloads.md](setup/04-workloads.md)).
- Session transcripts (`*.jsonl`) can contain source code, paths, and secrets. Do not commit them. `tokenomics reduce` makes a copy with no text and no paths.
- If `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set, a tool can bill at API rates instead of your subscription.
