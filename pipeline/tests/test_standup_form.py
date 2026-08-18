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
from tam.ingest.standup import declared_blockers, is_standup, parse, standups

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
        records, {"m1": ["รอ requirement จาก ROPS"]}, "active", "", "", float("nan")
    )
    assert state == "blocked"
    assert evidence_id == "m1", "the evidence is the person's own message"
    assert "คนกรอกเองว่า" in evidence
    assert since == 5_000.0


def test_a_later_resolve_still_wins() -> None:
    # Measured on the real export: all three declarations sit in one topic that was
    # resolved two months later, so reading the form correctly changes nothing there.
    # Declaring a blocker in June and fixing it in August is not blocked now.
    records = [post("รอ requirement จาก ROPS", ts=1_000.0)]
    state, _, evidence_id, _ = digest.apply_declarations(
        records, {"m1": ["รอ requirement จาก ROPS"]}, "resolved", "resolved on …", "later", 9_000.0
    )
    assert state == "resolved"
    assert evidence_id == "later"


def test_no_declaration_leaves_the_inferred_state_untouched() -> None:
    state, evidence, evidence_id, since = digest.apply_declarations(
        [post("")], {}, "active", "keep", "keep-id", float("nan")
    )
    assert (state, evidence, evidence_id) == ("active", "keep", "keep-id")
    assert np.isnan(since)
