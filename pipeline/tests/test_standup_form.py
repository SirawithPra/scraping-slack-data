"""The team already labels its own blockers; read the label instead of guessing.

Every other signal in this project infers intent from prose. A standup form does not
need inference: text typed under "Are there any blockers?" is a blocker by its
author's own account. On a real 936-message export 18 posts carry the form, 7 answer
the blockers question with "none", and 3 describe an obstacle — and **only one of
those three contains any word from the blocked_by cue list**, so this is a different
kind of evidence rather than a longer keyword list.

Two mistakes are pinned here because both were made while writing it:

* The literal word "blockers" appears 22 times in the corpus and 14 of those are the
  question itself. Matching the word marks every standup as blocked.
* The form has four fields. After the blockers question comes "Others", and the first
  parser read `• Others` as the blocker answer for every post — eight characters that
  looked like real content in the measurements and were the next heading.
"""

from __future__ import annotations

import numpy as np

import tam.analysis.digest as digest
from tam.ingest.standup import cleared_blockers, declared_blockers, is_standup, parse, standups

FORM = """Daily update
• What did accomplish yesterday?
ปิด ticket เรื่อง sorting แล้ว
• What are you working on today?
ต่อหน้า profile
• Are there any blockers?
{blockers}
• Others
งานเลี้ยงศุกร์นี้
"""


def post(blockers: str, rid: str = "m1", ts: float = 1_000.0) -> dict[str, object]:
    return {"id": rid, "text": FORM.format(blockers=blockers), "ts": ts, "user": "U0PERSON01"}


def test_the_question_alone_is_not_an_answer() -> None:
    # The failure this guards: 14 of 22 occurrences of "blockers" in the real corpus
    # are the question. A rule that fires on the word marks every standup as blocked.
    assert declared_blockers([post("")]) == []
    assert is_standup(FORM.format(blockers="")), "it is still a standup form, just an empty slot"


def test_the_next_heading_is_not_the_answer() -> None:
    # `• Others` is eight characters and directly follows the blockers question, so a
    # parser that stops at a blank line rather than at the next heading reads it as the
    # answer for every post in the corpus.
    slots = parse(FORM.format(blockers=""))
    assert slots["blockers"] == []
    assert slots["other"] == ["งานเลี้ยงศุกร์นี้"]


def test_an_explicit_none_is_an_answer_and_not_a_blocker() -> None:
    # 7 of 18 forms answer this way. "No blockers today" is worth trusting, and must
    # not read as a described obstacle.
    for none in ["-", "None.", "N/A", "ไม่มี", "• none"]:
        assert declared_blockers([post(none)]) == [], none
    assert standups([post("None.")])[0].says_no_blockers


def test_a_described_obstacle_is_taken_at_the_author_s_word() -> None:
    described = "รอ requirement จาก ROPS ก่อน ถึงจะปรับ UI ต่อได้"
    found = declared_blockers([post(described)])
    assert len(found) == 1
    assert found[0][1] == [described]


def test_a_bare_blockers_heading_still_counts() -> None:
    # One of the three real declarations sits under a lone "Blockers:" with no other
    # field, so requiring the whole form discards it. Declaring a blocker without
    # filling in the rest of the form is still declaring one.
    lone = {"id": "m2", "text": "Blockers:\nรอ API จากทีมหลังบ้าน ยังต่อไม่ได้", "ts": 1.0}
    assert not is_standup(str(lone["text"])), "not a form"
    assert len(declared_blockers([lone])) == 1, "but still a declaration"


def test_a_declaration_beats_an_inferred_state() -> None:
    records = [post("รอ requirement จาก ROPS ก่อน ถึงจะปรับ UI ต่อได้", ts=5_000.0)]
    state, evidence, evidence_id, since = digest.apply_declarations(
        records, {"m1": ["รอ requirement จาก ROPS"]}, {}, "active", "", "", float("nan")
    )
    assert state == "blocked"
    assert evidence_id == "m1", "the evidence is the person's own message"
    assert "คนกรอกเองว่า" in evidence
    assert since == 5_000.0


def test_the_author_s_own_later_none_retires_their_declaration() -> None:
    # The one thing that does retire a declaration: the person who wrote it later
    # answering the same question with "none". Two of the three real declarations are
    # withdrawn exactly this way, five weeks and one month later respectively.
    records = [post("รอ requirement จาก ROPS", ts=1_000.0)]
    state, _, evidence_id, _ = digest.apply_declarations(
        records,
        {"m1": ["รอ requirement จาก ROPS"]},
        {"U0PERSON01": 9_000.0},
        "active",
        "inferred",
        "other",
        float("nan"),
    )
    assert state == "active", "their own withdrawal is the counter-evidence"
    assert evidence_id == "other"


def test_a_resolve_elsewhere_in_the_cluster_does_not_retire_a_declaration() -> None:
    # The regression this rule exists for. This used to defer to any later `resolved`
    # state on the topic, and on the real export all three declarations land in one
    # 71-message, 69-day cluster that a `closed` cue marks resolved on its final day —
    # so every declaration in it was silently retired, including "waiting for clearing
    # user on dev because data issues", which nobody had cleared. A cluster that wide is
    # a channel, not a work item; its tail is not evidence about any message inside it.
    records = [post("waiting for clearing user on dev because data issues", ts=1_000.0)]
    state, evidence, evidence_id, since = digest.apply_declarations(
        records,
        {"m1": ["waiting for clearing user on dev because data issues"]},
        {},  # nobody withdrew anything
        "resolved",
        "resolved on 2026-08-17 — cue “closed”",
        "some-other-message",
        9_000.0,  # two months after the declaration, and about something else
    )
    assert state == "blocked", "an unrelated later resolve must not clear a declaration"
    assert evidence_id == "m1"
    assert since == 1_000.0


def test_each_declaration_is_tested_against_its_own_author() -> None:
    # A withdrawal is personal. One person clearing their blocker says nothing about
    # somebody else's, and testing only the newest declaration would let the cleared one
    # answer for the standing one.
    mine = post("รอ API จากทีมหลังบ้าน", rid="m1", ts=1_000.0)
    theirs = dict(post("รอ design", rid="m2", ts=2_000.0), user="U0PERSON02")
    state, _, evidence_id, _ = digest.apply_declarations(
        [mine, theirs],
        {"m1": ["รอ API จากทีมหลังบ้าน"], "m2": ["รอ design"]},
        {"U0PERSON02": 3_000.0},  # only the newer declaration was withdrawn
        "active",
        "",
        "",
        float("nan"),
    )
    assert state == "blocked"
    assert evidence_id == "m1", "the standing declaration decides, not the withdrawn one"


def test_a_none_answer_reads_as_a_withdrawal_and_an_obstacle_does_not() -> None:
    assert [r["id"] for r in cleared_blockers([post("-", rid="clear")])] == ["clear"]
    assert cleared_blockers([post("รอ API จากทีมหลังบ้าน ยังต่อไม่ได้", rid="stuck")]) == []
    # An unanswered form is silence, not a withdrawal — the distinction the parser
    # exists to keep.
    assert cleared_blockers([{"id": "bare", "text": "สวัสดีครับ", "ts": 1.0}]) == []


def test_no_declaration_leaves_the_inferred_state_untouched() -> None:
    state, evidence, evidence_id, since = digest.apply_declarations(
        [post("")], {}, {}, "active", "keep", "keep-id", float("nan")
    )
    assert (state, evidence, evidence_id) == ("active", "keep", "keep-id")
    assert np.isnan(since)
