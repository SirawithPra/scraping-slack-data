"""The prototype web app: daily digest, blockers, work-item timelines, grounding.

    python3 -m tam.web.server                 # http://127.0.0.1:8000
    python3 -m tam.web.server --records data/processed/combined.json --days 7

Four pages, each backed by a JSON endpoint so a real frontend can replace the
HTML later without touching the logic:

    /                digest      — what moved, blocked items first
    /blockers        blockers    — only what is stuck, longest first
    /item/{key}      timeline    — one work item as dated, typed events
    /search?q=       grounding   — paste a meeting note, find the Slack behind it

The expensive work — embedding, clustering, typing relations — is done once at
startup and cached in `State`. Only `/search` runs per request, because only it
depends on the query. Uploading a transcript invalidates the cache and rebuilds,
which is why the upload response is slow and a search is not.

The HTML reuses visualize.build_page, so the pages inherit the same validated
palette and typography as the offline reports rather than growing a second
design system.
"""

from __future__ import annotations

import argparse
import html
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from tam.analysis.digest import DEFAULT_WINDOW_DAYS, Digest, build_digest, timeline, window_start
from tam.retrieval.embeddings import model_name, quiet_third_party_logs
from tam.ingest.meetings import merge_into, merge_utterances, parse_timestamp, parse_transcript, to_records
from tam.retrieval.retrieve import DEFAULT_PRESET, Hit, build_retriever
from tam.core import DEFAULT_RECORDS, format_timestamp, load_records
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
    """Everything built once and reused across requests."""

    records_path: Path
    days: float = DEFAULT_WINDOW_DAYS
    language: str = "th"
    preset: str = "hybrid"
    records: list[dict[str, Any]] = field(default_factory=list)
    digest: Digest | None = None
    summaries: list[TopicSummary] = field(default_factory=list)
    retriever: Any = None
    built_at: str = ""

    def rebuild(self) -> None:
        """Reload the corpus and recompute everything derived from it."""
        log.info("Building index from %s", self.records_path)
        self.records = load_records(self.records_path)
        self.retriever = build_retriever(self.records, self.preset)
        self.digest = build_digest(self.records, since=window_start(self.days))
        self.summaries = summarize_digest(self.digest, language=self.language)
        self.built_at = datetime.now(tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        log.info(
            "Ready: %d record(s), %d topic(s), %d blocked, summariser %s",
            len(self.records),
            len(self.digest.topics),
            len(self.digest.blocked),
            backend_name(),
        )

    def summary_for(self, key: int) -> TopicSummary | None:
        return next((summary for summary in self.summaries if summary.key == key), None)


state = State(records_path=DEFAULT_RECORDS)
app = FastAPI(title="Slack + meeting digest")


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
        f'{esc(", ".join(topic.participants[:5]))}</p>',
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
            f'<span class="who">{esc(record.get("user") or "-")}</span> '
            f'{esc(" ".join(str(record["text"]).split())[:200])}</p>'
        )
    if summary and summary.unverified:
        parts.append('<p class="warn">ไม่มี citation ที่ตรวจสอบผ่าน — อ่านข้อความต้นทางก่อนเชื่อ</p>')
    parts.append("</div>")
    return "".join(parts)


def require_digest() -> Digest:
    if state.digest is None:
        raise HTTPException(status_code=503, detail="Index is still building. Retry in a moment.")
    return state.digest


# ---- pages -----------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def digest_page() -> HTMLResponse:
    digest = require_digest()
    tiles = [
        stat_tile("ติดอยู่", str(len(digest.blocked)), "ต้องมีคนไล่"),
        stat_tile("ปิดแล้ว", str(len(digest.resolved)), f"ในช่วง {state.days:g} วัน"),
        stat_tile("เรื่องทั้งหมด", str(len(digest.topics)), f"จาก {digest.corpus_size} ข้อความ"),
        stat_tile("สรุปโดย", backend_name(), "SUMMARIZER"),
    ]
    body = "".join(topic_card(topic, state.summary_for(topic.key), digest.since) for topic in digest.topics)
    if not body:
        body = "<p class='meta'>ไม่มีความเคลื่อนไหวในช่วงนี้</p>"
    sections = [
        (
            "เรียงตามความเร่งด่วน — ที่ติดอยู่ขึ้นก่อน สถานะมาจาก typed relation ในข้อความจริง "
            "ไม่ได้เดา กดที่หัวข้อเพื่อดู timeline",
            body,
        )
    ]
    subtitle = f"{state.days:g} วันล่าสุด · {model_name()} · สร้างเมื่อ {state.built_at}"
    return HTMLResponse(nav("/") + build_page("Daily digest", tiles, sections, subtitle))


