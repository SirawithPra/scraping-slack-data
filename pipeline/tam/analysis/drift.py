"""Where Slack and the ticket disagree.

This is the one finding that needs two sources, and therefore the one thing that
pointing a chat model at Slack cannot produce: the second source is not in Slack. Every
other signal in this project reads the conversation and reasons about it. Drift reads
the conversation *and* the ticket, and reports only the places they contradict each
other.

Three kinds, each a different mistake with a different fix:

`ticket_closed_but_talking`
    YouTrack says resolved; Slack is still discussing it, after the ticket closed.
    Usually a ticket closed early, or follow-up work nobody opened a ticket for.

`slack_blocked_but_ticket_open`
    Slack says somebody is stuck; the ticket looks like ordinary progress. The board
    is telling a standup that work is moving when a person has said it is not.

`slack_done_but_ticket_open`
    The conversation concluded; the ticket is still open. Either it needs closing or
    the conclusion was premature — both worth a glance, neither worth an alarm.

Every drift carries the message that triggered it, because a claim about a work item
has to name the message that proves it. A drift with no evidence would be exactly the
unverifiable assertion this project refuses everywhere else.

The honest limit, stated because it decides whether this is useful: a drift needs a
ticket key, and the key comes from somebody typing it into Slack. On the corpus this
was built against, 8 of 38 work items have one — so drift sees about a fifth of the
board, and silence from the other four fifths is absence of evidence, not evidence of
agreement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from tam.core import format_timestamp
from tam.retrieval.signals import timestamp

log = logging.getLogger("drift")

#: A ticket closing and the conversation continuing within a few minutes is one
#: exchange, not a disagreement — somebody says "closed" and a colleague says "thanks".
#: Below this, the two sources are telling the same story.
QUIET_AFTER_CLOSE_HOURS = 6.0


@dataclass
class Drift:
    """One disagreement between the conversation and the ticket."""

    kind: str
    item_id: str
    ticket: str
    ticket_state: str
    ticket_url: str
    our_state: str
    #: The message that makes this a disagreement — the last thing said in Slack, or the
    #: message that proves the state we inferred.
    evidence_id: str
    evidence: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "item_id": self.item_id,
            "ticket": self.ticket,
            "ticket_state": self.ticket_state,
            "ticket_url": self.ticket_url,
            "our_state": self.our_state,
            "evidence_id": self.evidence_id,
            "evidence": self.evidence,
            "detail": self.detail,
        }


def _mentioning(topic: Any, ticket: str) -> list[dict[str, Any]]:
    """Messages in this topic that name the ticket.

    This is the whole difference between a finding and an artefact. The first version
    compared the ticket's close time against the topic's last message, and every one of
    the six drifts it produced was wrong for the same reason: a topic here spans 41 to
    73 days across two to four channels, mentions its ticket once or twice, and its last
    message is about something else entirely. "The conversation continued after the
    ticket closed" was measuring the cluster's tail, not the ticket's.

    So the comparison is restricted to messages that actually name the ticket. Far fewer
    of them, and each one is about the thing being compared.
    """
    key = ticket.upper()
    hits = [
        record
        for record in topic.records
        if key in str(record.get("text", "")).upper() and np.isfinite(timestamp(record))
    ]
    return sorted(hits, key=timestamp)


def detect(topics: Sequence[Any], issues: Sequence[Any]) -> list[Drift]:
    """Compare each work item that has a ticket against that ticket.

    Items with no ticket key are skipped rather than guessed at: matching an item to a
    ticket by wording would invent the very link the comparison depends on, and a wrong
    link produces a confident disagreement about the wrong pair of things.
    """
    by_key = {issue.key.upper(): issue for issue in issues}
    drifts: list[Drift] = []

    for topic in topics:
        issue = by_key.get(str(topic.item_id).upper())
        if issue is None:
            continue

        mentions = _mentioning(topic, issue.key)
        last = mentions[-1] if mentions else None
        last_ts = timestamp(last) if last else float("nan")
        last_id = str(last["id"]) if last else ""
        talked_after = (
            np.isfinite(last_ts)
            and issue.updated > 0
            and last_ts > issue.updated + QUIET_AFTER_CLOSE_HOURS * 3600
        )

        if issue.resolved and topic.state == "blocked":
            # The sharpest one: a person has said they are stuck on work the board
            # believes is finished. Reported as blocked-vs-open because that is the
            # direction that matters — somebody is waiting.
            drifts.append(Drift(
                kind="slack_blocked_but_ticket_open",
                item_id=topic.item_id, ticket=issue.key, ticket_state=issue.state,
                ticket_url=issue.url, our_state=topic.state,
                evidence_id=topic.evidence_id or last_id,
                evidence=topic.evidence or "",
                detail=f"ticket ปิดแล้ว ({issue.state}) แต่ใน Slack ยังติดอยู่",
            ))
        elif issue.resolved and talked_after:
            hours = (last_ts - issue.updated) / 3600.0
            drifts.append(Drift(
                kind="ticket_closed_but_talking",
                item_id=topic.item_id, ticket=issue.key, ticket_state=issue.state,
                ticket_url=issue.url, our_state=topic.state,
                evidence_id=last_id,
                evidence=f"พูดถึง {issue.key} ครั้งล่าสุด {format_timestamp(str(last_ts))}",
                detail=(
                    f"ticket ปิดแล้ว ({issue.state}) เมื่อ {format_timestamp(str(issue.updated))} "
                    f"แต่ยังมีคนพูดถึง {issue.key} อีก {hours:.0f} ชั่วโมงหลังจากนั้น"
                ),
            ))
        elif not issue.resolved and topic.state == "resolved":
            drifts.append(Drift(
                kind="slack_done_but_ticket_open",
                item_id=topic.item_id, ticket=issue.key, ticket_state=issue.state,
                ticket_url=issue.url, our_state=topic.state,
                evidence_id=topic.evidence_id or last_id,
                evidence=topic.evidence or "",
                detail=f"Slack คุยจบแล้ว แต่ ticket ยังเปิดอยู่ ({issue.state or 'ไม่ทราบสถานะ'})",
            ))

    return drifts


def coverage(topics: Sequence[Any], issues: Sequence[Any]) -> dict[str, int]:
    """How much of the board drift can see at all.

    Reported alongside the drifts so a quiet result is readable: no drift among eight
    items that have tickets says nothing about the thirty that do not.
    """
    with_key = [t for t in topics if not str(t.item_id).startswith("c")]
    matched = {i.key.upper() for i in issues}
    return {
        "topics": len(topics),
        "with_ticket_key": len(with_key),
        "matched_in_youtrack": sum(1 for t in with_key if str(t.item_id).upper() in matched),
    }
