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
            "text": f"{self.summary}\n\n{self.description}".strip(),
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
            "a read-only token is enough, nothing here writes to YouTrack."
        )
    return base, token, projects


def _get(base: str, token: str, path: str) -> Any:
    request = urllib.request.Request(
        base + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        hint = {
            401: "the token is wrong, expired, or revoked",
            403: "the token authenticates but lacks read access to that project",
            404: "no such issue or project, or the token cannot see it",
        }.get(error.code, "see https://www.jetbrains.com/help/youtrack/devportal/api-getting-started.html")
        raise YouTrackError(f"HTTP {error.code} on {path.split('?')[0]} — {hint}. {detail}") from error
    except OSError as error:
        raise YouTrackError(f"cannot reach {base}: {error}") from error


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