@app.get("/blockers", response_class=HTMLResponse)
def blockers_page() -> HTMLResponse:
    digest = require_digest()
    blocked = digest.blocked
    oldest = max((topic.age_days for topic in blocked if np.isfinite(topic.age_days)), default=float("nan"))
    tiles = [
        stat_tile("ติดอยู่", str(len(blocked)), "งานที่ไปต่อไม่ได้"),
        stat_tile("ค้างนานสุด", "-" if np.isnan(oldest) else f"{oldest:.1f} วัน", "ตั้งแต่ blocked ครั้งล่าสุด"),
    ]
    body = "".join(topic_card(topic, state.summary_for(topic.key), digest.since, show_messages=2) for topic in blocked)
    sections = [
        (
            "งานที่มี relation แบบ blocked_by แล้วยังไม่มี resolves ตามมา — "
            "คำนวณจาก tam.analysis.relations ล้วนๆ ไม่ผ่าน LLM",
            body or "<p class='meta'>ไม่มีอะไรติด</p>",
        )
    ]
    return HTMLResponse(nav("/blockers") + build_page("Blockers", tiles, sections, f"{state.days:g} วันล่าสุด"))


@app.get("/item/{key}", response_class=HTMLResponse)
def item_page(key: int) -> HTMLResponse:
    digest = require_digest()
    topic = next((candidate for candidate in digest.topics if candidate.key == key), None)
    if topic is None:
        raise HTTPException(status_code=404, detail=f"No active topic {key}")

    events = timeline(topic, state.records)
    rows = []
    for event in events:
        also = f' · ตอบข้อความก่อนหน้าอีก {event["also_answers"]} ข้อความ' if event.get("also_answers") else ""
        rows.append(
            f'<div class="event"><div class="when">{esc(event["when"])}</div><div>'
            f'<div class="rel">{esc(event["relation"])}</div>'
            f'<p class="msg"><span class="who">{esc(event["from_user"])}</span> {esc(event["from_text"])}</p>'
            f'<p class="msg">↳ <span class="who">{esc(event["to_user"])}</span> {esc(event["to_text"])}</p>'
            f'<p class="meta">{esc(event["evidence"])}{esc(also)}</p></div></div>'
        )
    every = "".join(
        f'<p class="msg"><span class="tag">{esc(record.get("source") or "slack")}</span> '
        f'<span class="who">{esc(format_timestamp(str(record.get("ts", ""))))} '
        f'{esc(record.get("user") or "-")}</span> {esc(" ".join(str(record["text"]).split())[:300])}</p>'
        for record in topic.records
    )

    summary = state.summary_for(topic.key)
    tiles = [
        stat_tile("สถานะ", STATE_STYLE.get(topic.state, ("", topic.state))[1], topic.evidence[:38] or "ไม่มี relation"),
        stat_tile("ข้อความ", str(len(topic.records)), " + ".join(f"{c} {n}" for n, c in sorted(topic.sources.items()))),
        stat_tile("คนเกี่ยวข้อง", str(len(topic.participants)), ", ".join(topic.participants[:3])),
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
    return HTMLResponse(nav("") + build_page(title, tiles, sections, f"work item #{topic.key}"))


@app.get("/search", response_class=HTMLResponse)
def search_page(q: str = Query(default=""), k: int = Query(default=10, ge=1, le=50)) -> HTMLResponse:
    form = (
        '<form class="search" method="get" action="/search">'
        f'<input type="text" name="q" value="{esc(q)}" placeholder="วางโน้ตประชุม หรือพิมพ์สิ่งที่อยากหา…">'
        '<button type="submit">ค้นหา</button></form>'
    )
    body = form
    hits: list[Hit] = []
    if q.strip():
        if state.retriever is None:
            raise HTTPException(status_code=503, detail="Index is still building.")
        hits = state.retriever.rank(q, top_k=k)
        for hit in hits:
            terms = ", ".join(hit.terms[:6])
            body += (
                f'<div class="topic active"><h3>{hit.rank}. '
                f'<span class="tag">{esc(hit.record.get("source") or "slack")}</span> '
                f'<span class="who">{esc(hit.record.get("user") or "-")} · '
                f'{esc(format_timestamp(str(hit.record.get("ts", ""))))}</span></h3>'
                f'<p class="detail">{esc(" ".join(str(hit.record["text"]).split())[:400])}</p>'
                f'<p class="meta">score {hit.score:.3f}'
                + (f" · ตรงคำ: {esc(terms)}" if terms else "")
                + f' · {esc(hit.record.get("id", ""))}</p></div>'
            )
        if not hits:
            body += "<p class='meta'>ไม่พบอะไรเลย</p>"

    tiles = [
        stat_tile("Pipeline", state.preset, "dense + BM25 fused"),
        stat_tile("ค้นได้", str(len(state.records)), "Slack + meeting รวมกัน"),
    ]
    sections = [
        (
            "วางบันทึกการประชุมลงไป แล้วดูว่าเรื่องนี้เคยคุยกันไว้ที่ไหนใน Slack — "
            "ค้นทั้งไทยและอังกฤษด้วย hybrid dense + BM25",
            body,
        )
    ]
    return HTMLResponse(nav("/search") + build_page("Ground a note", tiles, sections, f"preset {state.preset}"))


@app.get("/upload", response_class=HTMLResponse)
def upload_page() -> HTMLResponse:
    now = datetime.now().strftime("%Y-%m-%dT%H:%M")
    body = (
        '<form class="search" method="post" action="/upload" enctype="multipart/form-data">'
        '<input type="file" name="transcript" accept=".vtt,.srt,.txt,.json" required>'
        '<input type="text" name="title" placeholder="ชื่อการประชุม">'
        f'<input type="datetime-local" name="started" value="{esc(now)}">'
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


@app.post("/upload")
async def upload_meeting(
    transcript: UploadFile = File(...), title: str = Form(default=""), started: str = Form(default="")
) -> RedirectResponse:
    """Ingest a transcript into the live corpus, then rebuild the index."""
    suffix = Path(transcript.filename or "transcript.txt").suffix or ".txt"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        shutil.copyfileobj(transcript.file, handle)
        temporary = Path(handle.name)
    try:
        meeting_title = title.strip() or Path(transcript.filename or "meeting").stem
        start = parse_timestamp(started) if started.strip() else datetime.now(tz=timezone.utc)
        utterances = merge_utterances(parse_transcript(temporary))
        if not utterances:
            raise HTTPException(status_code=400, detail="No utterances found in that transcript.")
        records = to_records(utterances, title=meeting_title, started=start)
        if not records:
            raise HTTPException(status_code=400, detail="Every line was filtered as noise.")
        merge_into(records, state.records_path)
        log.info("Ingested %d record(s) from %s", len(records), transcript.filename)
    finally:
        temporary.unlink(missing_ok=True)
    state.rebuild()  # embeddings are content-hashed, so only the new lines are encoded
    return RedirectResponse(url="/", status_code=303)


# ---- JSON API --------------------------------------------------------------


@app.get("/api/digest")
def api_digest() -> JSONResponse:
    digest = require_digest()
    return JSONResponse(
        {
            "built_at": state.built_at,
            "window_days": state.days,
            "summariser": backend_name(),
            "corpus_size": digest.corpus_size,
            "topics": [
                {**topic.as_dict(), "summary": (state.summary_for(topic.key).as_dict() if state.summary_for(topic.key) else None)}
                for topic in digest.topics
            ],
        }
    )


@app.get("/api/blockers")
def api_blockers() -> JSONResponse:
    digest = require_digest()
    return JSONResponse({"blocked": [topic.as_dict() for topic in digest.blocked]})


@app.get("/api/item/{key}")
def api_item(key: int) -> JSONResponse:
    digest = require_digest()
    topic = next((candidate for candidate in digest.topics if candidate.key == key), None)
    if topic is None:
        raise HTTPException(status_code=404, detail=f"No active topic {key}")
    summary = state.summary_for(key)
    return JSONResponse(
        {
            "topic": topic.as_dict(),
            "summary": summary.as_dict() if summary else None,
            "timeline": timeline(topic, state.records),
            "messages": [
                {
                    "id": str(record["id"]),
                    "when": format_timestamp(str(record.get("ts", ""))),
                    "user": record.get("user"),
                    "source": record.get("source"),
                    "text": record["text"],
                }
                for record in topic.records
            ],
        }
    )


@app.get("/api/search")
def api_search(q: str = Query(...), k: int = Query(default=10, ge=1, le=50), preset: str | None = None) -> JSONResponse:
    if state.retriever is None:
        raise HTTPException(status_code=503, detail="Index is still building.")
    retriever = state.retriever
    if preset and preset != state.preset:
        from tam.retrieval.retrieve import PRESETS

        if preset not in PRESETS:
            raise HTTPException(status_code=400, detail=f"Unknown preset {preset}")
        retriever = retriever.with_config(PRESETS[preset])  # reuses the embedded matrix
    return JSONResponse(
        {
            "query": q,
            "preset": preset or state.preset,
            "hits": [
                {
                    "rank": hit.rank,
                    "score": hit.score,
                    "id": hit.record_id,
                    "source": hit.record.get("source"),
                    "user": hit.record.get("user"),
                    "when": format_timestamp(str(hit.record.get("ts", ""))),
                    "text": hit.record["text"],
                    "why": hit.parts,
                    "terms": hit.terms,
                }
                for hit in retriever.rank(q, top_k=k)
            ],
        }
    )


@app.post("/api/reindex")
def api_reindex() -> JSONResponse:
    state.rebuild()
    return JSONResponse({"built_at": state.built_at, "records": len(state.records)})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--days", type=float, default=DEFAULT_WINDOW_DAYS, help=f"Digest window (default {DEFAULT_WINDOW_DAYS})")
    parser.add_argument("--language", default="th", choices=("th", "en"), help="Digest language (default th)")
    parser.add_argument("--preset", default="hybrid", help=f"Search pipeline preset (default hybrid, see tam.retrieval.retrieve; {DEFAULT_PRESET} adds the reranker)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()

    state.records_path = args.records
    state.days = args.days
    state.language = args.language
    state.preset = args.preset
    state.rebuild()  # fail at startup on a bad corpus, not on the first request

    print(f"\n  digest    http://{args.host}:{args.port}/")
    print(f"  blockers  http://{args.host}:{args.port}/blockers")
    print(f"  search    http://{args.host}:{args.port}/search")
    print(f"  add meet  http://{args.host}:{args.port}/upload\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
