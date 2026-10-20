"""Evidence screenshot shooter: a tiny internal HTTP service around headless Chromium (Playwright).

    GET  /health
    POST /shoot   {"url": "...", "title": "..."}            -> image/png of the page
    POST /render  {"title": "...", "data": {...}}           -> image/png of a labelled API-response render

Response headers say what the picture is: ``X-Shot-Kind: page`` (a real screenshot of the URL) or
``X-Shot-Kind: api_render`` (we drew the JSON ourselves, because there is no web page; the picture carries a
banner saying so). ``X-Shot-Status`` is the HTTP status of the page, ``X-Shot-Content-Type`` its type.
A /shoot of a URL that does not answer HTML (e.g. ActivityPub JSON) is rendered as api_render, labelled.

It only runs under compose profile ``evidence``, on the internal networks, with no published port. The worker
calls it for URLs on our own test instances only (the certification harness checks that first).
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from playwright.sync_api import sync_playwright

PORT = 3000
VIEWPORT = {"width": 1280, "height": 900}
BANNER = ("API RESPONSE RENDER - not a screenshot of a web page. This destination has no public page here, "
          "so the JSON answer is drawn as HTML by the IncuBrix evidence shooter.")


def render_html(title: str, data: Any, note: str | None = None) -> str:
    body = json.dumps(data, indent=2, ensure_ascii=False, default=str) if not isinstance(data, str) else data
    when = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>" + html.escape(title) + "</title><style>"
        "body{font-family:sans-serif;margin:0;background:#fff;color:#111}"
        ".b{background:#7a1f1f;color:#fff;padding:12px 16px;font-weight:bold}"
        "h1{font-size:18px;margin:16px} .m{margin:0 16px;color:#555;font-size:13px}"
        "pre{margin:16px;padding:12px;background:#f4f4f4;border:1px solid #ddd;white-space:pre-wrap;word-break:break-all;font-size:13px}"
        "</style></head><body><div class='b'>" + html.escape(BANNER) + "</div><h1>" + html.escape(title) + "</h1>"
        "<p class='m'>rendered " + when + (" &middot; " + html.escape(note) if note else "") + "</p>"
        "<pre>" + html.escape(body[:20000]) + "</pre></body></html>"
    )


class Shooter:
    def __init__(self) -> None:
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch()

    def _png(self, setup: Any) -> bytes:
        context = self.browser.new_context(viewport=VIEWPORT)
        try:
            page = context.new_page()
            setup(page)
            return page.screenshot(full_page=False)
        finally:
            context.close()

    def shoot(self, url: str, title: str) -> tuple[bytes, dict[str, str]]:
        meta: dict[str, str] = {"X-Shot-Kind": "page", "X-Shot-Url": url}

        def load(page: Any) -> None:
            # the sandbox has no internet route, so a page that links external fonts/scripts never fires "load":
            # wait for the document, then give the rest a bounded chance
            response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_load_state("load", timeout=5000)
            except Exception:  # noqa: BLE001 - external assets unreachable: the document is already there
                meta["X-Shot-Note"] = "load event not reached in 5 s (external assets unreachable); document shown"
            status = response.status if response is not None else 0
            ctype = (response.headers.get("content-type", "") if response is not None else "").split(";")[0]
            meta.update({"X-Shot-Status": str(status), "X-Shot-Content-Type": ctype or "unknown", "X-Shot-Final-Url": page.url})
            if ctype and "html" not in ctype:  # JSON / text: draw it, labelled, instead of the browser's raw view
                text = response.text() if response is not None else ""
                try:
                    data: Any = json.loads(text)
                except ValueError:
                    data = text
                meta["X-Shot-Kind"] = "api_render"
                page.set_content(render_html(title, data, f"GET {url} -> HTTP {status} ({ctype})"))
            page.wait_for_timeout(500)

        return self._png(load), meta

    def render(self, title: str, data: Any, note: str | None) -> tuple[bytes, dict[str, str]]:
        return self._png(lambda page: page.set_content(render_html(title, data, note))), {"X-Shot-Kind": "api_render"}


SHOOTER: Shooter | None = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # one short line per request, no bodies
        print(f"shooter {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", flush=True)

    def _send(self, status: int, body: bytes, ctype: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value.encode("ascii", "replace").decode())
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(404, b'{"error":"not_found"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        try:
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            title = str(request.get("title") or "evidence")
            if self.path == "/shoot" and str(request.get("url", "")).startswith(("http://", "https://")):
                png, meta = SHOOTER.shoot(request["url"], title)
            elif self.path == "/render":
                png, meta = SHOOTER.render(title, request.get("data"), request.get("note"))
            else:
                self._send(400, b'{"error":"bad_request"}', "application/json")
                return
            self._send(200, png, "image/png", meta)
        except Exception as exc:  # noqa: BLE001 - report, never crash the service
            self._send(502, json.dumps({"error": type(exc).__name__, "message": str(exc)[:300]}).encode(), "application/json")


if __name__ == "__main__":
    SHOOTER = Shooter()
    print(f"shooter listening on :{PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104 - internal compose network only
