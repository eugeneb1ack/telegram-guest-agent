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


# Transport contract, injected on every turn (including resumed native sessions).
# https://core.telegram.org/bots/api#rich-message-formatting-options
# Editorial defaults below are deliberately narrower than Telegram's API limits.
RICH_TEXT_POLICY = """Telegram Rich Text formatting policy:
The sidecar delivers your final answer as native Telegram Rich Message blocks,
including real tables. Do not claim Telegram cannot display tables. Return the
answer itself in the supported Markdown below; the sidecar sends it. Do not use
Userbot to post a second copy unless the owner explicitly asks for another send.
Choose structure for the reader, never to demonstrate every available effect.
Lead with the answer. A short reply needs plain prose, not a title and sections.
Separate prose paragraphs with a blank line. For longer answers, use meaningful
## headings and ### subsections without skipping levels; # is an optional title.
Use **bold** for a few important words, *italic* for gentle emphasis, ~~strike~~
for an actual correction, ==marked== only for a critical result. Never emphasize
whole paragraphs. <u>, <sub>, <sup> are available for meaningful inline uses.
Use a native Markdown table when comparing multiple alternatives by the SAME
fields, reporting repeated measurements, prices, limits or a compact schedule,
or when explicitly requested. Prefer 2-4 short columns, one clear header row,
short cell values and units in headers. A header separator row is mandatory:
| Option | Cost |\n|:---|---:|\n| A | 100 ₽ |\n| B | 200 ₽ |
Never wrap an actual table in a code fence or simulate it with spaces/box art.
Escape a literal pipe outside code as \\|; inside code cells use `a|b` directly.
Only inline formatting belongs inside cells; put
long explanations below the table. Never exceed 20 columns; split wide data
into focused tables or use per-item lists. Mobile readability takes priority.
For steps use 1. / 2. ordered lists; for parallel facts use - bullets; use
- [ ] / - [x] only for tasks with a known completion state. Indent nested items
by two spaces. Do not turn an ordinary explanation into a long checklist.
Use `inline code` for identifiers/commands, and fenced code with a language for
multiline code, exact logs or literal examples. Keep code literal, with no
decorative bold, links or HTML inside it. Escape Markdown punctuation with a
backslash when literal text would otherwise become formatting.
Use [descriptive label](https://example.com) for actual sources; preserve exact
destinations, encode spaces/parentheses in URLs. Never invent citations. Footnotes
use [^id] and a separate [^id]: Source text line. Avoid bare URL walls.
Use > only for a real quotation and name its source in surrounding prose.
Use <blockquote expandable> on its own line, then quoted text, then
</blockquote> on its own line for a long optional quotation. It is not a heading.
For optional background use <details><summary>Descriptive label</summary>, then
a blank line, supported Markdown paragraphs/lists/tables, then </details> on
its own line. Omit open to start collapsed; open means initially expanded.
Keep the answer, recommendation and essential caveats outside collapsed blocks.
Use ||spoiler|| only for intentionally hidden words; it does not collapse space.
Use --- sparingly for a real topic break. Display math uses $$formula$$ on its
own line. Inline $math$ is not supported by this renderer; currency stays text.
Media: a standalone ![label](https://example.com/photo.jpg) embeds a real image;
an optional quoted title is its caption. Use only verified relevant media.
The same syntax recognizes GIF animation, MP4/MOV/WebM video, MP3/M4A/AAC/FLAC
audio, OGG/OGA/Opus voice notes and PDF/ZIP documents by their URL path suffix.
Other downloadable documents should be ordinary labelled links or generated
local files delivered through MEDIA. Collages/slideshows and maps are available
only when requested and useful: put standalone media lines inside <tg-collage>
or <tg-slideshow> on separate lines; maps use a standalone
<tg-map lat="41.9" long="12.5" zoom="14"/> with verified coordinates.
Generated local files must follow the separate MEDIA delivery contract. Never
invent file IDs, attach unrelated media or replace a readable table with an image.
Supported output is this Markdown subset plus the inline tags and details/quote
tags described above. Do not emit full HTML documents, HTML tables/lists, CSS,
raw API JSON, buttons or widgets; those require separate transport capabilities.
Before answering, check table columns/values, paragraph boundaries, closed code
fences/details, exact links and that every formatting choice serves the content.
"""

