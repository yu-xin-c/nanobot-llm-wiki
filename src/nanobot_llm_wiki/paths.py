"""Path helpers for NanoBot LLM Wiki."""

from __future__ import annotations

import os
from pathlib import Path


def expand_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def default_workspace() -> Path:
    """Return NanoBot's default workspace, honoring common env overrides."""
    for env_name in ("NANOBOT_LLM_WIKI_WORKSPACE", "NANOBOT_WORKSPACE"):
        value = os.environ.get(env_name)
        if value:
            return expand_path(value)
    return expand_path("~/.nanobot/workspace")
