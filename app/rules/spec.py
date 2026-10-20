"""Destination format checks (DEST_SPEC_VIOLATION): does the post fit the surface's limits?

    check_spec(rendition, surface_spec, overrides) -> list[Violation]   (empty list = fits)

``surface_spec`` is either a manifest surface ``{"key", "content_types", "spec": {...}}`` or a flat spec
dict. ``overrides`` is the destination's ``spec_overrides`` and wins over the manifest. Every problem
is returned (not only the first), each with its field, rule, limit and actual value.

Spec keys understood (all optional):
  title_max_chars, title_min_chars, title_required, body_max_chars, body_min_chars, body_required
  count_unit             codepoints (default) | graphemes | utf16 | bytes   (how the platform counts)
  content_types          e.g. [text, image, video_link]  (checked for the post and for each media item)
  mime_types             e.g. [image/png, image/*]        (each media item)
  media_max_count, media_max_bytes, image_max_bytes, video_max_bytes, media_max_duration_s
  tag_max, tag_pattern (regex, whole tag), tag_max_chars
  links_allowed          false = no links anywhere in the post
Unknown media size or duration is not guessed (no network here); it is checked when the caller sends it.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from app.contract.models import Rendition, Violation
from app.rules.graphemes import measure
from app.rules.links import extract_links

# Media kind from file extension, used only when the caller did not send a MIME type.
_EXT_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "heic": "image/heic",
    "svg": "image/svg+xml",
    "bmp": "image/bmp",
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "pdf": "application/pdf",
}


def effective_spec(surface_spec: dict[str, Any] | None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flatten a manifest surface and apply the destination's overrides."""
    surface_spec = dict(surface_spec or {})
    spec = dict(surface_spec.pop("spec", None) or {})
    for key, value in surface_spec.items():
        if key not in ("key", "name"):
            spec.setdefault(key, value)
    spec.update(overrides or {})
    return spec


def media_mime(media) -> str | None:
    if media.mime:
        return media.mime.lower().split(";")[0].strip()
    path = media.url.split("?")[0].split("#")[0]
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return _EXT_MIME.get(ext)


def media_kind(mime: str | None) -> str | None:
    """'image' | 'video' | 'audio' | other top-level type, or None when unknown."""
    return mime.split("/")[0] if mime else None


def _length_violations(field: str, text: str | None, spec: dict[str, Any], unit: str) -> list[Violation]:
    out: list[Violation] = []
    length = measure(text, unit)
    unit_word = "characters" if unit in ("codepoints", "graphemes") else unit
    if spec.get(f"{field}_required") and not (text or "").strip():
        out.append(
            Violation(field=field, rule=f"{field}_required", message=f"{field} is required", limit=True, actual=False)
        )
    max_len = spec.get(f"{field}_max_chars")
    if max_len is not None and length > int(max_len):
        out.append(
            Violation(
                field=field,
                rule=f"{field}_max_chars",
                limit=int(max_len),
                actual=length,
                message=f"{field} is {length} {unit_word}; the limit is {max_len}",
            )
        )
    min_len = spec.get(f"{field}_min_chars")
    if min_len and length < int(min_len):
        out.append(
            Violation(
                field=field,
                rule=f"{field}_min_chars",
                limit=int(min_len),
                actual=length,
                message=f"{field} is {length} {unit_word}; the minimum is {min_len}",
            )
        )
    return out


def check_spec(
    rendition: Rendition | dict[str, Any], surface_spec: dict[str, Any] | None, overrides: dict[str, Any] | None = None
) -> list[Violation]:
    r = rendition if isinstance(rendition, Rendition) else Rendition.model_validate(rendition)
    spec = effective_spec(surface_spec, overrides)
    unit = spec.get("count_unit", "codepoints")
    violations: list[Violation] = []

    # 1. text lengths
    violations += _length_violations("title", r.title, spec, unit)
    violations += _length_violations("body", r.body, spec, unit)

    # 2. content type of the post
    content_types = spec.get("content_types")
    if content_types and r.content_type not in content_types:
        violations.append(
            Violation(
                field="content_type",
                rule="content_types",
                limit=list(content_types),
                actual=r.content_type,
                message=f"content type '{r.content_type}' is not accepted here (accepted: {', '.join(content_types)})",
            )
        )

    # 3. media
    violations += media_violations(r, spec, content_types)

    # 4. tags
    violations += _tag_violations(r.tags, spec)

    # 5. links
    if spec.get("links_allowed") is False:
        for link in extract_links(r):
            violations.append(
                Violation(
                    field=link.field,
                    rule="links_allowed",
                    limit=False,
                    actual=link.url,
                    message=f"links are not allowed here; found {link.url}",
                )
            )
    return violations


