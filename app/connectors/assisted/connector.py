"""Assisted connector: a person posts, IncuBrix prepares everything (plan §3.2, §6 "assisted certification").

``build_assist_pack(rendition, destination)`` writes, under the object-storage prefix ``assist/<marker>/``:
    media/NN-<name>   every media item (copied from our storage or downloaded, size-capped)
    title.txt         the title, if any
    caption.txt       the text to paste (body + link + hashtags)
    checklist.md      the steps and every confirmed posting rule in plain words
    pack.json         an index of the above (keys, sizes, deep link) used by purge_pack()
and returns signed download links (``/api/v1/files/...``, served through the API port), the caption, a
``deep_link`` (the destination's submit page, with the post prefilled where that site supports it)
and ``checklist[]``.

The marker is derived from the rendition + destination, so building the same pack twice writes the same
keys (naturally idempotent). The publication stays ``awaiting_human`` until the person reports the live URL
(``POST /api/v1/assist-tasks/{id}/complete``, owned by the publications API). ``purge_pack(marker)``
deletes the objects; certification uses it to prove the links stop working.
"""

from __future__ import annotations

import json
import mimetypes
import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from app.connectors.base import BaseConnector, ConnectorContext, note
from app.contract.models import AssistFile, AssistPack, Connection, Destination, Media, Rendition
from app.errors import ErrorCode, IbxError
from app.reliability.idempotency import marker_for, request_hash
from app.reliability.safe_fetch import read_media_url
from app.storage.links import signed_download_url

PREFIX = "assist"
DEEP_LINK_TEXT_MAX = 1500  # keep prefilled URLs well under common 2 KB URL limits
COMPLETE_HINT = "POST /api/v1/assist-tasks/{task_id}/complete with the live_url"

# Sites whose submit page accepts a prefilled post: (host suffix or None, path suffix, our field -> query param).
PREFILL_PATTERNS: list[tuple[str | None, str, dict[str, str]]] = [
    ("reddit.com", "/submit", {"title": "title", "url": "url", "body": "text"}),
    ("news.ycombinator.com", "/submitlink", {"title": "t", "url": "u"}),
    ("dev.to", "/new", {"devto_prefill": "prefill"}),
    (None, "/create_post", {"title": "title", "url": "url", "body": "body"}),  # lemmy-ui
    (None, "/new-topic", {"title": "title", "body": "body"}),  # Discourse
    (None, "/submit", {"title": "title", "url": "url", "body": "text"}),  # generic submit forms (and the mock)
]


def pack_marker(rendition: Rendition, destination_key: str) -> str:
    """Deterministic marker (= storage prefix) for one rendition on one destination."""
    return marker_for("assist:" + request_hash(rendition.model_dump(mode="json"), destination_key, "assist"))


def caption_text(rendition: Rendition) -> str:
    body = (rendition.body or "").strip()
    link = rendition.link_url or rendition.canonical_url
    if link and link not in body:
        body = f"{body}\n\n{link}" if body else link
    if rendition.tags:
        tags = " ".join("#" + re.sub(r"\s+", "", t.lstrip("#")) for t in rendition.tags)
        if tags not in body:
            body = f"{body}\n\n{tags}"
    return body


