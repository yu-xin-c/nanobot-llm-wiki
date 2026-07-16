#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="${NANOBOT_WORKSPACE:-$HOME/.nanobot/workspace}"

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

if ! command -v uv >/dev/null 2>&1; then
  fail "uv is required. Install it from https://docs.astral.sh/uv/ first."
fi

TOOL_DIR="$(uv tool dir)"
TOOL_BIN="$(uv tool dir --bin)"
NANOBOT_ENV="$TOOL_DIR/nanobot-ai"

if [[ -x "$NANOBOT_ENV/bin/python" ]]; then
  NANOBOT_PYTHON="$NANOBOT_ENV/bin/python"
  WIKI_EXECUTABLE="$NANOBOT_ENV/bin/nanobot-wiki"
elif [[ -x "$NANOBOT_ENV/Scripts/python.exe" ]]; then
  NANOBOT_PYTHON="$NANOBOT_ENV/Scripts/python.exe"
  WIKI_EXECUTABLE="$NANOBOT_ENV/Scripts/nanobot-wiki.exe"
else
  fail "NanoBot's uv environment was not found at $NANOBOT_ENV."
fi

if [[ -x "$WIKI_EXECUTABLE" ]]; then
  "$WIKI_EXECUTABLE" --workspace "$WORKSPACE" uninstall >/dev/null
fi

if uv pip show --python "$NANOBOT_PYTHON" nanobot-llm-wiki >/dev/null 2>&1; then
  uv pip uninstall --quiet --python "$NANOBOT_PYTHON" nanobot-llm-wiki
fi

if [[ -L "$TOOL_BIN/nanobot-wiki" ]]; then
  rm -f "$TOOL_BIN/nanobot-wiki"
fi
rm -f "$TOOL_BIN/nanobot-wiki.exe"

printf '\nNanoBot LLM Wiki was removed from NanoBot.\n'
printf 'Wiki data was kept at: %s/memory/wiki\n' "$WORKSPACE"
printf 'NanoBot and its other plugins were not changed.\n\n'
