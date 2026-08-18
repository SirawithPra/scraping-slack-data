"""Interactive semantic search over prepared Slack records.

    python3 -m tam.core --top-k 10
    python3 -m tam.core -q "FE sorting เสร็จแล้วแต่ยังรอ BE API"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from dotenv import load_dotenv

from tam.ingest.quoted import for_analysis
from tam.retrieval.embeddings import cosine_top_k, embed_texts, embed_with_cache, model_name, quiet_third_party_logs, set_model

DEFAULT_RECORDS = Path("data/processed/messages.json")
PREVIEW_LIMIT = 400

log = logging.getLogger("semantic_search")


class TamDataError(Exception):
    """The corpus on disk cannot be used, and the caller needs to be told why.

    A plain Exception on purpose: these functions run inside the web app as well
    as in CLIs, and a SystemExit raised in a request handler is a BaseException
    that slips past every `except Exception` between here and the response, so
    the operator gets a bare 500 instead of the diagnostic. CLIs get the same
    one-line message from `load_records`, which converts it.
    """


def validate_records(path: Path, parsed: Any) -> list[dict[str, Any]]:
    """Confirm a file really holds prepared records before anything indexes it.

    The mistake this catches is pointing --records at one of the other
    list-shaped JSON files this repo writes into data/processed/ (clusters.json,
    relations.json); without it the run dies deep inside a comprehension.
    """
    if not isinstance(parsed, list):
        raise TamDataError(f"{path} should contain a list of records.")
    for position, record in enumerate(parsed):
        if not isinstance(record, dict) or "id" not in record or "text" not in record:
            raise TamDataError(
                f"{path}: record {position} is not an object with 'id' and 'text'. "
                "Expected the output of tam.ingest.prepare_messages."
            )
    return parsed


def read_records(path: Path, *, include_threads: bool = False) -> list[dict[str, Any]]:
    """Read prepared records; thread-context records are opt-in.

    Raises TamDataError, so a long-lived caller (the web app) can answer its own
    request instead of dying. CLIs call `load_records` below.
    """
    if not path.exists():
        raise TamDataError(f"Missing {path}. Run tam.ingest.prepare_messages first.")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise TamDataError(f"{path} is not valid JSON: {error}") from error
    records = validate_records(path, parsed)
    if not include_threads:
        records = [record for record in records if record.get("source") != "slack_thread"]
    if not records:
        raise TamDataError(f"No searchable records in {path}. Re-run tam.ingest.prepare_messages.")
    return records


def load_records(path: Path, *, include_threads: bool = False) -> list[dict[str, Any]]:
    """`read_records` for command lines: one line of explanation, no traceback."""
    try:
        return read_records(path, include_threads=include_threads)
    except TamDataError as error:
        raise SystemExit(str(error)) from error


def write_records(path: Path, records: Sequence[dict[str, Any]]) -> None:
    """Write the corpus so no reader can observe a half-written file.

    Several stages rewrite this one file, and the web app re-reads it while they
    do, so the bytes land in a sibling temp file and are moved into place with a
    single atomic rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(list(records), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except BaseException:  # Ctrl-C included: never leave a stray half-corpus behind
        temporary.unlink(missing_ok=True)
        raise


def embed_records(records: list[dict[str, Any]], *, use_cache: bool = True, prune: bool = False) -> np.ndarray:
    """Embed the corpus. Only message text is embedded, never the metadata.

    `prune` is off by default even though every caller here passes a whole corpus,
    because one cache file is shared by every corpus using the same model — the
    quickstart's sample and the real export both land in it. Pruning while embedding
    one would evict the other's vectors and re-embed them on the next run, trading
    disk space for a slower loop. Pass it when you are cleaning up on purpose.
    """
    # `analysis_text`, not `text`: one 6,191-character terms document is 5.5% of this
    # corpus's characters, and embedding pasted bulk pulls unrelated messages together
    # on shared boilerplate. See tam.ingest.quoted.
    return embed_with_cache([for_analysis(record) or str(record["text"]) for record in records], use_cache=use_cache, prune=prune)


def search(
    query: str, records: list[dict[str, Any]], matrix: np.ndarray, top_k: int
) -> list[tuple[float, dict[str, Any]]]:
    """Return the top_k most similar records, best first."""
    query_vector = embed_texts([query], role="query")[0]  # one-off, so not cached
    ranked, scores = cosine_top_k(query_vector, matrix, top_k)
    return [(float(scores[index]), records[index]) for index in ranked]


def format_timestamp(ts: str, *, tz: tzinfo | None = None) -> str:
    """Render epoch seconds for display, in `tz` (default: this machine's zone).

    The conversion goes through UTC explicitly. Stored timestamps are instants,
    and a naive `fromtimestamp` reads as if the epoch had no zone at all — which
    is how a display and the value behind it came to disagree by an offset.
    """
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d %H:%M")
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
    parser.add_argument(
        "--prune-cache",
        action="store_true",
        help="Drop cached vectors this corpus does not use. One cache file is shared by every "
        "corpus on the same model, so this evicts the others — use it to reclaim disk, not routinely",
    )
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
    matrix = embed_records(records, use_cache=not args.no_cache, prune=args.prune_cache)

    if args.query:
        for query in args.query:
            print(f"\nSearch:\n> {query}")
            print_matches(search(query, records, matrix, args.top_k))
        return
    run_interactive(records, matrix, args.top_k)


if __name__ == "__main__":
    main()
