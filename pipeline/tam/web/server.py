"""The prototype web app: daily digest, blockers, work-item timelines, grounding.

    python3 -m tam.web.server                 # http://127.0.0.1:8000
    python3 -m tam.web.server --records data/processed/combined.json --days 7

Seven pages, each backed by a JSON endpoint so a real frontend can replace the
HTML later without touching the logic:

    /                digest      — what moved, blocked items first
    /blockers        blockers    — only what is stuck, longest first
    /people          people      — who is on what, most-stuck people first
    /person/{user}   one person  — one person's items, messages and neighbours
    /tracker         tracker     — where the ticket board and Slack disagree
    /item/{key}      timeline    — one work item as dated, typed events
    /search?q=       grounding   — paste a meeting note, find the Slack behind it

Every number a tile states is a list somewhere below it. A tile that says 61 open
tickets and cannot show which 61 asks the reader to take the count on faith, which is
the one thing this project refuses to ask of anyone.

The audience is the whole team — product, design and QA as much as the people who
wrote the pipeline — so the pages say what a thing means and put how it was computed
behind a disclosure. The mechanism is not hidden, it is just not the first sentence.

`GET /api/health` is the one surface with no page: it reports the build being
served and whether the last refresh failed, because a failed refresh is otherwise
invisible — every page keeps serving the last good build as if it were current.

The expensive work — embedding, clustering, typing relations — is done once at
startup and cached in `State`. Only `/search` runs per request, because only it
depends on the query. Uploading a transcript invalidates the cache and rebuilds,
which is why the upload response is slow and a search is not.

The HTML reuses visualize.build_page, so the pages inherit the same validated
palette and typography as the offline reports rather than growing a second
design system.

Two routes change the corpus — `POST /upload` and `POST /api/reindex` — and an
injected record becomes a citation, which is the one thing this product treats as
proof. There is no login (this is a local prototype), so they are protected the
way a local tool can be, and a hostile page has to beat all three layers:

* the bind is loopback unless `--expose` says otherwise, and `--expose` refuses to
  start without `TAM_ADMIN_TOKEN` set;
* every write must carry the startup token, in the `X-TAM-Token` header or the
  upload form's hidden field. main() prints the one it generated;
* a write whose `Origin` is not this server's own host is refused, which is the
  exact shape of a drive-by cross-site form POST.

    curl -X POST -H "X-TAM-Token: $TAM_ADMIN_TOKEN" localhost:8000/api/reindex
"""

from __future__ import annotations

import argparse
import html
import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import quote, quote_plus, urlsplit

import numpy as np
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from tam.analysis.digest import DEFAULT_WINDOW_DAYS, Digest, Topic, build_digest, timeline, window_start
from tam.analysis.linker import load_overrides, save_overrides, unresolved_overrides
from tam.analysis.digest import names
from tam.retrieval.embeddings import model_name, quiet_third_party_logs
from tam.ingest.meetings import merge_into, merge_utterances, parse_iso, parse_transcript, to_records
from tam.retrieval.retrieve import DEFAULT_PRESET, build_retriever
from tam.core import DEFAULT_RECORDS, TamDataError, channel_projects, format_timestamp, read_records
from tam.analysis.summarize import TopicSummary, backend_name, summarize_digest
from tam.ingest.notes import merge_into as merge_note
from tam.ingest.notes import to_record as note_record
from tam.ingest.slack_paste import PasteParse
from tam.ingest.slack_paste import merge_into as merge_paste
from tam.ingest.slack_paste import read_paste
from tam.retrieval.signals import timestamp
from tam.report.visualize import build_page, cat, section, stat_tile

log = logging.getLogger("server")

#: Semantic colour is separate from the accent on purpose: "blocked" has to read as a
#: state, and if it borrows the paw coral that carries the brand then every heading looks
#: like a warning. These are CSS variables rather than hex so both themes resolve.
#: Thai only, and short. The chip is colour-coded and carries a title attribute with
#: the English name for whoever greps the analysis layer; the label itself is read by
#: product, design and QA, for whom "ติดอยู่ / blocked" is one word of noise per card.
STATE_STYLE = {
    "blocked": ("var(--state-blocked)", "ติดอยู่"),
    "resolved": ("var(--state-resolved)", "เสร็จแล้ว"),
    "active": ("var(--state-active)", "กำลังทำ"),
}


@dataclass
class State:
    """One whole build: the corpus and everything derived from it, plus its config.

    Never mutated in place after it is published. A summary's citations only mean
    anything against the records they were computed from, so a request must see one
    coherent build — never a new digest beside the previous build's summaries.
    """

    records_path: Path
    days: float = DEFAULT_WINDOW_DAYS
    language: str = "th"
    preset: str = "hybrid"
    records: list[dict[str, Any]] = field(default_factory=list)
    digest: Digest | None = None
    summaries: list[TopicSummary] = field(default_factory=list)
    retriever: Any = None
    built_at: str = ""  # local wall clock, for people reading the page
    built_at_iso: str = ""  # the same instant with its offset, for machines
    # About the last *attempt*, not this build: a refresh that failed leaves the
    # previous build serving, and without these nobody can tell that happened.
    last_attempt_at: str = ""
    last_error: str = ""
    # The ticket side, refreshed with the corpus. Best-effort: YouTrack being
    # unreachable must not stop a build, but it must not look like agreement either —
    # `tracker_error` is what /api/tracker reports instead of an empty list.
    drifts: list[dict[str, Any]] = field(default_factory=list)
    silent: list[dict[str, Any]] = field(default_factory=list)
    #: Every open ticket in the project, longest-untouched first. `silent` is this
    #: list's tail past the quiet threshold; keeping the whole thing is what lets the
    #: page show the "open tickets" tile as rows a reader can check.
    open_tickets: list[dict[str, Any]] = field(default_factory=list)
    #: The ticket side of every work item that names a ticket, keyed by upper-case
    #: ticket key. Counted for `tracker_coverage`; kept whole so the page can say
    #: *which* items matched and which of them anybody made comparable.
    tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    tracker_coverage: dict[str, int] = field(default_factory=dict)
    tracker_error: str = ""
    #: Human ticket links this build could not honour, because the record they name
    #: is not in the corpus. Carried on the build rather than logged, so the page can
    #: show the person who made them that they did nothing. See
    #: `linker.unresolved_overrides` for what puts a row here.
    unresolved_links: list[dict[str, str]] = field(default_factory=list)
    #: The corrections this build read, `{record_id: work_item_key}`. Kept beside the
    #: digest because a page has to be able to compare what a person asked for with
    #: what the clustering did — those two are not the same, and only one of them is
    #: visible anywhere else.
    link_overrides: dict[str, str] = field(default_factory=dict)

    def summary_for(self, key: int) -> TopicSummary | None:
        return next((summary for summary in self.summaries if summary.key == key), None)


state = State(records_path=DEFAULT_RECORDS)
app = FastAPI(title="Slack + meeting digest")


