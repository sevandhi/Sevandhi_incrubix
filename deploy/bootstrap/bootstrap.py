"""Idempotent one-shot bootstrap for the local Lemmy and Ghost test instances (and Discourse if present).

Writes credentials for the API service into $BOOTSTRAP_OUT_DIR (default /bootstrap/out):
  lemmy.json, ghost.json, (discourse.json is written by the discourse-bootstrap service), status.json
Secret values are never printed. Exit code 0 only when every enabled platform is ready and verified.

Run by the `bootstrap` service in deploy/compose/bootstrap.yaml. Safe to run any number of times.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ibxboot import Http, ghost_admin_jwt, log, now_iso, read_json, wait_until, write_json_secure  # noqa: E402

OUT_DIR = os.environ.get("BOOTSTRAP_OUT_DIR", "/bootstrap/out")
TIMEOUT_S = float(os.environ.get("IBX_BOOTSTRAP_TIMEOUT_S", "300"))


def env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


# ======================================================================= Lemmy

LEMMY_URL = env("IBX_LEMMY_URL", "http://lemmy:8536")
LEMMY_USER = env("IBX_LEMMY_ADMIN_USERNAME", "ibx_admin")
LEMMY_PASSWORD = env("IBX_LEMMY_ADMIN_PASSWORD", "ibx-local-dev-only-lemmy")
# Lemmy actor names allow only [a-z0-9_], so "ibx-test" is stored as "ibx_test".
LEMMY_COMMUNITIES = [c.strip() for c in env("IBX_LEMMY_COMMUNITIES", "ibx_test,ibx_test_2").split(",") if c.strip()]

# Raised local rate limits: (count, interval_seconds). Lemmy counts per client IP, and the API service is a single IP.
LEMMY_RATE_LIMITS = {
    "message": (int(env("IBX_LEMMY_RL_MESSAGE", "1000")), int(env("IBX_LEMMY_RL_MESSAGE_PER_SECOND", "60"))),
    "post": (int(env("IBX_LEMMY_RL_POST", "1000")), int(env("IBX_LEMMY_RL_POST_PER_SECOND", "60"))),
    "register": (int(env("IBX_LEMMY_RL_REGISTER", "100")), int(env("IBX_LEMMY_RL_REGISTER_PER_SECOND", "3600"))),
    "image": (int(env("IBX_LEMMY_RL_IMAGE", "1000")), int(env("IBX_LEMMY_RL_IMAGE_PER_SECOND", "60"))),
    "comment": (int(env("IBX_LEMMY_RL_COMMENT", "1000")), int(env("IBX_LEMMY_RL_COMMENT_PER_SECOND", "60"))),
    "search": (int(env("IBX_LEMMY_RL_SEARCH", "1000")), int(env("IBX_LEMMY_RL_SEARCH_PER_SECOND", "60"))),
}


def lemmy_rate_limit_payload(limits: dict[str, tuple[int, int]]) -> dict[str, int]:
    body: dict[str, int] = {}
    for kind, (count, per_s) in limits.items():
        body[f"rate_limit_{kind}"] = count
        body[f"rate_limit_{kind}_per_second"] = per_s
    return body


def lemmy_login(http: Http) -> str:
    r = http.request("POST", "/api/v3/user/login", json_body={"username_or_email": LEMMY_USER, "password": LEMMY_PASSWORD})
    data = r.json() or {}
    if r.status != 200 or not data.get("jwt"):
        raise RuntimeError(f"lemmy login failed: HTTP {r.status} error={data.get('error')!r}")
    return data["jwt"]


def lemmy_existing_jwt(http: Http) -> str | None:
    """Reuse the JWT from a previous run if Lemmy still accepts it (0.19 JWTs do not expire until logout)."""
    path = os.path.join(OUT_DIR, "lemmy.json")
    try:
        jwt = read_json(path)["jwt"]
    except (KeyError, ValueError, OSError):
        return None
    r = http.request("GET", "/api/v3/site", headers={"Authorization": f"Bearer {jwt}"})
    return jwt if r.status == 200 and (r.json() or {}).get("my_user") else None


def bootstrap_lemmy() -> dict:
    http = Http(LEMMY_URL)
    wait_until("lemmy /api/v3/site", lambda: http.request("GET", "/api/v3/site").status == 200, TIMEOUT_S)
    jwt = lemmy_existing_jwt(http) or lemmy_login(http)
    auth = {"Authorization": f"Bearer {jwt}"}

    # Federation off + raised rate limits (admin site API). Idempotent: PUT the desired state every run.
    edit = {"federation_enabled": False, **lemmy_rate_limit_payload(LEMMY_RATE_LIMITS)}
    r = http.request("PUT", "/api/v3/site", json_body=edit, headers=auth)
    if r.status != 200:
        raise RuntimeError(f"lemmy edit site failed: HTTP {r.status} error={(r.json() or {}).get('error')!r}")

    def applied() -> dict | None:
        s = http.request("GET", "/api/v3/site", headers=auth).json()
        ls, lrl = s["site_view"]["local_site"], s["site_view"]["local_site_rate_limit"]
        ok = ls.get("federation_enabled") is False and all(
            lrl.get(k) == c and lrl.get(f"{k}_per_second") == p for k, (c, p) in LEMMY_RATE_LIMITS.items()
        )
        return s if ok else None

    # GET /api/v3/site is cached server-side for a few seconds: poll until the edit is visible, else fail closed.
    try:
        site = wait_until("lemmy site edit (federation off, rate limits)", applied, 30, 1)
    except RuntimeError as e:
        raise RuntimeError(f"{e}; refusing to continue with federation possibly enabled") from None
    local_site = site["site_view"]["local_site"]
    rl = site["site_view"]["local_site_rate_limit"]
    my_user = (site.get("my_user") or {}).get("local_user_view", {}).get("person", {})

    communities: dict[str, int] = {}
    for name in LEMMY_COMMUNITIES:
        r = http.request("GET", f"/api/v3/community?name={name}", headers=auth)
        if r.status == 200:
            communities[name] = r.json()["community_view"]["community"]["id"]
            continue
        err = (r.json() or {}).get("error")
        if err != "couldnt_find_community":
            raise RuntimeError(f"lemmy get community {name}: HTTP {r.status} error={err!r}")
        r = http.request(
            "POST",
            "/api/v3/community",
            json_body={"name": name, "title": f"IBX test {name}", "description": "Local test community (IncuBrix)."},
            headers=auth,
        )
        if r.status != 200:
            raise RuntimeError(f"lemmy create community {name}: HTTP {r.status} error={(r.json() or {}).get('error')!r}")
        communities[name] = r.json()["community_view"]["community"]["id"]
        log(f"lemmy: created community {name}")

    out = {
        "platform": "lemmy",
        "version": site.get("version"),
        "base_url": LEMMY_URL,
        "hostname": site["site_view"]["site"]["actor_id"].rstrip("/"),
        "admin_username": LEMMY_USER,
        "admin_password": LEMMY_PASSWORD,
        "admin_person_id": my_user.get("id"),
        "jwt": jwt,
        "communities": communities,
        "federation_enabled": local_site.get("federation_enabled"),
        "rate_limits": {k: {"count": rl.get(k), "per_second": rl.get(f"{k}_per_second")} for k in LEMMY_RATE_LIMITS},
        "written_at": now_iso(),
    }
    write_json_secure(os.path.join(OUT_DIR, "lemmy.json"), out)
    log(f"lemmy: ready (communities={sorted(communities)}, federation_enabled=false, post limit "
        f"{rl.get('post')}/{rl.get('post_per_second')}s)")
    return {"ok": True, "communities": sorted(communities)}


# ======================================================================= Ghost

GHOST_URL = env("IBX_GHOST_URL", "http://ghost:2368")
GHOST_ORIGIN = env("IBX_GHOST_ORIGIN", GHOST_URL)  # must match the `url` Ghost is configured with
GHOST_OWNER_EMAIL = env("IBX_GHOST_OWNER_EMAIL", "owner@ghost.ibx.invalid")
GHOST_OWNER_PASSWORD = env("IBX_GHOST_OWNER_PASSWORD", "ibx-local-dev-only-owner-pw1")
GHOST_OWNER_NAME = env("IBX_GHOST_OWNER_NAME", "IBX Owner")
GHOST_BLOG_TITLE = env("IBX_GHOST_BLOG_TITLE", "IBX local Ghost")
GHOST_INTEGRATION = env("IBX_GHOST_INTEGRATION_NAME", "IncuBrix connector (local)")
ADMIN = "/ghost/api/admin"


def _ghost_err(r) -> str:
    data = r.json() or {}
    errs = data.get("errors") or [{}]
    return f"HTTP {r.status} type={errs[0].get('type')!r} message={errs[0].get('message')!r}"


def ghost_verify_key(http: Http, admin_key: str) -> bool:
    token = ghost_admin_jwt(admin_key)
    r = http.request("GET", f"{ADMIN}/posts/?limit=1", headers={"Authorization": f"Ghost {token}"})
    return r.status == 200


def bootstrap_ghost() -> dict:
    http = Http(GHOST_URL, default_headers={"Origin": GHOST_ORIGIN, "Accept-Version": "v6.0"})
    wait_until("ghost admin site", lambda: http.request("GET", f"{ADMIN}/site/").status == 200, TIMEOUT_S)

    path = os.path.join(OUT_DIR, "ghost.json")
    if os.path.exists(path):
        try:
            prev = read_json(path)
            if ghost_verify_key(http, prev["admin_api_key"]):
                log("ghost: existing integration key still valid; nothing to do")
                return {"ok": True, "reused": True}
        except (KeyError, ValueError, OSError):
            pass

    r = http.request("GET", f"{ADMIN}/authentication/setup/")
    is_setup = bool(((r.json() or {}).get("setup") or [{}])[0].get("status"))
    if not is_setup:
        r = http.request(
            "POST",
            f"{ADMIN}/authentication/setup/",
            json_body={
                "setup": [
                    {
                        "name": GHOST_OWNER_NAME,
                        "email": GHOST_OWNER_EMAIL,
                        "password": GHOST_OWNER_PASSWORD,
                        "blogTitle": GHOST_BLOG_TITLE,
                    }
                ]
            },
        )
        if r.status not in (200, 201):
            raise RuntimeError(f"ghost owner setup failed: {_ghost_err(r)}")
        log("ghost: owner created")

    r = http.request("POST", f"{ADMIN}/session/", json_body={"username": GHOST_OWNER_EMAIL, "password": GHOST_OWNER_PASSWORD})
    if r.status not in (200, 201):
        raise RuntimeError(f"ghost session failed: {_ghost_err(r)}")

    r = http.request("GET", f"{ADMIN}/integrations/?include=api_keys&limit=all")
    if r.status != 200:
        raise RuntimeError(f"ghost list integrations failed: {_ghost_err(r)}")
    integration = next((i for i in r.json().get("integrations", []) if i.get("name") == GHOST_INTEGRATION), None)
    if integration is None:
        r = http.request(
            "POST",
            f"{ADMIN}/integrations/?include=api_keys",
            json_body={"integrations": [{"name": GHOST_INTEGRATION, "description": "Local test integration"}]},
        )
        if r.status not in (200, 201):
            raise RuntimeError(f"ghost create integration failed: {_ghost_err(r)}")
        integration = r.json()["integrations"][0]
        log("ghost: integration created")

    keys = {k["type"]: k for k in integration.get("api_keys", [])}
    admin = keys["admin"]
    secret = admin["secret"]
    # Ghost 6 may return the secret as "<id>:<hex>"; normalise to the documented "<id>:<hex>" admin key.
    admin_key = secret if ":" in secret else f"{admin['id']}:{secret}"
    if not ghost_verify_key(http, admin_key):
        raise RuntimeError("ghost admin key verification failed")

    site = http.request("GET", f"{ADMIN}/site/").json()["site"]
    out = {
        "platform": "ghost",
        "version": site.get("version"),
        "base_url": GHOST_URL,
        "admin_api_url": f"{GHOST_URL}{ADMIN}/",
        "content_api_url": f"{GHOST_URL}/ghost/api/content/",
        "admin_api_key": admin_key,
        "content_api_key": (keys.get("content") or {}).get("secret"),
        "integration_id": integration.get("id"),
        "owner_email": GHOST_OWNER_EMAIL,
        "site_url": site.get("url"),
        "written_at": now_iso(),
    }
    write_json_secure(path, out)
    log(f"ghost: ready (version {site.get('version')}, admin key verified)")
    return {"ok": True, "reused": False}


# ======================================================================= main


def run_once() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    enabled = [p.strip() for p in env("IBX_BOOTSTRAP_PLATFORMS", "lemmy,ghost").split(",") if p.strip()]
    status: dict = {"started_at": now_iso(), "platforms": {}}
    t0 = time.monotonic()
    rc = 0
    for name, fn in (("lemmy", bootstrap_lemmy), ("ghost", bootstrap_ghost)):
        if name not in enabled:
            continue
        try:
            status["platforms"][name] = fn()
        except Exception as e:  # noqa: BLE001 - report and fail the one-shot
            log(f"{name}: FAILED: {type(e).__name__}: {e}")
            status["platforms"][name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            rc = 1
    status["finished_at"] = now_iso()
    status["elapsed_s"] = round(time.monotonic() - t0, 1)
    status["ok"] = rc == 0
    write_json_secure(os.path.join(OUT_DIR, "status.json"), status)
    log(f"done ok={rc == 0} in {status['elapsed_s']}s")
    return rc


def main(argv: list[str]) -> int:
    """Default: bootstrap, then stay up and re-verify every IBX_BOOTSTRAP_RECHECK_S seconds (self-heal).

    The container healthcheck reads status.json, so `docker compose up --wait` returns only after a successful
    bootstrap, and the API service can use `depends_on: bootstrap: condition: service_healthy`.
    `--once` runs a single pass and exits with its status (for `docker compose run --rm bootstrap ... --once`).
    """
    if "--once" in argv:
        return run_once()
    recheck_s = float(env("IBX_BOOTSTRAP_RECHECK_S", "300"))
    try:  # never report healthy from a previous container's status file
        os.remove(os.path.join(OUT_DIR, "status.json"))
    except FileNotFoundError:
        pass
    while True:
        rc = run_once()
        time.sleep(recheck_s if rc == 0 else 10)


def healthcheck() -> int:
    try:
        return 0 if read_json(os.path.join(OUT_DIR, "status.json")).get("ok") else 1
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    if "--healthcheck" in sys.argv:
        sys.exit(healthcheck())
    sys.exit(main(sys.argv[1:]))
