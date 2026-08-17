"""Flatten a Slack export into searchable records.

Accepts either input format:
  * export_slack.py output -- parents with a nested "replies" list
  * a flat scraped export  -- one row per message, keyed by "message_id"

Produces two kinds of record:
  * one per individual message  (source="slack")
  * one per thread, parent + useful replies concatenated (source="slack_thread")

Thai, English, and mixed Thai/English text is kept as written -- nothing is
translated. Only messages that carry no information at all are dropped.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
from pathlib import Path
from typing import Any

DEFAULT_INPUT = Path("data/raw/slack_messages.json")
DEFAULT_OUTPUT = Path("data/processed/messages.json")
MAX_THREAD_REPLIES = 20

# Slack system messages that are never about the work itself.
IGNORED_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "group_join",
    "group_leave",
    "pinned_item",
    "unpinned_item",
    "bot_add",
    "bot_remove",
}

# Slack markup -> plain text. LINK_RE is the catch-all for <target|label> and runs
# last, so it must not swallow mentions (<@U...>), channels (<#C...>), or <!here>.
# Slack also emits links with no scheme, e.g. <trailhead.com|@Sam Rivera>.
LINK_RE = re.compile(r"<(?![@#!])([^>|\s]+)(?:\|([^>]*))?>")
SCHEME_RE = re.compile(r"^(?:https?://|mailto:|tel:)", re.IGNORECASE)
CHANNEL_REF_RE = re.compile(r"<#[A-Z0-9]+(?:\|([^>]*))?>")
MENTION_RE = re.compile(r"<@[A-Z0-9]+(?:\|[^>]*)?>")
BROADCAST_RE = re.compile(r"<!(?:here|channel|everyone)(?:\|[^>]*)?>")
SUBTEAM_RE = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")
EMOJI_CODE_RE = re.compile(r":[a-z0-9_+\-]+:(?::skin-tone-\d:)?", re.IGNORECASE)
WHITESPACE_RE = re.compile("[ \t\u00a0\u200b\u200c\ufeff]+")
BLANK_LINES_RE = re.compile(r"\n{2,}")

# Pictographs, dingbats, and variation selectors: ignored when judging whether a
# message says anything, but left in the stored text.
PICTOGRAPH_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # emoji, pictographs, transport, symbols
    "\u2190-\u21ff"  # arrows
    "\u2300-\u27bf"  # technical symbols and dingbats
    "\u2b00-\u2bff"  # extra arrows and geometric shapes
    "\ufe0f\u200d"  # variation selector, zero-width joiner
    "\u3030\u303d"  # wavy dash, part alternation mark
    "]"
)
TOKEN_SPLIT_RE = re.compile(r"[\s,.!?~\-_/\\|()\[\]{}<>\"'`“”„…•+*=:;^]+")

# Standalone acknowledgements. A message is dropped only if *every* token is here.
NOISE_WORDS = {
    "ok", "okay", "okey", "oke", "k", "kk", "yes", "yep", "yeah", "y", "yy",
    "no", "nope", "noted", "ack", "same", "up", "lol", "haha", "hahaha", "hehe",
    "thanks", "thank", "thx", "ty", "tks", "nice", "good", "great", "cool",
    "โอเค", "โอเคครับ", "โอเคค่ะ", "ครับ", "คับ", "ครับผม", "ค่ะ", "คะ", "จ้า", "จ้าา", "จ๊ะ",
    "ได้", "ได้ครับ", "ได้ค่ะ", "รับทราบ", "โอ", "อ่อ", "อ๋อ", "เอ่อ", "งับ", "จร้า",
    "ขอบคุณ", "ขอบคุณครับ", "ขอบคุณค่ะ", "ขอบคุณมาก", "ขอบคุณมากครับ", "ๆ",
}

# Slack system messages that arrive as plain text (no subtype) in scraped exports.
SYSTEM_TEXT_RE = re.compile(
    r"^(?:has (?:joined|left) the channel"
    r"|has renamed the channel"
    r"|set the channel (?:topic|purpose|description)"
    r"|(?:un)?pinned a message to this channel"
    r"|.{0,60}\bmoved some of the messages from this conversation\b"
    r"|added an integration to this channel"
    r"|cleared the channel (?:topic|purpose))",
    re.IGNORECASE,
)

# Developer vocabulary that keeps a message even when it is very short.
TECHNICAL_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    r"be|fe|api|apis|uat|prod|production|staging|dev|deploy|deployed|deployment|"
    r"merge|merged|mr|pr|commit|branch|rebase|revert|build|ci|cd|pipeline|release|hotfix|"
    r"bug|fix|fixed|error|crash|log|logs|db|sql|migration|migrate|index|query|cache|"
    r"endpoint|token|jwt|auth|login|logout|session|sort|sorting|filter|pagination|paginate|"
    r"mock|mocked|test|tests|qa|docker|redis|kafka|s3|frontend|backend|server|client|"
    r"pending|blocked|done|wip|todo|review|revert|rollback|timeout|latency|schema"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)

log = logging.getLogger("prepare_messages")


def link_text(match: re.Match[str]) -> str:
    """Prefer a link's label; otherwise show the target without its scheme."""
    target, label = match.group(1), match.group(2)
    return label.strip() if label and label.strip() else SCHEME_RE.sub("", target)


