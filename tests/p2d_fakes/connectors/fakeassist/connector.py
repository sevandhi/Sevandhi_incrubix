"""Test-only assisted connector: stores a text file and returns signed API links (as the real one should)."""

from __future__ import annotations

from app.connectors.base import BaseConnector
from app.contract.models import AssistFile, AssistPack
from app.rules.models import RuleSet
from app.storage.links import signed_download_url


class Connector(BaseConnector):
    key = "fakeassist"

    async def _build_assist_pack(self, ctx, rendition, destination):  # noqa: ANN001, ANN201
        key = f"assist/{rendition.rendition_id or 'r'}/post.md"
        await ctx.storage.put_bytes(key, f"# {rendition.title}\n\n{rendition.body}\n".encode(), "text/markdown")
        checklist = ["Post by hand: this community does not allow automated posting."]
        rules = RuleSet.model_validate(destination.rules) if destination.rules else None
        if rules and rules.frequency_cap:
            checklist.append(f"At most {rules.frequency_cap.max_posts} post(s) per {rules.frequency_cap.window}.")
        if rules and rules.self_promo != "allowed":
            checklist.append("No self-promotion.")
        if rules and rules.disclosure.required:
            checklist.append("Disclose AI-generated content.")
        return AssistPack(files=[AssistFile(name="post.md", url=signed_download_url(key, ctx.settings, now=ctx.clock.now()),
                                            mime="text/markdown")],
                          caption=rendition.title or rendition.body[:80], deep_link=self.instance_value(destination), checklist=checklist)
