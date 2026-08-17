"""Generate a labelled evaluation set from Slack threads, with no hand labelling.

The measurement problem is worse than the model problem. Four hand-labelled
queries over 38 messages cannot separate two models: one message changing place
moves Recall@5 by 0.2, so every difference is noise. Hand-labelling 50 queries is
a day of work that has to be redone whenever the corpus changes.

Slack already contains the labels. Messages in one thread are the same work item
by definition, so:

    query        = one message from a thread
    relevant_ids = the other messages in that thread

That is free, it scales with the export, and it regenerates whenever the corpus
does. What it measures is "given one message, can the pipeline find the rest of
its conversation" — a proxy for real search, not a substitute:

* **Easier than a real query** in vocabulary: a real user types a paraphrase from
  memory, while a thread message shares wording with its own thread.
* **Harder than a real query** in specificity: the target set is exactly one
  conversation, so a topically perfect but wrong-thread hit counts as a miss.

So read weak-label numbers as *relative* — good for ranking pipelines against each
other, not for quoting an absolute recall figure. Keep a small hand-labelled set
alongside it, and check the two agree on which pipeline wins.

    python3 -m tam.evaluation.weak_labels
    python3 -m tam.evaluation.weak_labels --per-thread 2 --min-thread 3 --out data/eval_queries.weak.json
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from tam.core import DEFAULT_RECORDS, load_records

DEFAULT_OUTPUT = Path("data/eval_queries.weak.json")
# Below this, a "thread" is one message plus an acknowledgement and the label set
# is too thin to score anything.
MIN_THREAD_SIZE = 3

log = logging.getLogger("weak_labels")


def thread_groups(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Messages grouped by thread, thread-context records excluded.

    A slack_thread record is a concatenation of its own thread's messages, so
    including it would put the answer inside the query.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record.get("source") == "slack_thread":
            continue
        thread = str(record.get("thread_ts") or "")
        if thread:
            groups[thread].append(record)
    for members in groups.values():
        members.sort(key=lambda record: str(record.get("ts", "")))
    return groups


def build_cases(
    records: list[dict[str, Any]], *, per_thread: int = 1, min_thread: int = MIN_THREAD_SIZE
) -> list[dict[str, Any]]:
    """One case per chosen message: its text as query, its thread-mates as labels.

    `per_thread` messages are taken evenly across each thread rather than from the
    front, because a thread's opening message and its closing message are very
    different queries — the opener describes a problem, the closer reports a fix.
    """
    cases: list[dict[str, Any]] = []
    for thread, members in sorted(thread_groups(records).items()):
        if len(members) < min_thread:
            continue
        take = max(1, min(per_thread, len(members)))
        step = max(1, len(members) // take)
        for position in range(0, take * step, step):
            if position >= len(members):
                break
            anchor = members[position]
            relevant = [str(other["id"]) for other in members if other["id"] != anchor["id"]]
            if not relevant:
                continue
            cases.append(
                {
                    "query": str(anchor["text"]),
                    "relevant_ids": relevant,
                    # Kept so evaluate.py can exclude the query's own record and so
                    # a human can see where a case came from.
                    "exclude_ids": [str(anchor["id"])],
                    "source": "thread_weak_label",
                    "thread_ts": thread,
                }
            )
    return cases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help=f"Output label file (default {DEFAULT_OUTPUT})")
    parser.add_argument("--per-thread", type=int, default=1, help="Queries to take from each thread (default 1)")
    parser.add_argument("--min-thread", type=int, default=MIN_THREAD_SIZE, help=f"Smallest usable thread (default {MIN_THREAD_SIZE})")
    parser.add_argument("--max-query-chars", type=int, default=400, help="Truncate long messages used as queries (default 400)")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()

    records = load_records(args.records, include_threads=True)
    cases = build_cases(records, per_thread=args.per_thread, min_thread=args.min_thread)
    if not cases:
        raise SystemExit(
            f"No thread in {args.records} has {args.min_thread} messages. "
            "Export a channel with real conversations, or lower --min-thread."
        )
    for case in cases:
        if len(case["query"]) > args.max_query_chars:
            case["query"] = case["query"][: args.max_query_chars]

    threads = len({case["thread_ts"] for case in cases})
    labels = sum(len(case["relevant_ids"]) for case in cases)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info("Wrote %d case(s) from %d thread(s), %d label(s) total, to %s", len(cases), threads, labels, args.out)
    print(f"\n{len(cases)} weak-labelled queries, {labels / len(cases):.1f} relevant messages each on average.")
    print(f"Use them with:  python3 -m tam.evaluation.evaluate --eval-file {args.out}")
    print("Read the numbers as relative — see this module's docstring for why.")


if __name__ == "__main__":
    main()
