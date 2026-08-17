"""Score embedding models, space transforms, and CSLS on one labelled set.

    python3 compare_models.py
    python3 compare_models.py --models intfloat/multilingual-e5-base --transforms none abtt
    python3 compare_models.py --records data/processed/messages.json --eval-file data/eval_queries.json

Writes output/model_comparison.html and prints the same numbers as a table.

Three knobs are varied independently, because they fix different things:

* **model** — what the text is turned into.
* **space transform** — anisotropy. Raw transformer output sits in a narrow
  cone, so unrelated pairs already score ~0.8 and the ranking has little room
  left. `center` / `abtt` / `whiten` pull the space apart again.
* **CSLS** — hubness. A few messages sit near *everything* and get retrieved for
  unrelated queries; CSLS subtracts each record's own neighbourhood density.

Fusion uses Reciprocal Rank Fusion, which combines *ranks* rather than scores.
That matters here: models put cosine on different scales (e5 compresses
everything into ~0.80-1.00), so averaging raw scores would let one dominate.
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import plotly.graph_objects as go
from dotenv import load_dotenv

from embeddings import (
    apply_transform,
    cosine_scores,
    csls_scores,
    embed_texts,
    fit_transform,
    hubness_penalty,
    quiet_third_party_logs,
    set_model,
)
from evaluate import (
    DEFAULT_EVAL_FILE,
    first_hit_rank,
    load_eval_set,
    ndcg_at_k,
    recall_at_k,
    recall_ceiling,
    usable_cases,
)
from fusion import fuse_rankings
from semantic_search import DEFAULT_RECORDS, embed_records, load_records
from visualize import BLUE_SCALE, INK_MUTED, SERIES_1, SURFACE, base_layout, build_page, shorten, stat_tile

# Everything worth trying on Thai/English mixed chat, with the size that decides
# whether it is practical. Prefixes and remote-code flags per family live in
# embeddings.MODEL_SPECS, so any of these can be dropped in with --models.
MODEL_CATALOG: dict[str, str] = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": "118M · 384d · 128 ctx · the fast baseline",
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": "278M · 768d · 128 ctx · older, stronger baseline",
    "intfloat/multilingual-e5-base": "278M · 768d · 512 ctx · needs query:/passage: prefixes",
    "intfloat/multilingual-e5-large": "560M · 1024d · 512 ctx · straight upgrade on e5-base",
    "BAAI/bge-m3": "568M · 1024d · 8192 ctx · also yields sparse and ColBERT vectors",
    "Qwen/Qwen3-Embedding-0.6B": "595M · 1024d · 32k ctx · instruction-aware, top of MTEB multilingual",
    "google/embeddinggemma-300m": "308M · 768d · 2048 ctx · smallest of the modern multilingual set",
    # Both ship custom modelling code that transformers 5.x rejects: gte's shared
    # Alibaba-NLP/new-impl module indexes its RoPE table with uninitialised
    # position_ids, and jina-v3 has the same class of problem. Verified broken on
    # this machine, not assumed — keep them listed so the next transformers
    # release can be retried, but they are not in the default run.
    "Alibaba-NLP/gte-multilingual-base": "305M · 768d · 8192 ctx · BROKEN on transformers 5.x remote code",
    "jinaai/jina-embeddings-v3": "572M · 1024d · 8192 ctx · query/passage LoRA; same remote-code risk",
}
DEFAULT_MODELS = (
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
    "intfloat/multilingual-e5-base",
    "intfloat/multilingual-e5-large",
    "BAAI/bge-m3",
)
DEFAULT_TRANSFORMS = ("none", "center", "abtt", "abtt3", "whiten")
DEFAULT_KS = (1, 3, 5, 10)
RRF_K = 60  # standard damping constant; higher flattens the weight of top ranks
FUSION = "RRF fusion"

log = logging.getLogger("compare_models")


@dataclass(frozen=True)
class Variant:
    """One post-processing setting applied on top of a model's raw vectors."""

    transform: str  # none | center | abtt | whiten
    drop: int = 1  # principal components removed, abtt only
    csls: bool = False

    @property
    def label(self) -> str:
        name = self.transform if self.transform != "abtt" else f"abtt(drop {self.drop})"
        return f"{name} + CSLS" if self.csls else name


