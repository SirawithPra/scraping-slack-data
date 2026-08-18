"""What happened, per work item — the analysis a daily digest is made of.

This module answers the standup questions without generating a word of prose:

* **What are the work items?**  graph.py's communities, not a keyword list. Each
  one is named by its ticket key where the linker found one and by a hash of its
  earliest message otherwise — never by the cluster index, which is a size rank
  and would rename every item on the next rebuild.
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
    python3 -m tam.analysis.digest --item REV-1421
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from dotenv import load_dotenv

from tam.retrieval.embeddings import apply_transform, fit_transform, quiet_third_party_logs, set_model
from tam.analysis.graph import DEFAULT_RESOLUTION, EdgeWeights, build_graph, cluster_label, detect_communities
from tam.analysis.linker import Link, link_records, load_overrides
from tam.analysis.relations import Relation, extract_relations
from tam.core import DEFAULT_RECORDS, embed_records, format_timestamp, load_records
from tam.ingest.quoted import for_analysis
from tam.ingest.blockers import blocker_lines
from tam.ingest.standup import cleared_blockers, declared_blockers
from tam.ingest.users import Names
from tam.retrieval.signals import SignalIndex, timestamp

# A "day" of standup usually means "since the last one", which is rarely 24h.
DEFAULT_WINDOW_DAYS = 7
# Relations that say something about whether work is finished or stuck. Only
# these two decide state — see infer_state.
STATE_RELATIONS = ("resolves", "blocked_by")
# Typed relations that prove an item moved without saying anything about whether
# it is still stuck. Chasing an item, answering a question about it, or filing it
# twice is activity, not progress.
MOVEMENT_RELATIONS = ("duplicates", "follows_up", "answers")
# A reply this short, in a topic this large, is not enough to change the item's state
# on its own. Both numbers come from reading the state-deciding message of every
# resolved item on a real export: the three that were wrong sat at 7, 8 and 9
# characters against 27-39 message topics, and the smallest legitimate one was 35
# characters. See infer_state.may_set_state.
ACK_CHARS = 25
ACK_MAX_TOPIC = 10

log = logging.getLogger("digest")

# One resolver per process, built on first use. Reading the name cache per topic would
# re-read the file for every render; the mode cannot change inside a run anyway.
_names: Names | None = None


def names() -> Names:
    """The display-name resolver for this process. See tam.ingest.users."""
    global _names
    if _names is None:
        _names = Names()
    return _names


@dataclass
class Topic:
    """One work item: its messages, who is on it, and whether it is stuck.

    `key` is the Louvain cluster index, which is a *size rank* — one new message
    that grows a cluster past its neighbour renumbers both. It is fine as a
    position inside one build and useless as a name to store: anything that
    outlives a rebuild (a human's correction, a cross-reference in Slack) must
    quote `item_id` instead, which is derived from content — see `item_ids`.
    """

    key: int
    label: str
    item_id: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    state: str = "active"  # active | blocked | resolved
    evidence: str = ""
    evidence_id: str = ""
    state_since: float = float("nan")

    @property
    def participants(self) -> list[str]:
        """Raw ids and transcript speaker names, as stored. For machines.

        Kept unresolved on purpose: this is what a caller needs to DM someone or to
        join a person to a message. `participant_names` is the same list rendered for
        a reader, and both go on the wire so neither use has to guess.
        """
        return sorted({str(record.get("user") or "") for record in self.records} - {""})

    @property
    def participant_names(self) -> list[str]:
        """The same people, readable. See tam.ingest.users for the modes."""
        return names().all(self.participants)

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
            "item_id": self.item_id,
            "label": self.label,
            "state": self.state,
            "evidence": self.evidence,
            "evidence_id": self.evidence_id,
            "participants": self.participants,
            # Both, deliberately: ids address a person, names describe one, and a
            # consumer that has to derive one from the other gets it wrong.
            "participant_names": self.participant_names,
            "sources": self.sources,
            "messages": len(self.records),
            "first": format_timestamp(str(self.first_ts)),
            "last": format_timestamp(str(self.last_ts)),
            # The epochs alongside the display strings: 'YYYY-MM-DD HH:mm' carries no
            # zone, so a reader in another timezone reparsing it as local miscomputes
            # staleness by the offset. Anything deciding how old this is reads these.
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
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


def apply_declarations(
    topic_records: Sequence[dict[str, Any]],
    declarations: dict[str, list[str]],
    clearances: dict[str, float],
    state: str,
    evidence: str,
    evidence_id: str,
    state_since: float,
) -> tuple[str, str, str, float]:
    """Let a self-declared blocker override an inferred state.

    `declarations` maps a record id to what its author wrote under the blockers
    heading. If one of this topic's messages carries such a declaration that nobody has
    since withdrawn, the item is blocked and the evidence is the person's own words —
    no cue, no inference, and nothing to argue about.

    `clearances` maps a user to the last time they answered the blockers question with
    "none". That, and not a cue elsewhere in the cluster, is what retires a
    declaration. The distinction was measured, not assumed: this used to defer to any
    later `resolved` state in the same topic, and on the real export all three declared
    blockers land in one 71-message, 69-day cluster that a `closed` cue marks resolved
    on its last day — so every declaration in it was silently retired, including
    "waiting for clearing user on dev because data issues", which nobody had cleared.
    A cluster that wide is a channel, not a work item, and its tail says nothing about
    any one message in it. The same error, in the same shape, once made every drift
    finding an artifact (see `analysis/drift._mentioning`).

    Whoever wrote a blocker is the one who gets to say it is gone, so each declaration
    is tested against its own author's later "none" — not against the cluster's, and
    not against anybody else's.
    """
    declared = [
        (record, declarations[str(record["id"])])
        for record in topic_records
        if str(record["id"]) in declarations
    ]
    # Test every declaration, not just the newest: a person can clear Monday's blocker
    # while Tuesday's still stands, and taking the newest first would let the cleared
    # one decide for both.
    standing = [
        (record, answers)
        for record, answers in declared
        if clearances.get(str(record.get("user") or ""), -np.inf) <= timestamp(record)
    ]
    if not standing:
        return state, evidence, evidence_id, state_since

    record, answers = max(standing, key=lambda pair: timestamp(pair[0]))
    when = timestamp(record)
    marker = format_timestamp(str(record.get("ts", "")))
    answer = " / ".join(answers)[:120]
    return "blocked", f'blocked since {marker} — คนกรอกเองว่า "{answer}"', str(record["id"]), when


def infer_state(topic_records: Sequence[dict[str, Any]], relations: Sequence[Relation], records: Sequence[dict[str, Any]]) -> tuple[str, str, str, float]:
    """Decide whether a work item is blocked, resolved, or simply active.

    The rule is the latest *state-bearing* relation wins, judged by the timestamp
    of its later message. A `blocked_by` from Tuesday followed by a `resolves` on
    Thursday is resolved; the same `blocked_by` with nothing after it is still
    blocking someone right now.

    Only `resolves` and `blocked_by` are state-bearing. Somebody asking "any
    update?" on Wednesday is a `follows_up`, and a `follows_up` is not a
    `resolves` — chasing a blocked item is the most common thing that happens to
    one, and letting it clear the blocker would empty the standup agenda exactly
    when it matters. Those relations still count as movement, so they are shown
    after the state, not instead of it. Untyped `same_topic` edges are ignored —
    they say two messages are related, not what happened.
    """
    member_ids = {str(record["id"]) for record in topic_records}

    def may_set_state(relation: Relation) -> bool:
        """Whether this relation's later message is allowed to decide the item's state.

        Two refusals, both measured on a real 936-message export rather than guessed.

        A bot may not. `resolves` fires on a deploy notification reading "success",
        which is a machine reporting that a command finished, not a teammate reporting
        that the work is done. One of fifteen resolved items was decided this way.

        A bare acknowledgement may not decide a large item. Three of fifteen were
        resolved by a message of seven, eight and nine characters — the Thai
        equivalents of "done" and "all set" — standing for clusters of 27, 35 and 39
        messages spanning weeks. In a three-message item such a reply is unambiguous
        about what it refers to. In a forty-message one it is not, and the cost of
        being wrong is a standup told that unfinished work is finished. The rule is
        proportionate rather than absolute for exactly that reason.
        """
        later = records[relation.target]
        if later.get("is_bot"):
            return False
        return not (len(for_analysis(later)) <= ACK_CHARS and len(topic_records) > ACK_MAX_TOPIC)

    relevant = [
        relation
        for relation in relations
        if relation.name in STATE_RELATIONS + MOVEMENT_RELATIONS
        and str(records[relation.source]["id"]) in member_ids
        and str(records[relation.target]["id"]) in member_ids
    ]
    if not relevant:
        return "active", "", "", float("nan")

    def when(relation: Relation) -> float:
        moment = timestamp(records[relation.target])
        return moment if np.isfinite(moment) else 0.0

    def marker_for(relation: Relation) -> str:
        return format_timestamp(str(records[relation.target].get("ts", "")))

    newest = max(relevant, key=when)
    stateful = [relation for relation in relevant if relation.name in STATE_RELATIONS and may_set_state(relation)]
    if not stateful:
        return "active", f"last movement {marker_for(newest)} ({newest.name})", str(records[newest.target]["id"]), when(newest)

    latest = max(stateful, key=when)
    target = records[latest.target]
    marker = marker_for(latest)
    # The evidence id stays on the message that proves the state; a chase after it
    # is reported as movement, and never as the reason the item is blocked.
    moved = f" · last movement {marker_for(newest)} ({newest.name})" if when(newest) > when(latest) else ""
    if latest.name == "resolves":
        return "resolved", f"resolved on {marker} — {latest.evidence}{moved}", str(target["id"]), when(latest)
    return "blocked", f"blocked since {marker} — {latest.evidence}{moved}", str(target["id"]), when(latest)


def content_id(topic_records: Sequence[dict[str, Any]]) -> str:
    """A short id for a work item nobody filed a ticket for.

    Hashed from the id of its earliest message, because that is the one thing
    about a cluster that a rebuild does not move: new messages arrive at the end,
    and the message that started the item stays the message that started it.
    """
    def order(record: dict[str, Any]) -> tuple[float, str]:
        moment = timestamp(record)
        return (moment if np.isfinite(moment) else float("inf"), str(record["id"]))

    earliest = min(topic_records, key=order)
    return "c" + hashlib.sha1(str(earliest["id"]).encode("utf-8")).hexdigest()[:6]


def item_ids(members_by_topic: Sequence[Sequence[dict[str, Any]]], links: dict[str, Link]) -> list[str]:
    """A stable name per work item: its ticket key where there is one.

    The cluster index cannot be the identity — it is a size rank, so `TAM-3`
    denotes a different item after any rebuild that changes relative cluster
    sizes, and every stored cross-reference then points at the wrong work. A
    ticket key is content the team typed, so it survives; `content_id` covers the
    work nobody filed.

    A ticket names at most one item: when Louvain splits one ticket's discussion,
    the half that mentions it most keeps the key and the other falls back to its
    hash, so two items can never answer to the same name.
    """
    def ticket_of(topic_records: Sequence[dict[str, Any]]) -> tuple[str, int]:
        votes = Counter(
            links[str(record["id"])].ticket
            for record in topic_records
            if str(record["id"]) in links and links[str(record["id"])].ticket
        )
        return votes.most_common(1)[0] if votes else ("", 0)

    claims = [ticket_of(topic_records) for topic_records in members_by_topic]
    owner: dict[str, int] = {}
    for position, (ticket, votes) in enumerate(claims):
        if ticket and (ticket not in owner or votes > claims[owner[ticket]][1]):
            owner[ticket] = position
    return [
        ticket if ticket and owner.get(ticket) == position else content_id(members_by_topic[position])
        for position, (ticket, _) in enumerate(claims)
    ]


def build_digest(
    records: Sequence[dict[str, Any]],
    *,
    since: float,
    until: float | None = None,
    knn: int = 6,
    resolution: float = DEFAULT_RESOLUTION,
    method: str = "rules",
    min_messages: int = 2,
    overrides: dict[str, str] | None = None,
) -> Digest:
    """Cluster the whole corpus, then keep the topics that moved in the window.

    Clustering runs over everything on purpose. Restricting it to the window
    first would split a work item at the window boundary and report this
    morning's fix as an unrelated new topic.

    `overrides` is the linker's human tier (`linker.load_overrides`). It only
    affects what each item is *called*, never its state — but a human who
    reassigned a message to a ticket has said which work item it belongs to, and
    that is exactly the question `item_ids` answers.
    """
    records = list(records)
    until = until if until is not None else datetime.now(tz=timezone.utc).timestamp()

    raw = embed_records(records)
    matrix = apply_transform(raw, fit_transform(raw, "none"))
    signals = SignalIndex(records)
    graph = build_graph(records, matrix, signals, weights=EdgeWeights(), knn=knn)
    labels = detect_communities(graph, resolution=resolution)
    relations = extract_relations(records, [(int(a), int(b)) for a, b in graph.edges], method=method)
    # Self-declared blockers, keyed by the message that carries them. Computed once per
    # build: reading the form is a property of the corpus, not of a topic.
    declarations = {str(record["id"]): answers for record, answers in declared_blockers(records)}
    # The form's blockers field is one way this team declares an obstacle; a bullet inside
    # a long daily post is the commoner one, and both are the person's own words about
    # their own work. Fed through the same tier for that reason: `apply_declarations`
    # already holds the two rules that matter — a declaration outranks anything inferred,
    # and only its own author can withdraw it. Measured: 26 lines across 17 posts, against
    # 3 messages the whole-message cue list can reach (docs/EXPERIMENTS.md §7.1).
    for line in blocker_lines(records):
        declarations.setdefault(line.record_id, []).append(line.line)
    # The latest "no blockers" per person. Only the declarer's own withdrawal retires
    # their declaration — see apply_declarations for the measurement that forced this.
    clearances: dict[str, float] = {}
    for record in cleared_blockers(records):
        user = str(record.get("user") or "")
        moment = timestamp(record)
        if np.isfinite(moment):
            clearances[user] = max(clearances.get(user, -np.inf), moment)
    # Same clustering the linker would compute, handed to it, so the work items
    # here and the links there cannot disagree about who is in which cluster.
    links = {
        link.record_id: link
        for link in link_records(records, [int(label) for label in labels], overrides=overrides)
    }

    topics: list[Topic] = []
    for community in sorted(set(labels)):
        members = [index for index, label in enumerate(labels) if label == community]
        member_records = [records[index] for index in members]
        if len(member_records) < min_messages:
            continue
        # A work item is something people talked about; a ticket annotates one, and must
        # never be able to create one on its own. Measured when the tracker was first
        # merged into the corpus: of 194 issues, 82 landed in topics with real
        # conversation — the win this merge exists for — while 112 collapsed into three
        # topics that had almost no messages at all, the largest holding 108 issues
        # beside 2 unrelated lines. That topic would render as one work item named
        # "api, then, view" with 110 messages, and it is not a work item, it is a
        # backlog. Tickets nobody has discussed already have a report that handles them
        # properly, with dates and a quiet-days threshold: `tam.analysis.drift`'s silent
        # list. Requiring conversation to at least match the tracker records states the
        # intent — this digest is a record of what people said, annotated with tickets —
        # rather than tuning a number, and the issues stay in the corpus either way, so
        # retrieval and the comparison still reach every one of them.
        talked = sum(1 for record in member_records if record.get("source") != "youtrack")
        if talked * 2 < len(member_records):
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
        # A person who typed an obstacle under "Are there any blockers?" has labelled it
        # themselves, which outranks anything inferred from a cue. Applied after
        # infer_state rather than inside it because it is evidence of a different kind:
        # not a relation between two messages, but one message's own declaration. On the
        # real export only one of the three such declarations contains any word from the
        # blocked_by cue list, so this finds what no keyword list can.
        state, evidence, evidence_id, state_since = apply_declarations(
            member_records, declarations, clearances, state, evidence, evidence_id, state_since
        )
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

    for topic, name in zip(topics, item_ids([topic.records for topic in topics], links)):
        topic.item_id = name

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
    is kept, because it is the message the event is actually answering; the ones
    it displaces are counted, so `also_answers` is the same number whichever
    order the relations happen to arrive in.
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
            "from_user": names().of(source.get("user")) or "-",
            "to_id": str(target["id"]),
            "to_text": " ".join(str(target["text"]).split())[:160],
            "to_user": names().of(target.get("user")) or "-",
            "evidence": relation.evidence,
            "also_answers": existing["also_answers"] + 1 if existing else 0,
        }
    return sorted(events.values(), key=lambda event: (event["ts"], event["relation"]))


def window_start(days: float) -> float:
    return (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--days", type=float, default=DEFAULT_WINDOW_DAYS, help=f"Window in days (default {DEFAULT_WINDOW_DAYS})")
    parser.add_argument("--blockers", action="store_true", help="Only the stuck items")
    parser.add_argument("--item", help="Show one topic's timeline, by stable item id (REV-1421, c3f9a2b) or cluster key")
    parser.add_argument("--knn", type=int, default=6, help="Dense neighbours per message (default 6)")
    parser.add_argument("--resolution", type=float, default=DEFAULT_RESOLUTION, help=f"Louvain resolution (default {DEFAULT_RESOLUTION:g})")
    parser.add_argument("--method", default="rules", choices=("rules", "nli"), help="Relation typing (default rules)")
    parser.add_argument("--overrides", type=Path, help="Human link corrections, as tam.analysis.linker reads them — they name work items, not states")
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
    try:
        overrides = load_overrides(args.overrides)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    digest = build_digest(
        records,
        since=window_start(args.days),
        knn=args.knn,
        resolution=args.resolution,
        method=args.method,
        overrides=overrides,
    )
    log.info("%d topic(s) active in the last %.0f day(s) over %d record(s)", len(digest.topics), args.days, len(records))

    if args.item is not None:
        wanted = args.item.strip().upper()
        matches = [
            topic for topic in digest.topics if topic.item_id.upper() == wanted or str(topic.key) == wanted
        ]
        if not matches:
            available = ", ".join(f"{topic.item_id} (#{topic.key})" for topic in digest.topics)
            raise SystemExit(f"No active topic {args.item}. Available: {available}")
        topic = matches[0]
        print(f"\n{topic.item_id} · #{topic.key} {topic.label}   [{topic.state}]")
        print(f"{topic.evidence or 'no typed relation yet'}\n")
        events = timeline(topic, records)
        if not events:
            print("  (no typed relation in this topic — only same_topic edges)")
        for event in events:
            print(f"  {event['when']}  {event['relation']}")
            print(f"     from [{event['from_user']}] {names().in_text(event['from_text'])[:88]}")
            print(f"       to [{event['to_user']}] {names().in_text(event['to_text'])[:88]}")
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
        print(f"\n{marker} {topic.item_id} · #{topic.key} {topic.label}")
        print(f"   {topic.state:8} {age:>7}   {len(topic.records)} msg ({sources})   {', '.join(topic.participant_names[:5])}")
        if topic.evidence:
            print(f"   {topic.evidence}")
        for record in topic.recent(digest.since)[-3:]:
            tag = "meeting" if record.get("source") == "meeting" else "slack"
            body = names().in_text(" ".join(str(record["text"]).split()))
            print(f"     [{tag}] {names().of(record.get('user')) or '-'}: {body[:92]}")

    print(f"\n{len(digest.blocked)} blocked · {len(digest.resolved)} resolved · {len(digest.topics)} active topics")
    print("state comes from typed relations (tam.analysis.relations); nothing here is generated")


if __name__ == "__main__":
    main()
