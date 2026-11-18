#!/usr/bin/env bash
# CEI Life Sciences Bot — guided deployment (F1, B47).
#
# Thin wrapper: creates/reuses a local virtualenv, installs deploy/requirements.txt only when it
# has changed, then hands off to the Textual TUI (deploy/cei_deploy) which collects deployment
# configuration and runs the actual CodeBuild deployment pipeline. Every real build/deploy step
# still runs inside CodeBuild (buildspec.yml), never on your machine. Works in bash on Linux,
# macOS, CloudShell and Git Bash.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.deploy-venv"
REQUIREMENTS_FILE="$SCRIPT_DIR/deploy/requirements.txt"
STAMP_FILE="$VENV_DIR/.requirements.sha256"

# ADR: python3 first, then python (CloudShell/Linux/macOS ship python3; Git Bash on Windows
# commonly only has "python" on PATH) | Rationale: deploy/cei_deploy is Python-3.9-compatible, so
# any interpreter found just needs to satisfy that floor
find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        printf '%s' "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON_BIN="$(find_python)" || { echo "Python >= 3.9 is required (python3 or python on PATH)." >&2; exit 1; }

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating the deploy tool's virtualenv at $VENV_DIR..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# ADR: Scripts/ on Windows (Git Bash still uses the Windows venv layout), bin/ elsewhere
if [ -x "$VENV_DIR/Scripts/python.exe" ]; then
  VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
else
  VENV_PYTHON="$VENV_DIR/bin/python"
fi

REQUIREMENTS_SHA="$("$PYTHON_BIN" -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$REQUIREMENTS_FILE")"
if [ ! -f "$STAMP_FILE" ] || [ "$(cat "$STAMP_FILE" 2>/dev/null)" != "$REQUIREMENTS_SHA" ]; then
  echo "Installing deploy tool dependencies..."
  "$VENV_PYTHON" -m pip install -q -r "$REQUIREMENTS_FILE"
  printf '%s' "$REQUIREMENTS_SHA" > "$STAMP_FILE"
fi

cd "$SCRIPT_DIR/deploy"
exec "$VENV_PYTHON" -m cei_deploy "$@"
