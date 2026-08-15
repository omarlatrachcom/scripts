#!/usr/bin/env python3
"""GUI and reusable extraction core for articles and optional public comments.

Every supported source has an explicit ``ArticleAdapter`` and
``WebsiteExtractorDefinition``. The GUI shell, chunking, and output code are
shared without guessing an extractor from a URL.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import queue
import re
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterable, Protocol


APP_NAME = "Article & Comments Extractor"
TOKEN_LIMIT = 12_500
MAX_FILE_TOKENS = TOKEN_LIMIT - 1  # The requirement says files must be under 12,500.
PROMPT_DRAFT_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "ArticleExtractor"
    / "website_prompt_draft.json"
)
COMMENT_OPTIONS = (
    "No comments",
    "Comments only",
    "Comments and nested replies",
)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)


class ExtractionError(RuntimeError):
    """An extraction failure that is suitable for showing to the user."""


class OversizedBlockError(ExtractionError):
    """A source paragraph cannot fit without violating paragraph boundaries."""


class WebsiteRemovalError(ExtractionError):
    """A website's managed source sections could not be removed safely."""


@dataclass
class MediaReference:
    placeholder: str
    kind: str
    source: str = ""
    alt_text: str = ""
    location: str = "article body"
    downloaded_file: str = ""
    download_error: str = ""


@dataclass
class Comment:
    identifier: str
    author: str
    published: str
    blocks: list[str]
    parent_identifier: str = ""
    media: list[MediaReference] = field(default_factory=list)


@dataclass
class ExtractedArticle:
    title: str
    subtitle: str
    author: str
    published: str
    canonical_url: str
    slug: str
    blocks: list[str]
    comments: list[Comment] = field(default_factory=list)
    media: list[MediaReference] = field(default_factory=list)
    adapter_name: str = "Generic article"


def _class_tokens(attrs: dict[str, str | None]) -> set[str]:
    return set((attrs.get("class") or "").split())


