"""A drift has two messages behind it, and the page has to say when they are not one.

`mentioning` proves the *cluster* is about the ticket. It does not prove that the message
which decided the item's state is. On the real project those were different messages in 4
findings out of 4 — one reported "the board says REVERAPP-87 is Done but somebody is
stuck on it" and quoted a line about REVERAPP-110.

The rule pinned here is deliberately not "drop those findings". A cluster that names a
ticket and contradicts it is worth a look, and requiring one message to carry both the
key and the state cue would have emptied the page on this corpus. What is required is
that the difference is reported: `evidence_names_ticket`, plus the mention itself so a
reader can see both sentences without leaving the card.
"""

from __future__ import annotations

from types import SimpleNamespace

from tam.analysis.drift import detect


def issue(key: str, *, state: str, resolved: float = 0.0, updated: float = 1_000.0) -> SimpleNamespace:
    return SimpleNamespace(
        key=key, state=state, resolved=resolved, updated=updated,
        url=f"https://example.invalid/issue/{key}", summary=key, description="",
    )


def msg(rid: str, text: str, ts: float) -> dict[str, object]:
    return {"id": rid, "text": text, "ts": ts, "user": "U1", "source": "slack"}


def topic(item_id: str, records: list[dict[str, object]], *, state: str, evidence_id: str) -> SimpleNamespace:
    # The evidence *quotes* the message that set the state, the way digest.infer_state
    # writes it. That is what makes a mismatch visible to a reader at all.
    quoted = next((str(record["text"]) for record in records if record["id"] == evidence_id), "")
    return SimpleNamespace(
        item_id=item_id, key=1, records=records, state=state,
        evidence=f'{state} — คนกรอกเองว่า "{quoted}"', evidence_id=evidence_id,
    )


def test_the_state_evidence_naming_another_ticket_is_reported_not_hidden() -> None:
    # The shape of the real REVERAPP-87 finding: a release note lists the key, and an
    # unrelated blocker line in the same cluster is what set the state.
    records = [
        msg("m1", "Submitted 3.2.0 for review. This release includes PROJ-87 reward page", 1_000.0),
        msg("m2", "additional fix [PROJ-110] and pending deploy to retest", 2_000.0),
    ]
    drifts = detect(
        [topic("PROJ-87", records, state="blocked", evidence_id="m2")],
        [issue("PROJ-87", state="Done", resolved=1.0)],
    )
    assert len(drifts) == 1, "the finding stands: the cluster names the ticket and the states disagree"
    found = drifts[0]
    assert found.evidence_names_ticket is False
    assert found.link_id == "m1", "the mention is the release note, not the blocker line"
    assert "PROJ-87" in found.link_text
    assert "PROJ-110" in found.evidence, "and the state still cites the message that set it"


def test_one_message_carrying_both_is_marked_as_such() -> None:
    records = [msg("m1", "PROJ-87 ยังติดอยู่ รอ BE เคลียร์ data ก่อน", 2_000.0)]
    drifts = detect(
        [topic("PROJ-87", records, state="blocked", evidence_id="m1")],
        [issue("PROJ-87", state="Done", resolved=1.0)],
    )
    assert len(drifts) == 1
    assert drifts[0].evidence_names_ticket is True
    assert drifts[0].link_id == "m1"


def test_talking_after_a_close_is_timed_off_the_mention_itself() -> None:
    # This kind reads the mention's own timestamp, so the two messages are one by
    # construction and the flag must not claim otherwise.
    records = [
        msg("m1", "ok ครับ", 2_000.0),
        msg("m2", "PROJ-90 ตัวนี้ยังมีปัญหาอยู่นะ ลอง deploy ใหม่แล้วยังไม่หาย", 100_000.0),
    ]
    drifts = detect(
        [topic("PROJ-90", records, state="active", evidence_id="m1")],
        [issue("PROJ-90", state="Done", resolved=1.0, updated=1_000.0)],
    )
    assert [d.kind for d in drifts] == ["ticket_closed_but_talking"]
    assert drifts[0].evidence_names_ticket is True
    assert drifts[0].link_id == "m2"
