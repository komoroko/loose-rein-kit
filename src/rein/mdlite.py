"""Escape-first rendering of the Markdown subset the deliverable scaffolds use — for the review pane.

Why hand-rolled instead of a library: the dashboard page holds the per-start write token that can
record a **gate approval**, and what this module renders is *agent-written* Markdown. A
prompt-injected agent that could smuggle `<script>` (or an `onerror` attribute, or a `javascript:`
href) into a deliverable would turn the review pane into XSS → token theft → self-approved gate.
So the design makes injection structurally impossible, the same way ui.action_argv makes arbitrary
command execution impossible: **every character of input text is HTML-escaped first**, and the only
tags in the output are the fixed set this module itself emits. PyPI `markdown` passes raw HTML
through (safe-mode is deprecated), and a vendored JS renderer would need a vendored sanitizer on
top — both would put a sanitizer, not a constructor, between the agent and the token.

The dialect is deliberately the scaffold subset (docs/*.md templates): ATX headings, unordered /
ordered lists with task checkboxes, tables, fenced code, block quotes, horizontal rules, and the
inline trio bold / `code` / [links](…). Links keep only http(s), in-page anchors, and same-origin
relative targets — another scheme, or a scheme-relative `//host` that merely reads like a path,
stays visible as plain text instead. HTML comments (scaffold guidance) are dropped. Anything
outside the dialect degrades to escaped paragraph text — never to markup.

Pure functions, no I/O; review_api.py composes them per deliverable.
"""

from __future__ import annotations

import html
import re

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_ITEM_RE = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")
_CHECKBOX_RE = re.compile(r"^\[([ xX])\]\s*(.*)$")
_HR_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^()\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def _strip_nul(text: str) -> str:
    """Drop NUL, the one character the stash placeholders are built from.

    `_inline` parks generated fragments in `\\x00<index>\\x00` markers while it rewrites the rest of
    the line. A NUL surviving from the input would be read back as a marker and index into a stash
    that never held it (IndexError) — a deliverable carrying stray binary would take the whole
    gate's review pane down. NUL is not text, so removing it costs nothing.
    """
    return text.replace("\x00", "")


def _safe_href(href: str) -> bool:
    """Only http(s), in-page anchors, and same-origin relative paths may become a real link."""
    if href.startswith("//"):
        return False  # scheme-relative: reads as a path but resolves cross-origin
    scheme = _SCHEME_RE.match(href)
    if scheme:
        return scheme.group(0).lower() in ("http:", "https:")
    return True  # anchor or relative — resolves same-origin, which never carries the token anywhere


def _inline(text: str) -> str:
    """Render the inline trio over already-escaped text (escape-first: no input char survives raw)."""
    escaped = html.escape(_strip_nul(text), quote=True)
    stash: list[str] = []

    def _stash(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    # Code spans first so their content is exempt from bold/link rewriting.
    escaped = _CODE_SPAN_RE.sub(lambda m: _stash(f"<code>{m.group(1)}</code>"), escaped)

    def _link(m: re.Match[str]) -> str:
        href = html.unescape(m.group(2))
        if not _safe_href(href):
            return m.group(0)  # stays visible as escaped text — the reviewer sees the attempt
        # noreferrer too: the dashboard URL is not secret, but nothing about this page needs to
        # travel to a target an agent chose.
        return _stash(f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer">{m.group(1)}</a>')

    escaped = _LINK_RE.sub(_link, escaped)
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], escaped)


def _checkbox(content: str) -> str:
    """A task-list item body: `[ ]`/`[x]` prefix becomes a marker span, the rest renders inline."""
    m = _CHECKBOX_RE.match(content)
    if not m:
        return _inline(content)
    checked = m.group(1) != " "
    mark = "☑" if checked else "☐"
    cls = "cb done" if checked else "cb"
    return f'<span class="{cls}">{mark}</span> {_inline(m.group(2))}'


def _flush_paragraph(buf: list[str], out: list[str]) -> None:
    if buf:
        out.append("<p>" + _inline(" ".join(buf)) + "</p>")
        buf.clear()


