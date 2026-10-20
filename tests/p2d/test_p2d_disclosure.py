"""AI disclosure through /api/v1/publications: a Spanish community gets the Spanish disclosure inserted."""

from __future__ import annotations

import httpx


async def test_ai_content_gets_localised_disclosure(p2d):
    dest = (await p2d.client.post("/api/v1/destinations", json={
        "name": "Foro IA", "url": "https://foro-ia.example.org", "connector_key": "fakeforum", "language": "es",
        "frequency_cap": "3/P7D", "self_promo_allowed": False, "disclosure_required": True, "automation_allowed": True,
    })).json()
    assert (await p2d.certify(dest["destination_id"]))["state"] == "PASSED"  # certify before publishing
    accepted = await p2d.client.post("/api/v1/publications", json={"destination_id": dest["destination_id"], "content": {
        "title": "¿Cómo probáis vuestros foros?", "body": "Comparamos tres enfoques para probar publicaciones. ¿Qué os funciona?",
        "language": "es", "ai_generated": True}})
    assert accepted.json()["publication"]["state"] == "pending"
    await p2d.drain()
    pub = (await p2d.client.get(accepted.json()["publication_url"])).json()
    assert pub["state"] == "published", pub
    post = httpx.get(f"{p2d.mock}/fakeforum/foro-ia-example-org/posts/{pub['platform_post_id']}").json()
    page = httpx.get(post["url"]).text
    assert "inteligencia artificial (IA)" in page  # the Spanish text from app/rules/disclosure.yaml
    assert any("DISCLOSURE" in w for w in pub["tailoring"]["warnings"])
