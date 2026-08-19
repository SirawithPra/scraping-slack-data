"""Turn a Slack conversation somebody copied out of the app into corpus records.

`export_slack` needs a token, a channel id, and an app that was invited. The
conversations that decide things often happen where none of that reaches: a DM, a
private group, another workspace, a phone. What a person can always do is select the
messages, press ⌘C, and paste — and this module reads that paste.

Slack's clipboard text is a real format, not free prose::

    image.png 6 repliesAim Sirawith  [2:21 PM]
    พี่มอสเคยแก้ให้ผมที่ dev
    [2:22 PM]ของผมสุดท้ายคือแตก
    [2:22 PM]400
    jah natta  [2:22 PM]
    ของพี่ไม่แตกหวะ

A name and a clock open a run of messages; a bare clock is the next message from the
same person; the body sits underneath or glued to the closing bracket. Around that
Slack pastes furniture — `6 replies`, an attachment's file name, `(edited)`, a
reaction row, a day separator — and all of it arrives welded to the line it precedes.

Four decisions here could each have gone the other way, so they are stated:

**Only a bracket that parses as a clock opens a message.** Real messages contain
brackets: ``REVERAPP-228 [BUG][View redemption result]The system-missing …`` is one
line of the paste this module was written from. Treating every ``[…]`` as a timestamp
would have split that sentence into three fake messages by a person called
"เรื่องคอลัม REVERAPP-228". `[BUG]` is not a time, so it stays body text.

**No `thread_ts`.** A copied scroll is not a thread — the ``6 replies`` marker proves
the replies were *not* copied. Giving the whole paste one `thread_ts` would tell
`graph.py` that fifteen unrelated messages are one guaranteed-cohesive topic, the
mistake `EdgeWeights.meeting_thread` exists to undo for meetings. The paste gets its
own `channel_id` instead, which binds it with the small same-channel weight: a nudge,
not a claim.

**A run of messages from one speaker becomes one record.** Chat is typed in
fragments: "ของผมสุดท้ายคือแตก" then "400" fifteen seconds later is one thought split
by the Enter key, and `400` alone is a record that embeds against nothing and would be
dropped as noise. Consecutive messages from the same person inside `MERGE_GAP_MINUTES`
are joined with newlines — newlines, not spaces, because `ingest.blockers` reads cues
line by line and a joined-with-spaces paragraph hides the line that says `รอ`.

**Ids are content hashes over (conversation, speaker, clock, text).** The real habit
is pasting an overlapping scroll — today's chat includes the tail of yesterday's — so
the same message will arrive twice. Hashing means the second paste replaces those
records instead of doubling them, and no one has to remember what they already pasted.

    python3 -m tam.ingest.slack_paste --file chat.txt --title "DM พี่ Natta" --date 2026-08-19 --dry-run
    pbpaste | python3 -m tam.ingest.slack_paste --title "DM พี่ Natta" --merge-into data/processed/real_all.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable, Sequence

from tam.core import TamDataError, validate_records, write_records

log = logging.getLogger("slack_paste")

DEFAULT_OUTPUT = Path("data/processed/paste_records.json")
#: Two messages from one person closer than this are one thought typed in pieces.
MERGE_GAP_MINUTES = 2.0
#: Same floor the Slack and meeting paths apply; `is_useful` still lets short code through.
MIN_MESSAGE_CHARS = 3
#: Longer than this in front of a clock is prose, not a display name.
MAX_SPEAKER_CHARS = 48

#: Any bracketed run short enough to be a timestamp. Whether it *is* one is decided by
#: `parse_clock`, which is what keeps `[BUG]` inside the sentence it belongs to.
TIME_BRACKET = re.compile(r"\[([^\[\]]{1,40})\]")
#: `2:21 PM`, `14:21`, `2:21:05 PM`, and the dated forms Slack uses in a copied thread
#: (`Today at 2:21 PM`, `Aug 19th at 2:21 PM`).
CLOCK = re.compile(
    r"^(?:(?P<day>.+?)\s+(?:at|เวลา)\s+)?(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    r"\s*(?P<meridiem>[ap]\.?\s?m\.?)?$",
    re.I,
)
#: `6 replies` glued to the next speaker's name. Everything up to and including it is
#: thread furniture, and the name starts immediately after with no separator.
REPLIES = re.compile(r"\d+\s*(?:replies|reply|การตอบกลับ|ตอบกลับ)", re.I)
#: An attachment's file name, pasted in front of the message that carried it.
FILE_PREFIX = re.compile(
    r"^\s*\S+\.(?:png|jpe?g|gif|webp|heic|pdf|mp4|mov|m4a|csv|xlsx?|docx?|pptx?|zip|txt|json|log|sql)\b"
    r"\s*(?:\([^)]*\))?\s*",
    re.I,
)
EDITED = re.compile(r"\s*\((?:edited|แก้ไขแล้ว)\)", re.I)
#: Slack's own chrome, when it lands on a line of its own.
NOISE_LINE = re.compile(
    r"^(?:\d+\s*(?:replies|reply|new\s+messages?|การตอบกลับ)"
    r"|view\s+(?:thread|newer\s+replies|older\s+replies|\d+\s+(?:more\s+)?replies?)"
    r"|last\s+reply\s+.*|show\s+more|added\s+an\s+integration|also\s+send\s+to\s+.*)$",
    re.I,
)
#: A reaction row: an emoji, or a `:shortcode:`, and a count.
REACTION = re.compile(r"^(?::[a-z0-9_+\-]+:|[^\w\s]{1,4})\s*\d{0,3}$", re.I)
#: A bot's badge, which Slack copies as part of the name line.
BADGE = re.compile(r"\s+(?:APP|BOT|บอท)$")
WEEKDAY = re.compile(r"^(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s*", re.I)

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTHS: dict[str, int] = {}
for _number, _name in enumerate(_MONTH_NAMES, start=1):
    MONTHS[_name] = _number
    MONTHS[_name[:3]] = _number
MONTHS["sept"] = 9

MONTH_FIRST = re.compile(r"^(?P<month>[a-z]+)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(?P<year>\d{4}))?$", re.I)
DAY_FIRST = re.compile(r"^(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>[a-z]+)\.?(?:,?\s*(?P<year>\d{4}))?$", re.I)


@dataclass(frozen=True)
class Stamp:
    """A clock read off one line, plus whatever day text was in front of it."""

    day_text: str
    hour: int
    minute: int
    second: int


@dataclass(frozen=True)
class Pasted:
    """One message as it was pasted: who, when, and what they wrote."""

    speaker: str
    when: datetime
    text: str


@dataclass
class PasteParse:
    """What the parser made of a paste, including what it could not place.

    `skipped` is not diagnostics for the log — it is shown to the person pasting.
    A parser this heuristic has to say what it dropped, or a mis-read paste looks
    exactly like a short conversation.
    """

    messages: list[Pasted] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def speakers(self) -> list[str]:
        return sorted({message.speaker for message in self.messages})


def parse_clock(content: str) -> Stamp | None:
    """A bracket's contents as a clock, or None if it is not one."""
    match = CLOCK.match(content.strip())
    if not match:
        return None
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    second = int(match.group("second") or 0)
    meridiem = (match.group("meridiem") or "").replace(".", "").replace(" ", "").lower()
    if minute > 59 or second > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem.startswith("p") else 0)
    elif hour > 23:
        return None
    return Stamp((match.group("day") or "").strip(), hour, minute, second)