def overrides_path() -> Path:
    """Where the bot writes human ticket-link corrections.

    Same default and same env var as slack-bot/src/store.ts resolves, so the two
    halves agree on the file without either being configured. A missing file is
    normal — it only exists once someone has corrected something.
    """
    configured = os.environ.get("TAM_OVERRIDES_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_RECORDS.parent.parent / "link_overrides.json"


def read_overrides() -> dict[str, str]:
    """The corrections file, or nothing if it is mid-write.

    The bot's writer is not atomic, so a torn read is a normal event here. Failing
    the whole rebuild over it would take the dashboard down because someone clicked
    a menu item; the build proceeds without the corrections and says so, and the
    next reindex picks them up.
    """
    path = overrides_path()
    try:
        return load_overrides(path)
    except ValueError as error:
        log.warning("Building without human corrections: %s", error)
        return {}

# Reentrant on purpose: an upload holds this across merge_into *and* the rebuild
# that follows, and rebuild() takes it again from inside that.
_build_lock = threading.RLock()
_building = False


class Busy(RuntimeError):
    """A build is already running. The exception handler below turns this into 409."""


@contextmanager
def building() -> Iterator[None]:
    """One build at a time, and no corpus write while one is running.

    merge_into read-modify-writes the records file, so an upload has to hold this
    from before that read until after its rebuild, or two concurrent uploads lose
    a meeting between them.
    """
    global _building
    if not _build_lock.acquire(blocking=False):
        raise Busy("A rebuild is already running. Retry in a moment.")
    was_building = _building
    _building = True
    try:
        yield
    finally:
        _building = was_building
        _build_lock.release()


def ticket_facts(issue: Any) -> dict[str, Any]:
    """One ticket reduced to what a page and an API response need.

    A `State` is snapshotted per request and serialised by /api/tracker, so it holds
    plain dicts rather than the tracker client's dataclass — otherwise both the page and
    the JSON depend on the shape of a library that talks to YouTrack.
    """
    return {
        "ticket": issue.key,
        "summary": issue.summary,
        "state": issue.state,
        "url": issue.url,
        "resolved": bool(issue.resolved),
        # `updated` is an epoch and `updated_at` the same instant for reading. A page
        # that only gets the formatted string cannot sort or age it.
        "updated": issue.updated or None,
        "updated_at": format_timestamp(str(issue.updated)) if issue.updated else "",
    }


def open_ticket_rows(issues: Sequence[Any], records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every open ticket, longest-untouched first, with whether Slack ever named it.

    The tile counted these and no page could list them, so "61 open" was a number
    nobody could check. Idle days is the same clock `silent_tickets` uses, which makes
    the quiet section a visible tail of this table rather than a separate claim about
    the same tickets. A ticket with no `updated` still gets a row — a missing timestamp
    is a fact about the ticket, and dropping it would put a row count under the tile
    that disagrees with the tile.
    """
    now = datetime.now(tz=timezone.utc).timestamp()
    named = " ".join(str(record.get("text", "")) for record in records).upper()
    rows = [
        {
            **ticket_facts(issue),
            "idle_days": round((now - issue.updated) / 86400.0, 1) if issue.updated else None,
            "mentioned_in_slack": issue.key.upper() in named,
        }
        for issue in issues
        if not issue.resolved
    ]
    # An unknown age sorts last rather than first: it is not evidence of a fresh ticket.
    return sorted(rows, key=lambda row: float("inf") if row["idle_days"] is None else -as_days(row["idle_days"]))


def ticket_side(topics: Sequence[Any], issues: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Each fetched ticket, plus how often the conversation actually names it.

    The mention count is the reason a matched pair can produce no finding at all:
    `detect` compares an item against its ticket only when a person typed the key into
    the chat, so a page that shows the pairs has to show which of them anybody made
    comparable. Counted here, once per build, rather than per request.
    """
    from tam.analysis.drift import mentioning

    by_item = {str(topic.item_id).upper(): topic for topic in topics}
    side: dict[str, dict[str, Any]] = {}
    for issue in issues:
        key = issue.key.upper()
        topic = by_item.get(key)
        hits = mentioning(topic, issue.key) if topic is not None else []
        side[key] = {
            **ticket_facts(issue),
            "mentions": len(hits),
            "last_mention": format_timestamp(str(timestamp(hits[-1]))) if hits else "",
        }
    return side


#: What every tracker field looks like when the tracker could not be read. Spelled once
#: so a field added later cannot be missing from either failure return, where an absent
#: list reads as a finding of nothing rather than as "unknown".
NO_TRACKER: dict[str, Any] = {"drifts": [], "silent": [], "open_tickets": [], "tickets": {}, "coverage": {}}


def read_tracker(digest: Digest, records: list[dict[str, Any]]) -> dict[str, Any]:
    """The ticket side of the picture, or a recorded reason it is missing.

    Deliberately best-effort. YouTrack is a separate service that can be down, rate
    limited, or simply not configured, and none of that should stop a build of the Slack
    half — but an empty drift list must never be mistakable for "the two sources agree".
    So a failure is captured and reported to the caller, which is the same discipline the
    rest of this file uses for a failed refresh.
    """
    from tam.analysis.drift import coverage, detect, silent_tickets
    from tam.ingest.youtrack import YouTrackError, config, fetch_by_keys, fetch_project

    try:
        _, _, projects = config()
    except YouTrackError as error:
        return {**NO_TRACKER, "error": str(error)}
    try:
        keys = [topic.item_id for topic in digest.topics if not str(topic.item_id).startswith("c")]
        issues = fetch_by_keys(keys) if keys else []
        # Every configured project, not just the first. `YOUTRACK_PROJECTS` is plural and
        # a team with two of them was having the second silently excluded from "open
        # tickets" and from the silent-ticket list — which reads as a tracker where those
        # tickets do not exist, rather than as one this never looked at.
        every = [issue for project in projects for issue in fetch_project(project)]
        return {
            "drifts": [drift.as_dict() for drift in detect(digest.topics, issues)],
            "silent": [quiet.as_dict() for quiet in silent_tickets(every, records)],
            "open_tickets": open_ticket_rows(every, records),
            "tickets": ticket_side(digest.topics, issues),
            "coverage": {**coverage(digest.topics, issues), "tracker_issues": len(every),
                         "tracker_open": sum(1 for i in every if not i.resolved)},
            "error": "",
        }
    except YouTrackError as error:
        log.warning("Tracker unavailable; serving the Slack half only: %s", error)
        return {**NO_TRACKER, "error": str(error)}


def rebuild() -> State:
    """Build a new State from the corpus and publish it only if every step worked.

    Every step here can fail: read_records on a truncated file, build_retriever on a
    model that will not load, summarize_digest without credentials. Building into a
    local and rebinding `state` once at the end means a failure leaves the previous
    build serving, whole, with the failure recorded on it for /api/health.
    """
    global state
    with building():
        previous = state
        attempt = datetime.now(tz=timezone.utc).astimezone()
        stamp = attempt.strftime("%Y-%m-%d %H:%M")
        log.info("Building index from %s", previous.records_path)
        try:
            records = read_records(previous.records_path)  # TamDataError, not the CLI's SystemExit
            retriever = build_retriever(records, previous.preset)
            # The corrections the bot writes are the linker's top tier. Without this
            # they are written, confirmed to the person who filed them, and then
            # ignored by every path that person can actually see.
            overrides = read_overrides()
            # Search is one of those paths. It ranks text, so until the link is in the
            # index the message somebody linked to REV-250 is not findable by "REV-250"
            # — which is the state that made a correct link indistinguishable from none.
            retriever.index_links({
                record_id: key.split(":", 1)[1]
                for record_id, key in overrides.items()
                if key.startswith("ticket:")
            })
            unresolved = unresolved_overrides(records, overrides)
            if unresolved:
                log.warning("%d human link(s) name a record this corpus does not have", len(unresolved))
            digest = build_digest(records, since=window_start(previous.days), overrides=overrides)
            summaries = summarize_digest(digest, language=previous.language)
            tracker = read_tracker(digest, records)
        except BaseException as error:  # summarize_digest and build_retriever still exit like CLIs
            state = replace(previous, last_attempt_at=stamp, last_error=f"{type(error).__name__}: {error}")
            log.error("Rebuild failed; still serving the build from %s: %s", previous.built_at or "(nothing)", error)
            raise
        state = replace(
            previous,
            records=records,
            retriever=retriever,
            digest=digest,
            summaries=summaries,
            built_at=stamp,
            built_at_iso=attempt.isoformat(timespec="seconds"),
            drifts=tracker["drifts"],
            silent=tracker["silent"],
            open_tickets=tracker["open_tickets"],
            tickets=tracker["tickets"],
            tracker_coverage=tracker["coverage"],
            tracker_error=tracker["error"],
            unresolved_links=unresolved,
            link_overrides=overrides,
            last_attempt_at=stamp,
            last_error="",
        )
        log.info(
            "Ready: %d record(s), %d topic(s), %d blocked, summariser %s",
            len(records),
            len(digest.topics),
            len(digest.blocked),
            backend_name(),
        )
        return state


@app.exception_handler(Busy)
def busy_response(request: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"rebuilding": True, "detail": str(error)}, status_code=409)


# ---- write protection ------------------------------------------------------
#
# Generated per process so a fresh clone is safe with no configuration at all;
# set TAM_ADMIN_TOKEN to keep it stable across restarts, which a cron reindex or
# the bot needs. The upload form carries it in a hidden field, so the dashboard
# keeps working for whoever can see the page.
admin_token = secrets.token_urlsafe(24)


def check_origin(request: Request) -> None:
    """Refuse a cross-site write. Browsers always send Origin on POST; curl does not.

    Compared against this request's own Host header rather than the configured bind,
    so it holds however the operator reached the server (localhost, 127.0.0.1, a LAN
    name). A drive-by form POST is exactly the case where the two disagree.
    """
    origin = request.headers.get("origin", "")
    if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(status_code=403, detail=f"Cross-origin write from {origin} refused.")


def check_token(supplied: str) -> None:
    if not admin_token or not secrets.compare_digest(supplied, admin_token):
        raise HTTPException(
            status_code=403,
            detail="This route changes the corpus. Send the token the server printed at startup "
            "(TAM_ADMIN_TOKEN) as the X-TAM-Token header.",
        )


def require_admin(request: Request, x_tam_token: str = Header(default="")) -> None:
    """Dependency for the JSON write routes; /upload also accepts the form field."""
    check_origin(request)
    check_token(x_tam_token)


# ---- HTML fragments --------------------------------------------------------


def esc(value: Any) -> str:
    """Escape everything that reaches the page. Message text is untrusted input."""
    return html.escape(str(value))


#: Entity names, excluded from term highlighting: `terms` comes from the same tokeniser
#: the index uses, and a query for "amp" would otherwise rewrite the `&amp;` that
#: escaping just produced, turning safe output back into markup.
ENTITY_NAMES = frozenset({"amp", "lt", "gt", "quot", "x27", "39"})

#: What each source is called to a reader. "youtrack" is the tool's name, not the
#: thing — the people reading this page call it a ticket, so the page does too.
SOURCE_LABEL = {
    "slack": "Slack",
    "meeting": "ประชุม",
    "youtrack": "youtrack",
    "slack_thread": "เธรด",
    # Pasted out of a DM or a private group. Named apart from "Slack" because the reader
    # should know the difference: an exported message is whole, a pasted one is whatever
    # somebody selected.
    "slack_paste": "แชทที่วาง",
}


def highlight(text: str, terms: Sequence[str]) -> str:
    """Escaped text with the query's own matched terms marked.

    One pass over a single alternation rather than a substitution per term, so a match
    can never land inside a `<mark>` an earlier term inserted — which is how the naive
    loop produces nested tags and, for a term like "mark", corrupted ones.
    """
    wanted = {esc(term) for term in terms if len(term) > 1 and term.lower() not in ENTITY_NAMES}
    escaped = esc(text)
    if not wanted:
        return escaped
    pattern = re.compile("|".join(re.escape(term) for term in sorted(wanted, key=len, reverse=True)), re.IGNORECASE)
    return pattern.sub(lambda match: f"<mark>{match.group(0)}</mark>", escaped)


def clean(text: Any, limit: int) -> str:
    """One line of message text, name-resolved and cut to length."""
    single = names().in_text(" ".join(str(text).split()))
    return single if len(single) <= limit else single[: limit - 1] + "…"


def as_days(value: Any) -> float:
    """A stored duration back as a float, with "not known" surviving the round trip.

    `None` is what JSON can carry and NaN is what `human_age` and the sorts want, so the
    conversion lives in one place instead of at every call site.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def human_age(days: float) -> str:
    """A duration the way a person says it.

    "0.6 วัน" is a number this page computed, not a thing anyone says out loud, and
    half the people reading it are not engineers. Hours below a day, days below a
    month, months above.
    """
    if not np.isfinite(days):
        return "ไม่ทราบ"
    if days < 0.04:
        return "เพิ่งเมื่อกี้"
    if days < 1:
        return f"{days * 24:.0f} ชั่วโมง"
    if days < 2:
        return "1 วัน"
    if days < 30:
        return f"{days:.0f} วัน"
    return f"{days / 30:.0f} เดือน"


def age_phrase(topic: Any) -> str:
    """The age with the verb that makes it mean something for this state."""
    age = human_age(topic.age_days)
    if topic.state == "blocked":
        return f"ค้างมา {age}"
    if topic.state == "resolved":
        return f"ปิดไป {age}"
    return f"อัปเดตล่าสุด {age}ที่แล้ว" if age.endswith(("ชั่วโมง", "วัน", "เดือน")) else f"อัปเดต {age}"


def source_summary(topic: Any) -> str:
    """Where the messages came from, named the way the team names them."""
    return " · ".join(
        f"{SOURCE_LABEL.get(name, name)} {count}" for name, count in sorted(topic.sources.items(), key=lambda item: -item[1])
    )


def ticket_title(topic: Any) -> str:
    """The ticket's own summary line, if this item has a ticket in the corpus.

    A cluster is named by the words its messages share, which produces "format ka, rpg,
    api" — accurate, and unreadable to the product owner this page is also for. When the
    item is a ticket, the ticket already has a title a person wrote.
    """
    for record in topic.records:
        if str(record.get("youtrack_key") or "").upper() == str(topic.item_id).upper():
            first = str(record.get("text") or "").strip().splitlines()
            if first and first[0].strip():
                return first[0].strip()[:110]
    return ""


#: Phrases that are a whole message on their own and say nothing about the work.
#: Naming an item after one produced cards titled "โอเคครับ ขอบคุณครับๆๆ".
PLEASANTRIES = ("ขอบคุณ", "โอเค", "ok", "okay", "thanks", "thank you", "ครับ", "ค่ะ", "จ้า", "noted", "รับทราบ", "+1")


def first_human_line(topic: Any) -> str:
    """The earliest thing a person typed that is substantial enough to name the item by.

    Earliest-message-wins picks an acknowledgement about as often as it picks the
    request, because "โอเคครับ" is a message like any other. A line has to be long
    enough to carry a subject, and not be only politeness, before it can be a title.
    """
    written = sorted(
        (record for record in topic.records if str(record.get("source") or "slack") != "youtrack"),
        key=lambda record: timestamp(record) if np.isfinite(timestamp(record)) else float("inf"),
    )
    for record in written:
        line = clean(record.get("text", ""), 90)
        stripped = line.strip().lower()
        if len(stripped) < 18:
            continue
        if any(stripped.startswith(word) and len(stripped) < 40 for word in PLEASANTRIES):
            continue
        return line
    # Nothing substantial: the longest thing anyone said still beats a keyword list.
    return max((clean(record.get("text", ""), 90) for record in written), key=len, default="")


def card_title(topic: Any, summary: TopicSummary | None) -> str:
    """What to call this work item, in the order of how readable each option is.

    A generated headline when a model wrote one; the ticket's title when there is a
    ticket; otherwise the first thing a person typed. The keyword label is the last
    resort rather than the default, because it is the only one of the four that nobody
    outside the team that built this can read.
    """
    if summary and summary.backend not in ("template", "fake") and summary.headline:
        return summary.headline
    ticket = ticket_title(topic)
    if ticket:
        return f"{topic.item_id} · {ticket}" if not str(topic.item_id).startswith("c") else ticket
    return first_human_line(topic) or topic.label


def page_styles() -> str:
    """The dashboard's own layer on top of build_page's tokens.

    Everything reads a variable, so both themes resolve from one set of rules. Dark is the
    default because that is the design; light is behind an explicit stamp for a bright room.
    """
    return """<style>
  :root {
    --state-blocked:#F0866A; --state-resolved:#6FBFA4; --state-active:#E0A860;
    --wash-blocked:rgba(240,134,106,.07); --wash-resolved:rgba(111,191,164,.05);
  }
  :root[data-theme="light"] {
    --state-blocked:#C4553A; --state-resolved:#2F6455; --state-active:#95610F;
    --wash-blocked:rgba(196,85,58,.05); --wash-resolved:rgba(47,100,85,.04);
  }
  /* The filter hides cards by setting `hidden`, and every rule below that gives one a
     `display` would otherwise win against the user-agent default for that attribute. */
  [hidden] { display: none !important; }

  /* ---- app chrome ------------------------------------------------------------
     A sticky bar rather than a nav floating above the content: where you are, whether
     what you are reading is current, and the one control that changes that, all in the
     same place on every page. */
  .topbar { position: sticky; top: 0; z-index: 30; background: var(--page);
            border-bottom: 1px solid var(--line); }
  .topbar .bar { max-width: 1100px; margin: 0 auto; display: flex; align-items: center;
                 flex-wrap: wrap; gap: calc(var(--u)*4); padding: calc(var(--u)*3) calc(var(--u)*5); }
  .brand { display: inline-flex; align-items: center; gap: calc(var(--u)*2); flex: none;
           color: var(--ink); border: 0; font-weight: 650; font-size: .88rem; letter-spacing: -.01em; }
  .brand i { width: 15px; height: 15px; background: var(--accent);
             -webkit-mask: var(--paw) center/contain no-repeat; mask: var(--paw) center/contain no-repeat; }
  .brand:hover { color: var(--accent); }
  .topbar nav { display: flex; flex-wrap: wrap; gap: calc(var(--u)*4.5); flex: 1 1 auto; margin: 0;
                padding: 0; border: 0; font-size: .78rem; }
  .topbar nav a { color: var(--ink3); border: 0; border-bottom: 1px solid transparent;
                  padding-bottom: 2px; text-decoration: none; white-space: nowrap; }
  .topbar nav a:hover { color: var(--ink2); }
  .topbar nav a.on { color: var(--accent); border-bottom-color: var(--accent); }
  .bar-end { display: flex; align-items: center; gap: calc(var(--u)*3); flex: none; }
  .status { display: inline-flex; align-items: center; gap: calc(var(--u)*2); border: 0;
            font-size: .7rem; color: var(--ink3); text-decoration: none; }
  .status:hover { color: var(--ink2); }
  .status .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--good); flex: none; }
  .status.err .dot { background: var(--state-blocked); }
  .status.busy .dot { background: var(--warn); animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .2; } }
  @media (max-width: 780px) { .status .label { display: none; } .topbar nav { order: 3; flex-basis: 100%; } }

  /* A failed refresh is otherwise invisible: every page keeps serving the last good
     build as if it were current, so it says so where the reader cannot miss it. */
  .alert { border: 1px solid var(--state-blocked); border-left-width: 3px; border-radius: var(--r-lg);
           background: var(--wash-blocked); padding: calc(var(--u)*3.5) calc(var(--u)*4);
           margin: 0 0 calc(var(--u)*5); font-size: .86rem; color: var(--ink2); }
  .alert b { color: var(--ink); font-weight: 650; }
  .alert code { font-size: .8em; }

  /* ---- reading ---------------------------------------------------------------
     Body text is capped at a measure and given real contrast. The audience is the
     whole team, not the person who wrote the pipeline, and a 100-character line of
     low-contrast Thai at .82rem is where most of them stopped reading. */
  .lede { font-size: .88rem; color: var(--ink2); max-width: 74ch; line-height: 1.7; }
  .detail, .next, .hit .text, .msg span:last-child { max-width: 78ch; }

  /* ---- controls -------------------------------------------------------------- */
  .controls { display: flex; flex-wrap: wrap; gap: calc(var(--u)*2); align-items: center;
              margin: 0 0 calc(var(--u)*4); }
  .seg { display: inline-flex; border: 1px solid var(--line-2); border-radius: 999px;
         overflow: hidden; flex: none; }
  .seg button { border: 0; border-radius: 0; background: transparent; color: var(--ink3);
                padding: calc(var(--u)*2) calc(var(--u)*3.5); font-family: inherit;
                font-size: .76rem; font-weight: 600; letter-spacing: 0; text-transform: none;
                cursor: pointer; }
  .seg button + button { border-left: 1px solid var(--line-2); }
  .seg button:hover { color: var(--ink); background: var(--surface-2); }
  .seg button[aria-pressed="true"] { background: var(--accent-wash); color: var(--accent); }
  .seg button b { margin-left: 6px; opacity: .6; font-variant-numeric: tabular-nums;
                  font-family: var(--f-mono); font-size: .9em; }
  select { font-family: inherit; font-size: .76rem; color: var(--ink2); background: var(--page);
           border: 1px solid var(--line-2); border-radius: 999px; cursor: pointer;
           padding: calc(var(--u)*2) calc(var(--u)*2.5); }
  select:hover { color: var(--ink); border-color: var(--ink3); }
  .tally { margin-left: auto; font-family: var(--f-mono); font-size: .68rem; color: var(--ink3);
           letter-spacing: .06em; font-variant-numeric: tabular-nums; }
  input.filter { flex: 1 1 240px; min-width: 0; padding: calc(var(--u)*2) calc(var(--u)*3.5);
                 font-size: .82rem; border: 1px solid var(--line-2); border-radius: 999px;
                 background: var(--page); color: var(--ink); font-family: inherit; }

  /* ---- work item card --------------------------------------------------------
     State is the left rule, because it is the one thing worth seeing without reading.
     No paw here: the mark is on the brand and the heading, and 31 of them down a page
     is decoration where the rule is information. */
  .card { position: relative; background: var(--surface-2); border: 1px solid var(--line);
          border-left: 3px solid var(--line-2); border-radius: var(--r-lg);
          padding: calc(var(--u)*4.5) calc(var(--u)*5); margin: 0 0 calc(var(--u)*3);
          transition: border-color .14s ease, transform .14s ease; }
  .card:hover { border-color: var(--line-2); transform: translateX(1px); }
  .card.blocked { border-left-color: var(--state-blocked); background: var(--wash-blocked); }
  .card.resolved { border-left-color: var(--state-resolved); background: var(--wash-resolved); }
  .card.active { border-left-color: var(--state-active); }
  .card-head { display: flex; align-items: baseline; gap: calc(var(--u)*2.5); flex-wrap: wrap; }
  .card h3 { margin: 0; flex: 1 1 240px; font-size: 1rem; font-weight: 650;
             letter-spacing: -.012em; line-height: 1.45; }
  .card h3 a { color: var(--ink); border: 0; }
  .card h3 a:hover { color: var(--accent); }

  .chip { flex: none; font-size: .68rem; font-weight: 600; letter-spacing: .01em;
          padding: 2px 8px; border-radius: 10px; border: 1px solid currentColor; white-space: nowrap; }
  .chip.blocked { color: var(--state-blocked); }
  .chip.resolved { color: var(--state-resolved); }
  .chip.active { color: var(--state-active); }
  .tag { font-family: var(--f-mono); font-size: .64rem; padding: 1px 7px; border-radius: 999px;
         border: 1px solid var(--line-2); color: var(--ink3); white-space: nowrap; letter-spacing: .04em; }
  .keys { display: flex; flex-wrap: wrap; gap: calc(var(--u)*1.5); margin: calc(var(--u)*2.5) 0 0; }
  .keys span { font-size: .7rem; color: var(--ink3); background: var(--surface);
               border: 1px solid var(--line); border-radius: 10px; padding: 1px 8px; }

  /* One line of facts, dot-separated by rule rather than by hand, so an absent fact
     cannot leave a dangling separator behind it. */
  .facts { display: flex; flex-wrap: wrap; align-items: baseline; margin: calc(var(--u)*2) 0 0;
           font-size: .76rem; color: var(--ink3); }
  .facts > * + *::before { content: "·"; margin: 0 calc(var(--u)*2); color: var(--line-2); }
  .facts .hot { color: var(--state-blocked); font-weight: 600; }
  .meta { font-size: .78rem; color: var(--ink3); margin: calc(var(--u)*2) 0 0; }
  .meta.mono { font-family: var(--f-mono); font-size: .72rem; letter-spacing: .02em; }
  .detail { font-size: .88rem; color: var(--ink2); margin: calc(var(--u)*2.5) 0 0; line-height: 1.72; }
  .next { font-size: .88rem; color: var(--ink); margin: calc(var(--u)*3) 0 0;
          padding-left: calc(var(--u)*3); border-left: 2px solid var(--accent-dim); }
  .warn { color: var(--warn); font-size: .8rem; margin: calc(var(--u)*2.5) 0 0; }

  /* Evidence and the message trail are the proof, not the summary. Collapsed by default
     so a page of 31 items stays a page, one click from the thing that backs each claim. */
  details.more { margin: calc(var(--u)*3) 0 0; }
  details.more > summary { list-style: none; cursor: pointer; display: inline-flex; gap: 7px;
                           align-items: center; font-size: .76rem; color: var(--ink3); }
  details.more > summary::-webkit-details-marker { display: none; }
  details.more > summary::before { content: "+"; color: var(--accent); font-weight: 700;
                                   font-family: var(--f-mono); }
  details.more[open] > summary::before { content: "−"; }
  details.more > summary:hover { color: var(--accent); }

  .msg { display: grid; grid-template-columns: max-content 1fr; gap: calc(var(--u)*3);
         align-items: baseline; font-size: .85rem; color: var(--ink2);
         padding: calc(var(--u)*2.5) 0; border-top: 1px solid var(--line); }
  .msg .who { font-size: .74rem; color: var(--ink3); white-space: nowrap; }
  .msg .tag { margin-right: calc(var(--u)*1.5); }
  @media (max-width: 660px) { .msg { grid-template-columns: 1fr; gap: calc(var(--u)*1); } }
  .day { font-family: var(--f-mono); font-size: .68rem; letter-spacing: .1em; color: var(--ink3);
         margin: calc(var(--u)*5) 0 0; padding-bottom: calc(var(--u)*1);
         border-bottom: 1px solid var(--line-2); }
  .day:first-child { margin-top: 0; }

  /* ---- forms ----------------------------------------------------------------- */
  form.search { display: flex; flex-wrap: wrap; gap: calc(var(--u)*2); margin: 0 0 calc(var(--u)*4); }
  form.search input[type=text], textarea, .row input[type=text], .row input[type=datetime-local] {
      padding: calc(var(--u)*2.5) calc(var(--u)*3.5); font-size: .88rem;
      border: 1px solid var(--line-2); border-radius: var(--r-lg); background: var(--page);
      color: var(--ink); font-family: inherit; }
  form.search input[type=text] { flex: 1 1 260px; min-width: 0; }
  textarea { width: 100%; line-height: 1.7; resize: vertical; margin: 0 0 calc(var(--u)*3);
      font-family: inherit; font-size: .88rem; }
  ::placeholder { color: var(--ink3); }
  textarea:focus, input:focus, select:focus { border-color: var(--accent); outline: none; }
  .row { display: flex; flex-wrap: wrap; gap: calc(var(--u)*2); align-items: center; margin: 0 0 calc(var(--u)*3); }
  .row input[type=text], .row input[type=datetime-local] { flex: 1 1 170px; min-width: 0; }
  .row input[type=file] { flex: 1 1 240px; font-size: .8rem; color: var(--ink2); }
  .row button { flex: 0 0 auto; }
  label.field { display: flex; flex-direction: column; gap: calc(var(--u)*1.5); flex: 1 1 170px;
                font-size: .72rem; color: var(--ink3); }
  label.field input { width: 100%; }
  button { font-family: inherit; font-size: .8rem; font-weight: 650; letter-spacing: .01em;
      text-transform: none; padding: calc(var(--u)*2.5) calc(var(--u)*5);
      border: 1px solid var(--accent); border-radius: 999px;
      background: var(--accent); color: var(--on-accent); cursor: pointer; }
  button:hover { background: transparent; color: var(--accent); }
  button:disabled { opacity: .5; cursor: progress; }
  button.ghost { font-family: inherit; font-size: .74rem; font-weight: 600; letter-spacing: 0;
                 text-transform: none; }

  /* ---- search hits -----------------------------------------------------------
     Deliberately not the work-item card: a hit is a single message the ranker chose,
     and dressing it as an item implied a state and a timeline it does not have. */
  .hit { display: grid; grid-template-columns: 26px 1fr; gap: calc(var(--u)*3);
         padding: calc(var(--u)*4) 0; border-top: 1px solid var(--line); }
  .hit:first-of-type { border-top: 0; padding-top: 0; }
  .hit .n { font-family: var(--f-mono); font-size: .78rem; color: var(--ink3);
            font-variant-numeric: tabular-nums; }
  .hit .head { display: flex; flex-wrap: wrap; align-items: baseline; gap: calc(var(--u)*2); }
  .hit .text { font-size: .9rem; color: var(--ink); margin: calc(var(--u)*2) 0 0; line-height: 1.7; }
  mark { background: var(--accent-wash); color: var(--accent); border-radius: 2px; padding: 0 2px; }
  /* Every stage's contribution, because a fused score alone cannot say whether a hit
     was found by meaning or by the words the note actually used. Folded away: it is
     the answer to "why this one", which most readers never ask. */
  .why { display: flex; flex-wrap: wrap; gap: calc(var(--u)*4); margin: calc(var(--u)*2) 0 0; }
  .why > div { flex: 0 1 128px; min-width: 100px; }
  .why .k { display: flex; justify-content: space-between; gap: calc(var(--u)*2);
            font-size: .68rem; color: var(--ink3); font-variant-numeric: tabular-nums; }
  /* Named `.meter`, not `.bar`: the topbar's own wrapper is `.bar`, and a 3px-tall
     rule with `overflow: hidden` matching it collapsed the whole navigation to a
     sliver on every page. Two components, two names. */
  .meter { height: 3px; background: var(--line); border-radius: 2px; overflow: hidden; margin-top: 4px; }
  .meter i { display: block; height: 100%; background: var(--accent); border-radius: 2px; }
  .meter.dim i { background: var(--ink3); }

  /* ---- timeline --------------------------------------------------------------- */
  .tl { position: relative; margin: 0; padding: 0 0 0 26px; }
  .tl::before { content: ""; position: absolute; left: 4px; top: 8px; bottom: 8px;
                width: 1px; background: var(--line); }
  .tl .ev { position: relative; padding: 0 0 calc(var(--u)*5); }
  .tl .ev:last-child { padding-bottom: 0; }
  .tl .ev::before { content: ""; position: absolute; left: -26px; top: 7px; width: 9px; height: 9px;
                    border-radius: 50%; background: var(--page); border: 1.5px solid var(--accent-dim); }
  .tl .rel { font-size: .74rem; font-weight: 600; color: var(--good); }
  .tl .when { font-family: var(--f-mono); font-size: .7rem; color: var(--ink3);
              font-variant-numeric: tabular-nums; }
  .tl .head { display: flex; flex-wrap: wrap; align-items: baseline; gap: calc(var(--u)*3); }

  tbody tr:hover { background: var(--surface-2); }
  /* Six or seven columns of Thai cannot shrink to a phone, so a table scrolls inside
     its own box and the page body never moves sideways. */
  .wide { overflow-x: auto; }
  .wide table { min-width: var(--least, 560px); }
  /* Nothing to show is a moment, not a dead end — most of all on /blockers, where
     an empty list is the best outcome this product has and used to read as an error. */
  /* A tile's value is sized for a number. A name lands in one sometimes, and at
     2.1rem mono it wrapped to three lines and outgrew the tile beside it. */
  .tile-value { overflow-wrap: anywhere; }
  .tile-value.text { font-size: 1.15rem; line-height: 1.4; letter-spacing: -.01em; }
  .empty { display: flex; flex-direction: column; align-items: center; gap: calc(var(--u)*3);
           font-size: .84rem; color: var(--ink3); text-align: center;
           padding: calc(var(--u)*8) calc(var(--u)*4); border: 1px dashed var(--line-2);
           border-radius: var(--r-lg); }
  .empty p { margin: 0; }
  .legend { display: flex; flex-wrap: wrap; gap: calc(var(--u)*4); margin: 0 0 calc(var(--u)*4);
            font-size: .76rem; color: var(--ink3); }
  .legend span { display: inline-flex; align-items: center; gap: calc(var(--u)*2); }
  .legend i { width: 10px; height: 3px; border-radius: 2px; }
