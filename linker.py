"""Which work item does a message belong to?

Clustering answers this without supervision, and for work nobody filed a ticket
for it is the *only* answer. But when a message says ``REV-1421``, guessing is
absurd: the ticket key is the answer already, and signals.py has been extracting
it all along — as one score among many, never as an identity.

This module makes the identity explicit and ranks the evidence behind it:

    tier        how the link was made                        key
    --------------------------------------------------------------------------
    override    a human said so                              wins over all
    explicit    the text contains a ticket key                ticket:REV-1421
    thread      a reply inherits its thread's link            ticket:REV-1421
    consensus   most linked messages in the cluster agree     ticket:REV-1421
    cluster     community membership, nothing more            cluster:7
    unassigned  none of the above                             ""

Two properties matter more than the accuracy of any single tier:

* **Every link names its evidence.** A link nobody can check is worse than no
  link, because it is a confident wrong answer. ``Link.evidence`` is a sentence.
* **Unassigned is a visible state, not a silent drop.** Messages that could not
  be placed are returned like any other, with an empty key, so the digest can
  say "3 threads we could not place" instead of quietly losing them.

``consensus`` is the tier that makes the hybrid worth having: it carries a ticket
key from the two messages that typed it to the eight that did not, without
asking anyone to type it again.

    python3 linker.py --records data/processed/combined.json
    python3 linker.py --records data/processed/syn.json --no-cluster
    python3 linker.py --explain msg_C0DEMOCHAN1_1786630933.931999
    python3 linker.py --overrides data/link_overrides.json --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv

from embeddings import apply_transform, fit_transform, quiet_third_party_logs, set_model
from graph import EdgeWeights, build_graph, cluster_label, detect_communities
from semantic_search import DEFAULT_RECORDS, embed_records, load_records
from signals import ANCHOR_PATTERNS, SignalIndex

# One definition of a ticket key, shared with the anchor extractor. Importing the
# pattern instead of restating it means a fix to either one fixes both.
TICKET_PATTERN = next(pattern for kind, pattern in ANCHOR_PATTERNS if kind == "ticket")

# `PREFIX-123` also describes half the standards a developer types in a day, and
# `UTF-8` really is in the sample export. As one retrieval signal among many that
# is harmless — IDF discounts it and nothing depends on it. As an *identity* it is
# not: one false key invents a work item, and the consensus tier then spreads it
# across a whole cluster. So the identity path is deliberately stricter than the
# retrieval path.
NON_TICKET_PREFIXES = frozenset(
    {
        "UTF", "ISO", "ASCII", "ANSI", "BASE", "SHA", "MD", "AES", "RSA", "HMAC",
        "RFC", "CVE", "ISBN", "IPV", "UTC", "GMT", "HTTP", "HTTPS", "TLS", "SSL",
        "GPT", "ES", "CSS", "HTML", "IEEE", "PCI", "SOC", "ARM", "X", "T", "COVID",
    }
)


# A cluster this small is not a work item, it is a loose end. Its messages are
# reported as unassigned so someone can place them.
MIN_CLUSTER_SIZE = 2

# Fraction of a cluster's *already linked* messages that must agree on one ticket
# before the rest of the cluster inherits it.
CONSENSUS_SHARE = 0.5

# Confidence per tier. These are not probabilities — they order the tiers and
# give the UI something to render as "how sure is this".
TIER_CONFIDENCE = {
    "override": 1.0,
    "explicit": 1.0,
    "thread": 0.9,
    "consensus": 0.75,
    "cluster": 0.5,
    "unassigned": 0.0,
}

log = logging.getLogger("linker")


@dataclass(frozen=True)
class Link:
    """One message's work item, and why."""

    record_id: str
    key: str
    tier: str
    confidence: float
    evidence: str

    @property
    def linked(self) -> bool:
        return bool(self.key)

    @property
    def ticket(self) -> str:
        """The ticket key, or "" when this link is only a cluster."""
        return self.key.split(":", 1)[1] if self.key.startswith("ticket:") else ""


def ticket_prefix(key: str) -> str:
    """``REV-1421`` -> ``REV``."""
    return key.split("-", 1)[0]


