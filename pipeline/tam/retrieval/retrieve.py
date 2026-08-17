"""The hybrid retrieval pipeline: every relation signal in one ranking.

One place that composes the pieces, so any combination can be measured against
any other instead of being argued about:

    text query ─┬─ dense cosine        (embeddings.py)   meaning, cross-language
                ├─ BM25                (lexical.py)      exact ids, names, codes
                └─ anchor overlap      (signals.py)      shared concrete strings
                        │
                        ├─ space transform + CSLS         de-skew the vector space
                        ├─ fusion  (RRF or z-score)       fusion.py
                        ├─ neighbourhood rerank            fusion.jaccard_rerank
                        └─ cross-encoder rerank            rerank.py
    message anchor ── everything above, plus thread / time / author  (signals.py)

`PRESETS` names the combinations worth comparing; `--preset` selects one
everywhere (search, evaluation, model comparison, reports) so the numbers in one
place mean the same thing in another.

    python3 -m tam.retrieval.retrieve -q "bug ใน Profile module แก้แล้วยัง"
    python3 -m tam.retrieval.retrieve -q "..." --preset hybrid --explain
    python3 -m tam.retrieval.retrieve --related msg_C0DEMOCHAN1_1786630937.113809
    python3 -m tam.retrieval.retrieve --list-presets
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from dotenv import load_dotenv

from tam.retrieval.embeddings import (
    SpaceTransform,
    apply_transform,
    cosine_scores,
    embed_texts,
    fit_transform,
    hubness_penalty,
    model_name,
    quiet_third_party_logs,
    set_model,
)
from tam.retrieval.fusion import jaccard_rerank, minmax, rrf_fuse, zscore_fuse
from tam.retrieval.lexical import Bm25Index
from tam.retrieval.signals import SignalIndex
from tam.core import DEFAULT_RECORDS, embed_records, format_timestamp, load_records, preview

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineConfig:
    """Which stages run, and how much each one counts.

    Every weight is "0 disables this stage", so a preset reads as a list of what
    is switched on. Weights apply to whichever fusion method is selected: under
    RRF they scale a rank contribution, under z-score a standardised score.
    """

    dense: float = 1.0
    lexical: float = 0.0
    anchors: float = 0.0
    # Structural signals only exist when the query is a message in the corpus,
    # so they are ignored by text search and used by `related`.
    thread: float = 0.0
    time: float = 0.0
    user: float = 0.0

    transform: str = "none"  # none | center | abtt | whiten
    abtt_drop: int = 1
    csls: int = 0  # neighbours for the hubness penalty; 0 disables
    csls_weight: float = 1.0
    jaccard: float = 0.0  # weight kept on the original score; 0 disables the rerank
    jaccard_depth: int = 20
    jaccard_neighbours: int = 10
    rerank_depth: int = 0  # cross-encoder candidates; 0 disables
    fusion: str = "rrf"  # rrf | zscore

    def describe(self) -> str:
        parts = [f"{name}={value:g}" for name, value in (
            ("dense", self.dense), ("bm25", self.lexical), ("anchors", self.anchors),
            ("thread", self.thread), ("time", self.time), ("user", self.user),
        ) if value]
        if self.transform != "none":
            parts.append(self.transform if self.transform != "abtt" else f"abtt-{self.abtt_drop}")
        if self.csls:
            parts.append(f"csls-{self.csls}")
        if self.jaccard:
            parts.append(f"jaccard-{self.jaccard:g}")
        if self.rerank_depth:
            parts.append(f"rerank-{self.rerank_depth}")
        return f"{self.fusion}[{', '.join(parts)}]"


# Ordered cheapest first, so a comparison table reads as "what does each addition buy".
PRESETS: dict[str, PipelineConfig] = {
    "dense": PipelineConfig(),
    "lexical": PipelineConfig(dense=0.0, lexical=1.0),
    "dense-abtt": PipelineConfig(transform="abtt"),
    "dense-whiten": PipelineConfig(transform="whiten"),
    "dense-csls": PipelineConfig(csls=10),
    "hybrid": PipelineConfig(lexical=1.0),
    "hybrid-anchors": PipelineConfig(lexical=1.0, anchors=0.5),
    "hybrid-jaccard": PipelineConfig(lexical=1.0, jaccard=0.7),
    "hybrid-rerank": PipelineConfig(lexical=1.0, rerank_depth=50),
    "full": PipelineConfig(lexical=1.0, anchors=0.5, transform="abtt", csls=10, jaccard=0.7, rerank_depth=50),
    # For "messages related to this message", where Slack's own structure is available.
    "related": PipelineConfig(lexical=1.0, anchors=0.5, thread=1.5, time=0.7, user=0.2, rerank_depth=50),
}
DEFAULT_PRESET = "hybrid-rerank"
RELATED_PRESET = "related"


@dataclass
class Hit:
    """One ranked record, with the per-stage numbers that put it there."""

    record: dict[str, Any]
    score: float
    rank: int
    parts: dict[str, float] = field(default_factory=dict)
    terms: list[str] = field(default_factory=list)
    shared_anchors: list[str] = field(default_factory=list)

    @property
    def record_id(self) -> str:
        return str(self.record["id"])


class Retriever:
    """A corpus with every index built once and reused across queries.

    The dense matrix, the BM25 postings, the space transform and the hubness
    penalty are all corpus properties, so they are computed here rather than per
    query. Only the cross-encoder is unavoidably per query.
    """

    def __init__(
        self,
        records: Sequence[dict[str, Any]],
        config: PipelineConfig | None = None,
        *,
        use_cache: bool = True,
        matrix: np.ndarray | None = None,
    ) -> None:
        self.records = list(records)
        self.config = config or PRESETS[DEFAULT_PRESET]
        self.ids = [str(record["id"]) for record in self.records]
        self.texts = [str(record["text"]) for record in self.records]
        self.index_of = {record_id: index for index, record_id in enumerate(self.ids)}

        raw = embed_records(self.records, use_cache=use_cache) if matrix is None else matrix
        self.transform: SpaceTransform = fit_transform(
            raw, self.config.transform, drop=self.config.abtt_drop
        )
        self.matrix = apply_transform(raw, self.transform)
        self.raw_matrix = raw

        self.bm25 = Bm25Index(self.texts) if self._needs_lexical() else None
        self.signals = SignalIndex(self.records) if self._needs_signals() else None
        self.hubness = (
            hubness_penalty(self.matrix, self.config.csls) if self.config.csls else np.zeros(len(self.records), dtype=np.float32)
        )

    def _needs_lexical(self) -> bool:
        return self.config.lexical > 0

    def _needs_signals(self) -> bool:
        return any((self.config.anchors, self.config.thread, self.config.time, self.config.user))

    def with_config(self, config: PipelineConfig) -> Retriever:
        """A retriever over the same corpus with different stages switched on.

        Reuses the already-embedded matrix, so comparing ten presets costs one
        embedding pass rather than ten.
        """
        return Retriever(self.records, config, matrix=self.raw_matrix)

    # ---- individual stages -------------------------------------------------

    def dense_scores(self, query: str) -> np.ndarray:
        """Cosine against the (optionally transformed) corpus, CSLS-corrected."""
        vector = apply_transform(embed_texts([query], role="query")[0], self.transform)
        scores = cosine_scores(vector, self.matrix)
        if self.config.csls:
            scores = scores - np.float32(self.config.csls_weight) * self.hubness
        return scores

    def lexical_scores(self, query: str) -> np.ndarray:
        if self.bm25 is None:
            return np.zeros(len(self.records), dtype=np.float32)
        return self.bm25.scores(query)

    def anchor_scores(self, query: str) -> np.ndarray:
        if self.signals is None:
            return np.zeros(len(self.records), dtype=np.float32)
        return self.signals.anchor_scores(query)

    # ---- composition -------------------------------------------------------

    def _stage_scores(self, query: str, anchor: int | None) -> dict[str, np.ndarray]:
        """Every enabled stage's raw scores, keyed by stage name."""
        config = self.config
        stages: dict[str, np.ndarray] = {}
        if config.dense:
            stages["dense"] = self.dense_scores(query)
        if config.lexical:
            stages["bm25"] = self.lexical_scores(query)
        if config.anchors:
            stages["anchors"] = self.anchor_scores(query)
        if anchor is not None and self.signals is not None:
            if config.thread:
                stages["thread"] = self.signals.thread_scores(anchor)
            if config.time:
                stages["time"] = self.signals.time_scores(anchor)
            if config.user:
                stages["user"] = self.signals.user_scores(anchor)
        if not stages:
            raise ValueError(f"Preset {config.describe()} has every stage disabled.")
        return stages

    def _weights(self, names: Sequence[str]) -> list[float]:
        config = self.config
        table = {
            "dense": config.dense,
            "bm25": config.lexical,
            "anchors": config.anchors,
            "thread": config.thread,
            "time": config.time,
            "user": config.user,
        }
        return [table[name] for name in names]

    def rank(self, query: str, *, anchor: int | None = None, top_k: int | None = None) -> list[Hit]:
        """Full pipeline for one query. `anchor` enables the structural signals."""
        config = self.config
        stages = self._stage_scores(query, anchor)
        names = list(stages)

        if len(names) == 1:
            fused = stages[names[0]].astype(np.float32)
        elif config.fusion == "rrf":
            fused = rrf_fuse([stages[name] for name in names], weights=self._weights(names))
        elif config.fusion == "zscore":
            fused = zscore_fuse([stages[name] for name in names], weights=self._weights(names))
        else:
            raise ValueError(f"Unknown fusion {config.fusion!r}; use rrf or zscore.")

        if config.jaccard:
            fused = jaccard_rerank(
                fused,
                self.matrix,
                depth=config.jaccard_depth,
                neighbours=config.jaccard_neighbours,
                blend=config.jaccard,
            )

        cross: dict[int, float] = {}
        if config.rerank_depth:
            from tam.retrieval.rerank import rerank_top  # imported late: it downloads a model

            fused, candidates, raw = rerank_top(query, self.texts, fused, depth=config.rerank_depth)
            cross = {int(index): float(score) for index, score in zip(candidates, raw)}

        if anchor is not None:
            fused = fused.copy()
            fused[anchor] = -np.inf  # a message is not its own relation

        limit = len(self.records) if top_k is None else max(1, min(top_k, len(self.records)))
        # Tie-break on the record id rather than the array index: fused ties are
        # common now that tied stage scores share a rank (see fusion.to_ranks),
        # and index order is only the order the records file was written in.
        order = np.lexsort((np.array(self.ids), -fused))
        if anchor is not None:
            # Masking the score is not enough — with no top_k the anchor would
            # still be listed, last, at -inf. Drop it before slicing so top_k
            # keeps meaning "this many other messages".
            order = order[order != anchor]
        order = order[:limit]
        normalised = {name: minmax(scores) for name, scores in stages.items()}

        hits: list[Hit] = []
        for position, index in enumerate(order, start=1):
            index = int(index)
            parts = {name: float(normalised[name][index]) for name in names}
            if index in cross:
                # The cross-encoder's own score, not a normalised one: it is the
                # stage that actually decided this ordering.
                parts["cross"] = cross[index]
            hits.append(
                Hit(
                    record=self.records[index],
                    score=float(fused[index]),
                    rank=position,
                    parts=parts,
                    terms=self.bm25.matched_terms(query, index) if self.bm25 is not None else [],
                    shared_anchors=(
                        self.signals.shared_anchors(anchor, index) if self.signals is not None and anchor is not None else []
                    ),
                )
            )
        return hits

    def rank_ids(self, query: str, *, anchor: int | None = None) -> list[str]:
        """Every record id, best first. What the evaluation metrics consume."""
        return [hit.record_id for hit in self.rank(query, anchor=anchor)]

    def related(self, record: str | int, *, top_k: int | None = None) -> list[Hit]:
        """Messages related to a message already in the corpus.

        The message's own text becomes the query, and Slack's structure joins in:
        thread membership, temporal proximity, and author. This is the mode the
        structural signals exist for — a typed query has no timestamp.
        """
        anchor = record if isinstance(record, int) else self.index_of.get(str(record))
        if anchor is None:
            raise SystemExit(f"No record with id {record!r}. Check data/processed/messages.json.")
        return self.rank(self.texts[anchor], anchor=anchor, top_k=top_k)


