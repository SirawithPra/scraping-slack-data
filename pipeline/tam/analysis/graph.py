"""Message graph + community detection: relation as structure, not as top-k.

Cosine ranking answers "what is closest to this one". It cannot answer "what are
the distinct topics in this channel, and which messages belong to each" — that is
a question about the whole set at once, and a threshold on a pairwise score is a
bad way to ask it. Two messages can be a weak pair yet clearly the same work item
because ten other messages tie them together.

So: build one graph whose edges combine every signal available — dense
similarity, shared thread, temporal proximity, shared anchors — and let a
community detection algorithm (Louvain, maximising modularity) find the groups.
Membership then depends on the *shape of the neighbourhood*, not on one number
clearing a cutoff.

The graph is deliberately sparse: each message keeps only its strongest few
neighbours. A dense graph makes every node adjacent to every other and modularity
becomes meaningless.

Slack threads are ground truth for "same topic", so the clustering is scored
against them with the adjusted Rand index — a real number for how well an
unsupervised grouping recovers the conversations it was never told about.

    python3 -m tam.analysis.graph
    python3 -m tam.analysis.graph --resolution 1.4 --knn 8
    python3 -m tam.analysis.graph --clusters data/processed/clusters.json
"""

from __future__ import annotations

import argparse
import html
import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import networkx as nx
import numpy as np
import plotly.graph_objects as go
from dotenv import load_dotenv

from tam.retrieval.embeddings import apply_transform, fit_transform, model_name, quiet_third_party_logs, set_model
from tam.core import DEFAULT_RECORDS, embed_records, format_timestamp, load_records
from tam.retrieval.signals import SignalIndex
from tam.report.visualize import (
    BLUE_SCALE,
    GRID,
    INK_MUTED,
    INK_SECONDARY,
    SERIES_1,
    SURFACE,
    base_layout,
    build_page,
    shorten,
    stat_tile,
    wrap_hover,
)

DEFAULT_OUTPUT = Path("output/graph.html")
# A heatmap of every pair stops being readable long before a real channel's size.
HEATMAP_LIMIT = 120
LOUVAIN_SEED = 7  # fixed, so two runs on one corpus give the same clusters

log = logging.getLogger("graph")


@dataclass(frozen=True)
class EdgeWeights:
    """How much each signal contributes to an edge.

    `thread` is the largest on purpose: it is the only exact signal, so where it
    exists it should dominate. `time` is small because a busy channel puts
    unrelated messages minutes apart.

    `meeting_thread` exists because the thread signal does not mean the same
    thing in both sources. A Slack thread is *one* topic by construction — people
    open a new thread for a new subject. A meeting is deliberately *several*: one
    standup covers the Android bug, the Omega deal, and Q4 reviews in fifteen
    minutes, and they all share a `thread_ts`. Weighting a meeting's thread like
    a Slack thread collapses the entire meeting into a single cluster — measured
    on this corpus, not assumed. So meeting utterances lean on wording, anchors,
    and adjacency in time instead.
    """

    dense: float = 1.0
    thread: float = 1.5
    time: float = 0.3
    anchors: float = 0.8
    meeting_thread: float = 0.25


