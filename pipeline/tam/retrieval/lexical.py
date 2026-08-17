"""BM25 lexical retrieval with a Thai-aware tokenizer.

Dense embeddings are good at "same topic, different words" and bad at the exact
strings a software team actually types: ticket ids (``REV-1421``), identifiers
(``getUserProfile``), error codes, product and customer names. Those carry almost
no semantic content, so an embedder blurs them — but they are precisely what
makes two messages about *the same* work item rather than a similar one.

BM25 is the complement: no training, no model download, exact-term matching with
frequency and length normalisation. Fused with the dense ranking it recovers the
cases embeddings drop. See fusion.py for the combination.

Thai is the wrinkle: it has no spaces between words, so whitespace tokenisation
turns a whole clause into one token that matches nothing. `word_tokenize` from
PyThaiNLP is used when installed; otherwise Thai runs fall back to overlapping
character n-grams, which is dictionary-free and works well enough for retrieval.

    python3 -m tam.retrieval.lexical -q "bug ใน Profile module"
"""

from __future__ import annotations

import argparse
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

# Okapi BM25 defaults. k1 caps how much a repeated term keeps helping; b is how
# hard long messages are penalised. 0.75 is standard; chat is short, so length
# normalisation matters less here than in document search.
K1 = 1.5
B = 0.75
# Terms below this IDF sit in roughly 60% of the corpus. They still count towards
# the score, but they are not an explanation, so `matched_terms` hides them.
MIN_EXPLAIN_IDF = 0.5

