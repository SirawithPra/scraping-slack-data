"""Turn a meeting transcript into the same records Slack produces.

The point of this module is how little it does. A meeting is not a second
pipeline — it is another `source` in the record schema `prepare_messages.py`
already emits:

    {"id": ..., "ts": ..., "user": ..., "text": ..., "thread_ts": ..., "source": ...}

One utterance becomes one record; the whole meeting becomes one `thread_ts`.
From there every existing module works untouched: graph.py clusters the meeting's
topics, relations.py finds "this resolves what we discussed in Slack on Tuesday",
retrieve.py searches Slack and meetings in one ranking. A meeting is just a
conversation that happened out loud.

Three transcript shapes are accepted, because that is what the tools export:

* **WebVTT** (`.vtt`) — Zoom, Google Meet, Teams. Timestamps come free.
* **Speaker lines** (`.txt`) — ``Name: what they said``, one per line. Otter and
  most human note-takers.
* **JSON** — ``[{"speaker": ..., "start": 12.5, "text": ...}]`` from an ASR API.

Consecutive lines from one speaker are merged. Transcripts fragment a single
thought across five lines, and five fragments embed far worse than one sentence.

    python3 -m tam.ingest.meetings --transcript data/raw/standup.vtt --title "Daily 14 Aug"
    python3 -m tam.ingest.meetings --transcript notes.txt --merge-into data/processed/messages.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_OUTPUT = Path("data/processed/meeting_records.json")
# Utterances closer together than this from the same speaker are one thought.
MERGE_GAP_SECONDS = 45.0
# Below this, a line is "ครับ" / "yeah" / "mm-hm" — the transcript's own noise.
MIN_UTTERANCE_CHARS = 3

VTT_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)
# "Sirawith Chan:" or "<v Sirawith>" — the two ways transcripts mark a speaker.
SPEAKER_LINE_RE = re.compile(r"^\s*([^:<>\n]{1,40}?)\s*:\s*(.+)$")
VTT_VOICE_RE = re.compile(r"<v\s+([^>]+)>(.*?)(?:</v>)?$", re.IGNORECASE)
VTT_TAG_RE = re.compile(r"</?[^>]+>")
# WebVTT cue identifiers: a bare number, or Zoom's UUID-ish ids.
CUE_ID_RE = re.compile(r"^[\w-]{1,64}$")

log = logging.getLogger("meetings")


@dataclass
class Utterance:
    """One thing one person said, with an offset in seconds from meeting start."""

    speaker: str
    text: str
    offset: float


def parse_timestamp(value: str) -> datetime:
    """Meeting start time. Accepts ISO 8601, with or without a timezone."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SystemExit(f"--started {value!r} is not an ISO timestamp (try 2026-08-14T09:30).") from error
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _vtt_seconds(hours: str | None, minutes: str, seconds: str, fraction: str) -> float:
    return (
        3600.0 * float(hours or 0)
        + 60.0 * float(minutes)
        + float(seconds)
        + float(fraction) / (10 ** len(fraction))
    )


def parse_vtt(text: str) -> list[Utterance]:
    """WebVTT cues. The speaker is a ``<v Name>`` tag or a ``Name:`` prefix."""
    utterances: list[Utterance] = []
    offset = 0.0
    speaker = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        body = " ".join(part.strip() for part in buffer if part.strip())
        if body:
            utterances.append(Utterance(speaker or "unknown", body, offset))
        buffer = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            flush()
            continue
        match = VTT_TIME_RE.search(line)
        if match:
            flush()
            offset = _vtt_seconds(*match.groups()[:4])
            speaker = ""
            continue
        if not buffer and CUE_ID_RE.fullmatch(line) and "-->" not in line and " " not in line:
            continue  # cue identifier, not content
        voice = VTT_VOICE_RE.match(line)
        if voice:
            speaker = voice.group(1).strip()
            line = voice.group(2)
        elif not buffer:
            named = SPEAKER_LINE_RE.match(line)
            if named:
                speaker = named.group(1).strip()
                line = named.group(2)
        cleaned = VTT_TAG_RE.sub("", line).strip()
        if cleaned:
            buffer.append(cleaned)
    flush()
    return utterances