def _table(lines: list[str], start: int, out: list[str]) -> int:
    """Consume a pipe table at `start` (header, separator, rows); returns the next unconsumed index."""
    rows: list[list[str]] = []
    i = start
    while i < len(lines):
        stripped = lines[i].strip()
        if not (stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1):
            break
        rows.append([c.strip() for c in stripped.strip("|").split("|")])
        i += 1
    body: list[str] = []
    for r, cells in enumerate(rows):
        if r == 1 and all(set(c) <= {"-", ":"} and c for c in cells):
            continue  # the |---|---| separator
        tag = "th" if r == 0 else "td"
        body.append("<tr>" + "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells) + "</tr>")
    out.append("<table>" + "".join(body) + "</table>")
    return i


def _list(lines: list[str], start: int, out: list[str]) -> int:
    """Consume a (possibly nested) list at `start`; returns the next unconsumed index.

    Nesting derives from leading-space depth; `-`/`*` items open <ul>, `N.` items <ol>. A blank line
    ends the list only when the next line is not indented list content (scaffolds never interleave).
    """
    stack: list[tuple[int, str]] = []  # (indent, "ul" | "ol")
    i = start
    while i < len(lines):
        m = _LIST_ITEM_RE.match(lines[i])
        if not m:
            break
        indent, marker, content = len(m.group(1)), m.group(2), m.group(3)
        kind = "ol" if marker[0].isdigit() else "ul"
        while stack and stack[-1][0] > indent:
            out.append(f"</{stack.pop()[1]}>")
        if not stack or stack[-1][0] < indent or stack[-1][1] != kind:
            if stack and stack[-1][0] == indent:  # same level, different marker type
                out.append(f"</{stack.pop()[1]}>")
            stack.append((indent, kind))
            out.append(f"<{kind}>")
        out.append(f"<li>{_checkbox(content)}</li>")
        i += 1
    while stack:
        out.append(f"</{stack.pop()[1]}>")
    return i


def render(text: str) -> str:
    """The whole document as HTML built only from this module's own tags (see the module docstring)."""
    lines = _COMMENT_RE.sub("", _strip_nul(text)).splitlines()
    out: list[str] = []
    paragraph: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("```"):
            _flush_paragraph(paragraph, out)
            fence: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                fence.append(lines[i])
                i += 1
            i += 1  # past the closing fence (or EOF)
            out.append("<pre><code>" + html.escape("\n".join(fence), quote=True) + "</code></pre>")
            continue
        if not stripped:
            _flush_paragraph(paragraph, out)
            i += 1
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            _flush_paragraph(paragraph, out)
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _HR_RE.match(stripped):
            _flush_paragraph(paragraph, out)
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith(">"):
            _flush_paragraph(paragraph, out)
            quoted: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quoted.append(_inline(lines[i].strip().lstrip(">").strip()))
                i += 1
            out.append("<blockquote>" + "<br>".join(q for q in quoted if q) + "</blockquote>")
            continue
        if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1:
            _flush_paragraph(paragraph, out)
            i = _table(lines, i, out)
            continue
        if _LIST_ITEM_RE.match(lines[i]):
            _flush_paragraph(paragraph, out)
            i = _list(lines, i, out)
            continue
        paragraph.append(stripped)
        i += 1
    _flush_paragraph(paragraph, out)
    return "\n".join(out)


def extract_section(text: str, heading: str) -> tuple[str | None, str]:
    """Split out the first heading whose text contains `heading` (case-insensitive) with its body.

    Returns `(section_markdown, remainder_markdown)`; `(None, text)` when no heading matches. The
    section runs until the next heading of the same or a shallower level. Fenced code is opaque —
    a `#` inside it neither starts nor ends a section. Tolerant like status_api._section_table:
    structural surprises shrink the match, never raise.
    """
    lines = text.splitlines()
    start = level = None
    in_fence = False
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        m = _HEADING_RE.match(stripped)
        if not in_fence and m and heading.lower() in m.group(2).lower():
            start, level = i, len(m.group(1))
            break
    if start is None or level is None:
        return None, text
    end = len(lines)
    in_fence = False
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        m = _HEADING_RE.match(stripped)
        if not in_fence and m and len(m.group(1)) <= level:
            end = j
            break
    section = "\n".join(lines[start:end]).strip("\n")
    rest = "\n".join(lines[:start] + lines[end:]).strip("\n")
    return section, rest
