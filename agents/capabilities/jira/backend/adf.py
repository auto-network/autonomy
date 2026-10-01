"""Markdown <-> ADF (Atlassian Document Format) conversion + ticket cleaning.

Jira Cloud's v3 REST API speaks ADF for every rich-text value: descriptions,
comments, and textarea custom fields (which editmeta misleadingly reports as
schema.type=string — a plain-string write is rejected with 400 "Operation value
must be an Atlassian Document"). Everything written goes through
``markdown_to_adf``; everything read comes back through ``adf_to_markdown``.

``process_ticket`` reduces a raw issue GET to the fields an agent needs, with
all ADF already converted to markdown.
"""

from __future__ import annotations

import re
from typing import Any, Callable

# A block-level markdown image: the image is the whole line. Only these can
# become ADF media nodes — mediaSingle is a block node, so an image in the
# middle of a paragraph has no ADF equivalent and stays literal text.
_BLOCK_IMAGE = re.compile(r'^!\[([^\]]*)\]\(([^)\s]+)\)\s*$')


def image_targets(markdown_text: str) -> list[str]:
    """The targets of block-level ``![alt](target)`` lines, in order.
    Callers use this to decide whether media resolution is needed at all
    (and which attachment filenames to resolve) before converting."""
    out = []
    for line in markdown_text.split('\n'):
        m = _BLOCK_IMAGE.match(line.strip())
        if m:
            out.append(m.group(2))
    return out


_TABLE_SEPARATOR = re.compile(r'^\s*\|[\s:|\-]+\|\s*$')
_HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
_LIST_ITEM = re.compile(r'^(?:[*-]\s|\d+\.\s)')
_HARD_BREAK = '\x00'


def _interrupts(lines: list[str], j: int) -> bool:
    """Whether ``lines[j]`` ends the paragraph above it. Only what CommonMark
    lets interrupt a paragraph does: a blank line, a fence, a heading, a quote,
    a bullet, a numbered list starting at 1, a table header with its separator
    row, or a block image. Anything else is a wrapped continuation."""
    line = lines[j]
    stripped = line.strip()
    return (not stripped or stripped.startswith(('```', '>'))
            or bool(_HEADING.match(stripped))
            or line.startswith(('* ', '- ')) or bool(re.match(r'^1\.\s', line))
            or (stripped.startswith('|') and j + 1 < len(lines)
                and bool(_TABLE_SEPARATOR.match(lines[j + 1])))
            or bool(_BLOCK_IMAGE.match(stripped)))


def _continues_item(lines: list[str], j: int) -> bool:
    """Whether ``lines[j]`` continues the list item above it."""
    return not _interrupts(lines, j) and not _LIST_ITEM.match(lines[j])


def _flowed(lines: list[str]) -> list[dict[str, Any]]:
    """Inline content for wrapped lines, as CommonMark reads them: a newline
    inside a paragraph is a space; only a line ending in two spaces or a
    backslash is a hard break. The lines are joined before inline marks are
    parsed, so bold or a link may wrap across them."""
    joined = ''
    for index, raw in enumerate(lines):
        hard = raw.endswith('  ') or raw.rstrip(' ').endswith('\\')
        text = raw.strip()
        if text.endswith('\\'):
            text = text[:-1].rstrip()
        joined += text
        if index < len(lines) - 1:
            joined += _HARD_BREAK if hard else ' '
    nodes: list[dict[str, Any]] = []
    for node in _parse_inline(joined):
        pieces = node.get("text", "").split(_HARD_BREAK)
        for n, piece in enumerate(pieces):
            if n:
                nodes.append({"type": "hardBreak"})
            if piece:
                nodes.append({**node, "text": piece})
    return nodes or [{"type": "text", "text": ""}]