def build_graph(
    records: Sequence[dict[str, Any]],
    matrix: np.ndarray,
    signals: SignalIndex,
    *,
    weights: EdgeWeights = EdgeWeights(),
    knn: int = 6,
    mutual: bool = True,
    min_weight: float = 0.05,
) -> nx.Graph:
    """A sparse weighted graph over the messages.

    Candidate edges come from each message's `knn` nearest dense neighbours, plus
    every same-thread pair regardless of wording — a thread edge is a fact, and
    dropping it because two replies are worded differently would throw away the
    best evidence in the export.

    With `mutual`, a dense edge survives only if both messages chose each other.
    That is the cheap fix for hub messages ("any update?") that would otherwise
    attach themselves to every cluster at once.
    """
    count = len(records)
    graph = nx.Graph()
    for index, record in enumerate(records):
        graph.add_node(
            index,
            record_id=str(record["id"]),
            text=str(record["text"]),
            thread=str(record.get("thread_ts", "")),
            user=str(record.get("user", "")),
            ts=str(record.get("ts", "")),
        )
    if count < 2:
        return graph

    with np.errstate(all="ignore"):  # Accelerate BLAS flag noise; see embeddings.cosine_scores
        similarity = matrix @ matrix.T
    np.fill_diagonal(similarity, -np.inf)
    keep = max(1, min(knn, count - 1))
    neighbours = [set(np.argpartition(-row, keep - 1)[:keep].tolist()) for row in similarity]

    is_meeting = [str(record.get("source") or "") == "meeting" for record in records]

    candidates: set[tuple[int, int]] = set()
    for left in range(count):
        for right in neighbours[left]:
            if mutual and left not in neighbours[right]:
                continue
            candidates.add((min(left, right), max(left, right)))
    # Thread edges are added unconditionally, after the kNN filter — but only for
    # sources where a thread really is one topic. Forcing a complete subgraph over
    # a meeting would make every utterance adjacent to every other one.
    threads: dict[str, list[int]] = {}
    for index in range(count):
        thread = signals.threads[index]
        if thread and not is_meeting[index]:
            threads.setdefault(thread, []).append(index)
    for members in threads.values():
        for position, left in enumerate(members):
            for right in members[position + 1 :]:
                candidates.add((left, right))

    for left, right in candidates:
        pair = signals.pair_signals(left, right)
        # Same meeting is weak evidence of the same topic; same Slack thread is strong.
        thread_weight = weights.meeting_thread if (is_meeting[left] and is_meeting[right]) else weights.thread
        weight = (
            weights.dense * max(0.0, float(similarity[left, right]))
            + thread_weight * pair["thread"]
            + weights.time * pair["time"]
            + weights.anchors * pair["anchors"]
        )
        if weight >= min_weight:
            graph.add_edge(left, right, weight=float(weight), **pair)

    isolated = [node for node in graph.nodes if graph.degree(node) == 0]
    if isolated:
        # A node with no edge cannot join any community, so give it its single
        # best neighbour back. Reported rather than done silently.
        for node in isolated:
            best = int(np.argmax(similarity[node]))
            graph.add_edge(node, best, weight=max(min_weight, float(similarity[node, best])), rescued=True)
        log.info("Reconnected %d isolated message(s) via their single best neighbour", len(isolated))
    return graph


def detect_communities(graph: nx.Graph, *, resolution: float = 1.0) -> list[int]:
    """Louvain community index per node.

    Resolution is the one knob: above 1.0 splits into more, smaller topics; below
    1.0 merges them. There is no correct value — it depends on whether "the Omega
    deal" is one topic or three.
    """
    if not graph.number_of_edges():
        return list(range(graph.number_of_nodes()))
    communities = nx.community.louvain_communities(
        graph, weight="weight", resolution=resolution, seed=LOUVAIN_SEED
    )
    # Largest community first, so cluster 0 is the channel's main subject.
    ordered = sorted(communities, key=len, reverse=True)
    labels = [0] * graph.number_of_nodes()
    for community_index, members in enumerate(ordered):
        for node in members:
            labels[node] = community_index
    return labels


def cluster_label(members: Sequence[int], signals: SignalIndex, limit: int = 3) -> str:
    """Name a cluster by the anchors its members share most distinctively."""
    counts: Counter[str] = Counter()
    for index in members:
        counts.update(signals.anchor_sets[index])
    if not counts:
        return "(no shared anchor)"
    # count x idf: frequent inside the cluster and rare outside it.
    scored = sorted(
        ((frequency * signals.anchor_idf.get(anchor, 0.0), anchor) for anchor, frequency in counts.items()),
        reverse=True,
    )
    return ", ".join(anchor for _, anchor in scored[:limit])


