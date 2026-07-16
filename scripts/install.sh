#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${NANOBOT_LLM_WIKI_REPO:-https://github.com/yu-xin-c/nanobot-llm-wiki}"
WORKSPACE="${NANOBOT_WORKSPACE:-$HOME/.nanobot/workspace}"

step() {
  printf '\n==> %s\n' "$1"
}

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

if ! command -v uv >/dev/null 2>&1; then
  fail "uv is required. Install it from https://docs.astral.sh/uv/ first."
fi

if [[ -n "${NANOBOT_LLM_WIKI_PACKAGE:-}" ]]; then
  PACKAGE_SPEC="$NANOBOT_LLM_WIKI_PACKAGE"
elif [[ "$REPO_URL" == git+* ]]; then
  PACKAGE_SPEC="nanobot-llm-wiki @ $REPO_URL"
else
  PACKAGE_SPEC="nanobot-llm-wiki @ git+$REPO_URL"
fi

TOOL_DIR="$(uv tool dir)"
TOOL_BIN="$(uv tool dir --bin)"
NANOBOT_ENV="$TOOL_DIR/nanobot-ai"

if [[ -x "$NANOBOT_ENV/bin/python" ]]; then
  NANOBOT_PYTHON="$NANOBOT_ENV/bin/python"
elif [[ -x "$NANOBOT_ENV/Scripts/python.exe" ]]; then
  NANOBOT_PYTHON="$NANOBOT_ENV/Scripts/python.exe"
else
  step "Installing NanoBot"
  uv tool install --quiet nanobot-ai
  if [[ -x "$NANOBOT_ENV/bin/python" ]]; then
    NANOBOT_PYTHON="$NANOBOT_ENV/bin/python"
  elif [[ -x "$NANOBOT_ENV/Scripts/python.exe" ]]; then
    NANOBOT_PYTHON="$NANOBOT_ENV/Scripts/python.exe"
  else
    fail "NanoBot's uv environment was not created at $NANOBOT_ENV."
  fi
fi

step "Installing NanoBot LLM Wiki into NanoBot's environment"
uv pip install \
  --quiet \
  --python "$NANOBOT_PYTHON" \
  --upgrade-package nanobot-llm-wiki \
  "$PACKAGE_SPEC"

if [[ -x "$NANOBOT_ENV/bin/nanobot-wiki" ]]; then
  WIKI_EXECUTABLE="$NANOBOT_ENV/bin/nanobot-wiki"
elif [[ -x "$NANOBOT_ENV/Scripts/nanobot-wiki.exe" ]]; then
  WIKI_EXECUTABLE="$NANOBOT_ENV/Scripts/nanobot-wiki.exe"
else
  fail "nanobot-wiki was installed but its command could not be found."
fi

mkdir -p "$TOOL_BIN"
if [[ "$WIKI_EXECUTABLE" == *.exe ]]; then
  cp -f "$WIKI_EXECUTABLE" "$TOOL_BIN/nanobot-wiki.exe"
else
  CLI_LINK="$TOOL_BIN/nanobot-wiki"
  if [[ -e "$CLI_LINK" && ! -L "$CLI_LINK" ]]; then
    fail "$CLI_LINK already exists and is not a symbolic link."
  fi
  ln -sfn "$WIKI_EXECUTABLE" "$CLI_LINK"
fi

step "Initializing the Wiki workspace"
"$WIKI_EXECUTABLE" --workspace "$WORKSPACE" install >/dev/null

step "Running installation diagnostics"
"$WIKI_EXECUTABLE" --workspace "$WORKSPACE" doctor

printf '\nNanoBot LLM Wiki is ready.\n'
printf 'Workspace: %s\n' "$WORKSPACE"

case ":${PATH:-}:" in
  *":$TOOL_BIN:"*) ;;
  *)
    printf '\nAdd uv tools to your PATH once, then open a new shell:\n'
    printf '  uv tool update-shell\n'
    ;;
esac

printf '\nNext commands:\n'
printf '  nanobot gateway\n'
printf '  nanobot-wiki ui --open\n\n'
