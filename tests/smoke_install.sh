#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/nanobot-wiki-install.XXXXXX")"
PACKAGE_SPEC="nanobot-llm-wiki @ file://$REPO_ROOT"

cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

run_installer() {
  local scenario="$1"
  env \
    HOME="$TMP_ROOT/$scenario/home" \
    UV_TOOL_DIR="$TMP_ROOT/$scenario/tools" \
    UV_TOOL_BIN_DIR="$TMP_ROOT/$scenario/bin" \
    NANOBOT_WORKSPACE="$TMP_ROOT/$scenario/workspace" \
    NANOBOT_LLM_WIKI_PACKAGE="$PACKAGE_SPEC" \
    bash "$REPO_ROOT/scripts/install.sh"
}

assert_installation() {
  local scenario="$1"
  local tool_env="$TMP_ROOT/$scenario/tools/nanobot-ai"
  local workspace="$TMP_ROOT/$scenario/workspace"

  test -x "$TMP_ROOT/$scenario/bin/nanobot"
  test -x "$TMP_ROOT/$scenario/bin/nanobot-wiki"
  test -f "$workspace/memory/wiki/links.jsonl"
  "$TMP_ROOT/$scenario/bin/nanobot-wiki" --workspace "$workspace" doctor --json \
    > "$TMP_ROOT/$scenario/doctor.json"
  "$tool_env/bin/python" - "$TMP_ROOT/$scenario/doctor.json" <<'PY'
import json
import sys
from importlib.metadata import entry_points

report = json.loads(open(sys.argv[1], encoding="utf-8").read())
assert report["ok"] is True, report

expected = {
    "wiki_doctor",
    "wiki_forget",
    "wiki_import",
    "wiki_link",
    "wiki_read",
    "wiki_search",
    "wiki_status",
    "wiki_unlink",
    "wiki_upsert",
}
registered = {
    item.name
    for item in entry_points(group="nanobot.tools")
    if item.value.startswith("nanobot_llm_wiki.tools:")
}
assert expected == registered, (expected, registered)
PY
}

mkdir -p "$TMP_ROOT/fresh/home"
run_installer fresh
assert_installation fresh

printf '\nUser-owned memory line.\n' >> "$TMP_ROOT/fresh/workspace/memory/MEMORY.md"
mkdir -p "$TMP_ROOT/fresh/workspace/memory/wiki/pages"
printf '# User page\n\nKeep this content.\n' \
  > "$TMP_ROOT/fresh/workspace/memory/wiki/pages/user-page.md"
"$TMP_ROOT/fresh/bin/nanobot-wiki" \
  --workspace "$TMP_ROOT/fresh/workspace" reindex >/dev/null
before_page="$(cksum "$TMP_ROOT/fresh/workspace/memory/wiki/pages/user-page.md")"

run_installer fresh
assert_installation fresh
grep -q 'User-owned memory line.' "$TMP_ROOT/fresh/workspace/memory/MEMORY.md"
test "$before_page" = "$(cksum "$TMP_ROOT/fresh/workspace/memory/wiki/pages/user-page.md")"

mkdir -p "$TMP_ROOT/existing/home"
env \
  HOME="$TMP_ROOT/existing/home" \
  UV_TOOL_DIR="$TMP_ROOT/existing/tools" \
  UV_TOOL_BIN_DIR="$TMP_ROOT/existing/bin" \
  uv tool install --quiet --with 'tomli==2.0.1' nanobot-ai

run_installer existing
assert_installation existing
uv pip show \
  --python "$TMP_ROOT/existing/tools/nanobot-ai/bin/python" \
  tomli >/dev/null 2>&1

env \
  HOME="$TMP_ROOT/existing/home" \
  UV_TOOL_DIR="$TMP_ROOT/existing/tools" \
  UV_TOOL_BIN_DIR="$TMP_ROOT/existing/bin" \
  NANOBOT_WORKSPACE="$TMP_ROOT/existing/workspace" \
  bash "$REPO_ROOT/scripts/uninstall.sh"

test -x "$TMP_ROOT/existing/bin/nanobot"
test ! -L "$TMP_ROOT/existing/bin/nanobot-wiki"
test -d "$TMP_ROOT/existing/workspace/memory/wiki"
test ! -e "$TMP_ROOT/existing/workspace/skills/llm-wiki/SKILL.md"
grep -q 'nanobot-llm-wiki:start' "$TMP_ROOT/existing/workspace/memory/MEMORY.md" && exit 1
uv pip show \
  --python "$TMP_ROOT/existing/tools/nanobot-ai/bin/python" \
  tomli >/dev/null 2>&1
if uv pip show \
  --python "$TMP_ROOT/existing/tools/nanobot-ai/bin/python" \
  nanobot-llm-wiki >/dev/null 2>&1; then
  exit 1
fi

printf '\nInstaller smoke test passed.\n'
