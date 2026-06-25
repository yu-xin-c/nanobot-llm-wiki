#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${NANOBOT_LLM_WIKI_REPO:-https://github.com/yu-xin-c/nanobot-llm-wiki}"
WORKSPACE="${NANOBOT_WORKSPACE:-$HOME/.nanobot/workspace}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it from https://docs.astral.sh/uv/ first." >&2
  exit 1
fi

echo "Installing NanoBot with nanobot-llm-wiki attached..."
uv tool install --force --with "git+$REPO_URL" nanobot-ai

echo "Initializing LLM Wiki workspace at $WORKSPACE..."
uvx --from "git+$REPO_URL" nanobot-wiki --workspace "$WORKSPACE" install

cat <<'MSG'

NanoBot LLM Wiki is installed.

Start NanoBot with:
  nanobot gateway

MSG
