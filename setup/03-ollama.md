# Install Ollama

This guide installs Ollama on a Mac with Apple Silicon. Lessons 6 and 7 use it to run a model on your own machine. Instructions were checked on 2026-11-14 against <https://docs.ollama.com/macos> and <https://docs.ollama.com/context-length>.

Demo 6.3 does not show this installation. Install Ollama before you record or follow demo 6.3. The demo sets the context window.

## Requirements

| Item | Requirement |
|---|---|
| Operating system | macOS 14 Sonoma or later |
| Processor | Apple Silicon (M series) for GPU acceleration. An Intel Mac runs models on the CPU only, which is too slow for the demos. |
| Memory | 32 GB of unified memory or more is recommended. With less, you can follow the concepts and calculations in Lessons 6 and 7. |
| Disk | 20 GB or more of free space for models. Models are stored in `~/.ollama/models`. |

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

Pull the models before demo 6.3. The main model is about 21 GB. The fallback model is about 13 GB.

```bash
ollama pull qwen3.5:35b
ollama pull gpt-oss:20b
```

Do not change the context window now. Demo 6.3 sets it, and records the values. For reference:

| Setting | Default | What the course uses |
|---|---|---|
| Context window | Set from GPU memory: 4K below 24 GiB, 32K from 24 to 48 GiB, 256K at 48 GiB or more | At least 64K, set in the app settings or with `OLLAMA_CONTEXT_LENGTH` |
| Model unload | 5 minutes after the last request | Recorded, because a reload adds time to the next run |
| Settings for the app | Environment variables for the app are set with `launchctl setenv`, then the app is restarted | Demo 6.3 |

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
