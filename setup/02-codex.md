# Install Codex (ChatGPT Plus subscription)

This guide installs the OpenAI Codex CLI on macOS and signs it in with a personal **ChatGPT Plus** subscription. ChatGPT Pro works the same way, with higher limits. Instructions were checked on 2026-11-14 against the Codex README (<https://github.com/openai/codex>) and <https://learn.chatgpt.com/docs/codex/cli>.

## Requirements

| Item | Requirement |
|---|---|
| Operating system | macOS or Linux for the standalone installer. Windows has its own installer. |
| Account | ChatGPT Plus, Pro, Business, Edu, or Enterprise. An OpenAI API key is an alternative, billed at API rates. |
| Network | An internet connection |

## 1. Install

Use the standalone installer from OpenAI. Run the same command again to update.

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Alternatives:

```bash
npm install -g @openai/codex       # needs Node.js. Use the scoped name: the unscoped "codex" package is a different project.
brew install --cask codex          # Homebrew. Update with: brew upgrade --cask codex
```

## 2. Verify the installation

Open a new terminal window, then:

```bash
codex --version
```

If the shell says `codex: command not found`, open a new terminal so that your PATH updates.

## 3. Sign in with your ChatGPT Plus subscription

1. Make sure that no API key is set in your shell, so that Codex uses your subscription.

   ```bash
   unset OPENAI_API_KEY
   ```

   Also remove the variable from your shell profile (`~/.zshrc`) if it is there.

2. Open a project directory and start Codex:

   ```bash
   codex
   ```

3. Select **Sign in with ChatGPT**. Complete the sign-in in the browser with the account that has the Plus subscription.
4. In the session, type `/status`. Make sure that it shows your ChatGPT sign-in and your remaining limits.
5. Type `/usage` to see your token activity. Lesson 2 uses these views.
6. In a browser that is signed in to the same ChatGPT account, open the usage dashboard: <https://chatgpt.com/settings/usage>. It shows the allowance that Codex shares with other products. It does not show Codex usage separately.

   The address `chatgpt.com/codex/settings/usage`, which some OpenAI pages still link to, redirects to the ChatGPT home page. If the address changes again, open the profile menu, then **Settings**, then **Usage**.
7. Exit the session.

Codex keeps its settings in `~/.codex/config.toml`. Lessons 3 and 6 add course settings to that file.

## 4. Make sure that non-interactive mode works

Lesson 3 runs Codex without the interactive screen. Make sure that this works now. Run the command in a git repository:

```bash
codex exec --json "Reply with the single word: ready"
```

The output is one JSON event on each line. Find the events that report token usage: input, cached input, output, and reasoning tokens. The `tokenomics` toolkit reads these events from Lesson 2.

If Codex prints `Not inside a trusted directory and --skip-git-repo-check was not specified.`, the current directory is not a git repository. Go to one, for example `cd ~/tokenomics/tokenomics-status-board-app`, and run the command again. A headless run can change files without a prompt, so Codex requires git, where you can see and undo the changes.

## What to know for this course

| Item | What it means for the demos |
|---|---|
| Shared limits | Codex shares one allowance with ChatGPT Work, workspace agents, and ChatGPT for Excel. Chat conversations do not count. Do not use those products just before or during a recording. The dashboard shows only the shared total, so measure a Codex run with `/status` before and after it. |
| No dollar figure | Codex reports the share of your limits that you used, not a dollar cost. Lesson 2 explains how to convert usage with the rate card. |
| Model names | Record the full model name for each run. Aliases change when OpenAI ships a new model. |
| Updates | Re-run the installer to update. Record the version for each run. |

## Uninstall

Remove the binary with the same tool that installed it (`npm uninstall -g @openai/codex` or `brew uninstall --cask codex`). The standalone installer prints its install location; delete the `codex` binary from that location. To remove settings and history: `rm -rf ~/.codex`.