</style>"""


def app_scripts() -> str:
    """Client-side behaviour: filtering, sorting, build status, reindex, copy, shortcuts.

    All of it is view work over markup the server already sent, so none of it costs a
    round trip: filtering 31 rendered cards is a class change, where asking the server
    would rebuild nothing and wait for the network to say so. The one exception is the
    build status, which is the one fact the page cannot know without asking.
    """
    return """<script>
(function () {
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var all = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  // ---- list: filter, sort, copy ------------------------------------------------
  var list = $('[data-list]');
  if (list) {
    var cards = all('.card', list);
    var box = $('[data-filter]'), tally = $('[data-tally]'), sort = $('[data-sort]'), empty = $('[data-empty]');
    var state = '';
    var copy = $('[data-copy]');

    function apply() {
      var needle = (box && box.value || '').trim().toLowerCase();
      var shown = 0;
      cards.forEach(function (card) {
        var ok = (!state || card.dataset.state === state) &&
                 (!needle || card.dataset.text.indexOf(needle) !== -1);
        card.hidden = !ok;
        if (ok) shown++;
      });
      if (tally) tally.textContent = shown === cards.length ? cards.length + ' เรื่อง' : shown + ' จาก ' + cards.length + ' เรื่อง';
      if (empty) empty.hidden = shown !== 0;
    }

    all('[data-state-filter]').forEach(function (button) {
      button.addEventListener('click', function () {
        state = button.dataset.stateFilter;
        all('[data-state-filter]').forEach(function (other) {
          other.setAttribute('aria-pressed', String(other === button));
        });
        apply();
      });
    });
    if (box) box.addEventListener('input', apply);
    if (sort) sort.addEventListener('change', function () {
      var by = sort.value;
      cards.slice().sort(function (a, b) {
        if (by === 'recent') return Number(b.dataset.last) - Number(a.dataset.last);
        if (by === 'size') return Number(b.dataset.count) - Number(a.dataset.count);
        if (by === 'age') return Number(b.dataset.age) - Number(a.dataset.age);
        return Number(a.dataset.rank) - Number(b.dataset.rank);
      }).forEach(function (card) { list.appendChild(card); });
    });
    apply();

    // Copies what is on screen, not the whole build: the filter is how someone picks
    // the part of the digest they are about to paste into a standup.
    function fallback(text, done) {
      var area = document.createElement('textarea');
      area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
      document.body.appendChild(area); area.select();
      try { document.execCommand('copy'); done(); } catch (e) { copy.textContent = 'คัดลอกไม่ได้'; }
      document.body.removeChild(area);
    }
    if (copy) copy.addEventListener('click', function () {
      var text = cards.filter(function (c) { return !c.hidden; })
                      .map(function (c) { return c.dataset.copy; }).join('\\n');
      var done = function () {
        copy.textContent = 'คัดลอกแล้ว';
        setTimeout(function () { copy.textContent = 'คัดลอกรายการ'; }, 1500);
      };
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () { fallback(text, done); });
      else fallback(text, done);
    });
  }

  // ---- tables: filter rows ------------------------------------------------------
  // The same affordance as the card filter, for the pages whose content is a table.
  // Scoped to its own section so /tracker can carry three tables and each filters itself.
  all('[data-rows-filter]').forEach(function (box) {
    var scope = box.closest('section') || document;
    var rows = all('tbody tr', scope);
    var tally = $('[data-rows-tally]', scope);
    var unit = (tally && tally.dataset.unit) || '';
    function apply() {
      var needle = box.value.trim().toLowerCase();
      var shown = 0;
      rows.forEach(function (row) {
        var hay = row.dataset.text || row.textContent.toLowerCase();
        var ok = !needle || hay.indexOf(needle) !== -1;
        row.hidden = !ok;
        if (ok) shown++;
      });
      if (tally) tally.textContent = (shown === rows.length ? rows.length : shown + ' จาก ' + rows.length) + ' ' + unit;
    }
    box.addEventListener('input', apply);
    apply();
  });

  // ---- build status ------------------------------------------------------------
  var status = $('[data-status]');
  if (status) {
    var label = $('.label', status);
    setInterval(function () {
      if (document.visibilityState !== 'visible') return;
      fetch('/api/health').then(function (r) { return r.json(); }).then(function (health) {
        status.className = 'status ' + (health.rebuilding ? 'busy' : health.ok ? 'ok' : 'err');
        if (label) label.textContent = health.rebuilding ? 'กำลังอัปเดต'
          : health.last_error ? 'อัปเดตล้มเหลว' : 'ข้อมูล ' + health.built_at;
      }).catch(function () {
        status.className = 'status err';
        if (label) label.textContent = 'ต่อไม่ติด';
      });
    }, 20000);
  }

  // ---- reindex -----------------------------------------------------------------
  var reindex = $('[data-reindex]');
  if (reindex) reindex.addEventListener('click', function () {
    var restore = function (text) { reindex.textContent = text; reindex.disabled = false; };
    reindex.disabled = true; reindex.textContent = 'กำลังอัปเดต…';
    fetch('/api/reindex', { method: 'POST', headers: { 'X-TAM-Token': reindex.dataset.token } })
      .then(function (response) {
        if (response.status === 409) return restore('กำลังอัปเดตอยู่');
        if (!response.ok) return restore('อัปเดตไม่สำเร็จ');
        // Only this tab's own reindex reloads. Reloading off the poll would throw away
        // a half-typed query in every other tab the moment a cron refresh landed.
        location.reload();
      })
      .catch(function () { restore('อัปเดตไม่สำเร็จ'); });
  });

  // ---- shortcuts ----------------------------------------------------------------
  document.addEventListener('keydown', function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    var tag = (event.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      if (event.key === 'Escape') event.target.blur();
      return;
    }
    if (event.key === '/') {
      var target = $('[data-filter]') || $('[data-rows-filter]') || $('form.search input[type=text]');
      if (target) { event.preventDefault(); target.focus(); target.select(); }
    }
  });
})();
</script>"""


NAV_LINKS = (
    ("/", "สรุปงาน"),
    ("/blockers", "งานที่ติด"),
    ("/people", "รายคน"),
    ("/tracker", "เทียบกับทิกเก็ต"),
    ("/search", "ค้นหา"),
    ("/upload", "เพิ่มโน้ต"),
)


def chrome(current: str, build: State) -> str:
    """The sticky bar: where you are, how fresh this is, and how to refresh it.

    The reindex button carries the write token in an attribute. That is the same
    exposure the upload form already has — anything that can read this markup can read
    that page — and it is what keeps the dashboard usable for whoever can see it. The
    cross-origin check is the layer that matters here: a hostile page cannot read this
    response, so it cannot learn the token, and its blind POST is refused on Origin.
    """
    links = "".join(
        f'<a href="{path}" class="{"on" if path == current else ""}">{esc(label)}</a>'
        for path, label in NAV_LINKS
    )
    if build.digest is None:
        tone, label = "busy", "กำลังสร้าง"
    elif build.last_error:
        tone, label = "err", "อัปเดตล้มเหลว"
    else:
        tone, label = "ok", f"ข้อมูล {build.built_at}"
    return (
        '<header class="topbar"><div class="bar">'
        '<a class="brand" href="/"><i></i>meowtam</a>'
        f"<nav>{links}</nav>"
        '<div class="bar-end">'
        f'<a class="status {tone}" href="/api/health" data-status title="สถานะข้อมูลที่กำลังแสดง">'
        f'<span class="dot"></span><span class="label">{esc(label)}</span></a>'
        f'<button class="ghost" type="button" data-reindex data-token="{esc(admin_token)}" '
        'title="อ่านข้อมูลใหม่ทั้งหมดแล้วสรุปอีกครั้ง">อัปเดตข้อมูล</button>'
        "</div></div></header>"
    )


def stale_alert(build: State) -> str:
    """Say it on the page, not only in /api/health.

    A refresh can fail every night for a week and every page still renders the last good
    build with a plausible timestamp on it. This is the only thing on the page that can
    tell the reader that what they are about to act on is older than the corpus.
    """
    if not build.last_error:
        return ""
    return (
        f'<div class="alert"><b>อัปเดตข้อมูลไม่สำเร็จเมื่อ {esc(build.last_attempt_at)}</b><br>'
        f"สิ่งที่เห็นอยู่นี้เป็นข้อมูลของ {esc(build.built_at or '(ยังไม่มี)')} ซึ่งเก่ากว่าข้อความจริงที่เก็บไว้ "
        "อ่านได้ แต่อย่าเพิ่งถือเป็นสถานะล่าสุด<br>"
        f"<code>{esc(build.last_error[:200])}</code></div>"
    )


def how(text: str) -> str:
    """The technical explanation, folded away.

    This page is read by product, design and QA as well as by the people who wrote the
    pipeline. The mechanism used to be the first sentence of every section, which is
    the wrong order for five readers out of six — and deleting it would be the wrong
    answer for the sixth.
    """
    return f'<details class="more"><summary>ระบบหาเรื่องนี้มาได้ยังไง</summary><p class="meta">{esc(text)}</p></details>'


def render(
    title: str,
    tiles: list[str],
    sections: list[str],
    subtitle: str,
    *,
    current: str = "",
    build: State | None = None,
    actions: str = "",
    hero: str = "",
) -> HTMLResponse:
    """Every page goes out through here.

    The nav and this file's stylesheet used to be concatenated in *front* of what
    build_page returns, which put both ahead of the doctype: quirks mode, and a nav
    sitting outside the content column it belongs to. They are slots now.
    """
    build = build if build is not None else live()
    body = [stale_alert(build), *sections] if build.last_error else sections
    return HTMLResponse(
        build_page(
            title,
            tiles,
            body,
            subtitle,
            head=page_styles(),
            topbar=chrome(current, build),
            actions=actions,
            hero=hero,
            tail=app_scripts(),
        )
    )


def state_chip(state: str) -> str:
    _, label = STATE_STYLE.get(state, STATE_STYLE["active"])
    return f'<span class="chip {esc(state)}" title="{esc(state)}">{esc(label)}</span>'


def topic_card(topic: Any, summary: TopicSummary | None, since: float, rank: int, *, show_messages: int = 3) -> str:
    """One work item: what it is, how it stands, and its proof one click away.

    The data-* attributes are what the filter and the sort read. They are here rather
    than fetched because the whole digest is already in the page: filtering it is a
    class change, and asking the server would be a round trip to rebuild nothing.
    """
    title = card_title(topic, summary)
    age = age_phrase(topic)
    hot = ' class="hot"' if topic.state == "blocked" and np.isfinite(topic.age_days) and topic.age_days >= 3 else ""
    people_shown = ", ".join(topic.participant_names[:4])
    if len(topic.participant_names) > 4:
        people_shown += f" +{len(topic.participant_names) - 4}"

    searchable = " ".join(
        [title, topic.label, topic.item_id, topic.state, people_shown, summary.detail if summary else ""]
    ).lower()
    copy_line = f"• {title} — {STATE_STYLE.get(topic.state, STATE_STYLE['active'])[1]} ({age})"
    if summary and summary.next_step:
        copy_line += f" → {summary.next_step}"

    parts = [
        f'<article class="card {esc(topic.state)}" data-state="{esc(topic.state)}" data-rank="{rank}" '
        f'data-age="{0 if np.isnan(topic.age_days) else topic.age_days:.3f}" '
        f'data-last="{0 if np.isnan(topic.last_ts) else topic.last_ts:.0f}" '
        f'data-count="{len(topic.records)}" data-text="{esc(searchable)}" data-copy="{esc(copy_line)}">',
        f'<div class="card-head">{state_chip(topic.state)}'
        f'<h3><a href="/item/{esc(topic.item_id or topic.key)}">{esc(title)}</a></h3></div>',
        f'<p class="facts"><span{hot}>{esc(age)}</span><span>{len(topic.records)} ข้อความ</span>'
        f"<span>{esc(source_summary(topic))}</span><span>{esc(people_shown)}</span></p>",
    ]
    if summary and summary.detail:
        parts.append(f'<p class="detail">{esc(summary.detail)}</p>')
    if summary and summary.next_step:
        parts.append(f'<p class="next">→ {esc(summary.next_step)}</p>')
    if summary and summary.unverified:
        parts.append('<p class="warn">สรุปนี้อ้างข้อความที่ตรวจสอบไม่ผ่าน — อ่านข้อความต้นทางก่อนเชื่อ</p>')
    if topic.evidence:
        parts.append(
            '<details class="more"><summary>ทำไมถึงเป็นสถานะนี้</summary>'
            f'<p class="meta">{esc(topic.evidence)}</p></details>'
        )
    recent = topic.recent(since)[-show_messages:]
    if recent:
        rows = "".join(
            f'<div class="msg"><span class="who"><span class="tag">'
            f'{esc(SOURCE_LABEL.get(str(record.get("source") or "slack"), "Slack"))}</span>'
            f'{esc(names().of(record.get("user")) or "-")}</span>'
            f"<span>{esc(clean(record['text'], 240))}</span></div>"
            for record in recent
        )
        parts.append(f'<details class="more"><summary>ข้อความล่าสุด ({len(recent)})</summary>{rows}</details>')
    parts.append("</article>")
    return "".join(parts)


def item_list(
    topics: Sequence[Any], build: State, since: float, *, show_messages: int = 3, empty: str = "ไม่มีเรื่องที่ตรงกับที่กรองไว้"
) -> str:
    """The cards plus the empty state the filter falls back to."""
    cards = "".join(
        topic_card(topic, build.summary_for(topic.key), since, rank, show_messages=show_messages)
        for rank, topic in enumerate(topics)
    )
    hidden = "" if not topics else " hidden"
    return f'<div data-list>{cards}</div><div class="empty" data-empty{hidden}>{nothing(empty)}</div>'


def nothing(message: str) -> str:
    """The empty state. A curled-up cat, because nothing-to-do is good news here.

    Every page had its own bare sentence in a dashed box; an empty blockers list in
    particular is the best outcome this product has, and it read like an error.
    """
    return f'{cat("sleep", eyes="shut", size=76)}<p>{esc(message)}</p>'


def list_controls(topics: Sequence[Any], *, sorts: bool = True) -> str:
    """Filter by state, filter by text, sort, copy. All of it over the rendered cards."""
    counts: dict[str, int] = {"": len(topics)}
    for topic in topics:
        counts[topic.state] = counts.get(topic.state, 0) + 1
    buttons = "".join(
        f'<button type="button" data-state-filter="{esc(value)}" aria-pressed="{"true" if not value else "false"}">'
        f"{esc(label)}<b>{counts.get(value, 0)}</b></button>"
        for value, label in (("", "ทั้งหมด"), ("blocked", "ติดอยู่"), ("active", "กำลังทำ"), ("resolved", "เสร็จแล้ว"))
        if counts.get(value, 0) or not value
    )
    sort = (
        '<select data-sort aria-label="เรียงตาม">'
        '<option value="urgency">เรียง: ที่ติดขึ้นก่อน</option>'
        '<option value="age">เรียง: ค้างนานสุด</option>'
        '<option value="recent">เรียง: เพิ่งขยับ</option>'
        '<option value="size">เรียง: คุยกันเยอะสุด</option>'
        "</select>"
        if sorts
        else ""
    )
    return (
        f'<div class="controls"><div class="seg">{buttons}</div>'
        '<input class="filter" type="search" data-filter '
        'placeholder="พิมพ์เพื่อกรอง — ชื่อคน ชื่อทิกเก็ต หรือคำในเรื่อง (กด /)">'
        f'{sort}<button class="ghost" type="button" data-copy title="คัดลอกรายการที่เห็นอยู่ไปวางในแชท">'
        "คัดลอกรายการ</button>"
        '<span class="tally" data-tally></span></div>'
    )


def row_filter(placeholder: str, unit: str) -> str:
    """A text filter over one table's own rows, for the pages whose content is a table.

    `list_controls` filters `.card` elements inside `[data-list]`; a table of 61 tickets
    or 23 people needs the same affordance and has no cards. Scoped to its own section,
    so a page can carry several tables and each one filters itself.
    """
    return (
        '<div class="controls"><input class="filter" type="search" data-rows-filter '
        f'placeholder="{esc(placeholder)}">'
        f'<span class="tally" data-rows-tally data-unit="{esc(unit)}"></span></div>'
    )


def live() -> State:
    """The published build, read once per request.

    Every handler takes the snapshot at the top and reads only from it. `state` is
    rebound by rebuild(), so re-reading the global mid-request is what would let a
    page pair one build's digest with another build's summaries.
    """
    return state


def require_build() -> tuple[State, Digest]:
    build = live()
    if build.digest is None:
        raise HTTPException(status_code=503, detail="Index is still building. Retry in a moment.")
    return build, build.digest


def item_key(key: str) -> str:
    """The bare work-item id from any of the three spellings in circulation.

    The linker's keys are namespaced — ``ticket:REVERAPP-250`` — and that is the form
    the bot writes into the corrections file and the form a person copies out of it.
    `item_id` is the bare ``REVERAPP-250``. The two were never reconciled, so
    ``/item/ticket:REVERAPP-250`` answered 404 while listing REVERAPP-250 among the
    available items, which reads as "the link did nothing".
    """
    bare = key.split(":", 1)[1] if key.lower().startswith("ticket:") else key
    return bare.strip()


#: A word that could be a work item id: a ticket key, or the seven-hex content id
#: `digest.content_id` mints for work nobody filed. Hyphens stay inside the token so
#: `REVERAPP-250` is one word rather than two.
ITEM_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*")


def items_named(digest: Digest, query: str) -> list[Topic]:
    """The work items this query asks for by name, if any.

    Search ranks message text, and a work item's *name* is frequently in none of its
    messages: a ticket key is typed once in a thread of twenty, and when a human
    supplied the link it may be typed nowhere at all. Searching "REVERAPP-250" then
    returns the messages that happen to quote the string and not the item itself,
    which reads as the item not existing.

    Matching is by whole token against `item_id`, not substring: `REVERAPP-25` must
    not answer for `REVERAPP-250`. A bare number is accepted as a suffix — people
    type "250", not "REVERAPP-250" — but only while exactly one item can mean it,
    since answering an ambiguous number with one confident item is the failure this
    function exists to fix, pointed the other way.
    """
    tokens = {token.upper() for token in ITEM_TOKEN_RE.findall(query)}
    if not tokens:
        return []
    named = [topic for topic in digest.topics if str(topic.item_id).upper() in tokens]
    if named:
        return named
    for token in sorted(tokens):
        if not token.isdigit():
            continue
        suffix = f"-{token}"
        matches = [topic for topic in digest.topics if str(topic.item_id).upper().endswith(suffix)]
        if len(matches) == 1:
            return matches
    return []


def find_topic(digest: Digest, key: str) -> Topic:
    """Resolve a work item by its stable id first, by cluster rank only as a fallback.

    `key` is the Louvain cluster index, which is a size rank: it names a different
    work item after the next rebuild. `item_id` is the ticket the item's messages
    mention, or a hash of its earliest message — stable across builds, and therefore
    the thing a bookmark, a Slack card or a human correction may point at. The int
    fallback stays because /blockers links and older bookmarks still carry it.

    Matched case-insensitively and through `item_key`, because the id travels by
    being pasted: out of a Slack card, out of the corrections file, out of a URL
    somebody lower-cased. None of those are a different work item.
    """
    bare = item_key(key)
    for candidate in digest.topics:
        if candidate.item_id == bare:
            return candidate
    folded = bare.upper()
    for candidate in digest.topics:
        if str(candidate.item_id).upper() == folded:
            return candidate
    if bare.isdigit():
        for candidate in digest.topics:
            if candidate.key == int(bare):
                return candidate
    known = ", ".join(f"{topic.item_id} (#{topic.key})" for topic in digest.topics)
    raise HTTPException(status_code=404, detail=f"No active work item {key}. Available: {known or '(none)'}")


# ---- pages -----------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def digest_page() -> HTMLResponse:
    build, digest = require_build()
    tiles = [
        stat_tile("ติดอยู่", str(len(digest.blocked)), "รอให้มีคนมาแก้"),
        stat_tile("เสร็จแล้ว", str(len(digest.resolved)), f"ใน {build.days:g} วันที่ผ่านมา"),
        stat_tile("เรื่องที่ขยับ", str(len(digest.topics)), f"จาก {digest.corpus_size} ข้อความ"),
        stat_tile("คนที่เกี่ยวข้อง", str(len(people_rows(digest))), "มีชื่ออยู่ในเรื่องใดเรื่องหนึ่ง"),
    ]
    body = (
        '<div class="legend">'
        '<span><i style="background:var(--state-blocked)"></i>ติดอยู่ — มีคนบอกว่าไปต่อไม่ได้ และยังไม่มีใครบอกว่าแก้แล้ว</span>'
        '<span><i style="background:var(--state-active)"></i>กำลังทำ — ยังคุยกันอยู่</span>'
        '<span><i style="background:var(--state-resolved)"></i>เสร็จแล้ว — มีคนบอกว่าจบแล้ว</span>'
        "</div>"
        + list_controls(digest.topics)
        + item_list(digest.topics, build, digest.since)
    )
    sections = [
        section(
            body,
            title="ทุกเรื่องที่ขยับในช่วงนี้",
            note="เรียงจากเรื่องที่ติดอยู่ก่อน กดชื่อเรื่องเพื่อดูว่าเกิดอะไรขึ้นบ้างตามลำดับเวลา "
            "ปุ่มด้านบนใช้กรองเฉพาะที่อยากดู แล้วกด “คัดลอกรายการ” เอาไปวางในแชทได้เลย",
        ),
        section(
            how(
                "ระบบอ่านข้อความ Slack ประชุม และทิกเก็ต แล้วจับข้อความที่พูดเรื่องเดียวกันมารวมเป็นหนึ่งเรื่อง "
                "จากนั้นดูว่าข้อความไหนบอกว่า 'ติด' และมีข้อความหลังจากนั้นบอกว่า 'แก้แล้ว' หรือยัง — "
                "เป็นการอ่านคำในข้อความจริงตามกฎที่เขียนไว้ ไม่ได้ให้ AI เดา ทุกสถานะจึงชี้กลับไปที่ข้อความต้นทางได้เสมอ"
            ),
            note="",
        ),
    ]
    subtitle = f"{build.days:g} วันล่าสุด · ข้อมูล ณ {build.built_at}"
    return render("สรุปงานประจำวัน", tiles, sections, subtitle, current="/", build=build, hero=cat("code", eyes="open"))


@app.get("/blockers", response_class=HTMLResponse)
def blockers_page() -> HTMLResponse:
    build, digest = require_build()
    blocked = digest.blocked
    oldest = max((topic.age_days for topic in blocked if np.isfinite(topic.age_days)), default=float("nan"))
    week = sum(1 for topic in blocked if np.isfinite(topic.age_days) and topic.age_days >= 7)
    tiles = [
        stat_tile("ติดอยู่", str(len(blocked)), "งานที่ไปต่อไม่ได้"),
        stat_tile("ค้างนานสุด", human_age(oldest), "นับจากครั้งล่าสุดที่มีคนบอกว่าติด"),
        stat_tile("ค้างเกินสัปดาห์", str(week), "ควรหยิบขึ้นมาคุยก่อน"),
    ]
    sections = [
        section(
            list_controls(blocked, sorts=True) + item_list(blocked, build, digest.since, show_messages=2, empty="ตอนนี้ไม่มีอะไรติด"),
            title="งานที่ยังไปต่อไม่ได้",
            note="เรียงจากค้างนานสุด นี่คือรายการที่ควรเอาเข้า standup กดชื่อเรื่องเพื่อดูว่าติดตรงไหนและใครเกี่ยวข้อง",
        ),
        section(
            how(
                "เรื่องจะขึ้นหน้านี้เมื่อมีคนพิมพ์ว่าติด รอ หรือ pending ในข้อความจริง "
                "แล้วยังไม่มีข้อความไหนหลังจากนั้นบอกว่าแก้แล้ว — กด “ทำไมถึงเป็นสถานะนี้” ในแต่ละเรื่องจะเห็นประโยคที่ระบบใช้ตัดสิน"
            ),
            note="",
        ),
    ]
    return render("งานที่ติดอยู่", tiles, sections, f"{build.days:g} วันล่าสุด · ข้อมูล ณ {build.built_at}", current="/blockers", build=build, hero=cat("blocked", eyes="squint"))


def people_rows(digest: Digest) -> list[dict[str, Any]]:
    """Who is on what, aggregated from the same topics the digest already built.

    The digest answers "what is stuck"; a standup also has to answer "whose is it".
    That was derivable from the API and nowhere on any page, so everyone read it off
    the participant list of thirty cards by eye.
    """
    rows: dict[str, dict[str, Any]] = {}
    for topic in digest.topics:
        for user in topic.participants:
            row = rows.setdefault(
                user,
                {"user": user, "name": names().of(user) or user, "items": 0, "blocked": 0,
                 "messages": 0, "last_ts": float("nan"), "blocked_items": []},
            )
            row["items"] += 1
            for record in topic.records:
                if str(record.get("user") or "") != user:
                    continue
                row["messages"] += 1
                when = timestamp(record)
                if np.isfinite(when) and not (np.isfinite(row["last_ts"]) and row["last_ts"] >= when):
                    row["last_ts"] = when
            if topic.state == "blocked":
                row["blocked"] += 1
                row["blocked_items"].append({"item_id": topic.item_id, "title": card_title(topic, None)})
    return sorted(rows.values(), key=lambda row: (-row["blocked"], -row["items"], -row["messages"]))


def person_link(user: Any, label: str = "") -> str:
    """A link to one person's page. The id goes in the path, encoded whole.

    A participant is either a Slack id or a speaker name off a meeting transcript, and
    the second kind carries spaces and Thai — so the segment is percent-encoded rather
    than assumed to be url-safe.
    """
    key = quote(str(user or ""), safe="")
    return f'<a href="/person/{esc(key)}">{esc(label or names().of(user) or user)}</a>'


#: Stuck items printed inside a person's row before the rest become a "+n" link. The
#: row is a summary; the page behind the name is where all of them are.
ROW_ITEMS = 3


def people_row(row: dict[str, Any]) -> str:
    """One person as a table row, with their stuck items linked rather than counted.

    A count of blocked items tells someone they should worry; the links tell them
    which conversation to open, which is the only action the number implies.
    """
    blocked = (
        f'<td class="num" style="color:var(--state-blocked);font-weight:650">{row["blocked"]}</td>'
        if row["blocked"]
        else '<td class="num">-</td>'
    )
    spoke = format_timestamp(str(row["last_ts"])) if np.isfinite(row["last_ts"]) else "-"
    items = " · ".join(
        f'<a href="/item/{esc(item["item_id"])}">{esc(clean(item["title"], 30))}</a>'
        for item in row["blocked_items"][:ROW_ITEMS]
    )
    rest = len(row["blocked_items"]) - ROW_ITEMS
    if rest > 0:
        items += " · " + person_link(row["user"], f"+{rest} เรื่อง")
    searchable = f'{row["name"]} {row["user"]}'.lower()
    return (
        f'<tr data-text="{esc(searchable)}"><td>{person_link(row["user"], row["name"])}</td>'
        f'<td class="num">{row["items"]}</td>'
        f"{blocked}"
        f'<td class="num">{row["messages"]}</td>'
        f'<td class="num">{esc(spoke)}</td>'
        f'<td>{items or "-"}</td></tr>'
    )


@app.get("/people", response_class=HTMLResponse)
def people_page() -> HTMLResponse:
    build, digest = require_build()
    rows = people_rows(digest)
    stuck = [row for row in rows if row["blocked"]]
    tiles = [
        stat_tile("คนที่เกี่ยวข้อง", str(len(rows)), f"ใน {len(digest.topics)} เรื่อง"),
        stat_tile("มีเรื่องที่ติด", str(len(stuck)), "อย่างน้อยหนึ่งเรื่อง"),
        stat_tile("คุยเยอะสุด", str(rows[0]["messages"]) if rows else "-", f"ข้อความจาก {rows[0]['name']}" if rows else ""),
    ]
    table = wide_table(
        "<th>คน</th><th>เรื่องที่เกี่ยวข้อง</th><th>ที่ติดอยู่</th>"
        "<th>ข้อความ</th><th>พูดล่าสุด</th><th>เรื่องที่ติด</th>",
        "".join(people_row(row) for row in rows),
    )
    sections = [
        section(
            (row_filter("พิมพ์ชื่อเพื่อกรอง (กด /)", "คน") + table)
            if rows
            else f'<div class="empty">{nothing("ยังไม่มีใครอยู่ในช่วงเวลานี้")}</div>',
            title="ใครอยู่กับเรื่องไหน",
            note="เรียงจากคนที่มีเรื่องติดมากที่สุด ใช้ดูว่าควรถามใครก่อนใน standup "
            "กดชื่อคนเพื่อดูรายละเอียดของคนนั้น — เรื่องที่เกี่ยวข้องทั้งหมด ข้อความล่าสุด และคนที่อยู่ในเรื่องเดียวกันบ่อย",
        ),
        section(
            how(
                "ตัวเลขทุกช่องนับจากเรื่องที่ระบบจับกลุ่มไว้แล้ว คนหนึ่งจะถูกนับเข้าเรื่องหนึ่งเมื่อมีข้อความของเขาอยู่ในเรื่องนั้น "
                "ไม่ได้แปลว่าเป็นเจ้าของงาน และชื่อที่มาจากไฟล์ถอดเสียงประชุมจะแยกแถวจากบัญชี Slack "
                "เพราะไฟล์ประชุมไม่มี id ของ Slack ให้จับคู่ — ระบบจึงไม่เดาว่าเป็นคนเดียวกัน"
            ),
            note="",
        ),
    ]
    return render("รายคน", tiles, sections, f"{build.days:g} วันล่าสุด · ข้อมูล ณ {build.built_at}", current="/people", build=build, hero=cat("people", eyes="open"))


def find_person(digest: Digest, key: str) -> str:
    """Resolve a URL segment to a participant, by id first and by display name second.

    The id is what every link on /people carries. The name is accepted too because the
    id of a meeting speaker *is* their name, and because a URL somebody typed or pasted
    from a standup ("/person/Mild") is worth answering rather than 404ing.
    """
    wanted = key.strip()
    everyone = sorted({user for topic in digest.topics for user in topic.participants})
    if wanted in everyone:
        return wanted
    for user in everyone:
        if (names().of(user) or user).lower() == wanted.lower():
            return user
    known = ", ".join(names().of(user) or user for user in everyone)
    raise HTTPException(status_code=404, detail=f"No participant {key}. In this window: {known or '(nobody)'}")


def person_detail(build: State, digest: Digest, user: str) -> dict[str, Any]:
    """One person's slice of the build: their items, their own messages, their neighbours.

    Counted over the same topics the digest already built, so these numbers are the
    numbers in the /people row that linked here. A person page recomputing its own
    totals straight from the corpus would disagree with that table, and a reader with
    two different answers has no way to tell which one is the page's mistake.
    """
    topics = [topic for topic in digest.topics if user in topic.participants]
    mine: list[dict[str, Any]] = []
    sources: dict[str, int] = {}
    partners: dict[str, dict[str, Any]] = {}
    for topic in topics:
        # Once per topic, not per message: card_title reads every record in the item.
        title = card_title(topic, build.summary_for(topic.key))
        for record in topic.records:
            if str(record.get("user") or "") != user:
                continue
            # The item travels with the message: on this page the constant is the person,
            # so what each line has to say is which work item it belongs to. The title
            # comes along because half the ids are content hashes, which name nothing.
            mine.append({**record, "item_id": topic.item_id, "item_title": title})
            source = str(record.get("source") or "slack")
            sources[source] = sources.get(source, 0) + 1
        for other in topic.participants:
            if other == user:
                continue
            seen = partners.setdefault(
                other, {"user": other, "name": names().of(other) or other, "items": 0, "blocked": 0}
            )
            seen["items"] += 1
            if topic.state == "blocked":
                seen["blocked"] += 1
    mine.sort(key=lambda record: timestamp(record) if np.isfinite(timestamp(record)) else 0.0, reverse=True)
    spoke = next((timestamp(record) for record in mine if np.isfinite(timestamp(record))), float("nan"))
    return {
        "user": user,
        "name": names().of(user) or user,
        "topics": topics,
        "blocked": [topic for topic in topics if topic.state == "blocked"],
        "messages": mine,
        "sources": sources,
        "partners": sorted(partners.values(), key=lambda row: (-row["items"], -row["blocked"], row["name"])),
        "last_ts": spoke,
    }


#: How many of a person's own messages the page prints. The busiest participant in this
#: corpus has 143 in a seven-day window, which is a wall rather than a page — and the
#: total is stated beside the list, because a cap nobody is told about reads as "that
#: was everything they said".
PERSON_MESSAGES = 40


@app.get("/person/{user}", response_class=HTMLResponse)
def person_page(user: str) -> HTMLResponse:
    """One person in detail: what they are on, what they said, and who they said it with.

    /people answers "who should I ask first" and then had nowhere to go. Every number in
    that row was a real question — which 21 items, which 5 stuck, what did they actually
    say — and all of it was already in the build.
    """
    build, digest = require_build()
    person = person_detail(build, digest, find_person(digest, user))
    name = person["name"]

    tiles = [
        stat_tile("เรื่องที่เกี่ยวข้อง", str(len(person["topics"])), f"จาก {len(digest.topics)} เรื่องที่ขยับ"),
        stat_tile("ที่ติดอยู่", str(len(person["blocked"])), "รอให้มีคนมาแก้"),
        stat_tile(
            "ข้อความ",
            str(len(person["messages"])),
            " · ".join(
                f"{SOURCE_LABEL.get(source, source)} {count}"
                for source, count in sorted(person["sources"].items(), key=lambda pair: -pair[1])
            ),
        ),
        stat_tile(
            "พูดล่าสุด",
            human_age((datetime.now(tz=timezone.utc).timestamp() - person["last_ts"]) / 86400.0)
            if np.isfinite(person["last_ts"])
            else "-",
            f'ที่แล้ว · {format_timestamp(str(person["last_ts"]))}' if np.isfinite(person["last_ts"]) else "",
        ),
    ]

    # Newest first, grouped by day. On an item page the question is "what happened, in
    # order"; on a person's page it is "what have they said lately", which reads the
    # other way round.
    days: list[tuple[str, list[str]]] = []
    for record in person["messages"][:PERSON_MESSAGES]:
        stamp = format_timestamp(str(record.get("ts", "")))
        day, _, clock = stamp.partition(" ")
        item = str(record.get("item_id") or "")
        where = (
            f' · <a href="/item/{esc(item)}" title="{esc(record.get("item_title") or "")}">{esc(item)}</a>'
            if item
            else ""
        )
        line = (
            f'<div class="msg"><span class="who"><span class="tag">'
            f'{esc(SOURCE_LABEL.get(str(record.get("source") or "slack"), "Slack"))}</span>'
            f"{esc(clock)}{where}</span>"
            f'<span>{esc(clean(record["text"], 320))}{ticket_link(record)}</span></div>'
        )
        if days and days[-1][0] == day:
            days[-1][1].append(line)
        else:
            days.append((day, [line]))
    trail = "".join(f'<p class="day">{esc(day)}</p>' + "".join(lines) for day, lines in days)
    left = len(person["messages"]) - PERSON_MESSAGES

    partner_rows = "".join(
        '<tr data-text="{}"><td>{}</td><td class="num">{}</td><td class="num">{}</td></tr>'.format(
            esc(f'{row["name"]} {row["user"]}'.lower()),
            person_link(row["user"], row["name"]),
            row["items"],
            f'<span style="color:var(--state-blocked)">{row["blocked"]}</span>' if row["blocked"] else "-",
        )
        for row in person["partners"]
    )

    sections = [
        section(
            (list_controls(person["topics"]) + item_list(person["topics"], build, digest.since, show_messages=2))
            if person["topics"]
            else f'<div class="empty">{nothing("ยังไม่มีเรื่องของคนนี้ในช่วงเวลานี้")}</div>',
            title=f"เรื่องที่ {name} อยู่ด้วย",
            note="เรียงจากที่ติดอยู่ก่อน กรองและเรียงได้เหมือนหน้าสรุปงาน กดชื่อเรื่องเพื่อดูไทม์ไลน์ของเรื่องนั้น",
        ),
        section(
            trail or f'<div class="empty">{nothing("ยังไม่พบข้อความของคนนี้")}</div>',
            title=f'ข้อความล่าสุดของ {name} ({min(len(person["messages"]), PERSON_MESSAGES)} จาก {len(person["messages"])})',
            note=(
                "ใหม่สุดอยู่บนสุด แบ่งตามวัน ข้อความละหนึ่งบรรทัดพร้อมรหัสเรื่องที่ระบบจัดให้ กดรหัสเพื่อไปดูบริบททั้งเรื่อง"
                + (f" · ที่เก่ากว่านี้อีก {left} ข้อความอ่านได้ในหน้าของแต่ละเรื่อง" if left > 0 else "")
            ),
        ),
        section(
            (row_filter("พิมพ์ชื่อเพื่อกรอง", "คน")
             + wide_table("<th>คน</th><th>เรื่องที่อยู่ด้วยกัน</th><th>ที่ติดอยู่</th>", partner_rows, least=380))
            if partner_rows
            else f'<div class="empty">{nothing("ยังไม่มีใครอยู่ในเรื่องเดียวกัน")}</div>',
            title="อยู่ในเรื่องเดียวกันกับใครบ่อย",
            note="ใช้ตอนหาคนที่รู้เรื่องเดียวกัน หรือตอนที่งานติดแล้วต้องรู้ว่าใครอยู่ในบทสนทนานั้นด้วย",
        ),
        section(
            how(
                f"หน้านี้นับจากเรื่องที่ระบบจับกลุ่มไว้ในช่วง {build.days:g} วันล่าสุด — {name} ถูกนับเข้าเรื่องหนึ่งเมื่อมีข้อความของเขาอยู่ในเรื่องนั้น "
                "ไม่ได้แปลว่าเป็นเจ้าของงาน “อยู่ในเรื่องเดียวกัน” ก็คือมีข้อความอยู่ในเรื่องเดียวกัน ไม่ได้แปลว่าคุยกันตรง ๆ "
                "และถ้าคนคนนี้พูดในที่ประชุมด้วย ชื่อจากไฟล์ถอดเสียงจะเป็นอีกหน้าหนึ่ง เพราะไฟล์ประชุมไม่มี id ของ Slack ให้จับคู่"
            ),
            note="",
        ),
    ]
    actions = '<a class="ghost" href="/people" style="text-decoration:none">ดูทุกคน</a>'
    return render(
        name,
        tiles,
        sections,
        f'{build.days:g} วันล่าสุด · ข้อมูล ณ {build.built_at}',
        current="/people",
        build=build,
        actions=actions,
        hero=cat("people", eyes="open"),
    )


#: Every kind detect() can emit — checked against tam.analysis.drift, because a kind
#: with no entry here rendered as its own identifier on the page.
DRIFT_LABEL = {
    "ticket_closed_but_slack_blocked": "ทิกเก็ตปิดแล้ว แต่ใน Slack ยังติดอยู่",
    "ticket_closed_but_talking": "ทิกเก็ตปิดแล้ว แต่ยังคุยเรื่องนี้กันอยู่",
    "slack_blocked_but_ticket_open": "ใน Slack บอกว่าติด แต่ทิกเก็ตยังเปิดตามปกติ",
    "slack_done_but_ticket_open": "ใน Slack บอกว่าจบแล้ว แต่ทิกเก็ตยังไม่ปิด",
}


def row_text(*parts: Any) -> str:
    """The lower-cased haystack a row filter matches, escaped for an attribute.

    Kept out of the f-strings that build the rows: an f-string nested inside an attribute
    is a syntax error before Python 3.12, and what a row is searchable by is a rule worth
    having in one place anyway.
    """
    return esc(" ".join(str(part) for part in parts if part).lower())


def wide_table(head: str, rows: str, *, least: int = 560) -> str:
    """A table that scrolls inside its own box rather than pushing the page sideways.

    Six or seven columns of Thai do not fit a phone, and `table { width: 100% }` alone
    either overflows the body or squeezes a column to one character per line. `least` is
    the width below which this table starts scrolling instead of squeezing — a three
    column table has no business scrolling at the width a seven column one needs.
    """
    return (
        f'<div class="wide" style="--least:{least}px"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def open_ticket_row(row: dict[str, Any], *, quiet: bool) -> str:
    """One open ticket, with the quiet ones marked rather than listed twice.

    A reader asking "which of the open ones are the ones nobody has touched" should not
    have to compare two tables by eye, so the silent list is this table's red rows.
    """
    idle = esc(human_age(as_days(row["idle_days"])))
    age = (
        f'<td class="num" style="color:var(--state-blocked)" title="เงียบเกินเกณฑ์ — อยู่ในรายการด้านบนด้วย">{idle}</td>'
        if quiet
        else f'<td class="num">{idle}</td>'
    )
    return (
        f'<tr data-text="{row_text(row["ticket"], row["summary"], row["state"])}">'
        f'<td><a href="{esc(row["url"])}" target="_blank" rel="noopener">{esc(row["ticket"])}</a></td>'
        f'<td>{esc(clean(row["summary"], 80))}</td>'
        f'<td>{esc(row["state"] or "-")}</td>{age}'
        f'<td>{"เคยพูดถึง" if row["mentioned_in_slack"] else "ไม่เคยพูดถึงเลย"}</td></tr>'
    )


def matched_row(row: dict[str, Any]) -> str:
    """One work item beside the ticket it names, and whether the two could be compared."""
    hits = (
        f'<td class="num" title="พูดถึงล่าสุด {esc(row["last_mention"])}">{row["mentions"]}</td>'
        if row["last_mention"]
        else f'<td class="num">{row["mentions"]}</td>'
    )
    return (
        f'<tr data-text="{row_text(row["title"], row["item_id"], row["ticket"], row["ticket_state"])}">'
        f'<td><a href="/item/{esc(row["item_id"])}">{esc(clean(row["title"], 62))}</a></td>'
        f'<td>{state_chip(row["our_state"])}</td>'
        f'<td><a href="{esc(row["ticket_url"])}" target="_blank" rel="noopener">{esc(row["ticket"])} ↗</a></td>'
        f'<td>{esc(row["ticket_state"] or "-")}</td>'
        f'<td>{agreement_cell(row)}</td>{hits}'
        f'<td class="num">{row["messages"]}</td></tr>'
    )


def unmatched_row(row: dict[str, Any]) -> str:
    """A work item that names a ticket the board did not return — the honest complement."""
    return (
        f'<tr><td><a href="/item/{esc(row["item_id"])}">{esc(clean(row["title"], 62))}</a></td>'
        f'<td>{esc(row["item_id"])}</td><td>{state_chip(row["our_state"])}</td>'
        f'<td class="num">{row["messages"]}</td></tr>'
    )


def tracker_pairs(build: State) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Work items that name a ticket, split by whether the board returned that ticket.

    This is the join the coverage tile counts. Without the rows behind it, "11 of 11
    matched" is a claim about a join nobody can inspect — and the unmatched half is the
    interesting one: it means somebody typed a key the board does not have.
    """
    digest = build.digest
    if digest is None:
        return [], []
    kinds = {drift["item_id"]: drift["kind"] for drift in build.drifts}
    matched: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for topic in digest.topics:
        if str(topic.item_id).startswith("c"):
            continue  # a hashed id, not a ticket key: there is nothing to look up
        ticket = build.tickets.get(str(topic.item_id).upper())
        row = {
            "item_id": topic.item_id,
            "title": card_title(topic, build.summary_for(topic.key)),
            "our_state": topic.state,
            "messages": len(topic.records),
            "people": ", ".join(topic.participant_names[:3]),
            "last": format_timestamp(str(topic.last_ts)) if np.isfinite(topic.last_ts) else "",
            "drift": kinds.get(topic.item_id, ""),
            "ticket": ticket["ticket"] if ticket else topic.item_id,
            "ticket_state": ticket["state"] if ticket else "",
            "ticket_url": ticket["url"] if ticket else "",
            "ticket_summary": ticket["summary"] if ticket else "",
            "ticket_resolved": bool(ticket["resolved"]) if ticket else False,
            "ticket_updated_at": ticket["updated_at"] if ticket else "",
            # Whether anybody typed this key into the chat, which is what decides
            # whether a comparison was possible at all. See agreement_cell.
            "mentions": ticket["mentions"] if ticket else 0,
            "last_mention": ticket["last_mention"] if ticket else "",
        }
        (matched if ticket else missing).append(row)
    return matched, missing


