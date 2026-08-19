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
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from pythainlp.tokenize import word_tokenize
import plotly.graph_objects as go
from dotenv import load_dotenv

from tam.retrieval.embeddings import apply_transform, fit_transform, model_name, quiet_third_party_logs, set_model
from tam.ingest.quoted import asserted_lines, for_analysis
from tam.analysis.graph import EdgeWeights, build_graph
from tam.core import DEFAULT_RECORDS, embed_records, format_timestamp, load_records
from tam.retrieval.signals import SignalIndex, anchors
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
            "any update", "any news", "any progress", "any status", "status update", "status on",
            "what's the status", "how's it going", "still waiting", "still blocked", "still not",
            "still no", "still pending", "any eta", "eta on", "eta for", "eta?", "when will",
            "อัพเดท", "อัปเดท", "คืบหน้า", "ถึงไหน", "เมื่อไหร่", "เป็นไง", "ได้ยัง", "แล้วยัง", "เสร็จยัง",
        ),
        "a status chase on an earlier item",
        # A chase leads with the ask and is short — "any update on REV-1421?".
        # Deep inside a long standup transcript the same words are narration, not
        # a chase, which is how a whole meeting chunk used to read as one.
        cue_window=48,
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


@lru_cache(maxsize=4096)
def _thai_tokens(text: str) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """(tokens, the character offset each token starts at).

    Cached because every relation type asks the same message about its own cues, so a
    corpus-sized run tokenises each text once instead of once per cue.
    """
    tokens = tuple(word_tokenize(text, engine="newmm"))
    offsets: list[int] = []
    cursor = 0
    for token in tokens:
        found = text.find(token, cursor)
        if found < 0:  # engine normalised something away; keep the offsets monotonic
            found = cursor
        offsets.append(found)
        cursor = found + len(token)
    return tokens, tuple(offsets)


def thai_offset(text: str, cue: str) -> int:
    """Where `cue` occurs in `text` as a run of whole words, or -1.

    Thai writes without spaces, so there is no boundary character to anchor a search
    to — which is why this used to be `text.find(cue)`, and why that was wrong in both
    directions on the real corpus.

    Substring search over-matches: `รอ` occurs inside `รอบ`, `กรอก` and `หรอ`, so the
    bare cue hit 92 messages of which almost none were about waiting, and `ต้องรอ`
    matched inside a sentence about asking a user tomorrow. Matching single tokens
    instead under-matches, and worse: the cue list is full of multi-syllable phrases
    (`ยังรอ`, `ยังไม่มา`, `ยังทำไม่ได้`) that the tokeniser splits, so `ไม่ได้` as one
    token never appears and those cues could never fire at all.

    Tokenising both sides and looking for the cue's tokens as a contiguous run handles
    both: `ยังรอ` is `[ยัง, รอ]` and matches `ยังรอ api อยู่`, while `รอบนี้` is
    `[รอบ, นี้]` and does not contain `[รอ]`. This is the fix `docs/EXPERIMENTS.md` §7
    named and nothing implemented.
    """
    haystack, offsets = _thai_tokens(text)
    needle = tuple(word_tokenize(cue, engine="newmm"))
    if not needle:
        return -1
    limit = len(haystack) - len(needle)
    for start in range(limit + 1):
        if haystack[start : start + len(needle)] == needle:
            return offsets[start]
    return -1


def cue_offset(text: str, cue: str) -> int:
    """Where `cue` occurs in `text`, or -1.

    Latin cues need word boundaries or they match inside other words — a bare
    substring search finds "no" in "Android", "know" and "now", which turned every
    message into an answer. Thai cues are matched as whole words too, by tokenising
    both sides — see `thai_offset` for why substring search failed both ways.
    """
    if not LATIN_RE.search(cue):
        return thai_offset(text, cue)
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


def cue_line(lines: Sequence[str], relation: RelationType) -> tuple[int, str, str]:
    """(index, the line, the cue) of the first asserted line carrying one of `relation`'s cues.

    `(-1, "", "")` when no line carries one. Matching per line rather than per message is
    what makes `cue_window` mean what its docstring says — "an answer opens with its
    answer word" is a fact about a line, and a twelve-line daily update collapsed into
    one string has no openings to speak of except the first.
    """
    for index, line in enumerate(lines):
        cue = matched_cue(line.lower(), relation)
        if cue:
            return index, line, cue
    return -1, "", ""


