"""Command-line interface for NanoBot LLM Wiki."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nanobot_llm_wiki.diagnostics import diagnose_workspace
from nanobot_llm_wiki.formatting import (
    format_doctor,
    format_page,
    format_search_results,
    format_status,
)
from nanobot_llm_wiki.installer import install_workspace
from nanobot_llm_wiki.paths import default_workspace
from nanobot_llm_wiki.storage import WikiStore
from nanobot_llm_wiki.ui import run_ui


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nanobot-wiki", description="Manage NanoBot LLM Wiki memory.")
    parser.add_argument("--workspace", default=None, help="NanoBot workspace path.")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="Initialize Wiki memory in a NanoBot workspace.")
    install.add_argument("--force-skill", action="store_true", help="Overwrite generated llm-wiki skill.")

    doctor = sub.add_parser("doctor", help="Check paths and installation state without changing files.")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable diagnostics.")
    sub.add_parser("status", help="Show Wiki status.")
    sub.add_parser("reindex", help="Rebuild the SQLite index from Markdown pages.")

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

    import_cmd = sub.add_parser("import", help="Import a local text knowledge base into Wiki pages.")
    import_cmd.add_argument("path", help="File or directory to import.")
    import_cmd.add_argument("--index-title", default=None, help="Title for the generated index page.")
    import_cmd.add_argument("--tag", action="append", default=[], help="Extra tag for imported pages.")
    import_cmd.add_argument("--type", default="knowledge-doc", dest="page_type")
    import_cmd.add_argument("--relation", default="contains", help="Graph relation from index to pages.")
    import_cmd.add_argument("--max-bytes", type=int, default=512_000, help="Max bytes per imported file.")

    forget = sub.add_parser("forget", help="Archive or delete a Wiki page.")
    forget.add_argument("selector")
    forget.add_argument("--delete", action="store_true", help="Delete instead of archiving.")

    link = sub.add_parser("link", help="Create a graph link between two Wiki pages.")
    link.add_argument("from_selector")
    link.add_argument("to_selector")
    link.add_argument("--relation", default="related")

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

    if args.command == "doctor":
        result = diagnose_workspace(workspace)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_doctor(result))
        return 0 if result["ok"] else 1

    store = WikiStore(workspace)
    if args.command == "status":
        print(format_status(store.status()))
        return 0
    if args.command == "reindex":
        print(json.dumps(store.reindex_from_disk(), ensure_ascii=False, indent=2))
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
        try:
            page = store.upsert_page(
                title=args.title,
                content=args.content,
                page_type=args.page_type,
                tags=args.tag,
                aliases=args.alias,
                mode=args.mode,
            )
        except ValueError as exc:
            print(f"Error: {_error_text(exc)}", file=sys.stderr)
            return 1
        store.write_memory_bridge()
        print(f"Saved {page.title} ({page.id})")
        return 0
    if args.command == "import":
        try:
            result = store.import_knowledge_base(
                args.path,
                index_title=args.index_title,
                tags=args.tag,
                page_type=args.page_type,
                relation=args.relation,
                max_bytes=args.max_bytes,
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            print(f"Error: {_error_text(exc)}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "raw_path": result.raw_path,
                    "index_page": {
                        "id": result.index_page.id,
                        "title": result.index_page.title,
                    },
                    "imported": [
                        {
                            "raw_path": item.path,
                            "id": item.page.id,
                            "title": item.page.title,
                        }
                        for item in result.imported
                    ],
                    "skipped": result.skipped,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "forget":
        try:
            page = store.forget_page(args.selector, archive=not args.delete)
        except KeyError as exc:
            print(f"Error: {_error_text(exc)}", file=sys.stderr)
            return 1
        store.write_memory_bridge()
        print(f"Forgot {page.title} ({page.id})")
        return 0
    if args.command == "link":
        try:
            from_page, to_page = store.link_pages(args.from_selector, args.to_selector, args.relation)
        except KeyError as exc:
            print(f"Error: {_error_text(exc)}", file=sys.stderr)
            return 1
        print(f"Linked {from_page.title} -> {to_page.title} ({args.relation or 'related'})")
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