def markdown_to_adf(markdown_text: str,
                    media_resolver: Callable[[str], str | None] | None = None,
                    ) -> dict[str, Any]:
    """Convert markdown to an ADF document node.

    *media_resolver* maps a block-image target (``![alt](target)`` alone on a
    line) to a media-services file UUID, or ``None`` when it can't. Resolved
    images become ``mediaSingle`` nodes — how Jira Cloud renders an attachment
    inline. Unresolved (or with no resolver) the line stays literal text, the
    pre-media behavior, so nothing is ever lost."""

    lines = markdown_text.split('\n')
    content = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Code blocks
        if line.strip().startswith('```'):
            lang_match = re.match(r'^```(\w+)?', line.strip())
            lang = lang_match.group(1) if lang_match and lang_match.group(1) else 'none'

            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code_lines.append(lines[i])
                i += 1

            content.append({
                "type": "codeBlock",
                "attrs": {"language": lang},
                "content": [{"type": "text", "text": '\n'.join(code_lines)}],
            })
            i += 1
            continue

        # Tables: a "| ... |" line followed by a "|---|---|" separator line.
        if (
            line.strip().startswith('|')
            and line.strip().endswith('|')
            and i + 1 < len(lines)
            and _TABLE_SEPARATOR.match(lines[i + 1])
        ):
            def _split_row(raw):
                return [c.strip() for c in raw.strip().strip('|').split('|')]

            def _cell_paragraph(cell_text):
                # ADF forbids empty-text text nodes, so emit an empty paragraph
                # (no content key) when the cell is empty.
                if not cell_text:
                    return {"type": "paragraph"}
                return {"type": "paragraph", "content": _parse_inline(cell_text)}

            header_cells = _split_row(line)
            i += 2  # consume header + separator

            data_rows = []
            while (
                i < len(lines)
                and lines[i].strip().startswith('|')
                and lines[i].strip().endswith('|')
            ):
                data_rows.append(_split_row(lines[i]))
                i += 1

            table_rows = [{
                "type": "tableRow",
                "content": [
                    {"type": "tableHeader", "attrs": {},
                     "content": [_cell_paragraph(cell)]}
                    for cell in header_cells
                ],
            }]
            for row in data_rows:
                # Pad short rows to header width so ADF stays well-formed.
                while len(row) < len(header_cells):
                    row.append('')
                table_rows.append({
                    "type": "tableRow",
                    "content": [
                        {"type": "tableCell", "attrs": {},
                         "content": [_cell_paragraph(cell)]}
                        for cell in row[: len(header_cells)]
                    ],
                })

            content.append({
                "type": "table",
                "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
                "content": table_rows,
            })
            continue

        # Blockquotes: consecutive "> " lines are one quote; a bare ">" line
        # separates its paragraphs, and wrapped lines flow as in a paragraph.
        if line.lstrip().startswith('>'):
            quoted = []
            while i < len(lines) and (
                lines[i].lstrip().startswith('>')
                # A lazy continuation: plain text right after a quoted line
                # stays in the quote, as CommonMark reads it.
                or (quoted and quoted[-1].strip() and not _interrupts(lines, i))
            ):
                quoted.append(re.sub(r'^\s*>\s?', '', lines[i]))
                i += 1
            paragraphs, current = [], []
            for text in quoted + ['']:
                if text.strip():
                    current.append(text)
                elif current:
                    paragraphs.append({"type": "paragraph", "content": _flowed(current)})
                    current = []
            if paragraphs:
                content.append({"type": "blockquote", "content": paragraphs})
            continue

        # Headings
        heading = _HEADING.match(line)
        if heading:
            content.append({"type": "heading", "attrs": {"level": len(heading.group(1))},
                            "content": _parse_inline(heading.group(2))})
        # Numbered lists
        elif re.match(r'^\d+\.\s+', line):
            list_items = []
            while i < len(lines) and re.match(r'^\d+\.\s+', lines[i]):
                item = [re.sub(r'^\d+\.\s+', '', lines[i])]
                i += 1
                while i < len(lines) and _continues_item(lines, i):
                    item.append(lines[i])
                    i += 1
                list_items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _flowed(item)}],
                })
            content.append({"type": "orderedList", "content": list_items})
            continue
        # Bullet lists
        elif line.startswith('* ') or line.startswith('- '):
            list_items = []
            while i < len(lines) and (lines[i].startswith('* ') or lines[i].startswith('- ')):
                item = [lines[i][2:]]
                i += 1
                while i < len(lines) and _continues_item(lines, i):
                    item.append(lines[i])
                    i += 1
                list_items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _flowed(item)}],
                })
            content.append({"type": "bulletList", "content": list_items})
            continue
        elif line.strip() == '':
            i += 1
            continue
        else:
            image = _BLOCK_IMAGE.match(line.strip())
            media_id = (media_resolver(image.group(2))
                        if image and media_resolver else None)
            if media_id:
                attrs: dict[str, Any] = {"type": "file", "id": media_id,
                                         "collection": ""}
                if image.group(1):
                    attrs["alt"] = image.group(1)
                content.append({
                    "type": "mediaSingle",
                    "attrs": {"layout": "center"},
                    "content": [{"type": "media", "attrs": attrs}],
                })
            else:
                # A paragraph runs until a blank line or the next block.
                paragraph = [line]
                while not image and i + 1 < len(lines) and not _interrupts(lines, i + 1):
                    i += 1
                    paragraph.append(lines[i])
                content.append({"type": "paragraph", "content": _flowed(paragraph)})

        i += 1

    return {"type": "doc", "version": 1, "content": content}