def _clean_inline(text: str) -> str:
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _clean_block(text: str) -> str:
    lines = [_clean_inline(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


class StructuredTextParser(HTMLParser):
    """Turn an HTML fragment into paragraph-like blocks and media markers."""

    BLOCK_TAGS = {
        "address", "aside", "dd", "details", "div", "dl", "dt", "figcaption",
        "footer", "header", "main", "p", "section", "summary",
    }
    VISUAL_TAGS = {"audio", "canvas", "iframe", "math", "svg", "table", "video"}
    EMBED_CLASS_PARTS = {
        "content-in-middle", "embedded-post-wrap", "poll-container",
        "subscription-widget-wrap", "tweet", "youtube-wrap", "native-video-embed",
    }
    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "source", "track", "wbr",
    }

    def __init__(self, first_media_number: int = 1) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.media: list[MediaReference] = []
        self._buffer: list[str] = []
        self._headings: list[int] = []
        self._list_stack: list[dict[str, int | str]] = []
        self._li_stack: list[dict[str, str | bool]] = []
        self._blockquote_depth = 0
        self._pre_depth = 0
        self._skip_depth = 0
        self._ignore_depth = 0
        self._figure_media: list[bool] = []
        self._next_media = first_media_number

    def _flush(self, prefix: str = "") -> None:
        text = _clean_block("".join(self._buffer))
        self._buffer.clear()
        if not text:
            return
        if prefix:
            text = prefix + text
        if self._blockquote_depth:
            text = "\n".join("> " + line for line in text.splitlines())
        if not self.blocks or self.blocks[-1] != text:
            self.blocks.append(text)

    def _add_media(self, kind: str, attrs: dict[str, str | None]) -> None:
        if self._figure_media and self._figure_media[-1]:
            return
        self._flush()
        marker = f"IMAGE-{self._next_media:02d}"
        self._next_media += 1
        source = attrs.get("src") or attrs.get("href") or ""
        alt_text = attrs.get("alt") or attrs.get("title") or ""
        self.media.append(
            MediaReference(marker, kind, source, _clean_inline(alt_text))
        )
        self.blocks.append(marker)
        if self._figure_media:
            self._figure_media[-1] = True

    def _flush_list_item(self) -> None:
        if not self._li_stack:
            self._flush()
            return
        item = self._li_stack[-1]
        prefix = str(item["prefix"]) if not item["emitted"] else "  " * len(self._li_stack)
        before = len(self.blocks)
        self._flush(prefix)
        if len(self.blocks) > before:
            item["emitted"] = True

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs = dict(attrs_list)
        if self._ignore_depth:
            if tag not in self.VOID_TAGS:
                self._ignore_depth += 1
            return
        if tag in {"script", "style", "noscript", "template"}:
            self._ignore_depth = 1
            return
        if self._skip_depth:
            if tag not in self.VOID_TAGS:
                self._skip_depth += 1
            return

        classes = _class_tokens(attrs)
        if tag == "div" and any(
            any(part in class_name for part in self.EMBED_CLASS_PARTS)
            for class_name in classes
        ):
            self._add_media("embedded content", attrs)
            self._skip_depth = 1
            return
        if tag in self.VISUAL_TAGS:
            self._add_media(tag, attrs)
            self._skip_depth = 1
            return
        if tag == "img":
            self._add_media("image", attrs)
            return
        if tag == "figure":
            self._flush()
            self._figure_media.append(False)
            return
        if tag in self.BLOCK_TAGS and not self._li_stack:
            self._flush()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            self._headings.append(int(tag[1]))
        elif tag in {"ul", "ol"}:
            if self._li_stack:
                self._flush_list_item()
            else:
                self._flush()
            self._list_stack.append({"tag": tag, "number": 0})
        elif tag == "li":
            self._flush()
            if self._list_stack and self._list_stack[-1]["tag"] == "ol":
                self._list_stack[-1]["number"] = int(self._list_stack[-1]["number"]) + 1
            if self._list_stack:
                current = self._list_stack[-1]
                bullet = f"{current['number']}. " if current["tag"] == "ol" else "- "
            else:
                bullet = "- "
            self._li_stack.append(
                {"prefix": "  " * max(0, len(self._list_stack) - 1) + bullet, "emitted": False}
            )
        elif tag == "blockquote":
            self._flush()
            self._blockquote_depth += 1
        elif tag == "pre":
            self._flush()
            self._pre_depth += 1
        elif tag == "br":
            self._buffer.append("\n")
        elif tag == "hr":
            self._flush()
            self.blocks.append("---")

    def handle_startendtag(
        self, tag: str, attrs_list: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs_list)
        if tag.lower() not in {"img", "br", "hr", "source", "meta", "link", "input"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignore_depth:
            self._ignore_depth -= 1
            return
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "figure":
            self._flush()
            if self._figure_media:
                self._figure_media.pop()
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = self._headings.pop() if self._headings else int(tag[1])
            self._flush("#" * level + " ")
        elif tag == "li":
            self._flush_list_item()
            if self._li_stack:
                self._li_stack.pop()
        elif tag in {"ul", "ol"}:
            self._flush()
            if self._list_stack:
                self._list_stack.pop()
        elif tag == "blockquote":
            self._flush()
            self._blockquote_depth = max(0, self._blockquote_depth - 1)
        elif tag == "pre":
            self._flush()
            self._pre_depth = max(0, self._pre_depth - 1)
        elif tag in self.BLOCK_TAGS:
            if self._li_stack:
                self._flush_list_item()
            else:
                self._flush()

    def handle_data(self, data: str) -> None:
        if self._ignore_depth or self._skip_depth:
            return
        if self._pre_depth:
            self._buffer.append(data)
            return
        if not data:
            return
        if self._buffer and not self._buffer[-1].endswith((" ", "\n")) and not data[0].isspace():
            # HTML inline tags often split words from surrounding punctuation; do not
            # inject a space before punctuation.
            if data[0] not in ".,;:!?)]}%":
                self._buffer.append(" ")
        self._buffer.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def html_to_blocks(
    fragment: str, first_media_number: int = 1
) -> tuple[list[str], list[MediaReference]]:
    parser = StructuredTextParser(first_media_number)
    parser.feed(fragment)
    parser.close()
    return parser.blocks, parser.media


# BEGIN WEBSITE CODE: ana-toledo-support
@dataclass
class _CommentBuilder:
    author: str
    start_depth: int
    parent: "_CommentBuilder | None"
    identifier: str = ""
    published: str = ""
    body_parts: list[str] = field(default_factory=list)
    body_depth: int = 0


class SubstackCommentsParser(HTMLParser):
    """Extract server-rendered comments, including nested public replies."""

    COMMENT_ID_RE = re.compile(r"/comment/(\d+)(?:[/?#]|$)")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self._active: list[_CommentBuilder] = []
        self._hierarchy: list[_CommentBuilder] = []
        self._ordered: list[_CommentBuilder] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        self.depth += 1
        attrs = dict(attrs_list)
        label = attrs.get("aria-label") or ""
        if tag == "div" and attrs.get("role") == "article" and label.startswith("Comment by "):
            while self._hierarchy and self._hierarchy[-1].start_depth >= self.depth:
                self._hierarchy.pop()
            builder = _CommentBuilder(
                author=label.removeprefix("Comment by ").strip() or "Unknown",
                start_depth=self.depth,
                parent=self._hierarchy[-1] if self._hierarchy else None,
            )
            self._active.append(builder)
            self._hierarchy.append(builder)
            self._ordered.append(builder)

        if not self._active:
            return
        current = self._active[-1]
        href = attrs.get("href") or ""
        match = self.COMMENT_ID_RE.search(href)
        if match and not current.identifier:
            current.identifier = match.group(1)
        if match and attrs.get("title") and not current.published:
            current.published = attrs["title"] or ""

        if tag == "div" and "comment-body" in _class_tokens(attrs):
            current.body_depth = self.depth
            return
        if current.body_depth and self.depth > current.body_depth:
            raw = self.get_starttag_text()
            if raw:
                current.body_parts.append(raw)

    def handle_startendtag(
        self, tag: str, attrs_list: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs_list)
        if self._active and self._active[-1].body_depth:
            self._active[-1].body_parts.append(f"</{tag}>")
        self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self._active and self._active[-1].body_depth:
            self._active[-1].body_parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._active and self._active[-1].body_depth:
            self._active[-1].body_parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._active and self._active[-1].body_depth:
            self._active[-1].body_parts.append(f"&#{name};")

    def handle_endtag(self, tag: str) -> None:
        if self._active:
            current = self._active[-1]
            if current.body_depth:
                if self.depth == current.body_depth and tag == "div":
                    current.body_depth = 0
                elif self.depth > current.body_depth:
                    current.body_parts.append(f"</{tag}>")
            if self.depth == current.start_depth and tag == "div":
                self._active.pop()
        self.depth = max(0, self.depth - 1)

    def comments(self) -> list[Comment]:
        results: list[Comment] = []
        seen: set[str] = set()
        for position, builder in enumerate(self._ordered, start=1):
            blocks, media = html_to_blocks("".join(builder.body_parts))
            if not blocks:
                continue
            identifier = builder.identifier or f"position-{position}"
            if identifier in seen:
                continue
            seen.add(identifier)
            results.append(
                Comment(
                    identifier=identifier,
                    author=builder.author,
                    published=builder.published,
                    blocks=blocks,
                    parent_identifier=(
                        builder.parent.identifier if builder.parent else ""
                    ),
                    media=media,
                )
            )
        return results


class SubstackRichTextConverter:
    """Convert Substack's ProseMirror comment JSON to ordered text blocks."""

    VISUAL_TYPES = {
        "audio", "canvas", "captioned_image", "captionedImage", "embed",
        "gallery", "iframe", "image", "image2", "math", "poll", "table",
        "video", "youtube",
    }

    def __init__(self) -> None:
        self.media: list[MediaReference] = []
        self._next_media = 1

    def _media_block(self, node: dict) -> str:
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        marker = f"IMAGE-{self._next_media:02d}"
        self._next_media += 1
        self.media.append(
            MediaReference(
                marker,
                str(node.get("type") or "embedded content"),
                str(attrs.get("src") or attrs.get("url") or attrs.get("href") or ""),
                _clean_inline(str(attrs.get("alt") or attrs.get("title") or "")),
                "comment body",
            )
        )
        return marker

    def _inline(self, nodes: object) -> list[str]:
        parts: list[str] = []
        if not isinstance(nodes, list):
            return parts
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = str(node.get("type") or "")
            if kind == "text":
                parts.append(str(node.get("text") or ""))
            elif kind in {"hard_break", "hardBreak"}:
                parts.append("\n")
            elif kind in self.VISUAL_TYPES:
                parts.append("\n" + self._media_block(node) + "\n")
            elif isinstance(node.get("content"), list):
                parts.extend(self._inline(node["content"]))
        return parts

    def _nodes(self, nodes: object) -> list[str]:
        blocks: list[str] = []
        if not isinstance(nodes, list):
            return blocks
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = str(node.get("type") or "")
            content = node.get("content")
            if kind in self.VISUAL_TYPES:
                blocks.append(self._media_block(node))
            elif kind in {"paragraph", "code_block", "codeBlock"}:
                text = _clean_block("".join(self._inline(content)))
                if text:
                    # Inline media markers are promoted to their own blocks.
                    blocks.extend(part for part in re.split(r"\n+(IMAGE-\d+)\n+", text) if part)
            elif kind == "heading":
                attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
                level = max(1, min(6, int(attrs.get("level") or 2)))
                text = _clean_block("".join(self._inline(content)))
                if text:
                    blocks.append("#" * level + " " + text)
            elif kind in {"bullet_list", "bulletList", "ordered_list", "orderedList"}:
                ordered = kind in {"ordered_list", "orderedList"}
                for index, item in enumerate(content if isinstance(content, list) else [], start=1):
                    item_blocks = self._nodes(item.get("content") if isinstance(item, dict) else [])
                    if not item_blocks:
                        continue
                    prefix = f"{index}. " if ordered else "- "
                    blocks.append(prefix + item_blocks[0])
                    blocks.extend("  " + block for block in item_blocks[1:])
            elif kind in {"list_item", "listItem", "doc"}:
                blocks.extend(self._nodes(content))
            elif kind == "blockquote":
                blocks.extend(
                    "\n".join("> " + line for line in block.splitlines())
                    for block in self._nodes(content)
                )
            elif kind in {"horizontal_rule", "horizontalRule"}:
                blocks.append("---")
            elif isinstance(content, list):
                blocks.extend(self._nodes(content))
        return blocks

    def convert(self, document: object) -> tuple[list[str], list[MediaReference]]:
        if not isinstance(document, dict):
            return [], []
        blocks = self._nodes(
            document.get("content") if document.get("type") == "doc" else [document]
        )
        return blocks, self.media
# END WEBSITE CODE: ana-toledo-support


class GenericPageParser(HTMLParser):
    """Capture metadata and the first article/main element for a fallback adapter."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.author = ""
        self.published = ""
        self.canonical = ""
        self._capture_tag = ""
        self._capture_depth = 0
        self._fragment: list[str] = []
        self._h1_depth = 0
        self._h1_parts: list[str] = []
        self._capture_done = False

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        prop = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content") or ""
        if tag == "meta":
            if prop in {"og:title", "twitter:title"} and not self.title:
                self.title = content
            elif prop in {"author", "article:author"} and not self.author:
                self.author = content
            elif prop in {"article:published_time", "date", "datepublished"} and not self.published:
                self.published = content
        elif tag == "link" and (attrs.get("rel") or "") == "canonical":
            self.canonical = attrs.get("href") or ""

        if not self._capture_done and not self._capture_tag and tag in {"article", "main"}:
            self._capture_tag = tag
            self._capture_depth = 1
            return
        if self._capture_tag:
            self._capture_depth += 1
            raw = self.get_starttag_text()
            if raw:
                self._fragment.append(raw)
        if tag == "h1" and not self.title:
            self._h1_depth = 1
        elif self._h1_depth:
            self._h1_depth += 1

    def handle_startendtag(
        self, tag: str, attrs_list: list[tuple[str, str | None]]
    ) -> None:
        if self._capture_tag:
            raw = self.get_starttag_text()
            if raw:
                self._fragment.append(raw)

    def handle_endtag(self, tag: str) -> None:
        if self._capture_tag:
            if self._capture_depth == 1 and tag == self._capture_tag:
                self._capture_tag = ""
                self._capture_depth = 0
                self._capture_done = True
            else:
                self._fragment.append(f"</{tag}>")
                self._capture_depth -= 1
        if self._h1_depth:
            self._h1_depth -= 1
            if not self._h1_depth:
                self.title = _clean_inline("".join(self._h1_parts))

    def handle_data(self, data: str) -> None:
        if self._capture_tag:
            self._fragment.append(data)
        if self._h1_depth:
            self._h1_parts.append(data)

    @property
    def fragment(self) -> str:
        return "".join(self._fragment)


# BEGIN WEBSITE CODE: tomatobible-parser
class TomatoBibleStructuredTextParser(StructuredTextParser):
    """Preserve TomatoBible's Substack body while marking interactive content."""

    # Let the nested iframe provide the exact public player URL. The shared
    # parser otherwise stops at Substack's youtube-wrap container, whose own
    # attributes do not include a normal src/href value.
    EMBED_CLASS_PARTS = StructuredTextParser.EMBED_CLASS_PARTS - {"youtube-wrap"}

    INTERACTIVE_COMPONENTS = {"ButtonCreateButton"}
    INTERACTIVE_CLASS_PARTS = {"file-attachment", "image-gallery"}

    @staticmethod
    def _component_source(attrs: dict[str, str | None]) -> str:
        raw = attrs.get("data-attrs") or ""
        if raw:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data = {}
            if isinstance(data, dict):
                for key in ("url", "src", "href"):
                    value = data.get(key)
                    if isinstance(value, str) and value:
                        return value
        return attrs.get("src") or attrs.get("href") or ""

    def _add_media(self, kind: str, attrs: dict[str, str | None]) -> None:
        enriched = dict(attrs)
        if not enriched.get("src") and not enriched.get("href"):
            enriched["src"] = self._component_source(enriched)
        super()._add_media(kind, enriched)

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        component = attrs.get("data-component-name") or ""
        classes = _class_tokens(attrs)
        if not self._ignore_depth and not self._skip_depth and (
            component in self.INTERACTIVE_COMPONENTS
            or any(
                part in class_name
                for part in self.INTERACTIVE_CLASS_PARTS
                for class_name in classes
            )
        ):
            self._add_media("interactive content", attrs)
            if tag not in self.VOID_TAGS:
                self._skip_depth = 1
            return
        super().handle_starttag(tag, attrs_list)


def tomatobible_html_to_blocks(
    fragment: str, canonical_url: str, first_media_number: int = 1
) -> tuple[list[str], list[MediaReference]]:
    parser = TomatoBibleStructuredTextParser(first_media_number)
    parser.feed(fragment)
    parser.close()
    for item in parser.media:
        if item.source:
            item.source = urllib.parse.urljoin(canonical_url, item.source)
    return parser.blocks, parser.media
# END WEBSITE CODE: tomatobible-parser




# BEGIN WEBSITE CODE: reese-report-support
class ReeseReportStructuredTextParser(StructuredTextParser):
    """Preserve Reese Report prose while marking Substack components in place."""

    VISUAL_TAGS = StructuredTextParser.VISUAL_TAGS | {
        "details", "form", "object", "select", "textarea",
    }
    # The iframe inside a YouTube wrapper has the useful source URL. Other
    # Substack embed wrappers are represented as one component so their chrome
    # and duplicated preview images do not become separate placeholders.
    EMBED_CLASS_PARTS = StructuredTextParser.EMBED_CLASS_PARTS - {"youtube-wrap"}
    INTERACTIVE_TAGS = {"button", "input"}
    INTERACTIVE_CLASS_PARTS = {
        "button-wrapper", "carousel", "file-attachment", "image-gallery",
        "interactive", "link-preview", "subscribe-widget",
    }
    INTERACTIVE_COMPONENT_PARTS = {
        "attachment", "audio", "button", "embed", "gallery", "interactive",
        "poll", "subscribe", "video",
    }

    @staticmethod
    def _component_source(attrs: dict[str, str | None]) -> str:
        for key in (
            "src", "data-src", "data-url", "data-href", "href", "poster", "action",
        ):
            value = attrs.get(key) or ""
            if value:
                return value
        raw = attrs.get("data-attrs") or ""
        if raw:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                data = {}
            if isinstance(data, dict):
                for key in (
                    "url", "src", "href", "base_url", "downloadUrl", "imageUrl",
                ):
                    value = data.get(key)
                    if isinstance(value, str) and value:
                        return value
        srcset = attrs.get("srcset") or attrs.get("data-srcset") or ""
        if srcset:
            candidates = [
                part.strip().split()[0]
                for part in srcset.split(",")
                if part.strip()
            ]
            if candidates:
                return candidates[-1]
        return ""

    def _add_media(self, kind: str, attrs: dict[str, str | None]) -> None:
        enriched = dict(attrs)
        if not enriched.get("src") and not enriched.get("href"):
            enriched["src"] = self._component_source(enriched)
        super()._add_media(kind, enriched)

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs = dict(attrs_list)
        classes = _class_tokens(attrs)
        component = (attrs.get("data-component-name") or "").lower()
        is_interactive_component = any(
            part in component for part in self.INTERACTIVE_COMPONENT_PARTS
        )
        is_interactive_class = any(
            part in class_name
            for part in self.INTERACTIVE_CLASS_PARTS
            for class_name in classes
        )
        if (
            not self._ignore_depth
            and not self._skip_depth
            and (
                tag in self.INTERACTIVE_TAGS
                or is_interactive_component
                or is_interactive_class
            )
        ):
            self._add_media("interactive content", attrs)
            if tag not in self.VOID_TAGS:
                self._skip_depth = 1
            return
        super().handle_starttag(tag, attrs_list)


def reese_report_html_to_blocks(
    fragment: str, canonical_url: str, first_media_number: int = 1
) -> tuple[list[str], list[MediaReference]]:
    parser = ReeseReportStructuredTextParser(first_media_number)
    parser.feed(fragment)
    parser.close()
    for item in parser.media:
        if item.source:
            item.source = urllib.parse.urljoin(canonical_url, item.source)
    return parser.blocks, parser.media


class ReeseReportRichTextConverter:
    """Convert public Substack comment JSON without depending on another site."""

    VISUAL_TYPES = {
        "attachment", "audio", "button", "canvas", "captioned_image",
        "captionedImage", "embed", "embedded_post", "embeddedPost", "file",
        "gallery", "iframe", "image", "image2", "link_preview", "linkPreview",
        "math", "native_video", "nativeVideo", "note_embed", "noteEmbed",
        "poll", "table", "tweet", "video", "youtube",
    }

    def __init__(self) -> None:
        self.media: list[MediaReference] = []
        self._next_media = 1

    @staticmethod
    def _is_media_marker(block: str) -> bool:
        return bool(re.fullmatch(r"IMAGE-\d+", block.strip()))

    @classmethod
    def _source_from_attrs(cls, attrs: object) -> str:
        if not isinstance(attrs, dict):
            return ""
        for key in (
            "src", "url", "href", "base_url", "action", "downloadUrl", "imageUrl",
            "videoUrl",
        ):
            value = attrs.get(key)
            if isinstance(value, str) and value:
                return value
        for value in attrs.values():
            if isinstance(value, dict):
                found = cls._source_from_attrs(value)
                if found:
                    return found
        return ""

    def _media_block(self, node: dict) -> str:
        attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        marker = f"IMAGE-{self._next_media:02d}"
        self._next_media += 1
        self.media.append(
            MediaReference(
                marker,
                str(node.get("type") or "embedded content"),
                self._source_from_attrs(attrs),
                _clean_inline(str(attrs.get("alt") or attrs.get("title") or "")),
                "comment body",
            )
        )
        return marker

    def _inline(self, nodes: object) -> list[str]:
        parts: list[str] = []
        if not isinstance(nodes, list):
            return parts
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = str(node.get("type") or "")
            if kind == "text":
                parts.append(str(node.get("text") or ""))
            elif kind in {"hard_break", "hardBreak"}:
                parts.append("\n")
            elif kind in self.VISUAL_TYPES:
                parts.append("\n" + self._media_block(node) + "\n")
            elif isinstance(node.get("content"), list):
                parts.extend(self._inline(node["content"]))
        return parts

    def _nodes(self, nodes: object) -> list[str]:
        blocks: list[str] = []
        if not isinstance(nodes, list):
            return blocks
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = str(node.get("type") or "")
            content = node.get("content")
            if kind in self.VISUAL_TYPES:
                blocks.append(self._media_block(node))
            elif kind in {"paragraph", "code_block", "codeBlock"}:
                text = _clean_block("".join(self._inline(content)))
                if text:
                    blocks.extend(
                        part.strip()
                        for part in re.split(r"(?m)^(IMAGE-\d+)$", text)
                        if part.strip()
                    )
            elif kind == "heading":
                attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
                level = max(1, min(6, int(attrs.get("level") or 2)))
                text = _clean_block("".join(self._inline(content)))
                if text:
                    blocks.append("#" * level + " " + text)
            elif kind in {"bullet_list", "bulletList", "ordered_list", "orderedList"}:
                ordered = kind in {"ordered_list", "orderedList"}
                items = content if isinstance(content, list) else []
                for index, item in enumerate(items, start=1):
                    item_blocks = self._nodes(
                        item.get("content") if isinstance(item, dict) else []
                    )
                    if not item_blocks:
                        continue
                    prefix = f"{index}. " if ordered else "- "
                    emitted_text = False
                    for block in item_blocks:
                        if self._is_media_marker(block):
                            blocks.append(block)
                        elif not emitted_text:
                            blocks.append(prefix + block)
                            emitted_text = True
                        else:
                            blocks.append("  " + block)
            elif kind in {"list_item", "listItem", "doc"}:
                blocks.extend(self._nodes(content))
            elif kind == "blockquote":
                for block in self._nodes(content):
                    if self._is_media_marker(block):
                        blocks.append(block)
                    else:
                        blocks.append(
                            "\n".join("> " + line for line in block.splitlines())
                        )
            elif kind in {"horizontal_rule", "horizontalRule"}:
                blocks.append("---")
            elif isinstance(content, list):
                blocks.extend(self._nodes(content))
        return blocks

    def convert(self, document: object) -> tuple[list[str], list[MediaReference]]:
        if not isinstance(document, dict):
            return [], []
        blocks = self._nodes(
            document.get("content") if document.get("type") == "doc" else [document]
        )
        return blocks, self.media
# END WEBSITE CODE: reese-report-support








def fetch_text(url: str, timeout: int = 40) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise ExtractionError(f"The website returned HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ExtractionError(f"Could not reach {url}: {reason}") from exc


class WebsiteMetadataParser(HTMLParser):
    """Read a website/publication name from common page metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.site_name = ""
        self.application_name = ""
        self.title = ""
        self.publisher_name = ""
        self._in_title = False
        self._title_parts: list[str] = []
        self._json_ld_depth = 0
        self._json_ld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            content = _clean_inline(attrs.get("content") or "")
            if key == "og:site_name" and content and not self.site_name:
                self.site_name = content
            elif key in {"application-name", "apple-mobile-web-app-title"} and content:
                if not self.application_name:
                    self.application_name = content
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and (attrs.get("type") or "").lower() == "application/ld+json":
            self._json_ld_depth = 1
            self._json_ld_parts.clear()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._json_ld_depth:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            self.title = _clean_inline("".join(self._title_parts))
        elif tag == "script" and self._json_ld_depth:
            self._json_ld_depth = 0
            try:
                data = json.loads("".join(self._json_ld_parts))
            except json.JSONDecodeError:
                return
            self.publisher_name = self._publisher_from_json(data) or self.publisher_name

    @classmethod
    def _publisher_from_json(cls, value: object) -> str:
        if isinstance(value, list):
            for item in value:
                found = cls._publisher_from_json(item)
                if found:
                    return found
            return ""
        if not isinstance(value, dict):
            return ""
        for key in ("publisher", "isPartOf", "sourceOrganization"):
            related = value.get(key)
            if isinstance(related, dict):
                name = related.get("name")
                if isinstance(name, str) and _clean_inline(name):
                    return _clean_inline(name)
        graph = value.get("@graph")
        if graph is not None:
            return cls._publisher_from_json(graph)
        return ""


def _website_name_from_host(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "website").lower()
    labels = [part for part in host.split(".") if part and part != "www"]
    hosted_platforms = {"blogspot", "substack", "wordpress"}
    if len(labels) >= 3 and labels[-2] in hosted_platforms:
        raw = labels[-3]
    elif len(labels) >= 2:
        raw = labels[-2]
    else:
        raw = labels[0] if labels else "website"
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw).replace("-", " ").replace("_", " ")
    return " ".join(part.capitalize() for part in words.split()) or "Website"


def infer_website_name(
    example_article_url: str,
    homepage_url: str = "",
    fetcher: Callable[[str], str] | None = None,
) -> str:
    """Infer a publication name from live metadata, with a domain fallback."""
    fetcher = fetcher or (lambda url: fetch_text(url, timeout=12))
    candidates = [url for url in (homepage_url.strip(), example_article_url.strip()) if url]
    for url in candidates:
        try:
            page = fetcher(url)
        except Exception:
            continue
        parser = WebsiteMetadataParser()
        try:
            parser.feed(page)
            parser.close()
        except Exception:
            continue
        for name in (parser.site_name, parser.publisher_name, parser.application_name):
            if name:
                return name
        if parser.title:
            pieces = [
                part.strip()
                for part in re.split(r"\s+(?:\||—|–|-|·)\s+", parser.title)
                if part.strip()
            ]
            if len(pieces) > 1 and len(pieces[-1]) <= 80:
                return pieces[-1]
    return _website_name_from_host(homepage_url or example_article_url)


class ArticleAdapter(Protocol):
    name: str

    @classmethod
    def matches(cls, url: str) -> bool: ...

    def extract(self, url: str, progress: Callable[[str], None]) -> ExtractedArticle: ...


# BEGIN WEBSITE CODE: ana-toledo-adapter
class SubstackAdapter:
    name = "Substack"

    @classmethod
    def matches(cls, url: str) -> bool:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return host == "substack.com" or host.endswith(".substack.com")

    @staticmethod
    def _normalized_post_url(url: str) -> tuple[str, str, str]:
        parsed = urllib.parse.urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0] != "p":
            raise ExtractionError(
                "Please paste a Substack article URL in the form https://site.substack.com/p/article-slug"
            )
        slug = parts[1]
        origin = f"{parsed.scheme}://{parsed.netloc}"
        post_url = f"{origin}/p/{urllib.parse.quote(slug)}"
        return origin, slug, post_url

    @staticmethod
    def _media_identity(url: str) -> str:
        decoded = urllib.parse.unquote(html.unescape(url))
        # Substack's CDN URL contains the original URL as its final path.  The
        # last scheme therefore identifies the underlying asset across resize
        # variants (w_424, w_1456, and so on).
        last_https = decoded.rfind("https://")
        last_http = decoded.rfind("http://")
        start = max(last_https, last_http)
        return decoded[start:] if start >= 0 else decoded

    @staticmethod
    def _comments_from_api(payload: object) -> list[Comment]:
        if not isinstance(payload, dict) or not isinstance(payload.get("comments"), list):
            raise ExtractionError("Substack returned invalid comment data.")
        results: list[Comment] = []

        def visit(items: object, parent_identifier: str = "") -> None:
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                identifier = str(item.get("id") or "")
                if item.get("deleted"):
                    # A deleted parent can still have public replies. Keep
                    # traversing the thread without exporting the tombstone.
                    visit(item.get("children"), identifier or parent_identifier)
                    continue
                converter = SubstackRichTextConverter()
                blocks, media = converter.convert(item.get("body_json"))
                if not blocks:
                    body = _clean_block(str(item.get("body") or ""))
                    blocks = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
                if blocks:
                    ancestor_path = str(item.get("ancestor_path") or "")
                    inferred_parent = ancestor_path.split(".")[-1] if ancestor_path else ""
                    results.append(
                        Comment(
                            identifier=identifier or f"position-{len(results) + 1}",
                            author=str(item.get("name") or item.get("handle") or "Unknown"),
                            published=str(item.get("date") or ""),
                            blocks=blocks,
                            parent_identifier=parent_identifier or inferred_parent,
                            media=media,
                        )
                    )
                visit(item.get("children"), identifier or parent_identifier)

        visit(payload["comments"])
        return results

    def extract(self, url: str, progress: Callable[[str], None]) -> ExtractedArticle:
        origin, slug, post_url = self._normalized_post_url(url)
        api_url = f"{origin}/api/v1/posts/{urllib.parse.quote(slug)}"
        progress("Downloading article data…")
        try:
            data = json.loads(fetch_text(api_url))
        except json.JSONDecodeError as exc:
            raise ExtractionError("Substack returned invalid article data.") from exc
        if not isinstance(data, dict) or not data.get("body_html"):
            raise ExtractionError(
                "No public article body was returned. The post may be private, paid, or unavailable."
            )

        media: list[MediaReference] = []
        blocks: list[str] = []
        cover = str(data.get("cover_image") or "")
        body_html = str(data["body_html"])
        next_number = 1
        cover_is_in_body = bool(
            cover
            and self._media_identity(cover)
            in urllib.parse.unquote(html.unescape(body_html))
        )
        if cover and not cover_is_in_body:
            marker = f"IMAGE-{next_number:02d}"
            media.append(MediaReference(marker, "cover image", cover, location="article cover"))
            blocks.append(marker)
            next_number += 1
        body_blocks, body_media = html_to_blocks(body_html, next_number)
        blocks.extend(body_blocks)
        media.extend(body_media)

        progress("Downloading public comments and replies…")
        comment_params = urllib.parse.urlencode(
            {
                "all_comments": "true",
                "sort": data.get("default_comment_sort") or "best_first",
            }
        )
        comments_api_url = f"{origin}/api/v1/post/{data['id']}/comments?{comment_params}"
        try:
            comments_payload = json.loads(fetch_text(comments_api_url))
            comments = self._comments_from_api(comments_payload)
        except (ExtractionError, json.JSONDecodeError, KeyError):
            # Older layouts may not expose the JSON endpoint.  Their dedicated
            # comments page is still a useful server-rendered fallback.
            comments_html = fetch_text(post_url + "/comments")
            parser = SubstackCommentsParser()
            parser.feed(comments_html)
            parser.close()
            comments = parser.comments()

        expected_comments = int(data.get("comment_count") or 0)
        if expected_comments and not comments:
            progress("Warning: Substack reported comments, but none were publicly readable.")
        elif expected_comments and len(comments) < expected_comments:
            progress(
                f"Warning: exported {len(comments)} of {expected_comments} reported comments; "
                "some may be hidden or restricted."
            )

        bylines = data.get("publishedBylines") or []
        author = ""
        if bylines and isinstance(bylines[0], dict):
            author = str(bylines[0].get("name") or "")
        return ExtractedArticle(
            title=str(data.get("title") or slug),
            subtitle=str(data.get("subtitle") or ""),
            author=author,
            published=str(data.get("post_date") or ""),
            canonical_url=str(data.get("canonical_url") or post_url),
            slug=str(data.get("slug") or slug),
            blocks=blocks,
            comments=comments,
            media=media,
            adapter_name=self.name,
        )
# END WEBSITE CODE: ana-toledo-adapter


class GenericArticleAdapter:
    name = "Generic HTML article"

    @classmethod
    def matches(cls, url: str) -> bool:
        return True

    def extract(self, url: str, progress: Callable[[str], None]) -> ExtractedArticle:
        progress("Downloading web page…")
        page = fetch_text(url)
        parser = GenericPageParser()
        parser.feed(page)
        parser.close()
        if not parser.fragment:
            raise ExtractionError(
                "This page has no recognizable <article> or <main> content. "
                "It needs a site-specific adapter."
            )
        blocks, media = html_to_blocks(parser.fragment)
        parsed_url = urllib.parse.urlparse(url)
        slug = Path(parsed_url.path.rstrip("/")).name or "article"
        return ExtractedArticle(
            title=parser.title or slug,
            subtitle="",
            author=parser.author,
            published=parser.published,
            canonical_url=parser.canonical or url,
            slug=slug,
            blocks=blocks,
            comments=[],
            media=media,
            adapter_name=self.name,
        )








# BEGIN WEBSITE CODE: tomatobible-adapter
class TomatoBibleAdapter:
    """Dedicated article-only adapter for TomatoBible's public Substack posts."""

    name = "TomatoBible(トマトバイブル)"

    @classmethod
    def matches(cls, url: str) -> bool:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return host == "tomatobible.substack.com"

    @staticmethod
    def _normalized_post_url(url: str) -> tuple[str, str, str]:
        parsed = urllib.parse.urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0] != "p":
            raise ExtractionError(
                "Please paste a TomatoBible article URL in the form "
                "https://tomatobible.substack.com/p/article-slug"
            )
        slug = parts[1]
        origin = f"{parsed.scheme}://{parsed.netloc}"
        post_url = f"{origin}/p/{urllib.parse.quote(slug)}"
        return origin, slug, post_url

    @staticmethod
    def _media_identity(url: str) -> str:
        decoded = urllib.parse.unquote(html.unescape(url))
        start = max(decoded.rfind("https://"), decoded.rfind("http://"))
        return decoded[start:] if start >= 0 else decoded

    def extract(self, url: str, progress: Callable[[str], None]) -> ExtractedArticle:
        origin, slug, post_url = self._normalized_post_url(url)
        api_url = f"{origin}/api/v1/posts/{urllib.parse.quote(slug)}"
        progress("Downloading TomatoBible's public article data…")
        try:
            data = json.loads(fetch_text(api_url))
        except json.JSONDecodeError as exc:
            raise ExtractionError("TomatoBible returned invalid article data.") from exc
        if not isinstance(data, dict) or not data.get("body_html"):
            raise ExtractionError(
                "No complete public TomatoBible article body was returned. "
                "The post may be private, paid, or unavailable."
            )

        canonical = str(data.get("canonical_url") or post_url)
        body_html = str(data["body_html"])
        blocks: list[str] = []
        media: list[MediaReference] = []
        cover = str(data.get("cover_image") or "")
        cover_is_in_body = bool(
            cover
            and self._media_identity(cover)
            in urllib.parse.unquote(html.unescape(body_html))
        )
        if cover and not cover_is_in_body:
            marker = "IMAGE-01"
            blocks.append(marker)
            media.append(
                MediaReference(marker, "cover image", cover, location="article cover")
            )

        body_blocks, body_media = tomatobible_html_to_blocks(
            body_html, canonical, len(media) + 1
        )
        blocks.extend(body_blocks)
        media.extend(body_media)
        if not blocks:
            raise ExtractionError("The TomatoBible article contained no readable public content.")

        bylines = data.get("publishedBylines")
        author = ""
        if isinstance(bylines, list) and bylines and isinstance(bylines[0], dict):
            author = _clean_inline(str(bylines[0].get("name") or ""))
        return ExtractedArticle(
            title=_clean_inline(str(data.get("title") or slug)),
            subtitle=_clean_inline(str(data.get("subtitle") or "")),
            author=author,
            published=_clean_inline(str(data.get("post_date") or "")),
            canonical_url=canonical,
            slug=str(data.get("slug") or slug),
            blocks=blocks,
            comments=[],
            media=media,
            adapter_name=self.name,
        )
# END WEBSITE CODE: tomatobible-adapter




# BEGIN WEBSITE CODE: reese-report-adapter
class ReeseReportAdapter:
    """Dedicated adapter for The Reese Report's public Substack posts."""

    name = "The Reese Report"
    HOST = "gregreese.substack.com"

    @classmethod
    def matches(cls, url: str) -> bool:
        return (urllib.parse.urlparse(url).hostname or "").lower() == cls.HOST

    @classmethod
    def _normalized_post_url(cls, url: str) -> tuple[str, str, str]:
        parsed = urllib.parse.urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme not in {"http", "https"}
            or (parsed.hostname or "").lower() != cls.HOST
            or len(parts) != 2
            or parts[0] != "p"
        ):
            raise ExtractionError(
                "Please paste a The Reese Report article URL in the form "
                "https://gregreese.substack.com/p/article-slug"
            )
        slug = urllib.parse.unquote(parts[1])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        post_url = f"{origin}/p/{urllib.parse.quote(slug, safe='')}"
        return origin, slug, post_url

    @staticmethod
    def _media_identity(url: str) -> str:
        decoded = urllib.parse.unquote(html.unescape(url))
        start = max(decoded.rfind("https://"), decoded.rfind("http://"))
        return decoded[start:] if start >= 0 else decoded

    @staticmethod
    def _video_source(video_upload_id: str) -> str:
        upload_id = urllib.parse.quote(video_upload_id, safe="")
        return f"https://api.substack.com/api/v1/video/upload/{upload_id}/src"

    @staticmethod
    def _comments_from_api(payload: object) -> list[Comment]:
        if not isinstance(payload, dict) or not isinstance(payload.get("comments"), list):
            raise ExtractionError("The Reese Report returned invalid public comment data.")
        results: list[Comment] = []
        seen: set[str] = set()

        def visit(items: object, parent_identifier: str = "") -> None:
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_identifier = item.get("id")
                identifier = str(raw_identifier) if raw_identifier is not None else ""
                effective_identifier = identifier or f"position-{len(results) + 1}"
                children = item.get("children")
                if item.get("deleted"):
                    # Keep the public tombstone so every exported reply points
                    # to an exported parent and the visible thread stays whole.
                    results.append(
                        Comment(
                            identifier=effective_identifier,
                            author="[Deleted]",
                            published=str(item.get("date") or ""),
                            blocks=["[Deleted comment]"],
                            parent_identifier=parent_identifier,
                        )
                    )
                    seen.add(effective_identifier)
                    visit(children, identifier or parent_identifier)
                    continue
                if effective_identifier in seen:
                    visit(children, identifier or parent_identifier)
                    continue

                converter = ReeseReportRichTextConverter()
                blocks, media = converter.convert(item.get("body_json"))
                if not blocks:
                    body = str(item.get("body") or "").strip()
                    blocks = [
                        _clean_block(part)
                        for part in re.split(r"\n\s*\n", body)
                        if _clean_block(part)
                    ]
                if blocks:
                    ancestor_path = str(item.get("ancestor_path") or "")
                    inferred_parent = ancestor_path.split(".")[-1] if ancestor_path else ""
                    results.append(
                        Comment(
                            identifier=effective_identifier,
                            author=str(item.get("name") or item.get("handle") or "Unknown"),
                            published=str(item.get("date") or ""),
                            blocks=blocks,
                            parent_identifier=parent_identifier or inferred_parent,
                            media=media,
                        )
                    )
                    seen.add(effective_identifier)
                visit(children, identifier or parent_identifier)

        visit(payload["comments"])
        return results

    def extract(self, url: str, progress: Callable[[str], None]) -> ExtractedArticle:
        origin, slug, post_url = self._normalized_post_url(url)
        article_api_url = f"{origin}/api/v1/posts/{urllib.parse.quote(slug, safe='')}"
        progress("Downloading The Reese Report's public article data…")
        try:
            data = json.loads(fetch_text(article_api_url))
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                "The Reese Report returned invalid public article data."
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("body_html"), str):
            raise ExtractionError(
                "No complete public The Reese Report article body was returned. "
                "The post may be private, paid, or unavailable."
            )
        if data.get("is_geoblocked"):
            raise ExtractionError("This The Reese Report post is not public in this region.")
        if data.get("free_unlock_required") or data.get("audience") not in {
            None,
            "everyone",
        }:
            raise ExtractionError(
                "The complete The Reese Report post is not publicly accessible."
            )
        if not data.get("id"):
            raise ExtractionError("The Reese Report article data had no public post ID.")

        canonical = str(data.get("canonical_url") or post_url)
        body_html = str(data["body_html"])
        blocks: list[str] = []
        media: list[MediaReference] = []

        # Reese Report video posts render one primary player before the prose.
        # Its cover image and extracted podcast audio are alternate assets of
        # that same player, so one placeholder accurately represents the live
        # reading position without duplicating the component.
        video_upload_id = str(data.get("video_upload_id") or "")
        podcast_url = str(data.get("podcast_url") or "")
        live_stream_id = str(data.get("live_stream_id") or "")
        cover = str(data.get("cover_image") or "")
        if video_upload_id:
            blocks.append("IMAGE-01")
            media.append(
                MediaReference(
                    "IMAGE-01",
                    "video",
                    self._video_source(video_upload_id),
                    location="primary article player",
                )
            )
        elif podcast_url:
            blocks.append("IMAGE-01")
            media.append(
                MediaReference(
                    "IMAGE-01", "audio", podcast_url, location="primary article player"
                )
            )
        elif live_stream_id:
            blocks.append("IMAGE-01")
            media.append(
                MediaReference(
                    "IMAGE-01", "live video", "", location="primary article player"
                )
            )
        elif cover and self._media_identity(cover) not in urllib.parse.unquote(
            html.unescape(body_html)
        ):
            blocks.append("IMAGE-01")
            media.append(
                MediaReference("IMAGE-01", "cover image", cover, location="article cover")
            )

        body_blocks, body_media = reese_report_html_to_blocks(
            body_html, canonical, len(media) + 1
        )
        blocks.extend(body_blocks)
        media.extend(body_media)
        if not blocks:
            raise ExtractionError(
                "The Reese Report article contained no readable public content."
            )

        progress("Downloading all public comments and nested replies…")
        comment_params = urllib.parse.urlencode(
            {
                "all_comments": "true",
                "sort": data.get("default_comment_sort") or "best_first",
            }
        )
        comments_api_url = f"{origin}/api/v1/post/{data['id']}/comments?{comment_params}"
        try:
            comments_payload = json.loads(fetch_text(comments_api_url))
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                "The Reese Report returned invalid public comment data."
            ) from exc
        comments = self._comments_from_api(comments_payload)

        expected_comments = int(data.get("comment_count") or 0)
        if expected_comments and not comments:
            progress(
                "Warning: The Reese Report reported comments, but none were publicly readable."
            )
        elif expected_comments and len(comments) < expected_comments:
            progress(
                f"Warning: exported {len(comments)} of {expected_comments} reported comments; "
                "the remainder were deleted, hidden, or not publicly returned."
            )

        bylines = data.get("publishedBylines")
        author = ""
        if isinstance(bylines, list) and bylines and isinstance(bylines[0], dict):
            author = _clean_inline(str(bylines[0].get("name") or ""))
        return ExtractedArticle(
            title=_clean_inline(str(data.get("title") or slug)),
            subtitle=_clean_inline(str(data.get("subtitle") or "")),
            author=author,
            published=_clean_inline(str(data.get("post_date") or "")),
            canonical_url=canonical,
            slug=str(data.get("slug") or slug),
            blocks=blocks,
            comments=comments,
            media=media,
            adapter_name=self.name,
        )
