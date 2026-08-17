"""What happened, per work item — the analysis a daily digest is made of.

This module answers the standup questions without generating a word of prose:

* **What are the work items?**  graph.py's communities, not a keyword list.
* **What moved today?**  Items with activity inside the window, in the context of
  their whole history — a bug reported last week and fixed this morning is one
  item, not two.
* **What is stuck?**  An item whose latest typed relation is ``blocked_by`` and
  has no ``resolves`` after it. That is a fact derived from tam.analysis.relations, and the
  evidence is a specific message you can open.
* **Who is on it, and where was it discussed?**  Slack, the meeting, or both.

Everything here is deterministic and inspectable. summarize.py is the optional
layer that turns a `Topic` into a sentence a human wants to read; the state, the
participants, and the blocked-since date are computed here and never invented.

    python3 -m tam.analysis.digest --records data/processed/combined.json --days 7
    python3 -m tam.analysis.digest --blockers
    python3 -m tam.analysis.digest --item 2
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from dotenv import load_dotenv

from tam.retrieval.embeddings import apply_transform, fit_transform, quiet_third_party_logs, set_model
from tam.analysis.graph import EdgeWeights, build_graph, cluster_label, detect_communities
from tam.analysis.relations import Relation, extract_relations
from tam.core import DEFAULT_RECORDS, embed_records, format_timestamp, load_records
from tam.retrieval.signals import SignalIndex, timestamp

# A "day" of standup usually means "since the last one", which is rarely 24h.
DEFAULT_WINDOW_DAYS = 7
# Relations that say something about whether work is finished.
STATE_RELATIONS = ("resolves", "blocked_by", "duplicates", "follows_up", "answers")

log = logging.getLogger("digest")


@dataclass
class Topic:
    """One work item: its messages, who is on it, and whether it is stuck."""

    key: int
    label: str
    records: list[dict[str, Any]] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    state: str = "active"  # active | blocked | resolved
    evidence: str = ""
    evidence_id: str = ""
    state_since: float = float("nan")

    @property
    def participants(self) -> list[str]:
        return sorted({str(record.get("user") or "") for record in self.records} - {""})

    @property
    def sources(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            key = str(record.get("source") or "slack")
            counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def first_ts(self) -> float:
        times = [timestamp(record) for record in self.records]
        finite = [value for value in times if np.isfinite(value)]
        return min(finite) if finite else float("nan")

    @property
    def last_ts(self) -> float:
        times = [timestamp(record) for record in self.records]
        finite = [value for value in times if np.isfinite(value)]
        return max(finite) if finite else float("nan")

    @property
    def age_days(self) -> float:
        """Days since the state was last established — how long it has been stuck."""
        anchor = self.state_since if np.isfinite(self.state_since) else self.last_ts
        if not np.isfinite(anchor):
            return float("nan")
        return (datetime.now(tz=timezone.utc).timestamp() - anchor) / 86400.0

    def recent(self, since: float) -> list[dict[str, Any]]:
        """Messages inside the window, oldest first."""
        return sorted(
            (record for record in self.records if timestamp(record) >= since),
            key=lambda record: timestamp(record),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "evidence": self.evidence,
            "evidence_id": self.evidence_id,
            "participants": self.participants,
            "sources": self.sources,
            "messages": len(self.records),
            "first": format_timestamp(str(self.first_ts)),
            "last": format_timestamp(str(self.last_ts)),
            "age_days": None if np.isnan(self.age_days) else round(self.age_days, 1),
        }


@dataclass
class Digest:
    """Every topic with activity in the window, most recently active first."""

    topics: list[Topic]
    since: float
    until: float
    corpus_size: int

    @property
    def blocked(self) -> list[Topic]:
        """Stuck items, longest-stuck first — the standup's real agenda."""
        return sorted(
            (topic for topic in self.topics if topic.state == "blocked"),
            key=lambda topic: -(topic.age_days if np.isfinite(topic.age_days) else 0.0),
        )

    @property
    def resolved(self) -> list[Topic]:
        return [topic for topic in self.topics if topic.state == "resolved"]


