"""MH9 proof: the Integration Standard (IS1-IS10) on the running service, and the live demo.

    scripts/prove_mh9.sh            (IBX_LIVE_URL=<demo URL> also checks the live demo)

* scripts/integration_selfcheck.sh -> evidence/integration/IS_selfcheck.txt (IS2, 3, 4, 5, 6, 8, 10) and the IS8 log sample.
* IS1: .env.example lists every setting (from the self-check) + the clean-copy run of MH8 (one-command start and test).
* IS4 on the contract: the Idempotency-Key HEADER on a contract publish (used when the body has no key; a mismatch -> 409).
* IS7: upload (multipart) and URL input to POST /api/v1/media, stored in S3, returned as a signed download link that opens.
* IS9: a job manifest carries providers/versions, settings, seed, timings, cost, warnings and licence/rights.
* IS10: the batch runner over test_pack/inputs/sample (--strict).
* Live demo: IBX_LIVE_URL /health + /ready (and evidence/metrics/uptime.csv if a monitor writes one).
Evidence: evidence/integration/IS_selfcheck.txt, IS8_log_sample.jsonl, IS7_files_in_out.log, IS9_manifest.json,
          IS10_batch_summary.csv, evidence/must_haves/MH9_integration.log
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from prove_lib import API, ROOT, TOKEN, Proof, local_path, read_proof, tag

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg==")
MANIFEST_KEYS = ["providers", "settings", "seed", "timings_ms", "cost", "warnings", "licence", "rights", "service"]


def main() -> int:
    p = Proof("MH9", "Integration Standard followed and the live demo works", "scripts/prove_mh9.sh")
    p.mode()
    status: dict[str, list[str]] = {f"IS{n}": [] for n in range(1, 11)}  # failure reasons per point

    p.section("self-check (IS2, IS3, IS4, IS5, IS6, IS8, IS10)")
    out = subprocess.run(["bash", "scripts/integration_selfcheck.sh", API], cwd=ROOT, capture_output=True, text=True, timeout=900,
                         env={**os.environ, "API_TOKEN": TOKEN})
    p.log(out.stdout[-4000:])
    for line in out.stdout.splitlines():
        match = re.match(r"(PASS|FAIL)\s+IS(\d+)", line)
        if match:
            ok = match.group(1) == "PASS"
            p.check(line[6:90], ok, line[90:300], group=f"IS{match.group(2)}")
            if not ok:
                status[f"IS{match.group(2)}"].append(line[:200])
    for f in ("evidence/integration/IS_selfcheck.txt", "evidence/integration/IS8_log_sample.jsonl"):
        if (ROOT / f).exists():
            p.evidence.append(f)
    if "PASS  IS8 log sample" not in out.stdout:
        status["IS8"].append("no IS8 log sample (needs docker access to the stack)")

    p.section("IS1: one-command start and test")
    is6 = [c for c in p.checks if c.get("group") == "IS6"]
    p.check("IS1 .env.example lists every setting (self-check IS6 line)", bool(is6) and all(c["ok"] for c in is6), "")
    mh8 = read_proof("MH8")
    if mh8 and mh8.get("run_id") == os.environ.get("IBX_PROVE_RUN_ID", mh8.get("run_id")) and mh8["status"] == "Yes":
        p.check("IS1 clean copy: docker compose up --wait + scripts/test.sh passed (MH8 proof)", True, mh8["finished_at"])
    else:
        status["IS1"].append("the clean-copy run (MH8) has not passed in this run" + (f": {mh8['status']}" if mh8 else ""))
        p.check("IS1 clean copy: docker compose up --wait + scripts/test.sh passed (MH8 proof)", False,
                (mh8 or {}).get("status", "not run"), critical=False)

    p.section("IS4 on the contract: Idempotency-Key header on publish")
    url = f"http://mock-platforms:8080/discourse/{tag('is4-')}"
    body = {"rendition": {"title": "Header key check for the contract publish", "body": "One key in the header, one post."},
            "destination": {"connector_key": "discourse", "url": url, "rules": {"automation_allowed": True}},
            "connection": {"connector_key": "discourse"}}
    key = tag("is4-")
    first = p.http("POST", "/connector/v1/discourse/publish", json=body, headers={"Idempotency-Key": key})
    second = p.http("POST", "/connector/v1/discourse/publish", json=body, headers={"Idempotency-Key": key})
    clash = p.http("POST", "/connector/v1/discourse/publish", json={**body, "idempotency_key": key + "-other"},
                   headers={"Idempotency-Key": key})
    ok = (first.status_code == 200 and second.status_code == 200
          and first.json().get("platform_post_id") == second.json().get("platform_post_id")
          and clash.status_code == 409)
    p.check("contract publish: header key used when the body has none (same post twice); header/body mismatch -> 409", ok,
            {"first": first.status_code, "second": second.status_code, "mismatch": clash.status_code,
             "mismatch_code": (clash.json().get("error") or {}).get("code") if clash.status_code >= 400 else None})
    if not ok:
        status["IS4"].append("Idempotency-Key header on contract publish not honoured")

    p.section("IS7: files in (upload, URL) and files out (S3 + signed link)")
    lines = []
    up = p.http("POST", "/api/v1/media", files={"file": ("dot.png", PNG, "image/png")})
    media_path = f"/assist/{tag('is7-')}/media/sample.png"
    by_url = p.http("POST", "/api/v1/media", json={"url": f"http://mock-platforms:8080{media_path}"})
    source_bytes = {"upload": PNG, "url": p.mock.get(media_path).content}
    links_ok = True
    for name, r in (("upload", up), ("url", by_url)):
        original = source_bytes[name]
        got = r.json() if r.status_code == 201 else {}
        dl = httpx.get(API.rstrip("/") + local_path(got["url"]), timeout=30) if got.get("url") else None  # no bearer: the signature is the credential
        ok = r.status_code == 201 and dl is not None and dl.status_code == 200 and dl.content == original
        links_ok &= ok
        lines.append(f"{name}: POST /api/v1/media -> {r.status_code} key={got.get('key')} mime={got.get('mime')}; "
                     f"signed link GET -> {dl.status_code if dl else None}, bytes equal to the source: {bool(dl and dl.content == original)}")
        p.check(f"IS7 {name} input stored in S3 and returned as a working signed download link", ok, lines[-1])
    tampered = httpx.get(API.rstrip("/") + re.sub(r"sig=[0-9a-f]+", "sig=" + "0" * 64, local_path(up.json()["url"])), timeout=30) \
        if up.status_code == 201 else None
    p.check("IS7 a tampered signature is refused (403)", tampered is not None and tampered.status_code == 403,
            tampered.status_code if tampered is not None else None)
    lines.append(f"tampered signature -> {tampered.status_code if tampered is not None else None}")
    p.write_text("evidence/integration/IS7_files_in_out.log", p.header() + "\n".join(lines) + "\n")
    if not links_ok:
        status["IS7"].append("upload/URL input or the signed download link failed")

    p.section("IS9: output manifest")
    job = p.http("POST", "/api/v1/jobs", json={"kind": "selfcheck"}).json()
    done = p.wait_job(job["job_id"])
    manifest = p.client.get(local_path(done["result"]["links"]["manifest"])).json()
    missing = [k for k in MANIFEST_KEYS if k not in manifest]
    p.write_text("evidence/integration/IS9_manifest.json", json.dumps(manifest, indent=1) + "\n")
    p.check("IS9 job manifest has providers, settings, seed, timings, cost, warnings, licence, rights, service version",
            not missing, missing)
    if missing:
        status["IS9"].append(f"manifest misses {missing}")

    p.section("IS10: batch runner over test_pack/inputs/sample")
    with tempfile.TemporaryDirectory(prefix="ibx_batch_") as tmp:
        run = subprocess.run([sys.executable, "-m", "app.batch", "run", "test_pack/inputs/sample", "--out", tmp, "--api", API,
                              "--token", TOKEN, "--strict"], cwd=ROOT, capture_output=True, text=True, timeout=900)
        summary = Path(tmp) / "summary.csv"
        if summary.exists():
            shutil.copy(summary, ROOT / "evidence/integration/IS10_batch_summary.csv")
            p.evidence.append("evidence/integration/IS10_batch_summary.csv")
    tail = (run.stdout.strip().splitlines() or [""])[-1]
    p.check("IS10 batch runner over the sample pack, every expectation met (--strict)", run.returncode == 0, tail)
    if run.returncode != 0:
        status["IS10"].append(f"sample pack: {tail}")

    p.section("live demo")
    live = os.environ.get("IBX_LIVE_URL")
    if live:
        try:
            health = httpx.get(f"{live.rstrip('/')}/health", timeout=20).status_code
            ready = httpx.get(f"{live.rstrip('/')}/ready", timeout=20).status_code
        except httpx.HTTPError as exc:
            health = ready = f"{type(exc).__name__}"
        p.check(f"live demo {live}: /health and /ready answer 200", health == 200 and ready == 200, {"health": health, "ready": ready})
    else:
        p.add_partial("no live demo URL yet (IBX_LIVE_URL not set): 'the live demo works for the whole review period' is unproven")
    if not (ROOT / "evidence/metrics/uptime.csv").exists():
        p.add_partial("no uptime record (evidence/metrics/uptime.csv) for the live demo")

    for n in range(1, 11):
        key = f"IS{n}"
        reasons = status[key] + [c["label"] for c in p.checks if c.get("group") == key and not c["ok"]]
        seen = key in ("IS1", "IS7", "IS9") or any(c.get("group") == key for c in p.checks) or key == "IS4"
        if not seen:
            reasons.append("not checked in this run")
        p.set_sub(key, ("Partial" if key == "IS1" else "No") if reasons else "Yes", reasons)
    return p.finish("evidence/must_haves/MH9_integration.log")


if __name__ == "__main__":
    sys.exit(main())