def parse_speaker_lines(text: str, *, seconds_per_line: float = 20.0) -> list[Utterance]:
    """``Name: what they said``, one per line.

    There are no timestamps in this format, so a nominal spacing is assigned.
    That is enough for ordering and for the time-decay signal, and it is recorded
    honestly rather than pretending to a precision the file does not have.
    """
    utterances: list[Utterance] = []
    speaker = "unknown"
    for position, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line:
            continue
        match = SPEAKER_LINE_RE.match(line)
        if match and not match.group(2).startswith("//"):
            speaker, line = match.group(1).strip(), match.group(2).strip()
        utterances.append(Utterance(speaker, line, position * seconds_per_line))
    return utterances


def parse_json(text: str) -> list[Utterance]:
    """``[{"speaker": ..., "start": 12.5, "text": ...}]`` from an ASR API."""
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as error:
        raise SystemExit(f"Transcript is not valid JSON: {error}") from error
    if not isinstance(rows, list):
        raise SystemExit("A JSON transcript should be a list of utterances.")
    utterances: list[Utterance] = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        body = str(row.get("text") or row.get("content") or "").strip()
        if not body:
            continue
        try:
            offset = float(row.get("start", row.get("offset", position * 20.0)))
        except (TypeError, ValueError):
            offset = position * 20.0
        utterances.append(Utterance(str(row.get("speaker") or row.get("user") or "unknown"), body, offset))
    return utterances


def parse_transcript(path: Path) -> list[Utterance]:
    """Pick a parser from the extension, falling back to content sniffing."""
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()
    if suffix == ".vtt" or text.lstrip().upper().startswith("WEBVTT"):
        return parse_vtt(text)
    if suffix == ".json" or text.lstrip().startswith("["):
        return parse_json(text)
    if suffix in {".srt"}:  # SRT differs from VTT only in the comma decimal separator
        return parse_vtt(text)
    return parse_speaker_lines(text)


def merge_utterances(utterances: Sequence[Utterance], *, gap: float = MERGE_GAP_SECONDS) -> list[Utterance]:
    """Join consecutive lines from the same speaker into one utterance.

    Transcripts break a single thought across several cues. Left alone, each
    fragment becomes its own record, each embeds badly, and the graph fills with
    edges between halves of the same sentence.
    """
    merged: list[Utterance] = []
    for utterance in utterances:
        if (
            merged
            and merged[-1].speaker == utterance.speaker
            and utterance.offset - merged[-1].offset <= gap
        ):
            merged[-1] = Utterance(
                merged[-1].speaker, f"{merged[-1].text} {utterance.text}".strip(), merged[-1].offset
            )
            continue
        merged.append(Utterance(utterance.speaker, utterance.text, utterance.offset))
    return merged


def meeting_slug(title: str, started: datetime) -> str:
    """Stable id stem, so re-importing the same meeting overwrites rather than duplicates."""
    cleaned = re.sub(r"[^A-Za-z0-9ก-๙]+", "-", title.strip()).strip("-").lower()
    return f"{started.strftime('%Y%m%d-%H%M')}-{cleaned or 'meeting'}"


