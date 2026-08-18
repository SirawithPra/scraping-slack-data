"""Not every message may decide whether work is finished.

`resolves` fires on a cue in a later message. That is right when a teammate says the
work is done, and wrong in two cases found on a real 936-message export, where four
of fifteen resolved items were resolved by something nobody would accept as proof:

* a deploy bot posting that a command succeeded, and
* a seven-to-nine character acknowledgement — the Thai for "done" or "all set" —
  standing for a cluster of 27 to 39 messages spanning weeks.

Telling a standup that unfinished work is finished is worse than telling it nothing,
so both are refused. The second refusal is proportionate rather than absolute: in a
small item a bare reply is unambiguous about what it refers to, and forbidding it
everywhere would throw away the most common way people actually say they finished.
"""

from __future__ import annotations

from typing import Any

import tam.analysis.digest as digest
from tam.analysis.relations import Relation


def record(rid: str, text: str, ts: float, *, is_bot: bool = False) -> dict[str, Any]:
    return {"id": rid, "text": text, "analysis_text": text, "ts": ts, "user": "U0PERSON01", "is_bot": is_bot}


def resolves(source: int, target: int) -> Relation:
    return Relation(name="resolves", source=source, target=target, evidence="cue", score=1.0)


def big_topic(closing: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A topic larger than ACK_MAX_TOPIC, ending in `closing`."""
    members = [record(f"m{i}", f"งานเรื่องนี้ยาว ๆ ข้อความที่ {i}", 1_000 + i) for i in range(digest.ACK_MAX_TOPIC + 2)]
    members.append(closing)
    return members, members


def test_a_bare_ack_does_not_resolve_a_large_item() -> None:
    members, records = big_topic(record("ack", "เรียบร้อย", 9_999))
    state, _, _, _ = digest.infer_state(members, [resolves(0, len(records) - 1)], records)
    assert state == "active", "nine characters cannot stand for a dozen messages of work"


def test_the_same_ack_does_resolve_a_small_item() -> None:
    # Proportionate, not absolute: in a two-message item the reply is unambiguous.
    members = [record("a", "แก้ตัวนี้ให้ด้วย", 1_000), record("b", "เรียบร้อย", 2_000)]
    state, _, evidence_id, _ = digest.infer_state(members, [resolves(0, 1)], members)
    assert state == "resolved"
    assert evidence_id == "b"


def test_a_bot_may_never_decide_state() -> None:
    # A deploy notification saying a command finished is not a teammate reporting that
    # the work is done — and it is long enough to pass the length rule, so the
    # authorship rule is what has to catch it.
    members = [
        record("a", "ขอ deploy รอบใหม่ด้วยครับ", 1_000),
        record("bot", "Deployment to production finished successfully in 42s. All checks passed.", 2_000, is_bot=True),
    ]
    state, _, _, _ = digest.infer_state(members, [resolves(0, 1)], members)
    assert state == "active"


def test_a_substantial_human_message_still_resolves_a_large_item() -> None:
    # The guard against over-blocking: the rule must not make large items unresolvable.
    closing = record("done", "แก้เสร็จแล้วครับ ทดสอบบน staging ผ่านหมด รอ deploy รอบหน้า", 9_999)
    members, records = big_topic(closing)
    state, _, evidence_id, _ = digest.infer_state(members, [resolves(0, len(records) - 1)], records)
    assert state == "resolved"
    assert evidence_id == "done"


def test_a_refused_relation_falls_through_to_the_next_one() -> None:
    # A refused `resolves` must not silently erase an earlier, valid state — the item
    # should read as whatever the last message allowed to speak said.
    members = [record(f"m{i}", f"ข้อความ {i}", 1_000 + i) for i in range(digest.ACK_MAX_TOPIC + 2)]
    members.append(record("blocked", "ยังรอ API จากทีมหลังบ้านอยู่ครับ ยังไปต่อไม่ได้", 5_000))
    members.append(record("ack", "โอเค", 9_999))
    records = members
    relations = [
        Relation(name="blocked_by", source=0, target=len(records) - 2, evidence="cue", score=1.0),
        resolves(0, len(records) - 1),
    ]
    state, evidence, evidence_id, _ = digest.infer_state(members, relations, records)
    assert state == "blocked", "a two-character 'โอเค' must not clear a stated blocker"
    assert evidence_id == "blocked"
    assert "ยังรอ" in evidence or "blocked" in evidence.lower()


def test_the_thresholds_are_the_measured_ones() -> None:
    # Pinned so a later edit has to justify moving them. Derived by reading the
    # state-deciding message of every resolved item on the real export: the wrong ones
    # sat at 7, 8 and 9 characters, the smallest legitimate one at 35.
    assert digest.ACK_CHARS == 25
    assert digest.ACK_MAX_TOPIC == 10
