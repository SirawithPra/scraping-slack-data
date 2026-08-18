"""Read the standup form the team already fills in.

Every cue list in this project guesses at intent from free prose. A standup template
does not need guessing: when somebody types something under "Are there any
blockers?", they have declared it a blocker themselves. That is the highest-quality
signal in the corpus and the only one with a human label attached.

Measured on a real 936-message export, 19 posts carry the form and 5 have something
under the blockers heading; 3 of those are substantive. Of those 3, **only one
contains any word from the existing blocked_by cue list** — the other two are
ordinary sentences like a dependency on another team's requirements, with no keyword
surface at all. So this is not a better keyword list, it is a different kind of
evidence, and it finds what no keyword can.

Two things make it worth writing carefully rather than grepping for "blocker":

* **The heading is not the answer.** The literal string "blockers" occurs 22 times
  in the corpus and 14 of those are the question itself. A rule that fires on the
  word marks every standup post as blocked.
* **The form has four fields, not three.** After the blockers question comes
  "Others", and the first version of this parser read `• Others` as the blocker
  content for every post — eight characters that looked like a real answer in the
  measurements and were the next heading.

Headings vary in wording and bullet style across the corpus, so they are matched by
shape. `filled` distinguishes a declared blocker from the far more common "-" or
"None." — an explicitly empty slot is a real answer and must not read as one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

#: Every heading the form uses, in any of the bullet and emphasis styles seen. Matched
#: as a whole line: a heading is a heading, and a sentence that merely mentions
#: "blockers" is content.
HEADING = re.compile(
    r"""^[\s•*\-–—]*\**\s*(?:
          what\ did\ .*yesterday          # • What did accomplish yesterday?
        | progress\ on\ pending\ task     # a variant of the same slot
        | what\ are\ you\ working\ on\ today
        | are\ there\ any\ blockers
        | blockers?
        | others
        | upcoming\ event.*
        | timeline
    )\s*\**\s*\??\s*:?\s*$""",
    re.I | re.X,
)

#: An explicitly empty answer. "-" and "None." are answers, and treating them as
#: blocker text would mark almost every standup post as blocked: 14 of the 19 posts
#: with the form answer the blockers question this way.
EMPTY_ANSWER = re.compile(r"^[\s•*\-–—.]*(?:n/?a|none|nope|no|ไม่มี|ยังไม่มี|ok|okay)?[\s.]*$", re.I)

#: Below this many characters an answer is punctuation or an acknowledgement rather
#: than a described obstacle. Not tuned: on the real export every threshold from 10 to
#: 20 gives the same three answers, so the number is not doing hidden work.
MIN_ANSWER_CHARS = 15


def slot_of(heading: str) -> str:
    """Which field a heading line opens."""
    if re.search(r"yesterday|pending task", heading, re.I):
        return "yesterday"
    if re.search(r"today", heading, re.I):
        return "today"
    if re.search(r"blocker", heading, re.I):
        return "blockers"
    return "other"


@dataclass
class Standup:
    """One filled-in standup form."""

    record_id: str
    user: str
    ts: float
    yesterday: list[str] = field(default_factory=list)
    today: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def declared_blockers(self) -> list[str]:
        """Answers under the blockers heading that describe an obstacle.

        A person wrote these in the slot that asks for blockers, so they need no cue
        and no inference — which is the whole point of reading the form.
        """
        return [
            answer
            for answer in self.blockers
            if not EMPTY_ANSWER.match(answer) and len(answer.strip(" •*-–—")) >= MIN_ANSWER_CHARS
        ]

    @property
    def says_no_blockers(self) -> bool:
        """The slot was answered, and the answer was "none".

        Distinguished from an unfilled form on purpose: "no blockers today" is a
        statement worth trusting, and treating it the same as silence throws it away.
        """
        return bool(self.blockers) and not self.declared_blockers


def parse(text: str) -> dict[str, list[str]]:
    """Split one message into the form's fields. Empty when it is not a standup post."""
    slots: dict[str, list[str]] = {}
    current: str | None = None
    for line in str(text).splitlines():
        stripped = line.strip()
        if HEADING.match(stripped):
            current = slot_of(stripped)
            slots.setdefault(current, [])
        elif current and stripped:
            slots[current].append(re.sub(r"\s+", " ", stripped))
    return slots


def is_standup(text: str) -> bool:
    """Whether a message is a filled-in form rather than prose mentioning blockers.

    Requires the blockers heading *and* one other, so a message that merely asks
    "any blockers?" in conversation does not qualify.
    """
    slots = parse(text)
    return "blockers" in slots and len(slots) >= 2


def declared_blockers(records: Sequence[dict[str, Any]]) -> list[tuple[dict[str, Any], list[str]]]:
    """Every self-declared blocker, whether or not the full form was used.

    Deliberately looser than `standups`, and the difference is not academic: one of
    the three substantive blockers on the real export sits under a bare "Blockers:"
    heading with no other field, so requiring the whole form discards it. Somebody who
    writes "Blockers:" and then describes an obstacle has declared one exactly as
    clearly as somebody filling in all four questions.

    Returns (record, answers) so the caller keeps the message the claim came from —
    every state this project reports has to name the message that proves it.
    """
    found: list[tuple[dict[str, Any], list[str]]] = []
    for record in records:
        slots = parse(str(record.get("text", "")))
        if "blockers" not in slots:
            continue
        answers = [
            answer
            for answer in slots["blockers"]
            if not EMPTY_ANSWER.match(answer) and len(answer.strip(" •*-–—")) >= MIN_ANSWER_CHARS
        ]
        if answers:
            found.append((record, answers))
    return found


def cleared_blockers(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every message whose blockers slot was answered, and answered "none".

    This is the counter-evidence to `declared_blockers`, and it has to come from the
    same person rather than from anywhere in the cluster. Measured on the real export:
    all three declared blockers sit in one 71-message, 69-day topic that a `closed`
    cue elsewhere marks resolved, so a cluster-wide "something finished later" test
    silently retires every declaration in it — including "waiting for clearing user on
    dev", which nobody had cleared. Whoever wrote a blocker is the one who gets to say
    it is gone.

    Kept as loose as `declared_blockers` on purpose: a bare "Blockers: -" is as clear a
    statement as the full four-question form.
    """
    found: list[dict[str, Any]] = []
    for record in records:
        slots = parse(str(record.get("text", "")))
        answered = slots.get("blockers")
        if not answered:
            continue
        substantive = [
            answer
            for answer in answered
            if not EMPTY_ANSWER.match(answer) and len(answer.strip(" •*-–—")) >= MIN_ANSWER_CHARS
        ]
        if not substantive:
            found.append(record)
    return found


def standups(records: Sequence[dict[str, Any]]) -> list[Standup]:
    """Every standup form in a corpus, oldest first."""
    found: list[Standup] = []
    for record in records:
        text = str(record.get("text", ""))
        if not is_standup(text):
            continue
        slots = parse(text)
        try:
            ts = float(record.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        found.append(
            Standup(
                record_id=str(record.get("id", "")),
                user=str(record.get("user") or ""),
                ts=ts,
                yesterday=slots.get("yesterday", []),
                today=slots.get("today", []),
                blockers=slots.get("blockers", []),
            )
        )
    return sorted(found, key=lambda s: s.ts)