# END WEBSITE CODE: reese-report-adapter


@dataclass(frozen=True)
class WebsiteExtractorDefinition:
    """One user-selectable website and the adapter that powers its extractor."""

    key: str
    display_name: str
    homepage: str
    description: str
    example_url: str
    allowed_hosts: tuple[str, ...]
    adapter_type: type
    extracts_comments: bool = False
    removable_sections: tuple[str, ...] = ()
    shared_sections: tuple[str, ...] = ()

    def validate_url(self, url: str) -> str:
        cleaned = url.strip()
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ExtractionError("Enter a complete http:// or https:// article URL.")
        host = (parsed.hostname or "").lower()
        if host not in self.allowed_hosts:
            raise ExtractionError(
                f"This extractor only accepts articles from {', '.join(self.allowed_hosts)}. "
                "Go back and select the correct website extractor."
            )
        return cleaned

    def make_adapter(self) -> ArticleAdapter:
        return self.adapter_type()


# This is the website menu shown before any extractor opens. Future sources get
# their own entry and adapter here; the GUI never guesses from a pasted URL.
WEBSITE_EXTRACTORS: tuple[WebsiteExtractorDefinition, ...] = (
    # BEGIN WEBSITE CODE: profile-ana-toledo
    WebsiteExtractorDefinition(
        key="ana-toledo",
        display_name="Ana Toledo — Mira!",
        homepage="https://anatoledo.substack.com/archive",
        description="Extract articles and all publicly accessible comments from Mira! on Substack.",
        example_url="https://anatoledo.substack.com/p/freemasons-in-france-convicted",
        allowed_hosts=("anatoledo.substack.com",),
        adapter_type=SubstackAdapter,
        extracts_comments=True,
        removable_sections=("ana-toledo-support", "ana-toledo-adapter"),
    ),
    # END WEBSITE CODE: profile-ana-toledo
    # BEGIN WEBSITE CODE: profile-tomatobible
    WebsiteExtractorDefinition(
        key="tomatobible",
        display_name="TomatoBible(トマトバイブル)",
        homepage="https://tomatobible.substack.com/archive",
        description=(
            "Extract complete public TomatoBible articles with ordered placeholders for "
            "images, video embeds, interactive buttons, and other non-linear content. "
            "Comments are not extracted."
        ),
        example_url=(
            "https://tomatobible.substack.com/p/"
            "predators-among-us-fascists-decodedunderstanding-b3d"
        ),
        allowed_hosts=("tomatobible.substack.com",),
        adapter_type=TomatoBibleAdapter,
        removable_sections=("tomatobible-parser", "tomatobible-adapter"),
    ),
    # END WEBSITE CODE: profile-tomatobible
    # BEGIN WEBSITE CODE: profile-reese-report
    WebsiteExtractorDefinition(
        key="reese-report",
        display_name="The Reese Report",
        homepage="https://gregreese.substack.com/archive",
        description=(
            "Extract complete public The Reese Report articles plus all publicly "
            "accessible comments and nested replies. Video, audio, images, embeds, "
            "tables, and other non-linear content remain in reading order as placeholders."
        ),
        example_url=(
            "https://gregreese.substack.com/p/"
            "cymatics-and-the-mysteries-of-the"
        ),
        allowed_hosts=("gregreese.substack.com",),
        adapter_type=ReeseReportAdapter,
        extracts_comments=True,
        removable_sections=("reese-report-support", "reese-report-adapter"),
    ),
    # END WEBSITE CODE: profile-reese-report
)

