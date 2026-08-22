"""mdlite: the scaffold dialect renders, and nothing agent-written ever reaches the page as markup.

The security half is the point (see the mdlite module docstring's threat model): the review pane
runs with the gate-approval token in scope, so every test in the XSS class asserts the *absence*
of raw input markup in the output, not just the presence of the pretty rendering.
"""

from __future__ import annotations

import re

import pytest

from rein import mdlite

# The complete tag vocabulary mdlite emits. Escape-first means every input "<" leaves as "&lt;",
# so every literal "<" in the output must start (or close) one of these — that is the invariant.
_OWN_TAG_RE = re.compile(r"</?(?:p|h[1-6]|ul|ol|li|table|tr|th|td|pre|code|blockquote|br|hr|strong|span|a)\b")


def assert_only_own_tags(html: str) -> None:
    lt_positions = [m.start() for m in re.finditer("<", html)]
    tag_positions = [m.start() for m in _OWN_TAG_RE.finditer(html)]
    assert lt_positions == tag_positions, f"foreign markup in output: {html!r}"


class TestRenderDialect:
    def test_heading_paragraph_and_inline_trio(self) -> None:
        html = mdlite.render("## Summary\n\nBold **yes**, code `x < 1`, see [docs](https://example.com).")
        assert "<h2>Summary</h2>" in html
        assert "<strong>yes</strong>" in html
        assert "<code>x &lt; 1</code>" in html
        assert '<a href="https://example.com" rel="noopener noreferrer">docs</a>' in html

    def test_checkbox_list_renders_markers(self) -> None:
        html = mdlite.render("- [ ] open item\n- [x] done item")
        assert '<span class="cb">☐</span> open item' in html
        assert '<span class="cb done">☑</span> done item' in html

    def test_nested_and_ordered_lists(self) -> None:
        html = mdlite.render("- outer\n  - inner\n\n1. first\n2. second")
        assert html.count("<ul>") == 2  # outer + nested
        assert "<ol>" in html
        assert "<li>inner</li>" in html

    def test_table_with_header_and_separator(self) -> None:
        html = mdlite.render("| Phase | Gate |\n|---|---|\n| build | ④ |")
        assert "<th>Phase</th>" in html
        assert "<td>build</td>" in html
        assert "---" not in html  # the separator row is structure, not content

    def test_fenced_code_is_opaque(self) -> None:
        html = mdlite.render("```yaml\ntasks:\n  - id: T-001  # a **comment**\n```")
        assert "<pre><code>" in html
        assert "**comment**" in html  # inline rules do not run inside a fence
        assert "<strong>" not in html

    def test_blockquote_and_hr(self) -> None:
        html = mdlite.render("> gate ① guidance\n> second line\n\n---")
        assert html.startswith("<blockquote>")
        assert "gate ① guidance<br>second line" in html
        assert "<hr>" in html

    def test_html_comments_are_dropped(self) -> None:
        html = mdlite.render("before <!-- scaffold\nguidance --> after")
        assert "scaffold" not in html
        assert "guidance" not in html
        assert "before" in html and "after" in html

    def test_stripping_comments_keeps_the_line_numbering(self) -> None:
        """`approve.py` reports the line an unresolved marker sits on, so a multi-line comment has
        to leave its lines behind rather than collapsing the ones after it upwards."""
        stripped = mdlite.strip_comments("one\n<!-- two\nthree -->\nfour\n")
        assert stripped.splitlines() == ["one", "", "", "four"]

    def test_stripping_comments_leaves_everything_else(self) -> None:
        assert mdlite.strip_comments("keep <!-- drop --> this") == "keep  this"

    def test_relative_and_anchor_links_allowed(self) -> None:
        html = mdlite.render("[adr](decisions/ADR-001.md) and [top](#summary)")
        assert '<a href="decisions/ADR-001.md"' in html
        assert '<a href="#summary"' in html


