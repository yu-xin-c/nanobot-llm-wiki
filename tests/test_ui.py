from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from nanobot_llm_wiki.storage import WikiStore
from nanobot_llm_wiki.ui import build_server


def _json_get(url: str) -> dict:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _json_request(url: str, *, method: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_ui_serves_page_and_api(tmp_path) -> None:
    store = WikiStore(tmp_path)
    store.upsert_page(title="UI Smoke", content="The browser UI can read this page.", tags=["ui"])
    server = build_server(tmp_path, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(base_url + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
        assert "NanoBot LLM Wiki" in html
        assert "Memory Dashboard" in html
        assert '<html lang="zh-CN">' in html
        assert 'id="languageBtn"' in html
        assert 'id="unlinkBtn"' in html
        assert 'data-i18n="dashboard"' in html
        assert "nanobot_llm_wiki_language_v2" in html
        assert "const translations" in html
        assert "记忆概览" in html
        assert "healthBadge" in html
        assert "Rebuild Index" in html
        assert "Knowledge Base" in html
        assert "Drag nodes to arrange the graph" in html
        assert "Reset Layout" in html
        assert "function graphNodeRadius" in html
        assert 'const graphDotRadius = 8' in html
        assert "lod-compact" in html
        assert "readOnlyDemo" in html
        assert 'new URL(normalizedPath, window.location.href)' in html

        status = _json_get(base_url + "/api/status")
        assert status["pages"] == 1
        assert status["read_only"] is False

        health = _json_get(base_url + "/api/doctor")
        assert health["health"] == "unhealthy"
        assert any(check["id"] == "memory_bridge" for check in health["checks"])

        repaired = _json_request(base_url + "/api/install", method="POST", payload={})
        assert repaired["memory_path"].endswith("memory/MEMORY.md")
        assert _json_get(base_url + "/api/doctor")["health"] == "healthy"

        page_path = store.page_path("ui-smoke")
        page_path.write_text(
            page_path.read_text(encoding="utf-8").replace(
                "The browser UI can read this page.",
                "Manual UI edit token.",
            ),
            encoding="utf-8",
        )
        reindex = _json_request(base_url + "/api/reindex", method="POST", payload={})
        assert reindex["indexed"] == repaired["pages"]
        assert _json_get(base_url + "/api/search?q=Manual%20UI%20edit")["results"][0]["page"]["title"] == "UI Smoke"

        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        (raw_dir / "guide.md").write_text("# UI Import Guide\n\nImported through the UI.", encoding="utf-8")
        imported = _json_request(
            base_url + "/api/import",
            method="POST",
            payload={
                "path": str(raw_dir),
                "index_title": "UI Imported Docs",
                "tags": "ui, imported",
            },
        )
        assert imported["index_page"]["title"] == "UI Imported Docs"
        assert imported["imported"][0]["page"]["title"] == "UI Import Guide"

        search = _json_get(base_url + "/api/search?q=UI%20Smoke")
        assert any(result["page"]["title"] == "UI Smoke" for result in search["results"])

        created = _json_request(
            base_url + "/api/pages",
            method="POST",
            payload={
                "title": "Created From UI",
                "content": "Saved through the HTTP API.",
                "tags": "ui, api",
            },
        )
        assert created["page"]["id"] == "created-from-ui"

        page = _json_get(base_url + "/api/pages/" + quote("Created From UI"))
        assert page["page"]["content"] == "Saved through the HTTP API."

        link = _json_request(
            base_url + "/api/links",
            method="POST",
            payload={
                "from_selector": "Created From UI",
                "to_selector": "UI Smoke",
                "relation": "mentions",
            },
        )
        assert link["from_page"]["title"] == "Created From UI"

        graph = _json_get(base_url + "/api/graph")
        graph_titles = {node["title"] for node in graph["nodes"]}
        assert {"UI Smoke", "Created From UI", "UI Imported Docs", "UI Import Guide"}.issubset(graph_titles)
        assert "mentions" in {link["relation"] for link in graph["links"]}

        unlinked = _json_request(
            base_url + "/api/links",
            method="DELETE",
            payload={
                "from_selector": "Created From UI",
                "to_selector": "UI Smoke",
                "relation": "mentions",
            },
        )
        assert unlinked["removed"] == 1
        assert "mentions" not in {
            item["relation"] for item in _json_get(base_url + "/api/links")["links"]
        }

        deleted = _json_request(
            base_url + "/api/pages/" + quote("Created From UI"),
            method="DELETE",
        )
        assert deleted["archived"] is True
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_ui_read_only_mode_rejects_mutations(tmp_path) -> None:
    store = WikiStore(tmp_path)
    store.upsert_page(title="Public Demo", content="Safe to browse.", tags=["demo"])
    server = build_server(tmp_path, "127.0.0.1", 0, read_only=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status = _json_get(base_url + "/api/status")
        assert status["read_only"] is True
        assert _json_get(base_url + "/api/pages/public-demo")["page"]["title"] == "Public Demo"

        with pytest.raises(HTTPError) as create_error:
            _json_request(
                base_url + "/api/pages",
                method="POST",
                payload={"title": "Blocked", "content": "Must not be saved."},
            )
        assert create_error.value.code == 405
        assert "read-only mode" in create_error.value.read().decode("utf-8")

        with pytest.raises(HTTPError) as delete_error:
            _json_request(base_url + "/api/pages/public-demo", method="DELETE")
        assert delete_error.value.code == 405

        with pytest.raises(HTTPError) as unlink_error:
            _json_request(
                base_url + "/api/links",
                method="DELETE",
                payload={"from_selector": "Public Demo", "to_selector": "Public Demo"},
            )
        assert unlink_error.value.code == 405
        assert [page.title for page in store.list_pages()] == ["Public Demo"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
