"""Non-semantic relation signals that Slack hands over for free.

Cosine only ever answers "is this worded like that". A Slack export also records
*who* said something, *when*, in *which thread*, and which concrete nouns two
messages share. Those are relation evidence of a different kind, and some of it is
stronger than any wording similarity:

* **thread** — messages in one thread are the same work item by definition. This
  is ground truth, not a guess.
* **time** — in a chat channel two messages minutes apart are almost always about
  the same thing, however differently they are phrased. "fixed, deploying now"
  carries no topic at all; its timestamp does.
* **anchors** — ticket keys, identifiers, file paths, versions, money amounts,
  quoted UI labels. Almost no semantic content, so embedders blur them, yet
  ``REV-1421`` in both messages is near-proof they are the same work item.
* **author** — weak on its own, real in aggregate: one person tends to keep
  posting about the thing they are working on.

Each signal is a score in 0-1 so fusion.py can weight them alongside the
retrievers. `pair_signals` exposes them per pair for the graph and the reports.

    python3 signals.py --anchors        # what the anchor extractor actually finds
"""

from __future__ import annotations

import argparse
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

import numpy as np

# How fast temporal proximity decays. 90 minutes means messages an hour and a
# half apart score ~0.5 — about the span of one working conversation in a channel.
TIME_HALF_LIFE_MINUTES = 90.0
# Beyond this, treat two messages as temporally unrelated rather than faintly related.
TIME_CUTOFF_HOURS = 48.0

# High-precision strings worth matching exactly. Deliberately not general nouns:
# BM25 already covers ordinary vocabulary, and these are the tokens embeddings lose.
ANCHOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ticket", re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")),  # REV-1421, PROJ-7
    ("version", re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")),
    ("path", re.compile(r"\b[\w./-]+\.(?:py|ts|tsx|js|jsx|json|ya?ml|sql|md|tf|sh|kt|swift|java|go|rb)\b")),
    ("url", re.compile(r"\bhttps?://[^\s<>\"]+|\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|co|dev|app|ai)\b", re.IGNORECASE)),
    ("identifier", re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+)+\b|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")),  # camelCase, snake_case
    ("acronym", re.compile(r"\b[A-Z]{3,6}\b")),
    ("money", re.compile(r"[$€£฿]\s?[\d,]+(?:\.\d+)?\s?[kKmM]?\b")),
    ("quoted", re.compile(r"[\"‘’“”*]([A-Za-z][\w .,&'-]{2,48}?)[\"‘’“”*]")),  # "Profile", *Omega, Inc.*
    # Straight single quotes need the letter guards, or English contractions pair
    # up into fake quotes: "I've fixed it, and we're" would yield 've fixed it, and we'.
    ("quoted", re.compile(r"(?<![A-Za-z])'([A-Za-z][\w .,&-]{2,48}?)'(?![A-Za-z])")),  # 'Profile'
    # Spaces only, never a newline, so a title does not absorb the line after it.
    ("proper", re.compile(r"\b[A-Z][a-z]{2,}(?:[ ]+[A-Z][a-z]{2,}){0,2}\b")),  # Profile, Sales Cloud, Omega Inc
)

# A capitalised word at the start of a sentence or line says nothing — English
# capitalises there anyway. These are the characters that mark such a position.
SENTENCE_START_CHARS = frozenset(".!?:;\n\r*•|-–—([{\"'“‘>")

# Words that match "proper" or "acronym" but say nothing about which work item
# this is. Kept short on purpose — IDF handles anything channel-specific.
ANCHOR_STOPWORDS = frozenset(
    {
        "the", "this", "that", "team", "hey", "hi", "hello", "thanks", "please", "just", "also",
        "any", "all", "and", "but", "for", "from", "with", "have", "has", "will", "can", "could",
        "would", "should", "here", "there", "what", "when", "where", "which", "who", "why", "how",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december", "today", "tomorrow", "yesterday",
        "update", "updates", "summary", "note", "notes", "thread", "channel", "message",
    }
)