def trusted_prefixes(records: Sequence[dict[str, Any]], *, configured: Sequence[str] | None = None) -> set[str]:
    """Which `PREFIX-123` prefixes may be treated as ticket identities.

    A configured list always wins: a team knows its own project keys, and nothing
    beats being told. Without one, the corpus is the evidence — a real project
    prefix shows up with *several different numbers* (REV-1421, REV-1500), while
    a standard is always the same string (UTF-8, UTF-8, UTF-8). That rule tunes
    itself as the channel grows and needs no list to be complete.
    """
    named = {prefix.strip().upper() for prefix in configured or () if prefix.strip()}
    if named:
        return named
    numbers: dict[str, set[str]] = defaultdict(set)
    for record in records:
        for match in TICKET_PATTERN.finditer(str(record.get("text", ""))):
            key = match.group(0).upper()
            numbers[ticket_prefix(key)].add(key)
    trusted = {
        prefix
        for prefix, keys in numbers.items()
        if prefix not in NON_TICKET_PREFIXES and (len(keys) > 1 or len(prefix) >= 3)
    }
    rejected = sorted(set(numbers) - trusted)
    if rejected:
        log.info("ignoring %d prefix(es) that look like standards, not tickets: %s", len(rejected), ", ".join(rejected))
    return trusted


def ticket_keys(text: str, trusted: set[str] | None = None) -> list[str]:
    """Ticket keys in `text`, upper-cased, first occurrence first.

    Surface form is folded to upper case because ``rev-1421`` and ``REV-1421``
    are the same ticket, and upper is how every tracker prints it. Pass `trusted`
    from `trusted_prefixes()` to keep standards like ``UTF-8`` out.
    """
    seen: list[str] = []
    for match in TICKET_PATTERN.finditer(text):
        value = match.group(0).upper()
        if trusted is not None and ticket_prefix(value) not in trusted:
            continue
        if value not in seen:
            seen.append(value)
    return seen


def corpus_ticket_counts(records: Sequence[dict[str, Any]], trusted: set[str] | None = None) -> Counter[str]:
    """How often each ticket key is mentioned across the whole corpus."""
    counts: Counter[str] = Counter()
    for record in records:
        for key in ticket_keys(str(record.get("text", "")), trusted):
            counts[key] += 1
    return counts


def pick_ticket(candidates: Sequence[str], counts: Counter[str]) -> str:
    """The one ticket a message is *about*, when it names several.

    A message that mentions two tickets usually belongs to the one the channel is
    already discussing and merely references the other ("blocked by REV-1400").
    Corpus frequency is a decent proxy for that; position breaks the tie, since
    the subject tends to come first.
    """
    if not candidates:
        return ""
    return max(candidates, key=lambda key: (counts.get(key, 0), -candidates.index(key)))


def thread_of(record: dict[str, Any]) -> str:
    """The thread this record belongs to, or "" when it is not in one."""
    thread = str(record.get("thread_ts", "") or "")
    return "" if thread in {"", "None", "nan"} else thread