def _parse_inline(text: str) -> list[dict[str, Any]]:
    """Parse inline markdown formatting (bold, italic, inline code, links)."""
    if not text.strip():
        return [{"type": "text", "text": ""}]

    nodes = []
    pos = 0

    pattern = (r'(\*\*(.+?)\*\*)|(`([^`]+)`)|((?<!!)\[([^\]]+)\]\(((?i:https?://|mailto:)(?:[^()\s]|\([^()\s]*\))+)\))'
               r'|(?<!\*)(\*([^*\s](?:[^*]*[^*\s])?)\*)(?!\*)')

    for match in re.finditer(pattern, text):
        if match.start() > pos:
            nodes.append({"type": "text", "text": text[pos:match.start()]})

        if match.group(1):  # bold
            nodes.append({"type": "text", "text": match.group(2),
                          "marks": [{"type": "strong"}]})
        elif match.group(3):  # inline code
            nodes.append({"type": "text", "text": match.group(4),
                          "marks": [{"type": "code"}]})
        elif match.group(5):  # link
            nodes.append({"type": "text", "text": match.group(6),
                          "marks": [{"type": "link", "attrs": {"href": match.group(7)}}]})
        elif match.group(8):  # italic
            nodes.append({"type": "text", "text": match.group(9),
                          "marks": [{"type": "em"}]})

        pos = match.end()

    if pos < len(text):
        nodes.append({"type": "text", "text": text[pos:]})

    return nodes if nodes else [{"type": "text", "text": text}]


def adf_to_markdown(adf: dict[str, Any] | None) -> str:
    """Convert an ADF document node to markdown."""
    if not adf or not isinstance(adf, dict):
        return ""
    return "\n".join(_process_content(adf.get("content", [])))


def _process_content(content: list[dict[str, Any]]) -> list[str]:
    lines = []

    for node in content:
        node_type = node.get("type")

        if node_type == "paragraph":
            text = _process_inline_content(node.get("content", []))
            lines.append(text if text else "")

        elif node_type == "heading":
            level = node.get("attrs", {}).get("level", 1)
            text = _process_inline_content(node.get("content", []))
            lines.append(f"{'#' * level} {text}")

        elif node_type == "bulletList":
            for item in node.get("content", []):
                item_content = _process_content(item.get("content", []))
                for i, line in enumerate(item_content):
                    prefix = "- " if i == 0 else "  "
                    lines.append(f"{prefix}{line}")

        elif node_type == "orderedList":
            for idx, item in enumerate(node.get("content", []), 1):
                item_content = _process_content(item.get("content", []))
                for i, line in enumerate(item_content):
                    prefix = f"{idx}. " if i == 0 else "   "
                    lines.append(f"{prefix}{line}")

        elif node_type == "blockquote":
            quote_content = _process_content(node.get("content", []))
            lines.extend(f"> {line}" for line in quote_content)

        elif node_type == "codeBlock":
            language = node.get("attrs", {}).get("language", "")
            code_lines = _process_inline_content(node.get("content", []))
            lines.append(f"```{language}")
            lines.append(code_lines)
            lines.append("```")

        elif node_type == "rule":
            lines.append("---")

        elif node_type == "panel":
            panel_content = _process_content(node.get("content", []))
            lines.append("┌─────────────────────────────────────────┐")
            lines.extend(f"│ {line}" for line in panel_content)
            lines.append("└─────────────────────────────────────────┘")

        elif node_type == "table":
            lines.extend(_table_to_markdown(node))

        elif node_type in ("mediaSingle", "mediaGroup"):
            for media in node.get("content", []):
                if media.get("type") != "media":
                    continue
                attrs = media.get("attrs", {})
                alt = attrs.get("alt") or "media"
                if attrs.get("type") == "external":
                    lines.append(f"![{alt}]({attrs.get('url', '')})")
                else:
                    # File media ids are media-services UUIDs, not attachment
                    # ids; the media: scheme marks them as non-downloadable
                    # here (use the ticket's attachments list for content).
                    lines.append(f"![{alt}](media:{attrs.get('id', '')})")

    return lines


