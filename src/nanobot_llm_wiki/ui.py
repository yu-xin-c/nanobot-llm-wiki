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
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d8dde6;
      --text: #172033;
      --muted: #677085;
      --accent: #0f766e;
      --accent-2: #2563eb;
      --danger: #b42318;
    }
    * { box-sizing: border-box; }
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
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      border-radius: 6px;
      padding: 8px 10px;
      cursor: pointer;
    }
    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: white;
    }
    button.danger {
      border-color: #f3b3ad;
      color: var(--danger);
    }
    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: white;
      color: var(--text);
      padding: 9px 10px;
    }
    textarea {
      min-height: 360px;
      resize: vertical;
      line-height: 1.45;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .app {
      display: grid;
      grid-template-columns: 320px 1fr;
      min-height: 100vh;
    }
    aside {
      border-right: 1px solid var(--line);
      background: #eef2f5;
      padding: 14px;
      min-width: 0;
    }
    main {
      padding: 18px;
      min-width: 0;
    }
    .top {
      display: flex;
      gap: 8px;
      align-items: center;
      margin-bottom: 12px;
    }
    .brand {
      font-weight: 700;
      font-size: 18px;
      flex: 1;
    }
    .search {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      margin-bottom: 12px;
    }
    .list {
      display: grid;
      gap: 8px;
      max-height: calc(100vh - 128px);
      overflow: auto;
    }
    .item {
      display: block;
      width: 100%;
      text-align: left;
      background: white;
      border-radius: 8px;
      padding: 10px;
    }
    .item.active {
      border-color: var(--accent-2);
      box-shadow: inset 3px 0 0 var(--accent-2);
    }
    .item-title {
      font-weight: 650;
      overflow-wrap: anywhere;
    }
    .item-meta, .status {
      color: var(--muted);
      font-size: 13px;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }
    .editor {
      display: grid;
      gap: 12px;
      max-width: 1040px;
    }
    .row {
      display: grid;
      grid-template-columns: 1fr 160px;
      gap: 10px;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
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
    @media (max-width: 820px) {
      .app { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      .list { max-height: 260px; }
      .row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="top">
        <div class="brand">NanoBot LLM Wiki</div>
        <button id="newBtn" title="New page">+</button>
      </div>
      <div class="search">
        <input id="searchInput" placeholder="Search pages">
        <button id="searchBtn">Search</button>
      </div>
      <div id="status" class="status"></div>
      <div id="pageList" class="list"></div>
    </aside>
    <main>
      <form id="editor" class="editor">
        <input type="hidden" id="pageId">
        <div class="row">
          <div class="field">
            <label for="title">Title</label>
            <input id="title" required>
          </div>
          <div class="field">
            <label for="pageType">Type</label>
            <input id="pageType" value="note">
          </div>
        </div>
        <div class="row">
          <div class="field">
            <label for="tags">Tags</label>
            <input id="tags">
          </div>
          <div class="field">
            <label for="aliases">Aliases</label>
            <input id="aliases">
          </div>
        </div>
        <div class="field">
          <label for="content">Markdown</label>
          <textarea id="content"></textarea>
        </div>
        <div class="toolbar">
          <button class="primary" type="submit">Save</button>
          <button type="button" id="refreshBtn">Refresh</button>
          <button class="danger" type="button" id="archiveBtn">Archive</button>
        </div>
        <div id="message" class="message"></div>
      </form>
    </main>
  </div>
  <script>
    const state = { pages: [], activeId: "" };
    const $ = (id) => document.getElementById(id);
    const message = (text) => { $("message").textContent = text || ""; };
    const pagePayload = () => ({
      id: $("pageId").value,
      title: $("title").value,
      page_type: $("pageType").value || "note",
      tags: $("tags").value,
      aliases: $("aliases").value,
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
    function renderList(pages) {
      state.pages = pages;
      $("pageList").innerHTML = "";
      pages.forEach((page) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "item" + (page.id === state.activeId ? " active" : "");
        btn.innerHTML = `<div class="item-title"></div><div class="item-meta"></div>`;
        btn.querySelector(".item-title").textContent = page.title;
        btn.querySelector(".item-meta").textContent = `${page.page_type} · ${page.tags.join(", ") || "untagged"}`;
        btn.addEventListener("click", () => loadPage(page.id));
        $("pageList").appendChild(btn);
      });
    }
    function fillEditor(page) {
      state.activeId = page.id || "";
      $("pageId").value = page.id || "";
      $("title").value = page.title || "";
      $("pageType").value = page.page_type || "note";
      $("tags").value = (page.tags || []).join(", ");
      $("aliases").value = (page.aliases || []).join(", ");
      $("content").value = page.content || "";
      renderList(state.pages);
    }
    async function loadStatus() {
      const status = await api("/api/status");
      $("status").textContent = `${status.pages} pages · cursor ${status.cursor}`;
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
    }
    async function loadPage(id) {
      const data = await api(`/api/pages/${encodeURIComponent(id)}`);
      fillEditor(data.page);
      message("");
    }
    $("newBtn").addEventListener("click", () => fillEditor({ title: "", page_type: "note", tags: [], aliases: [], content: "" }));
    $("refreshBtn").addEventListener("click", loadPages);
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
      fillEditor({ title: "", page_type: "note", tags: [], aliases: [], content: "" });
      await loadPages();
      message("Archived.");
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