WEBSITE_EXTRACTORS_BY_KEY = {item.key: item for item in WEBSITE_EXTRACTORS}

WEBSITE_CODE_MARKER_PREFIX = "WEBSITE CODE:"


def remove_managed_website_sections(source: str, section_names: Iterable[str]) -> str:
    """Remove exact managed sections, refusing ambiguous or missing boundaries."""
    updated = source
    for section_name in sorted(set(section_names)):
        start = f"# BEGIN {WEBSITE_CODE_MARKER_PREFIX} {section_name}"
        end = f"# END {WEBSITE_CODE_MARKER_PREFIX} {section_name}"
        pattern = re.compile(
            rf"(?m)^[ \t]*{re.escape(start)}[ \t]*\n"
            rf".*?"
            rf"^[ \t]*{re.escape(end)}[ \t]*(?:\n|$)",
            re.DOTALL,
        )
        matches = tuple(pattern.finditer(updated))
        if len(matches) != 1:
            raise WebsiteRemovalError(
                f"The managed source section {section_name!r} was missing or ambiguous. "
                "No website code was removed."
            )
        updated = pattern.sub("", updated, count=1)
    return updated


def website_sections_to_remove(
    website: WebsiteExtractorDefinition,
    installed_websites: Iterable[WebsiteExtractorDefinition],
) -> set[str]:
    """Include shared code only when the selected website is its final consumer."""
    installed = tuple(installed_websites)
    sections = set(website.removable_sections)
    sections.add(f"profile-{website.key}")
    for shared_section in website.shared_sections:
        used_elsewhere = any(
            candidate.key != website.key
            and shared_section in candidate.shared_sections
            for candidate in installed
        )
        if not used_elsewhere:
            sections.add(shared_section)
    return sections