def parse_day(text: str, *, base: date) -> date | None:
    """A day separator (`Today`, `Yesterday`, `Aug 18th`) as a date, relative to `base`."""
    cleaned = WEEKDAY.sub("", text.strip()).strip().strip(",").strip()
    lowered = cleaned.lower()
    if lowered in {"today", "วันนี้"}:
        return base
    if lowered in {"yesterday", "เมื่อวาน", "เมื่อวานนี้"}:
        return base - timedelta(days=1)
    for pattern in (MONTH_FIRST, DAY_FIRST):
        match = pattern.match(cleaned)
        if not match:
            continue
        month = MONTHS.get(match.group("month").lower())
        if not month:
            continue
        try:
            return date(int(match.group("year") or base.year), month, int(match.group("day")))
        except ValueError:
            return None
    return None


def find_stamp(line: str) -> tuple[str, Stamp, str] | None:
    """Split a line at its first bracket that really is a clock."""
    for match in TIME_BRACKET.finditer(line):
        stamp = parse_clock(match.group(1))
        if stamp:
            return line[: match.start()], stamp, line[match.end() :]
    return None


def speaker_from(before: str) -> str | None:
    """The display name in front of a clock.

    Three outcomes, and the difference matters: `None` means this line is not a
    message header at all (prose that happens to contain a time), `""` means the
    clock stands alone so the previous speaker is still talking, and a string is a
    new speaker.
    """
    if not before.strip():
        return ""
    if not before[-1].isspace():
        return None  # Slack always leaves a gap between the name and the bracket
    candidate = before
    replies = None
    for replies in REPLIES.finditer(candidate):
        pass  # the last one: `image.png 6 repliesAim Sirawith  `
    if replies:
        candidate = candidate[replies.end() :]
    while True:
        stripped = FILE_PREFIX.sub("", candidate)
        if stripped == candidate:
            break
        candidate = stripped
    candidate = BADGE.sub("", EDITED.sub("", candidate)).strip().strip("·|—–-").strip()
    if not candidate:
        return ""  # furniture only, e.g. an attachment above a message from the same person
    if len(candidate) > MAX_SPEAKER_CHARS or candidate.endswith((".", ",", "?", "!")):
        return None
    return candidate