def to_records(
    utterances: Iterable[Utterance], *, title: str, started: datetime, channel_id: str = "meeting"
) -> list[dict[str, Any]]:
    """Records in the exact shape prepare_messages.py emits.

    `thread_ts` is the meeting itself: every utterance shares it, so the whole
    meeting is one conversation to signals.py and one guaranteed-cohesive group
    to graph.py — the same guarantee a Slack thread gives.
    """
    from tam.ingest.prepare_messages import is_useful, normalize_text

    slug = meeting_slug(title, started)
    thread_ts = f"{started.timestamp():.6f}"
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    for utterance in utterances:
        text = normalize_text(utterance.text)
        if len(text) < MIN_UTTERANCE_CHARS or not is_useful(text):
            continue  # same noise filter as Slack: "ครับ", "yeah", "ok"
        ts = f"{started.timestamp() + utterance.offset:.6f}"
        record_id = f"mtg_{slug}_{ts}"
        if record_id in seen:
            continue
        seen.add(record_id)
        records.append(
            {
                "id": record_id,
                "channel_id": channel_id,
                "ts": ts,
                "thread_ts": thread_ts,
                "user": utterance.speaker,
                "text": text,
                "source": "meeting",
                "meeting_title": title,
            }
        )
    return records


def merge_into(records: Sequence[dict[str, Any]], path: Path) -> int:
    """Add the meeting to an existing corpus, replacing a previous import of it.

    Keyed on record id, which encodes the meeting slug — so re-importing a
    corrected transcript updates in place instead of doubling the meeting.
    """
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SystemExit(f"{path} is not valid JSON: {error}") from error
    incoming = {str(record["id"]) for record in records}
    slugs = {str(record["id"]).rsplit("_", 1)[0] for record in records}
    kept = [
        record
        for record in existing
        if str(record["id"]) not in incoming and str(record["id"]).rsplit("_", 1)[0] not in slugs
    ]
    replaced = len(existing) - len(kept)
    combined = kept + list(records)
    combined.sort(key=lambda record: str(record.get("ts", "")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    return replaced


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transcript", type=Path, required=True, help="Transcript file (.vtt, .srt, .txt or .json)")
    parser.add_argument("--title", help="Meeting title; defaults to the file name")
    parser.add_argument("--started", help="Meeting start, ISO 8601 (default: the file's modification time)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help=f"Write records here (default {DEFAULT_OUTPUT})")
    parser.add_argument("--merge-into", type=Path, help="Also merge into an existing corpus, replacing a prior import")
    parser.add_argument("--no-merge-speakers", action="store_true", help="Keep every transcript line as its own record")
    parser.add_argument("--channel-id", default="meeting", help="Channel id to record (default 'meeting')")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    if not args.transcript.exists():
        raise SystemExit(f"Missing {args.transcript}.")

    title = args.title or args.transcript.stem.replace("_", " ").replace("-", " ")
    started = (
        parse_timestamp(args.started)
        if args.started
        else datetime.fromtimestamp(args.transcript.stat().st_mtime, tz=timezone.utc)
    )

    utterances = parse_transcript(args.transcript)
    if not utterances:
        raise SystemExit(f"No utterances found in {args.transcript}. Check the format.")
    if not args.no_merge_speakers:
        before = len(utterances)
        utterances = merge_utterances(utterances)
        log.info("Merged %d transcript line(s) into %d utterance(s)", before, len(utterances))

    records = to_records(utterances, title=title, started=started, channel_id=args.channel_id)
    if not records:
        raise SystemExit("Every utterance was filtered as noise. Check the transcript.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    speakers = sorted({str(record["user"]) for record in records})
    log.info("Wrote %d record(s) from %d speaker(s) to %s", len(records), len(speakers), args.out)

    if args.merge_into:
        replaced = merge_into(records, args.merge_into)
        log.info("Merged into %s (replaced %d record(s) from a previous import)", args.merge_into, replaced)

    print(f"\nMeeting: {title}  ({started:%Y-%m-%d %H:%M})")
    print(f"Speakers: {', '.join(speakers)}")
    print(f"\nFirst few records:")
    for record in records[:4]:
        print(f"  [{record['user']}] {' '.join(str(record['text']).split())[:96]}")
    print(f"\nNow searchable with everything else:")
    print(f"  python3 -m tam.retrieval.retrieve --records {args.merge_into or args.out} -q \"...\"")
    print(f"  python3 -m tam.analysis.graph    --records {args.merge_into or args.out}")


if __name__ == "__main__":
    main()
