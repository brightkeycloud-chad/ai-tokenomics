# Install Claude Code (Claude Pro subscription)

This guide installs Claude Code on macOS and signs it in with a personal **Claude Pro** subscription. Instructions were checked on 2026-11-14 against <https://code.claude.com/docs/en/setup>.

## Requirements

| Item | Requirement |
|---|---|
| Operating system | macOS 13.0 or later (Windows and Linux are also supported; see the link above) |
| Hardware | 4 GB of RAM or more, Apple Silicon or Intel |
| Account | Claude Pro, Max, Team, Enterprise, or a Console (API) account. The free claude.ai plan does not include Claude Code. |
| Network | An internet connection |

## 1. Install

Use the native installer. It updates itself in the background.

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

The installer shows no progress while it downloads. When it finishes, open a **new** terminal window.

Alternative: Homebrew. Homebrew installations do not update themselves.

```bash
brew install --cask claude-code      # stable channel, about one week behind
brew upgrade claude-code             # run this to update
```

## 2. Verify the installation

```bash
claude --version     # prints a version, for example 2.1.323 (Claude Code)
claude doctor        # prints installation and settings diagnostics
```

If the shell says `claude: command not found`, the install directory (`~/.local/bin`) is not on your PATH. Open a new terminal, or see <https://code.claude.com/docs/en/troubleshoot-install>.

## 3. Sign in with your Claude Pro subscription

1. Make sure that no API key is set in your shell. If `ANTHROPIC_API_KEY` is set, Claude Code can use the key and bill at API rates instead of your subscription.

   ```bash
   unset ANTHROPIC_API_KEY
   ```

   Also remove the variable from your shell profile (`~/.zshrc`) if it is there.

2. Start Claude Code in any directory and follow the browser prompts. Sign in with the Claude account that has the Pro subscription.

   ```bash
   claude
   ```

3. In the session, type `/status`. Make sure that it shows your Claude account, not an API key.
4. Type `/usage`. It shows how much of your 5-hour and weekly limits you have used. Lesson 2 uses this view.
5. Type `/exit`.

To change accounts later, use `/logout` and `/login` in a session.

## 4. Make sure that print mode works

Lesson 3 runs the agent without the interactive screen. Make sure that this works now. Run the command in any git repository:

```bash
claude -p "Reply with the single word: ready" --output-format json
```

The output is one JSON object. Find the `result`, `total_cost_usd`, and `usage` fields. On a subscription, `total_cost_usd` is an estimate at list price. You do not pay it; the request counts against your plan limits.

## What to know for this course

| Item | What it means for the demos |
|---|---|
| Shared limits | Claude Code shares your usage limits with Claude chat. Do not record demos after a heavy chat session. |
| Cache lifetime | On a subscription, the prompt cache for the main conversation lasts one hour. On an API key, it lasts five minutes. Lesson 4 uses this difference. |
| Default model | Claude Code uses Opus by default. Lesson 3 sets the model explicitly for each run. |
| Updates | Native installs update automatically. A new version can change a default. Lesson 3 records the version for each run. |

## Uninstall

```bash
rm -f ~/.local/bin/claude
rm -rf ~/.local/share/claude
```

To remove settings and history as well: `rm -rf ~/.claude ~/.claude.json`. This deletes all settings, MCP server configuration, and session history.
