"""A cue below a post's opening line has to earn the pair it types.

This team writes work status as one line inside a long daily post. `docs/EXPERIMENTS.md`
§7.1 measured that and fixed it for blockers; relation typing kept reading whole
messages, so one line of a twelve-line update typed the entire update — and the pair it
typed was another person's twelve-line update, or the `Daily _ Please share an update`
prompt itself.

Three directions are pinned separately, because the first attempt traded them against
each other. Widening the cue window to every line's opening invented five `follows_up`
relations out of an API specification and dropped a real answer for sitting on line two;
gating only on the cue line's own anchors cut a genuine point-by-point fix report whose
cue line is four characters long. A future edit that fixes one of these must not silently
undo the others.
"""

from __future__ import annotations

from tam.analysis.relations import classify_by_rules, cue_line, TYPES_BY_NAME

RESOLVES = TYPES_BY_NAME["resolves"]
FOLLOWS_UP = TYPES_BY_NAME["follows_up"]


def record(rid: str, text: str, ts: str, user: str = "U0AUTHOR01") -> dict[str, object]:
    """A record the typer will read. `analysis_text` is absent on purpose.

    `for_analysis` falls back to computing it from `text`, and these fixtures want the
    line structure that a stored, already-collapsed `analysis_text` would have destroyed.
    """
    return {"id": rid, "text": text, "ts": ts, "user": user}


DAILY_POST = "\n".join(
    [
        "• What did accomplish yesterday?",
        "◦ fixing deploy dev uat",
        "• Are there any blockers?",
        "◦ waiting for clearing user on dev because data issues",
    ]
)


def test_cue_on_a_later_line_needs_a_link_to_the_other_message() -> None:
    """The measured failure: a daily post's blocker line typed against another daily post."""
    records = [
        record("a", "• What did accomplish yesterday?\n◦ main function and design", "1.0", "U0OTHER001"),
        record("b", DAILY_POST, "2.0"),
    ]
    assert classify_by_rules(records, 0, 1).name == "same_topic"


def test_a_shared_anchor_lets_the_same_line_through() -> None:
    """Nothing is being suppressed for being deep in a post — only for being unconnected."""
    records = [
        record("a", "REVERAPP-110 ยังค้างอยู่ครับ ใครดูอยู่", "1.0", "U0OTHER001"),
        record("b", "• update\n◦ waiting for REVERAPP-110 to be cleared on dev", "2.0"),
    ]
    typed = classify_by_rules(records, 0, 1)
    assert typed.name == "blocked_by"
    assert "reverapp-110" in typed.evidence.lower()


def test_a_cue_on_the_opening_line_speaks_for_the_whole_post() -> None:
    """What the post leads with needs no further justification."""
    records = [
        record("a", "sorting API ยังไม่มาเลยครับ", "1.0", "U0OTHER001"),
        record("b", "แก้แล้วครับ\n• อย่างอื่นยังทำต่ออยู่\n• พรุ่งนี้ค่อยว่ากัน", "2.0"),
    ]
    assert classify_by_rules(records, 0, 1).name == "resolves"


def test_a_single_line_message_is_its_own_opening_line() -> None:
    """Short conversational traffic — most of the corpus — is untouched by any of this."""
    records = [
        record("a", "อันนี้เสร็จยังครับ", "1.0", "U0OTHER001"),
        record("b", "เสร็จแล้วครับ", "2.0"),
    ]
    assert classify_by_rules(records, 0, 1).name == "resolves"


def test_a_bare_mention_on_line_one_scopes_the_lines_under_it() -> None:
    """The point-by-point reply the anchor test alone could not keep.

    `1.แก้แล้ว` is four characters and can carry no anchor, but the post opens by naming
    the person whose numbered list it is answering, which is how a reply is addressed.
    """
    records = [
        record("a", "ผมขอ list เรื่อง api ไว้ thread นี้ 1. event delete 2. start_date", "1.0", "U0ASKER001"),
        record("b", "@U0ASKER001\n1.แก้แล้ว\n2.สร้างไปแล้ว\n3.ส่ง start_date เป็นปัจจุบัน", "2.0"),
    ]
    typed = classify_by_rules(records, 0, 1)
    assert typed.name == "resolves"
    assert "เรียก" in typed.evidence


def test_a_windowed_cue_keeps_whole_message_semantics() -> None:
    """`follows_up` and `answers` are already confined to the message opening.

    Re-anchoring their window to every line's start read `Then reward of any status
    (DRAFT/PAUSED/EXPIRED included) is returned` — a line of an API spec — as somebody
    chasing a status, five times over.
    """
    spec = "[BE]03- Reward get API — single reward detail\nGiven id exists, Then reward of any status (DRAFT/PAUSED/EXPIRED included) is returned"
    records = [
        record("a", "[BE]01- Reward create API — server-derived status, Then status = DRAFT", "1.0", "U0OTHER001"),
        record("b", spec, "2.0"),
    ]
    assert classify_by_rules(records, 0, 1).name == "same_topic"


def test_cue_line_reports_where_the_cue_sat() -> None:
    """The index is what the evidence string quotes, so a reader can find the line."""
    lines = ["• update", "◦ nothing yet", "◦ fixed the sorting API"]
    index, line, cue = cue_line(lines, RESOLVES)
    assert (index, cue) == (2, "fixed")
    assert line.endswith("sorting API")
    assert cue_line(["• update", "◦ nothing yet"], RESOLVES) == (-1, "", "")


def test_a_cue_inside_pasted_material_is_still_invisible() -> None:
    """The line reader must not become a way back in for text nobody asserted.

    `blockers.py` reads raw `text` and would find a blocker inside a quoted block; this
    path reads the asserted lines, so a fenced review saying `เรียบร้อย` types nothing.
    """
    records = [
        record("a", "แอปล่มตอนเข้าโปรไฟล์", "1.0", "U0OTHER001"),
        record("b", "รีวิวที่ลูกค้าเขียนมา\n```\nใช้งานได้เรียบร้อย ดีมาก\n```", "2.0"),
    ]
    assert classify_by_rules(records, 0, 1).name == "same_topic"
