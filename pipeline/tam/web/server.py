"""The prototype web app: daily digest, blockers, work-item timelines, grounding.

    python3 -m tam.web.server                 # http://127.0.0.1:8000
    python3 -m tam.web.server --records data/processed/combined.json --days 7

Four pages, each backed by a JSON endpoint so a real frontend can replace the
HTML later without touching the logic:

    /                digest      — what moved, blocked items first
    /blockers        blockers    — only what is stuck, longest first
    /item/{key}      timeline    — one work item as dated, typed events
    /search?q=       grounding   — paste a meeting note, find the Slack behind it

`GET /api/health` is the fifth surface and has no page: it reports the build being
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
import secrets
import shutil
import socket
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import numpy as np
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from tam.analysis.digest import DEFAULT_WINDOW_DAYS, Digest, Topic, build_digest, timeline, window_start
from tam.analysis.linker import load_overrides
from tam.analysis.digest import names
from tam.retrieval.embeddings import model_name, quiet_third_party_logs
from tam.ingest.meetings import merge_into, merge_utterances, parse_iso, parse_transcript, to_records
from tam.retrieval.retrieve import DEFAULT_PRESET, Hit, build_retriever
from tam.core import DEFAULT_RECORDS, TamDataError, format_timestamp, read_records
from tam.analysis.summarize import TopicSummary, backend_name, summarize_digest
from tam.report.visualize import GRID, INK, INK_MUTED, INK_SECONDARY, SERIES_1, SERIES_2, SURFACE, build_page, stat_tile

log = logging.getLogger("server")

STATE_STYLE = {
    "blocked": (SERIES_2, "ติดอยู่ / blocked"),
    "resolved": ("#0ca30c", "ปิดแล้ว / resolved"),
    "active": (SERIES_1, "กำลังทำ / active"),
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
    tracker_coverage: dict[str, int] = field(default_factory=dict)
    tracker_error: str = ""

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
        return {"drifts": [], "silent": [], "coverage": {}, "error": str(error)}
    try:
        keys = [topic.item_id for topic in digest.topics if not str(topic.item_id).startswith("c")]
        issues = fetch_by_keys(keys) if keys else []
        every = fetch_project(projects[0]) if projects else []
        return {
            "drifts": [drift.as_dict() for drift in detect(digest.topics, issues)],
            "silent": [quiet.as_dict() for quiet in silent_tickets(every, records)],
            "coverage": {**coverage(digest.topics, issues), "tracker_issues": len(every),
                         "tracker_open": sum(1 for i in every if not i.resolved)},
            "error": "",
        }
    except YouTrackError as error:
        log.warning("Tracker unavailable; serving the Slack half only: %s", error)
        return {"drifts": [], "silent": [], "coverage": {}, "error": str(error)}


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
            digest = build_digest(records, since=window_start(previous.days), overrides=read_overrides())
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
            tracker_coverage=tracker["coverage"],
            tracker_error=tracker["error"],
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


def page_styles() -> str:
    """The few extras build_page does not already provide."""
    return f"""<style>
  nav {{ display: flex; gap: 18px; margin: -12px 0 26px; font-size: 14px; }}
  nav a {{ color: {INK_SECONDARY}; text-decoration: none; border-bottom: 2px solid transparent; padding-bottom: 3px; }}
  nav a:hover, nav a.on {{ color: {INK}; border-bottom-color: {SERIES_1}; }}
  .topic {{ border-left: 3px solid {GRID}; padding: 2px 0 2px 14px; margin: 0 0 22px; }}
  .topic.blocked {{ border-left-color: {SERIES_2}; }}
  .topic.resolved {{ border-left-color: #0ca30c; }}
  .topic.active {{ border-left-color: {SERIES_1}; }}
  .topic h3 {{ margin: 0 0 4px; font-size: 16px; }}
  .topic h3 a {{ color: {INK}; text-decoration: none; }}
  .topic h3 a:hover {{ text-decoration: underline; }}
  .meta {{ font-size: 12px; color: {INK_MUTED}; margin: 0 0 8px; }}
  .detail {{ font-size: 14px; margin: 0 0 8px; }}
  .next {{ font-size: 14px; color: {INK_SECONDARY}; margin: 0 0 8px; }}
  .msg {{ font-size: 13px; color: {INK_SECONDARY}; margin: 3px 0; }}
  .who {{ color: {INK_MUTED}; }}
  .tag {{ font-size: 11px; padding: 1px 6px; border-radius: 4px; border: 1px solid {GRID}; color: {INK_MUTED}; }}
  .warn {{ color: {SERIES_2}; font-size: 12px; }}
  form.search {{ display: flex; gap: 8px; margin: 0 0 20px; }}
  form.search input[type=text] {{ flex: 1; padding: 9px 12px; font-size: 14px; border: 1px solid {GRID};
      border-radius: 8px; background: {SURFACE}; color: {INK}; font-family: inherit; }}
  button {{ padding: 9px 16px; font-size: 14px; border: 0; border-radius: 8px; background: {SERIES_1};
      color: #fff; cursor: pointer; font-family: inherit; }}
  .event {{ display: grid; grid-template-columns: 120px 1fr; gap: 10px; margin: 0 0 14px; font-size: 13px; }}
  .event .when {{ color: {INK_MUTED}; font-variant-numeric: tabular-nums; }}
  .rel {{ font-weight: 600; }}
</style>"""


def nav(current: str) -> str:
    links = [("/", "Digest"), ("/blockers", "Blockers"), ("/search", "Ground a note"), ("/upload", "Add meeting")]
    return "<nav>" + "".join(
        f'<a href="{path}" class="{"on" if path == current else ""}">{esc(label)}</a>' for path, label in links
    ) + "</nav>" + page_styles()


def topic_card(topic: Any, summary: TopicSummary | None, since: float, *, show_messages: int = 3) -> str:
    colour, label = STATE_STYLE.get(topic.state, STATE_STYLE["active"])
    age = f"{topic.age_days:.1f} วัน" if np.isfinite(topic.age_days) else "-"
    sources = " + ".join(f"{count} {name}" for name, count in sorted(topic.sources.items()))
    headline = summary.headline if summary and summary.headline else topic.label

    parts = [
        f'<div class="topic {esc(topic.state)}">',
        f'<h3><a href="/item/{topic.key}">{esc(headline)}</a></h3>',
        f'<p class="meta"><span class="tag" style="color:{colour};border-color:{colour}">{esc(label)}</span> '
        f'&nbsp;{esc(age)} &nbsp;·&nbsp; {len(topic.records)} ข้อความ ({esc(sources)}) &nbsp;·&nbsp; '
        f'{esc(", ".join(topic.participant_names[:5]))}</p>',
    ]
    if summary and summary.detail:
        parts.append(f'<p class="detail">{esc(summary.detail)}</p>')
    if summary and summary.next_step:
        parts.append(f'<p class="next">→ {esc(summary.next_step)}</p>')
    if topic.evidence:
        parts.append(f'<p class="meta">{esc(topic.evidence)}</p>')
    for record in topic.recent(since)[-show_messages:]:
        source = "meeting" if record.get("source") == "meeting" else "slack"
        parts.append(
            f'<p class="msg"><span class="tag">{esc(source)}</span> '
            f'<span class="who">{esc(names().of(record.get("user")) or "-")}</span> '
            f'{esc(names().in_text(" ".join(str(record["text"]).split()))[:200])}</p>'
        )
    if summary and summary.unverified:
        parts.append('<p class="warn">ไม่มี citation ที่ตรวจสอบผ่าน — อ่านข้อความต้นทางก่อนเชื่อ</p>')
    parts.append("</div>")
    return "".join(parts)


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


def find_topic(digest: Digest, key: str) -> Topic:
    """Resolve a work item by its stable id first, by cluster rank only as a fallback.

    `key` is the Louvain cluster index, which is a size rank: it names a different
    work item after the next rebuild. `item_id` is the ticket the item's messages
    mention, or a hash of its earliest message — stable across builds, and therefore
    the thing a bookmark, a Slack card or a human correction may point at. The int
    fallback stays because /blockers links and older bookmarks still carry it.
    """
    for candidate in digest.topics:
        if candidate.item_id == key:
            return candidate
    if key.isdigit():
        for candidate in digest.topics:
            if candidate.key == int(key):
                return candidate
    known = ", ".join(f"{topic.item_id} (#{topic.key})" for topic in digest.topics)
    raise HTTPException(status_code=404, detail=f"No active work item {key}. Available: {known or '(none)'}")


# ---- pages -----------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def digest_page() -> HTMLResponse:
    build, digest = require_build()
    tiles = [
        stat_tile("ติดอยู่", str(len(digest.blocked)), "ต้องมีคนไล่"),
        stat_tile("ปิดแล้ว", str(len(digest.resolved)), f"ในช่วง {build.days:g} วัน"),
        stat_tile("เรื่องทั้งหมด", str(len(digest.topics)), f"จาก {digest.corpus_size} ข้อความ"),
        stat_tile("สรุปโดย", backend_name(), "SUMMARIZER"),
    ]
    body = "".join(topic_card(topic, build.summary_for(topic.key), digest.since) for topic in digest.topics)
    if not body:
        body = "<p class='meta'>ไม่มีความเคลื่อนไหวในช่วงนี้</p>"
    sections = [
        (
            "เรียงตามความเร่งด่วน — ที่ติดอยู่ขึ้นก่อน สถานะมาจาก typed relation ในข้อความจริง "
            "ไม่ได้เดา กดที่หัวข้อเพื่อดู timeline",
            body,
        )
    ]
    subtitle = f"{build.days:g} วันล่าสุด · {model_name()} · สร้างเมื่อ {build.built_at}"
    if build.last_error:
        # Otherwise a nightly reindex can fail for a week and the page still looks current.
        subtitle += f" · รีเฟรช {build.last_attempt_at} ล้มเหลว ({build.last_error[:120]}) — ข้อมูลนี้เก่ากว่า corpus"
    return HTMLResponse(nav("/") + build_page("Daily digest", tiles, sections, subtitle))


@app.get("/blockers", response_class=HTMLResponse)
def blockers_page() -> HTMLResponse:
    build, digest = require_build()
    blocked = digest.blocked
    oldest = max((topic.age_days for topic in blocked if np.isfinite(topic.age_days)), default=float("nan"))
    tiles = [
        stat_tile("ติดอยู่", str(len(blocked)), "งานที่ไปต่อไม่ได้"),
        stat_tile("ค้างนานสุด", "-" if np.isnan(oldest) else f"{oldest:.1f} วัน", "ตั้งแต่ blocked ครั้งล่าสุด"),
    ]
    body = "".join(topic_card(topic, build.summary_for(topic.key), digest.since, show_messages=2) for topic in blocked)
    sections = [
        (
            "งานที่มี relation แบบ blocked_by แล้วยังไม่มี resolves ตามมา — "
            "คำนวณจาก tam.analysis.relations ล้วนๆ ไม่ผ่าน LLM",
            body or "<p class='meta'>ไม่มีอะไรติด</p>",
        )
    ]
    return HTMLResponse(nav("/blockers") + build_page("Blockers", tiles, sections, f"{build.days:g} วันล่าสุด"))


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
    return f' <a class="meta" href="{esc(url)}" target="_blank" rel="noopener">{key}{tail} ↗</a>'


@app.get("/item/{key}", response_class=HTMLResponse)
def item_page(key: str) -> HTMLResponse:
    build, digest = require_build()
    topic = find_topic(digest, key)

    events = timeline(topic, build.records)
    rows = []
    for event in events:
        also = f' · ตอบข้อความก่อนหน้าอีก {event["also_answers"]} ข้อความ' if event.get("also_answers") else ""
        rows.append(
            f'<div class="event"><div class="when">{esc(event["when"])}</div><div>'
            f'<div class="rel">{esc(event["relation"])}</div>'
            f'<p class="msg"><span class="who">{esc(event["from_user"])}</span> {esc(names().in_text(event["from_text"]))}</p>'
            f'<p class="msg">↳ <span class="who">{esc(event["to_user"])}</span> {esc(names().in_text(event["to_text"]))}</p>'
            f'<p class="meta">{esc(event["evidence"])}{esc(also)}</p></div></div>'
        )
    every = "".join(
        f'<p class="msg"><span class="tag">{esc(record.get("source") or "slack")}</span> '
        f'<span class="who">{esc(format_timestamp(str(record.get("ts", ""))))} '
        f'{esc(names().of(record.get("user")) or "-")}</span> {esc(names().in_text(" ".join(str(record["text"]).split()))[:300])}'
        f"{ticket_link(record)}</p>"
        for record in topic.records
    )

    summary = build.summary_for(topic.key)
    tiles = [
        stat_tile("สถานะ", STATE_STYLE.get(topic.state, ("", topic.state))[1], topic.evidence[:38] or "ไม่มี relation"),
        stat_tile("ข้อความ", str(len(topic.records)), " + ".join(f"{c} {n}" for n, c in sorted(topic.sources.items()))),
        stat_tile("คนเกี่ยวข้อง", str(len(topic.participants)), ", ".join(topic.participant_names[:3])),
    ]
    sections = [
        (
            "ลำดับเหตุการณ์ของงานชิ้นนี้ — แต่ละแถวคือ relation ที่มีทิศทางและมีชนิด "
            "ซึ่ง cosine similarity บอกไม่ได้เพราะมันสมมาตร",
            "".join(rows) or "<p class='meta'>ยังไม่มี typed relation ในเรื่องนี้ (มีแต่ same_topic)</p>",
        ),
        ("ทุกข้อความในเรื่องนี้ เรียงตามเวลา", every),
    ]
    title = (summary.headline if summary and summary.headline else topic.label)[:80]
    return HTMLResponse(nav("") + build_page(title, tiles, sections, f"work item {topic.item_id} · #{topic.key}"))


@app.get("/search", response_class=HTMLResponse)
def search_page(q: str = Query(default=""), k: int = Query(default=10, ge=1, le=50)) -> HTMLResponse:
    form = (
        '<form class="search" method="get" action="/search">'
        f'<input type="text" name="q" value="{esc(q)}" placeholder="วางโน้ตประชุม หรือพิมพ์สิ่งที่อยากหา…">'
        '<button type="submit">ค้นหา</button></form>'
    )
    build = live()
    body = form
    hits: list[Hit] = []
    if q.strip():
        if build.retriever is None:
            raise HTTPException(status_code=503, detail="Index is still building.")
        hits = build.retriever.rank(q, top_k=k)
        for hit in hits:
            terms = ", ".join(hit.terms[:6])
            body += (
                f'<div class="topic active"><h3>{hit.rank}.{ticket_link(hit.record)} '
                f'<span class="tag">{esc(hit.record.get("source") or "slack")}</span> '
                f'<span class="who">{esc(names().of(hit.record.get("user")) or "-")} · '
                f'{esc(format_timestamp(str(hit.record.get("ts", ""))))}</span></h3>'
                f'<p class="detail">{esc(names().in_text(" ".join(str(hit.record["text"]).split()))[:400])}</p>'
                f'<p class="meta">score {hit.score:.3f}'
                + (f" · ตรงคำ: {esc(terms)}" if terms else "")
                + f' · {esc(hit.record.get("id", ""))}</p></div>'
            )
        if not hits:
            body += "<p class='meta'>ไม่พบอะไรเลย</p>"

    tiles = [
        stat_tile("Pipeline", build.preset, "dense + BM25 fused"),
        stat_tile("ค้นได้", str(len(build.records)), "Slack + meeting รวมกัน"),
    ]
    sections = [
        (
            "วางบันทึกการประชุมลงไป แล้วดูว่าเรื่องนี้เคยคุยกันไว้ที่ไหนใน Slack — "
            "ค้นทั้งไทยและอังกฤษด้วย hybrid dense + BM25",
            body,
        )
    ]
    return HTMLResponse(nav("/search") + build_page("Ground a note", tiles, sections, f"preset {build.preset}"))


@app.get("/upload", response_class=HTMLResponse)
def upload_page() -> HTMLResponse:
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    body = (
        '<form class="search" method="post" action="/upload" enctype="multipart/form-data">'
        '<input type="file" name="transcript" accept=".vtt,.srt,.txt,.json" required>'
        '<input type="text" name="title" placeholder="ชื่อการประชุม">'
        f'<input type="datetime-local" name="started" value="{esc(now)}">'
        # Same-origin HTML can read this; a cross-site page cannot, which is the point.
        f'<input type="hidden" name="token" value="{esc(admin_token)}">'
        '<button type="submit">เพิ่มเข้า corpus</button></form>'
        '<p class="meta">รองรับ WebVTT (.vtt) จาก Zoom/Meet/Teams, .srt, บรรทัดแบบ "ชื่อ: ข้อความ" (.txt) '
        'และ JSON จาก ASR</p>'
        '<p class="meta">เวลาเริ่มประชุมเป็นตัวระบุตัวตนของการประชุม — อัปทับด้วยชื่อและเวลาเดิม '
        'จะแทนที่ของเดิม ถ้าเวลาต่างกันจะถือเป็นคนละครั้ง</p>'
    )
    sections = [
        (
            "transcript จะถูกแปลงเป็น record หน้าตาเดียวกับข้อความ Slack แล้ว index ใหม่ทั้งชุด — "
            "หลังจากนั้นการประชุมจะถูกจัดกลุ่ม ค้นหา และเชื่อมโยงกับ Slack ได้เหมือนกันหมด",
            body,
        )
    ]
    return HTMLResponse(nav("/upload") + build_page("Add a meeting", [], sections, "meeting → records"))


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
    return JSONResponse({
        "coverage": build.tracker_coverage,
        "drift": build.drifts,
        "silent": build.silent,
        "error": build.tracker_error,
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

    print(f"\n  digest    http://{args.host}:{args.port}/")
    print(f"  blockers  http://{args.host}:{args.port}/blockers")
    print(f"  search    http://{args.host}:{args.port}/search")
    print(f"  add meet  http://{args.host}:{args.port}/upload")
    print(f"  health    http://{args.host}:{args.port}/api/health")
    if configured:
        print("\n  writes    X-TAM-Token: (TAM_ADMIN_TOKEN from the environment)\n", flush=True)
    else:
        # flush: without a tty stdout is block-buffered, and a token nobody can read
        # until the process ends is a token nobody can use.
        print(f"\n  writes    X-TAM-Token: {admin_token}   (set TAM_ADMIN_TOKEN to keep it across restarts)\n", flush=True)
    uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main()
