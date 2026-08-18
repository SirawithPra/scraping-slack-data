"""A drift claim needs the conversation to be about the ticket it names.

Two ways this has already gone wrong, both pinned here:

* The first version compared the ticket's close time against the topic's *last message*.
  All six findings it produced were artefacts, because a topic spans weeks across several
  channels and its last message is usually about something else.
* Merging the tracker into the corpus introduced a subtler version. An item can now be
  named after a ticket because that ticket's own record landed in its cluster, with no
  human ever having typed the key. The count went 1 → 4, and three of the four had no
  mention at all — one compared "added permission done krub" against a ticket nobody in
  the thread had named.

Cluster proximity is not evidence. Only 19 of 194 issues are named in Slack prose on the
real project, so this rule costs real coverage; reporting that is better than reporting
comparisons a reader cannot check.
"""

from __future__ import annotations

from types import SimpleNamespace

from tam.analysis.drift import detect


def issue(key: str, *, state: str, resolved: float = 0.0, updated: float = 1_000.0) -> SimpleNamespace:
    return SimpleNamespace(
        key=key, state=state, resolved=resolved, updated=updated,
        url=f"https://example.invalid/issue/{key}", summary=key, description="",
    )


def topic(item_id: str, records: list[dict[str, object]], *, state: str) -> SimpleNamespace:
    return SimpleNamespace(
        item_id=item_id, key=1, records=records, state=state,
        evidence=f"{state} by cue", evidence_id=str(records[0]["id"]) if records else "",
    )


def msg(rid: str, text: str, ts: float) -> dict[str, object]:
    return {"id": rid, "text": text, "ts": ts, "user": "U1", "source": "slack"}


def ticket_record(key: str, ts: float) -> dict[str, object]:
    return {"id": f"yt_{key}", "text": f"{key} the summary", "ts": ts, "source": "youtrack", "youtrack_key": key}


def test_a_ticket_sharing_a_cluster_is_not_a_conversation_about_it() -> None:
    # The regression the merge introduced: the ticket record is here, the state is here,
    # and nobody named the ticket.
    records = [msg("m1", "added permission done krub. just finish chat with aim.", 2_000.0),
               ticket_record("PROJ-157", 1_500.0)]
    drifts = detect([topic("PROJ-157", records, state="resolved")], [issue("PROJ-157", state="Backlog")])
    assert drifts == [], "cluster proximity must not stand in for evidence"


def test_a_human_naming_the_ticket_is_enough() -> None:
    records = [msg("m1", "PROJ-157 ยังติดอยู่ รอ data อยู่", 2_000.0),
               ticket_record("PROJ-157", 1_500.0)]
    drifts = detect([topic("PROJ-157", records, state="resolved")], [issue("PROJ-157", state="Backlog")])
    assert len(drifts) == 1
    assert drifts[0].ticket == "PROJ-157"


def test_a_ticket_quoting_its_own_key_does_not_count_as_being_discussed() -> None:
    # The tracker record's text carries the key, so a naive substring search would read
    # every merged issue as one somebody had mentioned.
    records = [msg("m1", "เรียบร้อยครับ", 2_000.0), ticket_record("PROJ-157", 1_500.0)]
    drifts = detect([topic("PROJ-157", records, state="resolved")], [issue("PROJ-157", state="Backlog")])
    assert drifts == []