def agreement_cell(row: dict[str, Any]) -> str:
    """Whether an item and its ticket agree — or whether nobody made them comparable.

    Three outcomes, not two. `detect` compares an item against its ticket only when a
    person named that ticket in the conversation, so "no drift" covers both "the two
    sources say the same thing" and "there was nothing to compare". Printing the first
    where the second is true is the confident-about-nothing answer drift.py refuses.
    """
    if row["drift"]:
        label = DRIFT_LABEL.get(row["drift"], row["drift"])
        return f'<span style="color:var(--state-blocked)">{esc(label)}</span>'
    if not row["mentions"]:
        return (
            '<span style="color:var(--ink3)" '
            'title="ไม่มีใครพิมพ์เลขทิกเก็ตนี้ในแชท จึงไม่มีข้อความให้เทียบกับสถานะในบอร์ด">เทียบไม่ได้</span>'
        )
    return '<span style="color:var(--state-resolved)">ตรงกัน</span>'


def drift_card(drift: dict[str, Any]) -> str:
    """One disagreement, with both of the messages behind it.

    A drift rests on two claims: somebody typed this ticket's key in this conversation,
    and something in this conversation says the work is stuck (or done). Those are almost
    never the same message — 0 of 4 on this project the day this was written — and the
    card used to show only the second under a heading that asserted the first. A reader
    who opened the evidence found a sentence about another ticket and had no way to tell
    whether the finding was wrong or the display was. Now both are here, labelled, and
    the card says plainly when they are not one message.
    """
    state_label = STATE_STYLE.get(drift["our_state"], ("", drift["our_state"]))[1]
    parts = [
        '<article class="card blocked">',
        f'<div class="card-head"><span class="chip blocked">{esc(DRIFT_LABEL.get(drift["kind"], drift["kind"]))}</span>'
        f'<h3><a href="/item/{esc(drift["item_id"])}">{esc(drift["item_id"])}</a></h3></div>',
        f'<p class="facts"><span>ทิกเก็ตว่า “{esc(drift["ticket_state"])}”</span>'
        f'<span>Slack ว่า “{esc(state_label)}”</span>'
        + (f'<span><a href="{esc(drift["ticket_url"])}" target="_blank" rel="noopener">เปิดทิกเก็ต ↗</a></span>'
           if drift.get("ticket_url") else "")
        + "</p>",
        f'<p class="detail">{esc(drift["detail"])}</p>',
    ]
    if not drift.get("evidence_names_ticket", True):
        parts.append(
            '<p class="warn">ประโยคที่ทำให้สถานะนี้เป็นอย่างนี้ ไม่ได้พูดถึงเลขทิกเก็ตนี้ตรง ๆ — '
            "มันมาจากบทสนทนาในเรื่องเดียวกัน ที่ผูกกับทิกเก็ตนี้เพราะมีคนพิมพ์เลขไว้อีกข้อความหนึ่ง "
            "อ่านสองประโยคข้างล่างเทียบกันก่อนเชื่อ</p>"
        )
    rows = [f'<p class="meta"><b>สถานะมาจากข้อความนี้</b><br>{esc(drift["evidence"])}</p>']
    if drift.get("link_text"):
        rows.append(f'<p class="meta"><b>ผูกกับทิกเก็ตนี้เพราะข้อความนี้</b><br>{esc(drift["link_text"])}</p>')
    parts.append(
        '<details class="more"><summary>ข้อความที่ใช้ตัดสิน (2 ประโยค)</summary>' + "".join(rows) + "</details>"
    )
    parts.append("</article>")
    return "".join(parts)