def _table_to_markdown(node: dict[str, Any]) -> list[str]:
    rows = []
    for row in node.get("content", []):
        cells = []
        for cell in row.get("content", []):
            cell_lines = _process_content(cell.get("content", []))
            cells.append(" ".join(l for l in cell_lines if l).strip())
        rows.append(cells)
    if not rows:
        return []
    out = ["| " + " | ".join(rows[0]) + " |",
           "|" + "|".join("---" for _ in rows[0]) + "|"]
    out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
    return out


def _process_inline_content(content: list[dict[str, Any]]) -> str:
    parts = []

    for node in content:
        node_type = node.get("type")

        if node_type == "text":
            text = node.get("text", "")
            marks = node.get("marks", [])

            for mark in marks:
                mark_type = mark.get("type")
                if mark_type == "strong":
                    text = f"**{text}**"
                elif mark_type == "em":
                    text = f"*{text}*"
                elif mark_type == "code":
                    text = f"`{text}`"
                elif mark_type == "link":
                    href = mark.get("attrs", {}).get("href", "")
                    text = f"[{text}]({href})"
                elif mark_type == "strike":
                    text = f"~~{text}~~"

            parts.append(text)

        elif node_type == "hardBreak":
            parts.append("\n")

        elif node_type == "inlineCard":
            parts.append(node.get("attrs", {}).get("url", ""))

        elif node_type == "mention":
            parts.append(f"@{node.get('attrs', {}).get('text', '')}")

        elif node_type == "emoji":
            parts.append(node.get("attrs", {}).get("shortName", ""))

    return "".join(parts)


def process_ticket(raw_data: dict[str, Any],
                   story_points_field_id: str = "customfield_10016",
                   ) -> dict[str, Any]:
    """Reduce a raw issue GET to the fields an agent needs, ADF -> markdown."""
    fields = raw_data.get("fields", {})

    status = fields.get("status") or {}
    priority = fields.get("priority") or {}
    assignee = fields.get("assignee") or {}
    reporter = fields.get("reporter") or {}
    issuetype = fields.get("issuetype") or {}

    ticket = {
        "key": raw_data.get("key"),
        "summary": fields.get("summary"),
        "status": status.get("name"),
        "priority": priority.get("name"),
        "assignee": assignee.get("displayName", "Unassigned"),
        "reporter": reporter.get("displayName"),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "issue_type": issuetype.get("name"),
        "labels": fields.get("labels", []),
    }

    description_adf = fields.get("description")
    ticket["description"] = adf_to_markdown(description_adf) if description_adf else None

    parent = fields.get("parent")
    ticket["parent"] = {
        "key": parent.get("key") if parent else None,
        "summary": parent.get("fields", {}).get("summary") if parent else None,
    }

    sprint_data = fields.get("customfield_10020", [])
    if isinstance(sprint_data, list):
        ticket["sprint"] = [s.get("name") for s in sprint_data if isinstance(s, dict)]
    else:
        ticket["sprint"] = []

    ticket["story_points"] = fields.get(story_points_field_id)

    acceptance_criteria_adf = fields.get("customfield_10017")
    ticket["acceptance_criteria"] = (
        adf_to_markdown(acceptance_criteria_adf) if acceptance_criteria_adf else None
    )

    ticket["components"] = [c.get("name") for c in fields.get("components", [])]
    ticket["fix_versions"] = [v.get("name") for v in fields.get("fixVersions", [])]

    comment_data = fields.get("comment", {})
    comments = []
    for comment in comment_data.get("comments", []):
        body_adf = comment.get("body")
        comments.append({
            "id": comment.get("id"),
            "author": comment.get("author", {}).get("displayName"),
            "created": comment.get("created"),
            "updated": comment.get("updated"),
            "body": adf_to_markdown(body_adf) if body_adf else "",
        })

    ticket["comments"] = {
        "total": comment_data.get("total", 0),
        "entries": comments,
    }

    attachments = []
    for attachment in fields.get("attachment", []):
        attachments.append({
            "id": attachment.get("id"),
            "filename": attachment.get("filename"),
            "created": attachment.get("created"),
            "size": attachment.get("size"),
            "mimeType": attachment.get("mimeType"),
            "url": attachment.get("content"),
        })

    ticket["attachments"] = attachments

    return ticket