_FENCE_RE = re.compile(r"^\s*```([A-Za-z0-9_+.-]*)\s*$")
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)([^\n]+?)\1(?!`)")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_HTML_HEADING_RE = re.compile(r"^\s*<h([1-6])>(.*?)</h\1>\s*$", re.IGNORECASE)
_LIST_RE = re.compile(r"^(\s*)([-+*]|\d+[.)])\s+(?:\[([ xX])\]\s+)?(.*)$")
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{1,}:?$")
_FOOTNOTE_RE = re.compile(r"^\[\^([A-Za-z0-9_-]{1,64})\]:\s*(.*)$")
_DETAILS_OPEN_RE = re.compile(r"^\s*<details(?:\s+open)?\s*>\s*$", re.IGNORECASE)
_DETAILS_SUMMARY_OPEN_RE = re.compile(
    r"^\s*<details(?:\s+open)?\s*>\s*<summary>(.*?)</summary>\s*$", re.IGNORECASE,
)
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
    ("escape", re.compile(r"\\([\\`*{}\[\]()#+.!_|~=$<>-])")),
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
    ("code", _CODE_SPAN_RE),
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

        if best_name == "escape":
            _append_text(parts, match.group(1))
        elif best_name == "custom_emoji":
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
            value = match.group(2) if best_name == "code" else _inner(match)
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
    # A pipe inside a matching code span or escaped with a backslash is content.
    source = line.strip()
    cells: list[str] = []
    start = index = 0
    last_delimiter = -1
    while index < len(source):
        if source[index] == "\\" and index + 1 < len(source):
            index += 2
            continue
        if source[index] == "`":
            span = _CODE_SPAN_RE.match(source, index)
            if span:
                index = span.end()
                continue
        if source[index] == "|":
            cells.append(source[start:index].strip())
            start = index + 1
            last_delimiter = index
        index += 1
    cells.append(source[start:].strip())
    if source.startswith("|"):
        cells.pop(0)
    if cells and last_delimiter == len(source) - 1:
        cells.pop()
    # GFM table escapes apply before inline code parsing, too: `a\|b` -> a|b.
    return [cell.replace(r"\|", "|") for cell in cells]


def _is_table(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines) or "|" not in lines[index] or "|" not in lines[index + 1]:
        return False
    cells = _split_table_row(lines[index + 1])
    return bool(cells) and len(cells) == len(_split_table_row(lines[index])) and all(
        _TABLE_SEPARATOR_CELL_RE.fullmatch(cell.replace(" ", "")) for cell in cells
    )


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
    if suffix.endswith((".pdf", ".zip")):
        return "document"
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
        or _DETAILS_SUMMARY_OPEN_RE.match(line)
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


def _render_blocks(text: str, depth: int = 0) -> list[dict[str, Any]]:
    if depth >= 8:
        return [{"type": "pre", "text": text}]
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
            blocks.append({"type": "heading", "text": parse_inline(value), "size": size})
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
                    "blocks": _render_blocks(body, depth + 1) or [{"type": "paragraph", "text": "…"}],
                }
            )
            if re.match(r"\s*<details\s+open\s*>", line, re.IGNORECASE):
                blocks[-1]["is_open"] = True
            index += 1
            continue

        summary_open = _DETAILS_SUMMARY_OPEN_RE.match(line)
        if _DETAILS_OPEN_RE.match(line) or summary_open:
            body: list[str] = []
            summary = summary_open.group(1) if summary_open else "Подробности"
            nesting = 0
            index += 1
            while index < len(lines):
                current = lines[index].strip()
                if current.lower() == "</details>" and nesting == 0:
                    break
                summary_match = _SUMMARY_RE.match(lines[index])
                if summary_match and nesting == 0:
                    summary = summary_match.group(1)
                else:
                    body.append(lines[index])
                nesting += len(re.findall(r"<details(?:\s+open)?\s*>", current, re.IGNORECASE))
                nesting -= len(re.findall(r"</details>", current, re.IGNORECASE))
                index += 1
            if index < len(lines):
                index += 1
            blocks.append(
                {
                    "type": "details",
                    "summary": parse_inline(summary),
                    "blocks": _render_blocks("\n".join(body), depth + 1) or [{"type": "paragraph", "text": "…"}],
                }
            )
            if re.match(r"\s*<details\s+open\s*>", line, re.IGNORECASE):
                blocks[-1]["is_open"] = True
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
            start = index
            raw_rows = [_split_table_row(lines[index])]
            separators = _split_table_row(lines[index + 1])
            alignments = [
                "center" if c.startswith(":") and c.endswith(":")
                else "right" if c.endswith(":") else "left"
                for c in separators
            ]
            index += 2
            while (
                index < len(lines) and "|" in lines[index] and lines[index].strip()
                and not _special_start(lines, index)
            ):
                raw_rows.append(_split_table_row(lines[index]))
                index += 1
            width = max(len(row) for row in raw_rows)
            if width > 20:
                blocks.append({"type": "pre", "text": "\n".join(lines[start:index])})
                continue
            alignments.extend(["left"] * (width - len(alignments)))
            cells: list[list[dict[str, Any]]] = []
            for row_index, row in enumerate(raw_rows):
                padded = row + [""] * (width - len(row))
                cells.append(
                    [
                        {
                            "text": parse_inline(cell),
                            "is_header": True,
                            "align": alignments[column],
                            "valign": "top",
                        }
                        if row_index == 0
                        else {"text": parse_inline(cell), "align": alignments[column], "valign": "top"}
                        for column, cell in enumerate(padded)
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
                    "blocks": _render_blocks("\n".join(quoted), depth + 1) or [{"type": "paragraph", "text": "…"}],
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
            if block.get("type") == "table":
                rows = block.get("cells") or []
                block["cells"] = rows[:budget[0]]
                budget[0] -= len(block["cells"])
                if not block["cells"]:
                    continue
            if isinstance(block.get("blocks"), list):
                block["blocks"] = visit(block["blocks"])
                if not block["blocks"]:
                    continue
            if isinstance(block.get("items"), list):
                items = []
                for item in block["items"]:
                    if budget[0] <= 1:
                        break
                    budget[0] -= 1  # List items also count towards Telegram's 500.
                    copied = dict(item)
                    copied["blocks"] = visit(copied.get("blocks") or [])
                    items.append(copied)
                block["items"] = items
                if not items:
                    continue
            limited.append(block)
        return limited

    return visit(blocks)


def render_blocks(text: str, maximum: int = 500) -> list[dict[str, Any]]:
    blocks = _limit_blocks(_render_blocks(text.strip()), maximum)
    return blocks or [{"type": "paragraph", "text": "Не удалось сформировать ответ."}]
