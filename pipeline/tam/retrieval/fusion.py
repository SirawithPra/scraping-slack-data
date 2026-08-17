"""Combine several rankings, and rescale scores so they can be combined at all.

Every retriever here produces a score on its own scale: BM25 is unbounded and
often exactly 0, cosine sits in a narrow band, a cross-encoder emits logits. Two
ways to merge them, and the choice matters more than it looks:

* **Reciprocal Rank Fusion** (`rrf_fuse`) throws the scores away and uses only
  positions. Scale-free by construction, so a retriever with a wide score range
  cannot drown a good one — the safe default, and what to use when the retrievers
  are of unknown or unequal quality.
* **Z-score fusion** (`zscore_fuse`) keeps the magnitudes after standardising
  them per query. It preserves "this match is *far* better than the next", which
  ranks throw away, but one badly-behaved retriever can still dominate.

`jaccard_rerank` is a different idea again: it rewrites scores using the
*structure of the neighbourhood* rather than any single distance. Two messages
that appear in each other's neighbour lists are related in a way that a raw
pairwise number cannot express.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Sequence

import numpy as np

RRF_DAMPING = 60  # standard constant; higher flattens the advantage of the top ranks

log = logging.getLogger(__name__)


def to_ranks(scores: np.ndarray) -> np.ndarray:
    """1-based rank of every entry, best score first. Ties break by index."""
    order = np.argsort(-scores, kind="stable")
    ranks = np.empty(len(scores), dtype=np.int64)
    ranks[order] = np.arange(1, len(scores) + 1)
    return ranks


def rrf_fuse(
    score_arrays: Sequence[np.ndarray], *, weights: Sequence[float] | None = None, damping: int = RRF_DAMPING
) -> np.ndarray:
    """Sum weight / (damping + rank) over retrievers. Higher is better."""
    arrays = [array for array in score_arrays if array is not None and array.size]
    if not arrays:
        raise ValueError("rrf_fuse needs at least one non-empty score array.")
    chosen = list(weights) if weights is not None else [1.0] * len(arrays)
    if len(chosen) != len(arrays):
        raise ValueError(f"Got {len(chosen)} weight(s) for {len(arrays)} retriever(s).")
    fused = np.zeros(len(arrays[0]), dtype=np.float32)
    for weight, array in zip(chosen, arrays):
        fused += np.float32(weight) / (damping + to_ranks(array))
    return fused


def zscore(scores: np.ndarray) -> np.ndarray:
    """Standardise one retriever's scores. A constant array becomes all zeros."""
    deviation = float(scores.std())
    if deviation < 1e-9:
        return np.zeros_like(scores, dtype=np.float32)
    return ((scores - scores.mean()) / deviation).astype(np.float32)


def zscore_fuse(score_arrays: Sequence[np.ndarray], *, weights: Sequence[float] | None = None) -> np.ndarray:
    """Weighted sum of per-retriever z-scores, keeping relative magnitudes."""
    arrays = [array for array in score_arrays if array is not None and array.size]
    if not arrays:
        raise ValueError("zscore_fuse needs at least one non-empty score array.")
    chosen = list(weights) if weights is not None else [1.0] * len(arrays)
    if len(chosen) != len(arrays):
        raise ValueError(f"Got {len(chosen)} weight(s) for {len(arrays)} retriever(s).")
    fused = np.zeros(len(arrays[0]), dtype=np.float32)
    for weight, array in zip(chosen, arrays):
        fused += np.float32(weight) * zscore(array)
    return fused


def minmax(scores: np.ndarray) -> np.ndarray:
    """Squash to 0-1 for display. Never use this to fuse — outliers set the scale."""
    low, high = float(scores.min()), float(scores.max())
    if high - low < 1e-9:
        return np.zeros_like(scores, dtype=np.float32)
    return ((scores - low) / (high - low)).astype(np.float32)


def fuse_rankings(rankings: Sequence[Sequence[str]], *, damping: int = RRF_DAMPING) -> list[str]:
    """RRF over id lists rather than score arrays, for comparing whole models.

    Same arithmetic as `rrf_fuse`; this form is convenient when the retrievers do
    not even share a vector space, so there is no aligned score array to build.
    """
    totals: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, record_id in enumerate(ranking, start=1):
            totals[record_id] += 1.0 / (damping + rank)
    return sorted(totals, key=lambda record_id: -totals[record_id])


def neighbour_sets(matrix: np.ndarray, size: int) -> list[set[int]]:
    """Each record's `size` nearest neighbours in the corpus, itself excluded."""
    if len(matrix) < 2:
        return [set() for _ in range(len(matrix))]
    with np.errstate(all="ignore"):  # Accelerate BLAS flag noise; see embeddings.cosine_scores
        similarity = matrix @ matrix.T
    np.fill_diagonal(similarity, -np.inf)
    size = max(1, min(size, len(matrix) - 1))
    top = np.argpartition(-similarity, size - 1, axis=1)[:, :size]
    return [set(row.tolist()) for row in top]


def jaccard_rerank(
    scores: np.ndarray,
    matrix: np.ndarray,
    *,
    depth: int = 20,
    neighbours: int = 10,
    blend: float = 0.5,
) -> np.ndarray:
    """Re-score the top `depth` candidates by neighbourhood overlap.

    The k-reciprocal idea, reduced to its useful core: a candidate is promoted
    when the query's own nearest neighbours and the candidate's nearest
    neighbours are largely the *same messages*. That is a statement about shared
    context, and it survives cases where the direct similarity is mediocre —
    a short reply like "fixed, deploying now" is close to nothing on its own,
    but its neighbourhood is the thread the query is about.

    `blend` is how much of the original score is kept: 1.0 changes nothing,
    0.0 ranks purely on overlap.
    """
    if len(matrix) < 3 or not 0.0 <= blend <= 1.0:
        return scores
    depth = max(1, min(depth, len(scores)))
    candidates = np.argpartition(-scores, depth - 1)[:depth]
    sets = neighbour_sets(matrix, neighbours)
    query_set = set(np.argsort(-scores)[:neighbours].tolist())

    overlap = np.zeros(len(scores), dtype=np.float32)
    for index in candidates:
        candidate_set = sets[index] | {int(index)}
        union = query_set | candidate_set
        if union:
            overlap[index] = len(query_set & candidate_set) / len(union)

    # Normalise both parts over the candidate slice only: the tail below `depth`
    # is untouched, so including it would shift the scale for no reason.
    mask = np.zeros(len(scores), dtype=bool)
    mask[candidates] = True
    blended = scores.astype(np.float32).copy()
    blended[mask] = blend * minmax(scores[mask]) + (1.0 - blend) * minmax(overlap[mask])
    # Keep every reranked candidate above the untouched tail, so the blend
    # cannot push a top-`depth` candidate below something never considered.
    if (~mask).any():
        blended[mask] += float(blended[~mask].max()) + 1.0
    return blended
