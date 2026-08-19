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

from tam.analysis.relations import TYPES_BY_NAME, cue_offset
from tam.ingest.quoted import asserted_lines

#: Waiting expressions, matched as whole words — Latin with boundaries, Thai by
#: tokenising both sides (`relations.cue_offset`). `pending` earns its place here and not
#: in the message-level list precisely because a line is small enough to attribute.
LINE_CUES: tuple[str, ...] = (
    "pending", "blocked", "waiting on", "waiting for", "on hold", "still waiting",
    "รออยู่", "ยังรอ", "ต้องรอ", "ยังไม่มา", "ยังทำไม่ได้",
    # The same declaration with one word inserted. The tokeniser splits
    # `ยังทำต่อไม่ได้` into `[ยัง, ทำ, ต่อ, ไม่, ได้]`, so the cue above cannot reach it.
    # Measured on the real corpus it adds exactly one line — `อันนั้นยังติดเรื่อง clearing
    # user on dev ครับ data ไม่สะอาด รอ ops เคลียร์ให้ก่อน ยังทำต่อไม่ได้` — and that is a
    # blocker by any reading. §7.11 of docs/EXPERIMENTS.md has the cues measured beside it
    # and rejected, including bare `รอ`, which is still wrong but no longer for the reason
    # §7.1 gave.
    "ยังทำต่อไม่ได้",
    "ติดที่",
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


#: Words that turn a sentence around. `เสร็จแล้วครับ แต่ Pending p'choke` finishes one
#: thing and is stuck on another; without the `แต่` the same two cues mean the opposite.
#: Completion vocabulary, borrowed from the relation typer rather than restated, so a
#: cue added there is understood here too.
DONE_CUES = TYPES_BY_NAME["resolves"].cues

CONTRAST = ("but", "however", "แต่")


def completion_governs(line: str, waiting_cue: str) -> bool:
    """True when the line's leading clause reports completion, not waiting.

    `P'Mos - Done all API pending + support` contains `pending`, and is a report that the
    pending APIs are done. The signal available is position: a line's first clause governs
    it, so a completion word ahead of the waiting cue disqualifies the line — unless a
    contrast word sits between them, which is the sentence turning.

    Stated plainly because it decides how far to trust this: the rule rests on two lines of
    the real corpus, one in each direction. `Done all API pending + support` is the line it
    exists to reject, and it was putting a 26-message work item on the blocked list on its
    own. `ฝั่ง API event ผมต่อเสร็จแล้วครับ แต่ Pending p'choke prepare api อีกตัวนึงอยู่` is
    the line a bare position rule would have cost, and it is a genuine blocker. That is
    thin evidence for a heuristic, so both directions are pinned in
    `tests/test_blocker_lines.py` rather than left to a future reading of this docstring —
    and the escape clause is deliberately three words long, not a general negation model.
    """
    lowered = line.lower()
    waiting_at = cue_offset(lowered, waiting_cue)
    if waiting_at < 0:
        return False
    done_at = min(
        (offset for offset in (cue_offset(lowered, cue) for cue in DONE_CUES) if offset >= 0),
        default=-1,
    )
    if done_at < 0 or done_at >= waiting_at:
        return False
    between = lowered[done_at:waiting_at]
    return not any(cue_offset(between, word) >= 0 for word in CONTRAST)


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
        if cue and completion_governs(body, cue):
            # The waiting word is real; what it is attached to is finished work.
            continue
        if cue:
            out.append(BlockerLine("", "", 0.0, body, cue, _named(body), False))
        elif under_waiting_heading:
            out.append(BlockerLine("", "", 0.0, body, "(หัวข้อ)", _named(body), True))
    return out


def blocker_lines(records: Iterable[dict[str, Any]]) -> list[BlockerLine]:
    """Every waiting line in a corpus, each carrying the message it came from.

    Tracker records are skipped: an issue's description is not somebody reporting that
    they are stuck, and its own summary would match these cues constantly.

    Read from the author's own lines, not raw `text`. `quoted.py` exists because a cue
    inside pasted material marked a work item `resolved` when a customer had written the
    word, and reading `text` here reintroduced that hole one granularity down. It was not
    hypothetical: `` `/meowtam recall` ,`/meowtam blocked` , `/meowtam silent` ดูสิว่า…``
    put a work item at the top of the standup as *blocked*, because `blocked` is the name
    of a slash command somebody was testing. Inline code is exactly the region
    `asserted_lines` removes. Measured on the 1,517-record corpus the change costs
    nothing else: 30 lines become 29, and the one that goes is that one.
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
        for line in lines_of("\n".join(asserted_lines(str(record.get("text", ""))))):
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