def _at(day: date, stamp: Stamp, zone: tzinfo) -> datetime:
    return datetime(day.year, day.month, day.day, stamp.hour, stamp.minute, stamp.second, tzinfo=zone)


def parse_paste(text: str, *, day: date, tz: tzinfo | None = None) -> PasteParse:
    """Read a copied Slack conversation into messages.

    `day` is the date the conversation *starts*, because the clipboard carries clocks
    and no dates. When the clocks run backwards the paste crossed midnight, so the day
    advances — a separator line or a dated bracket overrides that guess.
    """
    zone = tz or datetime.now().astimezone().tzinfo or timezone.utc
    result = PasteParse()
    current_day = day
    speaker = ""
    when: datetime | None = None
    last: datetime | None = None
    body: list[str] = []

    def flush() -> None:
        nonlocal body
        joined = "\n".join(part for part in (line.strip() for line in body) if part).strip()
        body = []
        if not joined:
            return
        if when is None:  # text above the first header: nobody owns it, so say so
            result.skipped.append(joined)
            return
        result.messages.append(Pasted(speaker or "unknown", when, joined))

    for raw in text.splitlines():
        line = raw.strip()
        if not line or NOISE_LINE.match(line) or REACTION.match(line):
            continue
        found = find_stamp(line)
        if found is None:
            separator = parse_day(line, base=day)
            if separator is not None:
                flush()
                current_day = separator
                continue
            body.append(EDITED.sub("", line))
            continue
        before, stamp, after = found
        name = speaker_from(before)
        if name is None:
            body.append(EDITED.sub("", line))  # a sentence with a time in it, not a header
            continue
        flush()
        if stamp.day_text:
            dated = parse_day(stamp.day_text, base=day)
            if dated is not None:
                current_day = dated
        when = _at(current_day, stamp, zone)
        if last is not None and when < last:
            when += timedelta(days=1)
            current_day = when.date()
        last = when
        if name:
            speaker = name
        body = [EDITED.sub("", after)] if after.strip() else []
    flush()
    return result


def merge_runs(messages: Sequence[Pasted], *, gap_minutes: float = MERGE_GAP_MINUTES) -> list[Pasted]:
    """Join a run of messages from one person into the thought they were typing."""
    merged: list[Pasted] = []
    for message in messages:
        if (
            merged
            and merged[-1].speaker == message.speaker
            and (message.when - merged[-1].when).total_seconds() <= gap_minutes * 60
        ):
            merged[-1] = Pasted(merged[-1].speaker, merged[-1].when, f"{merged[-1].text}\n{message.text}")
            continue
        merged.append(message)
    return merged


