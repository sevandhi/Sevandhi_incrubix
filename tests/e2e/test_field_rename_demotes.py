"""Hidden-test class: the platform renames a response field → certification FAILED → CLEANUP → DEMOTED."""

from __future__ import annotations


def test_field_rename_fails_certification_and_demotes(stack):
    tenant = f"rename-{stack.run_tag}"
    dest = stack.call("POST", "/api/v1/destinations", json={
        "name": f"Rename {stack.run_tag}", "url": f"http://mock-platforms:8080/discourse/{tenant}", "connector_key": "discourse",
        "frequency_cap": "1/P7D", "automation_allowed": "true"}).json()
    assert stack.certify(dest["destination_id"])["state"] == "PASSED"
    fault = stack.call("POST", "/api/v1/sim/faults", json={"platform": "discourse", "tenant": tenant, "method": "POST",
                                                           "fault": "rename_field:topic_id->topic_ident", "count": 1})
    assert fault.status_code == 201, fault.text
    run = stack.certify(dest["destination_id"])
    assert run["state"] == "DEMOTED" and run["result"]["failed_step"] == "PUBLISH"
    publish = next(s for s in run["steps"] if s["step"] == "PUBLISH")
    assert publish["detail"]["error"]["details"]["reason"] == "response_schema_mismatch"
    cleanup = next(s for s in run["steps"] if s["step"] == "CLEANUP")
    assert cleanup["outcome"] == "ok", cleanup
    after = stack.call("GET", f"/api/v1/destinations/{dest['destination_id']}").json()
    assert after["level"] == "L0" and after["status"] == "excluded"
    stack.write_evidence("HT_field_rename_demotes.log")
