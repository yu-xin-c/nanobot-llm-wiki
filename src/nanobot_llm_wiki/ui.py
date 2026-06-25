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
      --bg: #f4f6f8;
      --panel: #ffffff;
      --panel-soft: #f9fbfc;
      --line: #d9e0e7;
      --line-strong: #c5ced8;
      --text: #182131;
      --muted: #667085;
      --accent: #0f766e;
      --accent-2: #2563eb;
      --accent-3: #b7791f;
      --danger: #b42318;
      --shadow: 0 18px 42px rgba(25, 35, 50, 0.08);
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
      min-height: 36px;
      padding: 8px 11px;
      cursor: pointer;
      transition: border-color 120ms ease, background 120ms ease, transform 120ms ease;
      white-space: nowrap;
    }
    button:hover {
      border-color: var(--line-strong);
      background: #f8fafc;
    }
    button:active {
      transform: translateY(1px);
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
      padding: 10px 11px;
      outline: none;
    }
    input:focus, textarea:focus {
      border-color: var(--accent-2);
      box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12);
    }
    textarea {
      min-height: 430px;
      resize: vertical;
      line-height: 1.45;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
    }
    .app {
      display: grid;
      grid-template-columns: 360px 1fr;
      min-height: 100vh;
    }
    aside {
      border-right: 1px solid var(--line);
      background: #fbfcfd;
      padding: 16px;
      min-width: 0;
    }
    main {
      padding: 24px;
      min-width: 0;
    }
    .top {
      display: flex;
      gap: 8px;
      align-items: flex-start;
      margin-bottom: 14px;
    }
    .brand {
      font-weight: 700;
      font-size: 19px;
      flex: 1;
      line-height: 1.2;
    }
    .brand-subtitle {
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
      margin-top: 3px;
    }
    .side-actions {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .search {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      margin-bottom: 10px;
    }
    .status {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
      margin: 10px 0 14px;
      overflow-wrap: anywhere;
    }
    .status span {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f7fafc;
      padding: 3px 8px;
    }
    .list {
      display: grid;
      gap: 7px;
      max-height: calc(100vh - 150px);
      overflow: auto;
      padding-right: 2px;
    }
    .item {
      display: block;
      width: 100%;
      text-align: left;
      background: white;
      border-radius: 8px;
      padding: 11px 12px;
      border-color: transparent;
      box-shadow: 0 1px 2px rgba(20, 30, 45, 0.04);
    }
    .item.active {
      border-color: var(--accent-2);
      box-shadow: inset 3px 0 0 var(--accent-2), 0 8px 18px rgba(37, 99, 235, 0.08);
    }
    .item-title {
      font-weight: 650;
      overflow-wrap: anywhere;
    }
    .item-meta {
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }
    .item-tags {
      display: flex;
      gap: 4px;
      flex-wrap: wrap;
      margin-top: 7px;
    }
    .tag {
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      border-radius: 999px;
      background: #eef7f6;
      color: #0b5c55;
      font-size: 11px;
      padding: 2px 7px;
    }
    .workbar {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 14px;
      max-width: 1120px;
    }
    .view-title {
      margin: 0;
      font-size: 22px;
      line-height: 1.15;
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
      background: #eef2f5;
      padding: 3px;
    }
    .view-switch button {
      min-height: 30px;
      border: 0;
      background: transparent;
      padding: 5px 10px;
    }
    .view-switch button.active {
      background: white;
      box-shadow: 0 1px 2px rgba(20, 30, 45, 0.08);
    }
    .editor {
      display: grid;
      gap: 12px;
      max-width: 1040px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      box-shadow: var(--shadow);
      padding: 16px;
    }
    .row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 180px;
      gap: 12px;
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
    .hidden {
      display: none !important;
    }
    .graph-panel {
      display: grid;
      gap: 12px;
      max-width: 1120px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      box-shadow: var(--shadow);
      padding: 16px;
    }
    .graph-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .legend {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }
    .legend span {
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      display: inline-block;
    }
    .graph-shell {
      min-height: 560px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background:
        linear-gradient(#eef3f8 1px, transparent 1px),
        linear-gradient(90deg, #eef3f8 1px, transparent 1px),
        #fbfdff;
      background-size: 24px 24px;
      overflow: hidden;
    }
    #graphSvg {
      display: block;
      width: 100%;
      height: 560px;
    }
    .graph-link {
      fill: none;
      stroke: #8799ad;
      stroke-width: 1.7;
      opacity: 0.82;
    }
    .graph-relation {
      fill: var(--muted);
      font-size: 12px;
      paint-order: stroke;
      stroke: white;
      stroke-width: 5px;
      stroke-linejoin: round;
    }
    .graph-node circle {
      fill: #ffffff;
      stroke-width: 2.4;
      filter: drop-shadow(0 8px 12px rgba(30, 50, 75, 0.12));
    }
    .graph-node.active circle {
      fill: #e8f3ff;
      stroke: var(--accent);
      stroke-width: 3;
    }
    .graph-node text {
      fill: var(--text);
      font-size: 12px;
      text-anchor: middle;
      pointer-events: none;
    }
    @media (max-width: 820px) {
      .app { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      .list { max-height: 340px; }
      .row { grid-template-columns: 1fr; }
      .workbar { display: grid; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="top">
        <div class="brand">NanoBot LLM Wiki<div class="brand-subtitle">Local memory pages and graph</div></div>
        <div class="side-actions">
          <button id="newBtn" title="New page">New</button>
          <button id="graphBtn" title="Graph view">Graph</button>
        </div>
      </div>
      <div class="search">
        <input id="searchInput" placeholder="Search pages">
        <button id="searchBtn">Search</button>
      </div>
      <div id="status" class="status"></div>
      <div id="pageList" class="list"></div>
    </aside>
    <main>
      <div class="workbar">
        <div>
          <h1 id="viewTitle" class="view-title">Memory Pages</h1>
          <div id="viewSubtitle" class="view-subtitle">Browse, connect, and refine durable NanoBot memory.</div>
        </div>
        <div class="view-switch">
          <button type="button" id="editorTab" class="active">Editor</button>
          <button type="button" id="graphTab">Graph</button>
        </div>
      </div>
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
      </form>
      <section id="graphPanel" class="graph-panel hidden">
        <div class="graph-top">
          <div class="legend">
            <span><i class="dot" style="background:#2563eb"></i>Note</span>
            <span><i class="dot" style="background:#0f766e"></i>Profile</span>
            <span><i class="dot" style="background:#b7791f"></i>Project</span>
            <span><i class="dot" style="background:#8b5cf6"></i>Question</span>
          </div>
          <div class="toolbar">
            <button type="button" id="backToEditorBtn">Editor</button>
            <button type="button" id="refreshGraphBtn">Refresh Graph</button>
          </div>
        </div>
        <div class="graph-shell">
          <svg id="graphSvg" role="img" aria-label="Wiki page graph"></svg>
        </div>
        <div id="graphMessage" class="message"></div>
      </section>
    </main>
  </div>
  <script>
    const state = { pages: [], activeId: "", graph: { nodes: [], links: [] } };
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
      const title = $("title").value || "Memory Pages";
      $("viewTitle").textContent = title;
      $("viewSubtitle").textContent = $("pageId").value ? `Editing ${$("pageId").value}` : "Create or refine a Wiki memory page.";
    }
    function showGraph() {
      $("editor").classList.add("hidden");
      $("graphPanel").classList.remove("hidden");
      setView("graph");
      $("viewTitle").textContent = "Memory Graph";
      $("viewSubtitle").textContent = "Pages are nodes. Links are typed relationships.";
      loadGraph().catch((error) => graphMessage(error.message));
    }
    function renderList(pages) {
      state.pages = pages;
      $("pageList").innerHTML = "";
      pages.forEach((page) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "item" + (page.id === state.activeId ? " active" : "");
        btn.innerHTML = `<div class="item-title"></div><div class="item-meta"></div><div class="item-tags"></div>`;
        btn.querySelector(".item-title").textContent = page.title;
        btn.querySelector(".item-meta").textContent = `${page.page_type} · ${page.id}`;
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
    function fillEditor(page) {
      state.activeId = page.id || "";
      $("pageId").value = page.id || "";
      $("title").value = page.title || "";
      $("pageType").value = page.page_type || "note";
      $("tags").value = (page.tags || []).join(", ");
      $("aliases").value = (page.aliases || []).join(", ");
      $("content").value = page.content || "";
      $("linkTarget").value = "";
      $("linkRelation").value = "related";
      renderList(state.pages);
      showEditor();
    }
    async function loadStatus() {
      const status = await api("/api/status");
      $("status").innerHTML = "";
      [["Pages", status.pages], ["Links", status.links], ["Cursor", status.cursor]].forEach(([label, value]) => {
        const item = document.createElement("span");
        item.textContent = `${label}: ${value}`;
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
    function nodeColor(node) {
      const value = `${node.page_type || ""} ${(node.tags || []).join(" ")}`.toLowerCase();
      if (value.includes("profile") || value.includes("user")) return "#0f766e";
      if (value.includes("project")) return "#b7791f";
      if (value.includes("question")) return "#8b5cf6";
      if (value.includes("inbox") || value.includes("history")) return "#64748b";
      return "#2563eb";
    }
    async function loadGraph() {
      state.graph = await api("/api/graph?limit=200");
      renderGraph();
    }
    function renderGraph() {
      const svg = $("graphSvg");
      svg.innerHTML = "";
      const width = Math.max(svg.clientWidth || 900, 640);
      const height = Math.max(svg.clientHeight || 560, 420);
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

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
      marker.appendChild(svgEl("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#8ca0b3" }));
      defs.appendChild(marker);
      svg.appendChild(defs);

      const nodes = state.graph.nodes || [];
      const links = state.graph.links || [];
      if (!nodes.length) {
        const empty = svgEl("text", { x: width / 2, y: height / 2, "text-anchor": "middle", fill: "#677085" });
        empty.textContent = "No Wiki pages yet.";
        svg.appendChild(empty);
        graphMessage("");
        return;
      }

      const centerX = width / 2;
      const centerY = height / 2;
      const radius = Math.max(90, Math.min(width, height) * 0.34);
      const positions = new Map();
      nodes.forEach((node, index) => {
        const angle = nodes.length === 1 ? -Math.PI / 2 : (Math.PI * 2 * index) / nodes.length - Math.PI / 2;
        positions.set(node.id, {
          x: centerX + Math.cos(angle) * radius,
          y: centerY + Math.sin(angle) * radius
        });
      });

      links.forEach((link) => {
        const from = positions.get(link.from_id);
        const to = positions.get(link.to_id);
        if (!from || !to) return;
        const dx = to.x - from.x;
        const dy = to.y - from.y;
        const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
        const offset = 36;
        const startX = from.x + (dx / dist) * offset;
        const startY = from.y + (dy / dist) * offset;
        const endX = to.x - (dx / dist) * offset;
        const endY = to.y - (dy / dist) * offset;
        const curve = Math.min(48, dist * 0.18);
        const midX = (startX + endX) / 2 - (dy / dist) * curve;
        const midY = (startY + endY) / 2 + (dx / dist) * curve;
        const path = svgEl("path", {
          class: "graph-link",
          d: `M ${startX} ${startY} Q ${midX} ${midY} ${endX} ${endY}`,
          "marker-end": "url(#arrow)"
        });
        svg.appendChild(path);

        const label = svgEl("text", {
          class: "graph-relation",
          x: midX,
          y: midY - 6,
          "text-anchor": "middle"
        });
        label.textContent = trimLabel(link.relation, 18);
        svg.appendChild(label);
      });

      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        const group = svgEl("g", { class: "graph-node" + (node.id === state.activeId ? " active" : "") });
        group.setAttribute("tabindex", "0");
        group.style.cursor = "pointer";
        group.addEventListener("click", () => loadPage(node.id));
        group.addEventListener("keydown", (event) => {
          if (event.key === "Enter") loadPage(node.id);
        });
        group.appendChild(svgEl("circle", {
          cx: pos.x,
          cy: pos.y,
          r: 32,
          fill: "#ffffff",
          stroke: nodeColor(node)
        }));
        const title = svgEl("text", { x: pos.x, y: pos.y + 46 });
        title.textContent = trimLabel(node.title);
        group.appendChild(title);
        svg.appendChild(group);
      });

      graphMessage(`${nodes.length} pages · ${links.length} links`);
    }
    $("newBtn").addEventListener("click", () => fillEditor({ title: "", page_type: "note", tags: [], aliases: [], content: "" }));
    $("graphBtn").addEventListener("click", showGraph);
    $("editorTab").addEventListener("click", showEditor);
    $("graphTab").addEventListener("click", showGraph);
    $("refreshBtn").addEventListener("click", loadPages);
    $("refreshGraphBtn").addEventListener("click", () => loadGraph().catch((error) => graphMessage(error.message)));
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
      fillEditor({ title: "", page_type: "note", tags: [], aliases: [], content: "" });
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
