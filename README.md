# NanoBot LLM Wiki

`nanobot-llm-wiki` turns NanoBot's long-term memory into a local, inspectable Wiki made of Markdown pages and a SQLite index.

The first release is intentionally boring to deploy: no Postgres, no vector database, no external service. It installs NanoBot tools, initializes `memory/wiki/`, adds a workspace skill, and writes a small `memory/MEMORY.md` bridge so NanoBot knows to use the Wiki.

## Install From GitHub

NanoBot discovers tool plugins from the same Python environment it runs in. If you run NanoBot through `uv tool`, install NanoBot with this plugin attached:

```bash
uv tool install --force --with git+https://github.com/yu-xin-c/nanobot-llm-wiki nanobot-ai
uvx --from git+https://github.com/yu-xin-c/nanobot-llm-wiki nanobot-wiki install
nanobot gateway
```

For a virtualenv or source checkout:

```bash
python -m pip install git+https://github.com/yu-xin-c/nanobot-llm-wiki
nanobot-wiki install --workspace ~/.nanobot/workspace
nanobot gateway
```

There is also a helper script:

```bash
curl -fsSL https://raw.githubusercontent.com/yu-xin-c/nanobot-llm-wiki/main/scripts/install.sh | bash
```

## What Gets Installed

```text
~/.nanobot/workspace/
  memory/
    MEMORY.md                  # bridge block pointing NanoBot at the Wiki
    wiki/
      wiki.db                  # SQLite search/index database
      config.toml              # plugin settings scoped to this workspace
      pages/*.md               # human-editable Wiki pages
      archive/*.md             # archived forgotten pages
      .cursor                  # history ingestion cursor
  skills/
    llm-wiki/SKILL.md          # always-on guidance for when to use Wiki tools
```

## NanoBot Tools

- `wiki_search(query, limit, tag)` searches Wiki pages.
- `wiki_read(selector)` reads a page by title, id, or alias.
- `wiki_upsert(title, content, ...)` creates, replaces, or appends to a page.
- `wiki_link(from_selector, to_selector, relation)` links two pages.
- `wiki_forget(selector, archive)` deletes or archives a page.
- `wiki_status()` reports storage paths and counts.

## CLI

```bash
nanobot-wiki install
nanobot-wiki status
nanobot-wiki search "project preference"
nanobot-wiki read "User Profile"
nanobot-wiki upsert "Current Project" --content "Building a NanoBot memory plugin."
nanobot-wiki dream --once
nanobot-wiki ui
nanobot-wiki doctor
```

The local UI listens on [http://127.0.0.1:8766](http://127.0.0.1:8766) by default:

```bash
nanobot-wiki ui --workspace ~/.nanobot/workspace
```

`dream --once` consumes new entries from `memory/history.jsonl` into a `Conversation Inbox` page. It is deterministic in this first release so it can run without API keys. The intended next step is a NanoBot core `memory_processors` extension point that lets this package replace or augment Dream with an LLM-driven Wiki maintainer.

## Local Development

From this repository:

```bash
PYTHONPATH=src:/path/to/nanobot pytest -q
ruff check src tests
```

## Design Goals

- One-command setup for normal NanoBot users.
- Local-first memory that users can inspect, edit, back up, and delete.
- Safe bootstrap path that does not replace NanoBot's built-in `MemoryStore`.
- A clean migration path toward a first-class NanoBot memory backend.
