"""MH8 proof (clean-copy test): ``docker compose up`` starts the service plus the local Lemmy and Ghost (and the mocks)
from a fresh copy of the working tree, real posts run end to end, and the one test command passes there.

    scripts/prove_mh8.sh

1. Copy the working tree WITHOUT .venv, .git, .env, caches and results to a new temporary folder (what a reviewer clones).
2. There: ``docker compose up -d --build --wait`` (project $IBX_CLEAN_PROJECT, default ibx_clean; ports
   $IBX_CLEAN_API_PORT/$IBX_CLEAN_MOCK_PORT, default: free ports picked by the OS), timed.
3. /ready, then certify the REAL local Lemmy and Ghost through the fresh stack (publish, live, status, delete).
4. ``scripts/test.sh`` in the copy (unit + contract + integration, then the stack in simulation with the IS self-check and
   the end-to-end suite); it removes its stack with ``down -v``. No API keys are set.
5. ``docker compose down -v`` and the copy is deleted.
Evidence: evidence/must_haves/MH8_clean_copy.log (+ evidence/proof_runs/MH8.json)
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from prove_lib import ROOT, Proof


def free_port(exclude: str = "") -> str:
    """A host port nobody listens on right now (picked by the OS), so no other stack can answer for ours."""
    import socket

    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        if port != exclude:
            return port


EXCLUDE = (".venv", ".git", ".env", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis", "results",
           "node_modules", "*.pyc")


def main() -> int:
    p = Proof("MH8", "Local test instances: docker compose up on a clean copy, real posts end to end, one test command",
              "scripts/prove_mh8.sh")
    p._mode = "local (clean copy)"
    project = os.environ.get("IBX_CLEAN_PROJECT", "ibx_clean")
    api_port = os.environ.get("IBX_CLEAN_API_PORT") or free_port()
    mock_port = os.environ.get("IBX_CLEAN_MOCK_PORT") or free_port(exclude=api_port)
    tmp = Path(tempfile.mkdtemp(prefix="ibx_clean_copy_"))
    copy = tmp / "repo"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("IBX_", "COMPOSE_", "SIMULATION_MODE", "APP_ENV",
                                                                     "PUBLIC_BASE_URL", "API_TOKEN", "VIRTUAL_ENV"))}
    env.update(COMPOSE_PROJECT_NAME=project, IBX_API_PORT=api_port, IBX_MOCK_PORT=mock_port,
               IBX_BOOTSTRAP_OUT_UID="10001", IBX_BOOTSTRAP_OUT_GID="10001", PUBLIC_BASE_URL=f"http://127.0.0.1:{api_port}")
    for key in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "DEVTO_API_KEY"):
        env.pop(key, None)  # no API keys
    api = f"http://127.0.0.1:{api_port}"

    def sh(label: str, cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        result = subprocess.run(cmd, cwd=copy, env=env, capture_output=True, text=True, timeout=timeout)
        took = time.monotonic() - started
        output = (result.stdout + result.stderr).strip().splitlines()
        p.log(f"$ {' '.join(cmd)}   ({label}; exit {result.returncode}; {took:.1f} s; {len(output)} output lines, last 60:)")
        p.log("\n".join(output[-60:]))
        return result

    import socket

    def port_free(port: str) -> bool:
        with socket.socket() as sock:
            return sock.connect_ex(("127.0.0.1", int(port))) != 0

    if not (port_free(api_port) and port_free(mock_port)):
        p.check(f"host ports {api_port}/{mock_port} are free (otherwise another stack would answer)", False,
                "set IBX_CLEAN_API_PORT / IBX_CLEAN_MOCK_PORT to free ports")
        return p.finish("evidence/must_haves/MH8_clean_copy.log")
    try:
        p.section("1. clean copy of the working tree")
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(*EXCLUDE))
        files = sum(1 for _ in copy.rglob("*") if _.is_file())
        p.check("copy made without .venv/.git/.env/caches", not (copy / ".venv").exists() and not (copy / ".git").exists()
                and not (copy / ".env").exists(), f"{files} files in a fresh temporary folder")
        p.log(f"compose project {project}, host ports {api_port}/{mock_port} (checked free before start)")
        p.log(f"host: {platform.node()} {platform.system()} {platform.machine()}, {os.cpu_count()} CPUs; docker "
              + subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True).stdout.strip())

        p.section("2. docker compose up -d --build --wait")
        started = time.monotonic()
        up = sh("compose up", ["docker", "compose", "up", "-d", "--build", "--wait"], 1800)
        took = time.monotonic() - started
        p.check(f"docker compose up --wait: every service healthy ({took:.0f} s incl. image build)", up.returncode == 0, f"exit {up.returncode}")
        ps = subprocess.run(["docker", "compose", "ps", "--format", "{{.Service}} {{.Status}}"], cwd=copy, env=env,
                            capture_output=True, text=True).stdout
        p.log(ps)
        if up.returncode != 0:
            raise RuntimeError("docker compose up failed; nothing else is checked")
        p.check("lemmy, ghost, toxiproxy, bootstrap, api, worker, postgres, s3, mock-platforms running",
                all(s in ps for s in ("lemmy ", "ghost ", "toxiproxy ", "bootstrap ", "api ", "worker ", "postgres ", "s3 ", "mock-platforms ")), "")

        p.section("3. real posts end to end on the fresh stack")
        client = httpx.Client(base_url=api, headers={"Authorization": "Bearer ibx-dev-token"}, timeout=90)
        ready = client.get("/ready")
        p.check("/ready 200", ready.status_code == 200, ready.text[:300])
        for connector, url, level in (("lemmy", "http://lemmy:8536/c/ibx_test", "L4"), ("ghost", "http://ghost:2368", "L3")):
            dest = client.post("/api/v1/destinations", json={"name": f"Clean copy {connector}", "url": url, "connector_key": connector,
                                                              "automation_allowed": "true"}).json()
            job = client.post("/api/v1/certifications", json={"destination_id": dest["destination_id"]}).json()
            deadline = time.monotonic() + 180
            while client.get(f"/api/v1/jobs/{job['job_id']}").json()["status"] not in ("succeeded", "failed") and time.monotonic() < deadline:
                time.sleep(0.5)
            run = client.get(f"/api/v1/certifications/{job['run_id']}").json()
            steps = [(s["step"], s["outcome"]) for s in run.get("steps", [])]
            p.log(f"{connector}: {run.get('state')} {run.get('level_after')} live_url={(run.get('result') or {}).get('live_url')} "
                  f"delete_mode={(run.get('result') or {}).get('delete_mode')} steps={steps}")
            p.check(f"real local {connector}: certified {level} (publish, live, status, delete) on the clean copy",
                    run.get("state") == "PASSED" and run.get("level_after") == level, {"state": run.get("state")})
        client.close()

        p.section("4. scripts/test.sh (one command, all suites, no API keys)")
        started = time.monotonic()
        test = sh("scripts/test.sh", ["bash", "scripts/test.sh"], 3600)
        summary = [line for line in (test.stdout + test.stderr).splitlines() if " passed" in line or " failed" in line
                   or line.startswith(("PASS", "FAIL", "# "))]
        p.log("summary lines:\n" + "\n".join(summary[-20:]))
        p.check(f"scripts/test.sh exit 0 ({time.monotonic() - started:.0f} s)", test.returncode == 0,
                summary[-3:] if summary else f"exit {test.returncode}")
    except Exception as exc:  # noqa: BLE001 - reported as a failed check, then cleaned up
        p.check("clean-copy run completed without an exception", False, f"{type(exc).__name__}: {exc}"[:400])
    finally:
        p.section("5. teardown")
        if copy.exists():
            down = subprocess.run(["docker", "compose", "down", "-v", "--remove-orphans"], cwd=copy, env=env, capture_output=True,
                                  text=True, timeout=600)
            left = subprocess.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "-q"],
                                  capture_output=True, text=True).stdout.split()
            p.check("docker compose down -v: no containers left", down.returncode == 0 and not left, f"{len(left)} left")
        shutil.rmtree(tmp, ignore_errors=True)
    return p.finish("evidence/must_haves/MH8_clean_copy.log")


if __name__ == "__main__":
    sys.exit(main())