def unresolved_block(build: State) -> str:
    """The human links this build could not apply, or a line saying there are none.

    Rendered even when empty. A person who linked something wants to know the link is
    live, and a section that only appears on failure cannot answer that — it is
    indistinguishable from a section that does not exist.
    """
    if not build.unresolved_links:
        return f'<div class="empty">{nothing("การกดเชื่อมทุกครั้งมีผลกับ build นี้ครบ")}</div>'
    rows = "".join(
        f'<tr data-text="{row_text(row["record_id"], row["key"], row["why"])}">'
        f'<td><code>{esc(row["record_id"])}</code></td>'
        f'<td>{esc(item_key(row["key"]))}</td>'
        f'<td>{esc(row["why"])}</td>'
        "</tr>"
        for row in build.unresolved_links
    )
    return (
        '<div class="alert"><b>มีการเชื่อมที่ไม่ได้ทำอะไรเลย</b><br>'
        "ทิกเก็ตในตารางนี้ไม่ได้รับข้อความที่คนตั้งใจจะยกให้ และหน้าอื่นก็นับไม่ครบตามไปด้วย</div>"
        + wide_table("<th>ข้อความ</th><th>ตั้งใจจะเชื่อมกับ</th><th>ทำไมถึงไม่มีผล</th>", rows)
    )


@app.get("/tracker", response_class=HTMLResponse)
def tracker_page() -> HTMLResponse:
    """Where the ticket board and the conversation disagree, and what nobody is discussing.

    Both halves were computed on every build and served only as JSON, so the answer to
    "is the board telling the truth" existed and had no page. `tracker_error` is shown
    as loudly as the findings: an empty list next to an unreachable tracker means
    "unknown", never "the two agree".
    """
    build = live()
    coverage = build.tracker_coverage
    tiles = [
        stat_tile("ไม่ตรงกัน", str(len(build.drifts)), "ทิกเก็ตกับ Slack บอกคนละอย่าง"),
        stat_tile("ทิกเก็ตที่เงียบ", str(len(build.silent)), "เปิดอยู่แต่ไม่มีใครพูดถึง"),
        stat_tile("ทิกเก็ตที่เปิดอยู่", str(coverage.get("tracker_open", 0)), f'จากทั้งหมด {coverage.get("tracker_issues", 0)}'),
        stat_tile("เรื่องที่จับคู่ได้", str(coverage.get("matched_in_youtrack", 0)), f'จาก {coverage.get("with_ticket_key", 0)} เรื่องที่อ้างเลขทิกเก็ต'),
    ]
    # Only when there are some. A permanent "0 broken links" tile spends a quarter of
    # the row on the absence of a rare fault; the section below still says so in words.
    if build.unresolved_links:
        tiles.append(
            stat_tile("เชื่อมแล้วไม่มีผล", str(len(build.unresolved_links)), "คนกดเชื่อม แต่หาข้อความไม่เจอ")
        )
    if build.tracker_error:
        sections = [
            section(
                f'<div class="alert"><b>อ่านข้อมูลทิกเก็ตไม่ได้</b><br>'
                "ตัวเลขด้านบนจึงแปลว่า “ไม่รู้” ไม่ได้แปลว่า “ตรงกันดีอยู่แล้ว”<br>"
                f"<code>{esc(build.tracker_error[:240])}</code></div>",
                title="เทียบกับทิกเก็ตไม่ได้ตอนนี้",
            )
        ]
        return render("เทียบกับทิกเก็ต", tiles, sections, f"ข้อมูล ณ {build.built_at}", current="/tracker", build=build, hero=cat("tracker", eyes="squint"))

    drift_rows = "".join(drift_card(drift) for drift in build.drifts)
    silent_rows = "".join(
        f'<tr data-text="{row_text(quiet["ticket"], quiet["summary"], quiet["state"])}">'
        f'<td><a href="{esc(quiet["url"])}" target="_blank" rel="noopener">{esc(quiet["ticket"])}</a></td>'
        f'<td>{esc(clean(quiet["summary"], 80))}</td>'
        f'<td>{esc(quiet["state"])}</td>'
        f'<td class="num">{esc(human_age(as_days(quiet["quiet_days"])))}</td>'
        f'<td>{"เคยพูดถึง" if quiet["mentioned_in_slack"] else "ไม่เคยพูดถึงเลย"}</td>'
        "</tr>"
        for quiet in build.silent
    )
    hushed = {str(quiet["ticket"]).upper() for quiet in build.silent}
    open_rows = "".join(
        open_ticket_row(row, quiet=str(row["ticket"]).upper() in hushed) for row in build.open_tickets
    )

    matched, missing = tracker_pairs(build)
    matched_rows = "".join(matched_row(row) for row in matched)
    # Folded, because on a healthy corpus it is empty and on a real one it is short. It
    # is here at all because "9 of 11 matched" leaves two items whose key the board does
    # not know, and those two are the ones somebody typed wrong or the board deleted.
    missing_block = (
        f'<details class="more"><summary>เรื่องที่อ้างเลขทิกเก็ตแต่หาในบอร์ดไม่เจอ ({len(missing)})</summary>'
        + wide_table(
            "<th>เรื่อง</th><th>เลขที่อ้าง</th><th>Slack ว่า</th><th>ข้อความ</th>",
            "".join(unmatched_row(row) for row in missing),
        )
        + "</details>"
        if missing
        else ""
    )

    sections = [
        section(
            unresolved_block(build),
            title=f"การกดเชื่อมที่ยังไม่มีผล ({len(build.unresolved_links)})",
            note="คนกดเชื่อมข้อความกับทิกเก็ตไว้ แต่ build นี้หาข้อความนั้นไม่เจอ จึงไม่ได้ทำอะไรเลย "
            "ก่อนหน้านี้มันเป็นแค่บรรทัดใน log ที่ไม่มีใครเปิด ส่วนคนกดเห็นคำว่า “เชื่อมแล้ว”",
        ),
        section(
            drift_rows or f'<div class="empty">{nothing("ในบรรดาเรื่องที่เทียบได้ ไม่มีอันไหนขัดกับบอร์ด")}</div>',
            title=f"ที่ทิกเก็ตกับ Slack ไม่ตรงกัน ({len(build.drifts)})",
            note="ทิกเก็ตบอกอย่างหนึ่ง แต่คนในแชทพูดอีกอย่าง — ปกติแปลว่ามีคนลืมอัปเดตบอร์ด "
            "หรือมีปัญหาโผล่ขึ้นมาใหม่หลังปิดทิกเก็ตไปแล้ว · แต่ละใบยืนอยู่บนสองประโยค: "
            "ประโยคที่มีคนพิมพ์เลขทิกเก็ต และประโยคที่ทำให้สถานะฝั่ง Slack เป็นอย่างนั้น "
            "ถ้าสองประโยคนี้ไม่ใช่ข้อความเดียวกัน การ์ดจะบอกไว้ให้เห็น",
        ),
        section(
            (row_filter("พิมพ์เพื่อกรอง — เลขทิกเก็ต ชื่อเรื่อง หรือสถานะ", "youtrack")
             + wide_table(
                 "<th>youtrack</th><th>เรื่อง</th><th>สถานะ</th><th>เงียบมา</th><th>ใน Slack</th>", silent_rows
             ))
            if silent_rows
            else f'<div class="empty">{nothing("ไม่มีทิกเก็ตที่เงียบเกินเกณฑ์")}</div>',
            title=f"ทิกเก็ตที่เปิดค้างไว้แล้วเงียบ ({len(build.silent)})",
            note="งานที่ไม่มีใครคุยถึงจะไม่โผล่ในหน้าสรุป เพราะไม่มีข้อความให้อ่าน — "
            "ต้องดูจากบอร์ดเท่านั้น นี่คือส่วนที่ Slack มองไม่เห็น",
        ),
        section(
            (row_filter("พิมพ์เพื่อกรอง — เลขทิกเก็ต ชื่อเรื่อง หรือสถานะ", "youtrack")
             + wide_table(
                 "<th>youtrack</th><th>เรื่อง</th><th>สถานะในบอร์ด</th><th>แตะล่าสุด</th><th>ใน Slack</th>", open_rows
             ))
            if open_rows
            else f'<div class="empty">{nothing("ไม่มีทิกเก็ตที่เปิดอยู่ในบอร์ด")}</div>',
            title=f'ทิกเก็ตที่เปิดอยู่ทั้งหมด ({coverage.get("tracker_open", 0)})',
            note=f'ทั้งบอร์ด ไม่ใช่แค่ส่วนที่ Slack พูดถึง — จากทิกเก็ตทั้งหมด {coverage.get("tracker_issues", 0)} ใบ '
            f'“แตะล่าสุด” คือนานแล้วเท่าไหร่ที่ไม่มีใครขยับใบนี้ ตัวเลขสีแดงคือ {len(build.silent)} ใบที่เงียบเกินเกณฑ์ '
            "ซึ่งเป็นรายการเดียวกับหัวข้อด้านบน",
        ),
        section(
            (row_filter("พิมพ์เพื่อกรอง — ชื่อเรื่อง หรือเลขทิกเก็ต", "เรื่อง")
             + wide_table(
                 "<th>เรื่องใน Slack</th><th>Slack ว่า</th><th>youtrack</th><th>บอร์ดว่า</th>"
                 "<th>ผลเทียบ</th><th>ข้อความที่อ้างเลข</th><th>ข้อความทั้งเรื่อง</th>",
                 matched_rows,
                 least=780,
             )
             + missing_block)
            if matched_rows or missing_block
            else f'<div class="empty">{nothing("ยังไม่มีเรื่องไหนที่พิมพ์เลขทิกเก็ตไว้")}</div>',
            title=f"เรื่องที่จับคู่กับทิกเก็ตได้ ({len(matched)})",
            note="แต่ละแถวคือเรื่องใน Slack ที่ชื่อตรงกับเลขทิกเก็ตใบหนึ่ง — “ผลเทียบ” มีสามค่า: "
            "ขัดกัน (แดง) คือขึ้นอยู่ในหัวข้อแรกของหน้านี้, ตรงกัน (เขียว) คือเทียบแล้วเล่าเหมือนกัน, "
            "และ เทียบไม่ได้ คือไม่มีใครพิมพ์เลขทิกเก็ตนั้นในแชทเลย จึงไม่มีข้อความให้เทียบกับสถานะในบอร์ด",
        ),
        section(
            how(
                "การจับคู่ใช้เลขทิกเก็ตที่คนพิมพ์ไว้เท่านั้น ไม่จับคู่จากความคล้ายของคำ เพราะถ้าเดาคู่ผิด "
                "ระบบจะรายงาน “ขัดกัน” ของสองสิ่งที่ไม่เกี่ยวกันเลย — ในช่วงนี้มี "
                f'{coverage.get("topics", 0)} เรื่อง มีเลขทิกเก็ตอยู่ในชื่อ {coverage.get("with_ticket_key", 0)} เรื่อง '
                f'และหาเจอในบอร์ด {coverage.get("matched_in_youtrack", 0)} เรื่อง '
                "ส่วนที่เหลือไม่ได้ถูกเทียบเลย การที่มันไม่โผล่ในหัวข้อ “ไม่ตรงกัน” จึงไม่ได้แปลว่าตรงกัน"
            ),
            note="",
        ),
    ]
    return render("เทียบกับทิกเก็ต", tiles, sections, f"ข้อมูล ณ {build.built_at}", current="/tracker", build=build, hero=cat("tracker", eyes="squint"))