def permanently_remove_website_source(
    website: WebsiteExtractorDefinition,
    installed_websites: Iterable[WebsiteExtractorDefinition],
    source_path: Path | None = None,
) -> set[str]:
    """Atomically remove one profile and its now-unneeded adapter source code."""
    path = (source_path or Path(__file__)).resolve()
    if path.suffix != ".py" or not path.is_file():
        raise WebsiteRemovalError(
            "The application is not running from an editable Python source file."
        )
    sections = website_sections_to_remove(website, installed_websites)
    temporary: Path | None = None
    try:
        original = path.read_text(encoding="utf-8")
        updated = remove_managed_website_sections(original, sections)
        compile(updated, str(path), "exec")
        original_mode = path.stat().st_mode
        temporary = path.with_name(f".{path.name}.website-removal.tmp")
        temporary.write_text(updated, encoding="utf-8")
        temporary.chmod(original_mode)
        temporary.replace(path)
    except WebsiteRemovalError:
        raise
    except (OSError, SyntaxError) as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise WebsiteRemovalError(f"Website code could not be removed safely: {exc}") from exc
    return sections


def build_website_extension_prompt(
    website_name: str,
    homepage_url: str,
    example_article_url: str,
    comments_option: str,
) -> str:
    """Build the prompt users paste into Codex to add one website extractor."""
    website_name = website_name.strip()
    homepage_url = homepage_url.strip()
    example_article_url = example_article_url.strip()
    if comments_option == "Comments only":
        comments_instruction = (
            "Extract all publicly accessible top-level comments, but do not include nested "
            "replies. Put them in one well-structured comments TXT file. Only create numbered "
            "comment parts if the 12,500-token ceiling requires it, and split between complete "
            "comments whenever possible."
        )
    elif comments_option == "Comments and nested replies":
        comments_instruction = (
            "Extract all publicly accessible comments and nested replies. Preserve the thread "
            "relationship by recording the parent comment ID for every reply. Put everything "
            "in one well-structured comments TXT file. Only create numbered comment parts if "
            "the 12,500-token ceiling requires it, and split between complete comments whenever "
            "possible."
        )
    else:
        comments_instruction = (
            "Do not implement comment extraction for this website. The extractor should export "
            "the article only."
        )

    source_lines = [
        f"Website name: {website_name}",
        f"Required example article: {example_article_url}",
    ]
    if homepage_url:
        source_lines.insert(1, f"Website homepage/archive: {homepage_url}")

    return "\n".join(
        [
            "Extend article_extractor_gui.py in the current directory with a dedicated extractor "
            "for the following website.",
            "",
            *source_lines,
            "",
            "Inspect the supplied live example and determine the website's real article structure "
            "and public data endpoints before implementing anything. Add this website as a new "
            "explicit choice on the application's first website-selection screen. Selecting it "
            "must open its dedicated extractor. Do not use automatic URL-based extractor selection, "
            "and do not break or change existing website extractors.",
            "",
            "ARTICLE REQUIREMENTS",
            "",
            "Extract the complete public article while preserving its reading order, headings, "
            "paragraphs, lists, quotations, and other normal linear text structure.",
            "",
            "Everything that is not regular linear text must be represented at its original "
            "position by ordered placeholders named IMAGE-01, IMAGE-02, and so on. This includes "
            "images, tables, diagrams, charts, galleries, embeds, audio, video, interactive "
            "elements, and any other non-linear content. When a source URL exists, use exactly "
            "this layout with one empty line between the placeholder and source:",
            "",
            "IMAGE-01",
            "",
            "Media source: https://example.com/media",
            "",
            "Retain the existing unchecked media-download option. When selected, download direct "
            "media files into the extraction's media folder without adding type, location, or "
            "downloaded-filename lines to the article text.",
            "",
            "COMMENTS REQUIREMENTS",
            "",
            comments_instruction,
            "",
            "CHUNKING AND OUTPUT REQUIREMENTS",
            "",
            "Keep every generated TXT file strictly below 12,500 tokens. Split only at safe "
            "paragraph or complete-comment boundaries; never cut through a sentence or paragraph. "
            "Preserve headings with the content that follows them. Continue using a user-selected "
            "output folder and unique extraction directories so existing exports are never "
            "overwritten.",
            "",
            "IMPLEMENTATION REQUIREMENTS",
            "",
            "Use the existing shared GUI shell, structured-text conversion, media handling, token "
            "counter, chunk writer, dependency bootstrap, and output conventions wherever possible. "
            "Keep website-specific parsing isolated in its own adapter/profile. Add focused tests, "
            "run a live extraction against the supplied example, verify every generated TXT file's "
            "token count, and update the README website menu and usage notes.",
        ]
    ) + "\n"


