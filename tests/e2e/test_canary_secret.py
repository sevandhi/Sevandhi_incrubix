"""MH7: a canary credential goes through the whole flow and appears in no response, evidence file or container log."""

from __future__ import annotations

import os
import subprocess
import uuid


def test_canary_absent_from_responses_evidence_and_logs(stack):
    canary = f"canary-SECRET-{uuid.uuid4().hex}"
    conn = stack.call("POST", "/api/v1/connections", json={"connector_key": "discourse", "ref": f"canary-{stack.run_tag}",
                                                           "credentials": {"api_key": canary, "api_username": "system"}})
    assert conn.status_code == 201 and canary not in conn.text
    dest = stack.call("POST", "/api/v1/destinations", json={
        "name": f"Canary {stack.run_tag}", "url": f"http://mock-platforms:8080/discourse/canary-{stack.run_tag}",
        "connector_key": "discourse", "frequency_cap": "1/P7D", "automation_allowed": "true",
        "connection_ref": f"canary-{stack.run_tag}"}).json()
    run = stack.certify(dest["destination_id"])
    assert run["state"] == "PASSED"
    texts = [conn.text, str(run), stack.call("GET", "/api/v1/connections").text]
    for link in run["evidence"].values():
        path = "/api/v1/files/" + link.split("/api/v1/files/", 1)[1]
        texts.append(stack.client.get(path, headers={"Authorization": ""}).text)
    project = os.environ.get("IBX_E2E_COMPOSE_PROJECT")
    if project:
        logs = subprocess.run(["docker", "compose", "-p", project, "logs", "--no-color", "api", "worker"],
                              capture_output=True, text=True, timeout=120)
        texts.append(logs.stdout + logs.stderr)
        assert logs.stdout, "no container logs captured"
    assert all(canary not in t for t in texts)