def normalize_text(text: str) -> str:
    """Turn Slack markup into plain text, keeping the wording intact."""
    cleaned = html.unescape(text)
    cleaned = CHANNEL_REF_RE.sub(lambda match: f"#{match.group(1)}" if match.group(1) else " ", cleaned)
    cleaned = SUBTEAM_RE.sub(lambda match: match.group(1) or " ", cleaned)
    cleaned = MENTION_RE.sub(" ", cleaned)
    cleaned = BROADCAST_RE.sub(" ", cleaned)
    cleaned = LINK_RE.sub(link_text, cleaned)
    cleaned = EMOJI_CODE_RE.sub(" ", cleaned)
    cleaned = WHITESPACE_RE.sub(" ", cleaned.replace("\r", "\n"))
    cleaned = BLANK_LINES_RE.sub("\n", cleaned)
    return "\n".join(line.strip() for line in cleaned.split("\n")).strip()


def is_noise_token(token: str) -> bool:
    """Acknowledgement word, or one character repeated ("555", "kkk", "!!!")."""
    return token in NOISE_WORDS or len(set(token)) == 1


def is_useful(text: str) -> bool:
    """Keep anything that says something. Short technical notes always pass."""
    if not text:
        return False
    if SYSTEM_TEXT_RE.match(text):
        return False
    if TECHNICAL_RE.search(text):
        return True
    tokens = [token for token in TOKEN_SPLIT_RE.split(PICTOGRAPH_RE.sub(" ", text).lower()) if token]
    if not tokens:  # emoji or punctuation only
        return False
    return any(not is_noise_token(token) for token in tokens)


def make_record(message: dict[str, Any], channel_id: str, thread_ts: str) -> dict[str, Any] | None:
    """Build one searchable record, or None if the message carries no meaning."""
    if str(message.get("subtype", "")) in IGNORED_SUBTYPES:
        return None
    text = normalize_text(str(message.get("text", "")))
    if not is_useful(text):
        return None
    ts = str(message.get("ts") or thread_ts)
    return {
        "id": f"msg_{channel_id}_{ts}",
        "channel_id": channel_id,
        "ts": ts,
        "thread_ts": thread_ts,
        "user": str(message.get("user") or message.get("bot_id") or ""),
        "text": text,
        "source": "slack",
    }


def make_thread_record(parent_id: str, channel_id: str, thread_ts: str, texts: list[str]) -> dict[str, Any]:
    """Concatenate a conversation so thread-level context is searchable too."""
    return {
        "id": f"thread_{channel_id}_{thread_ts}",
        "channel_id": channel_id,
        "ts": thread_ts,
        "thread_ts": thread_ts,
        "user": "",
        "text": "\n".join(texts[: MAX_THREAD_REPLIES + 1]),
        "source": "slack_thread",
        "parent_id": parent_id,
    }


def flat_to_threads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group a flat export (one row per message) into parents holding their replies."""
    parents: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    pending: list[tuple[str, dict[str, Any]]] = []

    for row in rows:
        ts = str(row.get("message_id") or row.get("ts") or "")
        if not ts:
            continue
        message = {
            "ts": ts,
            "thread_ts": str(row.get("thread_ts") or ts),
            "user": str(row.get("user_name") or row.get("user_id") or row.get("user") or ""),
            "text": str(row.get("text") or ""),
            "subtype": str(row.get("subtype") or ""),
            "bot_id": "",
            "reply_count": 0,
            "channel_id": str(row.get("channel_id") or ""),
            "replies": [],
        }
        parent_ts = str(row.get("parent_ts") or "")
        if parent_ts and parent_ts != ts:
            pending.append((parent_ts, message))
        else:
            parents[ts] = message
            order.append(ts)

    for parent_ts, message in pending:
        parent = parents.get(parent_ts)
        if parent is None:  # thread parent falls outside the exported range
            parents[message["ts"]] = message
            order.append(message["ts"])
            continue
        parent["replies"].append(message)
        parent["reply_count"] = len(parent["replies"])

    for ts in order:
        parents[ts]["replies"].sort(key=lambda reply: str(reply["ts"]))
    return [parents[ts] for ts in order]


def load_export(path: Path) -> list[dict[str, Any]]:
    """Read either supported export shape and return parents with nested replies."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path} is not valid JSON: {error}") from error
    if not isinstance(rows, list):
        raise SystemExit(f"{path} should contain a list of messages.")
    if rows and isinstance(rows[0], dict) and "message_id" in rows[0]:
        log.info("Detected a flat scraped export; grouping %d row(s) into threads", len(rows))
        return flat_to_threads(rows)
    return rows


def prepare(exported: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    total = dropped = threads = 0

    for parent in exported:
        channel_id = str(parent.get("channel_id", ""))
        parent_ts = str(parent.get("ts", ""))
        thread_ts = str(parent.get("thread_ts") or parent_ts)

        thread_texts: list[str] = []
        parent_id = ""
        for position, message in enumerate([parent, *(parent.get("replies") or [])]):
            total += 1
            record = make_record(message, channel_id, thread_ts)
            if record is None:
                dropped += 1
                continue
            if record["id"] in seen_ids:  # broadcast replies can appear twice
                continue
            seen_ids.add(record["id"])
            records.append(record)
            thread_texts.append(record["text"])
            if position == 0:
                parent_id = record["id"]

        if len(thread_texts) > 1:
            records.append(make_thread_record(parent_id, channel_id, thread_ts, thread_texts))
            threads += 1

    log.info(
        "Kept %d/%d message(s), dropped %d as noise, built %d thread record(s)",
        total - dropped,
        total,
        dropped,
        threads,
    )
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_INPUT, help=f"Slack export (default {DEFAULT_INPUT})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help=f"Output path (default {DEFAULT_OUTPUT})")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    if not args.raw.exists():
        raise SystemExit(f"Missing {args.raw}. Run export_slack.py first.")

    records = prepare(load_export(args.raw))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Saved %d searchable record(s) to %s", len(records), args.out)


if __name__ == "__main__":
    main()
