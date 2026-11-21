# Install Ollama

This guide installs Ollama on a Mac with Apple Silicon. `bin/claude-local`, `bin/codex-local`, and `tokenomics ollama` use it to run a model on your own machine. Instructions were checked on 2026-11-14 against <https://docs.ollama.com/macos> and <https://docs.ollama.com/context-length>.

Do all the steps in this guide before you use a local model. Section 4 sets the context window. The tools need a window of 64K tokens or more.

## Requirements

| Item | Requirement |
|---|---|
| Operating system | macOS 14 Sonoma or later |
| Processor | Apple Silicon (M series) for GPU acceleration. An Intel Mac runs models on the CPU only, which is too slow for agent runs. |
| Memory | 32 GB of unified memory or more is recommended. With less, the main model can be too large. To find out if a model fits, run `tokenomics ollama fit MODEL`. |
| Disk | 35 GB or more of free space for the two models. Models are stored in `~/.ollama/models`. |

## 1. Install

1. Download Ollama for macOS from <https://ollama.com/download>.
2. Open `ollama.dmg` and drag the Ollama app to the **Applications** folder.
3. Start Ollama from Applications. If the `ollama` command is not on your PATH, the app asks for permission to create a link in `/usr/local/bin`. Accept.

## 2. Verify the installation

```bash
ollama --version      # prints the client and server versions
ollama list           # no models yet: prints only the column headings
ollama ps             # no models loaded: prints only the column headings
```

If `ollama --version` reports that it cannot connect to the server, start the Ollama app.

## 3. Pull the models

The tools read the model names from `config/local/ollama.json`:

| Setting in `ollama.json` | Value | Use |
|---|---|---|
| `model` | `qwen3.5:35b`, with thinking on | The main model. It is about 21 GB. |
| `fallback.model` | `gpt-oss:20b`, with thinking level `medium` | The fallback model, for `--fallback`. It is about 13 GB. |
| `context_length` | `65536` | The context window that the tools give to Claude Code and Codex. The Ollama server must serve the same window. |

Pull the two models:

```bash
ollama pull qwen3.5:35b
ollama pull gpt-oss:20b
```

## 4. Set the context window

By default, Ollama sets the context window from the GPU memory: 4K below 24 GiB, 32K from 24 to 48 GiB, and 256K at 48 GiB or more. A coding agent needs 64K or more. Set the window to 65536, the value of `context_length` in `config/local/ollama.json`.

1. Set the variable for the Ollama app:

   ```bash
   launchctl setenv OLLAMA_CONTEXT_LENGTH 65536
   ```

2. Quit the Ollama app. Use the Ollama icon in the menu bar, then **Quit Ollama**.
3. Start the Ollama app from Applications. The app reads the variable when it starts.

The `launchctl setenv` setting stays until you restart the Mac. After a restart, do steps 1 to 3 again. You can also set the context length in the settings of the Ollama app.

## 5. Check the context window

1. Load the main model with a short prompt:

   ```bash
   ollama run qwen3.5:35b "Reply with the single word: ready"
   ```

2. Show the loaded models:

   ```bash
   ollama ps
   ```

3. Make sure that the CONTEXT column shows `65536`.
4. Make sure that the PROCESSOR column shows `100% GPU`. If it shows a CPU share, the model and the window do not fit in the GPU memory. The model then runs slowly. Use the fallback model, or run `tokenomics ollama fit` to see the sizing.

If the CONTEXT column shows a smaller value, the app did not read the variable. Do steps 1 to 3 of section 4 again.

Ollama unloads a model 5 minutes after the last request. The next request loads the model again, and that adds time to the request. To measure this time, use `tokenomics ollama meters --cold`.

## Uninstall

Quit the Ollama app, then:

```bash
sudo rm -rf /Applications/Ollama.app
sudo rm /usr/local/bin/ollama
rm -rf "$HOME/Library/Application Support/Ollama"
rm -rf "$HOME/Library/Saved Application State/com.electron.ollama.savedState"
rm -rf "$HOME/Library/Caches/com.electron.ollama/"
rm -rf "$HOME/Library/Caches/ollama"
rm -rf "$HOME/Library/WebKit/com.electron.ollama"
rm -rf "$HOME/.ollama"      # deletes all downloaded models
```

To remove only one model and keep Ollama: `ollama rm <model>`.
