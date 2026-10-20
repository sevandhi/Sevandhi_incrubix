"""MH3: adding "Forum 41" is one catalogue row; certification then raises it to L3+ (simulation mode)."""

from __future__ import annotations


def test_forum_41_by_one_row_then_certified(stack):
    tenant = f"forum-41-{stack.run_tag}"
    row = ("name,url,platform_key,surface,connector_key,category,audience_tags,language,frequency_cap,self_promo_allowed,"
           "link_policy,disclosure_required,automation_allowed,connection_ref\n"
           f"Forum 41 {stack.run_tag},http://mock-platforms:8080/discourse/{tenant},discourse,topic,discourse,saas,"
           "\"ai;founders\",en,1/P7D,false,allow,true,true,conn-discourse-sim\n")
    created = stack.call("POST", "/api/v1/destinations", content=row.encode(), headers={"Content-Type": "text/csv"})
    assert created.status_code == 201, created.text
    dest = created.json()["results"][0]["destination"]
    assert dest["level"] == "L0" and dest["rules"]["status"] == "confirmed"
    run = stack.certify(dest["destination_id"])
    assert run["state"] == "PASSED", run
    assert run["level_after"] in ("L3", "L4")
    after = stack.call("GET", f"/api/v1/destinations/{dest['destination_id']}").json()
    assert after["level"] == run["level_after"]
    for name in ("run.json", "summary.md"):  # signed links work without the bearer token
        path = "/api/v1/files/" + run["evidence"][name].split("/api/v1/files/", 1)[1]
        assert stack.client.get(path, headers={"Authorization": ""}).status_code == 200
    stack.write_evidence("MH3_add_forum41.log")