def infer_state(topic_records: Sequence[dict[str, Any]], relations: Sequence[Relation], records: Sequence[dict[str, Any]]) -> tuple[str, str, str, float]:
    """Decide whether a work item is blocked, resolved, or simply active.

    The rule is the latest *typed* relation wins, judged by the timestamp of its
    later message. A `blocked_by` from Tuesday followed by a `resolves` on
    Thursday is resolved; the same `blocked_by` with nothing after it is still
    blocking someone right now. Untyped `same_topic` edges are ignored — they say
    two messages are related, not what happened.
    """
    member_ids = {str(record["id"]) for record in topic_records}
    relevant = [
        relation
        for relation in relations
        if relation.name in STATE_RELATIONS
        and str(records[relation.source]["id"]) in member_ids
        and str(records[relation.target]["id"]) in member_ids
    ]
    if not relevant:
        return "active", "", "", float("nan")

    def when(relation: Relation) -> float:
        moment = timestamp(records[relation.target])
        return moment if np.isfinite(moment) else 0.0

    latest = max(relevant, key=when)
    target = records[latest.target]
    marker = format_timestamp(str(target.get("ts", "")))
    if latest.name == "resolves":
        return "resolved", f"resolved on {marker} — {latest.evidence}", str(target["id"]), when(latest)
    if latest.name == "blocked_by":
        return "blocked", f"blocked since {marker} — {latest.evidence}", str(target["id"]), when(latest)
    return "active", f"last movement {marker} ({latest.name})", str(target["id"]), when(latest)


def build_digest(
    records: Sequence[dict[str, Any]],
    *,
    since: float,
    until: float | None = None,
    knn: int = 6,
    resolution: float = 1.0,
    method: str = "rules",
    min_messages: int = 2,
) -> Digest:
    """Cluster the whole corpus, then keep the topics that moved in the window.

    Clustering runs over everything on purpose. Restricting it to the window
    first would split a work item at the window boundary and report this
    morning's fix as an unrelated new topic.
    """
    records = list(records)
    until = until if until is not None else datetime.now(tz=timezone.utc).timestamp()

    raw = embed_records(records)
    matrix = apply_transform(raw, fit_transform(raw, "none"))
    signals = SignalIndex(records)
    graph = build_graph(records, matrix, signals, weights=EdgeWeights(), knn=knn)
    labels = detect_communities(graph, resolution=resolution)
    relations = extract_relations(records, [(int(a), int(b)) for a, b in graph.edges], method=method)

    topics: list[Topic] = []
    for community in sorted(set(labels)):
        members = [index for index, label in enumerate(labels) if label == community]
        member_records = [records[index] for index in members]
        if len(member_records) < min_messages:
            continue
        # Only topics touched inside the window belong in this digest.
        if not any(since <= timestamp(record) <= until for record in member_records):
            continue
        member_ids = {str(record["id"]) for record in member_records}
        own_relations = [
            relation
            for relation in relations
            if str(records[relation.source]["id"]) in member_ids and str(records[relation.target]["id"]) in member_ids
        ]
        state, evidence, evidence_id, state_since = infer_state(member_records, relations, records)
        topics.append(
            Topic(
                key=community,
                label=cluster_label(members, signals),
                records=sorted(member_records, key=lambda record: timestamp(record)),
                relations=sorted(own_relations, key=lambda relation: timestamp(records[relation.target])),
                state=state,
                evidence=evidence,
                evidence_id=evidence_id,
                state_since=state_since,
            )
        )

    # Blocked first, then most recently active — the order a standup reads in.
    order = {"blocked": 0, "active": 1, "resolved": 2}
    topics.sort(key=lambda topic: (order.get(topic.state, 1), -(topic.last_ts if np.isfinite(topic.last_ts) else 0.0)))
    return Digest(topics=topics, since=since, until=until, corpus_size=len(records))


