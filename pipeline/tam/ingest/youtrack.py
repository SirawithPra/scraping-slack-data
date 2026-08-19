"""Read YouTrack issues, so Slack has something to be compared against.

Everything else in this project reads one source and reasons about it. Drift needs
two: what the team said in Slack, and what the ticket says. The disagreement between
them is the finding — a ticket closed while the conversation continues, or a
conversation that stopped while the ticket stayed open — and it is the one thing that
connecting a chat model to Slack cannot produce, because the second source is not in
Slack.

    python3 -m tam.ingest.youtrack --check
    python3 -m tam.ingest.youtrack --keys PROJ-87,PROJ-148
    python3 -m tam.ingest.youtrack --project PROJ --out data/processed/issues.json
    python3 -m tam.ingest.youtrack --search "redemption" --limit 10
    python3 -m tam.ingest.youtrack --comment PROJ-87 --text "ผูกกับเธรดใน Slack: <url>"

**One function writes, and it is off unless somebody turned it on.** `add_comment` is
the only path here that changes YouTrack, and it refuses unless `YOUTRACK_WRITE=1`. A
comment on a ticket is visible to the whole team and cannot be taken back quietly, so
the read path and the write path do not share a switch: reading needs a token, writing
needs a token *and* a decision. `YOUTRACK_WRITE_TOKEN` exists so the read path can keep
a read-only token — if the two were the same variable, turning writing on would hand
write access to every read in this file.

**The state field is found by type, never by name.** YouTrack's default is called
`State`, and on the project this was built against it is called `Status new new`.
Looking it up by name returns nothing, with no error, for every issue — so a drift
detector built that way reports "no drift" forever and looks like it works. The field
is identified by its value being a `StateBundleElement`, which is what makes it a
state field regardless of what somebody renamed it to.

`resolved` is read as well and preferred where they disagree: it is YouTrack's own
computed answer to "is this finished", set from whichever states the project marks as
resolved, so it survives a project renaming its states.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from dotenv import load_dotenv

log = logging.getLogger("youtrack")

#: What to ask for. Requested explicitly because YouTrack returns only `$type` and `id`
#: otherwise — a silent empty result rather than an error.
FIELDS = (
    "idReadable,summary,description,resolved,updated,created,"
    "customFields(name,value(name,$type))"
)
#: How many issues one query may return. YouTrack caps a page; this is the page size.
PAGE = 200
#: Requests are batched by key, and a URL has a practical length limit.
KEYS_PER_QUERY = 40


class YouTrackError(Exception):
    """The API could not be used, with a message worth showing a person."""


#: A description line that carries no information. Measured on the real project before
#: writing this: of 155 issues with a description, 38 open with `### **URL:**`, 20 with a
#: bare Figma image, and 17 with `**Brief Info:**` — so "the first two lines" taken
#: literally feeds boilerplate to the embedder for half the tickets, and every ticket
#: that shares a template then looks like every other one.
#:
#: Headings go whole, not by matching the words inside them: the first attempt listed the
#: shapes it had seen (`### **URL:**`) and let `##### ➡️ Background / Problem Statement`
#: through, because an emoji is not in `[\w -/]`. A heading names the section under it, so
#: the content is always the next line.
#:
#: Table rows go too, content and all. 32 issues carry one in their opening lines, and
#: dropping them leaves 20 with only a title — which is the better trade: a row like
#: `| NO | Test Case | Pre-Condition |` says nothing about the subject while making every
#: QA test-case ticket look identical to every other, and those titles ("[QA][TC]Edit
#: Reward Detail") already say what the ticket is about.
_MARKUP_ONLY = re.compile(
    r"""^(?:
          \#{1,6}.*                              # any heading: it labels the section below it
        | \**[\w \-/]{0,30}:?\**                # the same without the hashes
        | !\[[^\]]*\]\([^)]*\)                  # an image
        | \[[^\]]*\]\([^)]*\)                   # a bare link
        | \|.*                                  # any table row, rule or content alike
        | [-*_=]{3,}                            # a horizontal rule
        | \s*
       )$""",
    re.X,
)
#: Enough for a title and a sentence or two of context. The point of the cap is that a
#: ticket record has to stay comparable in size to a Slack message: the real project's
#: median message is 46 characters and its median ticket 582, with one 14,556-character
#: QA test-case table, and a corpus where a fifth of the records are an order of
#: magnitude longer than the rest is one where the long ones decide every cluster.
EMBED_BUDGET = 400
EMBED_LINES = 2


def embed_text(summary: str, description: str, *, lines: int = EMBED_LINES, budget: int = EMBED_BUDGET) -> str:
    """The title, plus the first lines of the description that actually say something.

    Deliberately not the whole description. The full text is still available in
    YouTrack and is reachable by the key this record carries; what goes in the corpus is
    the part that lets retrieval and clustering recognise which conversation this ticket
    belongs to, which is what the title and the opening of the body do.
    """
    kept: list[str] = []
    for raw in str(description or "").splitlines():
        line = raw.strip()
        if not line or _MARKUP_ONLY.match(line):
            continue
        # Strip the emphasis so "**Feature:** Event detail" reads as prose to the
        # tokeniser rather than as punctuation.
        line = re.sub(r"[*_`]+", "", line).strip()
        if len(line) < 15:
            continue
        kept.append(line)
        if len(kept) >= lines:
            break
    text = "\n".join([str(summary or "").strip(), *kept]).strip()
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + "…"


@dataclass
class Issue:
    """One ticket, reduced to the fields a comparison needs."""

    key: str
    summary: str = ""
    description: str = ""
    state: str = ""
    state_field: str = ""  # what this project calls its state field, for the report
    resolved: bool = False
    updated: float = 0.0
    created: float = 0.0
    url: str = ""

    def as_record(self) -> dict[str, Any]:
        """The issue as a corpus record, so retrieval can reach it like any message.

        `source: youtrack` is already in the bot's Source union. The id is prefixed the
        way Slack and meeting records are, so nothing has to guess where a record came
        from by parsing its shape.
        """
        return {
            "id": f"yt_{self.key}",
            "text": embed_text(self.summary, self.description),
            "user": "",
            "ts": self.updated or self.created,
            "source": "youtrack",
            "thread_ts": "",
            "channel_id": "",
            "youtrack_key": self.key,
            "youtrack_state": self.state,
            "youtrack_url": self.url,
            "youtrack_resolved": self.resolved,
        }


def config() -> tuple[str, str, list[str]]:
    """(base url, token, projects) from the environment, or a message saying what is missing."""
    base = os.getenv("YOUTRACK_URL", "").strip().rstrip("/")
    token = os.getenv("YOUTRACK_TOKEN", "").strip()
    projects = [p.strip() for p in os.getenv("YOUTRACK_PROJECTS", "").split(",") if p.strip()]
    if not base or not token:
        missing = " and ".join(n for n, v in (("YOUTRACK_URL", base), ("YOUTRACK_TOKEN", token)) if not v)
        raise YouTrackError(
            f"{missing} not set — drift detection stays off. See pipeline/.env.example; "
            "a read-only token is enough for everything but `add_comment`, which is off "
            "until YOUTRACK_WRITE=1."
        )
    return base, token, projects


def write_config() -> tuple[str, str]:
    """(base url, token) for the one path that changes YouTrack — or a refusal.

    Two separate gates, because they answer different questions. `YOUTRACK_WRITE`
    is "may this deployment write at all", which is a decision somebody makes once
    and can revoke without touching a token. `YOUTRACK_WRITE_TOKEN` is "with whose
    permissions", and it falls back to the read token only when the operator has
    already said yes to writing — so the default posture stays: a read-only token,
    and nothing in this file can post with it.
    """
    base, read_token, _ = config()
    if os.getenv("YOUTRACK_WRITE", "").strip().lower() not in {"1", "true", "yes", "on"}:
        raise YouTrackError(
            "YOUTRACK_WRITE ยังไม่ได้เปิด — คอมเมนต์จะไม่ถูกเขียนลง ticket จริง "
            "(ตั้ง YOUTRACK_WRITE=1 และใส่ YOUTRACK_WRITE_TOKEN ที่มีสิทธิ์คอมเมนต์)"
        )
    token = os.getenv("YOUTRACK_WRITE_TOKEN", "").strip() or read_token
    return base, token


def allowed_issues() -> set[str]:
    """The issues this deployment may comment on — empty meaning "any of them".

    A third gate, because `YOUTRACK_WRITE` answers "may this deployment write at
    all" and not "onto what". A demo runs against the real tracker, with the team's
    real tickets one row away in the same picker, and the distance between those two
    questions is a single mis-click — after which somebody has to go and delete a
    comment from their own work item. Naming the ticket up front costs nothing and
    makes that mis-click a refusal instead of a cleanup.
    """
    raw = os.getenv("YOUTRACK_WRITE_ONLY", "")
    return {key.strip().upper() for key in raw.replace(",", " ").split() if key.strip()}


def _request(base: str, token: str, path: str, *, payload: Any = None, method: str = "") -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base + path, data=body, headers=headers, method=method or ("POST" if body is not None else "GET")
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        hint = {
            401: "the token is wrong, expired, or revoked",
            # A write path reaches this for a different reason than a read path does, and
            # "lacks read access" sends the operator to check the wrong permission.
            403: (
                "the token authenticates but may not comment on that issue"
                if body is not None
                else "the token authenticates but lacks read access to that project"
            ),
            404: "no such issue or project, or the token cannot see it",
        }.get(error.code, "see https://www.jetbrains.com/help/youtrack/devportal/api-getting-started.html")
        raise YouTrackError(f"HTTP {error.code} on {path.split('?')[0]} — {hint}. {detail}") from error
    except OSError as error:
        raise YouTrackError(f"cannot reach {base}: {error}") from error


def _get(base: str, token: str, path: str) -> Any:
    return _request(base, token, path)


def state_of(issue: dict[str, Any]) -> tuple[str, str]:
    """(field name, state) for whichever custom field is this project's state.

    Found by type rather than by name. See the module docstring: assuming the field is
    called `State` silently yields nothing on a project that renamed it, and a drift
    detector that reads nothing reports no drift and looks healthy.
    """
    for custom in issue.get("customFields") or []:
        value = custom.get("value")
        if isinstance(value, dict) and str(value.get("$type", "")).endswith("StateBundleElement"):
            return str(custom.get("name") or ""), str(value.get("name") or "")
    return "", ""


def to_issue(row: dict[str, Any], base: str) -> Issue:
    field_name, state = state_of(row)
    # YouTrack sends milliseconds; every timestamp in this project is epoch seconds.
    ms = lambda key: float(row.get(key) or 0.0) / 1000.0
    key = str(row.get("idReadable") or "")
    return Issue(
        key=key,
        summary=str(row.get("summary") or ""),
        description=str(row.get("description") or ""),
        state=state,
        state_field=field_name,
        resolved=bool(row.get("resolved")),
        updated=ms("updated"),
        created=ms("created"),
        url=f"{base}/issue/{key}" if key else "",
    )


def fetch_by_keys(keys: Sequence[str]) -> list[Issue]:
    """Only the issues named, batched — one request per KEYS_PER_QUERY keys.

    Fetching by key rather than by project is the cheap path and the usual one: the
    join comes from ticket keys people typed into Slack, so a corpus mentioning eight
    tickets should cost one request, not a walk through two hundred issues.

    A key that does not exist is simply absent from the result. That is not an error —
    somebody can type a ticket number that was never created — and the caller sees
    which keys came back.
    """
    base, token, _ = config()
    found: list[Issue] = []
    unique = list(dict.fromkeys(k.strip().upper() for k in keys if k.strip()))

    def query_for(batch: Sequence[str]) -> str:
        return urllib.parse.quote(f"issue id: {', '.join(batch)}")

    for start in range(0, len(unique), KEYS_PER_QUERY):
        batch = unique[start : start + KEYS_PER_QUERY]
        try:
            rows = _get(base, token, f"/api/issues?query={query_for(batch)}&fields={FIELDS}&$top={PAGE}")
            found.extend(to_issue(row, base) for row in rows)
        except YouTrackError as error:
            # `issue id:` rejects the WHOLE query with HTTP 400 if any single key does not
            # exist — and a key that does not exist is an ordinary event here, because
            # these come from ticket numbers people typed into Slack and people mistype.
            # Losing eight real issues to one typo would be the wrong failure, so the
            # batch is retried one key at a time to isolate it. Costly, and only on the
            # path where something is already wrong.
            if "HTTP 400" not in str(error):
                raise
            log.info("A key in this batch is not a real issue; retrying %d key(s) individually", len(batch))
            for key in batch:
                try:
                    rows = _get(base, token, f"/api/issues?query={query_for([key])}&fields={FIELDS}&$top=1")
                    found.extend(to_issue(row, base) for row in rows)
                except YouTrackError:
                    continue  # counted as missing below, with every other key intact

    missing = set(unique) - {issue.key.upper() for issue in found}
    if missing:
        log.info("Not in YouTrack (or not visible to this token): %s", ", ".join(sorted(missing)))
    return found


def fetch_project(project: str, limit: int = 1000) -> list[Issue]:
    """Every issue in one project, paged."""
    base, token, _ = config()
    out: list[Issue] = []
    while len(out) < limit:
        query = urllib.parse.quote(f"project: {project}")
        rows = _get(base, token, f"/api/issues?query={query}&fields={FIELDS}&$top={PAGE}&$skip={len(out)}")
        if not rows:
            break
        out.extend(to_issue(row, base) for row in rows)
        if len(rows) < PAGE:
            break
    return out[:limit]


#: `REV-1421` as somebody would type it into a search box. Recognised on its own so a
#: person who knows the number gets that issue rather than a text match on the digits.
KEY_QUERY = re.compile(r"^\s*#?([A-Za-z][A-Za-z0-9_]{0,9}-\d+)\s*$")
#: A bare number, which is what people actually type when the project is obvious from
#: the channel they are in. Only usable when the caller supplied exactly one project.
NUMBER_QUERY = re.compile(r"^\s*#?(\d{1,7})\s*$")
#: How many hits a search returns. A picker shows a screenful; asking for a thousand to
#: display twenty is latency the person typing pays for.
SEARCH_LIMIT = 50


def search_query(text: str, projects: Sequence[str] = ()) -> str:
    """The YouTrack query for what somebody typed into a ticket picker.

    Three shapes, because people type three different things and only one of them is
    prose. `REV-1421` and a bare `1421` are *identities* — the person already knows
    which ticket they want, and a text search for those digits buries it under every
    issue whose description happens to contain them. Anything else is handed to
    YouTrack as free text, scoped to the configured projects.

    An empty query is not an error: a picker opens before anyone types, and the useful
    thing to show then is the project's most recently touched issues.
    """
    scope = f"project: {', '.join(projects)}" if projects else ""
    key = KEY_QUERY.match(text or "")
    if key:
        return f"issue id: {key.group(1).upper()}"
    number = NUMBER_QUERY.match(text or "")
    if number and len(projects) == 1:
        return f"issue id: {projects[0].upper()}-{number.group(1)}"
    term = (text or "").strip()
    return " ".join(part for part in (scope, term, "sort by: updated desc") if part)


def search_issues(text: str, *, projects: Sequence[str] | None = None, limit: int = SEARCH_LIMIT) -> list[Issue]:
    """Issues matching what somebody typed, newest activity first.

    This is the whole tracker, not the corpus. A picker built from the corpus can only
    offer tickets somebody already mentioned in Slack, which is exactly the set that
    does *not* need linking — the ticket nobody has typed yet is the one the person is
    reaching for.

    A query that matches nothing returns an empty list. YouTrack answers `issue id:`
    for a non-existent key with HTTP 400, and a picker must show "no match", not an
    error, when somebody is still halfway through typing a number.
    """
    base, token, configured = config()
    wanted = list(projects) if projects is not None else configured
    query = search_query(text, wanted)
    try:
        rows = _get(base, token, f"/api/issues?query={urllib.parse.quote(query)}&fields={FIELDS}&$top={limit}")
    except YouTrackError as error:
        if "HTTP 400" in str(error):
            return []
        raise
    return [to_issue(row, base) for row in rows]


def delete_comment(key: str, comment_id: str) -> None:
    """Remove a comment this module wrote. Exists for `check_write` and nothing else.

    Not exposed anywhere a person can reach it. Deleting somebody's comment from a
    chat command is a destructive action with no undo and no audit trail the team
    would see; the one caller below deletes only a comment it created a second
    earlier, whose id it is still holding.
    """
    base, token = write_config()
    _request(
        base, token,
        f"/api/issues/{urllib.parse.quote(key)}/comments/{urllib.parse.quote(comment_id)}",
        method="DELETE",
    )


def check_write(key: str) -> dict[str, Any]:
    """Prove the token can comment on this project, and leave nothing behind.

    "The token can probably comment" is a guess, and the place nobody wants to find
    out is halfway through a demo — a 403 there looks like the feature is broken
    rather than like a permission nobody granted. There is no read-only way to ask
    YouTrack this: permissions live behind admin endpoints an ordinary token cannot
    see, and the only honest test of "may I write" is writing.

    So it writes a comment that says what it is, deletes it, and reports both steps
    separately. If the delete fails the comment stays and this says so with the id —
    a probe that cleans up silently on failure would be worse than one that does not
    clean up at all, because nobody would go and remove it.
    """
    marker = "🐾 Meowtam permission check — คอมเมนต์ทดสอบสิทธิ์ ระบบจะลบทิ้งทันที"
    written = add_comment(key, marker)
    result = {"key": written["key"], "wrote": True, "id": written["id"], "removed": False, "error": ""}
    try:
        delete_comment(written["key"], written["id"])
        result["removed"] = True
    except YouTrackError as error:
        result["error"] = str(error)
    return result


def add_comment(key: str, text: str) -> dict[str, Any]:
    """Write one comment on one issue, and return what YouTrack stored.

    The return value is the point. A write whose only evidence is that no exception was
    raised is indistinguishable from the mock this replaced, so the comment's own id
    comes back and the caller shows it — the reader can open the ticket and find that
    id there. Empty text is refused rather than posted: a blank comment on a ticket is
    noise somebody has to delete by hand.
    """
    issue = str(key or "").strip().upper()
    body = str(text or "").strip()
    if not issue:
        raise YouTrackError("ไม่ได้บอกว่า ticket ไหน")
    if not body:
        raise YouTrackError("คอมเมนต์ว่างเปล่า — ไม่เขียนลง ticket")
    allowed = allowed_issues()
    if allowed and issue not in allowed:
        raise YouTrackError(
            f"YOUTRACK_WRITE_ONLY เปิดให้เขียนเฉพาะ {', '.join(sorted(allowed))} — {issue} ไม่อยู่ในนั้น จึงไม่เขียน"
        )
    base, token = write_config()
    created = _request(
        base, token, f"/api/issues/{urllib.parse.quote(issue)}/comments?fields=id,text,created",
        payload={"text": body},
    )
    log.info("Commented on %s (comment %s)", issue, created.get("id"))
    return {
        "key": issue,
        "id": str(created.get("id") or ""),
        "url": f"{base}/issue/{issue}",
        "text": body,
    }


#: A ticket whose title and opening lines together say almost nothing — no description
#: and a title like "fix" — would join clusters on nothing but its own emptiness. Measured:
#: exactly one issue of 195 falls under this on the real project.
MIN_TICKET_CHARS = 12


def fetch_tickets(limit: int = 1000) -> list[Issue]:
    """Every issue in every configured project, ready to merge into the corpus.

    The corpus half of this module. `fetch_project` and `fetch_by_keys` answer questions
    about named issues; this one answers "what is in the tracker", which is what the
    dashboard needs in order to hold both sources at once instead of comparing them from
    a distance.

    Raises YouTrackError when nothing is configured, so a caller can carry on with the
    Slack half rather than reporting a tracker it never read as one that agreed.
    """
    _, _, wanted = config()  # raises when unset, before any request goes out
    if not wanted:
        # Refusing here rather than returning []. An empty project list reads as "the
        # tracker is empty" everywhere downstream: the corpus gains nothing, the digest
        # shows Slack only, and /api/tracker reports agreement it never checked. The
        # variable is plural and comma-separated, which is exactly the kind of name a
        # caller gets singular on the first try.
        # The listing is a convenience, so it must not become the error. Asking the API
        # what is visible needs the network, and when that is also down the caller would
        # be told about DNS instead of about the variable they have to set.
        try:
            visible = ", ".join(str(p.get("shortName") or "") for p in projects())
        except YouTrackError:
            visible = ""
        hint = f" This token can see: {visible}" if visible else ""
        raise YouTrackError(
            "YOUTRACK_PROJECTS is not set, so there is nothing to fetch — set it to a "
            f"comma-separated list of project short names.{hint}"
        )
    found: list[Issue] = []
    seen: set[str] = set()
    for project in wanted:
        for issue in fetch_project(project, limit=limit):
            if issue.key in seen:
                continue
            seen.add(issue.key)
            found.append(issue)
    return found


def whoami() -> dict[str, Any]:
    base, token, _ = config()
    return _get(base, token, "/api/users/me?fields=login,name,guest")


def projects() -> list[dict[str, Any]]:
    base, token, _ = config()
    return _get(base, token, "/api/admin/projects?fields=shortName,name,archived&$top=200")


def keys_in(records: Iterable[dict[str, Any]]) -> list[str]:
    """Ticket keys the corpus already carries, from the linker's own item ids.

    The linker names a work item after the ticket its messages mention, so the keys are
    already extracted and verified against the text — no second regex, and no risk of
    the two disagreeing about what counts as a key.
    """
    seen: dict[str, None] = {}
    for record in records:
        key = str(record.get("youtrack_key") or "")
        if key:
            seen.setdefault(key, None)
    return list(seen)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Verify the token and print what it can see")
    parser.add_argument("--keys", help="Comma-separated issue ids to fetch, e.g. PROJ-1,PROJ-2")
    parser.add_argument("--project", help="Fetch a whole project by short name")
    parser.add_argument("--search", help="Search the tracker the way the Slack ticket picker does")
    parser.add_argument("--limit", type=int, default=SEARCH_LIMIT, help=f"Cap on --search hits (default {SEARCH_LIMIT})")
    parser.add_argument("--comment", help="Issue id to comment on, e.g. PROJ-87. Needs YOUTRACK_WRITE=1")
    parser.add_argument(
        "--check-write",
        metavar="ISSUE",
        help="Prove the token may comment on this issue: writes a marked comment and deletes it again",
    )
    parser.add_argument("--text", default="", help="The comment body, used with --comment")
    parser.add_argument("--out", type=Path, help="Write the issues as corpus records here")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    args = parse_args()
    try:
        base, _, configured = config()
        if args.check:
            me = whoami()
            print(f"  ต่อได้ — {base}")
            print(f"  ล็อกอินเป็น {me.get('login')} ({me.get('name')})")
            live = [p for p in projects() if not p.get("archived")]
            print(f"  เห็น {len(live)} โปรเจกต์: {', '.join(p['shortName'] for p in live[:12])}")
            if configured:
                print(f"  ตั้งไว้ให้อ่าน: {', '.join(configured)}")
            return

        if args.check_write:
            probe = check_write(args.check_write)
            print(f"  เขียนคอมเมนต์ลง {probe['key']} ได้ (comment {probe['id']})")
            if probe["removed"]:
                print("  ลบคอมเมนต์ทดสอบออกแล้ว — ไม่เหลืออะไรบน ticket")
                print("\n  ✓ token นี้คอมเมนต์ได้จริง ตั้ง YOUTRACK_WRITE=1 แล้วใช้ได้เลย")
            else:
                # Said loudly, with the id, because nobody will go looking for a comment
                # they were told was cleaned up.
                print(f"  ⚠ ลบไม่ออก: {probe['error']}")
                print(f"  ⚠ คอมเมนต์ทดสอบยังค้างอยู่บน {probe['key']} — ต้องเข้าไปลบเอง (comment {probe['id']})")
                print("\n  ✓ เขียนได้ แต่ token นี้ลบคอมเมนต์ไม่ได้ ซึ่งไม่กระทบการใช้งานจริง")
            return

        if args.comment:
            written = add_comment(args.comment, args.text)
            print(f"  เขียนคอมเมนต์ลง {written['key']} แล้ว (comment {written['id']}) → {written['url']}")
            return

        if args.search is not None:
            hits = search_issues(args.search, limit=args.limit)
            print(f"  ค้น “{args.search}” ใน {', '.join(configured) or 'ทุกโปรเจกต์'} เจอ {len(hits)} ใบ")
            for issue in hits:
                print(f"  {issue.key:16} {issue.state:18} {'ปิดแล้ว' if issue.resolved else 'ยังเปิด':9} {issue.summary[:50]}")
            return

        issues = fetch_by_keys(args.keys.split(",")) if args.keys else fetch_project(args.project or (configured[0] if configured else ""))
        if not issues:
            raise SystemExit("No issues returned. Check --keys / --project and YOUTRACK_PROJECTS.")
        fields = {issue.state_field for issue in issues if issue.state_field}
        log.info("Read %d issue(s); state field on this project: %s", len(issues), ", ".join(sorted(fields)) or "(none found)")
        for issue in issues[:20]:
            print(f"  {issue.key:16} {issue.state:18} {'ปิดแล้ว' if issue.resolved else 'ยังเปิด':9} {issue.summary[:40]}")
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps([issue.as_record() for issue in issues], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log.info("Wrote %d record(s) to %s", len(issues), args.out)
    except YouTrackError as error:
        raise SystemExit(f"YouTrack: {error}") from error


if __name__ == "__main__":
    main()
