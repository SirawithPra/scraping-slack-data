"""Interactive semantic search over prepared Slack records.

    python3 -m tam.core --top-k 10
    python3 -m tam.core -q "FE sorting เสร็จแล้วแต่ยังรอ BE API"
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv

from tam.retrieval.embeddings import cosine_top_k, embed_texts, embed_with_cache, model_name, quiet_third_party_logs, set_model

DEFAULT_RECORDS = Path("data/processed/messages.json")
PREVIEW_LIMIT = 400

log = logging.getLogger("semantic_search")


def load_records(path: Path, *, include_threads: bool = False) -> list[dict[str, Any]]:
    """Read prepared records; thread-context records are opt-in."""
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run prepare_messages.py first.")
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path} is not valid JSON: {error}") from error
    if not isinstance(records, list):
        raise SystemExit(f"{path} should contain a list of records.")
    if not include_threads:
        records = [record for record in records if record.get("source") != "slack_thread"]
    if not records:
        raise SystemExit(f"No searchable records in {path}. Re-run prepare_messages.py.")
    return records


def embed_records(records: list[dict[str, Any]], *, use_cache: bool = True) -> np.ndarray:
    """Embed the corpus. Only message text is embedded, never the metadata."""
    return embed_with_cache([str(record["text"]) for record in records], use_cache=use_cache)


def search(
    query: str, records: list[dict[str, Any]], matrix: np.ndarray, top_k: int
) -> list[tuple[float, dict[str, Any]]]:
    """Return the top_k most similar records, best first."""
    query_vector = embed_texts([query], role="query")[0]  # one-off, so not cached
    ranked, scores = cosine_top_k(query_vector, matrix, top_k)
    return [(float(scores[index]), records[index]) for index in ranked]


def format_timestamp(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "unknown"


def preview(text: str, limit: int = PREVIEW_LIMIT) -> str:
    """Collapse a record to one line, since thread records are multi-line."""
    single_line = " / ".join(part.strip() for part in text.splitlines() if part.strip())
    return single_line if len(single_line) <= limit else single_line[: limit - 1] + "…"


def print_matches(matches: list[tuple[float, dict[str, Any]]]) -> None:
    print("\nTop Matches:")
    if not matches:
        print("  (nothing found)")
        return
    for position, (score, record) in enumerate(matches, start=1):
        kind = " [thread context]" if record.get("source") == "slack_thread" else ""
        print(f"\n{position}. {score:.2f}{kind}\n   {preview(str(record['text']))}")
        details = [
            f"user={record.get('user') or '-'}",
            f"time={format_timestamp(str(record.get('ts', '')))}",
            f"thread={record.get('thread_ts') or '-'}",
            f"id={record.get('id', '-')}",
        ]
        print(f"   {'  '.join(details)}")


def run_interactive(records: list[dict[str, Any]], matrix: np.ndarray, top_k: int) -> None:
    print(f"\nReady: {len(records)} record(s), model {model_name()}, top-k {top_k}.")
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
        print_matches(search(query, records, matrix, top_k))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--top-k", type=int, default=10, help="Matches to show (default 10)")
    parser.add_argument("-q", "--query", action="append", help="Run one query and exit; repeatable")
    parser.add_argument(
        "--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})"
    )
    parser.add_argument(
        "--include-threads",
        action="store_true",
        help="Also search whole-thread records (off by default, they duplicate their messages)",
    )
    parser.add_argument("--model", help="Embedding model id; overrides EMBEDDING_MODEL for this run")
    parser.add_argument("--no-cache", action="store_true", help="Recompute embeddings instead of using the cache")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()
    set_model(args.model)
    if args.top_k <= 0:
        raise SystemExit("--top-k must be greater than zero.")

    records = load_records(args.records, include_threads=args.include_threads)
    log.info("Loaded %d record(s) from %s", len(records), args.records)
    matrix = embed_records(records, use_cache=not args.no_cache)

    if args.query:
        for query in args.query:
            print(f"\nSearch:\n> {query}")
            print_matches(search(query, records, matrix, args.top_k))
        return
    run_interactive(records, matrix, args.top_k)


if __name__ == "__main__":
    main()