def timeline(topic: Topic, records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """One work item as a chain of typed, dated events.

    This is what cosine similarity structurally cannot produce: not "these ten
    messages are similar" but "reported Monday, blocked Tuesday, fixed Thursday".

    Deduplicated by (relation, later message): one message that resolves a thread
    pairs with every earlier message in it, which is correct as graph edges and
    wrong as history — a timeline wants one row per event. The earliest partner
    is kept, because it is the message the event is actually answering.
    """
    events: dict[tuple[str, str], dict[str, Any]] = {}
    for relation in topic.relations:
        if relation.name == "same_topic":
            continue
        source, target = records[relation.source], records[relation.target]
        key = (relation.name, str(target["id"]))
        existing = events.get(key)
        if existing is not None and existing["from_ts"] <= timestamp(source):
            existing["also_answers"] += 1
            continue
        events[key] = {
            "relation": relation.name,
            "when": format_timestamp(str(target.get("ts", ""))),
            "ts": timestamp(target),
            "from_ts": timestamp(source),
            "from_id": str(source["id"]),
            "from_text": " ".join(str(source["text"]).split())[:160],
            "from_user": str(source.get("user") or "-"),
            "to_id": str(target["id"]),
            "to_text": " ".join(str(target["text"]).split())[:160],
            "to_user": str(target.get("user") or "-"),
            "evidence": relation.evidence,
            "also_answers": existing["also_answers"] if existing else 0,
        }
    return sorted(events.values(), key=lambda event: (event["ts"], event["relation"]))


def window_start(days: float) -> float:
    return (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--days", type=float, default=DEFAULT_WINDOW_DAYS, help=f"Window in days (default {DEFAULT_WINDOW_DAYS})")
    parser.add_argument("--blockers", action="store_true", help="Only the stuck items")
    parser.add_argument("--item", type=int, help="Show one topic's timeline by cluster key")
    parser.add_argument("--knn", type=int, default=6, help="Dense neighbours per message (default 6)")
    parser.add_argument("--resolution", type=float, default=1.0, help="Louvain resolution (default 1.0)")
    parser.add_argument("--method", default="rules", choices=("rules", "nli"), help="Relation typing (default rules)")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    parser.add_argument("--model", help="Embedding model id; overrides EMBEDDING_MODEL for this run")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()
    set_model(args.model)

    records = load_records(args.records)
    digest = build_digest(
        records, since=window_start(args.days), knn=args.knn, resolution=args.resolution, method=args.method
    )
    log.info("%d topic(s) active in the last %.0f day(s) over %d record(s)", len(digest.topics), args.days, len(records))

    if args.item is not None:
        matches = [topic for topic in digest.topics if topic.key == args.item]
        if not matches:
            raise SystemExit(f"No active topic with key {args.item}. Available: {[t.key for t in digest.topics]}")
        topic = matches[0]
        print(f"\n#{topic.key} {topic.label}   [{topic.state}]")
        print(f"{topic.evidence or 'no typed relation yet'}\n")
        events = timeline(topic, records)
        if not events:
            print("  (no typed relation in this topic — only same_topic edges)")
        for event in events:
            print(f"  {event['when']}  {event['relation']}")
            print(f"     from [{event['from_user']}] {event['from_text'][:88]}")
            print(f"       to [{event['to_user']}] {event['to_text'][:88]}")
        return

    topics = digest.blocked if args.blockers else digest.topics
    if args.json:
        print(json.dumps([topic.as_dict() for topic in topics], ensure_ascii=False, indent=2))
        return

    heading = "BLOCKED" if args.blockers else f"DIGEST — last {args.days:g} day(s)"
    print(f"\n{heading}\n{'=' * len(heading)}")
    if not topics:
        print("\n  (nothing" + (" blocked" if args.blockers else " moved in this window") + ")")
        return

    for topic in topics:
        marker = {"blocked": "!", "resolved": "+", "active": "·"}.get(topic.state, "·")
        sources = ", ".join(f"{count} {name}" for name, count in sorted(topic.sources.items()))
        age = f"{topic.age_days:.1f}d" if np.isfinite(topic.age_days) else "-"
        print(f"\n{marker} #{topic.key} {topic.label}")
        print(f"   {topic.state:8} {age:>7}   {len(topic.records)} msg ({sources})   {', '.join(topic.participants[:5])}")
        if topic.evidence:
            print(f"   {topic.evidence}")
        for record in topic.recent(digest.since)[-3:]:
            tag = "meeting" if record.get("source") == "meeting" else "slack"
            print(f"     [{tag}] {record.get('user') or '-'}: {' '.join(str(record['text']).split())[:92]}")

    print(f"\n{len(digest.blocked)} blocked · {len(digest.resolved)} resolved · {len(digest.topics)} active topics")
    print("state comes from typed relations (tam.analysis.relations); nothing here is generated")


if __name__ == "__main__":
    main()