class TestRenderXss:
    """Raw HTML in agent-written deliverables must come out inert (escape-first invariant)."""

    @pytest.mark.parametrize(
        "payload",
        [
            "<script>fetch('/api/gate/approve')</script>",
            '<img src=x onerror="alert(1)">',
            "<a href='javascript:alert(1)'>x</a>",
            'text with "quotes" & <angle> brackets',
            "# <b>t</b>\n\n| <i>a</i> |\n|---|\n| <u>b</u> |",
            "- [ ] <svg onload=alert(1)>",
            "> <iframe src=//evil>",
        ],
    )
    def test_raw_html_is_entity_encoded(self, payload: str) -> None:
        html = mdlite.render(payload)
        assert_only_own_tags(html)
        assert "<script" not in html and "<img" not in html and "<svg" not in html

    def test_javascript_link_stays_text(self) -> None:
        html = mdlite.render("[click](javascript:alert(1))")
        assert "<a " not in html
        assert "javascript:alert(1)" in html  # visible, so the reviewer sees the attempt
        assert_only_own_tags(html)

    def test_data_link_stays_text(self) -> None:
        html = mdlite.render("[x](data:text/html;base64,PHNjcmlwdD4=)")
        assert "<a " not in html
        assert_only_own_tags(html)

    def test_scheme_relative_link_stays_text(self) -> None:
        # "//host" reads like a path but resolves cross-origin — the reviewer's click would leave
        # the dashboard for a target the agent chose.
        html = mdlite.render("[docs](//attacker.example/x)")
        assert "<a " not in html
        assert "//attacker.example/x" in html  # visible, so the reviewer sees the attempt
        assert_only_own_tags(html)

    def test_allowed_links_carry_noreferrer(self) -> None:
        assert 'rel="noopener noreferrer"' in mdlite.render("[docs](https://example.com)")

    def test_nul_in_input_cannot_reach_the_stash_placeholders(self) -> None:
        # The inline pass parks generated fragments in \x00<index>\x00 markers. A NUL surviving from
        # the input would be read back as a marker and index into a stash that never held it,
        # taking the whole gate's review pane down with an IndexError.
        html = mdlite.render("a \x000\x00 b `code` \x0099\x00")
        assert "\x00" not in html
        assert "<code>code</code>" in html
        assert_only_own_tags(html)

    def test_no_attribute_escape_through_link_href(self) -> None:
        # A quote inside a matching href must leave as &quot; — it can never close the attribute.
        html = mdlite.render('[x](https://e.com/"onload=x)')
        assert '<a href="https://e.com/&quot;onload=x"' in html
        assert '/"onload' not in html
        assert_only_own_tags(html)


class TestExtractSection:
    DOC = (
        "# Title\n\nintro\n\n## Self-assessment (assumptions, confidence)\n"
        "- **Confidence**: low\n\n### detail\nmore\n\n## Next\ntail"
    )

    def test_extracts_until_same_level_heading(self) -> None:
        section, rest = mdlite.extract_section(self.DOC, "Self-assessment")
        assert section is not None
        assert section.startswith("## Self-assessment")
        assert "Confidence" in section and "### detail" in section
        assert "## Next" not in section
        assert "intro" in rest and "tail" in rest and "Confidence" not in rest

    def test_missing_heading_returns_none_and_original(self) -> None:
        section, rest = mdlite.extract_section("# a\nbody", "Self-assessment")
        assert section is None
        assert rest == "# a\nbody"

    def test_heading_inside_fence_is_ignored(self) -> None:
        doc = "```\n## Self-assessment\n```\n\n## Self-assessment\nreal"
        section, _ = mdlite.extract_section(doc, "Self-assessment")
        assert section is not None
        assert "real" in section

    def test_match_is_case_insensitive(self) -> None:
        section, _ = mdlite.extract_section("## SELF-ASSESSMENT\nx", "self-assessment")
        assert section is not None
