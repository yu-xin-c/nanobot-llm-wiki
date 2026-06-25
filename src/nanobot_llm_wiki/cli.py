"""Command-line interface for NanoBot LLM Wiki."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nanobot_llm_wiki.formatting import format_page, format_search_results, format_status
from nanobot_llm_wiki.installer import install_workspace
from nanobot_llm_wiki.paths import default_workspace
from nanobot_llm_wiki.storage import WikiStore
from nanobot_llm_wiki.ui import run_ui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nanobot-wiki", description="Manage NanoBot LLM Wiki memory.")
    parser.add_argument("--workspace", default=None, help="NanoBot workspace path.")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="Initialize Wiki memory in a NanoBot workspace.")
    install.add_argument("--force-skill", action="store_true", help="Overwrite generated llm-wiki skill.")

    sub.add_parser("doctor", help="Check paths and installation state.")
    sub.add_parser("status", help="Show Wiki status.")

    search = sub.add_parser("search", help="Search Wiki pages.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--tag", default=None)

    read = sub.add_parser("read", help="Read a Wiki page.")
    read.add_argument("selector")

    upsert = sub.add_parser("upsert", help="Create or update a Wiki page.")
    upsert.add_argument("title")
    upsert.add_argument("--content", required=True)
    upsert.add_argument("--tag", action="append", default=[])
    upsert.add_argument("--alias", action="append", default=[])
    upsert.add_argument("--type", default="note", dest="page_type")
    upsert.add_argument("--mode", choices=["replace", "append"], default="replace")

    forget = sub.add_parser("forget", help="Archive or delete a Wiki page.")
    forget.add_argument("selector")
    forget.add_argument("--delete", action="store_true", help="Delete instead of archiving.")

    dream = sub.add_parser("dream", help="Import new memory/history.jsonl entries.")
    dream.add_argument("--once", action="store_true", help="Run one deterministic ingestion pass.")
    dream.add_argument("--limit", type=int, default=50)

    ui = sub.add_parser("ui", help="Start the local Wiki management page.")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8766)
    ui.add_argument("--open", action="store_true", help="Open the page in the default browser.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser() if args.workspace else default_workspace()

    if args.command == "install":
        result = install_workspace(workspace, force_skill=args.force_skill)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    store = WikiStore(workspace)

    if args.command == "doctor":
        result = install_workspace(workspace)
        print("NanoBot LLM Wiki is installed.\n")
        print(format_status(result))
        return 0
    if args.command == "status":
        print(format_status(store.status()))
        return 0
    if args.command == "search":
        print(format_search_results(store.search(args.query, limit=args.limit, tag=args.tag)))
        return 0
    if args.command == "read":
        page = store.get_page(args.selector)
        if not page:
            print(f"Wiki page not found: {args.selector}", file=sys.stderr)
            return 1
        print(format_page(page))
        return 0
    if args.command == "upsert":
        page = store.upsert_page(
            title=args.title,
            content=args.content,
            page_type=args.page_type,
            tags=args.tag,
            aliases=args.alias,
            mode=args.mode,
        )
        store.write_memory_bridge()
        print(f"Saved {page.title} ({page.id})")
        return 0
    if args.command == "forget":
        page = store.forget_page(args.selector, archive=not args.delete)
        store.write_memory_bridge()
        print(f"Forgot {page.title} ({page.id})")
        return 0
    if args.command == "dream":
        if not args.once:
            print("Only --once is supported in this release.", file=sys.stderr)
            return 2
        result = store.ingest_history(limit=args.limit)
        store.write_memory_bridge()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "ui":
        run_ui(workspace, host=args.host, port=args.port, open_browser=args.open)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
