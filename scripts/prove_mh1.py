"""MH1 proof: every connector implements the Appendix A methods, manifest and error codes, checked on the RUNNING service.

    scripts/prove_mh1.sh            (brings a stack up unless IBX_API_URL points at one)

1. The contract test suite in the repo (``tests/contract``: conformance kit over the registry, OpenAPI match).
2. Live probe of every registered connector over HTTP, with Appendix A arguments only:
   manifest keys and schema; error_map codes are shared codes; each method in ``capabilities`` answers with EXACTLY the
   Appendix A output keys (``get_status`` / ``delete`` / ``fetch_metrics`` get only ``platform_post_id`` + ``connection``);
   each method not in ``capabilities`` answers 404 ``METHOD_NOT_SUPPORTED``; errors use the IS5 envelope;
   the ``/api/v1/connector/...`` mirror answers the same.
   Targets: mock-platforms for all six, plus the REAL local Lemmy and Ghost when the stack runs in local mode.
Evidence: evidence/must_haves/MH1_contract.log, evidence/must_haves/MH1_contract_matrix.csv
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from prove_lib import CAPABILITY_METHOD, OUTPUT_KEYS, REAL_DEST, ROOT, Proof, dest_url, tag

from app.contract.manifest import Manifest

SHARED_CODES = {"DEST_SPEC_VIOLATION", "DEST_RULE_FREQUENCY_CAP", "CONNECTION_AUTH_EXPIRED", "PLATFORM_RATE_LIMIT",
                "PLATFORM_QUOTA_EXHAUSTED", "PLATFORM_CONTENT_REJECTED", "PLATFORM_UNAVAILABLE", "CONNECTOR_DISABLED"}
MANIFEST_KEYS = {"key", "version", "tier", "auth", "surfaces", "rate_limits", "capabilities", "error_map", "instance_param"}
ALL_METHODS = ["validate", "publish", "get_status", "fetch_metrics", "refresh_auth", "delete", "build_assist_pack"]
RENDITION = {"title": "Contract conformance check post", "content_type": "text", "ai_generated": False,
             "body": "This post checks the IncuBrix connector contract end to end. It is removed straight away."}


def envelope_ok(response: Any) -> bool:
    try:
        error = response.json()["error"]
    except (ValueError, KeyError, TypeError):
        return False
    return set(error) == {"code", "message", "retryable", "details"} and error["code"].isupper()


def probe_connector(p: Proof, key: str, manifest: dict[str, Any], url: str, target: str, rows: list[dict[str, Any]]) -> None:
    caps = set(manifest["capabilities"])
    supported = {"validate"} | {CAPABILITY_METHOD[c] for c in caps if c in CAPABILITY_METHOD}
    destination = {"connector_key": key, "url": url, "rules": {"automation_allowed": True}}
    connection = {"connector_key": key}
    post_id: str | None = None
    for method in ALL_METHODS:
        if method not in supported:
            response = p.http("POST", f"/connector/v1/{key}/{method}", json={"platform_post_id": "1", "connection": connection})
            ok = response.status_code == 404 and envelope_ok(response) and response.json()["error"]["code"] == "METHOD_NOT_SUPPORTED"
            p.check(f"{key}@{target} {method} not in capabilities -> 404 METHOD_NOT_SUPPORTED", ok, f"HTTP {response.status_code}")
            rows.append({"connector": key, "target": target, "method": method, "in_capabilities": False,
                         "http_status": response.status_code, "keys_exact": "", "result": "PASS" if ok else "FAIL"})
            continue
        if method == "validate":
            body = {"rendition": RENDITION, "destination": destination, "connection": connection}
        elif method == "publish":
            body = {"rendition": RENDITION, "destination": destination, "connection": connection,
                    "idempotency_key": tag(f"mh1-{key}-")}
        elif method == "build_assist_pack":
            body = {"rendition": RENDITION, "destination": destination}
        elif method == "refresh_auth":
            body = {"connection": connection, "destination": destination}  # Ghost needs to know which instance
        elif post_id is None:
            p.check(f"{key}@{target} {method} skipped: no post id from publish", False)
            continue
        elif method == "fetch_metrics":
            body = {"platform_post_id": post_id, "connection": connection, "since": "2026-01-01T00:00:00Z"}
        else:  # get_status, delete: exactly the Appendix A arguments, no destination
            body = {"platform_post_id": post_id, "connection": connection}
        response = p.http("POST", f"/connector/v1/{key}/{method}", json=body)
        got = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        exact = isinstance(got, dict) and set(got) == OUTPUT_KEYS[method]
        ok = response.status_code == 200 and exact
        label = "(Appendix A arguments only) " if "destination" not in body else ""
        p.check(f"{key}@{target} {method} {label}-> 200 with exactly {sorted(OUTPUT_KEYS[method])}", ok,
                f"HTTP {response.status_code}, keys {sorted(got) if isinstance(got, dict) else got}"
                + (f", {got['error']['message']}" if isinstance(got, dict) and isinstance(got.get("error"), dict) else ""))
        if not ok and label and response.status_code == 422:
            # Instance resolution: the connection may name its instance; shows whether only the ambiguity is the problem
            named = {**body, "connection": {**connection, manifest["instance_param"]: url}}
            again = p.http("POST", f"/connector/v1/{key}/{method}", json=named)
            again_got = again.json() if again.headers.get("content-type", "").startswith("application/json") else {}
            p.check(f"{key}@{target} {method} with the connection naming its {manifest['instance_param']} -> 200, exact keys",
                    again.status_code == 200 and isinstance(again_got, dict) and set(again_got) == OUTPUT_KEYS[method],
                    f"HTTP {again.status_code}")
        rows.append({"connector": key, "target": target, "method": method, "in_capabilities": True,
                     "http_status": response.status_code, "keys_exact": exact, "result": "PASS" if ok else "FAIL"})
        if method == "publish" and ok:
            post_id = got["platform_post_id"]
            mirror = p.http("POST", f"/api/v1/connector/{key}/publish", json=body)  # same key + body: a replay
            p.check(f"{key}@{target} /api/v1/connector mirror replays the same post", mirror.status_code == 200
                    and mirror.json().get("platform_post_id") == post_id, f"HTTP {mirror.status_code}")
    # one error per connector: an empty title/body is a spec violation (assisted has no limits: unknown method instead)
    bad = p.http("POST", f"/connector/v1/{key}/validate_typo", json={})
    p.check(f"{key}@{target} unknown method -> IS5 error envelope", bad.status_code == 404 and envelope_ok(bad),
            f"HTTP {bad.status_code}")


def main() -> int:
    p = Proof("MH1", "Exact connector contract (Appendix A methods, manifest, error codes)", "scripts/prove_mh1.sh")
    p.section("1. Contract test suite in the repo (tests/contract)")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/contract"], cwd=ROOT,
                            capture_output=True, text=True, timeout=900)
    tail = "\n".join(result.stdout.strip().splitlines()[-3:])
    p.log(tail)
    p.check("uv run pytest -q tests/contract", result.returncode == 0, tail.splitlines()[-1] if tail else result.stderr[-300:])

    p.section("2. Shared error codes served by the running API")
    codes = {c["code"] for c in p.http("GET", "/api/v1/errors", quiet=True).json()["codes"]}
    p.check("all 8 Appendix A shared codes are in /api/v1/errors", SHARED_CODES <= codes, sorted(SHARED_CODES - codes))

    p.section("3. Manifests and methods of every registered connector (live)")
    mode = p.mode()
    listing = p.http("GET", "/api/v1/connectors").json()
    keys = [c["key"] for c in listing["connectors"]]
    p.check("registry has no load errors", not listing.get("registry_errors"), listing.get("registry_errors"))
    p.check("the six Core connectors are registered", {"discourse", "lemmy", "devto", "ghost", "webhook", "assisted"} <= set(keys), keys)
    rows: list[dict[str, Any]] = []
    for key in keys:
        manifest = p.http("GET", f"/connector/v1/{key}/manifest").json()
        missing = MANIFEST_KEYS - set(manifest)
        try:
            Manifest.model_validate(manifest)
            schema_ok, why = True, ""
        except Exception as exc:  # noqa: BLE001 - reported
            schema_ok, why = False, str(exc)[:300]
        p.check(f"{key} manifest has every Appendix A key and validates", not missing and schema_ok, why or sorted(missing))
        bad_codes = {v for v in manifest.get("error_map", {}).values() if v not in codes}
        p.check(f"{key} error_map values are shared/listed codes", not bad_codes, sorted(bad_codes))
        p.check(f"{key} capabilities are contract methods", set(manifest["capabilities"]) <= set(CAPABILITY_METHOD),
                manifest["capabilities"])
        if key in ("discourse", "lemmy", "devto", "ghost", "webhook", "assisted"):
            probe_connector(p, key, manifest, dest_url(key, tag("mh1-")), "mock", rows)
            if mode == "local" and key in REAL_DEST:
                probe_connector(p, key, manifest, REAL_DEST[key], "real-local", rows)
    if mode != "local":
        p.add_partial("stack runs in simulation mode: the real local Lemmy/Ghost were not probed in this run")
    p.write_csv("evidence/must_haves/MH1_contract_matrix.csv", rows,
                ["connector", "target", "method", "in_capabilities", "http_status", "keys_exact", "result"])
    return p.finish("evidence/must_haves/MH1_contract.log")


if __name__ == "__main__":
    sys.exit(main())