def slug(text: str, *, limit: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9ก-๙]+", "-", text.strip()).strip("-").lower()
    return cleaned[:limit] or "paste"


def channel_for(title: str) -> str:
    """A channel id of this paste's own, never a real `C…` id it might collide with."""
    return f"paste-{slug(title)}"


def speaker_ids(names: dict[str, str] | None = None) -> dict[str, str]:
    """Display name → Slack id, for names the local cache can resolve unambiguously.

    A paste carries names where an export carries ids, so the same person would
    otherwise appear twice on the people page — once as `U0…`, once as "Aim Sirawith".
    Mapping back to the id joins them. A name two ids share is left unmapped: guessing
    which colleague said something is worse than showing the name as typed.
    """
    from tam.ingest.users import load_names

    try:
        cache = names if names is not None else load_names()
    except ValueError:  # an unreadable cache is not a reason to refuse the paste
        return {}
    seen: dict[str, list[str]] = {}
    for user_id, name in cache.items():
        seen.setdefault(str(name).strip().casefold(), []).append(str(user_id))
    return {name: ids[0] for name, ids in seen.items() if name and len(ids) == 1}


def to_records(
    messages: Iterable[Pasted],
    *,
    title: str,
    channel_id: str = "",
    names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Records in the shape `prepare_messages` emits, so every later stage just works."""
    from tam.ingest.prepare_messages import is_useful, normalize_text

    channel = channel_id or channel_for(title)
    people = speaker_ids(names)
    within_minute: dict[float, int] = {}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    for message in messages:
        text = normalize_text(message.text)
        if len(text) < MIN_MESSAGE_CHARS or not is_useful(text):
            continue  # same noise filter as Slack: "ครับ", "ok", a lone emoji
        # The clipboard has minute resolution, so several messages share one minute.
        # They are spread a second apart to keep the order they were pasted in, which
        # is the order they were sent — a tie would let sorting reshuffle a reply
        # above the message it answers.
        minute = message.when.replace(second=0, microsecond=0).timestamp()
        index = within_minute.get(minute, 0)
        within_minute[minute] = index + 1
        ts = minute + message.when.second + min(index, 59)
        user = people.get(message.speaker.casefold(), message.speaker)
        # The clock, not `ts`: the same message pasted again must hash the same even
        # when it lands at a different position inside the second paste.
        digest = hashlib.sha1(
            f"{channel}\n{user}\n{message.when:%Y-%m-%dT%H:%M}\n{text}".encode("utf-8")
        ).hexdigest()[:12]
        record_id = f"paste_{digest}"
        if record_id in seen:
            continue
        seen.add(record_id)
        record = {
            "id": record_id,
            "channel_id": channel,
            "ts": f"{ts:.6f}",
            "thread_ts": "",
            "user": user,
            "text": text,
            "source": "slack_paste",
            "paste_title": title,
        }
        if user != message.speaker:
            record["speaker_name"] = message.speaker  # what the paste said, kept beside the id
        records.append(record)
    return records


def merge_into(records: Sequence[dict[str, Any]], path: Path) -> tuple[int, int]:
    """Add these records to a corpus, replacing any earlier paste of the same messages.

    Keyed on id alone. Unlike the meeting path there is no "forget the previous import
    of this conversation" step: pastes overlap on purpose, and a second paste of a
    longer scroll must not delete the messages only the first one had.

    Reads the file directly rather than through `read_records`, which drops excluded
    channels — writing that filtered list back would erase them from disk.
    """
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise TamDataError(f"{path} is not valid JSON: {error}") from error
        existing = validate_records(path, parsed)
    incoming = {str(record["id"]) for record in records}
    kept = [record for record in existing if str(record["id"]) not in incoming]
    replaced = len(existing) - len(kept)
    combined = kept + list(records)
    combined.sort(key=lambda record: float(record.get("ts") or 0.0))
    write_records(path, combined)
    return len(combined), replaced


def read_paste(
    text: str,
    *,
    title: str,
    day: date,
    tz: tzinfo | None = None,
    channel_id: str = "",
    merge: bool = True,
    names: dict[str, str] | None = None,
) -> tuple[PasteParse, list[dict[str, Any]]]:
    """Paste in, (what was read, what will be stored) out. One call for CLI and web."""
    parse = parse_paste(text, day=day, tz=tz)
    messages = merge_runs(parse.messages) if merge else parse.messages
    return parse, to_records(messages, title=title, channel_id=channel_id, names=names)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise SystemExit(f"--date {value!r} is not a date (try 2026-08-19).") from error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, help="File holding the paste; omit to read stdin")
    parser.add_argument("--title", default="", help="What conversation this is, e.g. 'DM พี่ Natta'")
    parser.add_argument("--date", default="", help="The day the conversation starts (default: today)")
    parser.add_argument("--channel-id", default="", help=f"Channel id to record (default {channel_for('<title>')})")
    parser.add_argument("--no-merge", action="store_true", help="Keep every pasted message as its own record")
    parser.add_argument("--out", type=Path, help=f"Write the records here as well (e.g. {DEFAULT_OUTPUT})")
    parser.add_argument("--merge-into", type=Path, help="Corpus to add to, replacing messages pasted before")
    parser.add_argument("--dry-run", action="store_true", help="Show what was read and store nothing")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    text = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
    if not text.strip():
        raise SystemExit("ไม่มีข้อความให้อ่าน — วางบทสนทนาที่ copy จาก Slack มาทาง stdin หรือ --file")
    title = args.title.strip() or (args.file.stem.replace("_", " ") if args.file else "แชทที่วาง")
    day = parse_date(args.date) if args.date.strip() else datetime.now().astimezone().date()

    parse, records = read_paste(
        text, title=title, day=day, channel_id=args.channel_id, merge=not args.no_merge
    )
    if not parse.messages:
        raise SystemExit(
            "อ่านไม่เจอข้อความสักอัน — รูปแบบที่รองรับคือ 'ชื่อ  [2:21 PM]' แล้วขึ้นบรรทัดใหม่เป็นเนื้อความ"
        )

    print(f"\n{title}  ({day:%Y-%m-%d})")
    print(f"อ่านได้ {len(parse.messages)} ข้อความ จาก {len(parse.speakers)} คน → {len(records)} record")
    print(f"คน: {', '.join(parse.speakers)}\n")
    for record in records[:8]:
        when = datetime.fromtimestamp(float(record["ts"])).strftime("%d/%m %H:%M")
        body = " ".join(str(record["text"]).split())
        print(f"  {when}  [{record['user']}] {body[:88]}")
    if len(records) > 8:
        print(f"  … อีก {len(records) - 8} record")
    if parse.skipped:
        print(f"\nข้ามไป {len(parse.skipped)} ก้อน (อยู่ก่อนข้อความแรก หรือไม่มีเจ้าของ):")
        for line in parse.skipped[:3]:
            print(f"  - {' '.join(line.split())[:88]}")

    if args.dry_run:
        print("\n(--dry-run: ยังไม่ได้เขียนอะไรลงไฟล์)")
        return
    if args.out:
        write_records(args.out, records)
        log.info("Wrote %d record(s) to %s", len(records), args.out)
    if not args.merge_into:
        if not args.out:
            print("\n(ไม่ได้ใส่ --merge-into จึงยังไม่ได้เพิ่มเข้า corpus — เติม --merge-into data/processed/real_all.json)")
        return
    try:
        total, replaced = merge_into(records, args.merge_into)
    except TamDataError as error:
        raise SystemExit(str(error)) from error
    log.info("Merged into %s — corpus holds %d record(s), %d replaced", args.merge_into, total, replaced)
    print("อย่าลืม rebuild: curl -X POST -H 'X-TAM-Token: <token>' http://127.0.0.1:8899/api/reindex")


if __name__ == "__main__":
    main()
