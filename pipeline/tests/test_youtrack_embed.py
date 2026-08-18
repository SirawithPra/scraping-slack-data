r"""What goes into the corpus from a ticket, and what must not.

The reason this is bounded rather than the whole description: on the real project the
median Slack message is 46 characters and the median ticket 582, with one 14,556-character
QA test-case table. A corpus where a fifth of the records are an order of magnitude longer
than the rest is one where the long ones decide every cluster.

Two mistakes are pinned because both were made while writing it: matching the *words*
inside a heading (which let `##### ➡️ Background / Problem Statement` through, because an
emoji is not `\w`), and keeping table rows (which made every QA test-case ticket look
identical, since they all open with the same `| NO | Test Case |` header).
"""

from __future__ import annotations

from tam.ingest.youtrack import EMBED_BUDGET, embed_text


def test_the_title_always_survives_even_with_no_description() -> None:
    assert embed_text("[BE] Reward detail API", "") == "[BE] Reward detail API"
    assert embed_text("[BE] Reward detail API", "   \n\n") == "[BE] Reward detail API"


def test_headings_are_dropped_whole_including_ones_with_emoji() -> None:
    body = "### **URL:**\nhttps://example.com/x\n##### ➡️ Background / Problem Statement\nThe preview never refreshes after an edit."
    out = embed_text("E4 - US07 - Actual mobile preview", body)
    assert "URL" not in out and "Background" not in out, "a heading labels the section, it is not the content"
    assert "The preview never refreshes after an edit." in out


def test_table_rows_are_dropped_so_templated_tickets_do_not_look_alike() -> None:
    body = "| NO | Test Case | Pre-Condition |\n| --- | --- | --- |\n| 1 | Default mode is Viewing | any status |"
    out = embed_text("[QA][TC]Edit Reward Detail", body)
    assert out == "[QA][TC]Edit Reward Detail", "title only beats a table header shared by every QA ticket"


def test_images_and_bare_links_are_not_content() -> None:
    body = "![](https://www.figma.com/design/abc)\n[Figma link](https://www.figma.com/design/abc)\nMove the refresh button next to the filter."
    out = embed_text("E2-US07-Task- fix viewing table", body)
    assert "figma.com" not in out
    assert "Move the refresh button next to the filter." in out


def test_emphasis_is_stripped_so_prose_reads_as_prose() -> None:
    out = embed_text("E4-US04", "**Feature:** Event detail — the Mode bar and lifecycle rules")
    assert "**" not in out
    assert "Feature: Event detail" in out


def test_only_the_opening_lines_are_kept() -> None:
    body = "\n".join(f"Line number {n} of the description, long enough to count." for n in range(1, 9))
    out = embed_text("A ticket", body, lines=2)
    assert "Line number 1" in out and "Line number 2" in out
    assert "Line number 3" not in out, "the rest of the description stays in YouTrack"


def test_the_result_is_capped_and_says_it_was_cut() -> None:
    out = embed_text("A ticket", "x" * 50 + " " + "y" * 2000)
    assert len(out) <= EMBED_BUDGET + 1, "one character of slack for the ellipsis"
    assert out.endswith("…")


def test_a_line_too_short_to_mean_anything_is_skipped() -> None:
    out = embed_text("A ticket", "ok\nyes\nThis line actually describes the problem in full.")
    assert "This line actually describes the problem in full." in out
    assert "\nok" not in out
