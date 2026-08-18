"""Turn notes somebody typed into corpus records.

    python3 -m tam.ingest.notes --file today.txt --author "PO" --merge-into data/processed/real_all.json
    pbpaste | python3 -m tam.ingest.notes --title "Sprint planning" --merge-into …

The transcript path assumes a recording existed. On this team it usually did not: the PO
writes the notes by hand and posts them into Slack, so what needs ingesting is prose with
bullets in it — the same shape as the daily posts already in the corpus, and the shape the
line-level blocker reader is built for.

**One paste is one record, not one record per line.** That is the whole design decision
here, and it is not laziness: a note posted into Slack *is* one message, so splitting it
would model something that never happened, break the clustering that treats a post as a
unit, and produce a timeline of fragments nobody wrote. Everything that needs the lines
already reads them inside a record — see `ingest/blockers.py`, which finds 25 blocker lines
across 17 multi-line posts precisely because the lines stayed together.

The id is a content hash, so pasting a corrected version of the same note replaces it
rather than adding a near-duplicate that competes with it in every ranking.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tam.core import DEFAULT_RECORDS, read_records, write_records

log = logging.getLogger("notes")

#: Shorter than this and there is nothing to cluster on, so it would join topics by
#: accident. Matches the floor `prepare_messages` applies to Slack text.
MIN_NOTE_CHARS = 20


@dataclass(frozen=True)
class Note:
    """One pasted note, ready to become a record."""

    text: str
    title: str = ""
    author: str = ""
    when: datetime | None = None

    def record_id(self) -> str:
        """Stable across pastes of the same text, so a re-paste replaces.

        The date is in the hash and the title is not: fixing a typo in the title should
        not orphan the note and leave two, while the same words on a different day are
        genuinely a different note.
        """
        day = (self.when or datetime.now(tz=timezone.utc)).strftime("%Y-%m-%d")
        digest = hashlib.sha1(f"{day}\n{self.text.strip()}".encode("utf-8")).hexdigest()[:10]
        return f"note_{digest}"

    def as_record(self) -> dict[str, Any]:
        moment = self.when or datetime.now(tz=timezone.utc)
        body = self.text.strip()
        # The title is prepended rather than kept beside the text so retrieval and
        # clustering can see it. A field nothing reads is a field that does not exist.
        # Skipped when the paste already opens with its own bold heading, which the real
        # notes do — otherwise the record starts with two headings saying the same thing.
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        has_heading = first.startswith("*") and first.endswith("*") and len(first) > 2
        if self.title.strip() and not has_heading:
            body = f"*{self.title.strip()}*\n{body}"
        return {
            "id": self.record_id(),
            "text": body,
            "user": self.author.strip(),
            "ts": moment.timestamp(),
            "source": "note",
            "thread_ts": "",
            "channel_id": "",
            "note_title": self.title.strip(),
        }


def to_record(text: str, *, title: str = "", author: str = "", when: datetime | None = None) -> dict[str, Any]:
    """One record from one paste, or a ValueError saying why not."""
    if len(str(text or "").strip()) < MIN_NOTE_CHARS:
        raise ValueError(f"ต้องมีข้อความอย่างน้อย {MIN_NOTE_CHARS} ตัวอักษร ถึงจะจัดกลุ่มได้")
    return Note(text=str(text), title=title, author=author, when=when).as_record()


def merge_into(record: dict[str, Any], path: Path) -> tuple[int, bool]:
    """Add or replace this note in the corpus. Returns (corpus size, replaced?)."""
    existing = read_records(path, include_threads=True) if path.exists() else []
    kept = [one for one in existing if str(one.get("id")) != str(record["id"])]
    replaced = len(kept) != len(existing)
    kept.append(record)
    write_records(path, kept)
    return len(kept), replaced


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, help="File to read; omit to read stdin")
    parser.add_argument("--title", default="", help="What the note is about")
    parser.add_argument("--author", default="", help="Slack id or name of whoever wrote it")
    parser.add_argument("--when", default="", help="ISO timestamp; defaults to now")
    parser.add_argument("--merge-into", type=Path, help=f"Corpus to add to (e.g. {DEFAULT_RECORDS})")
    parser.add_argument("--json", action="store_true", help="Print the record instead of merging")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    text = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
    when = None
    if args.when.strip():
        try:
            when = datetime.fromisoformat(args.when.strip())
        except ValueError:
            raise SystemExit(f"--when {args.when!r} is not an ISO timestamp (try 2026-08-19T09:30)")
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)
    try:
        record = to_record(text, title=args.title, author=args.author, when=when)
    except ValueError as error:
        raise SystemExit(str(error))
    if args.json or not args.merge_into:
        print(json.dumps(record, ensure_ascii=False, indent=2))
        if not args.merge_into:
            print("\n(ไม่ได้ใส่ --merge-into จึงยังไม่ได้เพิ่มเข้า corpus)", file=sys.stderr)
        return
    total, replaced = merge_into(record, args.merge_into)
    log.info("%s note %s — corpus มี %d record", "แทนที่" if replaced else "เพิ่ม", record["id"], total)
    print("อย่าลืม rebuild: curl -X POST -H 'X-TAM-Token: <token>' http://127.0.0.1:8899/api/reindex")


if __name__ == "__main__":
    main()
