"""Tiny local Web UI for NanoBot LLM Wiki."""

from __future__ import annotations

import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from nanobot_llm_wiki.diagnostics import diagnose_workspace
from nanobot_llm_wiki.installer import install_workspace
from nanobot_llm_wiki.storage import WikiPage, WikiStore


def _page_to_dict(page: WikiPage) -> dict[str, Any]:
    return {
        "id": page.id,
        "title": page.title,
        "content": page.content,
        "page_type": page.page_type,
        "tags": page.tags,
        "aliases": page.aliases,
        "confidence": page.confidence,
        "created_at": page.created_at,
        "updated_at": page.updated_at,
        "source_cursors": page.source_cursors,
    }


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _link_to_dict(link: Any) -> dict[str, Any]:
    return {
        "from_id": link.from_id,
        "from_title": link.from_title,
        "to_id": link.to_id,
        "to_title": link.to_title,
        "relation": link.relation,
        "created_at": link.created_at,
    }


def build_server(
    workspace: str | Path | None,
    host: str,
    port: int,
    *,
    read_only: bool = False,
) -> ThreadingHTTPServer:
    store = WikiStore(workspace)

    class WikiUIHandler(BaseHTTPRequestHandler):
        server_version = "NanoBotWikiUI/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_html(self) -> None:
            data = INDEX_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSON body") from exc
            if not isinstance(parsed, dict):
                raise ValueError("JSON body must be an object")
            return parsed

        def _selector_from_path(self, prefix: str) -> str:
            value = self.path[len(prefix):].split("?", 1)[0]
            return unquote(value).strip("/")

        def _reject_read_only_write(self) -> bool:
            if not read_only:
                return False
            self._send_json(
                {"error": "This Wiki UI is running in read-only mode."},
                HTTPStatus.METHOD_NOT_ALLOWED,
            )
            return True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html()
                return
            if parsed.path == "/api/status":
                status = store.status()
                status["read_only"] = read_only
                self._send_json(status)
                return
            if parsed.path == "/api/doctor":
                self._send_json(diagnose_workspace(store.workspace))
                return
            if parsed.path == "/api/graph":
                query = parse_qs(parsed.query)
                limit = int((query.get("limit") or ["200"])[0])
                self._send_json(store.graph(limit=limit))
                return
            if parsed.path == "/api/links":
                self._send_json({"links": [_link_to_dict(link) for link in store.list_links()]})
                return
            if parsed.path == "/api/pages":
                query = parse_qs(parsed.query)
                limit = int((query.get("limit") or ["200"])[0])
                self._send_json({"pages": [_page_to_dict(page) for page in store.list_pages(limit=limit)]})
                return
            if parsed.path == "/api/search":
                query = parse_qs(parsed.query)
                q = (query.get("q") or [""])[0]
                tag = (query.get("tag") or [None])[0]
                limit = int((query.get("limit") or ["20"])[0])
                results = [
                    {"score": result.score, "page": _page_to_dict(result.page)}
                    for result in store.search(q, limit=limit, tag=tag)
                ]
                self._send_json({"results": results})
                return
            if parsed.path.startswith("/api/pages/"):
                selector = self._selector_from_path("/api/pages/")
                page = store.get_page(selector)
                if not page:
                    self._send_json({"error": "page not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_json({"page": _page_to_dict(page)})
                return
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self._reject_read_only_write():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/install":
                self._send_json(install_workspace(store.workspace))
                return
            if parsed.path == "/api/reindex":
                self._send_json(store.reindex_from_disk())
                return
            if parsed.path == "/api/import":
                try:
                    body = self._read_json()
                    import_path = str(body.get("path") or "").strip()
                    if not import_path:
                        raise ValueError("path is required")
                    result = store.import_knowledge_base(
                        import_path,
                        index_title=str(body.get("index_title") or "").strip() or None,
                        tags=_split_csv(body.get("tags")),
                        page_type=str(body.get("page_type") or "knowledge-doc").strip()
                        or "knowledge-doc",
                        relation=str(body.get("relation") or "contains").strip() or "contains",
                        max_bytes=int(body.get("max_bytes") or 512_000),
                    )
                except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({
                    "raw_path": result.raw_path,
                    "index_page": _page_to_dict(result.index_page),
                    "imported": [
                        {"raw_path": item.path, "page": _page_to_dict(item.page)}
                        for item in result.imported
                    ],
                    "skipped": result.skipped,
                })
                return
            if parsed.path == "/api/links":
                try:
                    body = self._read_json()
                    from_page, to_page = store.link_pages(
                        str(body.get("from_selector") or "").strip(),
                        str(body.get("to_selector") or "").strip(),
                        str(body.get("relation") or "related").strip() or "related",
                    )
                except (KeyError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({
                    "from_page": _page_to_dict(from_page),
                    "to_page": _page_to_dict(to_page),
                })
                return
            if parsed.path != "/api/pages":
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            try:
                body = self._read_json()
                page = store.upsert_page(
                    title=str(body.get("title") or "").strip(),
                    content=str(body.get("content") or "").strip(),
                    page_id=str(body.get("id") or "").strip() or None,
                    page_type=str(body.get("page_type") or "note").strip() or "note",
                    tags=_split_csv(body.get("tags")),
                    aliases=_split_csv(body.get("aliases")),
                    confidence=float(body.get("confidence") or 0.7),
                    source_cursors=[
                        int(item)
                        for item in body.get("source_cursors", [])
                        if str(item).strip()
                    ],
                    mode=str(body.get("mode") or "replace"),
                )
                store.write_memory_bridge()
            except (KeyError, TypeError, ValueError) as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"page": _page_to_dict(page)})

        def do_DELETE(self) -> None:
            if self._reject_read_only_write():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/links":
                try:
                    body = self._read_json()
                    relation_value = body.get("relation")
                    relation = (
                        str(relation_value).strip() or "related"
                        if relation_value is not None
                        else None
                    )
                    from_page, to_page, removed = store.unlink_pages(
                        str(body.get("from_selector") or "").strip(),
                        str(body.get("to_selector") or "").strip(),
                        relation,
                    )
                except (KeyError, ValueError) as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({
                    "from_page": _page_to_dict(from_page),
                    "to_page": _page_to_dict(to_page),
                    "removed": removed,
                })
                return
            if not parsed.path.startswith("/api/pages/"):
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            query = parse_qs(parsed.query)
            archive = (query.get("archive") or ["true"])[0].lower() not in {"0", "false", "no"}
            selector = self._selector_from_path("/api/pages/")
            try:
                page = store.forget_page(selector, archive=archive)
                store.write_memory_bridge()
            except (KeyError, ValueError) as exc:
                status = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
                self._send_json({"error": str(exc)}, status)
                return
            self._send_json({"page": _page_to_dict(page), "archived": archive})

    return ThreadingHTTPServer((host, port), WikiUIHandler)


def run_ui(
    workspace: str | Path | None,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    open_browser: bool = False,
    read_only: bool = False,
) -> None:
    server = build_server(workspace, host, port, read_only=read_only)
    url = f"http://{server.server_address[0]}:{server.server_address[1]}"
    print(f"NanoBot LLM Wiki UI: {url}")
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping NanoBot LLM Wiki UI.")
    finally:
        server.server_close()


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NanoBot LLM Wiki</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f2f4f3;
      --surface: #ffffff;
      --surface-soft: #f7f8f7;
      --surface-strong: #e9edeb;
      --line: #dde3df;
      --line-strong: #b9c4be;
      --text: #17201c;
      --muted: #65716b;
      --subtle: #8a958f;
      --teal: #17786e;
      --indigo: #5c55c7;
      --danger: #b4233f;
      --shadow: 0 18px 42px rgba(38, 49, 44, 0.09);
      --shadow-soft: 0 5px 18px rgba(38, 49, 44, 0.055);
    }
    * { box-sizing: border-box; }
    html {
      min-height: 100%;
      background: var(--bg);
    }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    button, input, textarea, select {
      font: inherit;
    }
    button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      border: 1px solid var(--line);
      background: var(--surface);
      color: var(--text);
      border-radius: 6px;
      min-height: 36px;
      padding: 8px 12px;
      cursor: pointer;
      transition: border-color 120ms ease, background 120ms ease, transform 120ms ease, box-shadow 120ms ease;
      white-space: nowrap;
    }
    button:hover {
      border-color: var(--line-strong);
      background: #f6f8f7;
      box-shadow: 0 3px 10px rgba(38, 49, 44, 0.07);
    }
    button:active {
      transform: translateY(1px);
    }
    button.primary {
      border-color: var(--teal);
      background: var(--teal);
      color: white;
    }
    button.primary:hover {
      border-color: #11675f;
      background: #11675f;
    }
    button.danger {
      border-color: #efb4c0;
      color: var(--danger);
    }
    button.ghost {
      background: transparent;
      border-color: transparent;
      color: var(--muted);
    }
    button.ghost:hover {
      background: rgba(255, 255, 255, 0.78);
      border-color: var(--line);
    }
    .button-mark {
      display: inline-grid;
      place-items: center;
      width: 18px;
      height: 18px;
      border-radius: 5px;
      background: rgba(15, 118, 110, 0.12);
      color: var(--teal);
      font-weight: 800;
    }
    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fcfdfc;
      color: var(--text);
      padding: 10px 11px;
      outline: none;
    }
    input:focus, textarea:focus {
      border-color: var(--indigo);
      box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.12);
    }
    textarea {
      min-height: 468px;
      resize: vertical;
      line-height: 1.45;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
    }
    .app {
      display: grid;
      grid-template-columns: minmax(286px, 328px) minmax(0, 1fr);
      min-height: 100vh;
    }
    .sidebar {
      border-right: 1px solid var(--line);
      background: #f8faf8;
      padding: 16px;
      min-width: 0;
      height: 100vh;
      position: sticky;
      top: 0;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .workspace {
      padding: 24px min(3vw, 36px) 36px;
      min-width: 0;
    }
    .top {
      display: grid;
      grid-template-columns: 38px minmax(0, 1fr);
      gap: 12px;
      align-items: flex-start;
    }
    .brand-mark {
      display: grid;
      place-items: center;
      width: 38px;
      height: 38px;
      flex: 0 0 38px;
      border: 1px solid rgba(23, 120, 110, 0.25);
      border-radius: 8px;
      background: #e8f2ef;
      color: var(--teal);
      font-weight: 800;
      font-size: 13px;
    }
    .brand {
      font-weight: 700;
      font-size: 18px;
      flex: 1;
      line-height: 1.2;
      min-width: 0;
    }
    .brand-subtitle {
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }
    .side-actions {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr)) minmax(48px, auto);
      gap: 6px;
      grid-column: 1 / -1;
      width: 100%;
    }
    .side-actions button {
      min-width: 0;
      min-height: 34px;
      padding-inline: 8px;
      background: rgba(255, 255, 255, 0.7);
    }
    .side-actions .language-toggle {
      min-width: 48px;
      color: var(--teal);
      font-weight: 750;
    }
    .search {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
    }
    .search input {
      min-height: 40px;
      background: var(--surface);
    }
    .status {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.66);
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .status span {
      display: grid;
      gap: 2px;
      min-height: 50px;
      border: 0;
      border-right: 1px solid var(--line);
      border-radius: 0;
      background: transparent;
      padding: 8px 9px;
    }
    .status span:last-child {
      border-right: 0;
    }
    .status strong {
      color: var(--text);
      font-size: 18px;
      line-height: 1;
    }
    .status em {
      font-style: normal;
      text-transform: uppercase;
      font-size: 10px;
      color: var(--subtle);
    }
    .list-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      margin-top: 2px;
    }
    .list {
      display: grid;
      gap: 5px;
      flex: 1;
      min-height: 0;
      max-height: none;
      align-content: start;
      overflow: auto;
      padding-right: 2px;
    }
    .item {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      justify-content: stretch;
      align-items: stretch;
      gap: 5px;
      width: 100%;
      text-align: left;
      background: rgba(255, 255, 255, 0.62);
      border: 1px solid transparent;
      border-radius: 8px;
      padding: 10px 11px 10px 14px;
      box-shadow: none;
      position: relative;
      overflow: hidden;
      min-height: 78px;
    }
    .item::before {
      content: "";
      position: absolute;
      left: 0;
      top: 10px;
      bottom: 10px;
      width: 3px;
      border-radius: 999px;
      background: var(--item-color, var(--indigo));
    }
    .item:hover {
      background: var(--surface);
      border-color: var(--line);
      transform: none;
    }
    .item.active {
      border-color: rgba(92, 85, 199, 0.32);
      background: #ffffff;
      box-shadow: 0 8px 20px rgba(92, 85, 199, 0.09);
    }
    .item-title {
      font-weight: 650;
      overflow-wrap: anywhere;
      line-height: 1.25;
    }
    .item-meta {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .item-preview {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      display: -webkit-box;
      -webkit-line-clamp: 1;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .item-tags {
      display: flex;
      gap: 4px;
      flex-wrap: wrap;
    }
    .item-match {
      color: #3f4f49;
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .item-actions {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 2px;
    }
    .item-actions .item-action {
      min-height: 28px;
      border-color: var(--line);
      background: rgba(255, 255, 255, 0.76);
      color: var(--muted);
      font-size: 12px;
      padding: 4px 8px;
    }
    .tag {
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      border-radius: 6px;
      background: #edf1ef;
      color: #53615a;
      font-size: 11px;
      padding: 2px 7px;
    }
    .workbar {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 16px;
      max-width: 1400px;
    }
    .eyebrow {
      color: var(--teal);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      margin-bottom: 6px;
    }
    .eyebrow-line {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .read-only-badge {
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      border: 1px solid #b9d8d2;
      border-radius: 4px;
      background: #eaf6f3;
      color: #17695f;
      font-size: 11px;
      font-weight: 800;
      padding: 2px 7px;
    }
    .view-title {
      margin: 0;
      font-size: 27px;
      line-height: 1.1;
      max-width: 760px;
      overflow-wrap: anywhere;
    }
    .view-subtitle {
      margin-top: 5px;
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .view-switch {
      display: inline-flex;
      gap: 4px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #e8ecea;
      padding: 3px;
      flex: 0 0 auto;
    }
    .view-switch button {
      min-height: 30px;
      border: 0;
      background: transparent;
      padding: 5px 10px;
    }
    .view-switch button.active {
      background: var(--surface);
      color: var(--teal);
      box-shadow: 0 1px 3px rgba(38, 49, 44, 0.10);
    }
    .dashboard {
      display: grid;
      gap: 12px;
      max-width: 1400px;
    }
    .dashboard-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(280px, 0.55fr);
      gap: 12px;
      align-items: start;
    }
    .dashboard-panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 1px 0 rgba(38, 49, 44, 0.03);
      min-width: 0;
      overflow: hidden;
    }
    .dashboard-panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 54px;
      padding: 13px 16px;
      border-bottom: 1px solid var(--line);
      background: #fafbfa;
    }
    .dashboard-panel-title {
      font-weight: 800;
      overflow-wrap: anywhere;
    }
    .dashboard-panel-kicker {
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .metric-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      border-bottom: 1px solid var(--line);
    }
    .metric {
      display: grid;
      gap: 6px;
      min-height: 78px;
      padding: 14px 16px;
      border-right: 1px solid var(--line);
    }
    .metric:last-child {
      border-right: 0;
    }
    .metric strong {
      color: var(--text);
      font-size: 25px;
      line-height: 1;
      font-variant-numeric: tabular-nums;
    }
    .metric span {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }
    .health-actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      flex-wrap: wrap;
    }
    .health-badge {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      color: var(--muted);
      font-size: 12px;
      font-weight: 750;
      padding: 4px 9px;
    }
    .health-badge[data-health="healthy"] {
      border-color: rgba(15, 118, 110, 0.28);
      background: rgba(15, 118, 110, 0.08);
      color: #0b625d;
    }
    .health-badge[data-health="attention"] {
      border-color: rgba(180, 83, 9, 0.28);
      background: rgba(245, 158, 11, 0.10);
      color: #92400e;
    }
    .health-badge[data-health="unhealthy"] {
      border-color: rgba(185, 28, 28, 0.28);
      background: rgba(220, 38, 38, 0.08);
      color: #991b1b;
    }
    .health-summary {
      display: grid;
      border-bottom: 1px solid var(--line);
      background: #fafbfa;
    }
    .health-check {
      display: grid;
      grid-template-columns: 10px minmax(0, 1fr) auto;
      align-items: center;
      gap: 10px;
      min-height: 48px;
      padding: 10px 16px;
      border-bottom: 1px solid var(--line);
    }
    .health-check:last-child {
      border-bottom: 0;
    }
    .health-indicator {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #0f766e;
    }
    .health-check[data-status="warning"] .health-indicator {
      background: #d97706;
    }
    .health-check[data-status="error"] .health-indicator {
      background: #dc2626;
    }
    .health-copy {
      min-width: 0;
    }
    .health-label {
      color: var(--text);
      font-size: 13px;
      font-weight: 750;
      overflow-wrap: anywhere;
    }
    .health-message {
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .health-check button {
      min-height: 30px;
      padding: 5px 9px;
    }
    .quick-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      padding: 12px 16px;
    }
    .dashboard-list {
      display: grid;
    }
    .dashboard-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      justify-content: stretch;
      align-items: stretch;
      gap: 5px;
      width: 100%;
      border: 0;
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      background: transparent;
      position: relative;
      padding: 12px 16px 12px 36px;
      text-align: left;
      white-space: normal;
    }
    .dashboard-row:last-child {
      border-bottom: 0;
    }
    .dashboard-row::before {
      content: "";
      position: absolute;
      left: 17px;
      top: 19px;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--row-color, var(--indigo));
      box-shadow: 0 0 0 3px rgba(92, 85, 199, 0.08);
    }
    .dashboard-row:hover {
      background: #f7f9f8;
      box-shadow: none;
      transform: none;
    }
    .dashboard-row-title {
      font-weight: 720;
      overflow-wrap: anywhere;
    }
    .dashboard-row-meta,
    .dashboard-row-preview {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .dashboard-tools {
      display: grid;
      gap: 14px;
      padding: 16px;
    }
    .tool-form {
      display: grid;
      gap: 10px;
    }
    .tool-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
    }
    .tool-result {
      min-height: 20px;
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .divider {
      height: 1px;
      background: var(--line);
    }
    .editor {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 300px;
      max-width: 1280px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .editor-main {
      display: grid;
      gap: 14px;
      padding: 20px;
      min-width: 0;
    }
    .section-label {
      color: var(--subtle);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }
    .row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 180px;
      gap: 12px;
    }
    .meta-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.5fr) minmax(140px, 0.5fr) minmax(120px, 0.35fr);
      gap: 12px;
    }
    .wide-field {
      grid-column: span 2;
    }
    .field input[type="number"] {
      font-variant-numeric: tabular-nums;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .field label {
      display: block;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 5px;
    }
    .message {
      min-height: 22px;
      color: var(--muted);
      font-size: 14px;
    }
    .page-inspector {
      border-left: 1px solid var(--line);
      background: var(--surface-soft);
      padding: 20px;
      min-width: 0;
    }
    .inspector-heading {
      font-size: 13px;
      font-weight: 800;
      color: var(--text);
      text-transform: uppercase;
      margin-bottom: 14px;
    }
    .signal-list {
      margin: 0;
      display: grid;
      gap: 12px;
    }
    .signal-list div {
      display: grid;
      gap: 3px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--line);
    }
    .signal-list dt {
      color: var(--subtle);
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
    }
    .signal-list dd {
      margin: 0;
      color: var(--text);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .signal-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 14px;
    }
    .hidden {
      display: none !important;
    }
    .dashboard-grid.single-column {
      grid-template-columns: minmax(0, 1fr);
    }
    #editor input[readonly],
    #editor textarea[readonly] {
      background: var(--surface-soft);
      color: var(--text-soft);
      cursor: text;
    }
    .graph-panel {
      display: grid;
      max-width: 1480px;
      border: 1px solid #2b3532;
      border-radius: 8px;
      background: #0c1211;
      color: #eff7f3;
      box-shadow: 0 24px 58px rgba(20, 29, 26, 0.20);
      overflow: hidden;
    }
    .graph-panel button {
      background: rgba(255, 255, 255, 0.055);
      border-color: rgba(194, 211, 204, 0.17);
      color: #edf8f3;
    }
    .graph-panel button:hover {
      background: rgba(255, 255, 255, 0.10);
      border-color: rgba(194, 211, 204, 0.32);
      box-shadow: none;
    }
    .graph-panel input,
    .graph-panel select {
      width: 100%;
      min-height: 36px;
      border: 1px solid rgba(194, 211, 204, 0.18);
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.05);
      color: #eef8f3;
      outline: none;
      padding: 8px 10px;
    }
    .graph-panel input::placeholder {
      color: rgba(224, 239, 233, 0.48);
    }
    .graph-panel input:focus,
    .graph-panel select:focus {
      border-color: #61cdbf;
      box-shadow: 0 0 0 3px rgba(97, 205, 191, 0.13);
    }
    .graph-panel select option {
      color: #111719;
      background: #ffffff;
    }
    .graph-panel .message {
      min-height: 0;
      padding: 0 16px 14px;
      color: rgba(224, 239, 233, 0.62);
      background: #0c1211;
    }
    .graph-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      padding: 13px 15px;
      border-bottom: 1px solid rgba(194, 211, 204, 0.13);
      background: #111817;
    }
    .graph-heading {
      display: grid;
      gap: 4px;
    }
    .graph-titleline {
      display: flex;
      align-items: center;
      gap: 9px;
      flex-wrap: wrap;
    }
    .graph-panel .section-label {
      color: rgba(218, 240, 232, 0.72);
    }
    .graph-perspective {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border: 1px solid rgba(97, 205, 191, 0.25);
      border-radius: 6px;
      background: rgba(97, 205, 191, 0.08);
      color: #9ce5dc;
      font-size: 11px;
      font-weight: 800;
      padding: 2px 8px;
      text-transform: uppercase;
    }
    .graph-stats {
      color: rgba(224, 239, 233, 0.62);
      font-size: 13px;
    }
    .graph-toolbar {
      justify-content: flex-end;
    }
    .graph-workbench {
      display: grid;
      grid-template-columns: 196px minmax(0, 1fr);
      min-height: 640px;
      background: #0c1211;
    }
    .graph-rail,
    .graph-inspector {
      min-width: 0;
      padding: 14px;
      background: #111817;
    }
    .graph-rail {
      border-right: 1px solid rgba(194, 211, 204, 0.12);
    }
    .graph-inspector {
      grid-column: 1 / -1;
      border-top: 1px solid rgba(194, 211, 204, 0.12);
    }
    .graph-panel-label {
      color: rgba(218, 240, 232, 0.66);
      font-size: 11px;
      font-weight: 850;
      letter-spacing: 0;
      text-transform: uppercase;
      margin-bottom: 9px;
    }
    .graph-control-block {
      display: grid;
      gap: 9px;
      padding-bottom: 15px;
      margin-bottom: 15px;
      border-bottom: 1px solid rgba(194, 211, 204, 0.10);
    }
    .graph-control-block:empty {
      display: block;
      height: 1px;
      padding: 0;
      margin: 15px 0;
    }
    .graph-search-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 7px;
    }
    .graph-search-row button,
    .graph-control-row button {
      min-height: 36px;
      padding: 7px 10px;
    }
    .graph-control-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 7px;
    }
    .graph-summary {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .graph-summary div {
      min-height: 54px;
      border: 1px solid rgba(194, 211, 204, 0.11);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.035);
      padding: 9px;
    }
    .graph-summary strong {
      display: block;
      color: #f6fffb;
      font-size: 19px;
      line-height: 1;
      font-variant-numeric: tabular-nums;
    }
    .graph-summary span {
      display: block;
      margin-top: 5px;
      color: rgba(224, 239, 233, 0.58);
      font-size: 11px;
      text-transform: uppercase;
    }
    .graph-legend {
      display: grid;
      gap: 7px;
    }
    .graph-legend button {
      display: flex;
      justify-content: flex-start;
      width: 100%;
      min-height: 34px;
      border-color: transparent;
      background: rgba(255, 255, 255, 0.025);
      color: rgba(239, 250, 246, 0.82);
      padding: 6px 8px;
      text-align: left;
    }
    .graph-legend button.active {
      border-color: rgba(97, 205, 191, 0.34);
      background: rgba(97, 205, 191, 0.10);
      color: #f6fffb;
    }
    .graph-legend span {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-width: 0;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      display: inline-block;
      flex: 0 0 9px;
    }
    .graph-shell {
      position: relative;
      min-height: 640px;
      border: 0;
      background-color: #0b1110;
      background-image:
        linear-gradient(rgba(135, 156, 148, 0.10) 1px, transparent 1px),
        linear-gradient(90deg, rgba(135, 156, 148, 0.10) 1px, transparent 1px);
      background-size: 40px 40px;
      overflow: hidden;
    }
    .graph-canvas-status {
      position: absolute;
      z-index: 2;
      top: 13px;
      left: 13px;
      right: 13px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      pointer-events: none;
    }
    .graph-canvas-status span {
      display: inline-flex;
      align-items: center;
      min-height: 25px;
      border: 1px solid rgba(194, 211, 204, 0.14);
      border-radius: 6px;
      background: rgba(11, 17, 16, 0.80);
      color: rgba(230, 247, 241, 0.72);
      font-size: 11px;
      font-weight: 760;
      padding: 4px 8px;
      backdrop-filter: blur(12px);
    }
    #graphSvg {
      display: block;
      width: 100%;
      height: 640px;
      touch-action: none;
      user-select: none;
      cursor: grab;
    }
    #graphSvg.panning {
      cursor: grabbing;
    }
    .graph-minimap {
      position: absolute;
      right: 14px;
      bottom: 14px;
      z-index: 3;
      width: 168px;
      height: 110px;
      border: 1px solid rgba(194, 211, 204, 0.17);
      border-radius: 8px;
      background: rgba(11, 17, 16, 0.86);
      box-shadow: 0 14px 34px rgba(0, 0, 0, 0.24);
      overflow: hidden;
      backdrop-filter: blur(14px);
    }
    #graphMiniMap {
      display: block;
      width: 100%;
      height: 100%;
      cursor: crosshair;
    }
    .minimap-link {
      stroke: rgba(177, 197, 189, 0.24);
      stroke-width: 1;
    }
    .minimap-node {
      fill: rgba(97, 205, 191, 0.72);
      stroke: rgba(239, 250, 246, 0.52);
      stroke-width: 0.8;
    }
    .minimap-node.active {
      fill: #fbbf24;
    }
    .minimap-viewport {
      fill: rgba(97, 205, 191, 0.10);
      stroke: #7ad8cc;
      stroke-width: 1.2;
    }
    .graph-link-glow {
      fill: none;
      stroke: #61cdbf;
      stroke-width: 6;
      stroke-linecap: round;
      opacity: 0.045;
    }
    .graph-link {
      fill: none;
      stroke: rgba(177, 197, 189, 0.52);
      stroke-width: 1.35;
      stroke-linecap: round;
      opacity: 0.78;
    }
    .graph-link.active {
      stroke: #7ad8cc;
      stroke-width: 2;
      opacity: 1;
    }
    .graph-link-glow.active {
      stroke: #7ad8cc;
      stroke-width: 8;
      opacity: 0.20;
    }
    .graph-link.muted,
    .graph-link-glow.muted,
    .graph-relation-pill.muted {
      opacity: 0.10;
    }
    .graph-relation-pill rect {
      fill: rgba(11, 17, 16, 0.94);
      stroke: rgba(194, 211, 204, 0.18);
      stroke-width: 1;
    }
    .graph-relation {
      fill: rgba(231, 242, 237, 0.76);
      font-size: 9.8px;
      font-weight: 700;
      text-anchor: middle;
      dominant-baseline: middle;
    }
    .graph-node {
      cursor: grab;
    }
    .graph-node.dragging {
      cursor: grabbing;
    }
    .graph-node .graph-hit {
      fill: transparent;
      stroke: transparent;
      stroke-width: 0;
      filter: none;
      pointer-events: all;
    }
    .graph-node .graph-dot-shadow {
      fill: #000000;
      opacity: 0.28;
    }
    .graph-node .graph-dot-halo {
      fill: none;
      stroke: var(--node-color, rgba(194, 211, 204, 0.34));
      stroke-width: 1;
      opacity: 0.20;
    }
    .graph-node .graph-dot {
      stroke: rgba(248, 253, 251, 0.82);
      stroke-width: 1.5;
    }
    .graph-node.active .graph-dot {
      stroke: #f6fffb;
      stroke-width: 2;
    }
    .graph-node.active .graph-dot-halo {
      stroke: var(--node-color, #7ad8cc);
      stroke-width: 1.6;
      opacity: 1;
    }
    .graph-node.neighbor .graph-dot-halo {
      stroke: var(--node-color, #61cdbf);
      opacity: 0.56;
    }
    .graph-node.muted {
      opacity: 0.20;
    }
    .graph-node .graph-title {
      fill: rgba(246, 255, 251, 0.92);
      font-size: 11.2px;
      font-weight: 720;
      text-anchor: middle;
      paint-order: stroke;
      stroke: rgba(11, 17, 16, 0.90);
      stroke-width: 2.8px;
      pointer-events: none;
    }
    .graph-node .graph-meta {
      fill: rgba(224, 239, 233, 0.54);
      font-size: 9.4px;
      text-anchor: middle;
      paint-order: stroke;
      stroke: rgba(11, 17, 16, 0.86);
      stroke-width: 2.2px;
      pointer-events: none;
    }
    #graphViewport.lod-minimal .graph-title,
    #graphViewport.lod-minimal .graph-meta,
    #graphViewport.lod-minimal .graph-relation-pill {
      display: none;
    }
    #graphViewport.lod-minimal .graph-node .graph-title {
      font-size: 11px;
    }
    #graphViewport.lod-compact .graph-meta,
    #graphViewport.lod-compact .graph-relation-pill {
      display: none;
    }
    .zoom-controls {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      border: 1px solid rgba(194, 211, 204, 0.16);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.055);
      padding: 3px;
    }
    .zoom-controls button {
      min-width: 34px;
      min-height: 30px;
      padding: 5px 8px;
      border: 0;
      background: transparent;
    }
    .zoom-controls button:hover {
      background: rgba(255, 255, 255, 0.13);
      box-shadow: none;
    }
    .zoom-value {
      min-width: 48px;
      color: rgba(224, 239, 233, 0.68);
      font-size: 12px;
      font-weight: 750;
      text-align: center;
      font-variant-numeric: tabular-nums;
    }
    .graph-inspector-title {
      display: flex;
      align-items: center;
      gap: 9px;
      margin: 0;
      color: #f6fffb;
      font-size: 18px;
      line-height: 1.18;
      overflow-wrap: anywhere;
    }
    .graph-inspector-title::before {
      content: "";
      width: 10px;
      height: 10px;
      flex: 0 0 10px;
      border-radius: 50%;
      background: var(--node-color, #61cdbf);
      box-shadow: 0 0 0 4px rgba(255, 255, 255, 0.055);
    }
    .graph-inspector-meta {
      margin-top: 7px;
      color: rgba(224, 239, 233, 0.60);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .graph-empty-state {
      display: grid;
      place-items: center;
      min-height: 164px;
      border: 1px solid rgba(194, 211, 204, 0.11);
      border-radius: 8px;
      color: rgba(224, 239, 233, 0.48);
      background: rgba(255, 255, 255, 0.04);
      font-size: 13px;
    }
    .graph-detail-list {
      display: grid;
      gap: 10px;
      margin: 15px 0;
    }
    .graph-detail-list div {
      display: grid;
      gap: 3px;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(194, 211, 204, 0.10);
    }
    .graph-detail-list dt {
      color: rgba(224, 239, 233, 0.48);
      font-size: 10px;
      font-weight: 850;
      text-transform: uppercase;
    }
    .graph-detail-list dd {
      margin: 0;
      color: rgba(244, 255, 251, 0.88);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .graph-tag-stack,
    .graph-link-stack {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 10px;
    }
    .graph-tag-stack .tag {
      background: rgba(97, 205, 191, 0.10);
      color: #9ce5dc;
    }
    .graph-link-stack button {
      justify-content: flex-start;
      width: 100%;
      min-height: 34px;
      color: rgba(244, 255, 251, 0.82);
      text-align: left;
      white-space: normal;
      overflow-wrap: anywhere;
    }
    .graph-link-stack .graph-empty-state {
      width: 100%;
      min-height: 56px;
    }
    @media (min-width: 1360px) {
      .graph-workbench {
        grid-template-columns: 190px minmax(0, 1fr) 238px;
      }
      .graph-inspector {
        grid-column: auto;
        border-left: 1px solid rgba(194, 211, 204, 0.12);
        border-top: 0;
      }
    }
    @media (min-width: 1840px) {
      .graph-workbench {
        grid-template-columns: 210px minmax(0, 1fr) 276px;
      }
    }
    @media (max-width: 980px) {
      .app { grid-template-columns: 1fr; }
      .sidebar {
        position: static;
        height: auto;
        padding: 14px;
        gap: 10px;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
      .list {
        display: flex;
        flex: none;
        max-height: none;
        overflow-x: auto;
        overflow-y: hidden;
        padding: 0 0 4px;
        scroll-snap-type: x proximity;
      }
      .item {
        flex: 0 0 min(274px, calc(100vw - 32px));
        min-height: 76px;
        scroll-snap-align: start;
      }
      .row { grid-template-columns: 1fr; }
      .meta-grid { grid-template-columns: 1fr; }
      .wide-field { grid-column: auto; }
      .dashboard-grid { grid-template-columns: 1fr; }
      .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .metric:nth-child(2n) { border-right: 0; }
      .metric:nth-child(-n + 2) { border-bottom: 1px solid var(--line); }
      .editor { grid-template-columns: 1fr; }
      .page-inspector { border-left: 0; border-top: 1px solid var(--line); }
      .workbar { display: grid; }
      .graph-workbench { grid-template-columns: 1fr; }
      .graph-shell { order: -1; }
      .graph-rail,
      .graph-inspector {
        grid-column: auto;
        border-left: 0;
        border-right: 0;
        border-bottom: 1px solid rgba(194, 211, 204, 0.12);
      }
      .graph-shell,
      #graphSvg {
        min-height: 560px;
        height: 560px;
      }
    }
    @media (min-width: 621px) and (max-width: 980px) {
      .top {
        grid-template-columns: 38px minmax(160px, 1fr) minmax(244px, auto);
        align-items: center;
      }
      .side-actions {
        grid-column: auto;
        width: auto;
      }
    }
    @media (max-width: 620px) {
      .workspace { padding: 18px; }
      .dashboard-panel-header { align-items: flex-start; flex-wrap: wrap; }
      .health-actions { justify-content: flex-start; }
      .health-check { grid-template-columns: 10px minmax(0, 1fr); }
      .health-check button { grid-column: 2; justify-self: start; }
      .search { grid-template-columns: minmax(0, 1fr) auto; }
      .status { grid-template-columns: 1fr 1fr 1fr; }
      .view-switch { width: 100%; }
      .view-switch button { flex: 1; }
      .item { min-height: 70px; }
      .item-preview { display: none; }
      .tool-row { grid-template-columns: 1fr; }
      .editor-main, .page-inspector { padding: 16px; }
      textarea { min-height: 330px; }
      .graph-top { align-items: stretch; }
      .graph-toolbar { justify-content: flex-start; }
      .graph-minimap {
        width: 142px;
        height: 94px;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="top">
        <div class="brand-mark">NB</div>
        <div class="brand">NanoBot LLM Wiki<div class="brand-subtitle" data-i18n="brandSubtitle">本地记忆工作区</div></div>
        <div class="side-actions">
          <button id="homeBtn" class="ghost" title="概览" data-i18n="home" data-i18n-title="dashboard">首页</button>
          <button id="newBtn" title="新建页面" data-i18n-title="newPage"><span class="button-mark">+</span><span data-i18n="new">新建</span></button>
          <button id="graphBtn" class="ghost" title="图谱视图" data-i18n="graph" data-i18n-title="graphView">图谱</button>
          <button id="languageBtn" class="ghost language-toggle" type="button" title="Switch to Chinese" aria-label="Switch to Chinese">中文</button>
        </div>
      </div>
      <div class="search">
        <input id="searchInput" placeholder="搜索标题、标签和别名" data-i18n-placeholder="searchPlaceholder">
        <button id="searchBtn" data-i18n="search">搜索</button>
      </div>
      <div id="status" class="status"></div>
      <div class="list-heading"><span data-i18n="pages">页面</span><span id="listCount">0</span></div>
      <div id="pageList" class="list"></div>
    </aside>
    <main class="workspace">
      <div class="workbar">
        <div>
          <div class="eyebrow-line">
            <div class="eyebrow" data-i18n="knowledgeConsole">知识控制台</div>
            <span id="readOnlyBadge" class="read-only-badge hidden" data-i18n="readOnlyDemo">只读演示</span>
          </div>
          <h1 id="viewTitle" class="view-title">记忆概览</h1>
          <div id="viewSubtitle" class="view-subtitle">本地持久记忆</div>
        </div>
        <div class="view-switch">
          <button type="button" id="dashboardTab" class="active" data-i18n="dashboard">概览</button>
          <button type="button" id="editorTab" data-i18n="editor">编辑器</button>
          <button type="button" id="graphTab" data-i18n="graph">图谱</button>
        </div>
      </div>
      <section id="dashboardPanel" class="dashboard">
        <div class="dashboard-panel">
          <div class="dashboard-panel-header">
            <div>
              <div class="dashboard-panel-title" data-i18n="memoryHealth">记忆健康</div>
              <div id="workspacePath" class="dashboard-panel-kicker">工作区</div>
            </div>
            <div class="health-actions">
              <span id="healthBadge" class="health-badge">检查中</span>
              <button type="button" id="dashboardRefreshBtn" data-i18n="refresh">刷新</button>
            </div>
          </div>
          <div id="metricGrid" class="metric-grid"></div>
          <div id="healthSummary" class="health-summary"></div>
          <div class="quick-actions">
            <button type="button" id="quickNewBtn" class="primary" data-i18n="newMemory">新建记忆</button>
            <button type="button" id="quickGraphBtn" data-i18n="graph">图谱</button>
            <button type="button" id="quickReindexBtn" data-i18n="rebuildIndex">重建索引</button>
          </div>
        </div>
        <div id="dashboardGrid" class="dashboard-grid">
          <div class="dashboard-panel">
            <div class="dashboard-panel-header">
              <div>
                <div class="dashboard-panel-title" data-i18n="recentPages">最近页面</div>
                <div id="recentPagesMeta" class="dashboard-panel-kicker">0 个页面</div>
              </div>
              <button type="button" id="openFirstRecentBtn" data-i18n="openLatest">打开最新</button>
            </div>
            <div id="recentPages" class="dashboard-list"></div>
          </div>
          <div id="writeToolsPanel" class="dashboard-panel">
            <div class="dashboard-panel-header">
              <div>
                <div class="dashboard-panel-title" data-i18n="knowledgeBase">知识库</div>
                <div id="toolStatus" class="dashboard-panel-kicker">就绪</div>
              </div>
            </div>
            <div class="dashboard-tools">
              <div class="tool-form">
                <div class="field">
                  <label for="importPath" data-i18n="path">路径</label>
                  <input id="importPath">
                </div>
                <div class="field">
                  <label for="importTitle" data-i18n="indexTitle">索引标题</label>
                  <input id="importTitle">
                </div>
                <div class="field">
                  <label for="importTags" data-i18n="tags">标签</label>
                  <input id="importTags">
                </div>
                <div class="tool-row">
                  <input id="importMaxBytes" type="number" min="1" value="512000" aria-label="最大字节数" data-i18n-aria-label="maxBytes">
                  <button type="button" id="importBtn" class="primary" data-i18n="import">导入</button>
                </div>
                <div id="importResult" class="tool-result"></div>
              </div>
              <div class="divider"></div>
              <div class="tool-form">
                <button type="button" id="reindexBtn" data-i18n="rebuildIndex">重建索引</button>
                <div id="reindexResult" class="tool-result"></div>
              </div>
            </div>
          </div>
        </div>
      </section>
      <form id="editor" class="editor hidden">
        <div class="editor-main">
          <input type="hidden" id="pageId">
          <div class="section-label" data-i18n="pageDetails">页面信息</div>
          <div class="meta-grid">
            <div class="field wide-field">
              <label for="title" data-i18n="title">标题</label>
              <input id="title" required>
            </div>
            <div class="field">
              <label for="pageType" data-i18n="type">类型</label>
              <input id="pageType" value="note">
            </div>
            <div class="field wide-field">
              <label for="tags" data-i18n="tags">标签</label>
              <input id="tags">
            </div>
            <div class="field">
              <label for="confidence" data-i18n="confidence">置信度</label>
              <input id="confidence" type="number" min="0" max="1" step="0.01" value="0.70">
            </div>
          </div>
          <div class="field">
            <label for="aliases" data-i18n="aliases">别名</label>
            <input id="aliases">
          </div>
          <div class="field">
            <label for="content">Markdown</label>
            <textarea id="content"></textarea>
          </div>
          <div class="section-label" data-i18n="relationship">页面关系</div>
          <div class="row">
            <div class="field">
              <label for="linkTarget" data-i18n="linkTo">链接至</label>
              <input id="linkTarget" placeholder="页面标题或 ID" data-i18n-placeholder="pageTitleOrId">
            </div>
            <div class="field">
              <label for="linkRelation" data-i18n="relation">关系类型</label>
              <input id="linkRelation" value="related">
            </div>
          </div>
          <div class="toolbar">
            <button class="primary" type="submit" id="saveBtn" data-i18n="save">保存</button>
            <button type="button" id="linkBtn" data-i18n="link">创建关系</button>
            <button type="button" id="unlinkBtn" data-i18n="unlink">解除关系</button>
            <button type="button" id="refreshBtn" data-i18n="refresh">刷新</button>
            <button class="danger" type="button" id="archiveBtn" data-i18n="archive">归档</button>
          </div>
          <div id="message" class="message"></div>
        </div>
        <aside class="page-inspector">
          <div class="inspector-heading" data-i18n="pageSignals">页面信号</div>
          <dl class="signal-list">
            <div><dt>ID</dt><dd id="detailId">新页面</dd></div>
            <div><dt data-i18n="updated">更新时间</dt><dd id="detailUpdated">未保存</dd></div>
            <div><dt data-i18n="confidence">置信度</dt><dd id="detailConfidence">0.70</dd></div>
            <div><dt data-i18n="sources">来源</dt><dd id="detailSources">0 个游标</dd></div>
          </dl>
          <div id="detailTags" class="signal-tags"></div>
        </aside>
      </form>
      <section id="graphPanel" class="graph-panel hidden">
        <div class="graph-top">
          <div class="graph-heading">
            <div class="graph-titleline">
              <div class="section-label" data-i18n="knowledgeGraph">知识图谱</div>
              <span class="graph-perspective" data-i18n="localMemory">本地记忆</span>
            </div>
            <div id="graphStats" class="graph-stats">0 个页面 / 0 条关系</div>
          </div>
          <div class="toolbar graph-toolbar">
            <button type="button" id="backToEditorBtn" data-i18n="editor">编辑器</button>
            <button type="button" id="refreshGraphBtn" data-i18n="refresh">刷新</button>
            <button type="button" id="fitGraphBtn" data-i18n="fit">适应画布</button>
            <button type="button" id="resetGraphBtn" data-i18n="resetLayout">重置布局</button>
            <div class="zoom-controls" aria-label="图谱缩放控制" data-i18n-aria-label="graphZoomControls">
              <button type="button" id="zoomOutBtn" aria-label="缩小" data-i18n-aria-label="zoomOut">-</button>
              <span id="zoomValue" class="zoom-value">100%</span>
              <button type="button" id="zoomInBtn" aria-label="放大" data-i18n-aria-label="zoomIn">+</button>
              <button type="button" id="zoomResetBtn" aria-label="重置缩放" data-i18n-aria-label="resetZoom">1:1</button>
            </div>
          </div>
        </div>
        <div class="graph-workbench">
          <aside class="graph-rail">
            <div class="graph-panel-label" data-i18n="explore">探索</div>
            <div class="graph-control-block">
              <div class="graph-search-row">
                <input id="graphSearch" type="search" placeholder="搜索图谱" aria-label="搜索图谱" data-i18n-placeholder="searchGraph" data-i18n-aria-label="searchGraph">
                <button type="button" id="graphFocusBtn" data-i18n="focus">聚焦</button>
              </div>
              <div class="graph-control-row">
                <select id="graphTypeFilter" aria-label="按页面类型筛选" data-i18n-aria-label="filterByPageType">
                  <option value="all">全部类型</option>
                </select>
                <button type="button" id="graphClearFocusBtn" data-i18n="clear">清除</button>
              </div>
            </div>
            <div id="graphSummary" class="graph-summary"></div>
            <div class="graph-control-block"></div>
            <div class="graph-panel-label" data-i18n="legend">图例</div>
            <div id="graphLegend" class="graph-legend"></div>
          </aside>
          <div class="graph-shell" title="滚轮缩放，拖动空白区域平移，拖动节点调整布局。" data-i18n-title="graphHelp">
            <div class="graph-canvas-status">
              <span id="graphFocusLabel">全部节点</span>
              <span id="graphDensityLabel">0 个可见</span>
            </div>
            <svg id="graphSvg" role="img" aria-label="Wiki 页面图谱" aria-description="单击节点查看详情，双击节点在编辑器中打开。" data-i18n-aria-label="wikiPageGraph"></svg>
            <div class="graph-minimap" aria-label="图谱小地图" data-i18n-aria-label="graphMinimap">
              <svg id="graphMiniMap" role="img" aria-label="图谱小地图" data-i18n-aria-label="graphMinimap"></svg>
            </div>
          </div>
          <aside class="graph-inspector">
            <div class="graph-panel-label" data-i18n="inspector">检查器</div>
            <div id="graphInspectorEmpty" class="graph-empty-state" data-i18n="noNodeSelected">未选择节点</div>
            <div id="graphInspectorContent" class="hidden">
              <h2 id="graphDetailTitle" class="graph-inspector-title"></h2>
              <div id="graphDetailMeta" class="graph-inspector-meta"></div>
              <dl class="graph-detail-list">
                <div><dt>ID</dt><dd id="graphDetailId"></dd></div>
                <div><dt data-i18n="connections">连接数</dt><dd id="graphDetailDegree"></dd></div>
                <div><dt data-i18n="updated">更新时间</dt><dd id="graphDetailUpdated"></dd></div>
              </dl>
              <div id="graphDetailTags" class="graph-tag-stack"></div>
              <div class="graph-control-block"></div>
              <div class="graph-panel-label" data-i18n="linkedNodes">关联节点</div>
              <div id="graphDetailLinks" class="graph-link-stack"></div>
              <div class="graph-control-block"></div>
              <button type="button" id="openGraphNodeBtn" class="primary" data-i18n="openInEditor">在编辑器中打开</button>
            </div>
          </aside>
        </div>
        <div id="graphMessage" class="message"></div>
      </section>
    </main>
  </div>
  <script>
    const languageStoreKey = "nanobot_llm_wiki_language_v2";
    const translations = {
      zh: {
        brandSubtitle: "本地记忆工作区",
        home: "首页",
        dashboard: "概览",
        newPage: "新建页面",
        new: "新建",
        graph: "图谱",
        graphView: "图谱视图",
        searchPlaceholder: "搜索标题、标签和别名",
        search: "搜索",
        pages: "页面",
        links: "关系",
        types: "类型",
        cursor: "游标",
        knowledgeConsole: "知识控制台",
        readOnlyDemo: "只读演示",
        memoryDashboard: "记忆概览",
        localDurableMemory: "本地持久记忆",
        editor: "编辑器",
        memoryEditor: "记忆编辑器",
        memoryHealth: "记忆健康",
        workspace: "工作区",
        checking: "检查中",
        refresh: "刷新",
        newMemory: "新建记忆",
        rebuildIndex: "重建索引",
        recentPages: "最近页面",
        openLatest: "打开最新",
        knowledgeBase: "知识库",
        ready: "就绪",
        path: "路径",
        indexTitle: "索引标题",
        tags: "标签",
        maxBytes: "最大字节数",
        import: "导入",
        pageDetails: "页面信息",
        title: "标题",
        type: "类型",
        confidence: "置信度",
        aliases: "别名",
        relationship: "页面关系",
        linkTo: "链接至",
        pageTitleOrId: "页面标题或 ID",
        relation: "关系类型",
        save: "保存",
        link: "创建关系",
        unlink: "解除关系",
        archive: "归档",
        pageSignals: "页面信号",
        updated: "更新时间",
        sources: "来源",
        knowledgeGraph: "知识图谱",
        localMemory: "本地记忆",
        fit: "适应画布",
        resetLayout: "重置布局",
        graphZoomControls: "图谱缩放控制",
        zoomOut: "缩小",
        zoomIn: "放大",
        resetZoom: "重置缩放",
        explore: "探索",
        searchGraph: "搜索图谱",
        focus: "聚焦",
        filterByPageType: "按页面类型筛选",
        allTypes: "全部类型",
        clear: "清除",
        legend: "图例",
        graphHelp: "滚轮缩放，拖动空白区域平移，拖动节点调整布局。",
        wikiPageGraph: "Wiki 页面图谱",
        graphMinimap: "图谱小地图",
        inspector: "检查器",
        connections: "连接数",
        linkedNodes: "关联节点",
        openInEditor: "在编辑器中打开",
        switchToEnglish: "切换到英文",
        switchToChinese: "切换到中文",
        requestFailed: "请求失败",
        newPageLabel: "新页面",
        memoryGraph: "记忆图谱",
        relationshipMap: "关系地图",
        noContent: "暂无内容。",
        untagged: "未加标签",
        matchedField: "匹配{field}",
        matchedContent: "匹配正文",
        matchedSearch: "匹配搜索条件",
        fieldTitle: "标题",
        fieldId: "ID",
        fieldType: "类型",
        fieldTag: "标签",
        fieldAlias: "别名",
        notSaved: "未保存",
        cursorOne: "{count} 个游标",
        cursorMany: "{count} 个游标",
        healthy: "健康",
        attention: "需要关注",
        unhealthy: "需要处理",
        unknown: "未知",
        workspaceReady: "工作区已就绪",
        checksPassed: "{count} 项健康检查已通过。",
        repairIndex: "修复索引",
        repairSetup: "修复安装",
        importedPageOne: "{count} 个导入页面",
        importedPageMany: "{count} 个导入页面",
        pageCountOne: "{count} 个页面",
        pageCountMany: "{count} 个页面",
        noPagesYet: "还没有页面",
        resultOne: "找到 {count} 条结果。",
        resultMany: "找到 {count} 条结果。",
        nodes: "节点",
        avgDegree: "平均连接",
        visibleCount: "{count} 个可见",
        allNodes: "全部节点",
        linkCountOne: "{count} 条关系",
        linkCountMany: "{count} 条关系",
        noLinkedNodes: "没有关联节点",
        noNodeSelected: "未选择节点",
        graphStats: "{pages} 个页面 / {links} 条关系",
        noWikiPages: "还没有 Wiki 页面。",
        running: "处理中...",
        indexResult: "已索引 {indexed}；移除 {removed}；跳过 {skipped}。",
        repairing: "修复中",
        workspaceRepaired: "工作区设置已修复。",
        importResult: "已导入 {imported}；跳过 {skipped}。",
        archiveConfirm: "确定要归档这个页面吗？",
        archived: "已归档。",
        chooseLink: "请先选择当前页面和目标页面。",
        linked: "关系已创建。",
        unlinked: "已解除 {count} 条关系。",
        saved: "已保存。"
      },
      en: {
        brandSubtitle: "Local memory workspace",
        home: "Home",
        dashboard: "Dashboard",
        newPage: "New page",
        new: "New",
        graph: "Graph",
        graphView: "Graph view",
        searchPlaceholder: "Search titles, tags, aliases",
        search: "Search",
        pages: "Pages",
        links: "Links",
        types: "Types",
        cursor: "Cursor",
        knowledgeConsole: "Knowledge Console",
        readOnlyDemo: "Read-only demo",
        memoryDashboard: "Memory Dashboard",
        localDurableMemory: "Local durable memory",
        editor: "Editor",
        memoryEditor: "Memory Editor",
        memoryHealth: "Memory Health",
        workspace: "Workspace",
        checking: "Checking",
        refresh: "Refresh",
        newMemory: "New Memory",
        rebuildIndex: "Rebuild Index",
        recentPages: "Recent Pages",
        openLatest: "Open Latest",
        knowledgeBase: "Knowledge Base",
        ready: "Ready",
        path: "Path",
        indexTitle: "Index Title",
        tags: "Tags",
        maxBytes: "Max bytes",
        import: "Import",
        pageDetails: "Page Details",
        title: "Title",
        type: "Type",
        confidence: "Confidence",
        aliases: "Aliases",
        relationship: "Relationship",
        linkTo: "Link To",
        pageTitleOrId: "Page title or id",
        relation: "Relation",
        save: "Save",
        link: "Link",
        unlink: "Unlink",
        archive: "Archive",
        pageSignals: "Page Signals",
        updated: "Updated",
        sources: "Sources",
        knowledgeGraph: "Knowledge Graph",
        localMemory: "Local Memory",
        fit: "Fit",
        resetLayout: "Reset Layout",
        graphZoomControls: "Graph zoom controls",
        zoomOut: "Zoom out",
        zoomIn: "Zoom in",
        resetZoom: "Reset zoom",
        explore: "Explore",
        searchGraph: "Search graph",
        focus: "Focus",
        filterByPageType: "Filter by page type",
        allTypes: "All types",
        clear: "Clear",
        legend: "Legend",
        graphHelp: "Wheel to zoom. Drag empty canvas to pan. Drag nodes to arrange the graph.",
        wikiPageGraph: "Wiki page graph",
        graphMinimap: "Graph minimap",
        inspector: "Inspector",
        connections: "Connections",
        linkedNodes: "Linked Nodes",
        openInEditor: "Open in Editor",
        switchToEnglish: "Switch to English",
        switchToChinese: "Switch to Chinese",
        requestFailed: "Request failed",
        newPageLabel: "New page",
        memoryGraph: "Memory Graph",
        relationshipMap: "Relationship map",
        noContent: "No content yet.",
        untagged: "untagged",
        matchedField: "Matched {field}",
        matchedContent: "Matched content",
        matchedSearch: "Matched search",
        fieldTitle: "title",
        fieldId: "id",
        fieldType: "type",
        fieldTag: "tag",
        fieldAlias: "alias",
        notSaved: "Not saved",
        cursorOne: "{count} cursor",
        cursorMany: "{count} cursors",
        healthy: "Healthy",
        attention: "Needs attention",
        unhealthy: "Action required",
        unknown: "Unknown",
        workspaceReady: "Workspace ready",
        checksPassed: "{count} health checks passed.",
        repairIndex: "Repair Index",
        repairSetup: "Repair Setup",
        importedPageOne: "{count} imported page",
        importedPageMany: "{count} imported pages",
        pageCountOne: "{count} page",
        pageCountMany: "{count} pages",
        noPagesYet: "No pages yet",
        resultOne: "{count} result.",
        resultMany: "{count} results.",
        nodes: "Nodes",
        avgDegree: "Avg degree",
        visibleCount: "{count} visible",
        allNodes: "All nodes",
        linkCountOne: "{count} link",
        linkCountMany: "{count} links",
        noLinkedNodes: "No linked nodes",
        noNodeSelected: "No node selected",
        graphStats: "{pages} pages / {links} links",
        noWikiPages: "No Wiki pages yet.",
        running: "Running...",
        indexResult: "Indexed {indexed}; removed {removed}; skipped {skipped}.",
        repairing: "Repairing",
        workspaceRepaired: "Workspace setup repaired.",
        importResult: "Imported {imported}; skipped {skipped}.",
        archiveConfirm: "Archive this page?",
        archived: "Archived.",
        chooseLink: "Choose the current page and a target page first.",
        linked: "Linked.",
        unlinked: "Removed {count} link(s).",
        saved: "Saved."
      }
    };
    const pageTypeLabels = {
      zh: {
        note: "笔记",
        profile: "用户资料",
        "project-index": "项目索引",
        project: "项目",
        questions: "待确认问题",
        qa: "质量验证",
        architecture: "架构",
        inbox: "收件箱",
        history: "历史",
        "knowledge-base-index": "知识库索引",
        "knowledge-doc": "知识文档",
        deployment: "部署",
        checklist: "清单"
      },
      en: {}
    };
    const healthCheckLabels = {
      workspace: "工作区",
      wiki_dir: "Wiki 目录",
      pages_dir: "Markdown 页面",
      database: "SQLite 数据库",
      index_sync: "搜索索引",
      link_source: "关系数据源",
      memory_bridge: "记忆桥",
      skill: "NanoBot 技能",
      config: "工作区配置",
      tool_registration: "NanoBot 工具"
    };
    const healthCheckMessages = {
      "workspace.ok": "工作区目录可用。",
      "workspace.error": "工作区目录缺失或不可访问。",
      "wiki_dir.ok": "Wiki 存储目录可用。",
      "wiki_dir.error": "Wiki 存储目录缺失。",
      "pages_dir.ok": "Markdown 页面目录可用。",
      "pages_dir.error": "Markdown 页面目录缺失。",
      "database.ok": "数据库完整性和结构检查通过。",
      "database.error": "SQLite 数据库缺失、损坏或结构不完整。",
      "index_sync.ok": "Markdown 页面与搜索索引保持一致。",
      "index_sync.warning": "Markdown 页面与搜索索引不一致，请重建索引。",
      "link_source.ok": "可迁移的关系数据源与图索引保持一致。",
      "link_source.warning": "关系数据源与图索引不一致，请重建索引。",
      "link_source.error": "关系数据源缺失或格式无效。",
      "memory_bridge.ok": "NanoBot 记忆桥已就绪。",
      "memory_bridge.error": "MEMORY.md 中缺少 Wiki 记忆桥。",
      "skill.ok": "Wiki 技能已包含核心记忆工作流。",
      "skill.warning": "Wiki 技能缺少必要的工具说明。",
      "skill.error": "Wiki 技能缺失或不可读取。",
      "config.ok": "工作区配置文件已就绪。",
      "config.warning": "工作区配置文件缺失。",
      "tool_registration.ok": "全部 Wiki 工具均已注册。",
      "tool_registration.error": "部分 Wiki 工具尚未注册。"
    };
    const graphPositionStoreKey = "nanobot_llm_wiki_graph_positions_v2";
    const graphNodeSize = { width: 124, height: 72 };
    const graphDotRadius = 8;
    const state = {
      allPages: [],
      visiblePages: [],
      status: {},
      health: null,
      readOnly: false,
      language: loadLanguage(),
      activePage: null,
      listQuery: "",
      view: "dashboard",
      activeId: "",
      graph: { nodes: [], links: [] },
      graphSelectedId: "",
      graphFilter: { query: "", type: "all" },
      nodePositions: loadGraphPositions(),
      drag: null,
      pan: null,
      suppressNodeClick: "",
      graphView: { zoom: 1, panX: 0, panY: 0 },
      graphSize: { width: 900, height: 620 }
    };
    const svgNS = "http://www.w3.org/2000/svg";
    const $ = (id) => document.getElementById(id);
    const message = (text) => { $("message").textContent = text || ""; };
    const graphMessage = (text) => { $("graphMessage").textContent = text || ""; };
    function applyReadOnlyMode() {
      const readOnly = state.readOnly;
      $("readOnlyBadge").classList.toggle("hidden", !readOnly);
      $("writeToolsPanel").classList.toggle("hidden", readOnly);
      $("dashboardGrid").classList.toggle("single-column", readOnly);
      ["newBtn", "quickNewBtn", "quickReindexBtn", "saveBtn", "linkBtn", "unlinkBtn", "archiveBtn"].forEach((id) => {
        $(id).classList.toggle("hidden", readOnly);
      });
      ["title", "pageType", "tags", "confidence", "aliases", "content", "linkTarget", "linkRelation"].forEach((id) => {
        $(id).readOnly = readOnly;
      });
    }
    function loadLanguage() {
      try {
        return localStorage.getItem(languageStoreKey) === "en" ? "en" : "zh";
      } catch (_error) {
        return "zh";
      }
    }
    function t(key, values = {}) {
      const dictionary = translations[state.language] || translations.zh;
      let text = dictionary[key] ?? translations.en[key] ?? key;
      Object.entries(values).forEach(([name, value]) => {
        text = text.split(`{${name}}`).join(String(value));
      });
      return text;
    }
    function countText(oneKey, manyKey, count) {
      return t(count === 1 ? oneKey : manyKey, { count });
    }
    function graphStatsText(pageCount, linkCount) {
      return t("graphStats", { pages: pageCount, links: linkCount });
    }
    function localizedHealthCheck(check) {
      if (state.language === "en" || !check.id) return check;
      return {
        ...check,
        label: healthCheckLabels[check.id] || check.label,
        message: healthCheckMessages[`${check.id}.${check.status}`] || check.message
      };
    }
    function applyStaticTranslations() {
      document.documentElement.lang = state.language === "zh" ? "zh-CN" : "en";
      document.querySelectorAll("[data-i18n]").forEach((element) => {
        element.textContent = t(element.dataset.i18n);
      });
      document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
        element.setAttribute("placeholder", t(element.dataset.i18nPlaceholder));
      });
      document.querySelectorAll("[data-i18n-title]").forEach((element) => {
        element.setAttribute("title", t(element.dataset.i18nTitle));
      });
      document.querySelectorAll("[data-i18n-aria-label]").forEach((element) => {
        element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel));
      });
      const languageButton = $("languageBtn");
      const nextIsEnglish = state.language === "zh";
      languageButton.textContent = nextIsEnglish ? "EN" : "中文";
      languageButton.title = nextIsEnglish ? t("switchToEnglish") : t("switchToChinese");
      languageButton.setAttribute("aria-label", languageButton.title);
    }
    function updateViewHeader() {
      if (state.view === "dashboard") {
        $("viewTitle").textContent = t("memoryDashboard");
        $("viewSubtitle").textContent = t("localDurableMemory");
      } else if (state.view === "editor") {
        $("viewTitle").textContent = $("title").value || t("memoryEditor");
        $("viewSubtitle").textContent = $("pageId").value || t("newPageLabel");
      } else {
        $("viewTitle").textContent = t("memoryGraph");
        $("viewSubtitle").textContent = t("relationshipMap");
      }
    }
    function setLanguage(language) {
      state.language = language === "en" ? "en" : "zh";
      try { localStorage.setItem(languageStoreKey, state.language); } catch (_error) {}
      applyStaticTranslations();
      updateViewHeader();
      renderList(state.visiblePages, { query: state.listQuery });
      renderStatus();
      renderDashboard();
      if (state.activePage) updateInspector(state.activePage);
      if ((state.graph.nodes || []).length) renderGraph();
    }
    const pagePayload = () => ({
      id: $("pageId").value,
      title: $("title").value,
      page_type: $("pageType").value || "note",
      tags: $("tags").value,
      aliases: $("aliases").value,
      confidence: Number($("confidence").value || 0.7),
      content: $("content").value,
      mode: "replace"
    });
    async function api(path, options = {}) {
      const normalizedPath = String(path).replace(/^\/+/, "");
      const requestUrl = new URL(normalizedPath, window.location.href);
      const res = await fetch(requestUrl, {
        headers: { "Content-Type": "application/json" },
        ...options
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || t("requestFailed"));
      return data;
    }
    function setView(mode) {
      state.view = mode;
      $("dashboardTab").classList.toggle("active", mode === "dashboard");
      $("editorTab").classList.toggle("active", mode === "editor");
      $("graphTab").classList.toggle("active", mode === "graph");
    }
    function showDashboard() {
      $("dashboardPanel").classList.remove("hidden");
      $("editor").classList.add("hidden");
      $("graphPanel").classList.add("hidden");
      setView("dashboard");
      updateViewHeader();
      renderDashboard();
    }
    function showEditor() {
      $("dashboardPanel").classList.add("hidden");
      $("editor").classList.remove("hidden");
      $("graphPanel").classList.add("hidden");
      setView("editor");
      updateViewHeader();
    }
    function showGraph() {
      $("dashboardPanel").classList.add("hidden");
      $("editor").classList.add("hidden");
      $("graphPanel").classList.remove("hidden");
      setView("graph");
      updateViewHeader();
      if (!state.graphSelectedId && state.activeId) state.graphSelectedId = state.activeId;
      loadGraph().catch((error) => graphMessage(error.message));
    }
    function renderList(pages, options = {}) {
      state.visiblePages = pages;
      $("pageList").innerHTML = "";
      $("listCount").textContent = String(pages.length);
      const query = options.query !== undefined ? options.query : state.listQuery;
      state.listQuery = query;
      pages.forEach((page) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "item" + (page.id === state.activeId ? " active" : "");
        btn.style.setProperty("--item-color", nodeColor(page));
        btn.innerHTML = `<div class="item-title"></div><div class="item-meta"></div><div class="item-preview"></div><div class="item-match"></div><div class="item-tags"></div>`;
        btn.querySelector(".item-title").textContent = page.title;
        btn.querySelector(".item-meta").textContent = `${formatGraphType(page.page_type)} / ${page.id}`;
        btn.querySelector(".item-preview").textContent = compactText(page.content || "", 118) || t("noContent");
        const match = matchSummary(page, query);
        const matchEl = btn.querySelector(".item-match");
        matchEl.textContent = match;
        matchEl.classList.toggle("hidden", !match);
        const tags = btn.querySelector(".item-tags");
        (page.tags && page.tags.length ? page.tags : [t("untagged")]).slice(0, 4).forEach((tag) => {
          const chip = document.createElement("span");
          chip.className = "tag";
          chip.textContent = tag;
          tags.appendChild(chip);
        });
        btn.addEventListener("click", () => loadPage(page.id));
        $("pageList").appendChild(btn);
      });
    }
    function matchSummary(page, query) {
      const q = String(query || "").trim().toLowerCase();
      if (!q) return "";
      const fields = [
        ["fieldTitle", page.title || ""],
        ["fieldId", page.id || ""],
        ["fieldType", page.page_type || ""],
        ["fieldTag", (page.tags || []).join(" ")],
        ["fieldAlias", (page.aliases || []).join(" ")],
      ];
      for (const [label, value] of fields) {
        if (String(value).toLowerCase().includes(q)) return t("matchedField", { field: t(label) });
      }
      const content = String(page.content || "").replace(/\s+/g, " ").trim();
      const index = content.toLowerCase().indexOf(q);
      if (index >= 0) {
        const start = Math.max(0, index - 34);
        const end = Math.min(content.length, index + q.length + 64);
        const prefix = start > 0 ? "..." : "";
        const suffix = end < content.length ? "..." : "";
        return `${t("matchedContent")} · ${prefix}${content.slice(start, end)}${suffix}`;
      }
      const terms = q.split(/\s+/).filter(Boolean);
      const term = terms.find((item) => content.toLowerCase().includes(item));
      if (term) return `${t("matchedContent")} · ${compactText(content, 92)}`;
      return t("matchedSearch");
    }
    function compactText(value, limit) {
      const text = String(value || "").replace(/\s+/g, " ").trim();
      return text.length > limit ? text.slice(0, limit - 1).trim() + "..." : text;
    }
    function formatDate(value) {
      if (!value) return t("notSaved");
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      const locale = state.language === "zh" ? "zh-CN" : "en-US";
      return date.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
    }
    function updateInspector(page) {
      const confidence = Number.isFinite(Number(page.confidence)) ? Number(page.confidence) : 0.7;
      const cursors = page.source_cursors || [];
      $("detailId").textContent = page.id || t("newPageLabel");
      $("detailUpdated").textContent = formatDate(page.updated_at);
      $("detailConfidence").textContent = confidence.toFixed(2);
      $("detailSources").textContent = countText("cursorOne", "cursorMany", cursors.length);
      $("detailTags").innerHTML = "";
      (page.tags && page.tags.length ? page.tags : [t("untagged")]).forEach((tag) => {
        const chip = document.createElement("span");
        chip.className = "tag";
        chip.textContent = tag;
        $("detailTags").appendChild(chip);
      });
    }
    function fillEditor(page) {
      state.activePage = page;
      state.activeId = page.id || "";
      $("pageId").value = page.id || "";
      $("title").value = page.title || "";
      $("pageType").value = page.page_type || "note";
      $("tags").value = (page.tags || []).join(", ");
      $("aliases").value = (page.aliases || []).join(", ");
      $("confidence").value = Number.isFinite(Number(page.confidence)) ? Number(page.confidence).toFixed(2) : "0.70";
      $("content").value = page.content || "";
      $("linkTarget").value = "";
      $("linkRelation").value = "related";
      updateInspector(page);
      renderList(state.visiblePages.length ? state.visiblePages : state.allPages);
      showEditor();
    }
    function pageTypeCounts(pages) {
      const counts = new Map();
      pages.forEach((page) => {
        const type = String(page.page_type || "note");
        counts.set(type, (counts.get(type) || 0) + 1);
      });
      return counts;
    }
    function renderHealth() {
      const result = state.health;
      const badge = $("healthBadge");
      const summary = $("healthSummary");
      summary.innerHTML = "";
      if (!result) {
        badge.textContent = t("checking");
        badge.dataset.health = "";
        return;
      }
      const badgeLabels = {
        healthy: t("healthy"),
        attention: t("attention"),
        unhealthy: t("unhealthy")
      };
      badge.textContent = badgeLabels[result.health] || t("unknown");
      badge.dataset.health = result.health || "";
      const issues = (result.checks || []).filter((check) => check.status !== "ok");
      const visibleChecks = issues.length ? issues : [{
        status: "ok",
        label: t("workspaceReady"),
        message: t("checksPassed", { count: result.summary.passed })
      }];
      visibleChecks.map(localizedHealthCheck).forEach((check) => {
        const row = document.createElement("div");
        row.className = "health-check";
        row.dataset.status = check.status;
        const indicator = document.createElement("span");
        indicator.className = "health-indicator";
        const copy = document.createElement("div");
        copy.className = "health-copy";
        const label = document.createElement("div");
        label.className = "health-label";
        label.textContent = check.label;
        const detail = document.createElement("div");
        detail.className = "health-message";
        detail.textContent = check.message;
        copy.appendChild(label);
        copy.appendChild(detail);
        row.appendChild(indicator);
        row.appendChild(copy);
        if (!state.readOnly && check.action === "reindex") {
          const repair = document.createElement("button");
          repair.type = "button";
          repair.textContent = t("repairIndex");
          repair.addEventListener("click", () => reindexWorkspace().catch((error) => {
            $("reindexResult").textContent = error.message;
          }));
          row.appendChild(repair);
        } else if (!state.readOnly && check.action === "install") {
          const repair = document.createElement("button");
          repair.type = "button";
          repair.textContent = t("repairSetup");
          repair.addEventListener("click", () => repairWorkspace().catch((error) => {
            message(error.message);
          }));
          row.appendChild(repair);
        }
        summary.appendChild(row);
      });
    }
    function renderDashboard() {
      const status = state.status || {};
      const pages = state.allPages || [];
      const typeCounts = pageTypeCounts(pages);
      $("workspacePath").textContent = status.workspace || t("workspace");
      $("metricGrid").innerHTML = "";
      [
        [t("pages"), status.pages ?? pages.length],
        [t("links"), status.links ?? 0],
        [t("types"), typeCounts.size],
        [t("cursor"), status.cursor ?? 0],
      ].forEach(([label, value]) => {
        const item = document.createElement("div");
        item.className = "metric";
        const number = document.createElement("strong");
        const caption = document.createElement("span");
        number.textContent = value;
        caption.textContent = label;
        item.appendChild(number);
        item.appendChild(caption);
        $("metricGrid").appendChild(item);
      });
      renderHealth();

      const kbCount = pages.filter((page) => {
        const text = `${page.page_type || ""} ${(page.tags || []).join(" ")}`.toLowerCase();
        return text.includes("knowledge-base") || text.includes("knowledge-doc") || text.includes("imported");
      }).length;
      $("toolStatus").textContent = countText("importedPageOne", "importedPageMany", kbCount);
      $("recentPagesMeta").textContent = countText("pageCountOne", "pageCountMany", pages.length);
      $("openFirstRecentBtn").disabled = !pages.length;
      $("recentPages").innerHTML = "";
      if (!pages.length) {
        const empty = document.createElement("div");
        empty.className = "dashboard-row";
        empty.innerHTML = `<div class="dashboard-row-title"></div><div class="dashboard-row-meta"></div>`;
        empty.querySelector(".dashboard-row-title").textContent = t("noPagesYet");
        empty.querySelector(".dashboard-row-meta").textContent = t("ready");
        $("recentPages").appendChild(empty);
        return;
      }
      pages.slice(0, 8).forEach((page) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "dashboard-row";
        row.style.setProperty("--row-color", nodeColor(page));
        row.innerHTML = `<div class="dashboard-row-title"></div><div class="dashboard-row-meta"></div><div class="dashboard-row-preview"></div>`;
        row.querySelector(".dashboard-row-title").textContent = page.title || page.id;
        row.querySelector(".dashboard-row-meta").textContent = `${formatGraphType(page.page_type)} / ${formatDate(page.updated_at)}`;
        row.querySelector(".dashboard-row-preview").textContent = compactText(page.content || "", 140) || t("noContent");
        row.addEventListener("click", () => loadPage(page.id));
        $("recentPages").appendChild(row);
      });
    }
    function renderStatus() {
      const status = state.status || {};
      $("status").innerHTML = "";
      [[t("pages"), status.pages || 0], [t("links"), status.links || 0], [t("cursor"), status.cursor || 0]].forEach(([label, value]) => {
        const item = document.createElement("span");
        const number = document.createElement("strong");
        const caption = document.createElement("em");
        number.textContent = value;
        caption.textContent = label;
        item.appendChild(number);
        item.appendChild(caption);
        $("status").appendChild(item);
      });
    }
    async function loadStatus() {
      const [status, health] = await Promise.all([api("/api/status"), api("/api/doctor")]);
      state.status = status;
      state.health = health;
      state.readOnly = Boolean(status.read_only);
      applyReadOnlyMode();
      renderStatus();
      renderDashboard();
    }
    async function loadPages() {
      const data = await api("/api/pages?limit=200");
      state.allPages = data.pages;
      renderList(data.pages, { query: "" });
      await loadStatus();
      if (state.view === "dashboard") renderDashboard();
    }
    async function searchPages() {
      const rawQuery = $("searchInput").value.trim();
      const q = encodeURIComponent(rawQuery);
      const data = q ? await api(`/api/search?q=${q}&limit=50`) : await api("/api/pages?limit=200");
      const pages = data.results ? data.results.map((result) => result.page) : data.pages;
      renderList(pages, { query: rawQuery });
      message(q ? countText("resultOne", "resultMany", pages.length) : "");
    }
    async function loadPage(id) {
      const data = await api(`/api/pages/${encodeURIComponent(id)}`);
      fillEditor(data.page);
      showEditor();
      message("");
    }
    function svgEl(name, attrs = {}) {
      const el = document.createElementNS(svgNS, name);
      Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
      return el;
    }
    function trimLabel(text, max = 24) {
      return text.length > max ? text.slice(0, max - 1) + "…" : text;
    }
    function relationLabelWidth(text) {
      return Math.max(46, Math.min(118, text.length * 6.2 + 18));
    }
    function nodeColor(node) {
      const value = `${node.page_type || ""} ${(node.tags || []).join(" ")}`.toLowerCase();
      if (value.includes("profile") || value.includes("user")) return "#21877d";
      if (value.includes("project-index")) return "#d0a03c";
      if (value.includes("project")) return "#c4882d";
      if (value.includes("question")) return "#c94f78";
      if (value.includes("qa") || value.includes("test")) return "#4f79c8";
      if (value.includes("architecture")) return "#6961d2";
      if (value.includes("inbox") || value.includes("history")) return "#74817b";
      return "#8a6db2";
    }
    function graphNodeById(id) {
      return (state.graph.nodes || []).find((node) => node.id === id);
    }
    function graphTypeKey(node) {
      return String(node.page_type || "note").toLowerCase();
    }
    function formatGraphType(value) {
      const key = String(value || "note").toLowerCase();
      const localized = pageTypeLabels[state.language] && pageTypeLabels[state.language][key];
      if (localized) return localized;
      const text = key.replace(/[-_]/g, " ");
      return text.charAt(0).toUpperCase() + text.slice(1);
    }
    function graphSearchText(node) {
      return `${node.title || ""} ${node.id || ""} ${node.page_type || ""} ${(node.tags || []).join(" ")}`.toLowerCase();
    }
    function matchesGraphFilter(node) {
      const query = state.graphFilter.query.trim().toLowerCase();
      const type = state.graphFilter.type;
      if (type !== "all" && graphTypeKey(node) !== type) return false;
      return !query || graphSearchText(node).includes(query);
    }
    function graphConnections(id) {
      return (state.graph.links || []).map((link, index) => {
        if (link.from_id === id) return { index, link, otherId: link.to_id, direction: "out" };
        if (link.to_id === id) return { index, link, otherId: link.from_id, direction: "in" };
        return null;
      }).filter(Boolean);
    }
    function graphNodeRadius(nodeOrId) {
      const id = typeof nodeOrId === "string" ? nodeOrId : nodeOrId && nodeOrId.id;
      const degree = id ? graphConnections(id).length : 0;
      return graphDotRadius + Math.min(4, degree * 1.25);
    }
    function graphNeighborhood(id) {
      const nodes = new Set([id]);
      const links = new Set();
      graphConnections(id).forEach((connection) => {
        nodes.add(connection.otherId);
        links.add(connection.index);
      });
      return { nodes, links };
    }
    function renderGraphSidebar() {
      const nodes = state.graph.nodes || [];
      const links = state.graph.links || [];
      const typeCounts = new Map();
      nodes.forEach((node) => {
        const type = graphTypeKey(node);
        typeCounts.set(type, (typeCounts.get(type) || 0) + 1);
      });

      const currentType = typeCounts.has(state.graphFilter.type) ? state.graphFilter.type : "all";
      state.graphFilter.type = currentType;
      const typeFilter = $("graphTypeFilter");
      typeFilter.innerHTML = "";
      const allOption = document.createElement("option");
      allOption.value = "all";
      allOption.textContent = t("allTypes");
      typeFilter.appendChild(allOption);
      Array.from(typeCounts.keys()).sort().forEach((type) => {
        const option = document.createElement("option");
        option.value = type;
        option.textContent = formatGraphType(type);
        typeFilter.appendChild(option);
      });
      typeFilter.value = state.graphFilter.type;

      const summary = $("graphSummary");
      summary.innerHTML = "";
      const averageDegree = nodes.length ? ((links.length * 2) / nodes.length).toFixed(1) : "0.0";
      [[t("nodes"), nodes.length], [t("links"), links.length], [t("types"), typeCounts.size], [t("avgDegree"), averageDegree]].forEach(([label, value]) => {
        const item = document.createElement("div");
        const number = document.createElement("strong");
        const caption = document.createElement("span");
        number.textContent = value;
        caption.textContent = label;
        item.appendChild(number);
        item.appendChild(caption);
        summary.appendChild(item);
      });

      const legend = $("graphLegend");
      legend.innerHTML = "";
      Array.from(typeCounts.entries()).sort(([left], [right]) => left.localeCompare(right)).forEach(([type, count]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = state.graphFilter.type === type ? "active" : "";
        const row = document.createElement("span");
        const dot = document.createElement("i");
        dot.className = "dot";
        dot.style.background = nodeColor({ page_type: type, tags: [] });
        const label = document.createElement("span");
        label.textContent = `${formatGraphType(type)} (${count})`;
        row.appendChild(dot);
        row.appendChild(label);
        button.appendChild(row);
        button.addEventListener("click", () => {
          state.graphFilter.type = type;
          $("graphTypeFilter").value = type;
          renderGraphSidebar();
          updateGraphFocusStyles();
        });
        legend.appendChild(button);
      });
    }
    function updateGraphChrome() {
      const nodes = state.graph.nodes || [];
      const matchingCount = nodes.filter((node) => matchesGraphFilter(node)).length;
      const selected = graphNodeById(state.graphSelectedId);
      $("graphDensityLabel").textContent = t("visibleCount", { count: matchingCount });
      $("graphFocusLabel").textContent = selected ? trimLabel(selected.title, 42) : t("allNodes");
    }
    function renderGraphInspector() {
      const node = graphNodeById(state.graphSelectedId);
      const empty = $("graphInspectorEmpty");
      const content = $("graphInspectorContent");
      if (!node) {
        empty.classList.remove("hidden");
        content.classList.add("hidden");
        updateGraphChrome();
        return;
      }

      empty.classList.add("hidden");
      content.classList.remove("hidden");
      $("graphDetailTitle").textContent = node.title || node.id;
      $("graphDetailTitle").style.setProperty("--node-color", nodeColor(node));
      $("graphDetailMeta").textContent = `${formatGraphType(node.page_type)} / ${node.id}`;
      $("graphDetailId").textContent = node.id;
      $("graphDetailUpdated").textContent = formatDate(node.updated_at);

      const connections = graphConnections(node.id);
      $("graphDetailDegree").textContent = countText("linkCountOne", "linkCountMany", connections.length);
      const tags = $("graphDetailTags");
      tags.innerHTML = "";
      (node.tags && node.tags.length ? node.tags : [t("untagged")]).slice(0, 8).forEach((tag) => {
        const chip = document.createElement("span");
        chip.className = "tag";
        chip.textContent = tag;
        tags.appendChild(chip);
      });

      const list = $("graphDetailLinks");
      list.innerHTML = "";
      if (!connections.length) {
        const emptyLink = document.createElement("div");
        emptyLink.className = "graph-empty-state";
        emptyLink.textContent = t("noLinkedNodes");
        list.appendChild(emptyLink);
      } else {
        connections.slice(0, 12).forEach((connection) => {
          const other = graphNodeById(connection.otherId);
          const button = document.createElement("button");
          button.type = "button";
          const direction = connection.direction === "out" ? "→" : "←";
          button.textContent = `${direction} ${trimLabel(connection.link.relation || "related", 18)} · ${trimLabel(other ? other.title : connection.otherId, 30)}`;
          button.addEventListener("click", () => selectGraphNode(connection.otherId, { center: true }));
          list.appendChild(button);
        });
      }
      updateGraphChrome();
    }
    function updateGraphFocusStyles() {
      const selectedId = state.graphSelectedId;
      const selected = graphNodeById(selectedId);
      const neighborhood = selected ? graphNeighborhood(selectedId) : { nodes: new Set(), links: new Set() };
      const matchingNodes = new Set((state.graph.nodes || []).filter((node) => matchesGraphFilter(node)).map((node) => node.id));
      document.querySelectorAll(".graph-node").forEach((group) => {
        const id = group.dataset.nodeId;
        const outOfFocus = selected && !neighborhood.nodes.has(id);
        const filtered = !matchingNodes.has(id);
        group.classList.toggle("active", Boolean(selected && id === selectedId));
        group.classList.toggle("neighbor", Boolean(selected && neighborhood.nodes.has(id) && id !== selectedId));
        group.classList.toggle("muted", Boolean(outOfFocus || filtered));
      });
      (state.graph.links || []).forEach((link, index) => {
        const selectedLink = selected && neighborhood.links.has(index);
        const filtered = !matchingNodes.has(link.from_id) || !matchingNodes.has(link.to_id);
        const muted = Boolean((selected && !selectedLink) || filtered);
        ["path", "glow", "label"].forEach((kind) => {
          const item = document.querySelector(`[data-link-${kind}="${index}"]`);
          if (!item) return;
          item.classList.toggle("active", Boolean(selectedLink));
          item.classList.toggle("muted", muted);
        });
      });
      renderGraphInspector();
      renderMiniMap();
    }
    function selectGraphNode(id, options = {}) {
      if (!graphNodeById(id)) return;
      state.graphSelectedId = id;
      updateGraphFocusStyles();
      if (options.center) centerGraphOnNode(id);
    }
    function focusGraphSearchResult() {
      const match = (state.graph.nodes || []).find((node) => matchesGraphFilter(node));
      if (match) selectGraphNode(match.id, { center: true });
    }
    function clearGraphFocus() {
      state.graphSelectedId = "";
      state.graphFilter = { query: "", type: "all" };
      $("graphSearch").value = "";
      $("graphTypeFilter").value = "all";
      renderGraphSidebar();
      updateGraphFocusStyles();
    }
    function openSelectedGraphNode() {
      if (!state.graphSelectedId) return;
      loadPage(state.graphSelectedId);
    }
    function loadGraphPositions() {
      try {
        const parsed = JSON.parse(localStorage.getItem(graphPositionStoreKey) || "{}");
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
        return Object.fromEntries(Object.entries(parsed).filter(([_id, point]) => {
          return point && Number.isFinite(point.x) && Number.isFinite(point.y);
        }));
      } catch (_error) {
        return {};
      }
    }
    function saveGraphPositions() {
      try {
        localStorage.setItem(graphPositionStoreKey, JSON.stringify(state.nodePositions));
      } catch (_error) {}
    }
    function clearGraphPositions() {
      try {
        localStorage.removeItem(graphPositionStoreKey);
      } catch (_error) {}
    }
    function graphContentBounds() {
      const nodes = state.graph.nodes || [];
      if (!nodes.length) {
        return { x: 0, y: 0, width: state.graphSize.width, height: state.graphSize.height };
      }
      const halfW = graphNodeSize.width / 2;
      const halfH = graphNodeSize.height / 2;
      const points = nodes.map((node) => state.nodePositions[node.id]).filter(Boolean);
      if (!points.length) {
        return { x: 0, y: 0, width: state.graphSize.width, height: state.graphSize.height };
      }
      const left = Math.min(...points.map((point) => point.x - halfW));
      const right = Math.max(...points.map((point) => point.x + halfW));
      const top = Math.min(...points.map((point) => point.y - halfH));
      const bottom = Math.max(...points.map((point) => point.y + halfH));
      return {
        x: left,
        y: top,
        width: Math.max(1, right - left),
        height: Math.max(1, bottom - top)
      };
    }
    function fitGraphToView() {
      const bounds = graphContentBounds();
      const padding = 72;
      const availableWidth = Math.max(1, state.graphSize.width - padding * 2);
      const availableHeight = Math.max(1, state.graphSize.height - padding * 2);
      const zoom = clampGraphZoom(Math.min(availableWidth / bounds.width, availableHeight / bounds.height));
      state.graphView.zoom = zoom;
      state.graphView.panX = state.graphSize.width / 2 - (bounds.x + bounds.width / 2) * zoom;
      state.graphView.panY = state.graphSize.height / 2 - (bounds.y + bounds.height / 2) * zoom;
      applyGraphViewport();
    }
    function centerGraphOnNode(id) {
      const point = state.nodePositions[id];
      if (!point) return;
      state.graphView.panX = state.graphSize.width / 2 - point.x * state.graphView.zoom;
      state.graphView.panY = state.graphSize.height / 2 - point.y * state.graphView.zoom;
      applyGraphViewport();
    }
    function miniMapPoint(point) {
      const miniWidth = 184;
      const miniHeight = 122;
      return {
        x: (point.x / state.graphSize.width) * miniWidth,
        y: (point.y / state.graphSize.height) * miniHeight
      };
    }
    function updateMiniMapViewport() {
      const rect = $("graphMiniMap").querySelector("[data-minimap-viewport]");
      if (!rect || !state.graphSize.width || !state.graphSize.height) return;
      const miniWidth = 184;
      const miniHeight = 122;
      const viewX = -state.graphView.panX / state.graphView.zoom;
      const viewY = -state.graphView.panY / state.graphView.zoom;
      const viewWidth = state.graphSize.width / state.graphView.zoom;
      const viewHeight = state.graphSize.height / state.graphView.zoom;
      const clippedX = Math.max(0, Math.min(state.graphSize.width, viewX));
      const clippedY = Math.max(0, Math.min(state.graphSize.height, viewY));
      const clippedRight = Math.max(0, Math.min(state.graphSize.width, viewX + viewWidth));
      const clippedBottom = Math.max(0, Math.min(state.graphSize.height, viewY + viewHeight));
      rect.setAttribute("x", String((clippedX / state.graphSize.width) * miniWidth));
      rect.setAttribute("y", String((clippedY / state.graphSize.height) * miniHeight));
      rect.setAttribute("width", String(Math.max(8, ((clippedRight - clippedX) / state.graphSize.width) * miniWidth)));
      rect.setAttribute("height", String(Math.max(8, ((clippedBottom - clippedY) / state.graphSize.height) * miniHeight)));
    }
    function renderMiniMap() {
      const mini = $("graphMiniMap");
      if (!mini) return;
      mini.innerHTML = "";
      const miniWidth = 184;
      const miniHeight = 122;
      mini.setAttribute("viewBox", `0 0 ${miniWidth} ${miniHeight}`);
      mini.appendChild(svgEl("rect", {
        x: 0,
        y: 0,
        width: miniWidth,
        height: miniHeight,
        fill: "rgba(255,255,255,0.025)"
      }));
      (state.graph.links || []).forEach((link) => {
        const from = state.nodePositions[link.from_id];
        const to = state.nodePositions[link.to_id];
        if (!from || !to) return;
        const start = miniMapPoint(from);
        const end = miniMapPoint(to);
        mini.appendChild(svgEl("line", {
          class: "minimap-link",
          x1: start.x,
          y1: start.y,
          x2: end.x,
          y2: end.y
        }));
      });
      (state.graph.nodes || []).forEach((node) => {
        const pos = state.nodePositions[node.id];
        if (!pos) return;
        const point = miniMapPoint(pos);
        mini.appendChild(svgEl("circle", {
          class: "minimap-node" + (node.id === state.graphSelectedId ? " active" : ""),
          cx: point.x,
          cy: point.y,
          r: node.id === state.graphSelectedId ? 3.8 : 2.6,
          fill: nodeColor(node)
        }));
      });
      mini.appendChild(svgEl("rect", {
        class: "minimap-viewport",
        "data-minimap-viewport": "true",
        x: 0,
        y: 0,
        width: miniWidth,
        height: miniHeight,
        rx: 4
      }));
      updateMiniMapViewport();
    }
    function handleMiniMapPointer(event) {
      event.preventDefault();
      const rect = $("graphMiniMap").getBoundingClientRect();
      const x = ((event.clientX - rect.left) / rect.width) * state.graphSize.width;
      const y = ((event.clientY - rect.top) / rect.height) * state.graphSize.height;
      state.graphView.panX = state.graphSize.width / 2 - x * state.graphView.zoom;
      state.graphView.panY = state.graphSize.height / 2 - y * state.graphView.zoom;
      applyGraphViewport();
    }
    async function loadGraph() {
      state.graph = await api("/api/graph?limit=200");
      if (state.graphSelectedId && !graphNodeById(state.graphSelectedId)) state.graphSelectedId = "";
      renderGraph();
    }
    function graphPoint(event) {
      const raw = graphViewportPoint(event);
      return {
        x: (raw.x - state.graphView.panX) / state.graphView.zoom,
        y: (raw.y - state.graphView.panY) / state.graphView.zoom
      };
    }
    function graphViewportPoint(event) {
      const svg = $("graphSvg");
      const rect = svg.getBoundingClientRect();
      const viewBox = svg.viewBox.baseVal;
      return {
        x: viewBox.x + ((event.clientX - rect.left) * viewBox.width) / rect.width,
        y: viewBox.y + ((event.clientY - rect.top) * viewBox.height) / rect.height
      };
    }
    function clampGraphZoom(value) {
      return Math.max(0.32, Math.min(3.2, value));
    }
    function applyGraphViewport() {
      const viewport = document.querySelector("#graphViewport");
      if (viewport) {
        viewport.setAttribute(
          "transform",
          `translate(${state.graphView.panX} ${state.graphView.panY}) scale(${state.graphView.zoom})`,
        );
        viewport.classList.toggle("lod-minimal", state.graphView.zoom < 0.72);
        viewport.classList.toggle(
          "lod-compact",
          state.graphView.zoom < 0.92 || state.graphSize.width < 520,
        );
        viewport.classList.toggle("lod-detail", state.graphView.zoom > 1.24);
      }
      $("zoomValue").textContent = `${Math.round(state.graphView.zoom * 100)}%`;
      updateMiniMapViewport();
    }
    function setGraphZoom(value, anchor = null) {
      const previousZoom = state.graphView.zoom;
      const nextZoom = clampGraphZoom(value);
      if (Math.abs(previousZoom - nextZoom) < 0.001) return;
      const focus = anchor || {
        x: state.graphSize.width / 2,
        y: state.graphSize.height / 2
      };
      const graphX = (focus.x - state.graphView.panX) / previousZoom;
      const graphY = (focus.y - state.graphView.panY) / previousZoom;
      state.graphView.zoom = nextZoom;
      state.graphView.panX = focus.x - graphX * nextZoom;
      state.graphView.panY = focus.y - graphY * nextZoom;
      applyGraphViewport();
    }
    function resetGraphZoom() {
      state.graphView = { zoom: 1, panX: 0, panY: 0 };
      applyGraphViewport();
    }
    function clampGraphPoint(point) {
      const marginX = graphNodeSize.width / 2 + 24;
      const marginY = graphNodeSize.height / 2 + 28;
      return {
        x: Math.max(marginX, Math.min(state.graphSize.width - marginX, point.x)),
        y: Math.max(marginY, Math.min(state.graphSize.height - marginY, point.y))
      };
    }
    function defaultGraphPosition(index, count, width, height) {
      const centerX = width / 2;
      const centerY = height / 2 + 6;
      const radiusX = Math.max(150, Math.min(width * 0.33, 330));
      const radiusY = Math.max(112, Math.min(height * 0.28, 212));
      const angle = count === 1 ? -Math.PI / 2 : (Math.PI * 2 * index) / count - Math.PI / 2;
      return {
        x: centerX + Math.cos(angle) * radiusX,
        y: centerY + Math.sin(angle) * radiusY
      };
    }
    function edgePoint(source, target, radius) {
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      return {
        x: source.x + (dx / distance) * radius,
        y: source.y + (dy / distance) * radius
      };
    }
    function routeLink(from, to, fromId, toId) {
      const start = edgePoint(from, to, graphNodeRadius(fromId) + 5);
      const end = edgePoint(to, from, graphNodeRadius(toId) + 5);
      const dx = end.x - start.x;
      const dy = end.y - start.y;
      const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const curve = Math.min(70, dist * 0.16);
      const midX = (start.x + end.x) / 2 - (dy / dist) * curve;
      const midY = (start.y + end.y) / 2 + (dx / dist) * curve;
      return { startX: start.x, startY: start.y, endX: end.x, endY: end.y, midX, midY };
    }
    function updateGraphGeometry() {
      const svg = $("graphSvg");
      const links = state.graph.links || [];
      links.forEach((link, index) => {
        const from = state.nodePositions[link.from_id];
        const to = state.nodePositions[link.to_id];
        if (!from || !to) return;
        const route = routeLink(from, to, link.from_id, link.to_id);
        const path = document.querySelector(`[data-link-path="${index}"]`);
        const glow = document.querySelector(`[data-link-glow="${index}"]`);
        const label = document.querySelector(`[data-link-label="${index}"]`);
        const d = `M ${route.startX} ${route.startY} Q ${route.midX} ${route.midY} ${route.endX} ${route.endY}`;
        if (glow) glow.setAttribute("d", d);
        if (path) path.setAttribute("d", d);
        if (label) {
          label.setAttribute("transform", `translate(${route.midX} ${route.midY - 6})`);
        }
      });
      (state.graph.nodes || []).forEach((node) => {
        const pos = state.nodePositions[node.id];
        const group = Array.from(svg.querySelectorAll("[data-node-id]")).find((item) => {
          return item.dataset.nodeId === node.id;
        });
        if (group && pos) group.setAttribute("transform", `translate(${pos.x} ${pos.y})`);
      });
      renderMiniMap();
    }
    function resetGraphLayout() {
      state.nodePositions = {};
      clearGraphPositions();
      renderGraph();
    }
    function renderGraph() {
      const svg = $("graphSvg");
      svg.innerHTML = "";
      const width = Math.max(svg.clientWidth || 940, 480);
      const height = Math.max(svg.clientHeight || 620, 480);
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
      state.graphSize = { width, height };

      const defs = svgEl("defs");
      const marker = svgEl("marker", {
        id: "arrow",
        viewBox: "0 0 10 10",
        refX: "9",
        refY: "5",
        markerWidth: "5",
        markerHeight: "5",
        orient: "auto-start-reverse"
      });
      marker.appendChild(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#8da69d" }));
      defs.appendChild(marker);
      svg.appendChild(defs);

      const viewportLayer = svgEl("g", { id: "graphViewport" });
      const edgeLayer = svgEl("g", { class: "graph-edges" });
      const labelLayer = svgEl("g", { class: "graph-edge-labels" });
      const nodeLayer = svgEl("g", { class: "graph-nodes" });
      viewportLayer.appendChild(edgeLayer);
      viewportLayer.appendChild(labelLayer);
      viewportLayer.appendChild(nodeLayer);
      svg.appendChild(viewportLayer);
      applyGraphViewport();

      const nodes = state.graph.nodes || [];
      const links = state.graph.links || [];
      $("graphStats").textContent = graphStatsText(nodes.length, links.length);
      renderGraphSidebar();
      if (!nodes.length) {
        const empty = svgEl("text", { x: width / 2, y: height / 2, "text-anchor": "middle", fill: "rgba(224,239,233,0.62)" });
        empty.textContent = t("noWikiPages");
        svg.appendChild(empty);
        renderGraphInspector();
        renderMiniMap();
        graphMessage("");
        return;
      }

      nodes.forEach((node, index) => {
        if (!state.nodePositions[node.id]) {
          state.nodePositions[node.id] = defaultGraphPosition(index, nodes.length, width, height);
        } else {
          state.nodePositions[node.id] = clampGraphPoint(state.nodePositions[node.id]);
        }
      });

      links.forEach((link, index) => {
        const from = state.nodePositions[link.from_id];
        const to = state.nodePositions[link.to_id];
        if (!from || !to) return;
        const route = routeLink(from, to, link.from_id, link.to_id);
        const d = `M ${route.startX} ${route.startY} Q ${route.midX} ${route.midY} ${route.endX} ${route.endY}`;
        const glow = svgEl("path", {
          class: "graph-link-glow",
          d,
          "data-link-glow": index
        });
        edgeLayer.appendChild(glow);

        const path = svgEl("path", {
          class: "graph-link",
          d,
          "data-link-path": index,
          "marker-end": "url(#arrow)"
        });
        edgeLayer.appendChild(path);

        const relationText = trimLabel(link.relation, 18);
        const labelWidth = relationLabelWidth(relationText);
        const labelGroup = svgEl("g", {
          class: "graph-relation-pill",
          "data-link-label": index,
          transform: `translate(${route.midX} ${route.midY - 6})`
        });
        labelGroup.appendChild(svgEl("rect", {
          x: -labelWidth / 2,
          y: -10,
          width: labelWidth,
          height: 20,
          rx: 5
        }));
        const label = svgEl("text", {
          class: "graph-relation",
          x: 0,
          y: 1
        });
        label.textContent = relationText;
        labelGroup.appendChild(label);
        labelLayer.appendChild(labelGroup);
      });

      nodes.forEach((node) => {
        const pos = state.nodePositions[node.id];
        const color = nodeColor(node);
        const nodeRadius = graphNodeRadius(node);
        const group = svgEl("g", {
          class: "graph-node" + (node.id === state.graphSelectedId ? " active" : ""),
          "data-node-id": node.id,
          "data-node-radius": nodeRadius,
          transform: `translate(${pos.x} ${pos.y})`
        });
        group.setAttribute("tabindex", "0");
        group.style.cursor = "grab";
        group.style.setProperty("--node-color", color);
        group.addEventListener("pointerdown", (event) => {
          event.preventDefault();
          event.stopPropagation();
          const start = graphPoint(event);
          const current = state.nodePositions[node.id];
          state.drag = {
            id: node.id,
            pointerId: event.pointerId,
            offsetX: current.x - start.x,
            offsetY: current.y - start.y,
            moved: false
          };
          group.classList.add("dragging");
          try { group.setPointerCapture(event.pointerId); } catch (_error) {}
        });
        group.addEventListener("pointermove", (event) => {
          if (!state.drag || state.drag.id !== node.id) return;
          const point = graphPoint(event);
          const next = clampGraphPoint({
            x: point.x + state.drag.offsetX,
            y: point.y + state.drag.offsetY
          });
          const previous = state.nodePositions[node.id];
          if (Math.abs(next.x - previous.x) > 1 || Math.abs(next.y - previous.y) > 1) {
            state.drag.moved = true;
          }
          state.nodePositions[node.id] = next;
          updateGraphGeometry();
        });
        group.addEventListener("pointerup", (event) => {
          if (state.drag && state.drag.id === node.id) {
            if (state.drag.moved) state.suppressNodeClick = node.id;
            if (state.drag.moved) saveGraphPositions();
            state.drag = null;
          }
          group.classList.remove("dragging");
          try { group.releasePointerCapture(event.pointerId); } catch (_error) {}
        });
        group.addEventListener("pointercancel", () => {
          state.drag = null;
          group.classList.remove("dragging");
        });
        group.addEventListener("click", () => {
          if (state.suppressNodeClick === node.id) {
            state.suppressNodeClick = "";
            return;
          }
          selectGraphNode(node.id);
        });
        group.addEventListener("dblclick", () => {
          loadPage(node.id);
        });
        group.addEventListener("keydown", (event) => {
          if (event.key === "Enter") selectGraphNode(node.id);
          const nudges = {
            ArrowUp: { x: 0, y: -1 },
            ArrowDown: { x: 0, y: 1 },
            ArrowLeft: { x: -1, y: 0 },
            ArrowRight: { x: 1, y: 0 }
          };
          const nudge = nudges[event.key];
          if (!nudge) return;
          event.preventDefault();
          const step = event.shiftKey ? 48 : 18;
          const current = state.nodePositions[node.id];
          state.nodePositions[node.id] = clampGraphPoint({
            x: current.x + nudge.x * step,
            y: current.y + nudge.y * step
          });
          updateGraphGeometry();
          saveGraphPositions();
        });
        const tooltip = svgEl("title");
        tooltip.textContent = `${node.title}\n${formatGraphType(node.page_type)} / ${node.id}`;
        group.appendChild(tooltip);
        group.appendChild(svgEl("circle", {
          class: "graph-hit",
          cx: 0,
          cy: 0,
          r: 30
        }));
        group.appendChild(svgEl("circle", {
          class: "graph-dot-shadow",
          cx: 1.2,
          cy: 2,
          r: nodeRadius + 2
        }));
        group.appendChild(svgEl("circle", {
          class: "graph-dot-halo",
          cx: 0,
          cy: 0,
          r: nodeRadius + 5
        }));
        group.appendChild(svgEl("circle", {
          class: "graph-dot",
          cx: 0,
          cy: 0,
          r: nodeRadius,
          fill: color
        }));
        const title = svgEl("text", {
          class: "graph-title",
          x: 0,
          y: nodeRadius + 17
        });
        title.textContent = trimLabel(node.title, 20);
        group.appendChild(title);
        const meta = svgEl("text", {
          class: "graph-meta",
          x: 0,
          y: nodeRadius + 30
        });
        meta.textContent = trimLabel(formatGraphType(node.page_type), 18);
        group.appendChild(meta);
        nodeLayer.appendChild(group);
      });

      updateGraphFocusStyles();
      graphMessage(graphStatsText(nodes.length, links.length));
    }
    function isGraphNodeEvent(event) {
      return Boolean(event.target && event.target.closest && event.target.closest(".graph-node"));
    }
    function handleGraphWheel(event) {
      event.preventDefault();
      const anchor = graphViewportPoint(event);
      const factor = event.deltaY > 0 ? 0.9 : 1.1;
      setGraphZoom(state.graphView.zoom * factor, anchor);
    }
    function handleGraphPanStart(event) {
      if (event.button !== 0 || isGraphNodeEvent(event)) return;
      event.preventDefault();
      const start = graphViewportPoint(event);
      state.pan = {
        pointerId: event.pointerId,
        startX: start.x,
        startY: start.y,
        panX: state.graphView.panX,
        panY: state.graphView.panY,
        moved: false
      };
      $("graphSvg").classList.add("panning");
      try { $("graphSvg").setPointerCapture(event.pointerId); } catch (_error) {}
    }
    function handleGraphPanMove(event) {
      if (!state.pan || state.pan.pointerId !== event.pointerId) return;
      const point = graphViewportPoint(event);
      const dx = point.x - state.pan.startX;
      const dy = point.y - state.pan.startY;
      if (Math.abs(dx) > 1 || Math.abs(dy) > 1) state.pan.moved = true;
      state.graphView.panX = state.pan.panX + dx;
      state.graphView.panY = state.pan.panY + dy;
      applyGraphViewport();
    }
    function handleGraphPanEnd(event) {
      if (!state.pan || state.pan.pointerId !== event.pointerId) return;
      state.pan = null;
      $("graphSvg").classList.remove("panning");
      try { $("graphSvg").releasePointerCapture(event.pointerId); } catch (_error) {}
    }
    function newPage() {
      fillEditor({ title: "", page_type: "note", tags: [], aliases: [], confidence: 0.7, content: "" });
    }
    async function refreshWorkspace() {
      await loadPages();
      message("");
    }
    async function reindexWorkspace() {
      $("reindexResult").textContent = t("running");
      const result = await api("/api/reindex", { method: "POST", body: "{}" });
      $("reindexResult").textContent = t("indexResult", {
        indexed: result.indexed,
        removed: result.removed,
        skipped: result.skipped.length
      });
      await loadPages();
    }
    async function repairWorkspace() {
      $("healthBadge").textContent = t("repairing");
      await api("/api/install", { method: "POST", body: "{}" });
      await loadPages();
      message(t("workspaceRepaired"));
    }
    async function importKnowledgeBase() {
      $("importResult").textContent = t("running");
      const result = await api("/api/import", {
        method: "POST",
        body: JSON.stringify({
          path: $("importPath").value,
          index_title: $("importTitle").value,
          tags: $("importTags").value,
          max_bytes: Number($("importMaxBytes").value || 512000),
        })
      });
      $("importResult").textContent = t("importResult", {
        imported: result.imported.length,
        skipped: result.skipped.length
      });
      await loadPages();
      if (result.index_page && result.index_page.id) await loadPage(result.index_page.id);
    }
    $("homeBtn").addEventListener("click", showDashboard);
    $("dashboardTab").addEventListener("click", showDashboard);
    $("dashboardRefreshBtn").addEventListener("click", () => refreshWorkspace().catch((error) => message(error.message)));
    $("quickNewBtn").addEventListener("click", newPage);
    $("quickGraphBtn").addEventListener("click", showGraph);
    $("quickReindexBtn").addEventListener("click", () => reindexWorkspace().catch((error) => {
      $("reindexResult").textContent = error.message;
    }));
    $("openFirstRecentBtn").addEventListener("click", () => {
      if (state.allPages[0]) loadPage(state.allPages[0].id);
    });
    $("reindexBtn").addEventListener("click", () => reindexWorkspace().catch((error) => {
      $("reindexResult").textContent = error.message;
    }));
    $("importBtn").addEventListener("click", () => importKnowledgeBase().catch((error) => {
      $("importResult").textContent = error.message;
    }));
    $("newBtn").addEventListener("click", newPage);
    $("graphBtn").addEventListener("click", showGraph);
    $("languageBtn").addEventListener("click", () => {
      setLanguage(state.language === "zh" ? "en" : "zh");
    });
    $("editorTab").addEventListener("click", showEditor);
    $("graphTab").addEventListener("click", showGraph);
    $("refreshBtn").addEventListener("click", loadPages);
    $("refreshGraphBtn").addEventListener("click", () => loadGraph().catch((error) => graphMessage(error.message)));
    $("fitGraphBtn").addEventListener("click", fitGraphToView);
    $("resetGraphBtn").addEventListener("click", resetGraphLayout);
    $("zoomOutBtn").addEventListener("click", () => setGraphZoom(state.graphView.zoom / 1.18));
    $("zoomInBtn").addEventListener("click", () => setGraphZoom(state.graphView.zoom * 1.18));
    $("zoomResetBtn").addEventListener("click", resetGraphZoom);
    $("graphSvg").addEventListener("wheel", handleGraphWheel, { passive: false });
    $("graphSvg").addEventListener("pointerdown", handleGraphPanStart);
    $("graphSvg").addEventListener("pointermove", handleGraphPanMove);
    $("graphSvg").addEventListener("pointerup", handleGraphPanEnd);
    $("graphSvg").addEventListener("pointercancel", handleGraphPanEnd);
    $("graphMiniMap").addEventListener("pointerdown", handleMiniMapPointer);
    $("graphSearch").addEventListener("input", () => {
      state.graphFilter.query = $("graphSearch").value;
      updateGraphFocusStyles();
    });
    $("graphSearch").addEventListener("keydown", (event) => {
      if (event.key === "Enter") focusGraphSearchResult();
    });
    $("graphFocusBtn").addEventListener("click", focusGraphSearchResult);
    $("graphTypeFilter").addEventListener("change", () => {
      state.graphFilter.type = $("graphTypeFilter").value;
      renderGraphSidebar();
      updateGraphFocusStyles();
    });
    $("graphClearFocusBtn").addEventListener("click", clearGraphFocus);
    $("openGraphNodeBtn").addEventListener("click", openSelectedGraphNode);
    $("backToEditorBtn").addEventListener("click", showEditor);
    $("searchBtn").addEventListener("click", searchPages);
    $("searchInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter") searchPages();
    });
    $("archiveBtn").addEventListener("click", async () => {
      const id = $("pageId").value;
      if (!id) return;
      if (!confirm(t("archiveConfirm"))) return;
      await api(`/api/pages/${encodeURIComponent(id)}?archive=true`, { method: "DELETE" });
      state.activeId = "";
      fillEditor({ title: "", page_type: "note", tags: [], aliases: [], confidence: 0.7, content: "" });
      await loadPages();
      message(t("archived"));
    });
    $("linkBtn").addEventListener("click", async () => {
      const from = $("pageId").value || $("title").value;
      const to = $("linkTarget").value;
      if (!from || !to) {
        message(t("chooseLink"));
        return;
      }
      await api("/api/links", {
        method: "POST",
        body: JSON.stringify({
          from_selector: from,
          to_selector: to,
          relation: $("linkRelation").value || "related"
        })
      });
      $("linkTarget").value = "";
      message(t("linked"));
    });
    $("unlinkBtn").addEventListener("click", async () => {
      const from = $("pageId").value || $("title").value;
      const to = $("linkTarget").value;
      if (!from || !to) {
        message(t("chooseLink"));
        return;
      }
      const relation = $("linkRelation").value.trim();
      const body = { from_selector: from, to_selector: to };
      if (relation) body.relation = relation;
      const result = await api("/api/links", {
        method: "DELETE",
        body: JSON.stringify(body)
      });
      $("linkTarget").value = "";
      message(t("unlinked", { count: result.removed }));
    });
    $("editor").addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = await api("/api/pages", { method: "POST", body: JSON.stringify(pagePayload()) });
      fillEditor(data.page);
      await loadPages();
      message(t("saved"));
    });
    applyStaticTranslations();
    updateViewHeader();
    loadPages().catch((error) => message(error.message));
  </script>
</body>
</html>
"""