def thread_agreement(labels: Sequence[int], threads: Sequence[str]) -> dict[str, float]:
    """Score the clustering against Slack threads, which are ground truth.

    Adjusted Rand index and normalised mutual information are the standard pair:
    ARI is chance-corrected (0.0 means no better than random), NMI is not but is
    more forgiving of a clustering that splits a thread into coherent parts.
    """
    import warnings

    from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score

    truth = [thread or f"__solo_{index}" for index, thread in enumerate(threads)]
    with warnings.catch_warnings():
        # Most channel messages start their own thread, so there are legitimately
        # more distinct "labels" than half the sample. sklearn warns that this
        # looks like a regression target; here it is simply what threads are.
        warnings.filterwarnings("ignore", message="The number of unique classes")
        return {
            "ari": float(adjusted_rand_score(truth, list(labels))),
            "nmi": float(normalized_mutual_info_score(truth, list(labels))),
        }


def cluster_purity(members: Sequence[int], threads: Sequence[str]) -> float:
    """Share of a cluster that comes from its most common thread."""
    counts = Counter(threads[index] for index in members)
    return max(counts.values()) / len(members) if members else 0.0


def block_heatmap(
    order: Sequence[int],
    labels: Sequence[int],
    similarity: np.ndarray,
    records: Sequence[dict[str, Any]],
) -> go.Figure:
    """Pairwise similarity with messages sorted by cluster.

    A matrix is magnitude across a grid, so a heatmap, one hue. Sorting by cluster
    is what makes it readable: real structure shows up as bright squares on the
    diagonal, and a bright off-diagonal patch is two clusters that should have
    been one.
    """
    picked = list(order)
    values = similarity[np.ix_(picked, picked)]
    ticks = [f"{labels[index]}" for index in picked]
    texts = [wrap_hover(shorten(str(records[index]["text"]), 160)) for index in picked]

    figure = go.Figure(
        go.Heatmap(
            z=values,
            zmin=float(np.percentile(values, 2)),
            zmax=1.0,
            colorscale=BLUE_SCALE,
            x=list(range(len(picked))),
            y=list(range(len(picked))),
            customdata=[[f"cluster {labels[left]}", texts[position]] for position, left in enumerate(picked)],
            colorbar={"title": {"text": "cosine", "font": {"color": INK_MUTED}}, "outlinewidth": 0, "tickfont": {"color": INK_MUTED}},
            hovertemplate="row %{y} · col %{x}<br>cosine %{z:.2f}<extra></extra>",
        )
    )
    # Cluster boundaries: the blocks are the point, so they get an explicit outline.
    start = 0
    for position in range(1, len(picked) + 1):
        if position == len(picked) or labels[picked[position]] != labels[picked[start]]:
            figure.add_shape(
                type="rect",
                x0=start - 0.5,
                x1=position - 0.5,
                y0=start - 0.5,
                y1=position - 0.5,
                line={"color": INK_SECONDARY, "width": 2},
                fillcolor="rgba(0,0,0,0)",
            )
            start = position

    layout = base_layout(f"Similarity matrix, messages ordered by cluster ({len(picked)} shown)", height=620)
    layout["xaxis"] |= {"tickmode": "array", "tickvals": list(range(len(picked))), "ticktext": ticks, "showgrid": False, "title": {"text": "cluster", "font": {"color": INK_MUTED}}}
    layout["yaxis"] |= {"tickmode": "array", "tickvals": list(range(len(picked))), "ticktext": ticks, "showgrid": False, "autorange": "reversed", "title": {"text": "cluster", "font": {"color": INK_MUTED}}}
    layout["margin"] |= {"l": 70, "b": 70}
    figure.update_layout(**layout)
    return figure


