"""The working copy that tailoring edits.

The body is kept in three parts so that fitting a length limit can shorten the creator's text
without cutting off what tailoring added:
    head blocks (e.g. a disclosure at the top)
    main        the creator's text (``rendition.body``), the only part that is ever truncated
    tail blocks (discussion question, link, dropped-media links, disclosure at the bottom)
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from app.contract.models import Rendition


@dataclass
class Block:
    text: str
    kind: str  # "question" | "link" | "media_link" | "disclosure"
    required: bool = False  # required blocks (disclosure) are never dropped to save space


@dataclass
class Draft:
    rendition: Rendition  # working copy; rendition.body is "main"
    lang: str | None = None  # content language (declared or detected)
    instance_host: str | None = None  # the destination's host, e.g. for Lemmy @user@instance mentions
    head: list[Block] = field(default_factory=list)
    tail: list[Block] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def warn(self, code: str, message: str) -> None:
        line = f"{code}: {message}"
        if line not in self.warnings:
            self.warnings.append(line)

    def body(self, main: str | None = None) -> str:
        """The full body: head blocks, main text, tail blocks, as separate paragraphs."""
        main = self.rendition.body if main is None else main
        if self.rendition.body_format == "html":
            parts = [f"<p>{html.escape(b.text)}</p>" for b in self.head] + ([main] if main.strip() else [])
            parts += [f"<p>{html.escape(b.text)}</p>" for b in self.tail]
            return "\n".join(parts)
        parts = [b.text for b in self.head] + ([main.strip()] if main.strip() else []) + [b.text for b in self.tail]
        return "\n\n".join(parts)

    def optional_blocks(self) -> list[Block]:
        return [b for b in self.head + self.tail if not b.required]

    def drop(self, block: Block) -> None:
        if block in self.tail:
            self.tail.remove(block)
        else:
            self.head.remove(block)

    def build(self) -> Rendition:
        return self.rendition.model_copy(update={"body": self.body()})
