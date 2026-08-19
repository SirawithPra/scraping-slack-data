"""Interactive semantic search over prepared Slack records.

    python3 -m tam.core --top-k 10
    python3 -m tam.core -q "FE sorting เสร็จแล้วแต่ยังรอ BE API"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from dataclasses import dataclass, field
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


def skipped_channels() -> frozenset[str]:
    """Channel ids `TAM_SKIP_CHANNELS` says are not work, as a comma-separated list.

    Some channels exist to try the bot out. On this workspace `#meow-meow` and `#meowtamm`
    hold thirteen and twelve messages of slash-command tests, pitch-deck drafting and an
    argument about ice cream, and none of it is the team building the product — but the
    clustering has no way to know that, so it read the pitch draft as a work item and
    reported it `resolved`, and `ยังกินติมอยุ่เลย` turned up as evidence inside a *real*
    blocked item. Filtering by hand beats inferring it: "is this channel about the work"
    is a fact only a person has, and the cost of guessing wrong is a channel silently
    missing from the standup.

    This is the same judgement `TAM_SELF_USER` makes about the bot's own posts, one level
    up. The bot still reads and answers in these channels — that is what they are for.
    Only the analysis ignores them.
    """
    raw = os.getenv("TAM_SKIP_CHANNELS", "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class ProjectMap:
    """Which channels belong to which project, as somebody wrote it down.

    Everything else in this pipeline infers structure from the text. This is the one
    place a person gets to *state* it, and it is worth stating because no amount of
    reading messages recovers it: two channels called `#reverapp-dev` and `#rvr-qa`
    are one project, `#mobile` is a different one, and only the team knows that. The
    consequences are concrete — the linker stops picking a foreign project's ticket
    out of a message that names two, and the Slack ticket picker opens on the right
    few hundred issues instead of every issue in the tracker.

    Empty is the ordinary state, and everything degrades to what it did before: the
    linker falls back to corpus frequency, the picker searches every configured project.
    """

    by_channel: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    #: `#name` keys, kept apart because a record carries a channel *id* and nothing here
    #: can resolve a name. The Slack bot can, so it reads this half; the pipeline does not.
    by_name: dict[str, str] = field(default_factory=dict)

    def project_for(self, channel_id: Any) -> str:
        return self.by_channel.get(str(channel_id or "").strip(), "")

    def channels_of(self, project: str) -> list[str]:
        wanted = project.strip().upper()
        return [channel for channel, name in self.by_channel.items() if name == wanted]

    def projects(self) -> list[str]:
        """Every project named, in the order they were written."""
        seen: dict[str, None] = {}
        for name in list(self.by_channel.values()) + list(self.by_name.values()):
            seen.setdefault(name, None)
        return list(seen)

    def label_of(self, project: str) -> str:
        key = project.strip().upper()
        return self.labels.get(key, key)

    def __bool__(self) -> bool:
        return bool(self.by_channel or self.by_name)


#: `REVERAPP (Rever App)=C0ABC,C0DEF; MOB=C0GHI` — one group per project, because that
#: is the direction people think in ("these channels are the same project"), and because
#: a channel-keyed syntax makes the many-channels-one-project case a repetition you can
#: get subtly wrong. The parenthesised label is optional and is what the UI prints.
_PROJECT_GROUP = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]{0,19})\s*(?:\(([^)]*)\))?\s*=\s*(.*)$", re.S)


def channel_projects(raw: str | None = None) -> ProjectMap:
    """Read `TAM_CHANNEL_PROJECTS`, which says what project each channel is.

    Unparseable groups are logged and skipped rather than raising: this is a
    convenience that makes several other things sharper, and a typo in it must not
    take down the digest that works fine without it.
    """
    text = os.getenv("TAM_CHANNEL_PROJECTS", "") if raw is None else raw
    by_channel: dict[str, str] = {}
    by_name: dict[str, str] = {}
    labels: dict[str, str] = {}
    for group in text.split(";"):
        if not group.strip():
            continue
        match = _PROJECT_GROUP.match(group)
        if not match:
            log.warning("TAM_CHANNEL_PROJECTS: ข้ามกลุ่มที่อ่านไม่ออก %r (รูปแบบ: PROJ=C0ABC,C0DEF)", group.strip())
            continue
        project = match.group(1).upper()
        if match.group(2):
            labels[project] = match.group(2).strip()
        for channel in match.group(3).split(","):
            entry = channel.strip()
            if not entry:
                continue
            if entry.startswith("#"):
                by_name[entry.lower()] = project
            else:
                by_channel[entry] = project
    return ProjectMap(by_channel=by_channel, labels=labels, by_name=by_name)


def read_records(
    path: Path, *, include_threads: bool = False, skip_channels: bool = True
) -> list[dict[str, Any]]:
    """Read prepared records; thread-context records are opt-in.

    Raises TamDataError, so a long-lived caller (the web app) can answer its own
    request instead of dying. CLIs call `load_records` below.

    Skipped channels are dropped here, at the one door every stage comes through, rather
    than at ingest. An id-keyed merge can add and replace but not forget, so a channel
    excluded at ingest would stay in a corpus built before the exclusion — and the digest,
    the search, the bot's API and the dashboard would each be reading a different set of
    messages depending on when their copy was written.

    `skip_channels=False` is for the one caller that reads the corpus in order to *write*
    it back: `ingest.daily` merges each export into what it loaded, so any filter applied
    on the way in deletes those records from disk on the way out. That turns a display
    setting into permanent data loss — measured, it removed 27 records the first time the
    morning job ran after the setting was introduced, and recovering them would have meant
    re-exporting from Slack. `include_threads` exists for exactly this reason and daily
    already passes it; this is the same hazard one filter later.
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
    skip = skipped_channels() if skip_channels else frozenset()
    if skip:
        before = len(records)
        records = [record for record in records if str(record.get("channel_id") or "") not in skip]
        if before != len(records):
            log.info("Skipped %d record(s) from %d excluded channel(s)", before - len(records), len(skip))
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