def build_variants(names: list[str], with_csls: bool) -> list[Variant]:
    """Expand transform names (abtt3 means all-but-the-top with 3 dropped)."""
    variants: list[Variant] = []
    for name in names:
        transform, drop = ("abtt", 3) if name == "abtt3" else (name, 1)
        variants.append(Variant(transform, drop))
        if with_csls:
            variants.append(Variant(transform, drop, csls=True))
    return variants


def short_label(model: str) -> str:
    return model.split("/")[-1]


def thread_separation(records: list[dict[str, Any]], matrix: np.ndarray) -> float:
    """Standardised gap between same-thread and cross-thread pair similarity.

    Same-thread messages share a topic by definition, so a model that encodes
    topic should score them higher. Standardising makes it comparable across
    spaces whose cosine scales differ.
    """
    threads = np.array([str(record.get("thread_ts", "")) for record in records])
    similarity = cosine_scores(matrix.T, matrix)
    upper = np.triu_indices(len(records), k=1)
    same_mask = threads[upper[0]] == threads[upper[1]]
    same, cross = similarity[upper][same_mask], similarity[upper][~same_mask]
    if not len(same) or not len(cross) or cross.std() == 0:
        return float("nan")
    return float((same.mean() - cross.mean()) / cross.std())


def score_spread(matrix: np.ndarray) -> float:
    """Standard deviation of every pairwise similarity — the anisotropy tell.

    A model whose pairs all land between 0.80 and 1.00 has almost no dynamic
    range: the ranking gets decided in the third decimal, where noise lives, and
    no threshold can answer "is this related at all". A wider spread means the
    space itself separates unrelated messages. This is what the transforms move.
    """
    similarity = cosine_scores(matrix.T, matrix)
    upper = np.triu_indices(len(matrix), k=1)
    return float(similarity[upper].std())


def score_rankings(
    per_case: list[list[str]], relevant_sets: list[set[str]], ks: tuple[int, ...]
) -> dict[str, Any]:
    """Mean recall and nDCG at each k, plus the worst first-hit rank.

    nDCG is the one to compare on: it discounts by position, so a pipeline that
    puts the answer at rank 1 beats one that puts it at rank 9, and it is
    normalised against a perfect ordering of the same labels so 1.00 is reachable
    at every k. Recall alone treats those two rankings as identical.
    """
    recall = {k: 0.0 for k in ks}
    gains = {k: 0.0 for k in ks}
    worst_rank = 0
    for ranking, relevant in zip(per_case, relevant_sets):
        for k in ks:
            recall[k] += recall_at_k(ranking, relevant, k)
            gains[k] += ndcg_at_k(ranking, relevant, k)
        rank = first_hit_rank(ranking, relevant)
        worst_rank = max(worst_rank, rank if rank else len(ranking))
    count = len(per_case)
    return {
        "recall": {k: value / count for k, value in recall.items()},
        "ndcg": {k: value / count for k, value in gains.items()},
        "worst": worst_rank,
    }


def rank_all(
    query_vectors: np.ndarray,
    matrix: np.ndarray,
    record_ids: list[str],
    hubness: np.ndarray | None,
    excluded: list[set[str]] | None = None,
) -> list[list[str]]:
    """Rank every record for every query, optionally with the CSLS correction.

    `excluded` drops ids per query — weak labels use the message itself as the
    query, and it would otherwise rank first against a copy of itself.
    """
    rankings = []
    for position, query_vector in enumerate(query_vectors):
        scores = cosine_scores(query_vector, matrix)
        if hubness is not None:
            scores = csls_scores(scores, hubness)
        ranked = [record_ids[index] for index in np.argsort(-scores)]
        drop = excluded[position] if excluded else set()
        rankings.append([record_id for record_id in ranked if record_id not in drop] if drop else ranked)
    return rankings