class TokenCounter:
    """Count CL100K tokens when available; otherwise use a safe byte upper bound."""

    def __init__(self) -> None:
        self.mode = "UTF-8 byte upper bound"
        self._encoding = None
        try:
            import tiktoken  # type: ignore

            self._encoding = tiktoken.get_encoding("cl100k_base")
            self.mode = "tiktoken cl100k_base"
        except Exception:
            self._encoding = None

    def count(self, text: str) -> int:
        if self._encoding is not None:
            return len(self._encoding.encode(text, disallowed_special=()))
        # Byte-level BPE tokenizers cannot emit more tokens than source bytes.
        # This fallback can make smaller chunks, but it never understates the
        # token count for the GPT-style tokenizers this limit is intended for.
        return len(text.encode("utf-8"))


def _semantic_units(blocks: Iterable[str]) -> list[str]:
    """Keep headings attached to the following paragraph/list item."""
    units: list[str] = []
    pending_headings: list[str] = []
    for raw in blocks:
        block = raw.strip()
        if not block:
            continue
        if re.match(r"^#{1,6} ", block):
            pending_headings.append(block)
            continue
        if pending_headings:
            block = "\n\n".join([*pending_headings, block])
            pending_headings.clear()
        units.append(block)
    if pending_headings:
        if units:
            units[-1] = units[-1] + "\n\n" + "\n\n".join(pending_headings)
        else:
            units.append("\n\n".join(pending_headings))
    return units


def split_blocks(
    header: str,
    blocks: Iterable[str],
    counter: TokenCounter,
    max_tokens: int = MAX_FILE_TOKENS,
) -> list[str]:
    """Greedily split only between paragraph-like semantic units."""
    clean_header = header.strip()
    units = _semantic_units(blocks)
    if not units:
        result = clean_header + "\n"
        if counter.count(result) > max_tokens:
            raise OversizedBlockError("The metadata header exceeds the token limit.")
        return [result]

    chunks: list[str] = []
    current: list[str] = []
    for unit in units:
        candidate_blocks = [*current, unit]
        candidate = clean_header + "\n\n" + "\n\n".join(candidate_blocks) + "\n"
        if counter.count(candidate) <= max_tokens:
            current = candidate_blocks
            continue
        if not current:
            raise OversizedBlockError(
                "One source paragraph is too large to fit below 12,500 tokens. "
                "It was not split because doing so would break the requested paragraph boundary."
            )
        chunks.append(clean_header + "\n\n" + "\n\n".join(current) + "\n")
        current = [unit]
        single = clean_header + "\n\n" + unit + "\n"
        if counter.count(single) > max_tokens:
            raise OversizedBlockError(
                "One source paragraph is too large to fit below 12,500 tokens. "
                "It was not split because doing so would break the requested paragraph boundary."
            )
    if current:
        chunks.append(clean_header + "\n\n" + "\n\n".join(current) + "\n")
    return chunks


def _safe_name(value: str, fallback: str = "article") -> str:
    value = html.unescape(value).strip()
    value = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-{2,}", "-", value).strip("-._")
    return value[:100] or fallback


def _article_header(article: ExtractedArticle) -> str:
    lines = [f"Title: {article.title}"]
    if article.subtitle:
        lines.append(f"Subtitle: {article.subtitle}")
    if article.author:
        lines.append(f"Author: {article.author}")
    if article.published:
        lines.append(f"Published: {article.published}")
    lines.extend(
        [
            f"Source: {article.canonical_url}",
            f"Extractor: {article.adapter_name}",
        ]
    )
    return "\n".join(lines)


def _media_details(item: MediaReference) -> str:
    """Render a compact placeholder followed by its source URL."""
    lines = [item.placeholder]
    if item.source:
        lines.extend(["", f"Media source: {item.source}"])
    return "\n".join(lines)


def _blocks_with_inline_media(
    blocks: Iterable[str], media: Iterable[MediaReference]
) -> list[str]:
    media_by_marker = {item.placeholder: item for item in media}
    rendered: list[str] = []
    for block in blocks:
        marker = block.strip()
        item = media_by_marker.get(marker)
        rendered.append(_media_details(item) if item else block)
    return rendered


def _media_extension(source: str, content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    preferred = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/svg+xml": ".svg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
    }
    if media_type in preferred:
        return preferred[media_type]
    guessed = mimetypes.guess_extension(media_type) if media_type else None
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    decoded_path = urllib.parse.urlparse(urllib.parse.unquote(source)).path
    suffix = Path(decoded_path).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ".bin"


