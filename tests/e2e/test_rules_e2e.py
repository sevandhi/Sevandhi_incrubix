"""MH4 on the stack: second post inside a weekly cap (also two at once) → rescheduled with retry_at;
a promotional post to a no-promotion destination → blocked with the reason."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor


def _dest(stack, name: str, *, certify: bool = True, **rules: str) -> dict:
    """One catalogue row; certified first (no publication before the destination is certified)."""
    row = {"name": f"{name} {stack.run_tag}", "url": f"http://mock-platforms:8080/discourse/{name}-{stack.run_tag}",
           "connector_key": "discourse", "language": "en", "automation_allowed": "true", **rules}
    response = stack.call("POST", "/api/v1/destinations", json=row)
    assert response.status_code == 201, response.text
    dest = response.json()
    if certify:
        assert stack.certify(dest["destination_id"])["state"] == "PASSED"
    return dest


def _post(stack, dest_id: str, expect: int = 202, **content: object) -> dict:
    body = {"title": "How do you certify long-tail forums?", "body": "We compared three ways to test forum posting "
            "end to end. Which one has worked for you?", "language": "en", "ai_generated": False, **content}
    response = stack.call("POST", "/api/v1/publications", json={"destination_id": dest_id, "content": body})
    assert response.status_code == expect, response.text
    return response.json()


def test_second_post_inside_weekly_cap_is_rescheduled(stack):
    dest = _dest(stack, "weekly-cap", frequency_cap="1/P7D")
    first = _post(stack, dest["destination_id"])
    assert stack.job(first["job_id"])["status"] == "succeeded"
    published = stack.call("GET", first["publication_url"]).json()
    assert published["state"] == "published" and published["live_url"]
    second = _post(stack, dest["destination_id"], title="A second question in the same week?")
    pub = second["publication"]
    assert pub["state"] == "rescheduled" and pub["code"] == "DEST_RULE_FREQUENCY_CAP" and pub["retry_at"]
    stack.write_evidence("MH4_weekly_cap_second_post.log")


def test_two_concurrent_posts_inside_cap(stack):
    dest = _dest(stack, "concurrent-cap", frequency_cap="1/P7D")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda t: _post(stack, dest["destination_id"], title=t),
                                ["Concurrent question A here?", "Concurrent question B here?"]))
    states = sorted(r["publication"]["state"] for r in results)
    assert states == ["pending", "rescheduled"], states
    stack.write_evidence("MH4_weekly_cap_concurrent.log")


def test_promo_to_no_promo_is_blocked(stack):
    dest = _dest(stack, "no-promo", certify=False, self_promo_allowed="false")  # the rule is named even before certification
    refused = _post(stack, dest["destination_id"], expect=409, title="Our tool launched today",
                    body="Sign up now with code LAUNCH20: https://mytool.example.com/?utm_source=forum", is_promotional=True,
                    owner_domains=["mytool.example.com"])
    error = refused["error"]  # a rules block is a synchronous 409 with the rule's envelope
    assert error["code"] == "DEST_RULE_SELF_PROMO" and error["retryable"] is False and error["message"]
    pub = stack.call("GET", f"/api/v1/publications/{error['details']['publication_id']}").json()
    assert pub["state"] == "blocked" and pub["code"] == "DEST_RULE_SELF_PROMO" and pub["reason"]
    stack.write_evidence("MH4_promo_to_no_promo.log")


def test_publication_to_an_uncertified_destination_is_refused(stack):
    dest = _dest(stack, "uncertified", certify=False, frequency_cap="1/P7D")
    error = _post(stack, dest["destination_id"], expect=409)["error"]
    assert error["code"] == "DESTINATION_NOT_CERTIFIED" and error["retryable"] is False
    stack.write_evidence("MH4_uncertified_destination_refused.log")