def _human_window(window: timedelta) -> str:
    seconds = int(window.total_seconds())
    for unit, size in (("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds % size == 0 and seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return f"{seconds} seconds"


def rules_in_plain_words(ruleset: Any) -> list[str]:
    """Every confirmed rule of a ``RuleSet`` as one plain sentence (nothing is left out)."""
    items: list[str] = []
    cap = ruleset.frequency_cap
    if cap is not None:
        items.append(f"Rule: post at most {cap.max_posts} time{'s' if cap.max_posts != 1 else ''} every "
                     f"{_human_window(cap.window_td)} in this community.")
    else:
        items.append("Rule: no posting-frequency limit is stated for this community.")
    if ruleset.self_promo == "disallowed":
        items.append("Rule: no self-promotion. Do not add links to your own products, channels or shops, and no calls to action.")
    elif ruleset.promo_ratio:
        items.append(f"Rule: at most 1 in {ruleset.promo_ratio} of your posts here may be promotional.")
    else:
        items.append("Rule: self-promotion is allowed.")
    policy = ruleset.link_policy
    if policy == "none":
        items.append("Rule: do not include any links.")
    elif policy == "no_shorteners":
        items.append("Rule: no link shorteners (bit.ly, t.co, ...); use full URLs.")
    elif policy == "allowlist":
        items.append(f"Rule: only link to these sites: {', '.join(ruleset.link_allowlist) or '(none listed)'}.")
    else:
        items.append("Rule: links are allowed.")
    if ruleset.disclosure.required:
        items.append(f"Rule: AI-assisted content must be disclosed; keep the disclosure at the {ruleset.disclosure.placement} of the post.")
    else:
        items.append("Rule: no AI-content disclosure is required.")
    policy_lang = getattr(ruleset, "effective_language_policy", None) or ruleset.language_policy
    if policy_lang == "must_match":
        langs = ", ".join(ruleset.languages) or "the community's language"
        items.append(f"Rule: write the post in the community's language: {langs}.")
    elif ruleset.languages:
        items.append(f"Rule: the community's language is {', '.join(ruleset.languages)}; other languages are accepted.")
    else:
        items.append("Rule: any language is accepted.")
    if ruleset.automation_allowed:
        items.append("Rule: automated posting is allowed here, but this pack is meant to be posted by a person.")
    else:
        items.append("Rule: automated posting is not allowed here; a person must post this by hand.")
    if ruleset.blocked_terms:
        items.append(f"Rule: do not use these words or phrases: {', '.join(ruleset.blocked_terms)}.")
    for key, value in (ruleset.model_extra or {}).items():  # rules this version does not model are still listed
        items.append(f"Rule: {key.replace('_', ' ')}: {value}.")
    if ruleset.rules_url:
        items.append(f"Rule: read the community rules before posting: {ruleset.rules_url}")
    return items


def deep_link(submit_url: str, rendition: Rendition, caption: str, prefill: Any = None) -> tuple[str, bool]:
    """The submit URL with prefilled query parameters where supported. Returns (url, truncated)."""
    parts = urlsplit(submit_url)
    mapping: dict[str, str] | None
    if prefill is False:
        mapping = None
    elif isinstance(prefill, dict):
        mapping = {str(k): str(v) for k, v in prefill.items()}
    else:
        host = (parts.hostname or "").lower()
        path = parts.path.rstrip("/")
        mapping = next(
            (m for suffix, path_end, m in PREFILL_PATTERNS if (suffix is None or host == suffix or host.endswith("." + suffix)) and path.endswith(path_end)),
            None,
        )
    if not mapping:
        return submit_url, False
    truncated = len(caption) > DEEP_LINK_TEXT_MAX
    body = caption[:DEEP_LINK_TEXT_MAX]
    values = {
        "title": rendition.title or "",
        "body": body,
        "url": rendition.link_url or rendition.canonical_url or "",
        "tags": ",".join(rendition.tags),
        "devto_prefill": "---\ntitle: {}\npublished: false\ntags: {}\n---\n\n{}".format(
            rendition.title or "", ", ".join(rendition.tags[:4]), body
        ),
    }
    params = {param: values[field] for field, param in mapping.items() if values.get(field)}
    if not params:
        return submit_url, False
    query = f"{parts.query}&{urlencode(params)}" if parts.query else urlencode(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment)), truncated


def _safe_name(media: Media, index: int) -> str:
    stem = urlsplit(media.url).path.rsplit("/", 1)[-1] or f"media-{index}"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")[:80] or f"media-{index}"
    if "." not in stem and media.mime:
        stem += mimetypes.guess_extension(media.mime) or ""
    return f"{index:02d}-{stem}"


class Connector(BaseConnector):
    key = "assisted"
    mock_route = "assist"

    def simulate(self, destination: Destination, connection: Connection) -> tuple[Destination, Connection]:
        sim_dest, sim_conn = super().simulate(destination, connection)
        if sim_dest is destination:
            return sim_dest, sim_conn
        # the mock serves each community's submit form at /assist/<tenant>/submit: keep a working deep link
        param = self.manifest.instance_param
        return sim_dest.model_copy(update={"instance": {**sim_dest.instance, param: sim_dest.instance[param] + "/submit"}}), sim_conn

    async def _build_assist_pack(self, ctx: ConnectorContext, rendition: Rendition, destination: Destination) -> AssistPack:
        storage = ctx.storage
        if storage is None:
            raise IbxError(ErrorCode.STORAGE_UNAVAILABLE, "assisted: object storage is not configured.")
        dest_key = self.destination_key(destination)
        if ctx.simulation:
            destination, _ = self.simulate(destination, Connection(connector_key=self.key, auth_type="none"))
        submit_url = self.instance_value(destination)
        marker = pack_marker(rendition, dest_key)
        prefix = f"{PREFIX}/{marker}/"
        note(marker=marker, storage_prefix=prefix)

        stored: list[dict[str, Any]] = []
        for index, media in enumerate(rendition.media, start=1):
            data, mime = await self._fetch_media(ctx, media)
            name = _safe_name(media, index)
            key = await storage.put_bytes(f"{prefix}media/{name}", data, mime)
            stored.append({"name": name, "key": key, "mime": mime, "bytes": len(data)})

        caption = caption_text(rendition)
        link, truncated = deep_link(submit_url, rendition, caption, destination.instance.get("prefill"))
        checklist = await self._checklist(rendition, destination, dest_key, submit_url, link, truncated, stored)

        if rendition.title:
            stored.append(await self._put_text(storage, f"{prefix}title.txt", "title.txt", rendition.title))
        stored.append(await self._put_text(storage, f"{prefix}caption.txt", "caption.txt", caption))
        stored.append(await self._put_text(storage, f"{prefix}checklist.md", "checklist.md",
                                           "# Posting checklist\n\n" + "\n".join(f"- [ ] {item}" for item in checklist) + "\n"))
        index_doc = {"marker": marker, "destination_key": dest_key, "submit_url": submit_url, "deep_link": link,
                     "files": stored, "checklist": checklist, "created_at": self.now().isoformat()}
        pack_key = f"{prefix}pack.json"
        await storage.put_bytes(pack_key, json.dumps(index_doc, indent=2).encode(), "application/json")

        expiry = ctx.settings.FILE_LINK_EXPIRY_S
        files = [AssistFile(name=f["name"], url=signed_download_url(f["key"], ctx.settings, expiry, self.now()), mime=f["mime"])
                 for f in stored]
        return AssistPack(files=files, caption=caption, deep_link=link, checklist=checklist)

    async def purge_pack(self, marker: str) -> list[str]:
        """Delete every object of one pack (certification proves the links stop working afterwards)."""
        storage = self.ctx.storage
        prefix = f"{PREFIX}/{marker}/"
        try:
            index = json.loads(await storage.get_bytes(f"{prefix}pack.json"))
        except IbxError:
            return []
        keys = [f["key"] for f in index.get("files", [])] + [f"{prefix}pack.json"]
        for key in keys:
            await storage.delete(key)
        return keys

    # ------------------------------------------------------------ internals

    @staticmethod
    async def _put_text(storage: Any, key: str, name: str, text: str) -> dict[str, Any]:
        data = text.encode()
        await storage.put_bytes(key, data, "text/plain; charset=utf-8" if not name.endswith(".md") else "text/markdown; charset=utf-8")
        return {"name": name, "key": key, "mime": "text/markdown" if name.endswith(".md") else "text/plain", "bytes": len(data)}

    async def _fetch_media(self, ctx: ConnectorContext, media: Media) -> tuple[bytes, str]:
        """Our own signed upload link (verified, read from storage) or a public URL, via the one SSRF guard."""
        fetched = await read_media_url(media.url, ctx.settings, ctx.storage, max_bytes=ctx.settings.MEDIA_MAX_BYTES,
                                       allowed_mime=ctx.settings.MEDIA_ALLOWED_MIME, client=ctx.http, now=self.now())
        return fetched.data, fetched.mime or "application/octet-stream"

    async def _checklist(
        self,
        rendition: Rendition,
        destination: Destination,
        dest_key: str,
        submit_url: str,
        link: str,
        truncated: bool,
        stored: list[dict[str, Any]],
    ) -> list[str]:
        items = [f"Open the submit page: {link}" + (" (the post is prefilled)" if link != submit_url else "")]
        if rendition.title:
            items.append(f"Use the title from title.txt ({len(rendition.title)} characters).")
        items.append("Paste the text from caption.txt" + (" (the prefilled text was shortened; paste the full caption)." if truncated else "."))
        items += [f"Attach the file {f['name']}." for f in stored]
        items += await self._rule_items(rendition, destination, dest_key)
        items.append(f"After posting, copy the post's public URL and report it to IncuBrix ({COMPLETE_HINT}).")
        items.append(f"The download links expire after {max(1, self.ctx.settings.FILE_LINK_EXPIRY_S // 60)} minutes.")
        return items

    async def _rule_items(self, rendition: Rendition, destination: Destination, dest_key: str) -> list[str]:
        from app.rules.engine import evaluate

        factory = self.ctx.db_session_factory
        if factory is None:
            ruleset, history = (await self.resolve_rules(None, destination, dest_key))[0], []
        else:
            async with factory() as session:
                ruleset = (await self.resolve_rules(session, destination, dest_key))[0]
                history = await self._history(session, dest_key) if ruleset is not None else []
        if ruleset is None:
            return ["Rules not confirmed: nobody has confirmed this community's posting rules yet. Read its rules page before posting."]
        items = rules_in_plain_words(ruleset)
        owner_domains = tuple((rendition.model_extra or {}).get("owner_domains") or ())
        decision = evaluate(rendition, ruleset, history, self.now(), owner_domains=owner_domains)
        for check in decision.checks:
            if check.outcome == "pass" or check.code == ErrorCode.DEST_RULE_AUTOMATION_NOT_ALLOWED:
                continue  # a person posts an assisted pack, so the automation rule is satisfied
            reason = check.reason or (check.code.value if check.code else check.check)
            items.append(f"Check before posting: {reason}")
        return items