def ticket_link(record: dict[str, Any]) -> str:
    """A ticket's own link, for records that have no Slack permalink and never will.

    Without this a merged tracker reads as a wall of text the reader cannot follow to
    the source, which is the one thing every other record on this page offers.
    """
    url = str(record.get("youtrack_url") or "").strip()
    if not url:
        return ""
    key = esc(str(record.get("youtrack_key") or "ticket"))
    state = str(record.get("youtrack_state") or "").strip()
    tail = f" · {esc(state)}" if state else ""
    return f' <a href="{esc(url)}" target="_blank" rel="noopener">{key}{tail} ↗</a>'


def linked_elsewhere(build: State, digest: Digest, topic: Topic) -> list[dict[str, str]]:
    """Messages a person linked to this ticket that the clustering put somewhere else.

    An override tells the linker what a message's work item is; it does not move the
    message, because membership is Louvain's and this page does not re-cluster. So a
    person can link five messages to a ticket and find two of them here — which is
    what happened, and which looked exactly like the link having failed.

    Naming where each one did land is the part that makes it actionable rather than
    alarming: "in another item" is a thing to go and read, "missing" is not.
    """
    ticket = str(topic.item_id or "").upper()
    if not ticket:
        return []
    mine = {str(record["id"]) for record in topic.records}
    home = {
        str(record["id"]): other
        for other in digest.topics
        for record in other.records
    }
    texts = {str(record["id"]): str(record.get("text", "")) for record in build.records}
    strays: list[dict[str, str]] = []
    for record_id, key in sorted(build.link_overrides.items()):
        if item_key(key).upper() != ticket or record_id in mine or record_id not in texts:
            continue
        other = home.get(record_id)
        strays.append({
            "record_id": record_id,
            "text": clean(texts[record_id], 200),
            "where": other.item_id if other else "",
            "where_key": str(other.item_id or other.key) if other else "",
        })
    return strays


@app.get("/item/{key}", response_class=HTMLResponse)
def item_page(key: str) -> HTMLResponse:
    build, digest = require_build()
    topic = find_topic(digest, key)
    summary = build.summary_for(topic.key)

    events = timeline(topic, build.records)
    rows = "".join(
        f'<div class="ev"><div class="head"><span class="rel">{esc(RELATION_LABEL.get(event["relation"], event["relation"]))}</span>'
        f'<span class="when">{esc(event["when"])}</span></div>'
        f'<div class="msg"><span class="who">{esc(event["from_user_name"])}</span>'
        f'<span>{esc(names().in_text(event["from_text"]))}</span></div>'
        f'<div class="msg"><span class="who">↳ {esc(event["to_user_name"])}</span>'
        f'<span>{esc(names().in_text(event["to_text"]))}</span></div>'
        f'<p class="meta">{esc(event["evidence"])}'
        + (f' · ตอบข้อความก่อนหน้าอีก {event["also_answers"]} ข้อความ' if event.get("also_answers") else "")
        + "</p></div>"
        for event in events
    )

    # Grouped by day: a flat list of ninety messages reads as one block, and the thing
    # a reader is looking for in a work item is almost always "what happened when".
    days: list[tuple[str, list[str]]] = []
    for record in sorted(topic.records, key=lambda item: timestamp(item) if np.isfinite(timestamp(item)) else 0.0):
        stamp = format_timestamp(str(record.get("ts", "")))
        day, _, clock = stamp.partition(" ")
        line = (
            f'<div class="msg"><span class="who"><span class="tag">'
            f'{esc(SOURCE_LABEL.get(str(record.get("source") or "slack"), "Slack"))}</span>'
            f'{esc(clock)} {esc(names().of(record.get("user")) or "-")}</span>'
            f'<span>{esc(clean(record["text"], 320))}{ticket_link(record)}</span></div>'
        )
        if days and days[-1][0] == day:
            days[-1][1].append(line)
        else:
            days.append((day, [line]))
    every = "".join(f'<p class="day">{esc(day)}</p>' + "".join(lines) for day, lines in days)

    tiles = [
        stat_tile("สถานะ", STATE_STYLE.get(topic.state, ("", topic.state))[1], age_phrase(topic)),
        stat_tile("ข้อความ", str(len(topic.records)), source_summary(topic)),
        stat_tile("คนเกี่ยวข้อง", str(len(topic.participants)), ", ".join(topic.participant_names[:3])),
        stat_tile("เหตุการณ์", str(len(events)), "ครั้งที่ข้อความหนึ่งตอบอีกข้อความ"),
    ]
    title = card_title(topic, summary)
    head = [f'<div class="card-head">{state_chip(topic.state)}<h3>{esc(title)}</h3></div>']
    if topic.label:
        head.append(
            '<div class="keys">' + "".join(f"<span>{esc(word.strip())}</span>" for word in topic.label.split(",") if word.strip()) + "</div>"
        )
    if summary and summary.detail:
        head.append(f'<p class="detail">{esc(summary.detail)}</p>')
    if summary and summary.next_step:
        head.append(f'<p class="next">→ {esc(summary.next_step)}</p>')
    if topic.evidence:
        head.append(f'<p class="meta">ทำไมถึงเป็นสถานะนี้: {esc(topic.evidence)}</p>')

    sections = [
        section(f'<article class="card {esc(topic.state)}">' + "".join(head) + "</article>", title="เรื่องนี้คืออะไร"),
        section(
            f'<div class="tl">{rows}</div>' if rows else f'<div class="empty">{nothing("ยังไม่พบข้อความที่ตอบกันตรง ๆ ในเรื่องนี้")}</div>',
            title="เกิดอะไรขึ้นบ้าง ตามลำดับ",
            note="แต่ละแถวคือข้อความหนึ่งที่ตอบอีกข้อความหนึ่ง และตอบแบบไหน — ถาม ตอบ แจ้งว่าติด หรือบอกว่าแก้แล้ว",
        ),
        section(every, title=f"ทุกข้อความในเรื่องนี้ ({len(topic.records)})", note="เรียงตามเวลา แบ่งตามวัน"),
    ]
    strays = linked_elsewhere(build, digest, topic)
    if strays:
        rows_out = "".join(
            f'<div class="msg"><span class="who">{esc(stray["record_id"][:28])}</span>'
            f'<span>{esc(stray["text"])}'
            + (
                f' <a href="/item/{esc(stray["where_key"])}">อยู่ในเรื่อง: {esc(stray["where"])}</a>'
                if stray["where_key"]
                else " <i>ตอนนี้ไม่ได้อยู่ในเรื่องไหนเลย</i>"
            )
            + "</span></div>"
            for stray in strays
        )
        sections.append(
            section(
                '<div class="alert"><b>ข้อความพวกนี้ถูกกดเชื่อมกับทิกเก็ตนี้ แต่ไม่ได้ถูกนับรวมข้างบน</b><br>'
                "การกดเชื่อมบอกระบบว่าข้อความนี้เป็นของงานไหน แต่ไม่ได้ย้ายมันออกจากกลุ่มที่การจัดกลุ่มวางไว้ "
                "ตัวเลข “ข้อความ” ด้านบนจึงยังไม่รวมพวกนี้</div>" + rows_out,
                title=f"เชื่อมไว้กับทิกเก็ตนี้ แต่ไปอยู่เรื่องอื่น ({len(strays)})",
                note="ถ้าอันไหนควรอยู่ในเรื่องนี้จริง ๆ ให้ดูว่ามันไปอยู่เรื่องไหน แล้วตัดสินว่าสองเรื่องนั้นคือเรื่องเดียวกันไหม",
            )
        )
    actions = f'<a class="ghost" href="/search?q={quote_plus(title[:80])}" style="text-decoration:none">ค้นหาเรื่องคล้ายกัน</a>'
    return render(
        title[:80],
        tiles,
        sections,
        f"รหัสเรื่อง {topic.item_id} · {build.days:g} วันล่าสุด",
        current="",
        build=build,
        actions=actions,
        hero=cat("clock"),
    )


#: The relation names come from the analysis layer, where they are English identifiers.
#: On the page they are read by people who never see that layer.
RELATION_LABEL = {
    "resolves": "แก้ให้แล้ว",
    "blocked_by": "ติดอยู่ที่",
    "duplicates": "เรื่องเดียวกับ",
    "answers": "ตอบคำถาม",
    "follows_up": "ตามต่อจาก",
    "same_topic": "เรื่องเดียวกัน",
}


