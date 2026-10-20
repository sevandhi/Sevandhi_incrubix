"""MH5: the certification test post is recorded in ``test_posts`` BEFORE the platform call
(as ``marker:<marker>``), so a post whose publish timed out or whose worker died can never escape the sweeper."""

from __future__ import annotations

from sqlalchemy import select

from app.db.tables import TestPostRow


async def test_test_post_row_exists_before_the_platform_call(p2d, monkeypatch):
    cls = p2d.app.state.registry.classes["discourse"]
    original = cls._create
    seen: list[list[str]] = []

    async def spy(self, ctx, rendition, destination, connection, marker):
        async with p2d.app.state.session_factory() as session:
            seen.append(list((await session.execute(select(TestPostRow.platform_post_id))).scalars()))
        assert f"marker:{marker}" in seen[-1], "no test_posts row before the platform call"
        return await original(self, ctx, rendition, destination, connection, marker)

    monkeypatch.setattr(cls, "_create", spy)
    dest = await p2d.add(name="Write-ahead forum", url="https://write-ahead.example.org", connector_key="discourse")
    run = await p2d.certify(dest["destination_id"])
    assert run["state"] == "PASSED", [s for s in run["steps"] if s["outcome"] != "ok"]
    assert seen, "the connector was never called"
    async with p2d.app.state.session_factory() as session:
        rows = (await session.execute(select(TestPostRow))).scalars().all()
    assert len(rows) == 1  # the same row, updated with the real id after the call
    assert not rows[0].platform_post_id.startswith("marker:") and rows[0].deleted_at is not None
