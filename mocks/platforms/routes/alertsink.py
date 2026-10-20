"""Alert receiver for tests and demos, mounted at /alertsink. It is OUR receiver, not a third-party platform.

    POST /alertsink/hook                         stores the request (headers + raw body) as one delivery; 200
    GET  /alertsink/_deliveries                  every stored delivery (raw body as text, headers lower-cased)
    POST /alertsink/github/repos/{o}/{r}/issues  GitHub-issues emulation: 201 {"number": n}
    POST /alertsink/github/repos/{o}/{r}/issues/{n}/comments   201
    PATCH /alertsink/github/repos/{o}/{r}/issues/{n}           200 (state closed)
    GET  /alertsink/github/_issues               the emulated issues
    PUT  /alertsink/pages/{name}                 {"html": "..."}: a rules page we control (drift tests/demos)
    GET  /alertsink/pages/{name}                 that page (text/html)
    POST /alertsink/_reset

The receiver does not verify the HMAC itself: the caller (test or evidence script) holds the secret and verifies
the stored raw body and ``X-IncuBrix-Signature``, exactly as a real receiver would.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()
_deliveries: list[dict[str, Any]] = []
_issues: dict[int, dict[str, Any]] = {}
_pages: dict[str, str] = {}


@router.post("/hook")
async def hook(request: Request) -> dict[str, Any]:
    body = await request.body()
    _deliveries.append({"headers": {k.lower(): v for k, v in request.headers.items() if k.lower() != "authorization"},
                        "body": body.decode("utf-8", errors="replace")})
    return {"received": True, "n": len(_deliveries)}


@router.get("/_deliveries")
async def deliveries() -> dict[str, Any]:
    return {"deliveries": _deliveries}


@router.post("/github/repos/{owner}/{repo}/issues")
async def create_issue(owner: str, repo: str, request: Request) -> JSONResponse:
    data = await request.json()
    number = len(_issues) + 1
    _issues[number] = {"number": number, "repo": f"{owner}/{repo}", "title": data.get("title"), "labels": data.get("labels"),
                       "state": "open", "comments": [], "authorized": request.headers.get("authorization", "").startswith("Bearer ")}
    return JSONResponse({"number": number, "state": "open"}, status_code=201)


@router.post("/github/repos/{owner}/{repo}/issues/{number}/comments")
async def comment(owner: str, repo: str, number: int, request: Request) -> JSONResponse:
    if number not in _issues:
        return JSONResponse({"message": "Not Found"}, status_code=404)
    _issues[number]["comments"].append((await request.json()).get("body"))
    return JSONResponse({"id": len(_issues[number]["comments"])}, status_code=201)


@router.patch("/github/repos/{owner}/{repo}/issues/{number}")
async def patch_issue(owner: str, repo: str, number: int, request: Request) -> JSONResponse:
    if number not in _issues:
        return JSONResponse({"message": "Not Found"}, status_code=404)
    _issues[number]["state"] = (await request.json()).get("state", _issues[number]["state"])
    return JSONResponse({"number": number, "state": _issues[number]["state"]})


@router.get("/github/_issues")
async def issues() -> dict[str, Any]:
    return {"issues": list(_issues.values())}


@router.put("/pages/{name}")
async def put_page(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    _pages[name] = str(payload.get("html") or "")
    return {"stored": name}


@router.get("/pages/{name}")
async def get_page(name: str) -> HTMLResponse:
    if name not in _pages:
        return HTMLResponse("<html><body>not found</body></html>", status_code=404)
    return HTMLResponse(_pages[name])


@router.post("/_reset")
async def reset() -> dict[str, Any]:
    _deliveries.clear()
    _issues.clear()
    _pages.clear()
    return {"reset": True}
