# Course setup

Do these steps before you follow the demos. The guides assume macOS, a personal **Claude Pro** subscription, and a personal **ChatGPT Plus** subscription.

| Step | Guide | Needed from |
|---|---|---|
| 1 | [Install Claude Code](01-claude-code.md) | Lesson 2 |
| 2 | [Install Codex](02-codex.md) | Lesson 2 |
| 3 | [Install Ollama](03-ollama.md) | Lesson 6 (install before demo 6.3) |
| 4 | [Publish and deploy the workloads](04-workloads.md) | Publish: any time. Deploy: demo 5.2 only. |

Lesson 1 needs only Python 3.10 or later.

## Tools for the whole course

| Tool | Check | Used for |
|---|---|---|
| Python 3.10 or later | `python3 --version` | The `tokenomics` toolkit |
| Git | `git --version` | Baseline runs and the workloads |
| GitHub CLI | `gh auth status` | Publishing the workloads |
| AWS CLI | `aws sts get-caller-identity` | Bootstrap, demo 5.2, and cleanup |
| Terraform 1.10 or later | `terraform version` | The infrastructure workload |

## Scripts

| Script | What it does |
|---|---|
| [`../install.sh`](../install.sh) | Installs the Claude Code status line and the Codex footer, for each tool that is on your PATH. `--dry-run` changes nothing. `--uninstall` removes only what it installed. `--help` shows all options. |
| [`codex-footer.py`](codex-footer.py) | Shows, sets, or removes the Codex footer in `~/.codex/config.toml`. `--set` writes the default items, or the items that you give (`--set ITEM,ITEM`). It makes a backup (`config.toml.before-tokenomics`) first, and changes only the two footer keys in `[tui]`. `remove --ours` removes only the lines that `--set` wrote. `install.sh` uses it. |
| [`publish-workloads.sh`](publish-workloads.sh) | Creates your two GitHub repositories from `workloads/`, and writes the bootstrap `terraform.tfvars` |
| [`configure-repos.sh`](configure-repos.sh) | Sets the GitHub repository variables from the bootstrap outputs, and enables GitHub Actions |
| [`deploy-stack.sh`](deploy-stack.sh) | Deploys the stack and the application through GitHub Actions, then checks the board. `--dry-run` checks the setup only. |
| [`teardown-stack.sh`](teardown-stack.sh) | Destroys the stack, removes the leftover task definitions, and checks that nothing that costs money remains. `--dry-run` shows what exists. |

All scripts are safe to run again. `lib-stack.sh` holds functions that the deploy and teardown scripts share.
