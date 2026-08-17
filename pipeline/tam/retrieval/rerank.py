"""Cross-encoder reranking: score (query, message) jointly instead of comparing vectors.

This is the one stage that is not a similarity metric at all. A bi-encoder has to
compress a message into a vector *before* it has seen the query, so everything it
discards is gone. A cross-encoder reads query and message together in one forward
pass, with attention across both, and outputs a relevance score directly. It can
tell "the sorting API is ready" from "waiting on the sorting API" — same words,
opposite meaning, near-identical embeddings.

The cost is that it cannot be indexed: N messages means N forward passes per
query. So it runs only over the top candidates from the cheap stages. That
retrieve-then-rerank split is why it is worth having both.

    python3 -m tam.retrieval.rerank -q "bug ใน Profile module แก้แล้วยัง"
    RERANKER_MODEL=jinaai/jina-reranker-v2-base-multilingual python3 -m tam.retrieval.rerank -q "..."
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

# XLM-RoBERTa-large based, trained on 100+ languages including Thai. The largest
# of the options and the only one worth trusting on Thai/English mixed text.
DEFAULT_RERANKER = "BAAI/bge-reranker-v2-m3"
DEFAULT_DEPTH = 50  # candidates to rerank; the cost is linear in this number
BATCH_SIZE = 16


@dataclass(frozen=True)
class RerankerSpec:
    trust_remote_code: bool = False
    max_length: int = 512
    note: str = ""


# Anything loadable by sentence_transformers.CrossEncoder. Qwen3-Reranker is
# deliberately absent: it is a causal LM scored on yes/no token logits, so it
# needs its own loader rather than this class.
RERANKER_SPECS: dict[str, RerankerSpec] = {
    "bge-reranker-v2-m3": RerankerSpec(max_length=1024, note="568M, multilingual, strongest here"),
    "bge-reranker-base": RerankerSpec(note="278M, faster, weaker outside en/zh"),
    "jina-reranker-v2-base-multilingual": RerankerSpec(trust_remote_code=True, max_length=1024, note="278M, fast, multilingual"),
    "gte-multilingual-reranker-base": RerankerSpec(trust_remote_code=True, max_length=1024, note="306M, long context"),
}

log = logging.getLogger(__name__)

_reranker: Any = None
_reranker_name: str = ""


def reranker_name() -> str:
    """Configured cross-encoder id."""
    return os.getenv("RERANKER_MODEL", "").strip() or DEFAULT_RERANKER


def reranker_spec(name: str | None = None) -> RerankerSpec:
    lowered = (name or reranker_name()).lower()
    for pattern, spec in RERANKER_SPECS.items():
        if pattern in lowered:
            return spec
    return RerankerSpec(note="unknown reranker; assuming plain CrossEncoder defaults")


def set_reranker(name: str | None) -> None:
    """Switch rerankers, dropping the loaded one so the next call reloads."""
    global _reranker
    if not name:
        return
    if name != reranker_name():
        _reranker = None
    os.environ["RERANKER_MODEL"] = name


def _load_reranker() -> Any:
    global _reranker, _reranker_name
    name = reranker_name()
    if _reranker is None or _reranker_name != name:
        from sentence_transformers import CrossEncoder  # heavy import

        spec = reranker_spec(name)
        log.info("Loading cross-encoder %s (the first run downloads it)", name)
        if spec.note:
            log.info("  %s", spec.note)
        _reranker = CrossEncoder(name, trust_remote_code=spec.trust_remote_code, max_length=spec.max_length)
        _reranker_name = name
    return _reranker


def rerank_scores(query: str, texts: Sequence[str]) -> np.ndarray:
    """Relevance of each text to the query, higher is better.

    The absolute values are not comparable across reranker models — some emit
    logits, some probabilities. Only the ordering is meaningful, which is all the
    fusion layer needs.
    """
    if not texts:
        return np.zeros(0, dtype=np.float32)
    scores = _load_reranker().predict(
        [(query, text) for text in texts],
        batch_size=BATCH_SIZE,
        show_progress_bar=len(texts) > 4 * BATCH_SIZE,
        convert_to_numpy=True,
    )
    return np.asarray(scores, dtype=np.float32).reshape(-1)


def rerank_top(
    query: str, texts: Sequence[str], base_scores: np.ndarray, *, depth: int = DEFAULT_DEPTH
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rerank the best `depth` candidates and splice them back above the tail.

    Returns (new scores for every record, reranked indices, their raw
    cross-encoder scores). Candidates keep their cross-encoder order and stay
    above everything that was never looked at — a candidate the reranker dislikes
    is still a candidate the first stage liked, so it must not fall below the
    unexamined tail.
    """
    empty = np.zeros(0, dtype=np.int64)
    if not len(base_scores):
        return base_scores, empty, empty.astype(np.float32)
    depth = max(1, min(depth, len(base_scores)))
    candidates = np.argpartition(-base_scores, depth - 1)[:depth]
    scores = rerank_scores(query, [texts[index] for index in candidates])

    reranked = base_scores.astype(np.float32).copy()
    tail = np.ones(len(base_scores), dtype=bool)
    tail[candidates] = False
    # Rank-based offset rather than raw values, so an unbounded logit scale
    # cannot swamp the tail's own ordering.
    order = np.argsort(-scores)
    lift = float(reranked[tail].max()) + 1.0 if tail.any() else 0.0
    for position, local in enumerate(order):
        reranked[candidates[local]] = lift + float(depth - position)
    return reranked, candidates, scores


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-q", "--query", action="append", required=True, help="Query to run; repeatable")
    parser.add_argument("--records", type=Path, default=Path("data/processed/messages.json"), help="Prepared records")
    parser.add_argument("--top-k", type=int, default=10, help="Matches to show (default 10)")
    parser.add_argument("--model", help="Cross-encoder id; overrides RERANKER_MODEL")
    return parser.parse_args()


def main() -> None:
    """Rerank the whole corpus for one query, to see the cross-encoder alone."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from tam.retrieval.embeddings import quiet_third_party_logs
    from tam.core import load_records

    quiet_third_party_logs()
    args = parse_args()
    set_reranker(args.model)
    records: list[dict[str, Any]] = load_records(args.records)
    texts = [str(record["text"]) for record in records]

    for query in args.query:
        scores = rerank_scores(query, texts)
        print(f"\nSearch:\n> {query}   ({reranker_name()})")
        for position, index in enumerate(np.argsort(-scores)[: args.top_k], start=1):
            print(f"{position:2}. {scores[index]:7.3f}  {' '.join(texts[index].split())[:120]}")


if __name__ == "__main__":
    main()
