"""Typed, directional relations between messages.

Cosine is symmetric: sim(A, B) == sim(B, A). So it can say two messages are
related and can never say *how*. But the relations a team actually needs are
directional and typed:

    "BE sorting API พร้อมแล้ว"        --resolves-->  "FE รอ API sorting อยู่"
    "deploy staging ค้าง"             --blocked_by-> "DB migration ยังไม่รัน"
    "แอปล่มตอนเข้าโปรไฟล์"            --duplicates-> "profile page crash on Android"

Two ways to get them, both here:

* ``rules``  — cue phrases plus message order. No download, works offline, and
  its mistakes are inspectable and fixable, because every decision names the cue
  that produced it. Thai and English cues are listed side by side.
* ``nli``    — a multilingual natural-language-inference cross-encoder scores each
  relation as a hypothesis about the pair. Catches paraphrases the cue lists miss,
  at the cost of a model download and no explanation beyond a number.

Direction comes from the timestamps: in a channel the later message is the one
that answers, resolves, or follows up on the earlier one. That is a chat-specific
prior no general similarity measure has access to.

Candidate pairs come from graph.py's edges, so the same tuned combination of
signals decides what is worth typing at all.

    python3 -m tam.analysis.relations
    python3 -m tam.analysis.relations --method nli --min-score 0.6
    python3 -m tam.analysis.relations --relations data/processed/relations.json
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import plotly.graph_objects as go
from dotenv import load_dotenv

from tam.retrieval.embeddings import apply_transform, fit_transform, model_name, quiet_third_party_logs, set_model
from tam.analysis.graph import EdgeWeights, build_graph
from tam.core import DEFAULT_RECORDS, embed_records, format_timestamp, load_records
from tam.retrieval.signals import SignalIndex
from tam.report.visualize import INK_MUTED, INK_SECONDARY, SERIES_1, SURFACE, base_layout, build_page, shorten, stat_tile

DEFAULT_OUTPUT = Path("output/relations.html")
# XNLI covers Thai, and this checkpoint is the standard multilingual zero-shot one.
DEFAULT_NLI_MODEL = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"

log = logging.getLogger("relations")


@dataclass(frozen=True)
class RelationType:
    """One relation, with both ways of detecting it.

    `hypothesis` is fed to the NLI model as a complete sentence about the pair —
    "source" is the earlier message, "target" the later one. `cues` are the
    phrases that mark it in the later message, in Thai and English.

    `cue_window` limits where a cue may appear. Some cues are only meaningful at
    the start of a message: "No, that endpoint is fine" is an answer, while a "no"
    buried in paragraph three is not.
    """

    name: str
    hypothesis: str
    cues: tuple[str, ...] = ()
    description: str = ""
    cue_window: int | None = None
    confidence: float = 0.7


# Ordered by how specific they are: the first matching type wins in rules mode, so
# "blocked_by" must be tested before the vaguer "follows_up".
RELATION_TYPES: tuple[RelationType, ...] = (
    RelationType(
        "resolves",
        "The later message reports that the problem or task in the earlier message is finished, fixed, or deployed.",
        (
            "fixed", "fix is", "resolved", "done", "deployed", "merged", "shipped", "released",
            "patched", "working now", "solved", "closed", "พร้อมแล้ว", "เสร็จแล้ว", "แก้แล้ว",
            "แก้เสร็จ", "fix แล้ว", "ขึ้นแล้ว", "deploy แล้ว", "merge แล้ว", "ปิดเคสแล้ว", "เรียบร้อย",
        ),
        "the work asked about earlier is now finished",
        confidence=0.9,
    ),
    RelationType(
        "blocked_by",
        "The later message says its work cannot continue until the thing in the earlier message is ready.",
        (
            "blocked", "waiting on", "waiting for", "depends on", "can't until", "cannot until",
            "still need", "รออยู่", "ยังรอ", "รอ api", "ติดที่", "ยังไม่มา", "ต้องรอ", "ยังทำไม่ได้",
        ),
        "one message cannot proceed until the other is done",
        confidence=0.9,
    ),
    RelationType(
        "duplicates",
        "The two messages report the same problem or ask the same question.",
        (
            "same issue", "same problem", "same as", "duplicate", "already reported", "also seeing",
            "me too", "same here", "เหมือนกัน", "เจอเหมือนกัน", "ปัญหาเดียวกัน", "ซ้ำกับ", "อันเดียวกัน",
        ),
        "the same thing reported twice",
        confidence=0.9,
    ),
    RelationType(
        "answers",
        "The later message answers the question asked in the earlier message.",
        (
            "yes", "no", "nope", "yep", "it's", "it is", "because", "the reason", "you need to",
            "you have to", "try", "here's", "here is", "correct", "right",
            "ใช่", "ไม่ใช่", "เพราะ", "ต้อง", "ลอง", "ได้เลย", "ตอบว่า", "อยู่ที่",
        ),
        "a direct answer to an earlier question",
        # An answer opens with its answer word. Further in, "no" and "right" are
        # ordinary vocabulary and say nothing about the pair.
        cue_window=48,
    ),
    RelationType(
        "follows_up",
        "The later message asks for the current status of the thing in the earlier message.",
        (
            "any update", "any news", "status", "how's it going", "still", "eta", "when will",
            "อัพเดท", "อัปเดท", "คืบหน้า", "ถึงไหน", "ยังไง", "เมื่อไหร่", "เป็นไง", "ได้ยัง", "แล้วยัง",
        ),
        "a status chase on an earlier item",
    ),
    RelationType(
        "same_topic",
        "The two messages are about the same work item.",
        (),
        "related, but no specific relation detected",
    ),
)
TYPES_BY_NAME = {relation.name: relation for relation in RELATION_TYPES}
TYPED_RELATIONS = tuple(relation for relation in RELATION_TYPES if relation.cues)

QUESTION_RE = re.compile(r"\?|ไหม|มั้ย|หรือเปล่า|หรือยัง|ยังไง|เมื่อไหร่|อะไร|ทำไม")
LATIN_RE = re.compile(r"[A-Za-z]")
_cue_patterns: dict[str, re.Pattern[str]] = {}


def cue_offset(text: str, cue: str) -> int:
    """Where `cue` occurs in `text`, or -1.

    Latin cues need word boundaries or they match inside other words — a bare
    substring search finds "no" in "Android", "know" and "now", which turned every
    message into an answer. Thai cues stay substring searches: Thai does not put
    spaces between words, so there is no boundary to anchor to.
    """
    if not LATIN_RE.search(cue):
        return text.find(cue)
    pattern = _cue_patterns.get(cue)
    if pattern is None:
        pattern = _cue_patterns[cue] = re.compile(rf"(?<![A-Za-z]){re.escape(cue)}(?![A-Za-z])")
    match = pattern.search(text)
    return match.start() if match else -1


@dataclass
class Relation:
    """One directed, typed edge between two messages."""

    source: int  # the earlier message
    target: int  # the later message
    name: str
    score: float
    evidence: str = ""

    def as_dict(self, records: Sequence[dict[str, Any]]) -> dict[str, Any]:
        return {
            "source_id": str(records[self.source]["id"]),
            "target_id": str(records[self.target]["id"]),
            "relation": self.name,
            "score": round(self.score, 4),
            "evidence": self.evidence,
        }


def order_by_time(records: Sequence[dict[str, Any]], left: int, right: int) -> tuple[int, int]:
    """(earlier, later). Ties fall back to corpus order, which is export order."""
    from tam.retrieval.signals import timestamp

    left_time, right_time = timestamp(records[left]), timestamp(records[right])
    if np.isfinite(left_time) and np.isfinite(right_time) and right_time != left_time:
        return (left, right) if left_time < right_time else (right, left)
    return (left, right) if left <= right else (right, left)


def matched_cue(text: str, relation: RelationType) -> str:
    """The first cue phrase present within the type's window, or "".

    Returned rather than a boolean because it becomes the evidence string: a
    relation you cannot explain is a relation you cannot debug.
    """
    lowered = text.lower()
    for cue in relation.cues:
        offset = cue_offset(lowered, cue)
        if offset < 0:
            continue
        if relation.cue_window is not None and offset > relation.cue_window:
            continue
        return cue
    return ""


def classify_by_rules(records: Sequence[dict[str, Any]], source: int, target: int) -> Relation:
    """Type a pair from cue phrases in the later message plus question shape.

    Cheap, explainable, and no model. The cost is coverage: a cue list cannot see
    a paraphrase it does not contain, which is what `--method nli` is for.
    """
    later = str(records[target]["text"])
    earlier = str(records[source]["text"])
    asked = bool(QUESTION_RE.search(earlier))

    for relation in TYPED_RELATIONS:
        cue = matched_cue(later, relation)
        if not cue:
            continue
        # "answers" is the weakest cue set — a bare "ได้เลย" is only an answer if
        # something was actually asked first.
        if relation.name == "answers" and not asked:
            continue
        return Relation(source, target, relation.name, relation.confidence, f"cue “{cue}”")
    return Relation(source, target, "same_topic", 0.5, "graph edge, no cue matched")


class NliClassifier:
    """Zero-shot relation typing with a multilingual NLI cross-encoder.

    Each relation is phrased as a hypothesis about the pair, and the pair itself
    is the premise. The hypothesis template is "{}" so the label *is* the sentence
    — the pipeline's default "This example is {}." would make nonsense of a full
    clause.
    """

    def __init__(self, model: str = DEFAULT_NLI_MODEL) -> None:
        from transformers import pipeline  # heavy import

        log.info("Loading NLI model %s (the first run downloads it)", model)
        self.pipeline = pipeline("zero-shot-classification", model=model)
        self.hypotheses = [relation.hypothesis for relation in RELATION_TYPES]
        self.by_hypothesis = {relation.hypothesis: relation.name for relation in RELATION_TYPES}

    def classify(self, records: Sequence[dict[str, Any]], source: int, target: int) -> Relation:
        premise = (
            f"Earlier message: {shorten(str(records[source]['text']), 600)}\n"
            f"Later message: {shorten(str(records[target]['text']), 600)}"
        )
        # multi_label=False makes the hypotheses compete in one softmax, so the
        # score is "which relation is it" rather than "is each plausible". Scored
        # independently they all saturate at 1.00 and the choice becomes arbitrary.
        result = self.pipeline(premise, self.hypotheses, hypothesis_template="{}", multi_label=False)
        best_label, best_score = result["labels"][0], float(result["scores"][0])
        runner_up = float(result["scores"][1]) if len(result["scores"]) > 1 else 0.0
        return Relation(
            source,
            target,
            self.by_hypothesis[best_label],
            best_score,
            f"NLI {best_score:.2f} vs next {runner_up:.2f}",
        )


def extract_relations(
    records: Sequence[dict[str, Any]],
    pairs: Sequence[tuple[int, int]],
    *,
    method: str = "rules",
    nli_model: str = DEFAULT_NLI_MODEL,
    min_score: float = 0.0,
) -> list[Relation]:
    """Type every candidate pair, dropping anything below `min_score`."""
    classifier = NliClassifier(nli_model) if method == "nli" else None
    relations: list[Relation] = []
    for left, right in pairs:
        source, target = order_by_time(records, left, right)
        relation = (
            classifier.classify(records, source, target)
            if classifier is not None
            else classify_by_rules(records, source, target)
        )
        if relation.score >= min_score:
            relations.append(relation)
    relations.sort(key=lambda relation: (-relation.score, relation.name))
    return relations


def type_counts_figure(relations: Sequence[Relation]) -> go.Figure:
    """One series: how much of each relation the channel actually contains."""
    counts = {relation.name: 0 for relation in RELATION_TYPES}
    for relation in relations:
        counts[relation.name] += 1
    names = [name for name in counts if counts[name]] or list(counts)
    values = [counts[name] for name in names]
    labels = [f"{name}<br><span style='color:{INK_MUTED}'>{TYPES_BY_NAME[name].description}</span>" for name in names]

    figure = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker={"color": SERIES_1, "cornerradius": 4},
            text=[str(value) for value in values],
            textposition="outside",
            textfont={"color": INK_SECONDARY},
            hovertemplate="%{y}<br>%{x} pair(s)<extra></extra>",
        )
    )
    layout = base_layout("Relations found, by type — direction included", height=120 + 58 * len(names))
    layout["xaxis"] |= {"title": {"text": "message pairs", "font": {"color": INK_MUTED}}}
    layout["yaxis"] |= {"autorange": "reversed", "gridcolor": SURFACE}
    layout["margin"] |= {"l": 330}
    layout["bargap"] = 0.4
    figure.update_layout(**layout)
    return figure


def relations_table(relations: Sequence[Relation], records: Sequence[dict[str, Any]], limit: int = 200) -> str:
    """The relations themselves. An arrow is the point, so the table shows one."""
    rows = "".join(
        "<tr>"
        f"<td class='num'>{html.escape(relation.name)}</td>"
        f"<td class='num'>{relation.score:.2f}</td>"
        f"<td>{html.escape(' '.join(str(records[relation.source]['text']).split())[:130])}"
        f"<div class='arrow'>↓ {html.escape(relation.name)}</div>"
        f"{html.escape(' '.join(str(records[relation.target]['text']).split())[:130])}</td>"
        f"<td class='num'>{html.escape(format_timestamp(str(records[relation.target].get('ts', ''))))}</td>"
        f"<td>{html.escape(relation.evidence)}</td>"
        "</tr>"
        for relation in relations[:limit]
    )
    note = f"<p class='lede'>Showing {min(limit, len(relations))} of {len(relations)}.</p>" if len(relations) > limit else ""
    return (
        "<details class='table-view' open><summary>Every relation, earlier message above later</summary>"
        f"{note}<table><thead><tr><th>relation</th><th>score</th><th>pair</th><th>when</th><th>why</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></details>"
        "<style>.arrow{color:#898781;font-size:12px;margin:4px 0;}</style>"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help=f"Output HTML (default {DEFAULT_OUTPUT})")
    parser.add_argument("--relations", type=Path, help="Also write the typed relations as JSON")
    parser.add_argument("--method", default="rules", choices=("rules", "nli"), help="How to type each pair (default rules)")
    parser.add_argument("--nli-model", default=DEFAULT_NLI_MODEL, help=f"NLI checkpoint (default {DEFAULT_NLI_MODEL})")
    parser.add_argument("--min-score", type=float, default=0.0, help="Drop relations scoring below this")
    parser.add_argument("--knn", type=int, default=6, help="Dense neighbours per message when building candidates (default 6)")
    parser.add_argument("--typed-only", action="store_true", help="Drop the untyped same_topic fallback")
    parser.add_argument("--model", help="Embedding model id; overrides EMBEDDING_MODEL for this run")
    parser.add_argument("--include-threads", action="store_true", help="Also relate whole-thread records")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()
    set_model(args.model)

    records = load_records(args.records, include_threads=args.include_threads)
    raw = embed_records(records)
    matrix = apply_transform(raw, fit_transform(raw, "none"))
    signals = SignalIndex(records)
    graph = build_graph(records, matrix, signals, weights=EdgeWeights(), knn=args.knn)
    pairs = [(int(left), int(right)) for left, right in graph.edges]
    log.info("Typing %d candidate pair(s) with method %s", len(pairs), args.method)

    relations = extract_relations(
        records, pairs, method=args.method, nli_model=args.nli_model, min_score=args.min_score
    )
    if args.typed_only:
        dropped = sum(1 for relation in relations if relation.name == "same_topic")
        relations = [relation for relation in relations if relation.name != "same_topic"]
        log.info("Dropped %d untyped same_topic pair(s)", dropped)

    counts: dict[str, int] = {}
    for relation in relations:
        counts[relation.name] = counts.get(relation.name, 0) + 1
    print(f"\n{'relation':14} count  what it means")
    print("-" * 72)
    for relation_type in RELATION_TYPES:
        if counts.get(relation_type.name):
            print(f"{relation_type.name:14} {counts[relation_type.name]:>5}  {relation_type.description}")
    typed = sum(count for name, count in counts.items() if name != "same_topic")
    print(f"\n{typed} of {len(relations)} pair(s) got a specific relation; the rest are same_topic only")

    print("\nStrongest typed relations:")
    for relation in [relation for relation in relations if relation.name != "same_topic"][:8]:
        print(f"\n  [{relation.name} {relation.score:.2f}] {relation.evidence}")
        print(f"    from: {shorten(str(records[relation.source]['text']), 96)}")
        print(f"      to: {shorten(str(records[relation.target]['text']), 96)}")

    if args.relations:
        args.relations.parent.mkdir(parents=True, exist_ok=True)
        args.relations.write_text(
            json.dumps([relation.as_dict(records) for relation in relations], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("Wrote %s", args.relations)

    tiles = [
        stat_tile("Candidate pairs", str(len(pairs)), f"graph edges, knn {args.knn}"),
        stat_tile("Relations kept", str(len(relations)), f"score >= {args.min_score:g}"),
        stat_tile("Specifically typed", str(typed), "not just same_topic"),
        stat_tile("Method", args.method, "rules = cue phrases, nli = entailment"),
    ]
    sections = [
        (
            "Every pair is directed: the earlier message points at the later one, because in a channel "
            "it is the later message that answers, resolves, or chases. A symmetric similarity score "
            "cannot carry that, which is the whole reason this module exists. "
            + (
                "Cue phrases decided each type, and the cue is shown as evidence."
                if args.method == "rules"
                else "An NLI model scored each relation as a hypothesis about the pair."
            ),
            type_counts_figure(relations).to_html(full_html=False, include_plotlyjs=True, config={"displayModeBar": False})
            + relations_table(relations, records),
        ),
    ]
    subtitle = f"{len(records)} messages · model {model_name()} · typing method {args.method}"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_page("Typed relations between messages", tiles, sections, subtitle), encoding="utf-8")
    log.info("Wrote %s. Open it with: open %s", args.out, args.out)


if __name__ == "__main__":
    main()
