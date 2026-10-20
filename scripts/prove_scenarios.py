"""Per-connector scenarios shared by the proofs (MH5 back-off, MH6 safe retries, CORE3 error matrix).

Every scenario makes REAL calls through the public contract (``POST /connector/v1/{key}/publish``) to either
  * mock-platforms, driven only through the platform's own behaviour (revoked keys, its own rate limiter, its own
    validation) that the mocks reproduce from recorded fixtures or the platform's docs, or through the generic fault
    header where a platform has no such behaviour locally (said so in ``trigger``), or
  * the REAL local Lemmy / Ghost containers (``real=True``, local mode only).
Each returns one result row (a dict) with what was sent, what came back, and whether it matches the expectation.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from prove_lib import REAL_DEST, Proof, dest_url, in_stack, lemmy_posts_with, lemmy_set_post_limit, tag, toxic

LEMMY_PRELUDE = ("import json, httpx; cfg = json.load(open('/bootstrap/out/lemmy.json')); B = cfg['base_url'];")
FAKE_GHOST_KEY = "0123456789abcdef01234567:" + "ab" * 32  # well-formed Admin API key that no Ghost knows


def rendition(title: str | None = None, body: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"title": title or f"IncuBrix connector check {uuid.uuid4().hex[:6]}", "content_type": "text", "ai_generated": False,
            "body": body or "A neutral check post from the IncuBrix connector suite. It is removed straight away.", **extra}


def _err(response: Any) -> dict[str, Any]:
    try:
        error = response.json().get("error")
    except ValueError:
        return {}
    return error if isinstance(error, dict) else {}


def _row(connector: str, target: str, scenario: str, trigger: str, response: Any, expected_code: str,
         expected_status: int, extra_ok: bool = True, **more: Any) -> dict[str, Any]:
    error = _err(response) if response is not None else {}
    status = response.status_code if response is not None else None
    ok = status == expected_status and error.get("code") == expected_code and extra_ok
    return {"connector": connector, "target": target, "scenario": scenario, "trigger": trigger,
            "http_status": status, "error_code": error.get("code"), "retryable": error.get("retryable"),
            "platform_status": (error.get("details") or {}).get("platform_status"),
            "platform_message": str((error.get("details") or {}).get("platform_message") or "")[:160],
            "message": str(error.get("message") or "")[:220], "expected": f"{expected_status} {expected_code}",
            "status": "PASS" if ok else "FAIL", **more}


def _dest(connector: str, target: str, tenant: str, **extra: Any) -> dict[str, Any]:
    url = REAL_DEST[connector] if target == "real-local" else dest_url(connector, tenant)
    # inline rules win over any catalogue row for the same instance (D9): no cap or promo rule interferes here
    return {"url": url, "rules": {"automation_allowed": True, "frequency_cap": "1000/PT1H", "self_promo_allowed": True}, **extra}


# ---------------------------------------------------------------------------------------------- rate limit
def rate_limit(p: Proof, connector: str, target: str = "mock") -> dict[str, Any]:
    """429 (or Lemmy's 400 rate_limit_error) with a reset time -> PLATFORM_RATE_LIMIT with retry_at; the next call
    short-circuits without calling the platform."""
    tenant = tag(f"rl-{connector}-")
    dest = _dest(connector, target, tenant)
    key_base = tag("rl-")
    trigger = ""
    created: list[str] = []
    if target == "real-local" and connector == "lemmy":
        trigger = "real Lemmy: site post limit lowered to 1 per 10 s (admin PUT /api/v3/site), then restored"
        if not lemmy_set_post_limit(1, 10):
            return {"connector": connector, "target": target, "scenario": "rate_limit", "trigger": trigger,
                    "status": "FAIL", "message": "could not lower the Lemmy post limit"}
    elif connector == "discourse":
        trigger = "Discourse's own topic rate limit (site setting rate_limit_create_topic=600 s) -> 429 + Retry-After + extras.wait_seconds"
        p.mock_call("PUT", f"/discourse/{tenant}/admin/site_settings/rate_limit_create_topic",
                    headers={"Api-Key": "sim-token", "Api-Username": "system"}, json={"rate_limit_create_topic": 600})
    elif connector == "lemmy":
        trigger = "Lemmy's own post limiter (mock, measured on 0.19.20): post limit 1 per 60 s -> 400 rate_limit_error"
        p.mock_call("PUT", f"/lemmy/{tenant}/api/v3/site", headers={"Authorization": "Bearer mock-admin"},
                    json={"rate_limit_post": 1, "rate_limit_post_per_second": 60})
    elif connector == "devto":
        trigger = "Forem's limiter (mock control): 1 article per 120 s -> 429 + Retry-After as an HTTP-date"
        p.mock_call("POST", f"/forem/{tenant}/_mock/rate_limit", json={"limit": 1, "window_s": 120, "retry_after": "http-date"})
    elif connector == "webhook":
        trigger = "partner limiter (mock): 1 post per 120 s -> 429 + X-RateLimit-Reset (epoch) + Retry-After"
        p.mock_call("POST", f"/partner/{tenant}/_partner/rate_limit", json={"limit": 1, "window_s": 120})
    elif connector == "ghost":
        trigger = ("generic 429 + Retry-After: 90 queued on the Ghost mock (the real Ghost Admin API gave no 429 locally; "
                   "only its login limiter did)")
    try:
        first = p.publish(connector, rendition(), dest, key=f"{key_base}-1")
        if first.status_code == 200:
            created.append(first.json()["platform_post_id"])
        if connector == "ghost" and target == "mock":
            p.mock_call("POST", "/_faults", json={"platform": "ghost", "tenant": tenant, "method": "POST", "fault": "429:90", "count": 1})
        before = p.creates(connector, tenant) if target == "mock" else None
        limited = p.publish(connector, rendition(), dest, key=f"{key_base}-2")
        if limited.status_code == 200:  # real Lemmy: the bucket may still have had a slot; one more
            created.append(limited.json()["platform_post_id"])
            limited = p.publish(connector, rendition(), dest, key=f"{key_base}-3")
        details = _err(limited).get("details") or {}
        mid = p.creates(connector, tenant) if target == "mock" else None
        again = p.publish(connector, rendition(), dest, key=f"{key_base}-4")
        after = p.creates(connector, tenant) if target == "mock" else None
        short = (_err(again).get("details") or {}).get("short_circuit") is True and again.status_code == 429
        no_call = (after == mid) if target == "mock" else True
        has_reset = bool(details.get("retry_at")) and (details.get("retry_after_s") or 0) > 0
        row = _row(connector, target, "rate_limit", trigger, limited, "PLATFORM_RATE_LIMIT", 429,
                   extra_ok=has_reset and short and no_call and limited.headers.get("retry-after") is not None,
                   retry_after_s=details.get("retry_after_s"), retry_at=details.get("retry_at"),
                   reset_source=details.get("reset_source"), retry_after_header=limited.headers.get("retry-after"),
                   next_call=f"{again.status_code} short_circuit={short}",
                   platform_creates=(f"{before}->{mid}->{after}" if target == "mock" else "n/a (real)"))
        if target == "real-local" and connector == "lemmy" and row["status"] == "PASS":
            time.sleep(float(details.get("retry_after_s") or 10) + 1)
            later = p.publish(connector, rendition(), dest, key=f"{key_base}-5")
            row["after_reset"] = later.status_code
            if later.status_code == 200:
                created.append(later.json()["platform_post_id"])
            else:
                row["status"] = "FAIL"
        return row
    finally:
        if target == "real-local" and connector == "lemmy":
            lemmy_set_post_limit(1000, 60)
        for post_id in created:
            if connector in ("discourse", "lemmy", "ghost", "webhook"):
                p.http("POST", f"/connector/v1/{connector}/delete", quiet=True,
                       json={"platform_post_id": post_id, "connection": {"connector_key": connector},
                             "destination": {"connector_key": connector, "url": dest["url"]}})


# ---------------------------------------------------------------------------------------------- expired token
def auth_expired(p: Proof, connector: str, target: str = "mock") -> dict[str, Any]:
    """A revoked / expired credential -> CONNECTION_AUTH_EXPIRED (424), the stored connection is paused and the next
    publish fails fast without a platform call."""
    tenant = tag(f"auth-{connector}-")
    dest = _dest(connector, target, tenant)
    conn_id = None
    if target == "real-local" and connector == "lemmy":
        code = LEMMY_PRELUDE + (
            "t = httpx.post(B + '/api/v3/user/login', json={'username_or_email': cfg['admin_username'], 'password': cfg['admin_password']}).json()['jwt'];"
            "httpx.post(B + '/api/v3/user/logout', headers={'Authorization': 'Bearer ' + t}); print(json.dumps(t))")
        credentials, trigger = {"jwt": in_stack(code)}, "real Lemmy: a JWT that was logged out (revoked) -> 401 incorrect_login"
    elif target == "real-local" and connector == "ghost":
        credentials, trigger = {"admin_api_key": FAKE_GHOST_KEY}, "real Ghost: an Admin API key Ghost does not know -> 401"
    elif connector == "discourse":
        key = tag("k-")
        credentials, trigger = {"api_key": key, "api_username": "system"}, "Discourse key revoked by the forum admin -> 403 invalid_access"
    elif connector == "lemmy":
        credentials, trigger = {"jwt": "revoked-" + tag()}, "Lemmy logged-out token (mock, recorded body) -> 401 incorrect_login"
    elif connector == "devto":
        key = tag("user-")
        credentials, trigger = {"api_key": key}, "Forem API key revoked -> 401"
    elif connector == "webhook":
        credentials, trigger = {"secret": "sim-secret"}, "partner revokes IncuBrix's access (POST /_partner/revoke) -> 401 revoked"
    elif connector == "ghost":
        credentials, trigger = {"admin_api_key": FAKE_GHOST_KEY}, "Ghost integration deleted (kid revoked on the mock) -> 401"
    else:
        raise ValueError(connector)
    created = p.http("POST", "/api/v1/connections", json={"connector_key": connector, "auth_type": "api_key",
                                                         "credentials": credentials, "account_handle": f"auth-check-{tenant}"})
    conn_id = created.json().get("connection_id")
    connection = {"connector_key": connector, "connection_id": conn_id}
    if target == "mock":
        if connector == "discourse":
            p.mock_call("POST", f"/discourse/{tenant}/_mock/revoke", json={"key": credentials["api_key"]})
        elif connector == "devto":
            p.mock_call("POST", f"/forem/{tenant}/_mock/revoke", json={"key": credentials["api_key"]})
        elif connector == "webhook":
            p.mock_call("POST", f"/partner/{tenant}/_partner/revoke")
        elif connector == "ghost":
            p.mock_call("POST", f"/ghost/{tenant}/_mock/revoke", json={"kid": FAKE_GHOST_KEY.split(':')[0]})
    response = p.publish(connector, rendition(), dest, connection=connection)
    conn = p.http("GET", f"/api/v1/connections/{conn_id}", quiet=True).json() if conn_id else {}
    paused = str(conn.get("status") or conn.get("state") or "").startswith("paused")
    before = p.creates(connector, tenant) if target == "mock" else None
    again = p.publish(connector, rendition(), dest, connection=connection)
    after = p.creates(connector, tenant) if target == "mock" else None
    fast = again.status_code == 424 and (before == after if target == "mock" else True)
    return _row(connector, target, "expired_token", trigger, response, "CONNECTION_AUTH_EXPIRED", 424,
                extra_ok=paused and fast, connection_paused=paused, next_call=f"{again.status_code} {_err(again).get('code')}",
                platform_creates=(f"{before}->{after}" if target == "mock" else "n/a (real)"))


# ---------------------------------------------------------------------------------------------- rejected content
def rejected(p: Proof, connector: str, target: str = "mock") -> dict[str, Any]:
    """Content the PLATFORM refuses (our spec check loosened with spec_overrides so the request really goes out)
    -> PLATFORM_CONTENT_REJECTED (422, not retryable) with the platform's own message."""
    tenant = tag(f"rej-{connector}-")
    if connector == "discourse":
        dest = _dest(connector, target, tenant)
        title = f"Duplicate topic title check {tag()}"
        first = p.publish(connector, rendition(title), dest)
        trigger = "Discourse refuses a duplicate topic title -> 422 errors[]"
        response = p.publish(connector, rendition(title), dest)
        if first.status_code == 200:
            p.http("POST", "/connector/v1/discourse/delete", quiet=True,
                   json={"platform_post_id": first.json()["platform_post_id"], "connection": {"connector_key": connector},
                         "destination": {"connector_key": connector, "url": dest["url"]}})
    elif connector == "lemmy":
        dest = _dest(connector, target, tenant, spec_overrides={"title_max_chars": 400})
        trigger = f"{'real ' if target != 'mock' else ''}Lemmy refuses a 250-character title -> 400 invalid_post_title"
        response = p.publish(connector, rendition("T" * 250), dest)
    elif connector == "ghost":
        dest = _dest(connector, target, tenant, spec_overrides={"title_max_chars": 400})
        trigger = f"{'real ' if target != 'mock' else ''}Ghost refuses a 300-character title -> 422 ValidationError"
        response = p.publish(connector, rendition("Long " * 60), dest)
    elif connector == "devto":
        dest = _dest(connector, target, tenant, spec_overrides={"title_max_chars": 400})
        trigger = "Forem refuses a 140-character title -> 422 'Title is too long'"
        response = p.publish(connector, rendition("D" * 140), dest)
    elif connector == "webhook":
        dest = _dest(connector, target, tenant, spec_overrides={"title_max_chars": 1000})
        trigger = "partner refuses a 350-character title -> 422 validation_failed"
        response = p.publish(connector, rendition("W" * 350), dest)
    else:
        raise ValueError(connector)
    error = _err(response)
    sent = p.creates(connector, tenant) if target == "mock" and connector != "discourse" else None
    return _row(connector, target, "rejected_content", trigger, response, "PLATFORM_CONTENT_REJECTED", 422,
                extra_ok=error.get("retryable") is False and bool((error.get("details") or {}).get("platform_message")),
                platform_creates=sent if sent is not None else "")


def assisted_rejected(p: Proof) -> dict[str, Any]:
    """Assisted: a person posts, so the only 'platform rejected it' signal is the person's report. A certified assisted
    destination gets a publication (pack, awaiting_human); completing it with outcome rejected_by_community must record
    PLATFORM_CONTENT_REJECTED with the reason."""
    tenant = tag("rej-assisted-")
    trigger = "person reports 'rejected_by_community' via POST /api/v1/assist-tasks/{id}/complete"
    dest = p.create_dest({"name": f"Assisted reject {tenant}", "url": dest_url("assisted", tenant), "connector_key": "assisted",
                          "automation_allowed": "false", "self_promo_allowed": "true", "language": "en"})
    run = p.certify(dest["destination_id"])
    started = p.http("POST", "/api/v1/publications", json={"destination_id": dest["destination_id"], "content": {
        "title": "How do you test posts to long-tail communities?", "language": "en", "ai_generated": False,
        "body": "We compared three ways to test forum posting end to end. Which one has worked for you?"}})
    if started.status_code != 202:
        return _row("assisted", "mock", "rejected_content", trigger, started, "PLATFORM_CONTENT_REJECTED", 422,
                    extra_ok=False, note=f"publication not accepted (cert {run.get('state')})")
    p.wait_job(started.json()["job_id"])
    pub_id = started.json()["publication_id"]
    done = p.http("POST", f"/api/v1/assist-tasks/{pub_id}/complete",
                  json={"outcome": "rejected_by_community", "reason": "Moderator removed it: self-promotion rule 3"})
    pub = p.http("GET", f"/api/v1/publications/{pub_id}").json()
    code = pub.get("code") or ((pub.get("error") or {}).get("code") if isinstance(pub.get("error"), dict) else None)
    ok = done.status_code in (200, 202) and code == "PLATFORM_CONTENT_REJECTED" and pub.get("state") in ("failed", "rejected")
    return {"connector": "assisted", "target": "mock", "scenario": "rejected_content", "trigger": trigger,
            "http_status": done.status_code, "error_code": code, "retryable": None, "platform_status": None,
            "platform_message": str(pub.get("reason") or "")[:160], "message": str(done.text)[:220],
            "expected": "publication failed with PLATFORM_CONTENT_REJECTED + reason", "status": "PASS" if ok else "FAIL",
            "publication_state": pub.get("state")}


# ---------------------------------------------------------------------------------------------- timeout + retry
def timeout_retry(p: Proof, connector: str, target: str = "mock") -> dict[str, Any]:
    """The platform creates the post but the answer never arrives in time -> 503 PLATFORM_UNAVAILABLE (outcome unknown);
    the retry with the same idempotency key returns the same post; exactly one post exists on the platform; the same key
    with a different body -> 409 IDEMPOTENCY_KEY_REUSED."""
    tenant = tag(f"to-{connector}-")
    key = tag(f"to-{connector}-")
    rend = rendition(f"Timeout and retry check {key}")
    if target == "real-local":
        proxied = {"lemmy": "http://toxiproxy:18536/c/ibx_test", "ghost": "http://toxiproxy:12368"}[connector]
        dest = {"url": proxied, "rules": {"automation_allowed": True, "frequency_cap": "1000/PT1H", "self_promo_allowed": True}}
        trigger = f"toxiproxy holds the real {connector.title()}'s answer for 13 s (> PLATFORM_TIMEOUT_S 10 s)"
        toxic(f"{connector}_proxy", 13000)
        try:
            first = p.publish(connector, rend, dest, key=key)
        finally:
            toxic(f"{connector}_proxy", None)
    else:
        dest = _dest(connector, target, tenant)
        trigger = "mock creates the post, then holds the answer 15 s (X-Sim-Fault: timeout_after_create:15)"
        first = p.publish(connector, rend, dest, key=key, headers={"X-Sim-Fault": "timeout_after_create:15"})
    waits = []
    retry = p.publish(connector, rend, dest, key=key, headers={"X-IncuBrix-Verbose": "1"})
    for _ in range(3):  # a well-behaved client honours "outcome still unknown, Retry-After: n" and retries
        if retry.status_code != 503 or not retry.headers.get("retry-after"):
            break
        waits.append(float(retry.headers["retry-after"]))
        time.sleep(waits[-1])
        retry = p.publish(connector, rend, dest, key=key, headers={"X-IncuBrix-Verbose": "1"})
    replay = p.publish(connector, rend, dest, key=key)
    changed = p.publish(connector, {**rend, "title": rend["title"] + " (edited)"}, dest, key=key)
    post_id = retry.json().get("platform_post_id") if retry.status_code == 200 else None
    if target == "mock":
        on_platform = p.creates(connector, tenant)
    elif connector == "lemmy":
        marker = ((retry.json().get("_meta") or {}).get("marker")) if retry.status_code == 200 else None
        on_platform = len(lemmy_posts_with(f"({marker})")) if marker else -1
    else:
        from prove_lib import ghost_posts_with_slug_suffix
        marker = ((retry.json().get("_meta") or {}).get("marker")) if retry.status_code == 200 else None
        on_platform = len(ghost_posts_with_slug_suffix(marker)) if marker else -1
    ok = (first.status_code == 503 and _err(first).get("code") == "PLATFORM_UNAVAILABLE"
          and (_err(first).get("details") or {}).get("outcome") == "unknown"
          and retry.status_code == 200 and replay.status_code == 200 and replay.json().get("platform_post_id") == post_id
          and on_platform == 1 and changed.status_code == 409 and _err(changed).get("code") == "IDEMPOTENCY_KEY_REUSED")
    if post_id and connector in ("discourse", "lemmy", "ghost", "webhook"):
        p.http("POST", f"/connector/v1/{connector}/delete", quiet=True,
               json={"platform_post_id": post_id, "connection": {"connector_key": connector},
                     "destination": {"connector_key": connector, "url": dest["url"]}})
    return {"connector": connector, "target": target, "scenario": "timeout_then_retry", "trigger": trigger,
            "first": f"{first.status_code} {_err(first).get('code')} outcome={(_err(first).get('details') or {}).get('outcome')}",
            "retry": f"{retry.status_code} after waits {waits} replayed={retry.headers.get('idempotent-replayed')} "
                     f"reconciled={(retry.json().get('_meta') or {}).get('reconciled') if retry.status_code == 200 else None}",
            "replay_same_id": replay.status_code == 200 and replay.json().get("platform_post_id") == post_id,
            "same_key_other_body": f"{changed.status_code} {_err(changed).get('code')}",
            "posts_on_platform": on_platform, "duplicates": max(0, on_platform - 1) if on_platform >= 0 else "unknown",
            "status": "PASS" if ok else "FAIL"}


def create_then_error(p: Proof, connector: str) -> dict[str, Any]:
    """The platform commits, then a proxy answers 502: the outcome is unknown, so the retry reconciles (no duplicate)."""
    tenant = tag(f"c5-{connector}-")
    key = tag(f"c5-{connector}-")
    rend = rendition(f"Create then 502 check {key}")
    dest = _dest(connector, "mock", tenant)
    first = p.publish(connector, rend, dest, key=key, headers={"X-Sim-Fault": "create_then_502"})
    retry = p.publish(connector, rend, dest, key=key)
    on_platform = p.creates(connector, tenant)
    ok = first.status_code in (502, 503) and retry.status_code == 200 and on_platform == 1
    return {"connector": connector, "target": "mock", "scenario": "create_then_502", "trigger": "X-Sim-Fault: create_then_502",
            "first": f"{first.status_code} {_err(first).get('code')}", "retry": str(retry.status_code),
            "replay_same_id": "", "same_key_other_body": "", "posts_on_platform": on_platform,
            "duplicates": max(0, on_platform - 1), "status": "PASS" if ok else "FAIL"}


def parallel_same_key(p: Proof, connector: str, copies: int = 8) -> dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor

    tenant = tag(f"par-{connector}-")
    key = tag(f"par-{connector}-")
    rend = rendition(f"Parallel duplicates check {key}")
    dest = _dest(connector, "mock", tenant)
    with ThreadPoolExecutor(copies) as pool:
        results = list(pool.map(lambda _: p.publish(connector, rend, dest, key=key), range(copies)))
    statuses = sorted(r.status_code for r in results)
    ids = {r.json().get("platform_post_id") for r in results if r.status_code == 200}
    on_platform = p.creates(connector, tenant)
    ok = on_platform == 1 and len(ids) <= 1 and 500 not in statuses
    return {"connector": connector, "target": "mock", "scenario": f"{copies}_parallel_same_key", "trigger": "concurrent requests",
            "first": str(statuses), "retry": f"distinct ids {len(ids)}", "replay_same_id": len(ids) == 1,
            "same_key_other_body": "", "posts_on_platform": on_platform, "duplicates": max(0, on_platform - 1),
            "status": "PASS" if ok else "FAIL"}
