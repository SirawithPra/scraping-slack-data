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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from tam.core import DEFAULT_RECORDS, format_timestamp
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

#: Days an open ticket may sit untouched before it is worth surfacing. Chosen from this
#: team's own rhythm rather than picked: the distribution of "days since last change" over
#: 61 open issues is p25=5, median=8, p75=22, so 21 is roughly the point where a ticket
#: has gone quiet by their standards. It also falls in a natural gap — no open ticket sat
#: between 14 and 21 days — so the threshold is not slicing through a dense region, which
#: is what makes a cutoff arbitrary. Override with TAM_SILENT_DAYS.
SILENT_DAYS = 21


@dataclass
class Silent:
    """An open ticket nobody has touched, and whether anyone discussed it."""

    ticket: str
    state: str
    url: str
    summary: str
    quiet_days: float
    #: Whether the ticket key appears anywhere in the Slack corpus. Reported per item
    #: rather than used as a filter: measured on this corpus, being stale in the tracker
    #: almost perfectly implies not being mentioned in Slack (24 of 24 at 21 days), so
    #: requiring both would add a condition that changes nothing while implying the
    #: finding needs two sources. It does not. What it needs two sources to know is the
    #: *difference* between a ticket nobody has touched and one being actively discussed
    #: without the tracker being updated, which is why the field is kept and shown.
    mentioned_in_slack: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "state": self.state,
            "url": self.url,
            "summary": self.summary,
            "quiet_days": round(self.quiet_days, 1),
            "mentioned_in_slack": self.mentioned_in_slack,
        }


def silent_tickets(
    issues: Sequence[Any],
    records: Sequence[dict[str, Any]],
    *,
    days: float | None = None,
    now: float | None = None,
) -> list[Silent]:
    """Open tickets that have gone quiet, longest-quiet first.

    This is the half of the picture Slack cannot contain, and the reason is structural
    rather than clever: work that nobody is discussing leaves no trace in a chat log, so
    no amount of reading Slack — by rule, by model, by anything — will surface it. On the
    corpus this was built against, 61 tickets were open and Slack mentioned 5.

    Unlike drift, this needs no ticket key in Slack, so it covers every open ticket
    rather than the quarter that happen to have been typed into a channel.

    `mentioned_in_slack` is bounded by what was exported: the corpus here spans 74 days,
    so "not mentioned" means "not in the window we have", not "never". A caller reporting
    this to people should say which window.
    """
    import os as _os

    limit = days if days is not None else float(_os.getenv("TAM_SILENT_DAYS", "").strip() or SILENT_DAYS)
    at = now if now is not None else datetime.now(tz=timezone.utc).timestamp()
    corpus = " ".join(str(record.get("text", "")) for record in records).upper()

    quiet: list[Silent] = []
    for issue in issues:
        if issue.resolved or not issue.updated:
            continue
        idle = (at - issue.updated) / 86400.0
        if idle < limit:
            continue
        quiet.append(Silent(
            ticket=issue.key,
            state=issue.state,
            url=issue.url,
            summary=issue.summary,
            quiet_days=idle,
            mentioned_in_slack=issue.key.upper() in corpus,
        ))
    return sorted(quiet, key=lambda s: -s.quiet_days)


# ---- command line ----------------------------------------------------------


def _parse_args() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--days", type=float, default=3650.0, help="Digest window for the Slack side (default 3650)")
    parser.add_argument("--silent-days", type=float, help=f"Quiet threshold for open tickets (default {SILENT_DAYS})")
    parser.add_argument("--project", help="Project to read for silent tickets; defaults to YOUTRACK_PROJECTS")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    return parser.parse_args()


def main() -> None:
    import json as _json
    import logging as _logging

    from dotenv import load_dotenv

    from tam.core import load_records
    from tam.ingest.youtrack import YouTrackError, config, fetch_by_keys, fetch_project

    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    args = _parse_args()

    from tam.analysis.digest import build_digest, window_start
    from tam.retrieval.embeddings import quiet_third_party_logs

    quiet_third_party_logs()
    records = load_records(args.records)
    digest = build_digest(records, since=window_start(args.days))

    try:
        _, _, projects = config()
        keys = [topic.item_id for topic in digest.topics if not str(topic.item_id).startswith("c")]
        issues = fetch_by_keys(keys) if keys else []
        project = args.project or (projects[0] if projects else "")
        every = fetch_project(project) if project else []
    except YouTrackError as error:
        raise SystemExit(f"YouTrack: {error}")

    drifts = detect(digest.topics, issues)
    quiet = silent_tickets(every, records, days=args.silent_days)
    cover = coverage(digest.topics, issues)

    if args.json:
        print(_json.dumps({
            "coverage": cover,
            "drift": [d.as_dict() for d in drifts],
            "silent": [s.as_dict() for s in quiet],
        }, ensure_ascii=False, indent=2))
        return

    print(f"\nงานใน Slack {cover['topics']} ชิ้น · มี ticket key {cover['with_ticket_key']} · match ใน YouTrack {cover['matched_in_youtrack']}")
    print(f"ticket ในโปรเจกต์ {project}: {len(every)} · เปิดอยู่ {sum(1 for i in every if not i.resolved)}")

    print(f"\n=== ขัดกัน ({len(drifts)}) — Slack กับ ticket เล่าไม่ตรงกัน ===")
    if not drifts:
        print("  (ไม่มี — ในบรรดางานที่มี ticket key เท่านั้น)")
    for d in drifts:
        print(f"  {d.ticket:14} {d.ticket_state:14} เราว่า {d.our_state}")
        print(f"     {d.detail}")

    print(f"\n=== เงียบ ({len(quiet)}) — ticket เปิดค้าง ไม่ถูกแตะเกิน {args.silent_days or SILENT_DAYS:.0f} วัน ===")
    for s in quiet[:20]:
        talk = "มีคนพูดถึงใน Slack" if s.mentioned_in_slack else "ไม่มีใครพูดถึง"
        print(f"  {s.ticket:14} {s.state:16} เงียบ {s.quiet_days:>4.0f} วัน · {talk}")
        print(f"     {s.summary[:66]}")
    if len(quiet) > 20:
        print(f"  … อีก {len(quiet)-20} อัน")


if __name__ == "__main__":
    main()
