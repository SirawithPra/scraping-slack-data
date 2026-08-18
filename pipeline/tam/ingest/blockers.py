"""Read blockers at the line, because that is the size this team writes them.

Every other blocker signal in this project works on a whole message: a cue anywhere in
the text types the message, and the message types its topic. Measured on the real export,
that is the wrong unit. Messages containing `pending` have a median length of 529
characters against the corpus median of 46, and 14 of 15 are multi-line — the team writes
blockers as one bullet inside a long daily or recap post.

Adding `pending` to the message-level cue list proved it: the blocked count rose from 2 to
3, and the new item's evidence was a post beginning "Sprint 3 is officially end *RECAP*".
The cue was real and the attribution was nonsense, because one line had decided a post of
thirty. So this module returns lines, and the line is the evidence.

Three shapes appear in the real posts, and all three are handled:

    • Pending add ui - หน้า Error case user redeem      leading state on a bullet
    *Pending task*                                     a section, the bullets under it
    • additional fix [PROJ-110] and pending deploy     the cue inside a work line

What it deliberately does not do is decide a topic's state on its own — see
`analysis/digest.apply_declarations` for the rule that a person's own words outrank an
inferred state, and `docs/EXPERIMENTS.md` §7.1 for why this exists at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from tam.analysis.relations import cue_offset

#: Waiting expressions, matched as whole words — Latin with boundaries, Thai by
#: tokenising both sides (`relations.cue_offset`). `pending` earns its place here and not
#: in the message-level list precisely because a line is small enough to attribute.
LINE_CUES: tuple[str, ...] = (
    "pending", "blocked", "waiting on", "waiting for", "on hold", "still waiting",
    "รออยู่", "ยังรอ", "ต้องรอ", "ยังไม่มา", "ยังทำไม่ได้", "ติดที่",
)
#: Tried and removed: `ยังไม่ได้` ("hasn't yet"). It fired five times and not one was a
#: blocker — they were things not started ("fe mobile ผมยังไม่ได้ดู", "address ยังไม่ได้เขียน
#: การ์ด"), one was struck through, and one was praise for a release ("แอปใหม่ยังไม่ได้ลอง
#: ครบทุกอย่างแต่ดูดีนะ โหลดเร็ว หน้าตาสวย"). Not-done and blocked are different states, and
#: conflating them would report the whole backlog as obstacles.

#: A bullet, in the several forms Slack renders. Stripped before matching so a cue at the
#: start of an item is found at offset 0 rather than after the marker.
BULLET = re.compile(r"^[\s>]*[•◦‣·\-\*\+•]+\s*|^[\s>]*\d+[.)]\s*")

#: A bold section heading on a line of its own: `*Pending task*`. Slack's own markup, so
#: this is the author's structure rather than a guess about their prose.
HEADING = re.compile(r"^\s*\*+([^*]{2,60})\*+\s*:?\s*$")

#: A heading whose subject is waiting: everything under it is pending until the next one.
#: Kept separate from LINE_CUES because a heading names a section, not an obstacle.
WAITING_HEADING = re.compile(r"(pending|blocked|blocker|on hold|ค้าง|ติดอยู่|รอ)", re.I)

#: Under this, a line is a fragment rather than a statement of anything.
MIN_LINE_CHARS = 12

#: A question about a blocker is not a declaration of one. "Progress on pending task?" is
#: somebody chasing an update; reading it as an obstacle attributes the blocker to the
#: person asking. The same distinction the standup parser draws between the form's
#: question and its answer.
QUESTION = re.compile(r"[?？]\s*$")

#: Slack user mentions, so a blocker can name who is being waited on. The real posts do
#: this constantly — "insurance pending from mild", "P'@U0… update progress on rops".
MENTION = re.compile(r"<@([UW][A-Z0-9]{6,})>|@([UW][A-Z0-9]{6,})")


@dataclass(frozen=True)
class BlockerLine:
    """One line that says something is waiting, and where it came from."""

    record_id: str
    user: str
    ts: float
    line: str
    cue: str
    #: Slack ids named on the line. Empty is normal and means "not stated" — never guessed.
    waiting_on: tuple[str, ...] = ()
    #: True when the line inherited a `*Pending task*` heading rather than carrying a cue.
    from_heading: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "user": self.user,
            "ts": self.ts,
            "line": self.line,
            "cue": self.cue,
            "waiting_on": list(self.waiting_on),
            "from_heading": self.from_heading,
        }


def _strip(line: str) -> str:
    return BULLET.sub("", line).strip()


def _named(line: str) -> tuple[str, ...]:
    found = [a or b for a, b in MENTION.findall(line)]
    seen: list[str] = []
    for one in found:
        if one not in seen:
            seen.append(one)
    return tuple(seen)


def lines_of(text: str) -> list[BlockerLine]:
    """The waiting lines in one message's text, without the record metadata."""
    out: list[BlockerLine] = []
    under_waiting_heading = False
    for raw in str(text or "").splitlines():
        heading = HEADING.match(raw)
        if heading:
            # A heading changes what the lines below it mean, and is not itself a blocker.
            under_waiting_heading = bool(WAITING_HEADING.search(heading.group(1)))
            continue
        body = _strip(raw)
        if len(body) < MIN_LINE_CHARS:
            continue
        if QUESTION.search(body):
            continue
        lowered = body.lower()
        cue = next((one for one in LINE_CUES if cue_offset(lowered, one) >= 0), "")
        if cue:
            out.append(BlockerLine("", "", 0.0, body, cue, _named(body), False))
        elif under_waiting_heading:
            out.append(BlockerLine("", "", 0.0, body, "(หัวข้อ)", _named(body), True))
    return out


def blocker_lines(records: Iterable[dict[str, Any]]) -> list[BlockerLine]:
    """Every waiting line in a corpus, each carrying the message it came from.

    Tracker records are skipped: an issue's description is not somebody reporting that
    they are stuck, and its own summary would match these cues constantly.
    """
    found: list[BlockerLine] = []
    for record in records:
        if record.get("youtrack_key"):
            continue
        try:
            ts = float(record.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        rid = str(record.get("id", ""))
        user = str(record.get("user") or "")
        for line in lines_of(str(record.get("text", ""))):
            found.append(
                BlockerLine(rid, user, ts, line.line, line.cue, line.waiting_on, line.from_heading)
            )
    return found


def by_record(records: Sequence[dict[str, Any]]) -> dict[str, list[BlockerLine]]:
    """Waiting lines grouped by the record they came from."""
    grouped: dict[str, list[BlockerLine]] = {}
    for line in blocker_lines(records):
        grouped.setdefault(line.record_id, []).append(line)
    return grouped