def score_bar(label: str, value: float, *, dim: bool = False) -> str:
    width = max(0.0, min(1.0, value)) * 100
    return (
        f'<div><div class="k"><span>{esc(label)}</span><span>{value:.2f}</span></div>'
        f'<div class="meter{" dim" if dim else ""}"><i style="width:{width:.0f}%"></i></div></div>'
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(q: str = Query(default=""), k: int = Query(default=10, ge=1, le=50)) -> HTMLResponse:
    build = live()
    options = "".join(
        f'<option value="{size}"{" selected" if size == k else ""}>{size} ผลลัพธ์</option>' for size in (5, 10, 20, 50)
    )
    form = (
        '<form class="search" method="get" action="/search">'
        f'<input type="text" name="q" value="{esc(q)}" autofocus '
        'placeholder="วางโน้ตประชุม หรือพิมพ์สิ่งที่อยากหา เช่น รอ api ของ event">'
        f'<select name="k">{options}</select>'
        "<button type=\"submit\">ค้นหา</button></form>"
    )

    body = form
    tiles = [
        stat_tile("ข้อความที่ค้นได้", f"{len(build.records):,}", "Slack + ประชุม + youtrack"),
        stat_tile("ผลลัพธ์", "-", "ยังไม่ได้ค้น"),
    ]
    # Answered before the ranking, because it is a different question. "REVERAPP-250"
    # asks whether that work item exists, and the ranker can only answer which message
    # is most like the string — so on an item whose key nobody typed, a perfectly
    # correct search reported nothing while the item sat in the digest.
    named = items_named(build.digest, q) if q.strip() and build.digest else []
    named_block = "".join(
        topic_card(topic, build.summary_for(topic.key), window_start(build.days), rank, show_messages=3)
        for rank, topic in enumerate(named, start=1)
    )
    if q.strip():
        if build.retriever is None:
            raise HTTPException(status_code=503, detail="Index is still building.")
        relevance = build.retriever.relevance(q)
        hits = build.retriever.rank(q, top_k=k)
        # Both signals near zero is the shape of a query the corpus has no answer to:
        # the ranker still returns k rows, because ranking answers "which of these is
        # best" and cannot answer "is any of them anything". Saying so is the whole
        # difference between a result and a plausible-looking wrong one.
        # Two signals that fail in opposite directions, reported as the one thing a
        # reader wants from them: is this in here at all. The numbers stay, in the note.
        #
        # Note the asymmetry — a high `dense` on its own is NOT evidence. Max cosine
        # over N documents rises with N, so on a corpus this size something always
        # looks similar and nonsense scores as well as a real question; Retriever
        # .relevance documents the measurement. `lexical` is the half that can say no,
        # because gibberish shares no vocabulary with anything. So confidence is high
        # only when both fire, and lexical alone decides whether to warn.
        lexical_hit, dense_hit = relevance["lexical"] > 0.01, relevance["dense"] >= 0.5
        confidence, why_confident = {
            (True, True): ("สูง", "เจอทั้งคำที่พิมพ์ตรง ๆ และเรื่องที่ความหมายใกล้กัน"),
            (True, False): ("ปานกลาง", "มีคำที่ตรงกัน แต่ความหมายไม่ได้ใกล้เป็นพิเศษ"),
            (False, True): ("ต่ำ", "ไม่มีคำไหนในคำค้นตรงกับข้อความจริงเลย"),
            (False, False): ("ต่ำ", "ไม่มีคำตรงและความหมายก็ไม่ใกล้ — น่าจะไม่มีเรื่องนี้"),
        }[(lexical_hit, dense_hit)]
        tiles = [
            stat_tile("ผลลัพธ์", str(len(hits)), "เรียงจากตรงที่สุด"),
            stat_tile("ความมั่นใจ", confidence, why_confident),
            stat_tile("ข้อความที่ค้นได้", f"{len(build.records):,}", "Slack + ประชุม + youtrack"),
        ]
        # `named` is a whole-token match on a work item id, so it is direct evidence
        # the corpus holds this — which outranks a BM25 score of zero on the same words.
        if not lexical_hit and not named:
            body += (
                '<div class="alert"><b>น่าจะไม่มีเรื่องนี้ในข้อมูลที่เก็บไว้</b><br>'
                "ไม่มีคำไหนในคำค้นตรงกับข้อความจริงสักคำ ระบบจึงเดาจากความหมายอย่างเดียว "
                "ซึ่งจะเจอ “อะไรสักอย่าง” เสมอไม่ว่าพิมพ์อะไรลงไป — "
                "ผลด้านล่างคือสิ่งที่ใกล้ที่สุดเท่าที่มี อาจไม่เกี่ยวเลยก็ได้</div>"
            )
        owner = {
            str(record["id"]): topic
            for topic in (build.digest.topics if build.digest else [])
            for record in topic.records
        }
        for hit in hits:
            topic = owner.get(hit.record_id)
            item = (
                f' <a href="/item/{esc(topic.item_id or topic.key)}">อยู่ในเรื่อง: {esc(card_title(topic, build.summary_for(topic.key))[:44])}</a>'
                if topic
                else ""
            )
            parts = "".join(
                score_bar(SCORE_LABEL.get(name, name), value, dim=name not in ("dense", "bm25"))
                for name, value in sorted(hit.parts.items())
            )
            # A message reachable by a ticket key it never types is reachable because a
            # person said so. Saying which person's act put it here keeps the promise
            # that every result can be checked — otherwise the matched term is a word
            # the reader will look for in the text and not find.
            linked_by = build.retriever.linked_ticket.get(hit.record_id, "")
            link_note = (
                f'<p class="meta">เจอได้เพราะมีคนกดเชื่อมข้อความนี้ไว้กับ {esc(linked_by)} '
                "— ตัวข้อความเองไม่ได้พิมพ์รหัสนี้</p>"
                if linked_by and linked_by.lower() not in str(hit.record.get("text", "")).lower()
                else ""
            )
            body += (
                f'<div class="hit"><div class="n">{hit.rank}</div><div>'
                f'<div class="head"><span class="tag">{esc(SOURCE_LABEL.get(str(hit.record.get("source") or "slack"), "Slack"))}</span>'
                f'<span class="meta" style="margin:0">{esc(names().of(hit.record.get("user")) or "-")} · '
                f'{esc(format_timestamp(str(hit.record.get("ts", ""))))}</span>'
                f"{ticket_link(hit.record)}{item}</div>"
                f'<p class="text">{highlight(clean(hit.record["text"], 420), hit.terms)}</p>'
                f'<details class="more"><summary>ทำไมถึงเจออันนี้</summary>'
                f'<p class="meta">คะแนนรวม {hit.score:.3f}'
                + (f' · คำที่ตรง: {esc(", ".join(hit.terms[:8]))}' if hit.terms else " · ไม่มีคำตรง เจอจากความหมายล้วน ๆ")
                + f'</p>{link_note}<div class="why">{parts}</div></details>'
                "</div></div>"
            )
        if not hits:
            body += f'<div class="empty">{nothing("ไม่พบอะไรเลย ลองใช้คำอื่นดู")}</div>'

    sections = [
        section(
            body,
            title="หาว่าเรื่องนี้เคยคุยกันไว้ที่ไหน",
            note="วางบันทึกประชุมหรือพิมพ์สิ่งที่อยากหาลงไป แล้วระบบจะหาข้อความจริงที่พูดเรื่องเดียวกัน "
            "พิมพ์ไทยหรืออังกฤษก็ได้ และไม่ต้องใช้คำเดียวกับต้นฉบับ",
        ),
    ]
    if named_block:
        sections.insert(
            0,
            section(
                named_block,
                title=f"ตรงกับเรื่องที่มีอยู่แล้ว ({len(named)})",
                note="คำที่พิมพ์มาเป็นรหัสเรื่อง จึงเปิดเรื่องนั้นให้เลย ไม่ต้องรอว่าจะมีข้อความไหนพิมพ์รหัสนี้ไว้บ้าง",
            ),
        )
    sections += [
        section(
            how(
                "ระบบค้นสองแบบพร้อมกันแล้วรวมผล — แบบแรกหาคำที่ตรงกันตรง ๆ แบบที่สองเทียบความหมายของประโยค "
                "จึงเจอได้แม้ใช้คนละคำ ตัวเลขสองค่าบนหัวหน้าคือความมั่นใจว่ามีเรื่องนี้อยู่จริงไหม "
                "ไม่ใช่แค่ว่าอันไหนดีที่สุดในผลที่ได้มา"
            ),
            note="",
        ),
    ]
    return render("ค้นหา", tiles, sections, f"ค้นจาก {len(build.records):,} ข้อความ", current="/search", build=build, hero=cat("search", eyes="open"))


#: The pipeline's stage names, as a reader would describe what each stage did.
SCORE_LABEL = {"dense": "ความหมายใกล้กัน", "bm25": "คำตรงกัน", "rerank": "จัดอันดับซ้ำ", "time": "ความสดใหม่", "hub": "ปรับความถี่"}


@app.get("/upload", response_class=HTMLResponse)
def upload_page() -> HTMLResponse:
    return upload_screen()


def upload_screen(preview: str = "") -> HTMLResponse:
    """The three ways something that was not exported gets into the corpus.

    `preview` is the pasted chat read back before it is stored, and it is the reason
    this is a function rather than a route: the paste parser is heuristic, so the
    person pasting has to see what it made of their text while they can still fix it.
    """
    build = live()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    today = datetime.now().strftime("%Y-%m-%d")
    # Notes first and transcript second, in that order on purpose: this team rarely has a
    # recording. What actually happens is that somebody writes the notes by hand and posts
    # them into Slack, so the paste box is the common path and the file picker is the
    # exception. The form that gets used should not be the one underneath.
    notes = (
        '<form method="post" action="/upload/notes">'
        '<textarea name="notes" rows="9" required '
        'placeholder="วางโน้ตที่จดไว้เลย เช่น&#10;&#10;*Sprint planning 19 ส.ค.*&#10;'
        '• Pending Mild - list all field&#10;• Tat จะขึ้น my vehicle พรุ่งนี้&#10;'
        '• รอ api จากพี่มอสก่อน ถึงจะต่อได้"></textarea>'
        '<div class="row">'
        '<label class="field">เรื่องอะไร (ไม่ใส่ก็ได้)<input type="text" name="title" placeholder="เช่น Sprint planning"></label>'
        '<label class="field">ใครจด<input type="text" name="author" placeholder="ชื่อ หรือ Slack id"></label>'
        f'<label class="field">ประชุมเมื่อไหร่<input type="datetime-local" name="when" value="{esc(now)}"></label>'
        f'<input type="hidden" name="token" value="{esc(admin_token)}">'
        "<button type=\"submit\">เพิ่มเข้าระบบ</button>"
        "</div></form>"
        '<p class="meta">วางหนึ่งครั้ง = <strong>หนึ่งบันทึก</strong> เหมือนโพสต์เดียวใน Slack ไม่ต้องแยกเป็นบรรทัด '
        'บรรทัดที่ขึ้นต้นว่า <code>Pending …</code> หรือมีคำว่า <code>รอ</code> จะถูกจับเป็นงานที่ติด '
        "และจะอ้างบรรทัดนั้นเป็นหลักฐานให้เอง</p>"
        '<p class="meta">วางข้อความเดิมซ้ำในวันเดียวกัน = <strong>แทนที่ของเดิม</strong> ไม่ได้เพิ่มอันใหม่ — แก้แล้ววางใหม่ได้เลย</p>'
    )
    # Copy out of Slack and paste. The conversations that decide things happen in DMs and
    # private groups the export token never reaches, and this is the only way in.
    chat = (
        '<form method="post" action="/upload/slack">'
        '<textarea name="chat" rows="9" required '
        'placeholder="เปิดแชทใน Slack → ลากเลือกข้อความ → copy → วางตรงนี้ เช่น&#10;&#10;'
        'Aim Sirawith  [2:21 PM]&#10;พี่มอสเคยแก้ให้ผมที่ dev&#10;[2:22 PM]ของผมสุดท้ายคือแตก&#10;'
        'jah natta  [2:22 PM]&#10;ของพี่ไม่แตกหวะ"></textarea>'
        '<div class="row">'
        '<label class="field">คุยที่ไหน<input type="text" name="title" required placeholder="เช่น DM พี่ Natta"></label>'
        f'<label class="field">วันแรกของบทสนทนา<input type="date" name="day" value="{esc(today)}"></label>'
        f'<input type="hidden" name="token" value="{esc(admin_token)}">'
        '<button type="submit" name="action" value="preview">ดูก่อนว่าอ่านถูกไหม</button>'
        "</div></form>"
        '<p class="meta">Slack copy มาเป็น <code>ชื่อ  [2:21 PM]</code> แล้วขึ้นบรรทัดใหม่เป็นเนื้อความ '
        'ระบบอ่านรูปแบบนี้ได้ตรง ๆ — ของแถมที่ติดมาอย่าง <code>6 replies</code> ชื่อไฟล์รูป หรือ <code>(edited)</code> ถูกตัดให้เอง</p>'
        '<p class="meta">ข้อความที่คนเดียวกันพิมพ์ติด ๆ กันภายใน 2 นาที = <strong>หนึ่งบันทึก</strong> '
        'เพราะ "ของผมสุดท้ายคือแตก" แล้วต่อด้วย "400" คือประโยคเดียวที่กด Enter คั่น</p>'
        '<p class="meta">ในคลิปบอร์ดมีแต่เวลา ไม่มีวันที่ จึงต้องบอกว่าเป็นวันไหน · '
        'วางซ้ำข้อความเดิม = <strong>แทนที่ของเดิม</strong> วางทับกันได้ ไม่ต้องจำว่าวางถึงไหนแล้ว</p>'
    )
    transcript = (
        '<form method="post" action="/upload" enctype="multipart/form-data">'
        '<div class="row">'
        '<input type="file" name="transcript" accept=".vtt,.srt,.txt,.json" required>'
        '<label class="field">ชื่อการประชุม<input type="text" name="title" placeholder="เช่น Weekly sync"></label>'
        f'<label class="field">เริ่มประชุมเมื่อไหร่<input type="datetime-local" name="started" value="{esc(now)}"></label>'
        f'<input type="hidden" name="token" value="{esc(admin_token)}">'
        "<button type=\"submit\">เพิ่มเข้าระบบ</button>"
        "</div></form>"
        '<p class="meta">รองรับไฟล์ซับจาก Zoom / Meet / Teams (.vtt, .srt), ไฟล์ข้อความแบบ "ชื่อ: ข้อความ" (.txt) '
        "และ JSON จากตัวถอดเสียง · เวลาเริ่มประชุมใช้ระบุว่าเป็นประชุมไหน ถ้าใส่ชื่อและเวลาเดิมจะแทนที่ของเดิม</p>"
    )
    sections = [
        section(
            notes,
            title="วางโน้ตที่จดด้วยมือ",
            note="ทางที่ใช้กันจริงคือทางนี้ — โน้ตที่วางจะกลายเป็นข้อมูลแบบเดียวกับข้อความ Slack "
            "แล้วถูกจัดกลุ่ม ค้นหา และเชื่อมกับบทสนทนาเดิมได้ทันที ระบบจะอ่านใหม่ทั้งชุดให้เอง (ใช้เวลาสักครู่)",
        ),
        section(
            chat,
            title="หรือ copy แชทจาก Slack มาวาง",
            note="สำหรับห้องที่ดึงอัตโนมัติไม่ได้ — DM, กลุ่มส่วนตัว, workspace อื่น "
            "วางแล้วจะกลายเป็นข้อความรายอันเหมือนที่ export มา ไม่ใช่ก้อนเดียว",
        ),
        section(transcript, title="หรือถ้ามีไฟล์ถอดเสียงจากการประชุม"),
    ]
    if preview:
        sections.insert(0, preview)
    return render("เพิ่มโน้ต", [], sections, "โน้ต / แชท / ประชุม → เข้าระบบ", current="/upload", build=build, hero=cat("note", eyes="open"))


@app.get("/api/people")
def api_people() -> JSONResponse:
    """The people view as data, so the bot can ask the same question the page answers."""
    _, digest = require_build()
    return JSONResponse({"people": people_rows(digest)})


@app.get("/api/person/{user}")
def api_person(user: str) -> JSONResponse:
    """One person as data — the same slice of the build the page renders."""
    build, digest = require_build()
    person = person_detail(build, digest, find_person(digest, user))
    return JSONResponse({
        "user": person["user"],
        "name": person["name"],
        "items": len(person["topics"]),
        "blocked": len(person["blocked"]),
        "messages": len(person["messages"]),
        "sources": person["sources"],
        "last_spoke": format_timestamp(str(person["last_ts"])) if np.isfinite(person["last_ts"]) else "",
        "last_spoke_ts": person["last_ts"] if np.isfinite(person["last_ts"]) else None,
        "topics": [
            {**topic.as_dict(), "title": card_title(topic, build.summary_for(topic.key))}
            for topic in person["topics"]
        ],
        "partners": person["partners"],
        "recent": [
            {
                "id": str(record.get("id") or ""),
                "item_id": record.get("item_id") or "",
                "item_title": record.get("item_title") or "",
                "when": format_timestamp(str(record.get("ts", ""))),
                "source": record.get("source"),
                "text": record.get("text") or "",
            }
            for record in person["messages"][:PERSON_MESSAGES]
        ],
    })


@app.post("/upload/notes")
def upload_notes(
    request: Request,
    notes: str = Form(...),
    title: str = Form(default=""),
    author: str = Form(default=""),
    when: str = Form(default=""),
    token: str = Form(default=""),
    x_tam_token: str = Header(default=""),
) -> RedirectResponse:
    """Ingest a typed note, then rebuild. Sync for the same reason as the transcript route."""
    check_origin(request)
    check_token(token or x_tam_token)
    moment = parse_started(when) if when.strip() else datetime.now(tz=timezone.utc)
    try:
        record = note_record(notes, title=title, author=author, when=moment)
    except ValueError as error:  # their paste, their fault: a 400, not a 500
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        with building():  # held across the write and the rebuild, as /upload does
            total, replaced = merge_note(record, live().records_path)
            log.info("Note %s %s — corpus holds %d record(s)", record["id"], "replaced" if replaced else "added", total)
            try:
                rebuild()
            except (TamDataError, SystemExit) as error:
                raise HTTPException(status_code=500, detail=f"Ingested, but the rebuild failed: {error}") from error
    except TamDataError as error:
        raise HTTPException(status_code=500, detail=f"Cannot update the corpus: {error}") from error
    return RedirectResponse(url="/", status_code=303)


def parse_started(value: str) -> datetime:
    """The meeting start as the browser meant it.

    A `datetime-local` field is the operator's own wall clock, so a bare value means
    *here* — where meetings.parse_timestamp reads one as UTC, which is what its
    --started flag documents. Reading the form's own prefill as UTC stored every
    uploaded meeting the machine's offset into the future: far enough that the
    temporal edge to its Slack counterpart decayed away, and far enough to fall
    outside the digest window it belongs to. The zone is the offset in effect now,
    which is the right reading for a field prefilled with today's wall clock.
    """
    try:
        return parse_iso(value, assume_tz=datetime.now().astimezone().tzinfo or timezone.utc)
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail=f"{value!r} is not an ISO timestamp (try 2026-08-14T09:30)."
        ) from error


def paste_day(value: str) -> date:
    """The day the pasted conversation starts.

    Slack's clipboard carries clocks and no dates, so this is not a nicety: without it
    every paste would land on the day it was pasted, and a chat copied on Monday about
    Friday's outage would sit a weekend away from the messages it belongs with.
    """
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"{value!r} ไม่ใช่วันที่ (ต้องเป็นแบบ 2026-08-19)") from error


def paste_preview(
    parse: PasteParse, records: Sequence[dict[str, Any]], *, chat: str, title: str, day: date
) -> str:
    """What the parser made of a paste, before any of it is stored.

    The paste parser is a heuristic over a format nobody documents, so the failure that
    matters is a silent one: a mis-read paste looks exactly like a short conversation.
    Everything it did is shown — including what it could not attribute to anyone — and
    the corpus is not touched until somebody looks at this and says yes.
    """
    rows = "".join(
        "<tr><td>{when}</td><td>{who}</td><td>{text}</td></tr>".format(
            when=esc(format_timestamp(str(record.get("ts", "")))),
            who=esc(names().of(record.get("user")) or "-"),
            text=esc(clean(record.get("text"), 220)),
        )
        for record in records
    )
    table = wide_table("<th>เมื่อไหร่</th><th>ใคร</th><th>ข้อความ</th>", rows, least=520)
    skipped = ""
    if parse.skipped:
        items = "".join(f"<li>{esc(clean(line, 160))}</li>" for line in parse.skipped[:5])
        skipped = (
            f'<p class="meta">ข้าม {len(parse.skipped)} ก้อนที่ไม่รู้ว่าใครพูด '
            "(อยู่ก่อนข้อความแรก หรืออ่านหัวข้อความไม่ออก) — ถ้าอันไหนสำคัญ ให้ copy ใหม่โดยเริ่มที่ชื่อคนพูด</p>"
            f'<ul class="meta">{items}</ul>'
        )
    confirm = (
        '<form method="post" action="/upload/slack">'
        f'<textarea name="chat" hidden>{esc(chat)}</textarea>'
        f'<input type="hidden" name="title" value="{esc(title)}">'
        f'<input type="hidden" name="day" value="{esc(day.isoformat())}">'
        f'<input type="hidden" name="token" value="{esc(admin_token)}">'
        '<button type="submit" name="action" value="add">ใช่ เพิ่มเข้าระบบเลย</button>'
        "</form>"
    )
    note = (
        f"อ่านได้ {len(parse.messages)} ข้อความ จาก {len(parse.speakers)} คน → เก็บเป็น {len(records)} บันทึก "
        f"({', '.join(parse.speakers)}) · ยังไม่ได้เขียนอะไรลง corpus จนกว่าจะกดยืนยัน"
    )
    return section(table + skipped + confirm, title=f"อ่าน “{title}” ได้แบบนี้", note=note)


@app.post("/upload/slack")
def upload_slack(
    request: Request,
    chat: str = Form(...),
    title: str = Form(default=""),
    day: str = Form(default=""),
    action: str = Form(default="preview"),
    token: str = Form(default=""),
    x_tam_token: str = Header(default=""),
) -> Response:
    """Read a conversation copied out of Slack, show it back, and on a second press store it.

    Annotated as the base `Response` and not as the union it really returns: FastAPI
    builds a response model out of the annotation, and a union of two response classes
    is not something it can build one from.

    Sync for the same reason as the two routes around it: the rebuild blocks for seconds
    and would stall the event loop for every other request.
    """
    check_origin(request)
    check_token(token or x_tam_token)
    conversation = title.strip() or "แชทที่วาง"
    started = paste_day(day) if day.strip() else datetime.now().astimezone().date()
    # The operator's own wall clock, as with a meeting's datetime-local field: the times
    # in the paste are what their Slack showed them, not UTC.
    zone = datetime.now().astimezone().tzinfo or timezone.utc
    parse, records = read_paste(chat, title=conversation, day=started, tz=zone)
    if not parse.messages:
        raise HTTPException(
            status_code=400,
            detail="อ่านไม่เจอข้อความสักอัน — รูปแบบที่รองรับคือ 'ชื่อ  [2:21 PM]' แล้วขึ้นบรรทัดใหม่เป็นเนื้อความ",
        )
    if action != "add":
        return upload_screen(paste_preview(parse, records, chat=chat, title=conversation, day=started))
    if not records:
        raise HTTPException(status_code=400, detail="ทุกข้อความถูกกรองว่าไม่มีเนื้อหา (เช่น มีแต่ ok / อีโมจิ)")
    try:
        with building():  # held across the write and the rebuild, as the routes above do
            total, replaced = merge_paste(records, live().records_path)
            log.info(
                "Pasted chat %r: %d record(s), %d replaced — corpus holds %d",
                conversation, len(records), replaced, total,
            )
            try:
                rebuild()
            except (TamDataError, SystemExit) as error:
                raise HTTPException(status_code=500, detail=f"Ingested, but the rebuild failed: {error}") from error
    except TamDataError as error:
        raise HTTPException(status_code=500, detail=f"Cannot update the corpus: {error}") from error
    return RedirectResponse(url="/", status_code=303)


