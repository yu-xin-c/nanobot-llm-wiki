from __future__ import annotations

import json
import threading
from urllib.parse import quote
from urllib.request import Request, urlopen

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

        status = _json_get(base_url + "/api/status")
        assert status["pages"] == 1

        search = _json_get(base_url + "/api/search?q=browser")
        assert search["results"][0]["page"]["title"] == "UI Smoke"

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

        deleted = _json_request(
            base_url + "/api/pages/" + quote("Created From UI"),
            method="DELETE",
        )
        assert deleted["archived"] is True
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
