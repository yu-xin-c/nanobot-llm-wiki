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


def build_server(workspace: str | Path | None, host: str, port: int) -> ThreadingHTTPServer:
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

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html()
                return
            if parsed.path == "/api/status":
                self._send_json(store.status())
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
            parsed = urlparse(self.path)
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
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/pages/"):
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            query = parse_qs(parsed.query)
            archive = (query.get("archive") or ["true"])[0].lower() not in {"0", "false", "no"}
            selector = self._selector_from_path("/api/pages/")
            try:
                page = store.forget_page(selector, archive=archive)
                store.write_memory_bridge()
            except KeyError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"page": _page_to_dict(page), "archived": archive})

    return ThreadingHTTPServer((host, port), WikiUIHandler)


def run_ui(
    workspace: str | Path | None,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    open_browser: bool = False,
) -> None:
    server = build_server(workspace, host, port)
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
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NanoBot LLM Wiki</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef2ef;
      --surface: #ffffff;
      --surface-soft: #f7f9f7;
      --surface-strong: #e8eee9;
      --line: #d5ded7;
      --line-strong: #aebbb3;
      --text: #111719;
      --muted: #63706b;
      --subtle: #84908b;
      --teal: #0f766e;
      --indigo: #4f46e5;
      --danger: #b4233f;
      --shadow: 0 18px 44px rgba(33, 43, 38, 0.10);
      --shadow-soft: 0 8px 24px rgba(33, 43, 38, 0.07);
    }
    * { box-sizing: border-box; }
    html {
      min-height: 100%;
      background: var(--bg);
    }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.75), rgba(255, 255, 255, 0) 280px),
        var(--bg);
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
      background: #f8faf8;
      box-shadow: var(--shadow-soft);
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
      border-color: #0b5f59;
      background: #0b5f59;
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
      background: #fbfcfb;
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
      grid-template-columns: minmax(310px, 382px) minmax(0, 1fr);
      min-height: 100vh;
    }
    .sidebar {
      border-right: 1px solid var(--line);
      background:
        linear-gradient(180deg, #fcfdfc, #f3f6f3);
      padding: 18px;
      min-width: 0;
      height: 100vh;
      position: sticky;
      top: 0;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .workspace {
      padding: 26px min(3vw, 36px);
      min-width: 0;
    }
    .top {
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }
    .brand-mark {
      display: grid;
      place-items: center;
      width: 38px;
      height: 38px;
      flex: 0 0 38px;
      border: 1px solid rgba(15, 118, 110, 0.28);
      border-radius: 8px;
      background: #e7f2ef;
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
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-left: auto;
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
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .status span {
      display: grid;
      gap: 2px;
      min-height: 54px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.72);
      padding: 8px 9px;
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
      gap: 8px;
      max-height: calc(100vh - 214px);
      overflow: auto;
      padding-right: 2px;
    }
    .item {
      display: grid;
      gap: 6px;
      width: 100%;
      text-align: left;
      background: rgba(255, 255, 255, 0.84);
      border-radius: 8px;
      padding: 12px 12px 12px 14px;
      border-color: rgba(255, 255, 255, 0.72);
      box-shadow: 0 1px 0 rgba(33, 43, 38, 0.04);
      position: relative;
      overflow: hidden;
      min-height: 92px;
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
      transform: translateY(-1px);
    }
    .item.active {
      border-color: rgba(79, 70, 229, 0.38);
      box-shadow: 0 12px 28px rgba(79, 70, 229, 0.12);
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
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .item-tags {
      display: flex;
      gap: 4px;
      flex-wrap: wrap;
    }
    .tag {
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      border-radius: 6px;
      background: rgba(15, 118, 110, 0.10);
      color: #0b625d;
      font-size: 11px;
      padding: 2px 7px;
    }
    .workbar {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 18px;
      max-width: 1280px;
    }
    .eyebrow {
      color: var(--teal);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      margin-bottom: 7px;
    }
    .view-title {
      margin: 0;
      font-size: 25px;
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
      background: rgba(232, 238, 233, 0.82);
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
      box-shadow: 0 1px 2px rgba(33, 43, 38, 0.08);
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
    .graph-panel {
      display: grid;
      max-width: 1480px;
      border: 1px solid rgba(113, 139, 130, 0.28);
      border-radius: 8px;
      background: #07100e;
      color: #eef8f3;
      box-shadow: 0 26px 70px rgba(15, 23, 20, 0.24);
      overflow: hidden;
    }
    .graph-panel button {
      background: rgba(255, 255, 255, 0.075);
      border-color: rgba(181, 209, 198, 0.20);
      color: #edf8f3;
    }
    .graph-panel button:hover {
      background: rgba(255, 255, 255, 0.13);
      border-color: rgba(181, 209, 198, 0.38);
      box-shadow: 0 10px 26px rgba(0, 0, 0, 0.18);
    }
    .graph-panel input,
    .graph-panel select {
      width: 100%;
      min-height: 36px;
      border: 1px solid rgba(181, 209, 198, 0.22);
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.075);
      color: #eef8f3;
      outline: none;
      padding: 8px 10px;
    }
    .graph-panel input::placeholder {
      color: rgba(224, 239, 233, 0.48);
    }
    .graph-panel input:focus,
    .graph-panel select:focus {
      border-color: #52d7c7;
      box-shadow: 0 0 0 3px rgba(82, 215, 199, 0.16);
    }
    .graph-panel select option {
      color: #111719;
      background: #ffffff;
    }
    .graph-panel .message {
      min-height: 0;
      padding: 0 16px 14px;
      color: rgba(224, 239, 233, 0.62);
      background: #07100e;
    }
    .graph-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      padding: 15px 16px;
      border-bottom: 1px solid rgba(181, 209, 198, 0.16);
      background:
        radial-gradient(circle at 16% 0%, rgba(82, 215, 199, 0.22), transparent 36%),
        linear-gradient(180deg, rgba(16, 28, 25, 0.98), rgba(8, 17, 15, 0.98));
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
      border: 1px solid rgba(82, 215, 199, 0.28);
      border-radius: 6px;
      background: rgba(82, 215, 199, 0.10);
      color: #9df2e4;
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
      grid-template-columns: 224px minmax(0, 1fr);
      min-height: 682px;
      background: #07100e;
    }
    .graph-rail,
    .graph-inspector {
      min-width: 0;
      padding: 15px;
      background:
        linear-gradient(180deg, rgba(18, 31, 28, 0.96), rgba(9, 18, 16, 0.98));
    }
    .graph-rail {
      border-right: 1px solid rgba(181, 209, 198, 0.14);
    }
    .graph-inspector {
      grid-column: 1 / -1;
      border-top: 1px solid rgba(181, 209, 198, 0.14);
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
      border-bottom: 1px solid rgba(181, 209, 198, 0.12);
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
      min-height: 58px;
      border: 1px solid rgba(181, 209, 198, 0.14);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.055);
      padding: 9px;
    }
    .graph-summary strong {
      display: block;
      color: #f6fffb;
      font-size: 20px;
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
      border-color: rgba(181, 209, 198, 0.12);
      background: rgba(255, 255, 255, 0.045);
      color: rgba(239, 250, 246, 0.82);
      padding: 6px 8px;
      text-align: left;
    }
    .graph-legend button.active {
      border-color: rgba(82, 215, 199, 0.45);
      background: rgba(82, 215, 199, 0.13);
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
      min-height: 682px;
      border: 0;
      background:
        radial-gradient(circle at 28% 22%, rgba(82, 215, 199, 0.18), transparent 27%),
        radial-gradient(circle at 76% 72%, rgba(124, 92, 255, 0.16), transparent 30%),
        linear-gradient(rgba(91, 121, 110, 0.16) 1px, transparent 1px),
        linear-gradient(90deg, rgba(91, 121, 110, 0.16) 1px, transparent 1px),
        #07100e;
      background-size: auto, auto, 34px 34px, 34px 34px, auto;
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
      border: 1px solid rgba(181, 209, 198, 0.17);
      border-radius: 6px;
      background: rgba(7, 16, 14, 0.78);
      color: rgba(230, 247, 241, 0.72);
      font-size: 11px;
      font-weight: 760;
      padding: 4px 8px;
      backdrop-filter: blur(12px);
    }
    #graphSvg {
      display: block;
      width: 100%;
      height: 682px;
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
      width: 184px;
      height: 122px;
      border: 1px solid rgba(181, 209, 198, 0.22);
      border-radius: 8px;
      background: rgba(7, 16, 14, 0.82);
      box-shadow: 0 18px 48px rgba(0, 0, 0, 0.30);
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
      stroke: rgba(159, 199, 185, 0.28);
      stroke-width: 1;
    }
    .minimap-node {
      fill: rgba(82, 215, 199, 0.72);
      stroke: rgba(239, 250, 246, 0.52);
      stroke-width: 0.8;
    }
    .minimap-node.active {
      fill: #fbbf24;
    }
    .minimap-viewport {
      fill: rgba(82, 215, 199, 0.12);
      stroke: #67e8f9;
      stroke-width: 1.2;
    }
    .graph-link-glow {
      fill: none;
      stroke: #48d9ca;
      stroke-width: 10;
      stroke-linecap: round;
      opacity: 0.13;
    }
    .graph-link {
      fill: none;
      stroke: rgba(165, 207, 191, 0.64);
      stroke-width: 1.75;
      stroke-linecap: round;
      opacity: 0.82;
    }
    .graph-link.active {
      stroke: #67e8f9;
      stroke-width: 2.25;
      opacity: 1;
    }
    .graph-link-glow.active {
      stroke: #67e8f9;
      opacity: 0.30;
    }
    .graph-link.muted,
    .graph-link-glow.muted,
    .graph-relation-pill.muted {
      opacity: 0.10;
    }
    .graph-relation-pill rect {
      fill: rgba(7, 16, 14, 0.92);
      stroke: rgba(181, 209, 198, 0.25);
      stroke-width: 1;
    }
    .graph-relation {
      fill: rgba(229, 248, 241, 0.82);
      font-size: 11px;
      font-weight: 750;
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
    .graph-node .graph-card-shadow {
      fill: #000000;
      opacity: 0.30;
    }
    .graph-node .graph-card {
      fill: rgba(18, 31, 28, 0.96);
      stroke: rgba(181, 209, 198, 0.28);
      stroke-width: 1.2;
    }
    .graph-node .graph-band {
      opacity: 0.95;
    }
    .graph-node .graph-orbit {
      fill: rgba(255, 255, 255, 0.065);
      stroke-width: 1.2;
    }
    .graph-node.active .graph-card {
      fill: rgba(13, 47, 43, 0.98);
      stroke: #67e8f9;
      stroke-width: 1.9;
    }
    .graph-node.neighbor .graph-card {
      stroke: rgba(104, 211, 194, 0.58);
    }
    .graph-node.muted {
      opacity: 0.20;
    }
    .graph-node .graph-title {
      fill: #f6fffb;
      font-size: 12px;
      font-weight: 800;
      pointer-events: none;
    }
    .graph-node .graph-meta {
      fill: rgba(224, 239, 233, 0.58);
      font-size: 10.5px;
      pointer-events: none;
    }
    .graph-node .graph-chip-bg {
      fill: rgba(255, 255, 255, 0.07);
      stroke: rgba(181, 209, 198, 0.16);
      stroke-width: 1;
    }
    .graph-node .graph-chip-text {
      fill: rgba(237, 248, 244, 0.76);
      font-size: 10px;
      font-weight: 700;
      pointer-events: none;
    }
    #graphViewport.lod-minimal .graph-chip-bg,
    #graphViewport.lod-minimal .graph-chip-text,
    #graphViewport.lod-minimal .graph-meta,
    #graphViewport.lod-minimal .graph-relation-pill {
      display: none;
    }
    #graphViewport.lod-minimal .graph-node .graph-title {
      font-size: 11px;
    }
    .zoom-controls {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      border: 1px solid rgba(181, 209, 198, 0.20);
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
      margin: 0;
      color: #f6fffb;
      font-size: 18px;
      line-height: 1.18;
      overflow-wrap: anywhere;
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
      border: 1px solid rgba(181, 209, 198, 0.13);
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
      border-bottom: 1px solid rgba(181, 209, 198, 0.11);
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
      background: rgba(82, 215, 199, 0.13);
      color: #9df2e4;
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
    @media (min-width: 1840px) {
      .graph-workbench {
        grid-template-columns: 224px minmax(0, 1fr) 288px;
      }
      .graph-inspector {
        grid-column: auto;
        border-left: 1px solid rgba(181, 209, 198, 0.14);
        border-top: 0;
      }
    }
    @media (max-width: 980px) {
      .app { grid-template-columns: 1fr; }
      .sidebar {
        position: static;
        height: auto;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
      .list { max-height: 340px; }
      .row { grid-template-columns: 1fr; }
      .meta-grid { grid-template-columns: 1fr; }
      .wide-field { grid-column: auto; }
      .editor { grid-template-columns: 1fr; }
      .page-inspector { border-left: 0; border-top: 1px solid var(--line); }
      .workbar { display: grid; }
      .graph-workbench { grid-template-columns: 1fr; }
      .graph-rail,
      .graph-inspector {
        grid-column: auto;
        border-left: 0;
        border-right: 0;
        border-bottom: 1px solid rgba(181, 209, 198, 0.14);
      }
      .graph-shell,
      #graphSvg {
        min-height: 560px;
        height: 560px;
      }
    }
    @media (max-width: 620px) {
      .workspace { padding: 18px; }
      .top { display: grid; grid-template-columns: 38px 1fr; }
      .side-actions { grid-column: 1 / -1; margin-left: 0; }
      .search { grid-template-columns: 1fr; }
      .status { grid-template-columns: 1fr 1fr 1fr; }
      .view-switch { width: 100%; }
      .view-switch button { flex: 1; }
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
        <div class="brand">NanoBot LLM Wiki<div class="brand-subtitle">Local memory workspace</div></div>
        <div class="side-actions">
          <button id="newBtn" title="New page"><span class="button-mark">+</span>New</button>
          <button id="graphBtn" class="ghost" title="Graph view">Graph</button>
        </div>
      </div>
      <div class="search">
        <input id="searchInput" placeholder="Search titles, tags, aliases">
        <button id="searchBtn">Search</button>
      </div>
      <div id="status" class="status"></div>
      <div class="list-heading"><span>Pages</span><span id="listCount">0</span></div>
      <div id="pageList" class="list"></div>
    </aside>
    <main class="workspace">
      <div class="workbar">
        <div>
          <div class="eyebrow">Knowledge Console</div>
          <h1 id="viewTitle" class="view-title">Memory Editor</h1>
          <div id="viewSubtitle" class="view-subtitle">Local durable memory</div>
        </div>
        <div class="view-switch">
          <button type="button" id="editorTab" class="active">Editor</button>
          <button type="button" id="graphTab">Graph</button>
        </div>
      </div>
      <form id="editor" class="editor">
        <div class="editor-main">
          <input type="hidden" id="pageId">
          <div class="section-label">Page Details</div>
          <div class="meta-grid">
            <div class="field wide-field">
              <label for="title">Title</label>
              <input id="title" required>
            </div>
            <div class="field">
              <label for="pageType">Type</label>
              <input id="pageType" value="note">
            </div>
            <div class="field wide-field">
              <label for="tags">Tags</label>
              <input id="tags">
            </div>
            <div class="field">
              <label for="confidence">Confidence</label>
              <input id="confidence" type="number" min="0" max="1" step="0.01" value="0.70">
            </div>
          </div>
          <div class="field">
            <label for="aliases">Aliases</label>
            <input id="aliases">
          </div>
          <div class="field">
            <label for="content">Markdown</label>
            <textarea id="content"></textarea>
          </div>
          <div class="section-label">Relationship</div>
          <div class="row">
            <div class="field">
              <label for="linkTarget">Link To</label>
              <input id="linkTarget" placeholder="Page title or id">
            </div>
            <div class="field">
              <label for="linkRelation">Relation</label>
              <input id="linkRelation" value="related">
            </div>
          </div>
          <div class="toolbar">
            <button class="primary" type="submit">Save</button>
            <button type="button" id="linkBtn">Link</button>
            <button type="button" id="refreshBtn">Refresh</button>
            <button class="danger" type="button" id="archiveBtn">Archive</button>
          </div>
          <div id="message" class="message"></div>
        </div>
        <aside class="page-inspector">
          <div class="inspector-heading">Page Signals</div>
          <dl class="signal-list">
            <div><dt>ID</dt><dd id="detailId">New page</dd></div>
            <div><dt>Updated</dt><dd id="detailUpdated">Not saved</dd></div>
            <div><dt>Confidence</dt><dd id="detailConfidence">0.70</dd></div>
            <div><dt>Sources</dt><dd id="detailSources">0 cursors</dd></div>
          </dl>
          <div id="detailTags" class="signal-tags"></div>
        </aside>
      </form>
      <section id="graphPanel" class="graph-panel hidden">
        <div class="graph-top">
          <div class="graph-heading">
            <div class="graph-titleline">
              <div class="section-label">Knowledge Graph</div>
              <span class="graph-perspective">Local Memory</span>
            </div>
            <div id="graphStats" class="graph-stats">0 pages / 0 links</div>
          </div>
          <div class="toolbar graph-toolbar">
            <button type="button" id="backToEditorBtn">Editor</button>
            <button type="button" id="refreshGraphBtn">Refresh</button>
            <button type="button" id="fitGraphBtn">Fit</button>
            <button type="button" id="resetGraphBtn">Reset Layout</button>
            <div class="zoom-controls" aria-label="Graph zoom controls">
              <button type="button" id="zoomOutBtn" aria-label="Zoom out">-</button>
              <span id="zoomValue" class="zoom-value">100%</span>
              <button type="button" id="zoomInBtn" aria-label="Zoom in">+</button>
              <button type="button" id="zoomResetBtn" aria-label="Reset zoom">1:1</button>
            </div>
          </div>
        </div>
        <div class="graph-workbench">
          <aside class="graph-rail">
            <div class="graph-panel-label">Explore</div>
            <div class="graph-control-block">
              <div class="graph-search-row">
                <input id="graphSearch" type="search" placeholder="Search graph" aria-label="Search graph">
                <button type="button" id="graphFocusBtn">Focus</button>
              </div>
              <div class="graph-control-row">
                <select id="graphTypeFilter" aria-label="Filter by page type">
                  <option value="all">All types</option>
                </select>
                <button type="button" id="graphClearFocusBtn">Clear</button>
              </div>
            </div>
            <div id="graphSummary" class="graph-summary"></div>
            <div class="graph-control-block"></div>
            <div class="graph-panel-label">Legend</div>
            <div id="graphLegend" class="graph-legend"></div>
          </aside>
          <div class="graph-shell" title="Wheel to zoom. Drag empty canvas to pan. Drag nodes to arrange the graph.">
            <div class="graph-canvas-status">
              <span id="graphFocusLabel">All nodes</span>
              <span id="graphDensityLabel">0 visible</span>
            </div>
            <svg id="graphSvg" role="img" aria-label="Wiki page graph" aria-description="Click a node to inspect it. Double-click a node to open it in the editor."></svg>
            <div class="graph-minimap" aria-label="Graph minimap">
              <svg id="graphMiniMap" role="img" aria-label="Graph minimap"></svg>
            </div>
          </div>
          <aside class="graph-inspector">
            <div class="graph-panel-label">Inspector</div>
            <div id="graphInspectorEmpty" class="graph-empty-state">No selection</div>
            <div id="graphInspectorContent" class="hidden">
              <h2 id="graphDetailTitle" class="graph-inspector-title"></h2>
              <div id="graphDetailMeta" class="graph-inspector-meta"></div>
              <dl class="graph-detail-list">
                <div><dt>ID</dt><dd id="graphDetailId"></dd></div>
                <div><dt>Connections</dt><dd id="graphDetailDegree"></dd></div>
                <div><dt>Updated</dt><dd id="graphDetailUpdated"></dd></div>
              </dl>
              <div id="graphDetailTags" class="graph-tag-stack"></div>
              <div class="graph-control-block"></div>
              <div class="graph-panel-label">Linked Nodes</div>
              <div id="graphDetailLinks" class="graph-link-stack"></div>
              <div class="graph-control-block"></div>
              <button type="button" id="openGraphNodeBtn" class="primary">Open in Editor</button>
            </div>
          </aside>
        </div>
        <div id="graphMessage" class="message"></div>
      </section>
    </main>
  </div>
  <script>
    const graphPositionStoreKey = "nanobot_llm_wiki_graph_positions_v2";
    const graphNodeSize = { width: 176, height: 78 };
    const state = {
      pages: [],
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
      const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Request failed");
      return data;
    }
    function setView(mode) {
      $("editorTab").classList.toggle("active", mode === "editor");
      $("graphTab").classList.toggle("active", mode === "graph");
    }
    function showEditor() {
      $("editor").classList.remove("hidden");
      $("graphPanel").classList.add("hidden");
      setView("editor");
      const title = $("title").value || "Memory Editor";
      $("viewTitle").textContent = title;
      $("viewSubtitle").textContent = $("pageId").value || "New page";
    }
    function showGraph() {
      $("editor").classList.add("hidden");
      $("graphPanel").classList.remove("hidden");
      setView("graph");
      $("viewTitle").textContent = "Memory Graph";
      $("viewSubtitle").textContent = "Relationship map";
      if (!state.graphSelectedId && state.activeId) state.graphSelectedId = state.activeId;
      loadGraph().catch((error) => graphMessage(error.message));
    }
    function renderList(pages) {
      state.pages = pages;
      $("pageList").innerHTML = "";
      $("listCount").textContent = String(pages.length);
      pages.forEach((page) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "item" + (page.id === state.activeId ? " active" : "");
        btn.style.setProperty("--item-color", nodeColor(page));
        btn.innerHTML = `<div class="item-title"></div><div class="item-meta"></div><div class="item-preview"></div><div class="item-tags"></div>`;
        btn.querySelector(".item-title").textContent = page.title;
        btn.querySelector(".item-meta").textContent = `${page.page_type} / ${page.id}`;
        btn.querySelector(".item-preview").textContent = compactText(page.content || "", 118) || "No content yet.";
        const tags = btn.querySelector(".item-tags");
        (page.tags && page.tags.length ? page.tags : ["untagged"]).slice(0, 4).forEach((tag) => {
          const chip = document.createElement("span");
          chip.className = "tag";
          chip.textContent = tag;
          tags.appendChild(chip);
        });
        btn.addEventListener("click", () => loadPage(page.id));
        $("pageList").appendChild(btn);
      });
    }
    function compactText(value, limit) {
      const text = String(value || "").replace(/\s+/g, " ").trim();
      return text.length > limit ? text.slice(0, limit - 1).trim() + "..." : text;
    }
    function formatDate(value) {
      if (!value) return "Not saved";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    }
    function updateInspector(page) {
      const confidence = Number.isFinite(Number(page.confidence)) ? Number(page.confidence) : 0.7;
      const cursors = page.source_cursors || [];
      $("detailId").textContent = page.id || "New page";
      $("detailUpdated").textContent = formatDate(page.updated_at);
      $("detailConfidence").textContent = confidence.toFixed(2);
      $("detailSources").textContent = `${cursors.length} cursor${cursors.length === 1 ? "" : "s"}`;
      $("detailTags").innerHTML = "";
      (page.tags && page.tags.length ? page.tags : ["untagged"]).forEach((tag) => {
        const chip = document.createElement("span");
        chip.className = "tag";
        chip.textContent = tag;
        $("detailTags").appendChild(chip);
      });
    }
    function fillEditor(page) {
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
      renderList(state.pages);
      showEditor();
    }
    async function loadStatus() {
      const status = await api("/api/status");
      $("status").innerHTML = "";
      [["Pages", status.pages], ["Links", status.links], ["Cursor", status.cursor]].forEach(([label, value]) => {
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
    async function loadPages() {
      const data = await api("/api/pages?limit=200");
      renderList(data.pages);
      if (!state.activeId && data.pages[0]) fillEditor(data.pages[0]);
      await loadStatus();
    }
    async function searchPages() {
      const q = encodeURIComponent($("searchInput").value.trim());
      const data = q ? await api(`/api/search?q=${q}&limit=50`) : await api("/api/pages?limit=200");
      const pages = data.results ? data.results.map((result) => result.page) : data.pages;
      renderList(pages);
      message(q ? `${pages.length} result${pages.length === 1 ? "" : "s"}.` : "");
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
      return Math.max(54, Math.min(132, text.length * 7 + 22));
    }
    function tagChipWidth(text) {
      return Math.max(36, Math.min(74, text.length * 6 + 16));
    }
    function nodeMetaText(node) {
      return trimLabel(`${node.page_type || "note"} / ${node.id}`, 31);
    }
    function nodeColor(node) {
      const value = `${node.page_type || ""} ${(node.tags || []).join(" ")}`.toLowerCase();
      if (value.includes("profile") || value.includes("user")) return "#0f766e";
      if (value.includes("project")) return "#ad741d";
      if (value.includes("question")) return "#b4235a";
      if (value.includes("inbox") || value.includes("history")) return "#65716b";
      return "#4f46e5";
    }
    function graphNodeById(id) {
      return (state.graph.nodes || []).find((node) => node.id === id);
    }
    function graphTypeKey(node) {
      return String(node.page_type || "note").toLowerCase();
    }
    function formatGraphType(value) {
      const text = String(value || "note").replace(/[-_]/g, " ");
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
      allOption.textContent = "All types";
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
      [["Nodes", nodes.length], ["Links", links.length], ["Types", typeCounts.size], ["Avg degree", averageDegree]].forEach(([label, value]) => {
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
      $("graphDensityLabel").textContent = `${matchingCount} visible`;
      $("graphFocusLabel").textContent = selected ? trimLabel(selected.title, 42) : "All nodes";
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
      $("graphDetailMeta").textContent = `${formatGraphType(node.page_type)} / ${node.id}`;
      $("graphDetailId").textContent = node.id;
      $("graphDetailUpdated").textContent = formatDate(node.updated_at);

      const connections = graphConnections(node.id);
      $("graphDetailDegree").textContent = `${connections.length} link${connections.length === 1 ? "" : "s"}`;
      const tags = $("graphDetailTags");
      tags.innerHTML = "";
      (node.tags && node.tags.length ? node.tags : ["untagged"]).slice(0, 8).forEach((tag) => {
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
        emptyLink.textContent = "No linked nodes";
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
    function edgePoint(source, target) {
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const halfW = graphNodeSize.width / 2 + 5;
      const halfH = graphNodeSize.height / 2 + 5;
      const scale = Math.max(Math.abs(dx) / halfW, Math.abs(dy) / halfH, 1);
      return {
        x: source.x + dx / scale,
        y: source.y + dy / scale
      };
    }
    function routeLink(from, to) {
      const start = edgePoint(from, to);
      const end = edgePoint(to, from);
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
        const route = routeLink(from, to);
        const path = document.querySelector(`[data-link-path="${index}"]`);
        const glow = document.querySelector(`[data-link-glow="${index}"]`);
        const label = document.querySelector(`[data-link-label="${index}"]`);
        const d = `M ${route.startX} ${route.startY} Q ${route.midX} ${route.midY} ${route.endX} ${route.endY}`;
        if (glow) glow.setAttribute("d", d);
        if (path) path.setAttribute("d", d);
        if (label) {
          label.setAttribute("transform", `translate(${route.midX} ${route.midY - 8})`);
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
      const width = Math.max(svg.clientWidth || 940, 680);
      const height = Math.max(svg.clientHeight || 620, 480);
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
      state.graphSize = { width, height };

      const defs = svgEl("defs");
      const marker = svgEl("marker", {
        id: "arrow",
        viewBox: "0 0 10 10",
        refX: "9",
        refY: "5",
        markerWidth: "6",
        markerHeight: "6",
        orient: "auto-start-reverse"
      });
      marker.appendChild(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#9fc7b9" }));
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
      $("graphStats").textContent = `${nodes.length} pages / ${links.length} links`;
      renderGraphSidebar();
      if (!nodes.length) {
        const empty = svgEl("text", { x: width / 2, y: height / 2, "text-anchor": "middle", fill: "rgba(224,239,233,0.62)" });
        empty.textContent = "No Wiki pages yet.";
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
        const route = routeLink(from, to);
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
          transform: `translate(${route.midX} ${route.midY - 8})`
        });
        labelGroup.appendChild(svgEl("rect", {
          x: -labelWidth / 2,
          y: -12,
          width: labelWidth,
          height: 24,
          rx: 8
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
        const width = graphNodeSize.width;
        const height = graphNodeSize.height;
        const left = -width / 2;
        const top = -height / 2;
        const group = svgEl("g", {
          class: "graph-node" + (node.id === state.graphSelectedId ? " active" : ""),
          "data-node-id": node.id,
          transform: `translate(${pos.x} ${pos.y})`
        });
        group.setAttribute("tabindex", "0");
        group.style.cursor = "grab";
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
        tooltip.textContent = `${node.title}\n${node.page_type || "note"} / ${node.id}`;
        group.appendChild(tooltip);
        group.appendChild(svgEl("rect", {
          class: "graph-hit",
          x: left - 10,
          y: top - 10,
          width: width + 20,
          height: height + 20,
          rx: 8
        }));
        group.appendChild(svgEl("rect", {
          class: "graph-card-shadow",
          x: left + 4,
          y: top + 6,
          width,
          height,
          rx: 8
        }));
        group.appendChild(svgEl("rect", {
          class: "graph-card",
          x: left,
          y: top,
          width,
          height,
          rx: 8
        }));
        group.appendChild(svgEl("rect", {
          class: "graph-band",
          x: left,
          y: top,
          width: 6,
          height,
          rx: 4,
          fill: color
        }));
        group.appendChild(svgEl("circle", {
          class: "graph-orbit",
          cx: width / 2 - 20,
          cy: top + 18,
          r: 8,
          stroke: color
        }));
        group.appendChild(svgEl("circle", {
          cx: width / 2 - 20,
          cy: top + 18,
          r: 3.4,
          fill: color
        }));
        const title = svgEl("text", {
          class: "graph-title",
          x: left + 18,
          y: top + 23
        });
        title.textContent = trimLabel(node.title, 25);
        group.appendChild(title);
        const meta = svgEl("text", {
          class: "graph-meta",
          x: left + 18,
          y: top + 43
        });
        meta.textContent = nodeMetaText(node);
        group.appendChild(meta);

        let chipX = left + 18;
        const chipY = top + 54;
        const chips = (node.tags && node.tags.length ? node.tags : [node.page_type || "note"]).slice(0, 2);
        chips.forEach((tag) => {
          const chipText = trimLabel(tag, 10);
          const chipWidth = tagChipWidth(chipText);
          if (chipX + chipWidth > width / 2 - 12) return;
          group.appendChild(svgEl("rect", {
            class: "graph-chip-bg",
            x: chipX,
            y: chipY,
            width: chipWidth,
            height: 18,
            rx: 6
          }));
          const chipLabel = svgEl("text", {
            class: "graph-chip-text",
            x: chipX + 8,
            y: chipY + 12
          });
          chipLabel.textContent = chipText;
          group.appendChild(chipLabel);
          chipX += chipWidth + 6;
        });
        nodeLayer.appendChild(group);
      });

      updateGraphFocusStyles();
      graphMessage(`${nodes.length} pages / ${links.length} links`);
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
    $("newBtn").addEventListener("click", () => fillEditor({ title: "", page_type: "note", tags: [], aliases: [], confidence: 0.7, content: "" }));
    $("graphBtn").addEventListener("click", showGraph);
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
      if (!confirm("Archive this page?")) return;
      await api(`/api/pages/${encodeURIComponent(id)}?archive=true`, { method: "DELETE" });
      state.activeId = "";
      fillEditor({ title: "", page_type: "note", tags: [], aliases: [], confidence: 0.7, content: "" });
      await loadPages();
      message("Archived.");
    });
    $("linkBtn").addEventListener("click", async () => {
      const from = $("pageId").value || $("title").value;
      const to = $("linkTarget").value;
      if (!from || !to) {
        message("Choose the current page and a target page first.");
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
      message("Linked.");
    });
    $("editor").addEventListener("submit", async (event) => {
      event.preventDefault();
      const data = await api("/api/pages", { method: "POST", body: JSON.stringify(pagePayload()) });
      fillEditor(data.page);
      await loadPages();
      message("Saved.");
    });
    loadPages().catch((error) => message(error.message));
  </script>
</body>
</html>
"""