def cluster_size_figure(sizes: Sequence[int], names: Sequence[str], purities: Sequence[float]) -> go.Figure:
    """One series — bar length carries the size, the label carries the identity."""
    labels = [f"{index}. {shorten(name, 46)}" for index, name in enumerate(names)]
    figure = go.Figure(
        go.Bar(
            x=list(sizes),
            y=labels,
            orientation="h",
            marker={"color": SERIES_1, "cornerradius": 4},
            text=[f"{size}" for size in sizes],
            textposition="outside",
            textfont={"color": INK_SECONDARY},
            customdata=[[f"{purity:.0%}"] for purity in purities],
            hovertemplate="%{y}<br>%{x} messages<br>%{customdata[0]} from one thread<extra></extra>",
        )
    )
    layout = base_layout("Clusters found, named by their shared anchors", height=110 + 44 * len(sizes))
    layout["xaxis"] |= {"title": {"text": "messages in cluster", "font": {"color": INK_MUTED}}}
    layout["yaxis"] |= {"autorange": "reversed", "gridcolor": SURFACE}
    layout["margin"] |= {"l": 400}
    layout["bargap"] = 0.4
    figure.update_layout(**layout)
    return figure


def clusters_table(
    labels: Sequence[int], records: Sequence[dict[str, Any]], names: Sequence[str], purities: Sequence[float]
) -> str:
    """Every cluster with its members, so nothing here depends on the heatmap."""
    blocks = []
    for community_index, name in enumerate(names):
        members = [index for index, label in enumerate(labels) if label == community_index]
        rows = "".join(
            "<tr>"
            f"<td>{html.escape(' '.join(str(records[index]['text']).split())[:170])}</td>"
            f"<td>{html.escape(str(records[index].get('user') or '-'))}</td>"
            f"<td class='num'>{html.escape(format_timestamp(str(records[index].get('ts', ''))))}</td>"
            "</tr>"
            for index in members
        )
        blocks.append(
            f"<details class='table-view'><summary>Cluster {community_index} — {html.escape(name)} "
            f"({len(members)} messages, {purities[community_index]:.0%} from one thread)</summary>"
            "<table><thead><tr><th>message</th><th>user</th><th>time</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></details>"
        )
    return "".join(blocks)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help=f"Output HTML (default {DEFAULT_OUTPUT})")
    parser.add_argument("--clusters", type=Path, help="Also write the cluster assignment as JSON")
    parser.add_argument("--resolution", type=float, default=1.0, help="Louvain resolution; >1 gives more clusters (default 1.0)")
    parser.add_argument("--knn", type=int, default=6, help="Dense neighbours per message (default 6)")
    parser.add_argument("--no-mutual", action="store_true", help="Keep one-sided dense edges too (denser graph)")
    parser.add_argument("--transform", default="none", choices=("none", "center", "abtt", "whiten"), help="Space transform before building edges")
    parser.add_argument("--model", help="Embedding model id; overrides EMBEDDING_MODEL for this run")
    parser.add_argument("--include-threads", action="store_true", help="Also cluster whole-thread records")
    parser.add_argument("--weights", type=float, nargs=4, metavar=("DENSE", "THREAD", "TIME", "ANCHORS"), help="Edge weights (default 1.0 1.5 0.3 0.8)")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()
    set_model(args.model)

    records = load_records(args.records, include_threads=args.include_threads)
    raw = embed_records(records)
    matrix = apply_transform(raw, fit_transform(raw, args.transform))
    signals = SignalIndex(records)
    weights = EdgeWeights(*args.weights) if args.weights else EdgeWeights()

    graph = build_graph(records, matrix, signals, weights=weights, knn=args.knn, mutual=not args.no_mutual)
    labels = detect_communities(graph, resolution=args.resolution)
    count = len(set(labels))
    modularity = (
        nx.community.modularity(graph, [{n for n, l in enumerate(labels) if l == c} for c in range(count)], weight="weight")
        if graph.number_of_edges()
        else 0.0
    )
    agreement = thread_agreement(labels, list(signals.threads))

    members_by_cluster = [[index for index, label in enumerate(labels) if label == community] for community in range(count)]
    names = [cluster_label(members, signals) for members in members_by_cluster]
    purities = [cluster_purity(members, list(signals.threads)) for members in members_by_cluster]
    sizes = [len(members) for members in members_by_cluster]

    log.info(
        "%d node(s), %d edge(s), %d cluster(s) · modularity %.3f · vs threads ARI %.3f NMI %.3f",
        graph.number_of_nodes(),
        graph.number_of_edges(),
        count,
        modularity,
        agreement["ari"],
        agreement["nmi"],
    )
    print(f"\n{'#':>3}  {'size':>4}  {'1-thread':>8}  label")
    print("-" * 76)
    for community_index, (size, purity, name) in enumerate(zip(sizes, purities, names)):
        print(f"{community_index:>3}  {size:>4}  {purity:>7.0%}  {shorten(name, 52)}")
    print(f"\nmodularity {modularity:.3f} (how block-like the graph is; >0.3 means real structure)")
    print(f"ARI {agreement['ari']:.3f}  NMI {agreement['nmi']:.3f} against Slack threads (1.0 = recovered them exactly)")

    if args.clusters:
        args.clusters.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "id": str(record["id"]),
                "cluster": int(label),
                "cluster_label": names[label],
                "thread_ts": record.get("thread_ts", ""),
            }
            for record, label in zip(records, labels)
        ]
        args.clusters.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Wrote %s", args.clusters)

    with np.errstate(all="ignore"):
        similarity = np.clip(matrix @ matrix.T, -1.0, 1.0)
    order = [index for community in range(count) for index in members_by_cluster[community]]
    if len(order) > HEATMAP_LIMIT:
        log.info("Heatmap shows the first %d of %d messages by cluster order", HEATMAP_LIMIT, len(order))
        order = order[:HEATMAP_LIMIT]

    tiles = [
        stat_tile("Messages", str(graph.number_of_nodes()), f"{graph.number_of_edges()} edges kept"),
        stat_tile("Clusters", str(count), f"Louvain, resolution {args.resolution:g}"),
        stat_tile("Modularity", f"{modularity:.2f}", "above 0.30 means real block structure"),
        stat_tile("ARI vs threads", f"{agreement['ari']:.2f}", "1.00 would recover threads exactly"),
        stat_tile("NMI vs threads", f"{agreement['nmi']:.2f}", "shared information with the threads"),
    ]

    sections = [
        (
            "Clusters come from the graph, not from a similarity cutoff: each message keeps only its "
            f"strongest {args.knn} neighbours, thread and anchor edges are added on top, and Louvain "
            "maximises modularity over the result. The name of each cluster is the anchors its members "
            "share most distinctively — no model wrote it.",
            cluster_size_figure(sizes, names, purities).to_html(full_html=False, include_plotlyjs=True, config={"displayModeBar": False})
            + clusters_table(labels, records, names, purities),
        ),
        (
            "The same data as a matrix, with messages sorted by cluster. Bright squares on the diagonal "
            "are clusters that hold together; a bright patch off the diagonal is two clusters that arguably "
            "belong together; a dim diagonal block was grouped by thread or anchors rather than by wording, "
            "which is exactly the case cosine alone would have missed.",
            block_heatmap(order, labels, similarity, records).to_html(full_html=False, include_plotlyjs=False, config={"displayModeBar": False}),
        ),
    ]

    subtitle = (
        f"{graph.number_of_nodes()} messages · model {model_name()} · edge weights "
        f"dense {weights.dense:g} / thread {weights.thread:g} / time {weights.time:g} / anchors {weights.anchors:g}"
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_page("Message graph — topics as communities", tiles, sections, subtitle), encoding="utf-8")
    log.info("Wrote %s. Open it with: open %s", args.out, args.out)


if __name__ == "__main__":
    main()
