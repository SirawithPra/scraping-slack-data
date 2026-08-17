"""State inference: a chase must never clear a blocker.

This is the one rule the standup agenda is made of, and it failed silently. When
`follows_up` and `answers` were state-bearing, "any update on sorting?" typed as a
`follows_up` with a newer timestamp than the `blocked_by`, `max(relevant)` picked
it, and the item read `active` — so `/blockers` printed "(nothing blocked)" one
message after someone chased the blocked item. Nothing crashed and no number
looked wrong; the agenda just emptied exactly when it mattered.

The corpus below is written in the Thai the team actually uses and goes through
`extract_relations(method="rules")`, not through hand-built `Relation` objects, so
a regression in *either* half — the cue lists that produce the type or the rule
that weighs it — fails here.
"""

from __future__ import annotations

import pytest

from tam.analysis.digest import MOVEMENT_RELATIONS, STATE_RELATIONS, Digest, Topic, infer_state
from tam.analysis.relations import extract_relations

# One hour apart, so ordering is by real time and not by corpus position.
HOUR = 3600.0
BASE = 1786500000.0


def record(index: int, text: str, *, hours: float, user: str = "U0DEMOUSER1") -> dict[str, object]:
    return {"id": f"msg_C0DEMOCHAN1_{BASE + hours * HOUR:.3f}", "text": text, "ts": f"{BASE + hours * HOUR:.3f}", "user": user}


ASKED = record(0, "BE sorting API ยังไม่เสร็จ เดี๋ยวทำต่อพรุ่งนี้", hours=0)
BLOCKED = record(1, "FE sorting ยังรอ API อยู่ ไปต่อไม่ได้", hours=1, user="U0DEMOUSER2")
CHASED = record(2, "sorting API ถึงไหนแล้วครับ", hours=2)
RESOLVED = record(3, "sorting API deploy แล้ว เสร็จแล้วครับ", hours=3, user="U0DEMOUSER2")

# Every pair graph.py would hand over for this thread: they are all one topic.
def pairs(count: int) -> list[tuple[int, int]]:
    return [(left, right) for left in range(count) for right in range(left + 1, count)]


def typed(records: list[dict[str, object]]) -> list:
    return extract_relations(records, pairs(len(records)), method="rules")


def by_name(relations) -> dict[str, list]:
    out: dict[str, list] = {}
    for relation in relations:
        out.setdefault(relation.name, []).append(relation)
    return out


def test_cue_lists_still_type_the_scenario() -> None:
    """Guard the premise: without these types the state test proves nothing."""
    found = by_name(typed([ASKED, BLOCKED, CHASED]))
    assert "blocked_by" in found, "‘ยังรอ … ไปต่อไม่ได้’ must still type as blocked_by"
    assert "follows_up" in found, "‘ถึงไหนแล้ว’ must still type as follows_up"
    assert all(relation.target == 1 for relation in found["blocked_by"])
    assert all(relation.target == 2 for relation in found["follows_up"])


def test_chase_after_blocker_stays_blocked() -> None:
    records = [ASKED, BLOCKED, CHASED]
    state, evidence, evidence_id, since = infer_state(records, typed(records), records)

    assert state == "blocked"
    # The evidence stays on the message that proves the blocker, not on the chase.
    assert evidence_id == BLOCKED["id"]
    assert since == pytest.approx(float(str(BLOCKED["ts"])))
    assert evidence.startswith("blocked since")
    # The chase is reported as movement, after the state — never instead of it.
    assert "follows_up" in evidence


def test_blocked_item_reaches_the_blockers_agenda() -> None:
    """The symptom the finding was reported as: `--blockers` printed nothing."""
    records = [ASKED, BLOCKED, CHASED]
    state, evidence, evidence_id, since = infer_state(records, typed(records), records)
    topic = Topic(key=0, label="sorting api", item_id="TAM-x", records=records,
                  state=state, evidence=evidence, evidence_id=evidence_id, state_since=since)
    digest = Digest(topics=[topic], since=BASE, until=BASE + 4 * HOUR, corpus_size=len(records))

    assert [t.item_id for t in digest.blocked] == ["TAM-x"]
    assert digest.resolved == []


def test_resolves_after_blocker_does_clear_it() -> None:
    """The other half of the rule: a real `resolves` must still win."""
    records = [ASKED, BLOCKED, CHASED, RESOLVED]
    found = by_name(typed(records))
    assert "resolves" in found, "‘deploy แล้ว เสร็จแล้ว’ must still type as resolves"

    state, evidence, evidence_id, _ = infer_state(records, typed(records), records)
    assert state == "resolved"
    assert evidence_id == RESOLVED["id"]
    assert evidence.startswith("resolved on")


def test_movement_alone_is_active_and_says_so() -> None:
    records = [ASKED, CHASED]
    state, evidence, evidence_id, _ = infer_state(records, typed(records), records)
    assert state == "active"
    assert evidence_id == CHASED["id"]
    assert "follows_up" in evidence


def test_state_bearing_relations_are_only_the_two() -> None:
    """The invariant in one line, so widening the set is a deliberate act.

    `duplicates`, `follows_up` and `answers` are things that happen *to* an item.
    None of them is a statement that the work is finished or unstuck.
    """
    assert set(STATE_RELATIONS) == {"resolves", "blocked_by"}
    assert not set(STATE_RELATIONS) & set(MOVEMENT_RELATIONS)