def _download_media_item(
    item: MediaReference,
    media_dir: Path,
    filename_stem: str,
    output_dir: Path,
) -> Path | None:
    parsed = urllib.parse.urlparse(item.source)
    downloadable_kind = any(
        word in item.kind.lower() for word in ("image", "audio", "video")
    )
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not downloadable_kind:
        item.download_error = "No direct downloadable media file was available."
        return None
    request = urllib.request.Request(
        item.source,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    part_path: Path | None = None
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_type = response.headers.get("Content-Type", "")
            extension = _media_extension(item.source, content_type)
            media_dir.mkdir(parents=True, exist_ok=True)
            destination = media_dir / f"{filename_stem}{extension}"
            part_path = media_dir / f".{filename_stem}{extension}.part"
            with part_path.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
            part_path.replace(destination)
            item.downloaded_file = destination.relative_to(output_dir).as_posix()
            return destination
    except Exception as exc:
        if part_path is not None:
            part_path.unlink(missing_ok=True)
        item.download_error = f"Download failed: {exc}"
        return None


def _download_article_media(
    article: ExtractedArticle,
    output_dir: Path,
    progress: Callable[[str], None],
) -> list[Path]:
    downloaded: list[Path] = []
    media_dir = output_dir / "media"
    all_items = len(article.media) + sum(len(comment.media) for comment in article.comments)
    current = 0
    for item in article.media:
        current += 1
        progress(f"Downloading media {current} of {all_items}: {item.placeholder}…")
        path = _download_media_item(item, media_dir, item.placeholder, output_dir)
        if path is not None:
            downloaded.append(path)
        elif item.download_error:
            progress(f"Warning: {item.placeholder}: {item.download_error}")
    comment_width = max(2, len(str(len(article.comments))))
    for comment_number, comment in enumerate(article.comments, start=1):
        for item in comment.media:
            current += 1
            stem = f"COMMENT-{comment_number:0{comment_width}d}_{item.placeholder}"
            progress(f"Downloading media {current} of {all_items}: {stem}…")
            path = _download_media_item(item, media_dir, stem, output_dir)
            if path is not None:
                downloaded.append(path)
            elif item.download_error:
                progress(f"Warning: {stem}: {item.download_error}")
    return downloaded


def _comments_header(article: ExtractedArticle) -> str:
    return "\n".join(
        [
            f"Comments for: {article.title}",
            f"Article source: {article.canonical_url}",
            f"Comments exported: {len(article.comments)}",
        ]
    )


def _comment_section(comment: Comment, number: int, width: int) -> str:
    lines = [
        "=" * 80,
        f"COMMENT {number:0{width}d}",
        f"Comment ID: {comment.identifier}",
        f"Author: {comment.author}",
    ]
    if comment.published:
        lines.append(f"Published: {comment.published}")
    if comment.parent_identifier:
        lines.append(f"Reply to comment ID: {comment.parent_identifier}")
    lines.append("-" * 80)
    body = "\n\n".join(_blocks_with_inline_media(comment.blocks, comment.media))
    lines.extend(["", body])
    return "\n".join(lines).rstrip()


def _unique_output_directory(parent: Path, slug: str) -> Path:
    base = parent / _safe_name(slug)
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{base.name}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def export_article(
    article: ExtractedArticle,
    output_parent: Path,
    progress: Callable[[str], None] = lambda _message: None,
    counter: TokenCounter | None = None,
    download_media: bool = False,
) -> tuple[Path, list[Path]]:
    counter = counter or TokenCounter()
    if not output_parent.is_dir():
        raise ExtractionError("The selected output folder does not exist.")
    output_dir = _unique_output_directory(output_parent, article.slug or article.title)
    written: list[Path] = []
    try:
        downloaded_files: list[Path] = []
        if download_media:
            downloaded_files = _download_article_media(article, output_dir, progress)
            written.extend(downloaded_files)
        progress(f"Chunking with {counter.mode}…")
        article_blocks = _blocks_with_inline_media(article.blocks, article.media)
        article_chunks = split_blocks(_article_header(article), article_blocks, counter)
        article_base = _safe_name(article.slug or article.title)
        for index, chunk in enumerate(article_chunks, start=1):
            if len(article_chunks) == 1:
                name = f"{article_base}.txt"
            else:
                name = f"{article_base}_part_{index:02d}.txt"
            path = output_dir / name
            path.write_text(chunk, encoding="utf-8")
            written.append(path)

        if article.comments:
            width = max(2, len(str(len(article.comments))))
            comment_sections = [
                _comment_section(comment, number, width)
                for number, comment in enumerate(article.comments, start=1)
            ]
            comment_chunks = split_blocks(
                _comments_header(article), comment_sections, counter
            )
            for index, chunk in enumerate(comment_chunks, start=1):
                suffix = f"_part_{index:02d}" if len(comment_chunks) > 1 else ""
                path = output_dir / f"{article_base}_comments{suffix}.txt"
                path.write_text(chunk, encoding="utf-8")
                written.append(path)

        summary = {
            "title": article.title,
            "source": article.canonical_url,
            "adapter": article.adapter_name,
            "article_files": len(article_chunks),
            "comments_exported": len(article.comments),
            "comment_files": len(comment_chunks) if article.comments else 0,
            "media_placeholders": len(article.media),
            "media_download_requested": download_media,
            "media_files_downloaded": len(downloaded_files),
            "token_counter": counter.mode,
            "strict_file_token_limit": MAX_FILE_TOKENS,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        summary_path = output_dir / "extraction_summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        written.append(summary_path)
    except Exception:
        # Leave successfully written files in place for diagnosis.  The unique
        # output directory also ensures a later retry cannot mix with them.
        raise
    return output_dir, written


def extract_to_folder(
    url: str,
    output_parent: Path,
    adapter: ArticleAdapter,
    progress: Callable[[str], None] = lambda _message: None,
    download_media: bool = False,
) -> tuple[ExtractedArticle, Path, list[Path]]:
    progress(f"Using the {adapter.name} adapter…")
    article = adapter.extract(url.strip(), progress)
    if not article.blocks:
        raise ExtractionError("The article was found, but it contained no readable text.")
    output_dir, files = export_article(
        article, output_parent, progress, download_media=download_media
    )
    return article, output_dir, files


class DangerButton(tk.Canvas):
    """A consistently red button on macOS, where Aqua ignores Tk button colors."""

    NORMAL_COLOR = "#c62828"
    HOVER_COLOR = "#a61f1f"
    PRESSED_COLOR = "#7f1717"
    DISABLED_COLOR = "#d7aaa8"

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        state: str = "normal",
    ) -> None:
        frame_background = ttk.Style(parent).lookup("TFrame", "background") or "#f0f0f0"
        super().__init__(
            parent,
            width=148,
            height=30,
            background=frame_background,
            borderwidth=0,
            highlightthickness=0,
            takefocus=1,
            cursor="arrow" if state == "disabled" else "hand2",
        )
        self._text = text
        self._command = command
        self._state = state
        self._hovered = False
        self._pressed = False
        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Key-space>", self._keyboard_activate)
        self.bind("<Key-Return>", self._keyboard_activate)
        self.bind("<FocusIn>", self._redraw)
        self.bind("<FocusOut>", self._redraw)
        self._redraw()

    def _fill_color(self) -> str:
        if self._state == "disabled":
            return self.DISABLED_COLOR
        if self._pressed:
            return self.PRESSED_COLOR
        if self._hovered:
            return self.HOVER_COLOR
        return self.NORMAL_COLOR

    def _redraw(self, _event: object = None) -> None:
        self.delete("all")
        width = max(2, self.winfo_width() - 1)
        height = max(2, self.winfo_height() - 1)
        outline = "#7f1717" if self.focus_get() is self else self._fill_color()
        self.create_rectangle(
            1,
            1,
            width,
            height,
            fill=self._fill_color(),
            outline=outline,
            width=2 if self.focus_get() is self else 1,
            tags=("surface",),
        )
        self.create_text(
            width / 2,
            height / 2,
            text=self._text,
            fill="white",
            font=("TkDefaultFont", 12, "bold"),
            tags=("label",),
        )

    def _enter(self, _event: object) -> None:
        self._hovered = True
        self._redraw()

    def _leave(self, _event: object) -> None:
        self._hovered = False
        self._pressed = False
        self._redraw()

    def _press(self, _event: object) -> None:
        if self._state == "normal":
            self.focus_set()
            self._pressed = True
            self._redraw()

    def _release(self, event: tk.Event) -> None:
        should_activate = (
            self._state == "normal"
            and self._pressed
            and 0 <= event.x < self.winfo_width()
            and 0 <= event.y < self.winfo_height()
        )
        self._pressed = False
        self._redraw()
        if should_activate:
            self._command()

    def _keyboard_activate(self, _event: object) -> str:
        if self._state == "normal":
            self._command()
        return "break"

    def configure(self, cnf: dict[str, object] | None = None, **kwargs: object) -> object:
        options = dict(cnf or {})
        options.update(kwargs)
        if "state" in options:
            state = str(options.pop("state"))
            if state not in {"normal", "disabled"}:
                raise tk.TclError(f"bad state {state!r}: must be normal or disabled")
            self._state = state
            options["cursor"] = "arrow" if state == "disabled" else "hand2"
        if "text" in options:
            self._text = str(options.pop("text"))
        result = super().configure(options) if options else None
        self._redraw()
        return result

    config = configure

    def cget(self, key: str) -> object:
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        if key == "background":
            return self._fill_color()
        return super().cget(key)


class ExtractorGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.minsize(760, 460)
        self.url_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(Path.home() / "Downloads"))
        self.download_media_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")
        self.website_var = tk.StringVar()
        prompt_draft = self._load_prompt_draft()
        self.prompt_website_name_var = tk.StringVar(
            value=str(prompt_draft.get("website_name") or "")
        )
        self.prompt_homepage_var = tk.StringVar(
            value=str(prompt_draft.get("homepage_url") or "")
        )
        self.prompt_example_var = tk.StringVar(
            value=str(prompt_draft.get("example_article_url") or "")
        )
        saved_comments = str(prompt_draft.get("comments_option") or "")
        self.prompt_comments_var = tk.StringVar(
            value=saved_comments if saved_comments in COMMENT_OPTIONS else ""
        )
        self.prompt_status_var = tk.StringVar(value="Fill the required fields, then generate.")
        self._last_inferred_website_name = (
            self.prompt_website_name_var.get()
            if prompt_draft.get("website_name_was_inferred")
            else ""
        )
        self._draft_save_job: str | None = None
        self._prompt_results: queue.Queue[tuple[str, bool, str]] = queue.Queue()
        self._busy = False
        self._removed_website_keys: set[str] = set()
        self.active_website: WebsiteExtractorDefinition | None = None
        self.frame: ttk.Frame | None = None
        for variable in (
            self.prompt_website_name_var,
            self.prompt_homepage_var,
            self.prompt_example_var,
            self.prompt_comments_var,
        ):
            variable.trace_add("write", self._schedule_prompt_draft_save)
        self.prompt_homepage_var.trace_add("write", self._clear_inferred_website_name)
        self.prompt_example_var.trace_add("write", self._clear_inferred_website_name)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._build_website_selector()

    @staticmethod
    def _load_prompt_draft() -> dict[str, object]:
        try:
            data = json.loads(PROMPT_DRAFT_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _schedule_prompt_draft_save(self, *_args: object) -> None:
        if self._draft_save_job is not None:
            self.root.after_cancel(self._draft_save_job)
        self._draft_save_job = self.root.after(400, self._save_prompt_draft)

    def _clear_inferred_website_name(self, *_args: object) -> None:
        if (
            self._last_inferred_website_name
            and self.prompt_website_name_var.get() == self._last_inferred_website_name
        ):
            self.prompt_website_name_var.set("")
            self._last_inferred_website_name = ""

    def _save_prompt_draft(self) -> None:
        self._draft_save_job = None
        data = {
            "website_name": self.prompt_website_name_var.get(),
            "website_name_was_inferred": bool(
                self._last_inferred_website_name
                and self.prompt_website_name_var.get() == self._last_inferred_website_name
            ),
            "homepage_url": self.prompt_homepage_var.get(),
            "example_article_url": self.prompt_example_var.get(),
            "comments_option": self.prompt_comments_var.get(),
        }
        try:
            PROMPT_DRAFT_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = PROMPT_DRAFT_PATH.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(PROMPT_DRAFT_PATH)
        except OSError:
            pass

    def _close(self) -> None:
        if self._draft_save_job is not None:
            self.root.after_cancel(self._draft_save_job)
        self._save_prompt_draft()
        self.root.destroy()

    def _new_frame(self) -> ttk.Frame:
        if self.frame is not None:
            self.frame.destroy()
        self.frame = ttk.Frame(self.root, padding=18)
        self.frame.pack(fill="both", expand=True)
        return self.frame

    def _build_website_selector(self) -> None:
        self.active_website = None
        self.website_var.set("")
        self.root.title(f"{APP_NAME} — Select Website")
        frame = self._new_frame()
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)

        ttk.Label(frame, text=APP_NAME, font=("TkDefaultFont", 18, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 14)
        )
        ttk.Label(
            frame,
            text="Select a website first. Each website opens its own dedicated extractor.",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 12))

        ttk.Label(frame, text="Website").grid(row=2, column=0, sticky="w", padx=(0, 10))
        installed_websites = self._installed_websites()
        names = [website.display_name for website in installed_websites]
        self.website_combo = ttk.Combobox(
            frame,
            textvariable=self.website_var,
            values=names,
            state="readonly" if names else "disabled",
        )
        self.website_combo.grid(row=2, column=1, sticky="ew")
        self.website_combo.bind("<<ComboboxSelected>>", self._website_selected)
        website_actions = ttk.Frame(frame)
        website_actions.grid(row=2, column=2, padx=(8, 0), sticky="e")
        self.open_extractor_button = ttk.Button(
            website_actions,
            text="Open selected website extractor",
            command=self._open_selected_extractor,
            state="disabled",
        )
        self.open_extractor_button.grid(row=0, column=0)
        self.delete_website_button = DangerButton(
            website_actions,
            text="Delete website",
            command=self._delete_selected_website,
            state="disabled",
        )
        self.delete_website_button.grid(row=0, column=1, padx=(8, 0))
        self.website_description = ttk.Label(frame, text="", wraplength=700)
        self.website_description.grid(
            row=3, column=0, columnspan=3, sticky="nw", pady=(16, 0)
        )
        ttk.Separator(frame, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(28, 16)
        )
        ttk.Label(
            frame,
            text="Need support for another website? Generate an implementation prompt for Codex.",
            wraplength=700,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Button(
            frame,
            text="Generate prompt to add a website",
            command=self._build_prompt_generator,
        ).grid(row=6, column=0, columnspan=3, sticky="ew")
        if not names:
            self.website_description.configure(
                text="No website extractors remain. Restore the script from GitHub to reinstall them."
            )
        self.website_combo.focus_set()

    def _build_prompt_generator(self) -> None:
        self.root.title(f"{APP_NAME} — Website Prompt Generator")
        frame = self._new_frame()
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)

        ttk.Button(
            frame, text="← Back to websites", command=self._build_website_selector
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        ttk.Label(
            frame,
            text="Website Extractor Prompt Generator",
            font=("TkDefaultFont", 18, "bold"),
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(
            frame,
            text="Fill the website details. The generated prompt can be edited before copying.",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 14))

        ttk.Label(frame, text="Website name (optional; inferred if empty)").grid(
            row=3, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(frame, textvariable=self.prompt_website_name_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=4
        )
        ttk.Label(frame, text="Homepage/archive URL").grid(
            row=4, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(frame, textvariable=self.prompt_homepage_var).grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=4
        )
        ttk.Label(frame, text="Example article URL *").grid(
            row=5, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(frame, textvariable=self.prompt_example_var).grid(
            row=5, column=1, columnspan=2, sticky="ew", pady=4
        )
        ttk.Label(frame, text="Comments").grid(
            row=6, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Combobox(
            frame,
            textvariable=self.prompt_comments_var,
            values=COMMENT_OPTIONS,
            state="readonly",
        ).grid(row=6, column=1, columnspan=2, sticky="ew", pady=4)

        preview_frame = ttk.LabelFrame(frame, text="Editable prompt preview", padding=8)
        preview_frame.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(14, 8))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.prompt_preview = tk.Text(preview_frame, wrap="word", height=18, undo=True)
        self.prompt_preview.grid(row=0, column=0, sticky="nsew")
        preview_scrollbar = ttk.Scrollbar(
            preview_frame, orient="vertical", command=self.prompt_preview.yview
        )
        preview_scrollbar.grid(row=0, column=1, sticky="ns")
        self.prompt_preview.configure(yscrollcommand=preview_scrollbar.set)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=8, column=0, columnspan=3, sticky="ew")
        button_frame.columnconfigure((0, 1), weight=1)
        self.generate_prompt_button = ttk.Button(
            button_frame, text="Generate prompt", command=self._generate_prompt
        )
        self.generate_prompt_button.grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        self.copy_prompt_button = ttk.Button(
            button_frame, text="Copy prompt", command=self._copy_prompt, state="disabled"
        )
        self.copy_prompt_button.grid(
            row=0, column=1, sticky="ew", padx=(4, 0)
        )
        ttk.Label(frame, textvariable=self.prompt_status_var).grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )

    @staticmethod
    def _validated_optional_url(value: str, label: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return ""
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ExtractionError(f"{label} must be a complete http:// or https:// URL.")
        return cleaned

    def _generate_prompt(self) -> None:
        try:
            homepage = self._validated_optional_url(
                self.prompt_homepage_var.get(), "Homepage/archive URL"
            )
            example = self._validated_optional_url(
                self.prompt_example_var.get(), "Example article URL"
            )
            if not example:
                raise ExtractionError("Example article URL is required.")
            if self.prompt_comments_var.get() not in COMMENT_OPTIONS:
                raise ExtractionError("Select how this website's comments should be handled.")
        except ExtractionError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        comments_option = self.prompt_comments_var.get()
        supplied_name = self.prompt_website_name_var.get().strip()
        self.generate_prompt_button.configure(state="disabled")
        self.copy_prompt_button.configure(state="disabled")
        self.prompt_status_var.set("Inferring the website name and generating the prompt…")
        threading.Thread(
            target=self._prompt_generation_worker,
            args=(supplied_name, homepage, example, comments_option),
            daemon=True,
            name="website-prompt-generator",
        ).start()
        self.root.after(50, self._poll_prompt_generation)

    def _prompt_generation_worker(
        self,
        supplied_name: str,
        homepage: str,
        example: str,
        comments_option: str,
    ) -> None:
        website_name = supplied_name or infer_website_name(example, homepage)
        prompt = build_website_extension_prompt(
            website_name, homepage, example, comments_option
        )
        self._prompt_results.put((website_name, not bool(supplied_name), prompt))

    def _poll_prompt_generation(self) -> None:
        try:
            website_name, was_inferred, prompt = self._prompt_results.get_nowait()
        except queue.Empty:
            self.root.after(50, self._poll_prompt_generation)
            return
        self._finish_prompt_generation(website_name, was_inferred, prompt)

    def _finish_prompt_generation(
        self, website_name: str, was_inferred: bool, prompt: str
    ) -> None:
        if not hasattr(self, "prompt_preview") or not self.prompt_preview.winfo_exists():
            return
        self._last_inferred_website_name = website_name if was_inferred else ""
        self.prompt_website_name_var.set(website_name)
        self.prompt_preview.delete("1.0", "end")
        self.prompt_preview.insert("1.0", prompt)
        self.prompt_status_var.set("Prompt generated. You can edit it before copying.")
        self.generate_prompt_button.configure(state="normal")
        self.copy_prompt_button.configure(state="normal")
        self._save_prompt_draft()

    def _copy_prompt(self) -> None:
        prompt = self.prompt_preview.get("1.0", "end-1c").strip()
        if not prompt:
            messagebox.showerror(
                APP_NAME, "Generate the prompt before copying it.", parent=self.root
            )
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(prompt)
        self.root.update_idletasks()
        self.prompt_status_var.set("Prompt copied to the clipboard.")

    def _installed_websites(self) -> tuple[WebsiteExtractorDefinition, ...]:
        return tuple(
            website
            for website in WEBSITE_EXTRACTORS
            if website.key not in self._removed_website_keys
        )

    def _selected_website(self) -> WebsiteExtractorDefinition | None:
        selected_name = self.website_var.get()
        return next(
            (
                website
                for website in self._installed_websites()
                if website.display_name == selected_name
            ),
            None,
        )

    def _website_selected(self, _event: object = None) -> None:
        website = self._selected_website()
        if website is None:
            self.open_extractor_button.configure(state="disabled")
            self.delete_website_button.configure(state="disabled")
            self.website_description.configure(text="")
            return
        self.open_extractor_button.configure(state="normal")
        self.delete_website_button.configure(state="normal")
        self.website_description.configure(
            text=f"{website.description}\nWebsite: {website.homepage}"
        )

    def _open_selected_extractor(self) -> None:
        website = self._selected_website()
        if website is None:
            messagebox.showerror(APP_NAME, "Select a website first.", parent=self.root)
            return
        self.active_website = website
        self.url_var.set("")
        self.status_var.set("Ready")
        self._build_extractor(website)

    def _delete_selected_website(self) -> None:
        website = self._selected_website()
        if website is None:
            messagebox.showerror(APP_NAME, "Select a website first.", parent=self.root)
            return
        confirmed = messagebox.askyesno(
            "Permanently delete website",
            (
                f"Permanently delete {website.display_name!r} and its dedicated extractor code?\n\n"
                "This edits article_extractor_gui.py itself. Code shared with other installed "
                "websites is preserved. Existing extraction folders are not deleted.\n\n"
                "The website can only be restored by reinstalling the script or restoring it "
                "from GitHub."
            ),
            icon="warning",
            default="no",
            parent=self.root,
        )
        if not confirmed:
            return
        installed = self._installed_websites()
        try:
            permanently_remove_website_source(website, installed)
        except WebsiteRemovalError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        self._removed_website_keys.add(website.key)
        self._build_website_selector()
        messagebox.showinfo(
            APP_NAME,
            (
                f"{website.display_name} and its dedicated extractor code were deleted.\n\n"
                "Existing extraction folders were left untouched."
            ),
            parent=self.root,
        )

    def _build_extractor(self, website: WebsiteExtractorDefinition) -> None:
        self.root.title(f"{website.display_name} Extractor")
        frame = self._new_frame()
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(9, weight=1)

        self.back_button = ttk.Button(
            frame, text="← Back to websites", command=self._build_website_selector
        )
        self.back_button.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        ttk.Label(
            frame,
            text=f"{website.display_name} Extractor",
            font=("TkDefaultFont", 18, "bold"),
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(frame, text=website.description, wraplength=700).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        ttk.Label(frame, text=f"Example: {website.example_url}", wraplength=700).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )

        ttk.Label(frame, text="Article URL").grid(row=4, column=0, sticky="w", padx=(0, 10))
        self.url_entry = ttk.Entry(frame, textvariable=self.url_var)
        self.url_entry.grid(row=4, column=1, columnspan=2, sticky="ew")

        ttk.Label(frame, text="Output folder").grid(
            row=5, column=0, sticky="w", padx=(0, 10), pady=(12, 0)
        )
        ttk.Entry(frame, textvariable=self.output_var).grid(
            row=5, column=1, sticky="ew", pady=(12, 0)
        )
        ttk.Button(frame, text="Choose…", command=self._choose_folder).grid(
            row=5, column=2, padx=(8, 0), pady=(12, 0)
        )

        ttk.Checkbutton(
            frame,
            text="Download media files when a direct file is available",
            variable=self.download_media_var,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))

        action_label = (
            "Extract article and comments" if website.extracts_comments else "Extract article"
        )
        self.extract_button = ttk.Button(frame, text=action_label, command=self._start)
        self.extract_button.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(16, 8))
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=8, column=0, columnspan=3, sticky="ew")

        self.log = tk.Text(frame, height=12, wrap="word", state="disabled")
        self.log.grid(row=9, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=9, column=3, sticky="ns", pady=(10, 0))
        self.log.configure(yscrollcommand=scrollbar.set)
        ttk.Label(frame, textvariable=self.status_var).grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        self.url_entry.focus_set()

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(
            title="Choose extraction output folder", initialdir=self.output_var.get()
        )
        if selected:
            self.output_var.set(selected)

    def _append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.status_var.set(message)

    def _thread_progress(self, message: str) -> None:
        self.root.after(0, self._append_log, message)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.extract_button.configure(state="disabled" if busy else "normal")
        self.back_button.configure(state="disabled" if busy else "normal")
        if busy:
            self.progress.start(10)
        else:
            self.progress.stop()

    def _start(self) -> None:
        if self._busy:
            return
        url = self.url_var.get().strip()
        output = Path(self.output_var.get()).expanduser()
        try:
            if self.active_website is None:
                raise ExtractionError("Go back and select a website extractor first.")
            url = self.active_website.validate_url(url)
            if not output.is_dir():
                raise ExtractionError("Choose an existing output folder.")
        except ExtractionError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return
        self._append_log(f"Starting: {url}")
        self._set_busy(True)
        website = self.active_website
        download_media = self.download_media_var.get()
        threading.Thread(
            target=self._worker,
            args=(website, url, output, download_media),
            daemon=True,
            name="article-extractor",
        ).start()

    def _worker(
        self,
        website: WebsiteExtractorDefinition,
        url: str,
        output: Path,
        download_media: bool,
    ) -> None:
        try:
            article, output_dir, files = extract_to_folder(
                url,
                output,
                website.make_adapter(),
                self._thread_progress,
                download_media=download_media,
            )
        except Exception as exc:
            self.root.after(0, self._finish_error, str(exc))
            return
        self.root.after(0, self._finish_success, article, output_dir, len(files))

    def _finish_error(self, error: str) -> None:
        self._set_busy(False)
        self._append_log(f"Error: {error}")
        messagebox.showerror(APP_NAME, error, parent=self.root)

    def _finish_success(
        self, article: ExtractedArticle, output_dir: Path, file_count: int
    ) -> None:
        self._set_busy(False)
        details = f"{len(article.media)} media placeholders"
        if self.active_website and self.active_website.extracts_comments:
            details = f"{len(article.comments)} comments and {details}"
        message = (
            f"Done: {article.title}\n"
            f"Wrote {file_count} files, including {details}, to {output_dir}"
        )
        self._append_log(message.replace("\n", " — "))
        messagebox.showinfo(APP_NAME, message, parent=self.root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Extract one URL without opening the GUI")
    parser.add_argument("--output", type=Path, help="Parent output folder for --url")
    parser.add_argument(
        "--website",
        choices=sorted(WEBSITE_EXTRACTORS_BY_KEY),
        help="Explicit website extractor key for --url",
    )
    parser.add_argument(
        "--download-media",
        action="store_true",
        help="Download direct image/audio/video files into the extraction folder",
    )
    args = parser.parse_args(argv)
    if bool(args.url) != bool(args.output):
        parser.error("--url and --output must be supplied together")
    if args.url and not args.website:
        parser.error("--website is required with --url; extractor selection is never automatic")
    if args.url and args.output:
        website = WEBSITE_EXTRACTORS_BY_KEY[args.website]
        try:
            url = website.validate_url(args.url)
            article, output_dir, files = extract_to_folder(
                url,
                args.output.expanduser(),
                website.make_adapter(),
                print,
                download_media=args.download_media,
            )
        except ExtractionError as exc:
            parser.exit(1, f"Error: {exc}\n")
        print(
            f"Exported {article.title!r}: {len(files)} files in {output_dir}"
        )
        return 0

    root = tk.Tk()
    ExtractorGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