def line_speaks_for_pair(line: str, opening: str, earlier: dict[str, Any]) -> str:
    """Why this line may type a relation to `earlier`, or "" if nothing connects them.

    A cue on a post's *opening* line speaks for the post: that is what the post leads
    with. Eight lines down it speaks for its own line, and the other eleven lines of a
    daily update are about other work — so something has to tie that one line to the
    message it is supposed to be answering.

    Three things count. A shared anchor is the strong one: a ticket key, a path, an
    identifier, a product name that both the line and the earlier message name. A mention
    of the earlier message's author counts too, and this team makes that link carry real
    weight — they write `Pending Mild - list all field`, where the person waited on *is*
    the subject. It is accepted from the post's `opening` line as well as from the cue's
    own line, because a bare `@somebody` on line one is how a person addresses a reply,
    and it scopes everything underneath it.

    Measured on the frozen 1,324-record corpus, this is why the rule exists: `blocked_by`
    was 11 relations of which 7 rested on a line nothing connected to its partner —
    `waiting for clearing user on dev` attached to three other people's daily posts and to
    the `Daily _ Please share an update` prompt itself. `resolves` had 37 such, including
    `Waiting for the bug to be fixed and retested` (a blocker, read as a resolution),
    `estimate time p'mos to fix is within 6th Aug` (a date in the future), and
    `Reward Redeemed คุณได้แลกรางวัลเรียบร้อย`, which is UI copy being specified rather
    than anybody reporting that work is done.

    The opening-line clause is not decoration: without it the gate also cut a real
    resolution — a post opening `@U0BGWS0ASN5` and continuing `1.แก้แล้ว`, `6.แก้ละ`,
    `7.สร้างแล้ว`, which answers that person's numbered list of API problems point by
    point. Its cue line is four characters long and can carry no anchor at all. With the
    clause, that pair is kept and none of the 18 wrong cases returns.
    """
    shared = anchors(line) & anchors(for_analysis(earlier))
    if shared:
        return f"ทั้งสองข้อความอ้าง “{sorted(shared)[0]}”"
    author = str(earlier.get("user") or "")
    if author and author in line:
        return "บรรทัดนี้เอ่ยถึงคนที่เขียนข้อความก่อนหน้า"
    if author and author in opening:
        return "โพสต์นี้เปิดด้วยการเรียกคนที่เขียนข้อความก่อนหน้า"
    return ""


def classify_by_rules(records: Sequence[dict[str, Any]], source: int, target: int) -> Relation:
    """Type a pair from cue phrases in the later message plus question shape.

    Cheap, explainable, and no model. The cost is coverage: a cue list cannot see
    a paraphrase it does not contain, which is what `--method nli` is for.

    Cues are read line by line, and a cue below the opening line has to earn the pair —
    see `line_speaks_for_pair`. A one-line message is its own opening line, so the short
    conversational traffic that is most of this corpus is unaffected: of 472 typed
    relations the rule changes 59, and all 59 are wrong today.
    """
    # The asserted part only. A cue inside a fenced block of quoted app-store reviews
    # marked a real work item `resolved`; the customer wrote the word, not the team.
    later = for_analysis(records[target])
    lines = asserted_lines(str(records[target].get("text", "")))
    earlier = str(records[source]["text"])
    asked = bool(QUESTION_RE.search(earlier))

    for relation in TYPED_RELATIONS:
        # A windowed type is already confined to the message opening, which is the same
        # protection the line gate gives — so it keeps the whole-message match it always
        # had. Widening the window to every line's opening was measured and was wrong in
        # both directions: it invented five `follows_up` where the cue `any status` sat
        # inside `reward of any status (DRAFT/PAUSED/EXPIRED included) is returned`, a
        # line of an API specification and not a soul chasing anything, and it dropped a
        # real answer — `3.4.0(352) เทสลิ้ง uat ที่ใช้อยู่ได้เลย` — for being on line two.
        if relation.cue_window is not None:
            cue = matched_cue(later, relation)
            if not cue:
                continue
            # "answers" is the weakest cue set — a bare "ได้เลย" is only an answer if
            # something was actually asked first.
            if relation.name == "answers" and not asked:
                continue
            return Relation(source, target, relation.name, relation.confidence, f"cue “{cue}”")

        index, line, cue = cue_line(lines, relation)
        if not cue:
            continue
        if index == 0 or len(lines) == 1:
            return Relation(source, target, relation.name, relation.confidence, f"cue “{cue}”")
        why = line_speaks_for_pair(line, lines[0], records[source])
        if not why:
            continue
        return Relation(
            source,
            target,
            relation.name,
            relation.confidence,
            f"cue “{cue}” ที่บรรทัด {index + 1}/{len(lines)} — {why}",
        )
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
