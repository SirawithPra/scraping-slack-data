"""Turn Slack user ids into names a person can read.

`U0EXAMPLE12` is not a participant, it is a database key. Every screen this project
renders — the digest, the blockers page, an item's timeline, a Slack card — is meant
to be read by someone before a standup, and a row of eleven-character ids is
unreadable in exactly the moment the product is supposed to help.

Resolution happens at DISPLAY time, never in the stored records. The records keep the
id, because the id is what joins a message to a thread, a citation to its evidence,
and a human correction to the thing it corrects. A name is a rendering of that id and
may change — someone edits their profile — without invalidating a single record.

Three modes, because they serve different rooms (TAM_NAMES):

    slack        real display names, read from a local cache this module writes.
                 What you want when the team is reading its own standup.
    pseudonym    stable invented names. Same id always gets the same name, so a
                 conversation still reads as a conversation, but nothing identifies
                 anyone. This is the mode for a demo, a screenshot, or a bug report.
    id           raw ids, the old behaviour, for debugging a join.

Default: `slack` when a cache exists, `pseudonym` when it does not. Never id, because
the id was the problem. The cache is written by:

    python3 -m tam.ingest.users --fetch

which needs SLACK_TOKEN with `users:read`. It writes data/user_names.json, which is
gitignored: display names are personal data and this repo is public.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

log = logging.getLogger("users")

NAMES_PATH = Path(__file__).resolve().parents[2] / "data" / "user_names.json"
SLACK_API = "https://slack.com/api/"

# Invented names for the pseudonym mode. Thai nicknames, because the corpora this
# reads are Thai teams and 'Alice' next to a Thai message reads like test data.
# Deliberately ordinary and clearly not real: nobody should mistake one for a person.
PSEUDONYMS: tuple[str, ...] = (
    "ก้อง", "แนน", "ต้น", "ฟ้า", "บอส", "มิ้น", "เอิร์ธ", "ปอ", "หนึ่ง", "จูน",
    "โดม", "แพร", "กัน", "อิ๋ว", "ภูมิ", "ตาล", "เจ", "นิว", "หมิว", "ป่าน",
    "อาร์ม", "เบล", "ดิว", "พลอย", "เก่ง", "ญาญ่า", "ท็อป", "ขวัญ", "วิน", "แก้ม",
)
# Bots get their own pool, so a reader can tell a person from an integration. The
# distinction matters: 27.9% of the characters in one real corpus came from bots and
# an AI assistant, and one of them had a `U` id, so the usual "does it start with B"
# test called it a person.
BOT_PSEUDONYMS: tuple[str, ...] = ("บอทแจ้งเตือน", "บอทดีพลอย", "บอทรายงาน", "ผู้ช่วย AI")
# Surname initials, so a name reads as a roster entry rather than a name with a
# counter after it. Thai consonants people actually have surnames starting with.
SURNAME_INITIALS: tuple[str, ...] = (
    "ก", "จ", "ช", "ณ", "ด", "ต", "ท", "ธ", "น", "บ",
    "ป", "พ", "ภ", "ม", "ย", "ร", "ล", "ว", "ส", "อ",
)

SLACK_ID = ("U", "W", "B")
#: A mention as prepare_messages leaves it: "@" + id. Anchored on a non-word char so
#: an email or a path containing the same run of characters is not rewritten.
MENTION = re.compile(r"@([UWB][A-Z0-9]{7,})\b")


def is_slack_id(value: str) -> bool:
    """True for something shaped like a Slack user or bot id, not a name.

    Meeting transcripts already carry speaker names, so a corpus is usually a mix of
    ids and names and only the ids need resolving.
    """
    return len(value) >= 8 and value[0] in SLACK_ID and value[1:].replace("_", "").isalnum() and value.upper() == value


def mode() -> str:
    """Configured display mode, defaulting on whether a real name cache exists."""
    configured = os.getenv("TAM_NAMES", "").strip().lower()
    if configured in {"slack", "pseudonym", "id"}:
        return configured
    if configured:
        raise SystemExit(f"TAM_NAMES={configured!r} is not one of slack, pseudonym, id.")
    return "slack" if NAMES_PATH.exists() else "pseudonym"


def load_names(path: Path | None = None) -> dict[str, str]:
    """The id → display-name cache. A missing file is normal, not an error."""
    path = path or NAMES_PATH
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not valid JSON ({error}) — re-fetch with: python3 -m tam.ingest.users --fetch") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path} should hold an object of id → name.")
    return {str(key): str(value) for key, value in data.items()}


def pseudonym(user_id: str) -> str:
    """A stable invented name for one id.

    Derived from the id rather than from the order ids happen to appear in, so the
    same person reads as the same name across runs, corpora and machines — which is
    what makes one screenshot comparable to the next.

    A first name plus a surname initial, the way a Thai team roster is actually
    written, rather than a name with a number glued on. `กัน26` is only marginally
    more readable than `U0EXAMPLE12`, which was the whole complaint; `กัน พ.` reads
    as a person. The pair gives 30 x 20 = 600 combinations, so a collision inside one
    channel is unlikely, and `Names` disambiguates the rest at render time where it
    can see the whole set.

    sha1, not hash(): hash() is salted per process, so the same id would read as a
    different person after a restart.
    """
    pool = BOT_PSEUDONYMS if user_id.startswith("B") else PSEUDONYMS
    digest = hashlib.sha1(user_id.encode("utf-8")).digest()
    name = pool[digest[0] % len(pool)]
    if pool is BOT_PSEUDONYMS:
        return name  # bots have no surname; the label is already descriptive
    return f"{name} {SURNAME_INITIALS[digest[1] % len(SURNAME_INITIALS)]}."


class Names:
    """Resolve ids to display strings under one mode, for one render."""

    def __init__(self, names: dict[str, str] | None = None, display: str | None = None) -> None:
        self.display = display or mode()
        self.names = names if names is not None else (load_names() if self.display == "slack" else {})

    def of(self, value: Any) -> str:
        """One user field, rendered. Non-ids (transcript speaker names) pass through."""
        text = str(value or "").strip()
        if not text or not is_slack_id(text):
            return text
        if self.display == "id":
            return text
        if self.display == "slack":
            # Fall through to a pseudonym rather than showing the raw id: an id that
            # is missing from the cache is usually someone who joined after the last
            # fetch, and the reader is no better served by the key than by a label.
            return self.names.get(text) or pseudonym(text)
        return pseudonym(text)

    def all(self, values: Iterable[Any]) -> list[str]:
        return [self.of(value) for value in values]

    def in_text(self, text: Any) -> str:
        """Rewrite `@U0123…` mentions inside a message body.

        prepare_messages keeps mentions because who a message is addressed to is
        often the whole point of it — "@someone can you deploy this" is the blocker.
        It keeps them as the id, though, so the rendered body reads "@U0EXAMPLE34",
        which is the same unreadable key in the place it matters most.

        Only the mention form is touched. A bare id in prose is left alone: it is
        usually someone quoting a log line or an API response, and silently rewriting
        that would change a quoted fact.
        """
        return MENTION.sub(lambda match: "@" + self.of(match.group(1)), str(text or ""))

    def describe(self) -> str:
        """One line for a startup banner, so the mode is never a surprise."""
        if self.display == "id":
            return "user ids (TAM_NAMES=id)"
        if self.display == "slack":
            return f"ชื่อจริงจาก Slack {len(self.names)} คน" + ("" if self.names else " — ยังไม่ได้ fetch จึงใช้ชื่อสมมุติ")
        return "ชื่อสมมุติ (TAM_NAMES=pseudonym) — ไม่ระบุตัวบุคคล"


# ---- fetching --------------------------------------------------------------


def _call(method: str, token: str, params: dict[str, str]) -> dict[str, Any]:
    url = SLACK_API + method + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_names(token: str) -> dict[str, str]:
    """Every user in the workspace, id → best available display name.

    Prefers the profile's display name, then the real name, then the handle — the
    order Slack itself renders them in, so the digest matches what people see in the
    client. Bots are kept and marked, because "a bot said the work was done" is a
    materially different claim from a person saying it.
    """
    names: dict[str, str] = {}
    cursor = ""
    while True:
        params = {"limit": "200"}
        if cursor:
            params["cursor"] = cursor
        payload = _call("users.list", token, params)
        if not payload.get("ok"):
            error = payload.get("error", "unknown")
            hint = " — the token needs the users:read scope" if error == "missing_scope" else ""
            raise SystemExit(f"users.list failed: {error}{hint}")
        for member in payload.get("members", []):
            profile = member.get("profile") or {}
            name = (
                str(profile.get("display_name") or "").strip()
                or str(profile.get("real_name") or "").strip()
                or str(member.get("real_name") or "").strip()
                or str(member.get("name") or "").strip()
            )
            if not name:
                continue
            if member.get("is_bot") or member.get("id") == "USLACKBOT":
                name = f"{name} (bot)"
            names[str(member["id"])] = name
        cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
        if not cursor:
            return names
        time.sleep(1)  # users.list is tier 2; a workspace of thousands paginates


def save_names(names: dict[str, str], path: Path | None = None) -> Path:
    path = path or NAMES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(names, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fetch", action="store_true", help="Read every display name from Slack into data/user_names.json")
    parser.add_argument("--show", action="store_true", help="Print the cached names and the active mode")
    parser.add_argument("--out", type=Path, default=NAMES_PATH, help=f"Cache path (default {NAMES_PATH.name})")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    args = parse_args()

    if args.fetch:
        token = os.getenv("SLACK_TOKEN", "").strip()
        if not token:
            raise SystemExit("SLACK_TOKEN is not set. It needs the users:read scope; see pipeline/.env.example.")
        names = fetch_names(token)
        path = save_names(names, args.out)
        bots = sum(1 for name in names.values() if name.endswith("(bot)"))
        log.info("Wrote %d name(s) to %s (%d bot(s))", len(names), path, bots)
        print("\nชื่อจะขึ้นในทุกหน้าจอทันที ไม่ต้อง reindex — การแปลชื่อเกิดตอนแสดงผล")
        print("อยากซ่อนชื่อจริงตอนเดโม: TAM_NAMES=pseudonym")
        return

    resolver = Names()
    print(f"โหมด: {resolver.describe()}")
    if args.show:
        for user_id, name in sorted(load_names(args.out).items(), key=lambda pair: pair[1]):
            print(f"  {user_id:14} {name}")
    elif not NAMES_PATH.exists():
        print("ยังไม่มี cache ชื่อ — ดึงด้วย: python3 -m tam.ingest.users --fetch")
        print("ตอนนี้ทุกหน้าจอจะใช้ชื่อสมมุติที่คงที่ต่อคน เช่น", pseudonym("U0EXAMPLE12"))


if __name__ == "__main__":
    main()
