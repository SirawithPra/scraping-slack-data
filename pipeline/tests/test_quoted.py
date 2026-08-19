"""Text a person pasted is not text a person asserted.

Every case here was found in a real 936-message export, and the first one shipped a
false claim to a standup: a work item read `resolved` because the Thai cue เรียบร้อย
occurred inside a fenced block of five-star app-store reviews that a teammate had
copied in to show the team. The customer wrote the word; nobody had resolved
anything. These tests pin the separation so a future edit cannot quietly hand pasted
text back to the cue matcher.
"""

from __future__ import annotations

from tam.ingest.quoted import analysis_text, annotate, asserted_lines, bot_ids, for_analysis, is_pasted


def test_a_cue_inside_a_fenced_block_does_not_survive() -> None:
    # The exact shape that produced the false `resolved`.
    pasted = '```5 ★ — English\n"Looks good and works fine"\nหน้าตาเรียบร้อย ใช้ง่าย```'
    assert "เรียบร้อย" in pasted
    assert "เรียบร้อย" not in analysis_text(pasted)
    assert is_pasted(pasted), "a message that is only a quoted block asserts nothing"


def test_an_unclosed_fence_swallows_the_rest() -> None:
    # People paste and then keep typing without closing the fence. Everything after it
    # renders as code in Slack, so reading it as prose is reading something the author
    # did not write as a claim.
    assert analysis_text("ดูนี่\n```log: fix เสร็จแล้ว") == "ดูนี่"


def test_struck_through_text_is_not_an_assertion() -> None:
    # Somebody struck it because it stopped being true. Counting it inverts its meaning.
    assert analysis_text("~fix แล้ว~ ยังไม่ได้ทำ") == "ยังไม่ได้ทำ"


def test_a_quoted_line_belongs_to_whoever_was_quoted() -> None:
    assert analysis_text("> เขาบอกว่าเสร็จแล้ว\nจริงเหรอ") == "จริงเหรอ"
    # Scraped exports arrive HTML-escaped where the API gives a bare '>'.
    assert analysis_text("&gt; เขาบอกว่าเสร็จแล้ว\nจริงเหรอ") == "จริงเหรอ"


def test_ordinary_prose_is_left_alone() -> None:
    # The guard against over-stripping: most messages must pass through untouched, or
    # the cleaning silently removes the signal it was meant to protect.
    for text in ["เรียบร้อยครับ", "fix เสร็จแล้ว รอ deploy", "ยังรอ API อยู่"]:
        assert analysis_text(text) == text


def test_inline_code_goes_but_its_neighbours_stay() -> None:
    # A fence removes its surroundings; a backtick span is usually an identifier inside
    # a real sentence, so only the span itself is dropped.
    assert analysis_text("แก้ `sortOrder` แล้วครับ") == "แก้ แล้วครับ"


def test_for_analysis_prefers_the_stored_field_and_falls_back() -> None:
    assert for_analysis({"analysis_text": "stored", "text": "raw"}) == "stored"
    # A corpus prepared before this existed must still be cleaned, so a missing field
    # means "not computed yet", never "nothing to assert".
    assert for_analysis({"text": "```pasted```"}) == ""
    # An empty stored value is a real answer and must not be re-derived.
    assert for_analysis({"analysis_text": "", "text": "```pasted```"}) == ""


def test_bots_are_identified_by_slack_flag_not_by_id_prefix() -> None:
    # Measured on the real workspace: all 146 bot-authored messages carry a `U…` id, so
    # the usual startswith("B") test flags none of them — while they are 27.6% of every
    # character in the corpus. A deploy notification saying "success" is not a teammate
    # reporting that work is done.
    names = {"U0BOTLIKE1": "Deploy Notifier (bot)", "U0PERSON01": "Somebody"}
    assert bot_ids(names) == {"U0BOTLIKE1"}

    records = [{"id": "a", "text": "ok", "user": "U0BOTLIKE1"}, {"id": "b", "text": "ok", "user": "U0PERSON01"}]
    annotated = annotate(records, bots=bot_ids(names))
    assert [r["is_bot"] for r in annotated] == [True, False]


def test_annotate_never_edits_the_displayed_text() -> None:
    # The dashboard, the citations and the evidence links must show what was really
    # said; only the derived field may differ.
    original = "ดูนี่ ```pasted```"
    annotated = annotate([{"id": "a", "text": original}])[0]
    assert annotated["text"] == original
    assert annotated["analysis_text"] == "ดูนี่"


def test_asserted_lines_keeps_the_line_structure_analysis_text_collapses() -> None:
    """The field cue matching needed and did not have.

    `analysis_text` collapses every run of whitespace, so on the real corpus 201 posts
    with four or more lines each read as a single line — which is why a cue anywhere in a
    twelve-line daily update used to type the whole update. See
    `tests/test_line_level_cues.py` for what reads them.
    """
    post = "• update\n\n◦ fixed the sorting API  \n◦ waiting for dev"
    assert asserted_lines(post) == ["• update", "◦ fixed the sorting API", "◦ waiting for dev"]
    assert len(asserted_lines("one line only")) == 1
    assert asserted_lines("   \n\n  ") == []


def test_asserted_lines_applies_the_same_removals() -> None:
    """Line structure must not become a way back in for text nobody asserted."""
    assert asserted_lines("ดูนี่\n```log: fix เสร็จแล้ว```") == ["ดูนี่"]
    assert asserted_lines("> เขาบอกว่าเสร็จแล้ว\nจริงเหรอ") == ["จริงเหรอ"]
    assert asserted_lines("~fix แล้ว~\nยังไม่ได้ทำ") == ["ยังไม่ได้ทำ"]


def test_analysis_text_is_exactly_the_asserted_lines_joined() -> None:
    """The invariant that let the refactor land without changing any existing caller.

    Embedding, BM25 and the state gate all read `analysis_text`; every one of them must
    see the byte-identical string it saw before lines became visible.
    """
    for text in (
        "",
        "one line",
        "a  b\n\n  c  ",
        "• update\n◦ fixed\n◦ waiting for dev",
        "ดูนี่\n```pasted```\nต่อ",
        "> quoted\nreal line\n~struck~ kept",
    ):
        assert analysis_text(text) == " ".join(asserted_lines(text))