def build_retriever(
    records: Sequence[dict[str, Any]], preset: str, *, use_cache: bool = True, matrix: np.ndarray | None = None
) -> Retriever:
    """Retriever for a named preset, with a clear error on a typo."""
    if preset not in PRESETS:
        raise SystemExit(f"Unknown preset {preset!r}. Available: {', '.join(PRESETS)}")
    return Retriever(records, PRESETS[preset], use_cache=use_cache, matrix=matrix)


def print_hits(hits: Sequence[Hit], *, explain: bool = False) -> None:
    print("\nTop Matches:")
    if not hits:
        print("  (nothing found)")
        return
    for hit in hits:
        kind = " [thread context]" if hit.record.get("source") == "slack_thread" else ""
        print(f"\n{hit.rank}. {hit.score:.3f}{kind}\n   {preview(str(hit.record['text']))}")
        details = [
            f"user={hit.record.get('user') or '-'}",
            f"time={format_timestamp(str(hit.record.get('ts', '')))}",
            f"thread={hit.record.get('thread_ts') or '-'}",
            f"id={hit.record.get('id', '-')}",
        ]
        print(f"   {'  '.join(details)}")
        if explain:
            parts = "  ".join(f"{name}={value:.2f}" for name, value in hit.parts.items())
            print(f"   why: {parts}")
            if hit.terms:
                print(f"   matched terms: {', '.join(hit.terms)}")
            if hit.shared_anchors:
                print(f"   shared anchors: {', '.join(hit.shared_anchors)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-q", "--query", action="append", help="Run one query and exit; repeatable")
    parser.add_argument("--related", help="Record id to find related messages for")
    parser.add_argument("--preset", default=DEFAULT_PRESET, help=f"Pipeline preset (default {DEFAULT_PRESET})")
    parser.add_argument("--top-k", type=int, default=10, help="Matches to show (default 10)")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--include-threads", action="store_true", help="Also rank whole-thread records")
    parser.add_argument("--model", help="Embedding model id; overrides EMBEDDING_MODEL for this run")
    parser.add_argument("--reranker", help="Cross-encoder id; overrides RERANKER_MODEL for this run")
    parser.add_argument("--explain", action="store_true", help="Show each stage's contribution per match")
    parser.add_argument("--no-cache", action="store_true", help="Recompute embeddings instead of using the cache")
    parser.add_argument("--list-presets", action="store_true", help="Print the presets and exit")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()

    if args.list_presets:
        width = max(len(name) for name in PRESETS)
        print(f"\n{'preset':{width}}  stages")
        print("-" * (width + 60))
        for name, config in PRESETS.items():
            print(f"{name:{width}}  {config.describe()}")
        print(f"\ndefault text preset: {DEFAULT_PRESET}   default related preset: {RELATED_PRESET}")
        return

    if args.reranker:
        from tam.retrieval.rerank import set_reranker

        set_reranker(args.reranker)
    set_model(args.model)

    records = load_records(args.records, include_threads=args.include_threads)
    preset = args.preset if not args.related or args.preset != DEFAULT_PRESET else RELATED_PRESET
    retriever = build_retriever(records, preset, use_cache=not args.no_cache)
    log.info("%d record(s) · model %s · preset %s = %s", len(records), model_name(), preset, retriever.config.describe())

    if args.related:
        anchor = retriever.index_of.get(args.related)
        if anchor is not None:
            print(f"\nRelated to:\n> {preview(retriever.texts[anchor])}")
        print_hits(retriever.related(args.related, top_k=args.top_k), explain=args.explain)
        return

    queries = args.query or []
    if not queries:
        print(f"\nReady: {len(records)} record(s), model {model_name()}, preset {preset}.")
        print("Type a Thai / English / mixed message. Ctrl-D or 'exit' quits.")
        while True:
            try:
                query = input("\nSearch:\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not query:
                continue
            if query.lower() in {"exit", "quit", ":q"}:
                return
            print_hits(retriever.rank(query, top_k=args.top_k), explain=args.explain)
        return

    for query in queries:
        print(f"\nSearch:\n> {query}")
        print_hits(retriever.rank(query, top_k=args.top_k), explain=args.explain)


if __name__ == "__main__":
    main()
