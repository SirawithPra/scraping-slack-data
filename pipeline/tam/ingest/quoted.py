"""Separate what a person wrote from what a person pasted.

A Slack message is not uniformly authored. Inside one message there can be a stack
trace, a block of app-store reviews someone copied in, a terms-and-conditions
document, a line struck through because it was retracted, and a quote of what
somebody else said last week. All of that is legitimately part of the message and a
reader needs to see it. None of it is the author making a claim.

Analysis that ignores the difference gets two things wrong, both measured on a real
936-message export:

* **State from text nobody asserted.** One work item read `resolved` because the
  cue `เรียบร้อย` matched inside a fenced block of quoted five-star reviews. The
  team had not resolved anything; a customer had written the word.
* **Clustering dominated by pasted bulk.** A single 6,191-character terms document
  is 5.5% of all characters in the corpus. Fenced blocks are another 6.0% and
  retracted text 6.2%. Embedding that as though it were conversation pulls unrelated
  messages together on shared boilerplate.

So records carry both. `text` stays exactly what Slack sent, because the dashboard,
the citations and the evidence links must show what was really said. `analysis_text`
is the same message with quoted, fenced and retracted regions removed, and is what
cue matching, embedding and BM25 should read. When a message is entirely pasted
material, `analysis_text` is empty — which is the honest answer to "what did this
person assert" and means the message can still be displayed while contributing
nothing to a state decision.

Authorship is the second axis. `is_bot` comes from Slack's own flag via the name
cache rather than from the id prefix: on this workspace all 146 bot-authored
messages — 27.6% of every character — have a `U…` id, so the usual
`user.startswith("B")` test catches exactly none of them. A deploy notification
saying "success" is not a teammate reporting that work is done.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Sequence

#: A fenced block. Non-greedy, and tolerant of the unclosed fence people leave when
#: they paste and then keep typing — an unterminated fence swallows the rest of the
#: message, which is the correct reading of what happened.
FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
#: Slack renders `~text~` as struck through. Somebody struck it because it stopped
#: being true, so treating it as an assertion inverts its meaning.
STRIKE = re.compile(r"~([^~\n]{2,})~")
#: A quoted line, Slack's `>` prefix. Also matches the HTML-escaped form, because
#: scraped exports arrive with `&gt;` where the API gives `>`.
QUOTE_LINE = re.compile(r"^[ \t]*(?:&gt;|>)[ \t]?.*$", re.M)
#: Inline code. Kept out of analysis for the same reason as a fence, but it is short
#: and often carries the identifier the message is about, so it is replaced by a
#: space rather than removed with its surroundings.
INLINE_CODE = re.compile(r"`[^`\n]+`")


def analysis_text(text: str) -> str:
    """The part of a message its author is asserting, for cue matching and embedding.

    Order matters: fences first, because a fence can contain `>` and `~` that are
    part of the pasted content rather than Slack markup.
    """
    stripped = FENCE.sub(" ", str(text))
    stripped = QUOTE_LINE.sub(" ", stripped)
    stripped = STRIKE.sub(" ", stripped)
    stripped = INLINE_CODE.sub(" ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def is_pasted(text: str) -> bool:
    """True when nothing the author asserted survives — the message is all quotation."""
    return not analysis_text(text)


def for_analysis(record: dict[str, Any]) -> str:
    """The text an analysis stage should read from one record.

    Falls back to `text` when the field is absent so a corpus prepared by an older
    version keeps working: a missing field means "not computed", never "empty".
    """
    if "analysis_text" in record:
        return str(record["analysis_text"])
    return analysis_text(str(record.get("text", "")))


def self_user() -> str:
    """This bot's own Slack user id, if configured.

    Meowtam posts a digest into channels it also reads, and that digest quotes item
    labels, evidence text and ticket keys. Left in the corpus it clusters with the very
    items it describes — measured, one such post produced a confident drift finding
    about the bot's own summary of the work.

    The exporter skips these going forward, but a corpus built before that keeps them:
    dropping a record from an export is invisible to an id-keyed merge, which can add
    and replace but not forget. So the filter is applied here too, on the way in, from
    an id stated outright rather than inferred from what an export stopped containing —
    the inference version had a hole exactly where it mattered, on a channel whose
    remaining messages were all filtered as noise.
    """
    return os.getenv("TAM_SELF_USER", "").strip()


def annotate(records: Sequence[dict[str, Any]], bots: set[str] | None = None) -> list[dict[str, Any]]:
    """Add `analysis_text` and `is_bot`, and drop what this bot wrote itself.

    `bots` is the set of user ids Slack marks as bots — read it from the name cache
    (`tam.ingest.users`), not from the id prefix, because on a real workspace every
    bot-authored message had a `U…` id and the prefix test caught none of them.

    Other bots are kept: a deploy notification is a real event worth clustering. Only
    this bot's own output is removed, because reading one's own summary of yesterday is
    not evidence of anything.
    """
    bots = bots or set()
    mine = self_user()
    out: list[dict[str, Any]] = []
    dropped = 0
    for record in records:
        if mine and str(record.get("user") or "") == mine:
            dropped += 1
            continue
        copy = dict(record)
        copy["analysis_text"] = analysis_text(str(record.get("text", "")))
        copy["is_bot"] = str(record.get("user") or "") in bots
        out.append(copy)
    if dropped:
        log.info("Dropped %d record(s) this bot posted itself (TAM_SELF_USER)", dropped)
    return out


def bot_ids(names: dict[str, str]) -> set[str]:
    """User ids Slack itself flags as bots, from the display-name cache.

    `tam.ingest.users.fetch_names` suffixes those names with "(bot)" precisely so this
    distinction survives into the corpus without a second API call.
    """
    return {user_id for user_id, name in names.items() if str(name).endswith("(bot)")}
