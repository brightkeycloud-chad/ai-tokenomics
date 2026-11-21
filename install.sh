#!/usr/bin/env bash
# Install the two status lines of the ai-tokenomics toolkit:
#   - the Claude Code status line (tools/statusline/statusline.py)
#   - the Codex footer (the [tui] status_line list in ~/.codex/config.toml)
#
# The script installs a status line only for a tool that is on your PATH.
# It asks no questions. You can run it again: it changes only what is not
# installed. It works on macOS and Linux with bash 3.2 or later.
#
# Run ./install.sh --help for the options.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATUSLINE="$ROOT/tools/statusline/statusline.py"
INSTALLER="$ROOT/tools/statusline/install.py"
SAMPLE="$ROOT/tools/statusline/sample-status.json"
FOOTER="$ROOT/setup/codex-footer.py"
MARKER="tools/statusline/statusline.py"   # in the command of our Claude Code status line
CLAUDE_SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
CODEX_CONFIG="${CODEX_HOME:-$HOME/.codex}/config.toml"
BACKUP_SUFFIX=".before-tokenomics"

DRY_RUN=0
UNINSTALL=0
REPLACE=0
DO_CLAUDE=1
DO_CODEX=1
FAILED=0

usage() {
  cat <<EOF
Usage: ./install.sh [OPTIONS]

Install the ai-tokenomics status lines for Claude Code and Codex. The script
installs a status line only for a tool that is on your PATH.

Options:
  --dry-run       Show what the script would do. Change nothing.
  --uninstall     Remove the status lines that this script installed. Keep
                  all other settings. Restore no backup.
  --replace       Replace a status line that is already there and that this
                  script did not install. The script makes a backup first.
  --claude-only   Do only the Claude Code status line.
  --codex-only    Do only the Codex footer.
  -h, --help      Show this help.

Files that the script changes:
  Claude Code: ${CLAUDE_SETTINGS/#$HOME/~} (the "statusLine" key)
  Codex:       ${CODEX_CONFIG/#$HOME/~} (status_line and status_line_use_colors in [tui])
Before it changes a file, the script copies it to <file>$BACKUP_SUFFIX.

Set PYTHON=/path/to/python3 to use a specific Python (3.9 or later).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --replace) REPLACE=1 ;;
    --claude-only) DO_CODEX=0 ;;
    --codex-only) DO_CLAUDE=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown option: $1" >&2; echo >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ "$DO_CLAUDE" -eq 0 ] && [ "$DO_CODEX" -eq 0 ]; then
  echo "error: use --claude-only or --codex-only, not both." >&2
  exit 2
fi

# Show paths below the home directory as ~/...
tilde() { sed "s#$HOME#~#g"; }
indent() { sed 's/^/    /'; }
say() { printf '%s\n' "$*"; }
header() { printf '\n== %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Python 3.9 or later
# ---------------------------------------------------------------------------
find_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3 /usr/bin/python3 python; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PY="$(find_python)"; then
  echo "error: this toolkit needs Python 3.9 or later, and the script did not find it." >&2
  echo "       Install Python 3 (macOS: xcode-select --install, or python.org)." >&2
  echo "       Then run ./install.sh again, or set PYTHON=/path/to/python3." >&2
  exit 1
fi

MODE="Install"
[ "$UNINSTALL" -eq 1 ] && MODE="Uninstall"
[ "$DRY_RUN" -eq 1 ] && MODE="$MODE (dry run: nothing changes)"
say "ai-tokenomics: $MODE"
say "Python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"
say "Toolkit: $(printf '%s' "$ROOT" | tilde)"
if [ -n "${VIRTUAL_ENV:-}" ] && [ -z "${PYTHON:-}" ] && [ "${PY#"$VIRTUAL_ENV"}" != "$PY" ]; then
  say "Warning: a virtual environment is active. The status line uses its Python."
  say "         Deactivate it first, or set PYTHON=/usr/bin/python3, if you will delete it later."
fi

# Copy a file to <file>.before-tokenomics before the first change.
backup() {
  local file="$1"
  [ -f "$file" ] || return 0
  cp -p "$file" "$file$BACKUP_SUFFIX"
  say "Backup: $(printf '%s' "$file$BACKUP_SUFFIX" | tilde)"
}

# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

# Print "none", "ours", "other", or "invalid" for the statusLine in the settings.
claude_state() {
  "$PY" - "$CLAUDE_SETTINGS" "$MARKER" <<'PYEOF'
import json, pathlib, sys
path, marker = pathlib.Path(sys.argv[1]), sys.argv[2]
try:
    text = path.read_text() if path.exists() else ""
    data = json.loads(text) if text.strip() else {}
except (OSError, ValueError):
    print("invalid"); sys.exit()
line = data.get("statusLine") if isinstance(data, dict) else None
if not isinstance(data, dict):
    print("invalid")
elif line is None:
    print("none")
elif isinstance(line, dict) and marker in str(line.get("command", "")):
    print("ours")
else:
    print("other")
PYEOF
}

claude_current() {
  "$PY" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])).get("statusLine")))' \
    "$CLAUDE_SETTINGS"
}

# Show the status line for a sample session. This proves that the script runs.
claude_sample() {
  say "Test: the status line for a sample session (tools/statusline/sample-status.json):"
  "$PY" "$STATUSLINE" --plain <"$SAMPLE" | indent
}