# Thai codepoints, and everything alphanumeric that may hold separators
# (kebab, snake, dotted, or slashed identifiers) as one run.
THAI_RUN_RE = re.compile(r"[฀-๿]+")
LATIN_RUN_RE = re.compile(r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*")
CAMEL_PART_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
SEPARATOR_RE = re.compile(r"[-_./]")
# Thai n-gram widths for the no-PyThaiNLP path. 2 and 3 together approximate
# syllables closely enough that real word overlap still scores.
FALLBACK_NGRAMS = (2, 3)

log = logging.getLogger(__name__)

_thai_tokenizer: Callable[[str], list[str]] | None = None
_thai_checked = False


def _load_thai_tokenizer() -> Callable[[str], list[str]] | None:
    """PyThaiNLP's dictionary tokenizer, or None so the caller can fall back."""
    global _thai_tokenizer, _thai_checked
    if _thai_checked:
        return _thai_tokenizer
    _thai_checked = True
    try:
        from pythainlp.tokenize import word_tokenize  # optional dependency
    except ImportError:
        log.info("PyThaiNLP is not installed; Thai falls back to character n-grams")
        return None

    def tokenize(text: str) -> list[str]:
        # newmm is the maximum-matching dictionary engine: fast and no model load.
        return [token for token in word_tokenize(text, engine="newmm") if token.strip()]

    _thai_tokenizer = tokenize
    return _thai_tokenizer


def thai_tokens(run: str) -> list[str]:
    """Split one run of Thai characters into indexable units."""
    tokenizer = _load_thai_tokenizer()
    if tokenizer is not None:
        return tokenizer(run)
    return [run[start : start + width] for width in FALLBACK_NGRAMS for start in range(len(run) - width + 1)] or [run]


def latin_tokens(run: str) -> list[str]:
    """Keep the identifier whole *and* emit its parts.

    ``REV-1421`` has to survive intact so a ticket id is a single strong match,
    while ``getUserProfile`` must also match a message that says "user profile".
    Emitting both means either phrasing finds it.
    """
    whole = run.lower()
    tokens = [whole]
    if SEPARATOR_RE.search(run):
        tokens.extend(part.lower() for part in SEPARATOR_RE.split(run) if part)
    parts = CAMEL_PART_RE.findall(run)
    if len(parts) > 1:
        tokens.extend(part.lower() for part in parts)
    return tokens


def tokenize(text: str) -> list[str]:
    """Tokens for BM25: Thai by dictionary, Latin by identifier-aware splitting.

    No stopword list. IDF already discounts the Thai particles and English filler
    that appear in most messages, and a hand-written list would need maintaining
    in two languages.
    """
    tokens: list[str] = []
    for match in re.finditer(r"[฀-๿]+|[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*", text):
        run = match.group(0)
        tokens.extend(thai_tokens(run) if THAI_RUN_RE.fullmatch(run) else latin_tokens(run))
    return tokens


class Bm25Index:
    """Okapi BM25 over a fixed corpus.

    Built as plain dicts rather than a sparse matrix: a Slack channel is small,
    and per-term posting lists keep `matched_terms` cheap for the reports.
    """

    def __init__(self, documents: Sequence[str], *, k1: float = K1, b: float = B) -> None:
        self.k1 = k1
        self.b = b
        self.count = len(documents)
        self.lengths = np.zeros(self.count, dtype=np.float32)
        self.postings: dict[str, dict[int, int]] = {}

        for index, document in enumerate(documents):
            tokens = tokenize(document)
            self.lengths[index] = len(tokens)
            for token, frequency in Counter(tokens).items():
                self.postings.setdefault(token, {})[index] = frequency

        self.average_length = float(self.lengths.mean()) if self.count else 0.0
        # Probabilistic IDF with the +1 guard, so a term in every message scores
        # ~0 instead of going negative and actively penalising a match.
        self.idf = {
            token: math.log(1.0 + (self.count - len(posting) + 0.5) / (len(posting) + 0.5))
            for token, posting in self.postings.items()
        }

    def scores(self, query: str) -> np.ndarray:
        """BM25 score of every document for `query`; unmatched documents score 0."""
        scores = np.zeros(self.count, dtype=np.float32)
        if not self.count or not self.average_length:
            return scores
        for token, query_frequency in Counter(tokenize(query)).items():
            posting = self.postings.get(token)
            if not posting:
                continue
            indices = np.fromiter(posting.keys(), dtype=np.int64, count=len(posting))
            frequencies = np.fromiter(posting.values(), dtype=np.float32, count=len(posting))
            denominator = frequencies + self.k1 * (1.0 - self.b + self.b * self.lengths[indices] / self.average_length)
            # A term repeated in the query counts more, capped the same way as in a document.
            query_weight = query_frequency * (self.k1 + 1.0) / (query_frequency + self.k1)
            scores[indices] += self.idf[token] * query_weight * frequencies * (self.k1 + 1.0) / denominator
        return scores

    def matched_terms(self, query: str, index: int, limit: int = 8, min_idf: float = MIN_EXPLAIN_IDF) -> list[str]:
        """Query terms that actually hit this document, strongest IDF first.

        This is the reason to prefer BM25 over a second embedding model: the
        answer is inspectable. A report can say *why* a message ranked.

        `min_idf` hides terms that appear in most of the corpus. They do match,
        and they contribute almost nothing to the score, so listing "the, to, a"
        as the reason a message ranked would be actively misleading.
        """
        hits = [
            (self.idf[token], token)
            for token in set(tokenize(query))
            if index in self.postings.get(token, {}) and self.idf[token] >= min_idf
        ]
        return [token for _, token in sorted(hits, reverse=True)[:limit]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-q", "--query", action="append", required=True, help="Query to run; repeatable")
    parser.add_argument("--records", type=Path, default=Path("data/processed/messages.json"), help="Prepared records")
    parser.add_argument("--top-k", type=int, default=10, help="Matches to show (default 10)")
    return parser.parse_args()


def main() -> None:
    """Run BM25 on its own, so its behaviour can be seen without the dense side."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from tam.core import load_records  # local import keeps the module import-light

    args = parse_args()
    records: list[dict[str, Any]] = load_records(args.records)
    index = Bm25Index([str(record["text"]) for record in records])
    log.info("Indexed %d record(s), %d distinct term(s)", index.count, len(index.postings))

    for query in args.query:
        scores = index.scores(query)
        print(f"\nSearch:\n> {query}")
        order = np.argsort(-scores)[: args.top_k]
        for position, record_index in enumerate(order, start=1):
            score = float(scores[record_index])
            if score <= 0:
                break
            terms = ", ".join(index.matched_terms(query, int(record_index)))
            text = " ".join(str(records[record_index]["text"]).split())[:120]
            print(f"{position:2}. {score:6.2f}  {text}\n     matched: {terms}")


if __name__ == "__main__":
    main()
