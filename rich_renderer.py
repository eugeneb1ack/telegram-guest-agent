"""Conservative Markdown/HTML to Telegram Bot API 10.3 rich blocks.

The renderer intentionally supports a useful, predictable subset. Unknown
syntax stays readable as plain text instead of being sent to Telegram's parser
and potentially changing meaning.
"""
from __future__ import annotations

import html
import re
import urllib.parse
from typing import Any


_FENCE_RE = re.compile(r"^\s*```([A-Za-z0-9_+.-]*)\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_HTML_HEADING_RE = re.compile(r"^\s*<h([1-6])>(.*?)</h\1>\s*$", re.IGNORECASE)
_LIST_RE = re.compile(r"^(\s*)([-+*]|\d+[.)])\s+(?:\[([ xX])\]\s+)?(.*)$")
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{1,}:?$")
_FOOTNOTE_RE = re.compile(r"^\[\^([A-Za-z0-9_-]{1,64})\]:\s*(.*)$")
_DETAILS_OPEN_RE = re.compile(r"^\s*<details(?:\s+open)?\s*>\s*$", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"^\s*<summary>(.*?)</summary>\s*$", re.IGNORECASE)
_DETAILS_INLINE_RE = re.compile(
    r"^\s*<details(?:\s+open)?\s*>\s*<summary>(.*?)</summary>(.*?)</details>\s*$",
    re.IGNORECASE | re.DOTALL,
)
_MEDIA_LINE_RE = re.compile(
    r'^\s*!\[([^\]]*)\]\((https?://[^\s)]+)(?:\s+["\']([^"\']*)["\'])?\)\s*$',
    re.IGNORECASE,
)
_MAP_RE = re.compile(r"^\s*<tg-map\s+([^>]+)/>\s*$", re.IGNORECASE)
_ATTR_RE = re.compile(r"([A-Za-z_][\w-]*)\s*=\s*[\"']([^\"']*)[\"']")
_COLLAGE_OPEN_RE = re.compile(r"^\s*<tg-(collage|slideshow)>\s*$", re.IGNORECASE)
_EXPANDABLE_BLOCKQUOTE_OPEN_RE = re.compile(
    r"^\s*<blockquote\s+expandable\s*>\s*$",
    re.IGNORECASE,
)
_EXPANDABLE_BLOCKQUOTE_INLINE_RE = re.compile(
    r"^\s*<blockquote\s+expandable\s*>(.*?)</blockquote>\s*$",
    re.IGNORECASE | re.DOTALL,
)


_INLINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "custom_emoji",
        re.compile(
            r'<tg-emoji\s+emoji-id=["\']([1-9]\d*)["\']\s*>(.*?)</tg-emoji>',
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "html_link",
        re.compile(
            r'<a\s+href=["\']([^"\']+)["\']\s*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    ("bold_html", re.compile(r"<(?:b|strong)>(.*?)</(?:b|strong)>", re.IGNORECASE | re.DOTALL)),
    ("italic_html", re.compile(r"<(?:i|em)>(.*?)</(?:i|em)>", re.IGNORECASE | re.DOTALL)),
    ("underline_html", re.compile(r"<(?:u|ins)>(.*?)</(?:u|ins)>", re.IGNORECASE | re.DOTALL)),
    (
        "strike_html",
        re.compile(r"<(?:s|strike|del)>(.*?)</(?:s|strike|del)>", re.IGNORECASE | re.DOTALL),
    ),
    ("code_html", re.compile(r"<code>(.*?)</code>", re.IGNORECASE | re.DOTALL)),
    ("marked_html", re.compile(r"<mark>(.*?)</mark>", re.IGNORECASE | re.DOTALL)),
    ("subscript_html", re.compile(r"<sub>(.*?)</sub>", re.IGNORECASE | re.DOTALL)),
    ("superscript_html", re.compile(r"<sup>(.*?)</sup>", re.IGNORECASE | re.DOTALL)),
    ("spoiler_html", re.compile(r"<tg-spoiler>(.*?)</tg-spoiler>", re.IGNORECASE | re.DOTALL)),
    (
        "markdown_link",
        re.compile(r"(?<!!)\[([^\]\n]+)\]\((https?://[^\s)]+|mailto:[^\s)]+|tel:[^\s)]+)\)"),
    ),
    ("code", re.compile(r"`([^`\n]+)`")),
    ("bold", re.compile(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__")),
    ("strike", re.compile(r"~~([^~\n]+)~~")),
    ("spoiler", re.compile(r"\|\|([^|\n]+)\|\|")),
    ("marked", re.compile(r"==([^=\n]+)==")),
    ("reference_link", re.compile(r"\[\^([A-Za-z0-9_-]{1,64})\]")),
    ("italic", re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<![\w_])_([^_\n]+)_(?![\w_])")),
)


def safe_truncate(text: str, limit: int) -> str:
    """Truncate at a readable boundary; block parsing repairs open structures."""
    if limit <= 1:
        return "…"[: max(0, limit)]
    if len(text) <= limit:
        return text
    target = limit - 1
    minimum = int(target * 0.6)
    cut = -1
    for marker in ("\n\n", "\n", ". ", "; ", ", ", " "):
        candidate = text.rfind(marker, minimum, target)
        if candidate > cut:
            cut = candidate + (1 if marker.endswith(" ") else 0)
    if cut < minimum:
        cut = target
    return text[:cut].rstrip() + "…"


def _append_text(parts: list[Any], value: str) -> None:
    value = html.unescape(value)
    if not value:
        return
    if parts and isinstance(parts[-1], str):
        parts[-1] += value
    else:
        parts.append(value)


def _inner(match: re.Match[str]) -> str:
    for value in match.groups():
        if value is not None:
            return value
    return ""


def _valid_link(url: str) -> bool:
    parsed = urllib.parse.urlparse(html.unescape(url))
    return parsed.scheme.lower() in {"http", "https", "mailto", "tel"}


def parse_inline(text: str, depth: int = 0) -> Any:
    """Return a Telegram RichText string, array, or typed object."""
    if not text:
        return ""
    if depth >= 8:
        return html.unescape(text)
    parts: list[Any] = []
    pos = 0
    while pos < len(text):
        best_name = ""
        best_match: re.Match[str] | None = None
        best_order = len(_INLINE_PATTERNS)
        for order, (name, pattern) in enumerate(_INLINE_PATTERNS):
            match = pattern.search(text, pos)
            if match is None:
                continue
            if best_match is None or (match.start(), order) < (best_match.start(), best_order):
                best_name, best_match, best_order = name, match, order
        if best_match is None:
            _append_text(parts, text[pos:])
            break
        _append_text(parts, text[pos : best_match.start()])
        match = best_match

        if best_name == "custom_emoji":
            parts.append(
                {
                    "type": "custom_emoji",
                    "custom_emoji_id": match.group(1),
                    "alternative_text": html.unescape(re.sub(r"<[^>]+>", "", match.group(2))) or "🙂",
                }
            )
        elif best_name in {"html_link", "markdown_link"}:
            if best_name == "html_link":
                url, label = match.group(1), match.group(2)
            else:
                label, url = match.group(1), match.group(2)
            if _valid_link(url):
                parts.append({"type": "url", "text": parse_inline(label, depth + 1), "url": html.unescape(url)})
            else:
                _append_text(parts, label)
        elif best_name == "reference_link":
            name = match.group(1)
            parts.append({"type": "reference_link", "text": f"[{name}]", "reference_name": name})
        else:
            style_map = {
                "bold_html": "bold",
                "italic_html": "italic",
                "underline_html": "underline",
                "strike_html": "strikethrough",
                "code_html": "code",
                "marked_html": "marked",
                "subscript_html": "subscript",
                "superscript_html": "superscript",
                "spoiler_html": "spoiler",
                "bold": "bold",
                "strike": "strikethrough",
                "spoiler": "spoiler",
                "marked": "marked",
                "italic": "italic",
                "code": "code",
            }
            value = next((group for group in match.groups() if group is not None), "")
            kind = style_map[best_name]
            nested = html.unescape(value) if kind == "code" else parse_inline(value, depth + 1)
            parts.append({"type": kind, "text": nested})
        pos = match.end()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return parts


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.replace(r"\|", "|").strip() for cell in re.split(r"(?<!\\)\|", stripped)]


def _is_table(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines) or "|" not in lines[index] or "|" not in lines[index + 1]:
        return False
    cells = _split_table_row(lines[index + 1])
    return bool(cells) and all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell.replace(" ", "")) for cell in cells)


def _media_input(kind: str, source: str) -> dict[str, Any]:
    return {"type": kind, "media": source}


def media_block(kind: str, source: str, caption: str = "") -> dict[str, Any]:
    """Build an InputRichBlock media object from a URL or Telegram file_id."""
    field = {
        "photo": "photo",
        "video": "video",
        "animation": "animation",
        "audio": "audio",
        "document": "document",
        "voice_note": "voice_note",
    }[kind]
    block: dict[str, Any] = {"type": kind, field: _media_input(kind, source)}
    if caption.strip():
        block["caption"] = {"text": parse_inline(caption.strip())}
    return block


def _media_kind(url: str) -> str:
    suffix = urllib.parse.urlparse(url).path.lower()
    if suffix.endswith(".gif"):
        return "animation"
    if suffix.endswith((".mp4", ".mov", ".webm")):
        return "video"
    if suffix.endswith((".mp3", ".m4a", ".aac", ".flac")):
        return "audio"
    if suffix.endswith((".ogg", ".oga", ".opus")):
        return "voice_note"
    return "photo"


def _special_start(lines: list[str], index: int) -> bool:
    line = lines[index]
    return bool(
        _FENCE_RE.match(line)
        or _HEADING_RE.match(line)
        or _HTML_HEADING_RE.match(line)
        or _LIST_RE.match(line)
        or _FOOTNOTE_RE.match(line)
        or _DETAILS_INLINE_RE.match(line)
        or _DETAILS_OPEN_RE.match(line)
        or _COLLAGE_OPEN_RE.match(line)
        or _EXPANDABLE_BLOCKQUOTE_INLINE_RE.match(line)
        or _EXPANDABLE_BLOCKQUOTE_OPEN_RE.match(line)
        or _MEDIA_LINE_RE.match(line)
        or _MAP_RE.match(line)
        or line.strip() in {"---", "***", "___", "$$"}
        or line.lstrip().startswith(">")
        or _is_table(lines, index)
    )


def _parse_list(lines: list[str], start: int) -> tuple[dict[str, Any], int]:
    first = _LIST_RE.match(lines[start])
    assert first is not None
    base_indent = len(first.group(1).replace("\t", "    "))
    items: list[dict[str, Any]] = []
    index = start
    while index < len(lines):
        match = _LIST_RE.match(lines[index])
        if match is None:
            break
        indent = len(match.group(1).replace("\t", "    "))
        if indent < base_indent:
            break
        if indent > base_indent:
            if items:
                nested, index = _parse_list(lines, index)
                items[-1]["blocks"].append(nested)
                continue
            break
        marker, checkbox, body = match.group(2), match.group(3), match.group(4)
        item: dict[str, Any] = {"blocks": [{"type": "paragraph", "text": parse_inline(body)}]}
        if checkbox is not None:
            item["has_checkbox"] = True
            if checkbox.lower() == "x":
                item["is_checked"] = True
        if marker[0].isdigit():
            item["value"] = int(re.match(r"\d+", marker).group(0))  # type: ignore[union-attr]
            item["type"] = "1"
        items.append(item)
        index += 1
    return {"type": "list", "items": items}, index


def _parse_attrs(raw: str) -> dict[str, str]:
    return {key.lower(): value for key, value in _ATTR_RE.findall(raw)}


def _render_blocks(text: str) -> list[dict[str, Any]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            language = fence.group(1)
            body: list[str] = []
            index += 1
            while index < len(lines) and not _FENCE_RE.match(lines[index]):
                body.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            block: dict[str, Any] = {"type": "pre", "text": "\n".join(body)}
            if language:
                block["language"] = language
            blocks.append(block)
            continue

        heading = _HEADING_RE.match(line)
        html_heading = _HTML_HEADING_RE.match(line)
        if heading or html_heading:
            size = int(html_heading.group(1)) if html_heading else len(heading.group(1))  # type: ignore[union-attr]
            value = html_heading.group(2) if html_heading else heading.group(2)  # type: ignore[union-attr]
            blocks.append({"type": "heading", "text": parse_inline(value), "size": max(2, min(6, size))})
            index += 1
            continue

        if stripped in {"---", "***", "___"}:
            blocks.append({"type": "divider"})
            index += 1
            continue

        if stripped.startswith("$$"):
            if stripped != "$$" and stripped.endswith("$$") and len(stripped) > 4:
                expression = stripped[2:-2].strip()
                index += 1
            else:
                body = []
                index += 1
                while index < len(lines) and lines[index].strip() != "$$":
                    body.append(lines[index])
                    index += 1
                if index < len(lines):
                    index += 1
                expression = "\n".join(body).strip()
            if expression:
                blocks.append({"type": "mathematical_expression", "expression": expression})
            continue

        footnote = _FOOTNOTE_RE.match(line)
        if footnote:
            name, value = footnote.groups()
            blocks.append(
                {
                    "type": "footer",
                    "text": {"type": "reference", "text": parse_inline(value), "name": name},
                }
            )
            index += 1
            continue

        inline_expandable_quote = _EXPANDABLE_BLOCKQUOTE_INLINE_RE.match(line)
        if inline_expandable_quote:
            blocks.append(
                {
                    "type": "expandable_blockquote",
                    "text": parse_inline(inline_expandable_quote.group(1).strip()),
                }
            )
            index += 1
            continue

        if _EXPANDABLE_BLOCKQUOTE_OPEN_RE.match(line):
            body: list[str] = []
            index += 1
            while index < len(lines) and lines[index].strip().lower() != "</blockquote>":
                body.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(
                {
                    "type": "expandable_blockquote",
                    "text": parse_inline("\n".join(body).strip() or "…"),
                }
            )
            continue

        inline_details = _DETAILS_INLINE_RE.match(line)
        if inline_details:
            summary, body = inline_details.groups()
            blocks.append(
                {
                    "type": "details",
                    "summary": parse_inline(summary),
                    "blocks": _render_blocks(body) or [{"type": "paragraph", "text": "…"}],
                }
            )
            index += 1
            continue

        if _DETAILS_OPEN_RE.match(line):
            body: list[str] = []
            summary = "Подробности"
            index += 1
            while index < len(lines) and lines[index].strip().lower() != "</details>":
                summary_match = _SUMMARY_RE.match(lines[index])
                if summary_match:
                    summary = summary_match.group(1)
                else:
                    body.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(
                {
                    "type": "details",
                    "summary": parse_inline(summary),
                    "blocks": _render_blocks("\n".join(body)) or [{"type": "paragraph", "text": "…"}],
                }
            )
            continue

        collage = _COLLAGE_OPEN_RE.match(line)
        if collage:
            kind = collage.group(1).lower()
            children: list[dict[str, Any]] = []
            closing = f"</tg-{kind}>"
            index += 1
            while index < len(lines) and lines[index].strip().lower() != closing:
                media = _MEDIA_LINE_RE.match(lines[index])
                if media:
                    alt, url, title = media.groups()
                    children.append(media_block(_media_kind(url), url, title or alt))
                index += 1
            if index < len(lines):
                index += 1
            if children:
                blocks.append({"type": kind, "blocks": children})
            continue

        media = _MEDIA_LINE_RE.match(line)
        if media:
            alt, url, title = media.groups()
            blocks.append(media_block(_media_kind(url), url, title or alt))
            index += 1
            continue

        map_match = _MAP_RE.match(line)
        if map_match:
            attrs = _parse_attrs(map_match.group(1))
            try:
                latitude = float(attrs.get("lat") or attrs["latitude"])
                longitude = float(attrs.get("long") or attrs.get("lon") or attrs["longitude"])
                zoom = max(0, min(24, int(attrs.get("zoom", "14"))))
            except (KeyError, TypeError, ValueError):
                latitude = longitude = None
            if latitude is not None and longitude is not None:
                blocks.append(
                    {
                        "type": "map",
                        "location": {"latitude": latitude, "longitude": longitude},
                        "zoom": zoom,
                        "width": 600,
                        "height": 340,
                    }
                )
            else:
                blocks.append({"type": "paragraph", "text": html.unescape(stripped)})
            index += 1
            continue

        if _is_table(lines, index):
            raw_rows = [_split_table_row(lines[index])]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                raw_rows.append(_split_table_row(lines[index]))
                index += 1
            width = max(len(row) for row in raw_rows)
            cells: list[list[dict[str, Any]]] = []
            for row_index, row in enumerate(raw_rows):
                padded = row + [""] * (width - len(row))
                cells.append(
                    [
                        {
                            "text": parse_inline(cell),
                            "is_header": True,
                            "align": "left",
                            "valign": "top",
                        }
                        if row_index == 0
                        else {"text": parse_inline(cell), "align": "left", "valign": "top"}
                        for cell in padded
                    ]
                )
            blocks.append(
                {
                    "type": "table",
                    "cells": cells,
                    "is_bordered": True,
                    "is_striped": True,
                    "is_compact": True,
                }
            )
            continue

        if _LIST_RE.match(line):
            block, index = _parse_list(lines, index)
            blocks.append(block)
            continue

        if line.lstrip().startswith(">"):
            quoted: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                value = lines[index].lstrip()[1:]
                quoted.append(value[1:] if value.startswith(" ") else value)
                index += 1
            blocks.append(
                {
                    "type": "blockquote",
                    "blocks": _render_blocks("\n".join(quoted)) or [{"type": "paragraph", "text": "…"}],
                }
            )
            continue

        paragraph: list[str] = [line]
        index += 1
        while index < len(lines) and lines[index].strip() and not _special_start(lines, index):
            paragraph.append(lines[index])
            index += 1
        value = "\n".join(paragraph).strip()
        paragraph_html = re.fullmatch(r"<p>(.*)</p>", value, flags=re.IGNORECASE | re.DOTALL)
        if paragraph_html:
            value = paragraph_html.group(1)
        blocks.append({"type": "paragraph", "text": parse_inline(value)})
    return blocks


def _limit_blocks(blocks: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    budget = [max(1, maximum)]

    def visit(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        limited: list[dict[str, Any]] = []
        for value in values:
            if budget[0] <= 0:
                break
            budget[0] -= 1
            block = dict(value)
            if isinstance(block.get("blocks"), list):
                block["blocks"] = visit(block["blocks"])
            if isinstance(block.get("items"), list):
                items = []
                for item in block["items"]:
                    if budget[0] <= 0:
                        break
                    copied = dict(item)
                    copied["blocks"] = visit(copied.get("blocks") or [])
                    items.append(copied)
                block["items"] = items
            limited.append(block)
        return limited

    return visit(blocks)


def render_blocks(text: str, maximum: int = 500) -> list[dict[str, Any]]:
    blocks = _limit_blocks(_render_blocks(text.strip()), maximum)
    return blocks or [{"type": "paragraph", "text": "Не удалось сформировать ответ."}]