install_claude() {
  header "Claude Code status line"
  local state args
  state="$(claude_state)"
  args=()
  # A later install.py can take --python. Then the settings keep the stable
  # path of the Python that this script found.
  if "$PY" "$INSTALLER" --help 2>/dev/null | grep -q -- '--python'; then
    args+=(--python "$PY")
  fi
  case "$state" in
    invalid)
      say "Not installed: $(printf '%s' "$CLAUDE_SETTINGS" | tilde) is not valid JSON. Correct the file, then run ./install.sh again."
      FAILED=1; return ;;
    other)
      if [ "$REPLACE" -eq 0 ]; then
        say "Not installed: Claude Code already has a different status line:"
        claude_current | indent
        say "To replace it, run ./install.sh --replace. The script makes a backup first."
        FAILED=1; return
      fi
      args+=(--replace) ;;
  esac
  if [ "$DRY_RUN" -eq 1 ]; then
    say "Would set \"statusLine\" in $(printf '%s' "$CLAUDE_SETTINGS" | tilde):"
    "$PY" "$INSTALLER" --dry-run ${args[@]+"${args[@]}"} --settings "$CLAUDE_SETTINGS" 2>/dev/null \
      | "$PY" -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get("statusLine"), indent=2))' \
      | tilde | indent
  else
    [ "$state" = "ours" ] || backup "$CLAUDE_SETTINGS"
    "$PY" "$INSTALLER" ${args[@]+"${args[@]}"} --settings "$CLAUDE_SETTINGS" | tilde
  fi
  claude_sample
}

uninstall_claude() {
  header "Claude Code status line"
  case "$(claude_state)" in
    none) say "No status line in $(printf '%s' "$CLAUDE_SETTINGS" | tilde). Nothing to remove." ;;
    invalid) say "Not changed: $(printf '%s' "$CLAUDE_SETTINGS" | tilde) is not valid JSON."; FAILED=1 ;;
    other)
      say "Not changed: the status line in $(printf '%s' "$CLAUDE_SETTINGS" | tilde) is not from this toolkit:"
      claude_current | indent ;;
    ours)
      if [ "$DRY_RUN" -eq 1 ]; then
        say "Would remove the toolkit status line from $(printf '%s' "$CLAUDE_SETTINGS" | tilde):"
        claude_current | tilde | indent
      else
        "$PY" "$INSTALLER" --remove --settings "$CLAUDE_SETTINGS" | tilde
      fi ;;
  esac
}

# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

# Print "none", "ours", or "other" for the footer in the Codex config.
codex_state() {
  local lines
  if ! lines="$("$PY" "$FOOTER" show "$CODEX_CONFIG")"; then
    echo none
  elif printf '%s\n' "$lines" | grep -qv '# ai-tokenomics'; then
    echo other
  else
    echo ours
  fi
}

install_codex() {
  header "Codex footer"
  local state
  state="$(codex_state)"
  if [ "$state" = "other" ] && [ "$REPLACE" -eq 0 ]; then
    say "Not installed: Codex already has a footer that this toolkit did not set:"
    "$PY" "$FOOTER" show "$CODEX_CONFIG" | indent
    say "To replace it, run ./install.sh --replace. The script makes a backup first."
    FAILED=1; return
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    "$PY" "$FOOTER" --set --dry-run "$CODEX_CONFIG" | tilde
    return
  fi
  "$PY" "$FOOTER" --set "$CODEX_CONFIG" | tilde
  say "The footer lines in [tui] now:"
  "$PY" "$FOOTER" show "$CODEX_CONFIG" | indent
}

uninstall_codex() {
  header "Codex footer"
  case "$(codex_state)" in
    none) say "No footer in $(printf '%s' "$CODEX_CONFIG" | tilde). Nothing to remove." ;;
    *)
      local out
      if [ "$DRY_RUN" -eq 1 ]; then
        out="$("$PY" "$FOOTER" remove --ours --dry-run "$CODEX_CONFIG")" || true
      else
        out="$("$PY" "$FOOTER" remove --ours "$CODEX_CONFIG")" || true
      fi
      printf '%s\n' "$out" | tilde ;;
  esac
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
SKIPPED=()
if [ "$DO_CLAUDE" -eq 1 ]; then
  if [ "$UNINSTALL" -eq 1 ]; then
    uninstall_claude
  elif command -v claude >/dev/null 2>&1; then
    install_claude
  else
    SKIPPED+=("Claude Code: the claude command is not on your PATH.")
  fi
else
  SKIPPED+=("Claude Code: you gave --codex-only.")
fi

if [ "$DO_CODEX" -eq 1 ]; then
  if [ "$UNINSTALL" -eq 1 ]; then
    uninstall_codex
  elif command -v codex >/dev/null 2>&1; then
    install_codex
  else
    SKIPPED+=("Codex: the codex command is not on your PATH.")
  fi
else
  SKIPPED+=("Codex: you gave --claude-only.")
fi

if [ ${#SKIPPED[@]} -gt 0 ]; then
  header "Skipped"
  for reason in "${SKIPPED[@]}"; do say "$reason"; done
  case "${SKIPPED[*]}" in
    *"not on your PATH"*) say "To add a status line later, install the tool, then run ./install.sh again." ;;
  esac
fi

header "Next"
if [ "$DRY_RUN" -eq 1 ]; then
  say "Dry run: no file changed. Run the command again without --dry-run to make the changes."
elif [ "$UNINSTALL" -eq 1 ]; then
  say "Your other settings did not change. The backups (*$BACKUP_SUFFIX) stay; delete them when you do not need them."
else
  say "Start a new Claude Code or Codex session to see the status line."
  say "Keep this directory at $(printf '%s' "$ROOT" | tilde)."
  say "The Claude Code status line runs statusline.py from it. If you move or delete it, the status line stops."
  say "To remove the status lines: $(printf '%s' "$ROOT" | tilde)/install.sh --uninstall"
fi
exit "$FAILED"