log = logging.getLogger(__name__)


def at_sentence_start(text: str, position: int) -> bool:
    """Is `position` the first word of a sentence, line, or bullet?"""
    cursor = position - 1
    while cursor >= 0 and text[cursor] in " \t":
        cursor -= 1
    return cursor < 0 or text[cursor] in SENTENCE_START_CHARS


def anchors(text: str) -> set[str]:
    """Concrete strings that pin a message to a specific work item.

    Case is folded for matching but the surface form drives extraction, since
    ``REV`` and ``rev`` are the same anchor while capitalisation is what marks a
    proper noun in the first place.
    """
    found: set[str] = set()
    for kind, pattern in ANCHOR_PATTERNS:
        for match in pattern.finditer(text):
            value = " ".join((match.group(1) if pattern.groups else match.group(0)).split()).lower()
            if len(value) < 3 or value in ANCHOR_STOPWORDS:
                continue
            if all(part in ANCHOR_STOPWORDS for part in value.split()):
                continue
            # "Wanted to share..." is not a product name. A single capitalised word
            # only counts mid-sentence; two in a row is a proper noun anywhere,
            # which keeps titles like "Performance Review Progress" usable.
            if kind == "proper" and " " not in value and at_sentence_start(text, match.start()):
                continue
            found.add(value)
    return found


def timestamp(record: dict[str, Any]) -> float:
    """Slack ts as a float, or NaN when it is missing or malformed."""
    try:
        return float(str(record.get("ts", "")))
    except (TypeError, ValueError):
        return float("nan")


