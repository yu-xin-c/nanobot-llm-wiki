"""Workspace installer for NanoBot LLM Wiki."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot_llm_wiki.storage import WikiStore

SEED_PAGES = [
    {
        "title": "User Profile",
        "content": "## Summary\n\nDurable user preferences and profile facts go here.\n",
        "page_type": "profile",
        "tags": ["user", "profile"],
    },
    {
        "title": "Projects",
        "content": "## Active Projects\n\nTrack long-running projects, repositories, and decisions here.\n",
        "page_type": "project-index",
        "tags": ["project", "index"],
    },
    {
        "title": "Open Questions",
        "content": "## Open Questions\n\nUse this page for unresolved facts that need confirmation.\n",
        "page_type": "questions",
        "tags": ["questions"],
    },
]


def install_workspace(workspace: str | Path | None = None, *, force_skill: bool = False) -> dict[str, Any]:
    store = WikiStore(workspace)
    created_pages: list[str] = []
    for seed in SEED_PAGES:
        if store.get_page(seed["title"]):
            continue
        page = store.upsert_page(**seed)
        created_pages.append(page.id)

    memory_path = store.write_memory_bridge()
    skill_path = store.write_skill(force=force_skill)
    config_path = write_user_config(store)
    status = store.status()
    return {
        **status,
        "created_pages": created_pages,
        "memory_path": str(memory_path),
        "skill_path": str(skill_path),
        "config_path": str(config_path),
    }


def write_user_config(store: WikiStore) -> Path:
    config_path = store.wiki_dir / "config.toml"
    if config_path.exists():
        return config_path
    config_path.write_text(
        "\n".join(
            [
                "# NanoBot LLM Wiki configuration",
                f'workspace = "{store.workspace}"',
                "auto_dream = true",
                "context_budget_tokens = 2500",
                'embedding_provider = "none"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path