def media_violations(r: Rendition, spec: dict[str, Any], content_types) -> list[Violation]:
    out: list[Violation] = []
    max_count = spec.get("media_max_count")
    if max_count is not None and len(r.media) > int(max_count):
        out.append(
            Violation(
                field="media",
                rule="media_max_count",
                limit=int(max_count),
                actual=len(r.media),
                message=f"{len(r.media)} media items; the limit is {max_count}",
            )
        )
    mime_types = spec.get("mime_types")
    for i, media in enumerate(r.media):
        field = f"media[{i}]"
        mime = media_mime(media)
        kind = media_kind(mime)
        if content_types or mime_types:
            if mime is None:
                out.append(
                    Violation(
                        field=field,
                        rule="mime_types",
                        limit=mime_types or content_types,
                        actual=None,
                        message=f"media type of {media.url} is unknown; send media.mime",
                    )
                )
                continue
            if content_types and kind not in content_types:
                out.append(
                    Violation(
                        field=field,
                        rule="content_types",
                        limit=list(content_types),
                        actual=mime,
                        message=f"{mime} media is not accepted here (accepted: {', '.join(content_types)})",
                    )
                )
            if mime_types and not any(fnmatch.fnmatch(mime, pattern.lower()) for pattern in mime_types):
                out.append(
                    Violation(
                        field=field,
                        rule="mime_types",
                        limit=list(mime_types),
                        actual=mime,
                        message=f"{mime} is not an accepted media type (accepted: {', '.join(mime_types)})",
                    )
                )
        size_limit = spec.get(f"{kind}_max_bytes") if kind else None
        size_limit = size_limit if size_limit is not None else spec.get("media_max_bytes")
        if size_limit is not None and media.bytes is not None and media.bytes > int(size_limit):
            out.append(
                Violation(
                    field=f"{field}.bytes",
                    rule="media_max_bytes",
                    limit=int(size_limit),
                    actual=media.bytes,
                    message=f"media is {media.bytes} bytes; the limit is {size_limit}",
                )
            )
        max_duration = spec.get("media_max_duration_s")
        if max_duration is not None and media.duration_s is not None and media.duration_s > float(max_duration):
            out.append(
                Violation(
                    field=f"{field}.duration_s",
                    rule="media_max_duration_s",
                    limit=float(max_duration),
                    actual=media.duration_s,
                    message=f"media is {media.duration_s} s long; the limit is {max_duration} s",
                )
            )
    return out


def _tag_violations(tags: list[str], spec: dict[str, Any]) -> list[Violation]:
    out: list[Violation] = []
    tag_max = spec.get("tag_max")
    if tag_max is not None and len(tags) > int(tag_max):
        out.append(
            Violation(
                field="tags",
                rule="tag_max",
                limit=int(tag_max),
                actual=len(tags),
                message=f"{len(tags)} tags; the limit is {tag_max}",
            )
        )
    pattern = spec.get("tag_pattern")
    regex = re.compile(pattern) if pattern else None
    tag_max_chars = spec.get("tag_max_chars")
    for i, tag in enumerate(tags):
        if regex and not regex.fullmatch(tag):
            out.append(
                Violation(
                    field=f"tags[{i}]",
                    rule="tag_pattern",
                    limit=pattern,
                    actual=tag,
                    message=f"tag '{tag}' does not match {pattern}",
                )
            )
        if tag_max_chars is not None and len(tag) > int(tag_max_chars):
            out.append(
                Violation(
                    field=f"tags[{i}]",
                    rule="tag_max_chars",
                    limit=int(tag_max_chars),
                    actual=len(tag),
                    message=f"tag '{tag}' is {len(tag)} characters; the limit is {tag_max_chars}",
                )
            )
    return out
