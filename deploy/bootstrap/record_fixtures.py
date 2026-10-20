"""Record REAL responses from the local Lemmy and Ghost test instances into golden fixtures.

Output: <out>/lemmy/*.json, <out>/ghost/*.json, <out>/INDEX.json (default out: /fixtures)
Each fixture holds the request (method, path, redacted headers/body) and the response (status, kept headers, body).
Tokens, passwords and keys are redacted (known values + patterns). Every post created here is removed within the
run (Lemmy: soft delete then admin purge; Ghost: DELETE) and the script fails if any removal is not confirmed.

Run inside the compose network (no host ports), e.g. via deploy/bootstrap/record_fixtures.sh.
Only ever talks to the local containers named by IBX_LEMMY_URL / IBX_GHOST_URL.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ibxboot import KEEP_HEADERS as KEEP  # noqa: E402
from ibxboot import Http, Redactor, Response, ghost_admin_jwt, log, now_iso, read_json, wait_until  # noqa: E402

BOOT_OUT = os.environ.get("BOOTSTRAP_OUT_DIR", "/bootstrap/out")
ALLOWED_HOSTS = {"lemmy", "ghost", "discourse", "toxiproxy", "localhost", "127.0.0.1"}
TEXT_LIMIT = 600  # characters kept from non-JSON bodies (HTML pages)


class Recorder:
    def __init__(self, out_dir: str, platform: str, version: str, redactor: Redactor) -> None:
        self.out_dir = os.path.join(out_dir, platform)
        self.platform = platform
        self.version = version
        self.redactor = redactor
        self.index: list[dict[str, Any]] = []
        os.makedirs(self.out_dir, exist_ok=True)

    def save(
        self,
        name: str,
        description: str,
        method: str,
        path: str,
        req_headers: dict[str, str] | None,
        req_json: Any,
        resp: Response,
        notes: str | None = None,
    ) -> None:
        body_json = resp.json()
        response: dict[str, Any] = {
            "status": resp.status,
            "headers": {k: v for k, v in sorted(resp.headers.items()) if k in KEEP},
            "elapsed_ms": resp.elapsed_ms,
        }
        if body_json is not None:
            response["json"] = body_json
        else:
            text = resp.text
            response["text"] = text[:TEXT_LIMIT]
            response["text_truncated"] = len(text) > TEXT_LIMIT
            response["body_length"] = len(resp.body_bytes)
        fixture = {
            "fixture": f"{self.platform}/{name}",
            "platform": self.platform,
            "platform_version": self.version,
            "description": description,
            "recorded_at": now_iso(),
            "recorded_by": "deploy/bootstrap/record_fixtures.py (real local instance, not hand-written)",
            "request": {
                "method": method,
                "path": path,
                "headers": req_headers or {},
                **({"json": req_json} if req_json is not None else {}),
            },
            "response": response,
        }
        if notes:
            fixture["notes"] = notes
        fixture = self.redactor.data(fixture)
        with open(os.path.join(self.out_dir, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(fixture, f, indent=2, ensure_ascii=False)
            f.write("\n")
        self.index.append({"fixture": fixture["fixture"], "status": resp.status, "description": description})
        log(f"recorded {self.platform}/{name} (HTTP {resp.status})")


def guard_url(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise SystemExit(f"refusing to record against non-local host {host!r}")


def call(rec: Recorder, http: Http, name: str, description: str, method: str, path: str, *, json_body: Any = None,
         headers: dict[str, str] | None = None, absolute: bool = False, notes: str | None = None) -> Response:
    if absolute:
        guard_url(path)
    resp = http.request(method, path, json_body=json_body, headers=headers, absolute=absolute)
    shown_path = urlparse(path).path if absolute else path
    rec.save(name, description, method, shown_path, headers, json_body, resp, notes)
    return resp


def expect(resp: Response, statuses: tuple[int, ...], what: str) -> None:
    if resp.status not in statuses:
        raise RuntimeError(f"{what}: expected HTTP {statuses}, got {resp.status}: {resp.text[:300]}")


# ======================================================================= Lemmy


def _site_rl_if(http: Http, auth: dict[str, str], post_count: int) -> dict[str, Any] | None:
    rl = http.request("GET", "/api/v3/site", headers=auth).json()["site_view"]["local_site_rate_limit"]
    return rl if rl["post"] == post_count else None


def record_lemmy(out_dir: str, redactor: Redactor) -> dict[str, Any]:
    cfg = read_json(os.path.join(BOOT_OUT, "lemmy.json"))
    base = os.environ.get("IBX_LEMMY_URL", cfg["base_url"])
    guard_url(base)
    redactor.add(cfg["jwt"], cfg["admin_password"])
    http = Http(base)
    rec = Recorder(out_dir, "lemmy", cfg.get("version") or "unknown", redactor)
    auth = {"Authorization": f"Bearer {cfg['jwt']}"}
    community_id = cfg["communities"]["ibx_test"]
    nonce = secrets.token_hex(4)
    created: dict[int, float] = {}
    removed: dict[int, float] = {}
    limits = cfg["rate_limits"]

    def purge(post_id: int) -> Response:
        return http.request("POST", "/api/v3/admin/purge/post", json_body={"post_id": post_id, "reason": "ibx fixture cleanup"},
                            headers=auth)

    try:
        # --- auth
        r = call(rec, http, "login_ok", "Login as the local admin; the JWT is redacted.", "POST", "/api/v3/user/login",
                 json_body={"username_or_email": cfg["admin_username"], "password": cfg["admin_password"]})
        expect(r, (200,), "login")
        tmp_jwt = r.json()["jwt"]
        redactor.add(tmp_jwt)
        call(rec, http, "login_incorrect", "Login with a wrong password.", "POST", "/api/v3/user/login",
             json_body={"username_or_email": cfg["admin_username"], "password": "wrong-password-" + nonce})

        # --- create / read / lookup / live URL
        marker = f"ibx-fix{nonce}"
        body = f"Fixture post recorded by the IncuBrix bootstrap.\n\n[//]: # ({marker})\n"
        r = call(rec, http, "create_post_ok", "Create a text post in community ibx_test (body carries an invisible "
                 "markdown-comment marker used for idempotent lookup).", "POST", "/api/v3/post",
                 json_body={"name": f"IBX fixture post {nonce}", "community_id": community_id, "body": body}, headers=auth)
        expect(r, (200,), "create post")
        post = r.json()["post_view"]["post"]
        post_id = post["id"]
        created[post_id] = time.time()

        call(rec, http, "get_post_ok", "Get the post by id.", "GET", f"/api/v3/post?id={post_id}", headers=auth)
        call(rec, http, "creator_posts_lookup",
             "Creator's newest posts (how a connector reconciles a publish by its body marker after a timeout).",
             "GET", f"/api/v3/user?username={cfg['admin_username']}&sort=New&limit=5", headers=auth)
        call(rec, http, "live_url_get", "GET the post's ap_id URL (federation disabled) as a live-URL check.", "GET",
             post["ap_id"], absolute=True, headers={"Accept": "text/html,application/json"},
             notes="ap_id is built from the configured hostname (lemmy:8536). This records what the backend returns; "
                   "the human-facing page is served by lemmy-ui (profile `ui`).")
        call(rec, http, "live_url_get_activity_json", "GET the post's ap_id URL with an ActivityPub Accept header.", "GET",
             post["ap_id"], absolute=True, headers={"Accept": "application/activity+json"})

        # --- errors
        call(rec, http, "create_post_title_too_long", "Create a post whose title is 201 characters (limit 200).", "POST",
             "/api/v3/post", json_body={"name": "T" * 201, "community_id": community_id, "body": "x"}, headers=auth)
        call(rec, http, "create_post_invalid_token", "Create a post with a malformed/forged bearer token.", "POST",
             "/api/v3/post", json_body={"name": f"IBX invalid token {nonce}", "community_id": community_id},
             headers={"Authorization": "Bearer not-a-real-token"})
        call(rec, http, "create_post_community_not_found", "Create a post in a community id that does not exist.",
             "POST", "/api/v3/post", json_body={"name": f"IBX no community {nonce}", "community_id": 999999},
             headers=auth)
        call(rec, http, "get_post_not_found", "Get a post id that never existed.", "GET", "/api/v3/post?id=999999",
             headers=auth)

        # Logged-out (revoked) token: Lemmy 0.19 tokens never expire on their own; logout is the revocation path.
        r = http.request("POST", "/api/v3/user/logout", headers={"Authorization": f"Bearer {tmp_jwt}"})
        rec.save("logout_ok", "Log out (revokes that login token).", "POST", "/api/v3/user/logout",
                 {"Authorization": f"Bearer {tmp_jwt}"}, None, r)
        call(rec, http, "create_post_revoked_token",
             "Create a post with a token that was revoked by logout (Lemmy's equivalent of an expired token).",
             "POST", "/api/v3/post", json_body={"name": f"IBX revoked token {nonce}", "community_id": community_id},
             headers={"Authorization": f"Bearer {tmp_jwt}"})

        # --- soft delete, purge, not found
        r = call(rec, http, "delete_post_soft", "Soft-delete the post (reversible; admins still see it).", "POST",
                 "/api/v3/post/delete", json_body={"post_id": post_id, "deleted": True}, headers=auth)
        expect(r, (200,), "soft delete")
        call(rec, http, "get_post_soft_deleted", "Get the soft-deleted post as the admin.", "GET",
             f"/api/v3/post?id={post_id}", headers=auth)
        r = purge(post_id)
        rec.save("purge_post", "Admin purge: permanent removal (what certification uses on the local instance).",
                 "POST", "/api/v3/admin/purge/post", auth, {"post_id": post_id, "reason": "ibx fixture cleanup"}, r)
        expect(r, (200,), "purge")
        removed[post_id] = time.time()
        r = call(rec, http, "get_post_not_found_after_purge", "Get the purged post.", "GET",
                 f"/api/v3/post?id={post_id}", headers=auth)
        if r.status == 200:
            raise RuntimeError("purged lemmy post is still readable")

        # --- rate limit: lower the post limit, trigger rate_limit_error, restore
        low = {"rate_limit_post": 2, "rate_limit_post_per_second": 600}
        r = http.request("PUT", "/api/v3/site", json_body=low, headers=auth)
        expect(r, (200,), "lower rate limit")
        try:
            wait_until("lemmy lowered rate limit", lambda: _site_rl_if(http, auth, 2), 30, 1)
            r = http.request("GET", "/api/v3/site", headers=auth)
            site = r.json()
            # keep the fixture small: re-save with only the rate-limit block and version
            r.body_bytes = json.dumps({
                "version": site.get("version"),
                "site_view": {"local_site_rate_limit": site["site_view"]["local_site_rate_limit"]},
            }).encode()
            rec.save("site_rate_limit_config", "GET /api/v3/site (trimmed to version + local_site_rate_limit) while "
                     "the post limit was lowered to 2 per 600 s. Lemmy sends no Retry-After; the reset is derived "
                     "from this block.", "GET", "/api/v3/site", auth, None, r,
                     notes="Body trimmed by the recorder to the rate-limit block; status and headers are as returned.")
            got_limit = False
            for i in range(6):
                body_i = {"name": f"IBX rate limit probe {nonce} {i}", "community_id": community_id}
                r = http.request("POST", "/api/v3/post", json_body=body_i, headers=auth)
                if r.status == 200:
                    created[r.json()["post_view"]["post"]["id"]] = time.time()
                    continue
                if (r.json() or {}).get("error") == "rate_limit_error":
                    rec.save("create_post_rate_limited",
                             f"Create a post after exceeding the (temporarily lowered) post limit of 2 per 600 s "
                             f"(attempt {i + 1}).", "POST", "/api/v3/post", auth, body_i, r,
                             notes="Lemmy returns HTTP 400 with error=rate_limit_error and no Retry-After/"
                                   "X-RateLimit-* headers.")
                    got_limit = True
                    break
                raise RuntimeError(f"unexpected error while probing rate limit: HTTP {r.status} {r.text[:200]}")
            if not got_limit:
                raise RuntimeError("could not trigger lemmy rate_limit_error")
        finally:
            restore = {"rate_limit_post": limits["post"]["count"],
                       "rate_limit_post_per_second": limits["post"]["per_second"]}
            r = http.request("PUT", "/api/v3/site", json_body=restore, headers=auth)
            expect(r, (200,), "restore rate limit")
            # GET /api/v3/site is cached server-side for a few seconds, so poll until the restore is visible.
            rl = wait_until("lemmy rate limit restore", lambda: _site_rl_if(http, auth, limits["post"]["count"]), 30, 1)
            log(f"lemmy: post rate limit restored to {rl['post']}/{rl['post_per_second']}s")
    finally:
        for pid in list(created):
            if pid in removed:
                continue
            r = purge(pid)
            if r.status == 200:
                removed[pid] = time.time()
            else:
                log(f"lemmy: WARNING purge of post {pid} failed: HTTP {r.status}")
    missing = [p for p in created if p not in removed]
    if missing:
        raise RuntimeError(f"lemmy posts not removed: {missing}")
    max_age = max((removed[p] - created[p] for p in created), default=0.0)
    return {"index": rec.index, "posts_created": len(created), "posts_removed": len(removed),
            "max_post_lifetime_s": round(max_age, 1)}


# ======================================================================= Ghost


def record_ghost(out_dir: str, redactor: Redactor) -> dict[str, Any]:
    cfg = read_json(os.path.join(BOOT_OUT, "ghost.json"))
    base = os.environ.get("IBX_GHOST_URL", cfg["base_url"])
    guard_url(base)
    admin_key = cfg["admin_api_key"]
    key_id, secret = admin_key.split(":", 1)
    redactor.add(admin_key, secret, cfg.get("content_api_key"), os.environ.get("IBX_GHOST_OWNER_PASSWORD"))
    http = Http(base, default_headers={"Accept-Version": "v6.0"})
    rec = Recorder(out_dir, "ghost", cfg.get("version") or "unknown", redactor)
    admin = "/ghost/api/admin"
    nonce = secrets.token_hex(4)
    created: dict[str, float] = {}
    removed: dict[str, float] = {}

    def auth(**kw: Any) -> dict[str, str]:
        token = ghost_admin_jwt(admin_key, **kw)
        redactor.add(token)
        return {"Authorization": f"Ghost {token}"}

    try:
        call(rec, http, "site", "Public admin site info (used as the health/ready probe).", "GET", f"{admin}/site/")

        slug = f"ibx-fixture-{nonce}"
        post_body = {"posts": [{"title": f"IBX fixture post {nonce}", "slug": slug, "status": "published",
                                "html": "<p>Fixture post recorded by the IncuBrix bootstrap.</p>"}]}
        r = call(rec, http, "create_post_ok", "Create and publish a post (HTML source, deterministic slug, no newsletter "
                 "parameter so no email is sent).", "POST", f"{admin}/posts/?source=html", json_body=post_body,
                 headers=auth())
        expect(r, (201,), "ghost create")
        post = r.json()["posts"][0]
        post_id = post["id"]
        created[post_id] = time.time()

        call(rec, http, "get_post_ok", "Get the post by id.", "GET", f"{admin}/posts/{post_id}/", headers=auth())
        call(rec, http, "get_post_by_slug_ok", "Get the post by slug (idempotent reconcile after a timeout).", "GET",
             f"{admin}/posts/slug/{slug}/", headers=auth())
        r = call(rec, http, "live_url_get", "GET the public post URL.", "GET", post["url"], absolute=True,
                 headers={"Accept": "text/html"})
        expect(r, (200,), "ghost live url")

        # --- errors
        call(rec, http, "create_post_title_too_long", "Create a post whose title is 256 characters (limit 255).",
             "POST", f"{admin}/posts/?source=html",
             json_body={"posts": [{"title": "T" * 256, "status": "draft", "html": "<p>x</p>"}]}, headers=auth())
        call(rec, http, "invalid_token_bad_signature", "Admin API call with a token signed by the wrong secret.",
             "GET", f"{admin}/posts/?limit=1", headers=auth(secret_override="00" * 32))
        call(rec, http, "expired_token", "Admin API call with a token whose exp is 5 minutes in the past.", "GET",
             f"{admin}/posts/?limit=1", headers=auth(iat=int(time.time()) - 600, ttl_s=300))
        call(rec, http, "token_lifetime_too_long", "Admin API call with a token valid for 1 hour (Ghost allows <= 5 min).",
             "GET", f"{admin}/posts/?limit=1", headers=auth(ttl_s=3600))
        call(rec, http, "unknown_key_id", "Admin API call with a token whose kid is not a known key.", "GET",
             f"{admin}/posts/?limit=1",
             headers={"Authorization": "Ghost " + ghost_admin_jwt(f"{'0' * 24}:{secret}")})
        call(rec, http, "get_post_not_found", "Get a post id that never existed.", "GET",
             f"{admin}/posts/{'0' * 24}/", headers=auth())

        # --- delete and not found
        r = call(rec, http, "delete_post", "Delete the post (permanent).", "DELETE", f"{admin}/posts/{post_id}/",
                 headers=auth())
        expect(r, (204,), "ghost delete")
        removed[post_id] = time.time()
        call(rec, http, "get_post_not_found_after_delete", "Get the deleted post.", "GET", f"{admin}/posts/{post_id}/",
             headers=auth())
        r = call(rec, http, "live_url_after_delete", "GET the public URL of the deleted post.", "GET", post["url"],
                 absolute=True, headers={"Accept": "text/html"})
        if r.status == 200:
            raise RuntimeError("deleted ghost post is still live")
        call(rec, http, "delete_post_not_found", "Delete a post that is already gone.", "DELETE",
             f"{admin}/posts/{post_id}/", headers=auth())

        # --- rate limit (login brute-force protection; the Admin API with a key is not rate limited locally)
        sess = Http(base, default_headers={"Origin": cfg["base_url"], "Accept-Version": "v6.0"})
        bogus = {"username": f"nobody-{nonce}@ghost.ibx.invalid", "password": "wrong-password-" + nonce}
        for i in range(20):
            r = sess.request("POST", f"{admin}/session/", json_body=bogus)
            if r.status == 429:
                rec.save("session_login_rate_limited",
                         f"Repeated failed staff logins for a non-existent user hit Ghost's brute-force limiter "
                         f"(attempt {i + 1}).", "POST", f"{admin}/session/", {"Origin": cfg["base_url"]}, bogus, r,
                         notes="Ghost's Admin API with an integration key had no rate limit locally; this is the only "
                               "429 the local instance produces. The owner account is not affected.")
                break
            if i == 0:
                rec.save("session_login_incorrect", "Staff login with an unknown email.", "POST", f"{admin}/session/",
                         {"Origin": cfg["base_url"]}, bogus, r)
        else:
            log("ghost: WARNING login limiter did not trigger within 20 attempts")
    finally:
        for pid in list(created):
            if pid in removed:
                continue
            r = http.request("DELETE", f"{admin}/posts/{pid}/", headers=auth())
            if r.status in (204, 404):
                removed[pid] = time.time()
    missing = [p for p in created if p not in removed]
    if missing:
        raise RuntimeError(f"ghost posts not removed: {missing}")
    max_age = max((removed[p] - created[p] for p in created), default=0.0)
    return {"index": rec.index, "posts_created": len(created), "posts_removed": len(removed),
            "max_post_lifetime_s": round(max_age, 1)}


# ======================================================================= Discourse (profile `full` only)


def record_discourse(out_dir: str, redactor: Redactor) -> dict[str, Any]:
    cfg = read_json(os.path.join(BOOT_OUT, "discourse.json"))
    base = os.environ.get("IBX_DISCOURSE_URL", cfg["base_url"])
    guard_url(base)
    redactor.add(cfg["api_key"])
    http = Http(base)
    anon = Http(base)
    rec = Recorder(out_dir, "discourse", cfg.get("version") or "unknown", redactor)
    auth = {"Api-Key": cfg["api_key"], "Api-Username": cfg["api_username"]}
    nonce = secrets.token_hex(4)
    created: dict[int, float] = {}
    removed: dict[int, float] = {}
    category = cfg["category_id"]

    try:
        call(rec, http, "site_basic_info", "Public site basic info (health/enrichment probe).", "GET",
             "/site/basic-info.json")
        external_id = f"ibx-fix{nonce}"
        title = f"IBX fixture topic {nonce} recorded locally"
        body = {"title": title, "raw": "Fixture topic recorded by the IncuBrix bootstrap on a local test instance.",
                "category": category, "external_id": external_id}
        r = call(rec, http, "create_topic_ok", "Create a topic (first post) with an external_id marker.", "POST",
                 "/posts.json", json_body=body, headers=auth)
        expect(r, (200,), "discourse create")
        topic_id = r.json()["topic_id"]
        created[topic_id] = time.time()
        call(rec, http, "get_topic_ok", "Get the topic.", "GET", f"/t/{topic_id}.json", headers=auth)
        call(rec, http, "get_topic_by_external_id", "Look up the topic by external_id (idempotent reconcile).", "GET",
             f"/t/external_id/{external_id}.json", headers=auth)
        call(rec, anon, "live_url_get", "GET the public topic URL without credentials.", "GET", f"/t/{topic_id}",
             headers={"Accept": "text/html"})

        call(rec, http, "create_topic_duplicate_title", "Create a second topic with the same title.", "POST",
             "/posts.json", json_body={**body, "external_id": external_id + "b"}, headers=auth)
        call(rec, http, "create_topic_title_too_long", "Create a topic whose title is 256 characters.", "POST",
             "/posts.json", json_body={"title": "Long title " + "x" * 245, "raw": body["raw"], "category": category},
             headers=auth)
        call(rec, http, "create_topic_title_too_short", "Create a topic whose title is below min_topic_title_length.",
             "POST", "/posts.json", json_body={"title": "Too short", "raw": body["raw"], "category": category},
             headers=auth)
        call(rec, http, "invalid_api_key", "Create a topic with an unknown API key.", "POST", "/posts.json",
             json_body={**body, "external_id": external_id + "c"},
             headers={"Api-Key": "0" * 64, "Api-Username": cfg["api_username"]})
        call(rec, http, "get_topic_not_found", "Get a topic id that never existed.", "GET", "/t/99999999.json",
             headers=auth)

        r = call(rec, http, "delete_topic", "Delete the topic (Discourse soft delete: deleted_at is set).", "DELETE",
                 f"/t/{topic_id}.json", headers=auth)
        expect(r, (200,), "discourse delete")
        removed[topic_id] = time.time()
        call(rec, http, "get_topic_after_delete_admin", "Get the deleted topic with the admin key.", "GET",
             f"/t/{topic_id}.json", headers=auth)
        r = call(rec, anon, "get_topic_after_delete_public", "Get the deleted topic anonymously.", "GET",
                 f"/t/{topic_id}.json")
        if r.status == 200:
            raise RuntimeError("deleted discourse topic is still public")
    finally:
        for tid in list(created):
            if tid not in removed:
                r = http.request("DELETE", f"/t/{tid}.json", headers=auth)
                if r.status in (200, 404):
                    removed[tid] = time.time()
    missing = [t for t in created if t not in removed]
    if missing:
        raise RuntimeError(f"discourse topics not removed: {missing}")
    max_age = max((removed[t] - created[t] for t in created), default=0.0)
    return {"index": rec.index, "posts_created": len(created), "posts_removed": len(removed),
            "max_post_lifetime_s": round(max_age, 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="/fixtures")
    ap.add_argument("--platforms", default="lemmy,ghost", help="comma list; add 'discourse' with profile full")
    args = ap.parse_args()
    redactor = Redactor()
    index_path = os.path.join(args.out, "INDEX.json")
    summary: dict[str, Any] = {"generator": "deploy/bootstrap/record_fixtures.py", "platforms": {}}
    if os.path.exists(index_path):  # keep other platforms' entries when recording a subset
        summary["platforms"] = read_json(index_path).get("platforms", {})
    for name, fn in (("lemmy", record_lemmy), ("ghost", record_ghost), ("discourse", record_discourse)):
        if name in args.platforms.split(","):
            summary["platforms"][name] = {"recorded_at": now_iso(), **fn(args.out, redactor)}
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(redactor.data(summary), f, indent=2)
        f.write("\n")
    for name, s in summary["platforms"].items():
        if name not in args.platforms.split(","):
            continue
        log(f"{name}: {len(s['index'])} fixtures, {s['posts_created']} posts created, {s['posts_removed']} removed, "
            f"max lifetime {s['max_post_lifetime_s']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