class SignalIndex:
    """Precomputed structural signals over one corpus.

    Built once per corpus and reused for every query, so the per-query cost is a
    few vector operations.
    """

    def __init__(self, records: Sequence[dict[str, Any]], *, half_life_minutes: float = TIME_HALF_LIFE_MINUTES) -> None:
        self.records = list(records)
        self.count = len(self.records)
        self.half_life = max(1.0, half_life_minutes)
        self.times = np.array([timestamp(record) for record in self.records], dtype=np.float64)
        self.threads = np.array([str(record.get("thread_ts", "")) for record in self.records])
        self.users = np.array([str(record.get("user", "")) for record in self.records])
        self.channels = np.array([str(record.get("channel_id", "")) for record in self.records])
        self.anchor_sets = [anchors(str(record["text"])) for record in self.records]

        # IDF over anchors: a ticket key mentioned once is decisive, a product name
        # in every message is not. Same reasoning as BM25's IDF, same formula.
        document_frequency = Counter(anchor for anchor_set in self.anchor_sets for anchor in anchor_set)
        self.anchor_idf = {
            anchor: math.log(1.0 + (self.count - frequency + 0.5) / (frequency + 0.5))
            for anchor, frequency in document_frequency.items()
        }
        self.anchor_weight = np.array(
            [sum(self.anchor_idf.get(anchor, 0.0) for anchor in anchor_set) for anchor_set in self.anchor_sets],
            dtype=np.float32,
        )

    def anchor_scores(self, text: str) -> np.ndarray:
        """IDF-weighted anchor overlap between `text` and every record, in 0-1.

        Works for a typed query as well as for another message, which is why this
        signal is available in both search modes.
        """
        scores = np.zeros(self.count, dtype=np.float32)
        query_anchors = anchors(text)
        if not query_anchors:
            return scores
        query_weight = sum(self.anchor_idf.get(anchor, 0.0) for anchor in query_anchors)
        if query_weight <= 0:
            return scores
        for index, anchor_set in enumerate(self.anchor_sets):
            shared = query_anchors & anchor_set
            if shared:
                # Normalised by the query's own weight: "how much of what the query
                # pins down does this record also pin down".
                scores[index] = sum(self.anchor_idf[anchor] for anchor in shared) / query_weight
        return scores

    def time_scores(self, index: int) -> np.ndarray:
        """Exponential temporal proximity to record `index`, in 0-1."""
        scores = np.zeros(self.count, dtype=np.float32)
        origin = self.times[index]
        if not np.isfinite(origin):
            return scores
        with np.errstate(all="ignore"):
            minutes = np.abs(self.times - origin) / 60.0
            decayed = np.exp(-minutes / self.half_life)
        decayed[~np.isfinite(minutes)] = 0.0
        decayed[minutes > TIME_CUTOFF_HOURS * 60.0] = 0.0
        return decayed.astype(np.float32)

    def thread_scores(self, index: int) -> np.ndarray:
        """1.0 for the same thread, 0.0 otherwise. The only exact signal here."""
        thread = self.threads[index]
        if not thread:
            return np.zeros(self.count, dtype=np.float32)
        return (self.threads == thread).astype(np.float32)

    def user_scores(self, index: int) -> np.ndarray:
        """1.0 for the same author. Weak alone, useful as a tie-breaker."""
        user = self.users[index]
        if not user:
            return np.zeros(self.count, dtype=np.float32)
        return (self.users == user).astype(np.float32)

    def channel_scores(self, index: int) -> np.ndarray:
        channel = self.channels[index]
        if not channel:
            return np.zeros(self.count, dtype=np.float32)
        return (self.channels == channel).astype(np.float32)

    def pair_signals(self, left: int, right: int) -> dict[str, float]:
        """Every signal for one pair, for the graph edges and the HTML reports."""
        shared = self.anchor_sets[left] & self.anchor_sets[right]
        total = self.anchor_weight[left] + self.anchor_weight[right]
        return {
            "thread": float(self.threads[left] == self.threads[right] and bool(self.threads[left])),
            "time": float(self.time_scores(left)[right]),
            "user": float(self.users[left] == self.users[right] and bool(self.users[left])),
            "channel": float(self.channels[left] == self.channels[right] and bool(self.channels[left])),
            # Symmetric here (2x shared / total weight), unlike the query-oriented
            # `anchor_scores`, because a graph edge has no direction to normalise by.
            "anchors": float(2.0 * sum(self.anchor_idf[anchor] for anchor in shared) / total) if total > 0 else 0.0,
        }

    def shared_anchors(self, left: int, right: int, limit: int = 6) -> list[str]:
        """The concrete strings two messages have in common, most distinctive first."""
        shared = self.anchor_sets[left] & self.anchor_sets[right]
        return [anchor for _, anchor in sorted(((self.anchor_idf[a], a) for a in shared), reverse=True)[:limit]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=Path("data/processed/messages.json"), help="Prepared records")
    parser.add_argument("--anchors", action="store_true", help="List the anchors extracted per message")
    parser.add_argument("--top", type=int, default=20, help="Most distinctive anchors to list (default 20)")
    return parser.parse_args()


def main() -> None:
    """Inspect the extractor: anchors are only useful if they are the right strings."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from semantic_search import load_records

    args = parse_args()
    records = load_records(args.records)
    index = SignalIndex(records)
    log.info("Extracted %d distinct anchor(s) from %d record(s)", len(index.anchor_idf), index.count)

    if args.anchors:
        for record, anchor_set in zip(index.records, index.anchor_sets):
            text = " ".join(str(record["text"]).split())[:70]
            print(f"\n{text}\n  -> {', '.join(sorted(anchor_set)) or '(none)'}")
        return

    print(f"\n{'anchor':40} idf   messages")
    print("-" * 60)
    ranked = sorted(index.anchor_idf.items(), key=lambda item: -item[1])
    for anchor, idf in ranked[: args.top]:
        appears = sum(1 for anchor_set in index.anchor_sets if anchor in anchor_set)
        print(f"{anchor[:40]:40} {idf:.2f}  {appears}")
    print(f"\n(most distinctive first; low idf means it is in most messages and carries little)")


if __name__ == "__main__":
    main()