@app.post("/upload")
def upload_meeting(
    request: Request,
    transcript: UploadFile = File(...),
    title: str = Form(default=""),
    started: str = Form(default=""),
    token: str = Form(default=""),
    x_tam_token: str = Header(default=""),
) -> RedirectResponse:
    """Ingest a transcript into the live corpus, then rebuild the index.

    Deliberately `def` and not `async def`: this blocks for seconds — embedding, then
    summarising — and on the event loop that stalls every other request for as long
    as it runs. Starlette hands a sync endpoint to the threadpool instead.
    """
    check_origin(request)
    check_token(token or x_tam_token)
    suffix = Path(transcript.filename or "transcript.txt").suffix or ".txt"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        shutil.copyfileobj(transcript.file, handle)
        temporary = Path(handle.name)
    try:
        meeting_title = title.strip() or Path(transcript.filename or "meeting").stem
        start = parse_started(started) if started.strip() else datetime.now(tz=timezone.utc)
        try:
            utterances = merge_utterances(parse_transcript(temporary))
        except TamDataError as error:  # their file, their fault: a 400, not a 500
            raise HTTPException(status_code=400, detail=str(error)) from error
        if not utterances:
            raise HTTPException(status_code=400, detail="No utterances found in that transcript.")
        records = to_records(utterances, title=meeting_title, started=start)
        if not records:
            raise HTTPException(status_code=400, detail="Every line was filtered as noise.")
        with building():  # held across the write and the rebuild; rebuild() nests inside
            merge_into(records, live().records_path)
            log.info("Ingested %d record(s) from %s", len(records), transcript.filename)
            try:
                rebuild()  # embeddings are content-hashed, so only the new lines are encoded
            except (TamDataError, SystemExit) as error:  # the corpus is on disk; only the rebuild failed
                raise HTTPException(status_code=500, detail=f"Ingested, but the rebuild failed: {error}") from error
    except TamDataError as error:  # merge_into: the corpus on disk, not the upload
        raise HTTPException(status_code=500, detail=f"Cannot update the corpus: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)
    return RedirectResponse(url="/", status_code=303)


# ---- JSON API --------------------------------------------------------------


@app.get("/api/digest")
def api_digest() -> JSONResponse:
    build, digest = require_build()
    return JSONResponse(
        {
            "built_at": build.built_at,
            # The same instant with its offset. built_at above is wall clock with no zone,
            # so a client in another timezone cannot tell how stale this build really is.
            "built_at_iso": build.built_at_iso,
            "last_error": build.last_error or None,
            "window_days": build.days,
            "summariser": backend_name(),
            "corpus_size": digest.corpus_size,
            "topics": [
                {**topic.as_dict(), "summary": (build.summary_for(topic.key).as_dict() if build.summary_for(topic.key) else None)}
                for topic in digest.topics
            ],
        }
    )


@app.get("/api/blockers")
def api_blockers() -> JSONResponse:
    _, digest = require_build()
    return JSONResponse({"blocked": [topic.as_dict() for topic in digest.blocked]})


@app.get("/api/item/{key}")
def api_item(key: str) -> JSONResponse:
    build, digest = require_build()
    topic = find_topic(digest, key)
    summary = build.summary_for(topic.key)
    return JSONResponse(
        {
            "topic": topic.as_dict(),
            "summary": summary.as_dict() if summary else None,
            "timeline": timeline(topic, build.records),
            "messages": [
                {
                    "id": str(record["id"]),
                    "when": format_timestamp(str(record.get("ts", ""))),
                    "ts": float(record["ts"]) if record.get("ts") else None,  # zone-free 'when' is for reading, not arithmetic
                    "user": record.get("user"),
                    "user_name": names().of(record.get("user")),
                    "source": record.get("source"),
                    "text": record["text"],
                    # A ticket record has no Slack permalink and never will, so without
                    # its own url it renders as text nobody can open — which is the same
                    # as not having merged the tracker in at all.
                    "url": record.get("youtrack_url") or "",
                    "ticket": record.get("youtrack_key") or "",
                    "ticket_state": record.get("youtrack_state") or "",
                }
                for record in topic.records
            ],
            # Messages a person linked to this ticket that the clustering left in
            # another item. Not in `messages`, because they are not in the item — and
            # reported beside it, because a caller counting `messages` as "everything
            # anybody attached to this ticket" would be undercounting silently.
            "linked_elsewhere": linked_elsewhere(build, digest, topic),
        }
    )


@app.get("/api/search")
def api_search(q: str = Query(...), k: int = Query(default=10, ge=1, le=50), preset: str | None = None) -> JSONResponse:
    build = live()
    if not q.strip():
        # /search refuses a blank query; without this the API answers one with k
        # arbitrary nearest neighbours, which read like results and are not.
        raise HTTPException(status_code=400, detail="q must not be empty.")
    if build.retriever is None:
        raise HTTPException(status_code=503, detail="Index is still building.")
    retriever = build.retriever
    if preset and preset != build.preset:
        from tam.retrieval.retrieve import PRESETS

        if preset not in PRESETS:
            raise HTTPException(status_code=400, detail=f"Unknown preset {preset}")
        retriever = retriever.with_config(PRESETS[preset])  # reuses the embedded matrix
    return JSONResponse(
        {
            "query": q,
            "preset": preset or build.preset,
            # Absolute, unlike every `why` below, which is min-max normalised across
            # the result set. A caller deciding whether to report *nothing* has to
            # read these: see Retriever.relevance for why it takes both.
            "relevance": retriever.relevance(q),
            # Work items the query named outright. A caller that reports "nothing
            # found" on an empty `hits` would be wrong whenever this is non-empty:
            # the item is here, it is just not spelled inside any of its messages.
            "items": [
                {
                    "item_id": topic.item_id,
                    "key": topic.key,
                    "state": topic.state,
                    "messages": len(topic.records),
                    "last": format_timestamp(str(topic.last_ts)),
                    "last_ts": topic.last_ts,
                }
                for topic in (items_named(build.digest, q) if build.digest else [])
            ],
            "hits": [
                {
                    "rank": hit.rank,
                    "score": hit.score,
                    "id": hit.record_id,
                    "source": hit.record.get("source"),
                    "user": hit.record.get("user"),
                    "user_name": names().of(hit.record.get("user")),
                    "when": format_timestamp(str(hit.record.get("ts", ""))),
                    "text": hit.record["text"],
                    "why": hit.parts,
                    "terms": hit.terms,
                    # Non-empty when this message matched through a human's ticket
                    # link rather than its own words — see Retriever.index_links.
                    "linked_ticket": retriever.linked_ticket.get(hit.record_id, ""),
                }
                for hit in retriever.rank(q, top_k=k)
            ],
        }
    )


@app.get("/api/tracker")
def api_tracker() -> JSONResponse:
    """Where Slack and the ticket tracker disagree, and which tickets went quiet.

    `error` non-empty means the tracker could not be read — the empty lists beside it are
    "unknown", not "nothing found", and a caller showing this to people has to say so.
    """
    build = live()
    matched, missing = tracker_pairs(build)
    return JSONResponse({
        "coverage": build.tracker_coverage,
        "drift": build.drifts,
        "silent": build.silent,
        # The rows behind the two counts `coverage` reports. `matched` carries the
        # mention count per pair, because that is what decides whether the absence of a
        # drift means "they agree" or "nothing was comparable".
        "open_tickets": build.open_tickets,
        "matched": matched,
        "unmatched": missing,
        # Human ticket links naming a record this corpus does not have. Reported here
        # because they change how the counts above should be read: a link that did
        # nothing leaves a pair uncounted, and nothing else on this endpoint says so.
        "unresolved_links": build.unresolved_links,
        "error": build.tracker_error,
        "built_at": build.built_at,
    })


@app.get("/api/projects")
def api_projects() -> JSONResponse:
    """What each channel is a project of, as `TAM_CHANNEL_PROJECTS` states it.

    Served rather than left to each half's own environment so that the bot, the
    dashboard and the linker cannot end up with three different answers to "is
    #reverapp-qa the same project as #reverapp-dev". The bot reads the same variable
    as a fallback when the pipeline is not configured, and this route is how the two
    are compared when they disagree.
    """
    mapping = channel_projects()
    return JSONResponse({
        "channels": mapping.by_channel,
        "names": mapping.by_name,
        "labels": mapping.labels,
        "projects": mapping.projects(),
    })


@app.get("/api/tickets/search")
def api_ticket_search(
    q: str = Query(default=""),
    project: str = Query(default=""),
    limit: int = Query(default=25, ge=1, le=100),
) -> JSONResponse:
    """Search the tracker itself, which is a different set from the corpus.

    The Slack ticket picker used to offer work items — tickets the corpus had already
    seen somebody mention. That is precisely the wrong set: a ticket already named in
    Slack is already linked, and the one a person is reaching for is the one nobody
    has typed yet. So this goes to YouTrack live rather than to `build.records`, and
    it stays honest about a tracker that is not configured instead of answering with
    an empty list that reads as "no such ticket".
    """
    from tam.ingest.youtrack import YouTrackError, search_issues

    wanted = [part.strip() for part in project.split(",") if part.strip()]
    try:
        issues = search_issues(q, projects=wanted or None, limit=limit)
    except YouTrackError as error:
        return JSONResponse({"issues": [], "error": str(error)}, status_code=503)
    return JSONResponse({
        "query": q,
        "project": wanted,
        "error": "",
        "issues": [
            {
                "key": issue.key,
                "summary": issue.summary,
                "state": issue.state,
                "resolved": issue.resolved,
                "url": issue.url,
                "updated": issue.updated,
            }
            for issue in issues
        ],
    })


@app.post("/api/ticket/{key}/comment", dependencies=[Depends(require_admin)])
def api_ticket_comment(key: str, body: dict[str, Any]) -> JSONResponse:
    """Write one comment on one ticket. Admin-token'd, and off unless YOUTRACK_WRITE=1.

    Behind the admin token for the same reason `/api/reindex` is: this leaves the
    machine and changes something other people can see. The 503 on a refusal is
    deliberate — "this deployment is not configured to write" is a state of the
    server, not a fault in the request, and the caller shows the reason verbatim
    rather than reporting a write that did not happen.
    """
    from tam.ingest.youtrack import YouTrackError, add_comment

    try:
        written = add_comment(key, str(body.get("text") or ""))
    except YouTrackError as error:
        # An empty body is the caller's mistake; everything else is configuration.
        status = 400 if "ว่างเปล่า" in str(error) or "ticket ไหน" in str(error) else 503
        return JSONResponse({"written": False, "error": str(error)}, status_code=status)
    return JSONResponse({"written": True, **written})


@app.post("/api/paste", dependencies=[Depends(require_admin)])
def api_paste(body: dict[str, Any]) -> JSONResponse:
    """Ingest a chat somebody pasted, optionally attaching all of it to one ticket.

    The JSON twin of `/upload/paste`, for the Slack bot. Two differences, and both
    are the point:

    `dry_run` returns what the parser made of the paste without touching the corpus.
    The paste format is a heuristic, and the browser path answers that with a preview
    screen; a modal has no second screen, so the bot previews through this and shows
    the person the parsed messages before anything is stored.

    `link_key` writes the corrections *before* the rebuild, not after. The linker only
    reads overrides when it runs, so writing them afterwards would leave the person
    told their chat is attached to a ticket while the built ledger still says it is
    not — until some later reindex. Ordering it this way means the response can state
    what actually landed.
    """
    chat = str(body.get("chat") or "")
    if not chat.strip():
        raise HTTPException(status_code=400, detail="ไม่มีข้อความที่วางมา")
    title = str(body.get("title") or "").strip() or "แชทที่วาง"
    day = str(body.get("day") or "").strip()
    started = paste_day(day) if day else datetime.now().astimezone().date()
    zone = datetime.now().astimezone().tzinfo or timezone.utc
    parse, records = read_paste(chat, title=title, day=started, tz=zone)
    parsed = [
        {
            "id": str(record["id"]),
            "user": record.get("speaker_name") or names().of(record.get("user")) or record.get("user"),
            "when": format_timestamp(str(record.get("ts", ""))),
            "text": str(record.get("text", "")),
        }
        for record in records
    ]
    if not parse.messages:
        raise HTTPException(
            status_code=400,
            detail="อ่านไม่เจอข้อความสักอัน — รูปแบบที่รองรับคือ 'ชื่อ  [2:21 PM]' แล้วขึ้นบรรทัดใหม่เป็นเนื้อความ",
        )
    if body.get("dry_run"):
        return JSONResponse({
            "stored": False, "channel_id": records[0]["channel_id"] if records else "",
            "records": parsed, "skipped": parse.skipped[:5], "title": title, "day": started.isoformat(),
        })
    if not records:
        raise HTTPException(status_code=400, detail="ทุกข้อความถูกกรองว่าไม่มีเนื้อหา (เช่น มีแต่ ok / อีโมจิ)")

    link_key = str(body.get("link_key") or "").strip().upper()
    linked = 0
    try:
        with building():  # held across both writes and the rebuild, as /upload/paste does
            total, replaced = merge_paste(records, live().records_path)
            if link_key:
                linked = len(records)
                save_overrides(
                    overrides_path(),
                    [
                        {
                            "record_id": str(record["id"]),
                            "key": f"ticket:{link_key}",
                            "by": str(body.get("by") or "slack"),
                            "at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
                        }
                        for record in records
                    ],
                )
            log.info(
                "Pasted chat %r via API: %d record(s), %d replaced, %d linked to %s — corpus holds %d",
                title, len(records), replaced, linked, link_key or "-", total,
            )
            try:
                rebuild()
            except (TamDataError, SystemExit) as error:
                raise HTTPException(status_code=500, detail=f"เก็บข้อความแล้ว แต่ build ใหม่ไม่ผ่าน: {error}") from error
    except ValueError as error:  # save_overrides on a corrupt corrections file
        raise HTTPException(status_code=500, detail=str(error)) from error
    except TamDataError as error:
        raise HTTPException(status_code=500, detail=f"เขียน corpus ไม่ได้: {error}") from error

    # What the *built* ledger says now, which is the only claim worth making: the
    # override file agreeing with itself proves nothing a person can check.
    build = live()
    landed = ""
    counted = 0
    if link_key and build.digest is not None:
        for topic in build.digest.topics:
            if str(topic.item_id).upper() == link_key:
                landed = topic.item_id
                counted = sum(1 for record in topic.records if str(record.get("id")) in {r["id"] for r in parsed})
                break
    return JSONResponse({
        "stored": True,
        "title": title,
        "day": started.isoformat(),
        "channel_id": records[0]["channel_id"],
        "records": parsed,
        "skipped": parse.skipped[:5],
        "replaced": replaced,
        "corpus_size": total,
        "link_key": link_key,
        "linked": linked,
        # Non-empty only when the rebuilt digest really does hold a work item by that
        # key, with that many of these messages in it.
        "item_key": landed,
        "in_item": counted,
        "built_at": build.built_at,
    })


@app.post("/api/reindex", dependencies=[Depends(require_admin)])
def api_reindex() -> JSONResponse:
    try:
        build = rebuild()
    except (TamDataError, SystemExit) as error:  # a bad corpus or an unusable summariser, not a bare 500
        raise HTTPException(status_code=500, detail=f"Rebuild failed: {error}") from error
    return JSONResponse({"built_at": build.built_at, "built_at_iso": build.built_at_iso, "records": len(build.records)})


@app.get("/api/health")
def api_health() -> JSONResponse:
    """Is what we serve the current corpus, and did the last refresh succeed?

    Without this a nightly reindex can fail indefinitely while every page and every
    Slack digest presents the last good build as current: /api/digest answers 200
    with byte-identical content whether the refresh worked or not.
    """
    build = live()
    try:
        stat = build.records_path.stat()
        records_file: dict[str, Any] = {
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).astimezone().isoformat(timespec="seconds"),
            "bytes": stat.st_size,
        }
    except OSError as error:
        records_file = {"error": str(error)}
    healthy = build.digest is not None and not build.last_error
    return JSONResponse(
        {
            "ok": healthy,
            "records_path": str(build.records_path),
            "records_file": records_file,
            "records": len(build.records),
            "topics": len(build.digest.topics) if build.digest else 0,
            "model": model_name(),
            "summariser": backend_name(),
            "preset": build.preset,
            "window_days": build.days,
            "built_at": build.built_at,
            "built_at_iso": build.built_at_iso,
            "rebuilding": _building,
            "last_attempt_at": build.last_attempt_at,
            "last_error": build.last_error or None,
            # Not a failure of the build, so it does not touch `ok` — but a monitor
            # watching only `ok` would never learn that people's corrections stopped
            # landing, which is silent and permanent until somebody looks.
            "unresolved_links": len(build.unresolved_links),
        },
        status_code=200 if healthy else 503,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--days", type=float, default=DEFAULT_WINDOW_DAYS, help=f"Digest window (default {DEFAULT_WINDOW_DAYS})")
    parser.add_argument("--language", default="th", choices=("th", "en"), help="Digest language (default th)")
    parser.add_argument("--preset", default="hybrid", help=f"Search pipeline preset (default hybrid, see tam.retrieval.retrieve; {DEFAULT_PRESET} adds the reranker)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1, this machine only)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    parser.add_argument(
        "--expose",
        action="store_true",
        help="Permit a non-loopback --host. Needs TAM_ADMIN_TOKEN set, because /upload is then reachable from the network",
    )
    return parser.parse_args()


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() in ("localhost", "")


def listen(host: str, port: int) -> socket.socket:
    """Take the port before the expensive build, and name the likely culprit if it is taken.

    Building first and binding second meant a taken port cost a full index build and
    then printed a banner of URLs belonging to the *other* server on that port —
    which is how you end up uploading a transcript into someone else's corpus.
    """
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as error:
        sock.close()
        raise SystemExit(
            f"Cannot bind {host}:{port} — {error}. Another tam.web.server is probably already on that port."
        ) from error
    return sock


def main() -> None:
    global admin_token, state
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()

    configured = os.getenv("TAM_ADMIN_TOKEN", "").strip()
    if not is_loopback(args.host) and not args.expose:
        raise SystemExit(
            f"--host {args.host} would serve the dashboard beyond this machine, and /upload writes the corpus. "
            "Add --expose if that is really what you want."
        )
    if not is_loopback(args.host) and not configured:
        raise SystemExit("--expose needs TAM_ADMIN_TOKEN set, so the write token is one you chose and can hand out.")
    if configured:
        admin_token = configured

    sock = listen(args.host, args.port)  # bind first: a taken port should cost a second, not a build

    state = replace(state, records_path=args.records, days=args.days, language=args.language, preset=args.preset)
    try:
        rebuild()  # fail at startup on a bad corpus, not on the first request
    except TamDataError as error:  # a command line wants the one line, not the traceback
        raise SystemExit(str(error)) from error

    base = f"http://{args.host}:{args.port}"
    print(f"\n  digest    {base}/")
    print(f"  blockers  {base}/blockers")
    print(f"  people    {base}/people")
    print(f"  tracker   {base}/tracker")
    print(f"  search    {base}/search")
    print(f"  add notes {base}/upload")
    print(f"  health    {base}/api/health")
    if configured:
        print("\n  writes    X-TAM-Token: (TAM_ADMIN_TOKEN from the environment)\n", flush=True)
    else:
        # flush: without a tty stdout is block-buffered, and a token nobody can read
        # until the process ends is a token nobody can use.
        print(f"\n  writes    X-TAM-Token: {admin_token}   (set TAM_ADMIN_TOKEN to keep it across restarts)\n", flush=True)
    uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main()
