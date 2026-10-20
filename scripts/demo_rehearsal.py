"""Local rehearsal of the EXACT live-demo deploy shape, with stand-ins for the free hosts:

    uv run python scripts/demo_rehearsal.py [--keep] [--skip-build] [--cpus 0.1] [--memory 512m]

* Supabase Postgres  → ``postgres:16-alpine`` behind ``edoburu/pgbouncer`` in SESSION mode, pool size 15 (Supabase's
  default), TLS required on the client side (``?sslmode=require``, self-signed cert made here), Supabase-style
  ``postgresql://`` URL. Supavisor itself is not run (it needs its own metadata database); PgBouncer session mode has
  the same contract the app relies on: one server connection per client, so LISTEN/NOTIFY and session advisory
  locks work, and a hard cap on clients.
* Supabase Storage S3 → SeaweedFS 3.97 S3 gateway (path-style, SigV4), fresh random keys.
* Render free         → the ``Dockerfile.demo`` image run with ``--cpus 0.1 --memory 512m`` (Render free: 0.1 CPU,
  512 MB) and ``PORT`` set, published on a free 127.0.0.1 port.

Measured and written to ``evidence/integration/DEMO_local_rehearsal.{json,txt}``:
first boot (container start → /ready 200, → all 14 demo destinations certified), restart (= Render spin-down/up:
container restart → /ready 200), memory (cgroup peak + idle), then ``scripts/integration_selfcheck.sh`` and
``scripts/rehearse.py`` against it (``DEMO_local_rehearsal_selfcheck.txt``, ``DEMO_local_rehearsal_summary.csv``),
``POST /api/v1/sim/reset`` timing, and a token/secret scan of the container log (``DEMO_local_rehearsal_log.jsonl``).
rehearse.py runs in a scratch copy of the repo, so the official evidence/hard_cases files are not overwritten.
Everything (containers, network, volumes, image unless --keep) is removed at the end. Secrets are generated here,
used only in this run and never written to evidence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
P = "ibx_p6c"
NET, PG, BOUNCER, S3, DEMO = f"{P}_net", f"{P}_pg", f"{P}_bouncer", f"{P}_s3", f"{P}_demo"
IMAGE = "ibx-p6c-demo:local"
EVID = ROOT / "evidence" / "integration"
LEVELS = {"demo-discourse": "L4", "demo-lemmy": "L4", "demo-ghost": "L3", "demo-webhook": "L3", "demo-devto": "L2",
          "demo-assisted": "L1", "demo-slack": "L4", "demo-peertube": "L4", "demo-mrss": "L2",
          "dest-weekly-cap-1": "L4", "dest-no-promo": "L4", "dest-ai-disclosure-es": "L4", "dest-strict-limits": "L4",
          "dest-partner-revocable": "L3"}


def sh(*args: str, check: bool = True, capture: bool = True, timeout: float = 900) -> str:
    done = subprocess.run(list(args), capture_output=capture, text=True, timeout=timeout)
    if check and done.returncode != 0:
        raise SystemExit(f"command failed ({done.returncode}): {' '.join(args[:4])} ...\n{(done.stderr or '')[-1500:]}")
    return (done.stdout or "").strip()


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def make_cert(directory: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, BOUNCER)])
    now = dt.datetime.now(dt.UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=2)).sign(key, hashes.SHA256()))
    (directory / "server.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                                             serialization.NoEncryption()))
    (directory / "server.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    for f in directory.iterdir():
        f.chmod(0o644)


def wait_for(fn: Any, timeout_s: float, every_s: float = 0.25) -> float | None:
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        try:
            if fn():
                return time.monotonic() - start
        except Exception:  # noqa: BLE001,S110 - not up yet
            pass
        time.sleep(every_s)
    return None


def cleanup(keep_image: bool) -> None:
    for name in (DEMO, BOUNCER, PG, S3):
        sh("docker", "rm", "-f", "-v", name, check=False)
    sh("docker", "network", "rm", NET, check=False)
    for vol in (f"{P}_pgdata", f"{P}_s3data"):
        sh("docker", "volume", "rm", "-f", vol, check=False)
    if not keep_image:
        sh("docker", "image", "rm", "-f", IMAGE, check=False)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def memory(name: str) -> dict[str, Any]:
    raw = sh("docker", "exec", name, "sh", "-c",
             "cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.peak 2>/dev/null || true", check=False).split()
    stats = sh("docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", name, check=False)
    out: dict[str, Any] = {"docker_stats": stats}
    if len(raw) >= 1:
        out["current_mib"] = round(int(raw[0]) / 2**20, 1)
    if len(raw) >= 2:
        out["peak_mib"] = round(int(raw[1]) / 2**20, 1)
    return out


def main() -> int:  # noqa: PLR0915 - one linear script
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the image (containers are always removed)")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--cpus", default="0.1")
    parser.add_argument("--memory", default="512m")
    args = parser.parse_args()

    pg_pw, s3_key, s3_secret = secrets.token_urlsafe(18), secrets.token_hex(10), secrets.token_urlsafe(24)
    token, callback = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    secret_values = [pg_pw, s3_key, s3_secret, token, callback]
    result: dict[str, Any] = {"started_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                              "shape": {"cpus": args.cpus, "memory": args.memory, "db": "postgres:16-alpine behind pgbouncer "
                                        "session mode (pool 15, TLS required)", "s3": "seaweedfs 3.97 path-style"}}
    cleanup(keep_image=True)
    tmp = Path(tempfile.mkdtemp(prefix="ibx_p6c_"))
    try:
        if not args.skip_build:
            log("building Dockerfile.demo")
            t = time.monotonic()
            sh("docker", "build", "-q", "-f", "Dockerfile.demo", "-t", IMAGE, str(ROOT), timeout=1800)
            result["image_build_s"] = round(time.monotonic() - t, 1)
        size = sh("docker", "image", "inspect", IMAGE, "--format", "{{.Size}}")
        result["image_size_mib"] = round(int(size) / 2**20)
        certs = tmp / "certs"
        certs.mkdir()
        make_cert(certs)
        sh("docker", "network", "create", NET)
        log("starting postgres:16, pgbouncer (session, TLS), seaweedfs")
        sh("docker", "run", "-d", "--name", PG, "--network", NET, "-v", f"{P}_pgdata:/var/lib/postgresql/data",
           "-e", f"POSTGRES_PASSWORD={pg_pw}", "-e", "POSTGRES_USER=postgres", "-e", "POSTGRES_DB=postgres",
           "-e", "POSTGRES_HOST_AUTH_METHOD=scram-sha-256", "-e", "POSTGRES_INITDB_ARGS=--auth=scram-sha-256",
           "postgres:16-alpine")
        if wait_for(lambda: "accepting" in sh("docker", "exec", PG, "pg_isready", "-U", "postgres", check=False), 90) is None:
            raise SystemExit("postgres did not start")
        sh("docker", "run", "-d", "--name", BOUNCER, "--network", NET, "-v", f"{certs}:/certs:ro",
           "-e", f"DB_HOST={PG}", "-e", "DB_USER=postgres", "-e", f"DB_PASSWORD={pg_pw}", "-e", "DB_NAME=postgres",
           "-e", "POOL_MODE=session", "-e", "DEFAULT_POOL_SIZE=15", "-e", "MAX_CLIENT_CONN=200", "-e", "MAX_DB_CONNECTIONS=15",
           "-e", "AUTH_TYPE=scram-sha-256", "-e", "LISTEN_PORT=5432",
           "-e", "CLIENT_TLS_SSLMODE=require", "-e", "CLIENT_TLS_KEY_FILE=/certs/server.key",
           "-e", "CLIENT_TLS_CERT_FILE=/certs/server.crt", "edoburu/pgbouncer:latest")
        s3_json = json.dumps({"identities": [{"name": "ibx", "credentials": [{"accessKey": s3_key, "secretKey": s3_secret}],
                                              "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]})
        sh("docker", "run", "-d", "--name", S3, "--network", NET, "-v", f"{P}_s3data:/data", "-e", f"S3CFG={s3_json}",
           "--entrypoint", "/bin/sh", "chrislusf/seaweedfs:3.97", "-c",
           'printf "%s" "$S3CFG" > /tmp/s3.json && exec weed server -dir=/data -s3 -s3.port=8333 -s3.config=/tmp/s3.json '
           "-volume.max=8 -master.volumeSizeLimitMB=256")
        if wait_for(lambda: subprocess.run(["docker", "exec", S3, "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8333/healthz"],
                                           capture_output=True).returncode == 0, 90) is None:
            raise SystemExit("seaweedfs did not start")

        port = free_port()
        api = f"http://127.0.0.1:{port}"
        env = {"DATABASE_URL": f"postgresql://postgres:{pg_pw}@{BOUNCER}:5432/postgres?sslmode=require",
               "S3_ENDPOINT": f"http://{S3}:8333", "S3_PUBLIC_ENDPOINT": f"http://{S3}:8333", "S3_REGION": "us-east-1",
               "S3_ACCESS_KEY": s3_key, "S3_SECRET_KEY": s3_secret, "API_TOKEN": token, "CALLBACK_SECRET": callback,
               "PUBLIC_BASE_URL": api, "PORT": "10000"}
        env_file = tmp / "demo.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        env_file.chmod(0o600)
        log(f"starting the demo container ({args.cpus} CPU, {args.memory}) on {api}")
        t0 = time.monotonic()
        sh("docker", "run", "-d", "--name", DEMO, "--network", NET, "--cpus", args.cpus, "--memory", args.memory,
           "--memory-swap", args.memory, "--env-file", str(env_file), "-p", f"127.0.0.1:{port}:10000", IMAGE)
        client = httpx.Client(base_url=api, timeout=30, headers={"Authorization": f"Bearer {token}"})
        ready = wait_for(lambda: client.get("/ready").status_code == 200, 600)
        result["first_boot_ready_s"] = round(time.monotonic() - t0, 1) if ready is not None else None
        log(f"first boot: /ready 200 after {result['first_boot_ready_s']} s")

        def all_certified() -> bool:
            items = client.get("/api/v1/destinations", params={"limit": 500}).json()["destinations"]
            got = {d["name"]: d["level"] for d in items if d["name"] in LEVELS}
            return got == LEVELS

        seeded = wait_for(all_certified, 900, every_s=2)
        result["first_boot_seeded_s"] = round(time.monotonic() - t0, 1) if seeded is not None else None
        items = client.get("/api/v1/destinations", params={"limit": 500}).json()["destinations"]
        result["demo_destinations"] = {d["name"]: d["level"] for d in items if d["name"] in LEVELS}
        log(f"14 demo destinations certified after {result['first_boot_seeded_s']} s")
        result["memory_after_seed"] = memory(DEMO)

        log("restart = Render spin-down/spin-up")
        t1 = time.monotonic()
        sh("docker", "restart", "-t", "25", DEMO)
        again = wait_for(lambda: client.get("/ready").status_code == 200, 600)
        result["restart_ready_s"] = round(time.monotonic() - t1, 1) if again is not None else None
        result["after_restart_levels_unchanged"] = all_certified()
        log(f"restart: /ready 200 after {result['restart_ready_s']} s (includes up to 25 s graceful stop)")
        anon = httpx.get(f"{api}/api/v1/connectors", timeout=30)
        result["no_token_status"] = anon.status_code

        # run the checks from a scratch copy so the official evidence files are untouched
        copy = tmp / "repo"
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".venv", ".git", "evidence", "outputs", "__pycache__",
                                                                  ".pytest_cache", ".ruff_cache", ".env"))
        (copy / "evidence" / "integration").mkdir(parents=True)
        py = str(ROOT / ".venv" / "bin" / "python")
        log("integration_selfcheck.sh against the demo")
        selfcheck = subprocess.run([py, "scripts/selfcheck.py", "--api", api, "--token", token, "--out",
                                    str(copy / "evidence/integration/IS_selfcheck.txt")], cwd=copy, capture_output=True,
                                   text=True, timeout=1800)
        sc_text = (copy / "evidence/integration/IS_selfcheck.txt").read_text() if (copy / "evidence/integration/IS_selfcheck.txt").exists() \
            else selfcheck.stdout + selfcheck.stderr
        result["selfcheck_exit"] = selfcheck.returncode
        result["selfcheck_tail"] = sc_text.strip().splitlines()[-1] if sc_text.strip() else ""
        log(f"selfcheck exit {selfcheck.returncode}: {result['selfcheck_tail']}")

        log("rehearse.py (51 hidden-test cases) against the demo; /sim is the mock URL")
        t2 = time.monotonic()
        rehearse = subprocess.run([py, "scripts/rehearse.py", "--api", api, "--token", token, "--mock", f"{api}/sim"],
                                  cwd=copy, capture_output=True, text=True, timeout=3600)
        result["rehearse_exit"] = rehearse.returncode
        result["rehearse_s"] = round(time.monotonic() - t2, 1)
        summary = copy / "evidence/hard_cases/rehearsal_summary.csv"
        result["rehearse_tail"] = "\n".join((rehearse.stdout or "").strip().splitlines()[-12:])
        log(f"rehearse exit {rehearse.returncode} in {result['rehearse_s']} s")
        result["memory_after_rehearsal"] = memory(DEMO)

        log("POST /api/v1/sim/reset")
        t3 = time.monotonic()
        started = client.post("/api/v1/sim/reset")
        job_id = started.json().get("job_id")
        job: dict[str, Any] = {}
        wait_for(lambda: (job.update(client.get(f"/api/v1/jobs/{job_id}").json()) or True) and job.get("status") in ("succeeded", "failed"),
                 600, every_s=1)
        res = job.get("result") or {}
        result["reset"] = {"http_status": started.status_code, "job_status": job.get("status"), "took_s": round(time.monotonic() - t3, 1),
                           "destinations_deleted": (res.get("reset") or {}).get("destinations_deleted"),
                           "recertified": {n: r.get("state") for n, r in (res.get("certification_results") or {}).items()}}
        result["memory_final"] = memory(DEMO)

        both = subprocess.run(["docker", "logs", DEMO], capture_output=True, text=True)
        logs = both.stdout + "\n" + both.stderr
        json_lines = [line for line in logs.splitlines() if line.startswith("{")]
        leaks = [i for i, v in enumerate(secret_values) if any(v in line for line in logs.splitlines())]
        result["log"] = {"json_lines": len(json_lines), "secret_values_found": len(leaks),
                         "errors": sum('"level": "error"' in line for line in json_lines),
                         "unhandled_exception": sum("unhandled_exception" in line for line in json_lines)}
        result["finished_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

        EVID.mkdir(parents=True, exist_ok=True)
        if summary.exists():
            shutil.copy(summary, EVID / "DEMO_local_rehearsal_summary.csv")
        (EVID / "DEMO_local_rehearsal_selfcheck.txt").write_text(sc_text)
        (EVID / "DEMO_local_rehearsal_log.jsonl").write_text("\n".join(json_lines[-80:]) + "\n" if not leaks else "")
        (EVID / "DEMO_local_rehearsal.json").write_text(json.dumps(result, indent=2) + "\n")
        lines = [f"# Live-demo deploy shape, local rehearsal (scripts/demo_rehearsal.py), {result['started_at']}",
                 f"stand-ins: {json.dumps(result['shape'])}"]
        for key in ("image_size_mib", "first_boot_ready_s", "first_boot_seeded_s", "restart_ready_s",
                    "after_restart_levels_unchanged", "no_token_status", "selfcheck_exit", "selfcheck_tail", "rehearse_exit",
                    "rehearse_s"):
            lines.append(f"{key}: {result.get(key)}")
        for key in ("memory_after_seed", "memory_after_rehearsal", "memory_final", "reset", "log"):
            lines.append(f"{key}: {json.dumps(result.get(key))}")
        lines += ["rehearse.py tail:", result["rehearse_tail"]]
        (EVID / "DEMO_local_rehearsal.txt").write_text("\n".join(lines) + "\n")
        log("evidence written: evidence/integration/DEMO_local_rehearsal.{json,txt}, _summary.csv, _selfcheck.txt, _log.jsonl")
        print("\n".join(lines))
        return 0 if (result["log"]["secret_values_found"] == 0 and ready is not None and seeded is not None) else 1
    finally:
        cleanup(keep_image=args.keep)
        shutil.rmtree(tmp, ignore_errors=True)
        log("cleaned up containers, network and volumes" + ("" if args.keep else " and the image"))


if __name__ == "__main__":
    sys.exit(main())