def link_records(
    records: Sequence[dict[str, Any]],
    labels: Sequence[int] | None = None,
    *,
    overrides: dict[str, str] | None = None,
    min_cluster_size: int = MIN_CLUSTER_SIZE,
    consensus_share: float = CONSENSUS_SHARE,
    cluster_names: dict[int, str] | None = None,
    projects: Sequence[str] | None = None,
) -> list[Link]:
    """Assign every record a work item key, cheapest reliable evidence first.

    `labels` is one community id per record, as `graph.detect_communities`
    returns. Pass None to run without clustering at all: the explicit and thread
    tiers need no embeddings, which makes the linker cheap enough to run on every
    incoming message and re-run the expensive tiers on a schedule.
    """
    records = list(records)
    overrides = overrides or {}
    trusted = trusted_prefixes(records, configured=projects)
    counts = corpus_ticket_counts(records, trusted)
    names = cluster_names or {}

    keys: list[str] = [""] * len(records)
    tiers: list[str] = ["unassigned"] * len(records)
    evidence: list[str] = [""] * len(records)

    def assign(index: int, key: str, tier: str, reason: str) -> None:
        keys[index] = key
        tiers[index] = tier
        evidence[index] = reason

    # ---- tier: explicit -----------------------------------------------------
    for index, record in enumerate(records):
        candidates = ticket_keys(str(record.get("text", "")), trusted)
        if not candidates:
            continue
        ticket = pick_ticket(candidates, counts)
        reason = f"ข้อความอ้าง {ticket}"
        if len(candidates) > 1:
            others = ", ".join(key for key in candidates if key != ticket)
            reason += f" (อ้าง {others} ด้วย แต่ {ticket} ถูกพูดถึงมากกว่าในแชนเนล)"
        assign(index, f"ticket:{ticket}", "explicit", reason)

    # ---- tier: thread -------------------------------------------------------
    # A reply is about whatever its thread is about. Direction does not matter:
    # the key can arrive from the parent or from a later reply that named it.
    threads: dict[str, list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        thread = thread_of(record)
        if thread:
            threads[thread].append(index)
    for thread, members in threads.items():
        voted = Counter(keys[index] for index in members if tiers[index] == "explicit" and keys[index])
        if not voted:
            continue
        key, votes = voted.most_common(1)[0]
        for index in members:
            if keys[index]:
                continue
            assign(index, key, "thread", f"อยู่ใน thread ที่ {votes} ข้อความอ้าง {key.split(':', 1)[1]}")

    # ---- tiers: consensus and cluster --------------------------------------
    if labels is not None:
        members_by_label: dict[int, list[int]] = defaultdict(list)
        for index, label in enumerate(labels):
            members_by_label[int(label)].append(index)

        for label, members in sorted(members_by_label.items()):
            if len(members) < min_cluster_size:
                continue  # a loose end, not a work item — leave it unassigned
            linked = [index for index in members if keys[index].startswith("ticket:")]
            voted = Counter(keys[index] for index in linked)
            adopted = ""
            if len(linked) >= 2 and voted:
                key, votes = voted.most_common(1)[0]
                if votes / len(linked) >= consensus_share:
                    adopted = key
                    share = f"{votes} จาก {len(linked)}"
            name = names.get(label, str(label))
            for index in members:
                if keys[index]:
                    continue
                if adopted:
                    assign(
                        index,
                        adopted,
                        "consensus",
                        f"อยู่คลัสเตอร์ “{name}” ซึ่ง {share} ข้อความที่ผูกแล้วอ้าง {adopted.split(':', 1)[1]}",
                    )
                else:
                    assign(index, f"cluster:{label}", "cluster", f"อยู่คลัสเตอร์ “{name}” ({len(members)} ข้อความ)")

    # ---- tier: override -----------------------------------------------------
    # Last, so a human always wins — including the power to unlink by passing "".
    by_id = {str(record["id"]): index for index, record in enumerate(records)}
    for record_id, key in overrides.items():
        index = by_id.get(record_id)
        if index is None:
            log.warning("override for unknown record %s — ignored", record_id)
            continue
        if key:
            assign(index, key, "override", "คนแก้เอง")
        else:
            assign(index, "", "unassigned", "คนสั่งให้ปลดการผูก")

    return [
        Link(
            record_id=str(record["id"]),
            key=keys[index],
            tier=tiers[index],
            confidence=TIER_CONFIDENCE.get(tiers[index], 0.0),
            evidence=evidence[index],
        )
        for index, record in enumerate(records)
    ]


def cluster_labels(records: Sequence[dict[str, Any]], *, knn: int = 6, resolution: float = 1.0) -> tuple[list[int], dict[int, str]]:
    """Community per record, plus a readable name per community.

    Same path digest.py takes, so the cluster tier here and the work items there
    cannot disagree about who is in which cluster.
    """
    raw = embed_records(records)
    matrix = apply_transform(raw, fit_transform(raw, "none"))
    signals = SignalIndex(records)
    graph = build_graph(records, matrix, signals, weights=EdgeWeights(), knn=knn)
    labels = detect_communities(graph, resolution=resolution)
    names: dict[int, str] = {}
    for label in sorted(set(labels)):
        members = [index for index, value in enumerate(labels) if value == label]
        names[int(label)] = cluster_label(members, signals)
    return [int(label) for label in labels], names


def by_key(links: Sequence[Link]) -> dict[str, list[Link]]:
    """Links grouped by work item, unassigned excluded."""
    grouped: dict[str, list[Link]] = defaultdict(list)
    for link in links:
        if link.linked:
            grouped[link.key].append(link)
    return dict(grouped)


def unassigned(links: Sequence[Link]) -> list[Link]:
    return [link for link in links if not link.linked]


def tier_counts(links: Sequence[Link]) -> Counter[str]:
    return Counter(link.tier for link in links)


def load_overrides(path: Path | None) -> dict[str, str]:
    """``{record_id: work_item_key}`` from disk. Missing file is not an error.

    Overrides are the one part of the linker a human writes, so losing them to a
    reindex would be unforgivable — they live in their own file, never in the
    derived records.
    """
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return {str(key): str(value) for key, value in data.items()}
    # Also accept the list-of-events shape a UI would append to.
    return {str(row["record_id"]): str(row.get("key", "")) for row in data}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--overrides", type=Path, help="JSON of human corrections: {record_id: work_item_key}")
    parser.add_argument("--projects", help="Comma-separated ticket prefixes, e.g. REV,PROJ. Overrides TICKET_PROJECTS and the corpus guess")
    parser.add_argument("--no-cluster", action="store_true", help="Explicit and thread tiers only — no embeddings")
    parser.add_argument("--knn", type=int, default=6, help="Dense neighbours per message (default 6)")
    parser.add_argument("--resolution", type=float, default=1.0, help="Louvain resolution (default 1.0)")
    parser.add_argument("--min-cluster", type=int, default=MIN_CLUSTER_SIZE, help=f"Smallest cluster that counts as a work item (default {MIN_CLUSTER_SIZE})")
    parser.add_argument("--explain", help="Show one record's link and the evidence for it")
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
    labels: list[int] | None = None
    names: dict[int, str] = {}
    if not args.no_cluster:
        labels, names = cluster_labels(records, knn=args.knn, resolution=args.resolution)

    links = link_records(
        records,
        labels,
        overrides=load_overrides(args.overrides),
        min_cluster_size=args.min_cluster,
        cluster_names=names,
        projects=[prefix for prefix in (args.projects or os.getenv("TICKET_PROJECTS", "")).split(",") if prefix.strip()],
    )
    counts = tier_counts(links)
    texts = {str(record["id"]): str(record.get("text", "")) for record in records}

    if args.explain:
        matches = [link for link in links if link.record_id == args.explain]
        if not matches:
            raise SystemExit(f"No record with id {args.explain}")
        link = matches[0]
        print(f"\n{link.record_id}")
        print(f"  {texts[link.record_id][:160]}")
        print(f"\n  work item : {link.key or '(ยังจับคู่ไม่ได้)'}")
        print(f"  tier      : {link.tier}  (confidence {link.confidence:.2f})")
        print(f"  หลักฐาน    : {link.evidence or '—'}")
        return

    if args.json:
        print(json.dumps({"links": [asdict(link) for link in links], "tiers": dict(counts)}, ensure_ascii=False, indent=2))
        return

    grouped = by_key(links)
    print(f"\nLINKED WORK ITEMS — {len(grouped)} item(s) over {len(records)} record(s)")
    print("=" * 62)
    for key, members in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        mix = ", ".join(f"{tier} {count}" for tier, count in Counter(link.tier for link in members).most_common())
        print(f"\n{key:24} {len(members):3} msg   [{mix}]")
        print(f"   {members[0].evidence}")
        for link in members[:2]:
            print(f"     · {texts[link.record_id][:88]}")

    loose = unassigned(links)
    print(f"\n\nยังจับคู่ไม่ได้ — {len(loose)} ข้อความ")
    print("=" * 62)
    for link in loose[:10]:
        print(f"  {link.record_id}")
        print(f"     {texts[link.record_id][:88]}")
    if len(loose) > 10:
        print(f"  … อีก {len(loose) - 10} ข้อความ")

    print("\n" + "  ".join(f"{tier}={count}" for tier, count in counts.most_common()))
    print("ทุก link มีหลักฐานกำกับ และของที่ผูกไม่ได้ไม่ถูกซ่อน")


if __name__ == "__main__":
    main()