def grid_figure(
    rows: list[str], columns: list[str], values: list[list[float]], title: str, note: str, zmax: float | None = None
) -> go.Figure:
    """Variant × model grid. Magnitude across a grid is a heatmap."""
    figure = go.Figure(
        go.Heatmap(
            z=values,
            x=columns,
            y=rows,
            zmin=0,
            zmax=zmax,
            colorscale=BLUE_SCALE,
            xgap=2,
            ygap=2,
            texttemplate="%{z:.2f}",
            textfont={"size": 12},
            colorbar={"title": {"text": note, "font": {"color": INK_MUTED}}, "outlinewidth": 0, "tickfont": {"color": INK_MUTED}},
            hovertemplate="%{y}<br>%{x}: %{z:.3f}<extra></extra>",
        )
    )
    layout = base_layout(title, height=150 + 44 * len(rows))
    layout["margin"] |= {"l": 190}
    layout["yaxis"] |= {"autorange": "reversed", "gridcolor": SURFACE}
    figure.update_layout(**layout)
    return figure


def best_bar_figure(labels: list[str], values: list[float], title: str, axis: str) -> go.Figure:
    """One series, so one colour: bar length carries the magnitude."""
    figure = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker={"color": SERIES_1, "cornerradius": 4},
            text=[f"{value:.2f}" for value in values],
            textposition="outside",
            textfont={"color": INK_MUTED},
            hovertemplate="%{y}<br>%{x:.3f}<extra></extra>",
        )
    )
    layout = base_layout(title, height=130 + 44 * len(labels))
    layout["margin"] |= {"l": 320}
    layout["xaxis"] |= {"title": {"text": axis, "font": {"color": INK_MUTED}}}
    layout["yaxis"] |= {"autorange": "reversed", "gridcolor": SURFACE}
    layout["bargap"] = 0.35
    figure.update_layout(**layout)
    return figure


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS), help="Model ids to compare")
    parser.add_argument("--catalog", action="store_true", help="Print the known models with their sizes and exit")
    parser.add_argument(
        "--metric", default="ndcg", choices=("ndcg", "recall"), help="Metric the grid and ranking use (default ndcg)"
    )
    parser.add_argument(
        "--transforms", nargs="+", default=list(DEFAULT_TRANSFORMS),
        help="Space transforms: none center abtt abtt3 whiten (default all)",
    )
    parser.add_argument("--no-csls", action="store_true", help="Skip the CSLS variants")
    parser.add_argument("--no-fusion", action="store_true", help="Skip the Reciprocal Rank Fusion column")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--eval-file", type=Path, default=DEFAULT_EVAL_FILE, help=f"Labelled queries (default {DEFAULT_EVAL_FILE})")
    parser.add_argument("--ks", type=int, nargs="+", default=list(DEFAULT_KS), help="K values (default 1 3 5 10)")
    parser.add_argument("--out", type=Path, default=Path("output/model_comparison.html"), help="Output HTML")
    parser.add_argument("--include-threads", action="store_true", help="Also rank whole-thread records")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()

    if args.catalog:
        width = max(len(name) for name in MODEL_CATALOG)
        print(f"\n{'model':{width}}  notes")
        print("-" * (width + 58))
        for name, note in MODEL_CATALOG.items():
            print(f"{'*' if name in DEFAULT_MODELS else ' '}{name:{width - 1}}  {note}")
        print("\n* is in the default comparison. Any Sentence Transformers id works with --models.")
        print("Instruction prefixes and trust_remote_code are handled per family in embeddings.MODEL_SPECS.")
        return

    ks = tuple(sorted({k for k in args.ks if k > 0}))
    if not ks:
        raise SystemExit("--ks needs at least one positive integer.")
    variants = build_variants(args.transforms, not args.no_csls)

    records = load_records(args.records, include_threads=args.include_threads)
    record_ids = [str(record["id"]) for record in records]
    cases = load_eval_set(args.eval_file)
    queries, relevant_sets, excluded = usable_cases(cases, set(record_ids))
    if not queries:
        raise SystemExit("No labelled query matches the corpus. Check the ids in the label file.")
    log.info(
        "Comparing %d model(s) x %d variant(s) on %d query/queries over %d record(s)",
        len(args.models), len(variants), len(queries), len(records),
    )

    # metrics[(model, variant)] -> dict; rankings[(model, variant)] -> per-query rankings
    metrics: dict[tuple[str, str], dict[str, Any]] = {}
    rankings: dict[tuple[str, str], list[list[str]]] = {}

    for model in args.models:
        set_model(model)
        column = short_label(model)  # keys match the display columns
        log.info("Model %s — %s", model, MODEL_CATALOG.get(model, "not in the catalog"))
        base = embed_records(records)
        query_vectors = embed_texts(queries, role="query")
        for variant in variants:
            transform = fit_transform(base, variant.transform, drop=variant.drop)
            matrix = apply_transform(base, transform)
            moved_queries = apply_transform(query_vectors, transform)
            hubness = hubness_penalty(matrix) if variant.csls else None
            ranked = rank_all(moved_queries, matrix, record_ids, hubness, excluded)
            entry = score_rankings(ranked, relevant_sets, ks)
            entry["separation"] = thread_separation(records, matrix)
            entry["spread"] = score_spread(matrix)
            metrics[(column, variant.label)] = entry
            rankings[(column, variant.label)] = ranked
        log.info("  %s done, %d dim", column, base.shape[1])

    columns = [short_label(model) for model in args.models]
    if not args.no_fusion and len(args.models) > 1:
        for variant in variants:
            fused = [
                fuse_rankings([rankings[(short_label(model), variant.label)][index] for model in args.models])
                for index in range(len(queries))
            ]
            entry = score_rankings(fused, relevant_sets, ks)
            # Fusion combines ranks from several spaces, so neither number that
            # describes a single space is defined for it.
            entry["separation"] = float("nan")
            entry["spread"] = float("nan")
            metrics[(FUSION, variant.label)] = entry
        columns.append(FUSION)

    variant_labels = [variant.label for variant in variants]
    headline = ks[-1]
    metric = args.metric
    metric_label = f"{'Recall' if metric == 'recall' else 'nDCG'}@{headline}"
    grid = [[metrics[(column, label)][metric][headline] for column in columns] for label in variant_labels]
    separation_grid = [
        [metrics[(column, label)]["separation"] for column in columns if column != FUSION] for label in variant_labels
    ]
    spread_grid = [
        [metrics[(column, label)]["spread"] for column in columns if column != FUSION] for label in variant_labels
    ]

    counts = [len(relevant) for relevant in relevant_sets]
    ceilings = [recall_ceiling(counts, k) for k in ks]

    header = (
        f"{'model':40} {'variant':18} " + " ".join(f"R@{k:<5}" for k in ks) + " "
        + " ".join(f"nDCG@{k:<3}" for k in ks) + " separation  spread  worst"
    )
    print("\n" + header)
    print("-" * len(header))
    ordered = sorted(metrics.items(), key=lambda item: -item[1][metric][headline])
    for (column, label), entry in ordered:
        recalls = " ".join(f"{entry['recall'][k]:<7.2f}" for k in ks)
        gains = " ".join(f"{entry['ndcg'][k]:<8.2f}" for k in ks)
        gap = "       n/a" if np.isnan(entry["separation"]) else f"{entry['separation']:>10.2f}"
        spread = "     n/a" if np.isnan(entry["spread"]) else f"{entry['spread']:>7.3f}"
        print(f"{column:40} {label:18} {recalls} {gains} {gap} {spread}  {entry['worst']:>5}")
    print(f"\n{'best possible recall (set by label counts)':59} " + " ".join(f"{value:<7.2f}" for value in ceilings))
    print("nDCG       = position-discounted and normalised, so 1.00 is reachable at every k — compare on this")
    print("separation = (same-thread mean − cross-thread mean) / cross-thread sd, over all pairs")
    print("spread     = sd of all pairwise similarities; small means the space squashes everything together")
    print("worst      = deepest rank of the first correct match, across queries (1 is best)")

    (best_column, best_label), best_entry = ordered[0]
    baseline = metrics[(columns[0], variant_labels[0])][metric][headline]
    tiles = [
        stat_tile("Combinations tested", f"{len(metrics)}", f"{len(args.models)} models x {len(variants)} variants"),
        stat_tile(f"Best {metric_label}", f"{best_entry[metric][headline]:.2f}", f"{best_column} · {best_label}"),
        stat_tile("Baseline", f"{baseline:.2f}", f"{columns[0]} · {variant_labels[0]}"),
    ]
    finite_spread = [
        (entry["spread"], f"{column} · {label}")
        for (column, label), entry in metrics.items()
        if not np.isnan(entry["spread"])
    ]
    if finite_spread:
        widest, widest_name = max(finite_spread)
        tiles.append(stat_tile("Widest spread", f"{widest:.2f}", f"{widest_name} — most room to rank"))
    if metric == "recall" and ceilings[-1]:
        tiles.append(
            stat_tile("Share of achievable", f"{best_entry['recall'][headline] / ceilings[-1]:.0%}", f"ceiling is {ceilings[-1]:.2f}")
        )

    ceiling_note = (
        f" Best possible here is {ceilings[-1]:.2f}."
        if metric == "recall"
        else " nDCG is normalised against a perfect ordering of the same labels, so 1.00 is reachable."
    )
    sections = [
        (
            f"{metric_label} for every combination, on identical queries and corpus. Rows are "
            "post-processing applied to the same vectors: 'none' is raw cosine, the rest pull apart "
            "the narrow cone that transformer embeddings sit in, and '+ CSLS' additionally penalises "
            f"messages that sit near everything.{ceiling_note}",
            grid_figure(
                variant_labels, columns, grid,
                f"{metric_label} — model (columns) x post-processing (rows)", metric, zmax=1.0,
            ).to_html(full_html=False, include_plotlyjs=True, config={"displayModeBar": False}),
        ),
        (
            "The structural check, computed over every pair of messages rather than a few queries: "
            "messages in one Slack thread share a topic, so they should score further apart from "
            "unrelated pairs. CSLS is absent here because it rescores queries and does not change "
            "the space; fusion is absent because it has no single space.",
            grid_figure(
                variant_labels,
                [column for column in columns if column != FUSION],
                separation_grid,
                "Thread separation — higher means topic beats chat style", "separation",
            ).to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}),
        ),
        (
            "How much of the 0–1 range each space actually uses. A model that puts every pair between "
            "0.80 and 1.00 has to decide the ranking in the third decimal, where noise lives, and no "
            "threshold can answer “is this related at all”. This is the number the transforms are "
            "there to move, so read it next to the grid above: a transform that widens the spread "
            "without improving the metric has bought resolution but not accuracy.",
            grid_figure(
                variant_labels,
                [column for column in columns if column != FUSION],
                spread_grid,
                "Score spread — sd of every pairwise similarity", "sd",
            ).to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}),
        ),
        (
            f"Top combinations by {metric_label}.",
            best_bar_figure(
                [f"{column} · {label}" for (column, label), _ in ordered[:8]],
                [entry[metric][headline] for _, entry in ordered[:8]],
                f"Best combinations by {metric_label}", metric_label,
            ).to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}),
        ),
    ]

    subtitle = (
        f"{len(queries)} labelled queries · {len(records)} messages · "
        f"{len(args.models)} models × {len(variants)} post-processing variants · RRF k={RRF_K}"
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_page("Embedding model comparison", tiles, sections, subtitle), encoding="utf-8")
    log.info("Wrote %s", args.out)


if __name__ == "__main__":
    main()
