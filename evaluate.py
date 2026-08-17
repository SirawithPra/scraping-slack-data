"""Score retrieval pipelines on a labelled query set.

    cp data/eval_queries.example.json data/eval_queries.json  # then edit the ids
    python3 evaluate.py
    python3 evaluate.py --presets dense hybrid hybrid-rerank full
    python3 evaluate.py --eval-file data/eval_queries.weak.json --presets dense hybrid

Four metrics, because Recall@K alone hides the thing that matters most — *where*
in the list the answer landed:

* **Recall@K** — share of the labelled messages found in the top K. Answers
  "did we get them", ignores order entirely.
* **nDCG@K** — the same, discounted by position, so rank 1 counts far more than
  rank 10. This is the metric to optimise: it is what a user experiences.
* **MRR** — 1 / rank of the *first* correct hit. Answers "how far do they scroll
  before something useful appears".
* **MAP** — precision averaged over every correct hit. Punishes a ranking that
  finds one good message then buries the rest.

Every metric is reported against its ceiling where it has one. A query with 10
labelled messages caps Recall@1 at 0.10, so a bare 0.10 is a perfect score, not a
broken one.

Label file format (`exclude_ids` is optional, used by weak_labels.py to drop the
message the query was taken from):
    [{"query": "BE sorting API พร้อมแล้ว", "relevant_ids": ["msg_C123_1754000500.000500"]}]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from dotenv import load_dotenv

from embeddings import model_name, quiet_third_party_logs, set_model
from semantic_search import DEFAULT_RECORDS, load_records

DEFAULT_EVAL_FILE = Path("data/eval_queries.json")
DEFAULT_KS = (1, 3, 5, 10)

log = logging.getLogger("evaluate")


def load_eval_set(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(
            f"Missing {path}. Copy data/eval_queries.example.json to {path} and label your own ids, "
            "or generate a set with: python3 weak_labels.py"
        )
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path} is not valid JSON: {error}") from error
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"{path} should contain a non-empty list of labelled queries.")
    for position, case in enumerate(cases, start=1):
        if not isinstance(case, dict) or not str(case.get("query", "")).strip():
            raise SystemExit(f"Entry {position} in {path} needs a non-empty 'query'.")
        if not isinstance(case.get("relevant_ids"), list):
            raise SystemExit(f"Entry {position} in {path} needs 'relevant_ids' as a list.")
    return cases


# ---- metrics ---------------------------------------------------------------


def recall_at_k(ranked_ids: Sequence[str], relevant: set[str], k: int) -> float:
    """Share of the labelled relevant records that appear in the top k."""
    if not relevant:
        return 0.0
    return len(relevant.intersection(ranked_ids[:k])) / len(relevant)


def recall_ceiling(relevant_counts: Sequence[int], k: int) -> float:
    """Best mean recall@k any ranking could reach.

    A query with n labelled messages caps at min(k, n)/n, so with n > k a
    perfect ranking still scores below 1.0. Without this, Recall@1 looks broken
    when it is in fact maxed out.
    """
    if not relevant_counts:
        return 0.0
    return sum(min(k, count) / count for count in relevant_counts) / len(relevant_counts)


def ndcg_at_k(ranked_ids: Sequence[str], relevant: set[str], k: int) -> float:
    """Position-discounted gain, normalised by the best possible arrangement.

    Binary relevance, log2 discount. Already normalised against its own ideal, so
    unlike recall this one really can reach 1.00 at any k — it compares against
    "the same labels, perfectly ordered", not against "all labels in the top k".
    """
    if not relevant:
        return 0.0
    gains = sum(1.0 / math.log2(rank + 1) for rank, record_id in enumerate(ranked_ids[:k], start=1) if record_id in relevant)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(k, len(relevant)) + 1))
    return gains / ideal if ideal else 0.0


def first_hit_rank(ranked_ids: Sequence[str], relevant: set[str]) -> int | None:
    for rank, record_id in enumerate(ranked_ids, start=1):
        if record_id in relevant:
            return rank
    return None


def reciprocal_rank(ranked_ids: Sequence[str], relevant: set[str]) -> float:
    """1 / rank of the first correct hit; 0 if there is none."""
    rank = first_hit_rank(ranked_ids, relevant)
    return 1.0 / rank if rank else 0.0


def average_precision(ranked_ids: Sequence[str], relevant: set[str]) -> float:
    """Precision at each correct hit, averaged over all of them."""
    if not relevant:
        return 0.0
    hits = 0
    total = 0.0
    for rank, record_id in enumerate(ranked_ids, start=1):
        if record_id in relevant:
            hits += 1
            total += hits / rank
    return total / len(relevant)


@dataclass
class Metrics:
    """Mean metrics over a query set, plus what the labels made achievable."""

    recall: dict[int, float] = field(default_factory=dict)
    ndcg: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    mean_average_precision: float = 0.0
    worst_first_hit: int = 0
    ceilings: dict[int, float] = field(default_factory=dict)
    queries: int = 0

    def row(self, ks: Sequence[int]) -> list[float]:
        """Values for a table, ordered recall then nDCG then MRR then MAP."""
        return [self.recall[k] for k in ks] + [self.ndcg[k] for k in ks] + [self.mrr, self.mean_average_precision]


def evaluate_rankings(
    rankings: Sequence[Sequence[str]], relevant_sets: Sequence[set[str]], ks: Sequence[int]
) -> Metrics:
    """Aggregate every metric over aligned rankings and label sets."""
    if not rankings:
        raise ValueError("evaluate_rankings needs at least one ranking.")
    metrics = Metrics(queries=len(rankings))
    counts = [len(relevant) for relevant in relevant_sets]
    for k in ks:
        metrics.recall[k] = float(np.mean([recall_at_k(r, s, k) for r, s in zip(rankings, relevant_sets)]))
        metrics.ndcg[k] = float(np.mean([ndcg_at_k(r, s, k) for r, s in zip(rankings, relevant_sets)]))
        metrics.ceilings[k] = recall_ceiling(counts, k)
    metrics.mrr = float(np.mean([reciprocal_rank(r, s) for r, s in zip(rankings, relevant_sets)]))
    metrics.mean_average_precision = float(np.mean([average_precision(r, s) for r, s in zip(rankings, relevant_sets)]))
    metrics.worst_first_hit = max(
        (first_hit_rank(r, s) or len(r)) for r, s in zip(rankings, relevant_sets)
    )
    return metrics


# ---- running a pipeline over a label set -----------------------------------


def usable_cases(
    cases: Sequence[dict[str, Any]], known_ids: set[str]
) -> tuple[list[str], list[set[str]], list[set[str]]]:
    """Keep queries with at least one labelled id in the corpus.

    Returns (queries, relevant sets, ids to drop from the ranking). The third is
    for weak labels, where the query *is* one of the messages and would otherwise
    rank first against itself.
    """
    from visualize import shorten

    queries: list[str] = []
    relevant_sets: list[set[str]] = []
    excluded: list[set[str]] = []
    for case in cases:
        relevant = {str(value) for value in case["relevant_ids"]}.intersection(known_ids)
        if not relevant:
            log.warning("Skipping %r: no labelled id is in the corpus", shorten(str(case["query"]), 40))
            continue
        queries.append(str(case["query"]))
        relevant_sets.append(relevant)
        excluded.append({str(value) for value in case.get("exclude_ids", [])})
    return queries, relevant_sets, excluded


def rank_for_cases(retriever: Any, queries: Sequence[str], excluded: Sequence[set[str]]) -> list[list[str]]:
    """One full ranking per query, with excluded ids removed."""
    rankings = []
    for query, drop in zip(queries, excluded):
        ranked = retriever.rank_ids(query)
        rankings.append([record_id for record_id in ranked if record_id not in drop] if drop else ranked)
    return rankings


def print_table(names: Sequence[str], results: Sequence[Metrics], ks: Sequence[int]) -> None:
    """One row per pipeline. The ceiling row is the reference, not 1.00."""
    ceiling_label = "best possible recall"
    width = max(max(len(name) for name in names), len(ceiling_label)) + 2
    header = (
        f"{'pipeline':{width}}"
        + " ".join(f"R@{k:<5}" for k in ks)
        + " "
        + " ".join(f"nDCG@{k:<3}" for k in ks)
        + " MRR     MAP     worst"
    )
    print("\n" + header)
    print("-" * len(header))
    for name, metrics in zip(names, results):
        cells = " ".join(f"{metrics.recall[k]:<7.2f}" for k in ks)
        gains = " ".join(f"{metrics.ndcg[k]:<8.2f}" for k in ks)
        print(f"{name:{width}}{cells} {gains} {metrics.mrr:<7.2f} {metrics.mean_average_precision:<7.2f} {metrics.worst_first_hit:>5}")
    ceilings = results[0].ceilings
    print(f"{ceiling_label:{width}}" + " ".join(f"{ceilings[k]:<7.2f}" for k in ks))
    print("\nR@k       share of labelled messages inside the top k (capped by label counts, see last row)")
    print("nDCG@k    the same, discounted by position — rank 1 counts more than rank 10; 1.00 is reachable")
    print("MRR       1 / rank of the first correct hit, averaged over queries")
    print("MAP       precision at every correct hit, averaged — rewards finding all of them, not just one")
    print("worst     deepest first-hit rank across queries (1 is best)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--eval-file", type=Path, default=DEFAULT_EVAL_FILE, help=f"Labelled queries (default {DEFAULT_EVAL_FILE})")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--ks", type=int, nargs="+", default=list(DEFAULT_KS), help="K values (default 1 3 5 10)")
    parser.add_argument("--preset", help="Single pipeline preset to score")
    parser.add_argument("--presets", nargs="+", help="Several presets to score side by side")
    parser.add_argument("--per-query", action="store_true", help="Also print each query's own numbers")
    parser.add_argument("--include-threads", action="store_true", help="Also rank whole-thread records")
    parser.add_argument("--model", help="Embedding model id; overrides EMBEDDING_MODEL for this run")
    parser.add_argument("--no-cache", action="store_true", help="Recompute embeddings instead of using the cache")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()
    from retrieve import DEFAULT_PRESET, PRESETS, Retriever, build_retriever
    from visualize import shorten

    set_model(args.model)
    ks = tuple(sorted({k for k in args.ks if k > 0}))
    if not ks:
        raise SystemExit("--ks needs at least one positive integer.")
    presets = args.presets or [args.preset or DEFAULT_PRESET]
    unknown = [name for name in presets if name not in PRESETS]
    if unknown:
        raise SystemExit(f"Unknown preset(s) {', '.join(unknown)}. Available: {', '.join(PRESETS)}")

    cases = load_eval_set(args.eval_file)
    records = load_records(args.records, include_threads=args.include_threads)
    record_ids = {str(record["id"]) for record in records}
    queries, relevant_sets, excluded = usable_cases(cases, record_ids)
    if not queries:
        raise SystemExit(f"No labelled id in {args.eval_file} matches {args.records}.")
    log.info("Scoring %d preset(s) on %d query/queries over %d record(s)", len(presets), len(queries), len(records))

    # One embedding pass shared by every preset, so the comparison is cheap and
    # the presets provably see identical vectors.
    base = build_retriever(records, presets[0], use_cache=not args.no_cache)
    results: list[Metrics] = []
    for preset in presets:
        retriever: Retriever = base if preset == presets[0] else base.with_config(PRESETS[preset])
        log.info("Preset %s = %s", preset, retriever.config.describe())
        rankings = rank_for_cases(retriever, queries, excluded)
        metrics = evaluate_rankings(rankings, relevant_sets, ks)
        results.append(metrics)
        if args.per_query:
            print(f"\n{preset}")
            for query, ranking, relevant in zip(queries, rankings, relevant_sets):
                parts = "  ".join(f"R@{k}={recall_at_k(ranking, relevant, k):.2f}" for k in ks)
                rank = first_hit_rank(ranking, relevant)
                print(f"  {shorten(query, 52):54} relevant={len(relevant):<3} {parts}  first-hit={rank or '-'}")

    print_table(presets, results, ks)
    print(f"\nmodel {model_name()} · {len(records)} records · {len(queries)} labelled queries from {args.eval_file}")
    if len(queries) < 20:
        print(
            f"WARNING: {len(queries)} queries is too few to separate pipelines — one message changing "
            f"place moves R@{ks[0]} by {1 / len(queries):.2f}. Generate more with: python3 weak_labels.py"
        )


if __name__ == "__main__":
    main()
