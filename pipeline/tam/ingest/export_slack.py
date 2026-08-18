"""Export a Slack channel's messages and thread replies to data/raw/slack_messages.json.

Raw message text is stored verbatim; cleaning happens in tam.ingest.prepare_messages.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

DEFAULT_OUTPUT = Path("data/raw/slack_messages.json")
MAX_PAGE_SIZE = 200
MAX_RATE_LIMIT_RETRIES = 5
FALLBACK_RETRY_AFTER = 5

# Slack error code -> what the operator should actually do about it.
ERROR_HINTS: dict[str, str] = {
    "invalid_auth": "SLACK_TOKEN is invalid, expired, or revoked.",
    "not_authed": "SLACK_TOKEN was not sent; check your .env file.",
    "token_revoked": "SLACK_TOKEN was revoked; reinstall the app to get a new one.",
    "account_inactive": "The token belongs to a deactivated user or app.",
    "channel_not_found": "SLACK_CHANNEL_ID is wrong, or this token cannot see that channel.",
    "not_in_channel": "Invite the app to the channel first: /invite @your-app",
    "missing_scope": "The token is missing a required OAuth scope (see README).",
    "not_allowed_token_type": "Use a bot token (xoxb-...) or a user token (xoxp-...).",
    "ratelimited": "Slack is rate limiting this token; retry with a smaller SLACK_MAX_MESSAGES.",
}

log = logging.getLogger("export_slack")


def header_value(headers: Any, name: str) -> str | None:
    """Read a header case-insensitively (Slack SDK casing varies by transport)."""
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return value[0] if isinstance(value, list) and value else value
    return None


def retry_after_seconds(response: Any) -> int:
    raw = header_value(getattr(response, "headers", None), "Retry-After")
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return FALLBACK_RETRY_AFTER


def call_slack(method: Callable[..., Any], **kwargs: Any) -> Any:
    """Call a Slack Web API method, sleeping for Retry-After when rate limited."""
    for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return method(**kwargs)
        except SlackApiError as error:
            if getattr(error.response, "status_code", None) != 429:
                raise
            delay = retry_after_seconds(error.response)
            log.warning(
                "Rate limited (429); sleeping %ss then retrying [%d/%d]",
                delay,
                attempt,
                MAX_RATE_LIMIT_RETRIES,
            )
            time.sleep(delay)
    raise SystemExit(
        f"Slack kept rate limiting after {MAX_RATE_LIMIT_RETRIES} retries. "
        "Lower SLACK_MAX_MESSAGES or wait a few minutes."
    )


def check_token_shape(token: str) -> None:
    """Reject token types that can never read channel history, before any API call."""
    if token.startswith("xoxe-"):
        raise SystemExit(
            "SLACK_TOKEN looks like a refresh / app-configuration token (xoxe-...).\n"
            "Those only work with the apps.* manifest APIs, not conversations.history.\n"
            "Use the Bot User OAuth Token (xoxb-...) from api.slack.com/apps -> your app\n"
            "-> OAuth & Permissions -> Install to Workspace, or a user token (xoxp-...)."
        )
    if token.startswith("xapp-"):
        raise SystemExit(
            "SLACK_TOKEN is an app-level token (xapp-...), which only works for Socket Mode.\n"
            "Use the Bot User OAuth Token (xoxb-...) instead."
        )
    if not token.startswith(("xoxb-", "xoxp-", "xoxe.xoxb-", "xoxe.xoxp-")):
        log.warning("SLACK_TOKEN does not look like a bot or user token; trying it anyway.")


def verify_auth(client: WebClient) -> str:
    """Confirm the token works before paging, and return this bot's own user id.

    The id is what lets the export skip the bot's own messages. Without that, a bot
    that posts a digest into a channel it also reads feeds its own output back in: the
    digest text quotes item labels and evidence, so it clusters with the very items it
    describes and the group reinforces itself on every refresh. Measured on the test
    channel, four of the bot's own posts had entered the corpus this way.

    Other bots are kept. A deploy notification is a real event worth clustering; the
    bot reading its own summary of yesterday is not.
    """
    response = call_slack(client.auth_test)
    log.info(
        "Authenticated as %s in workspace %s",
        response.get("user") or response.get("bot_id") or "unknown",
        response.get("team") or "unknown",
    )
    return str(response.get("user_id") or "")


def next_cursor(response: Any) -> str | None:
    return (response.get("response_metadata") or {}).get("next_cursor") or None


def fetch_parent_messages(
    client: WebClient, channel_id: str, max_messages: int, page_size: int, oldest: str = ""
) -> list[dict[str, Any]]:
    """Page through conversations.history until max_messages is reached.

    `oldest` is what makes a daily run affordable. Apps created after 2025-05-29 that
    are not on the Marketplace get roughly one conversations.history call per minute,
    so re-fetching two hundred messages every morning to find the handful that are new
    spends the whole budget on messages already on disk. Passing the newest timestamp
    already exported turns that into one page.
    """
    messages: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(messages) < max_messages:
        response = call_slack(
            client.conversations_history,
            channel=channel_id,
            limit=min(page_size, max_messages - len(messages)),
            cursor=cursor,
            **({"oldest": oldest} if oldest else {}),
        )
        page = response.get("messages") or []
        messages.extend(page)
        log.info("history: %d/%d parent messages", min(len(messages), max_messages), max_messages)
        cursor = next_cursor(response)
        if not page or not cursor:
            break
    return messages[:max_messages]


def fetch_thread_replies(client: WebClient, channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
    """Page through conversations.replies, dropping the repeated parent message."""
    replies: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        response = call_slack(
            client.conversations_replies,
            channel=channel_id,
            ts=thread_ts,
            limit=MAX_PAGE_SIZE,
            cursor=cursor,
        )
        replies.extend(
            message
            for message in response.get("messages") or []
            if message.get("ts") != thread_ts
        )
        cursor = next_cursor(response)
        if not cursor:
            return replies


def to_record(message: dict[str, Any], channel_id: str, thread_ts: str) -> dict[str, Any]:
    """Keep the metadata the later stages need, with text preserved as sent."""
    return {
        "ts": str(message.get("ts", "")),
        "thread_ts": str(message.get("thread_ts") or thread_ts),
        "user": str(message.get("user", "")),
        "text": message.get("text", ""),
        "subtype": str(message.get("subtype", "")),
        "bot_id": str(message.get("bot_id", "")),
        "reply_count": int(message.get("reply_count", 0) or 0),
        "channel_id": channel_id,
        "replies": [],
    }


def has_thread(message: dict[str, Any]) -> bool:
    """True for a thread parent; broadcast replies also appear in history."""
    ts = str(message.get("ts", ""))
    return int(message.get("reply_count", 0) or 0) > 0 and str(message.get("thread_ts") or ts) == ts


def export_channel(
    client: WebClient, channel_id: str, max_messages: int, page_size: int, oldest: str = "", skip_user: str = ""
) -> list[dict[str, Any]]:
    parents = fetch_parent_messages(client, channel_id, max_messages, page_size, oldest)
    if skip_user:
        before = len(parents)
        parents = [message for message in parents if str(message.get("user") or "") != skip_user]
        if before != len(parents):
            log.info("Skipped %d message(s) this bot posted itself", before - len(parents))
    log.info("Fetched %d parent message(s); collecting thread replies", len(parents))

    exported: list[dict[str, Any]] = []
    reply_total = 0
    for index, message in enumerate(parents, start=1):
        record = to_record(message, channel_id, str(message.get("ts", "")))
        if has_thread(message):
            raw_replies = fetch_thread_replies(client, channel_id, record["ts"])
            record["replies"] = [
                to_record(reply, channel_id, record["ts"]) for reply in raw_replies
            ]
            reply_total += len(record["replies"])
            log.info("replies: %d/%d threads (+%d)", index, len(parents), len(record["replies"]))
        exported.append(record)

    log.info("Collected %d parent message(s) and %d thread reply/replies", len(exported), reply_total)
    return exported


def member_channels(client: WebClient) -> list[tuple[str, str]]:
    """(id, name) for every channel this bot is a member of, public or private.

    Discovering them beats listing them in configuration: the set changes whenever
    somebody types `/invite`, and a config file that has to be edited for each one is a
    config file that will be out of date the first time it matters. The bot can only
    read channels it was invited to anyway, so membership already *is* the permission
    boundary — asking Slack for it means the export follows the invitations rather than
    the other way round.
    """
    channels: list[tuple[str, str]] = []
    cursor: str | None = None
    while True:
        response = call_slack(
            client.users_conversations,
            types="public_channel,private_channel",
            limit=200,
            exclude_archived=True,
            cursor=cursor,
        )
        for channel in response.get("channels") or []:
            channels.append((str(channel["id"]), str(channel.get("name") or channel["id"])))
        cursor = next_cursor(response)
        if not cursor:
            return sorted(channels, key=lambda pair: pair[1])


def newest_ts(path: Path) -> str:
    """The latest message timestamp already in an export, or "" if there is none.

    Read from the file rather than kept in a separate state file, so the resume point
    cannot drift away from the data it describes: delete the export and the next run
    starts over, which is the behaviour anyone would expect.
    """
    if not path.exists():
        return ""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("%s is not valid JSON; exporting from the beginning", path)
        return ""
    stamps = [str(row.get("ts") or "") for row in rows if isinstance(row, dict)]
    stamps += [str(reply.get("ts") or "") for row in rows if isinstance(row, dict) for reply in (row.get("replies") or [])]
    numeric = [s for s in stamps if s.replace(".", "", 1).isdigit()]
    return max(numeric, key=float) if numeric else ""


def merge_exports(existing: Sequence[dict[str, Any]], fresh: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Union two exports by message ts, newest version of a message winning.

    An incremental run re-fetches the boundary message (Slack's `oldest` is inclusive)
    and any thread that gained a reply, so the same parent legitimately arrives twice
    with different reply lists. Keyed on ts because that is the identity Slack gives a
    message, and taking the fresh copy means a thread that grew is not truncated back.
    """
    merged = {str(row.get("ts") or f"__{index}"): row for index, row in enumerate(existing)}
    for row in fresh:
        merged[str(row.get("ts") or f"__fresh_{id(row)}")] = row
    return sorted(merged.values(), key=lambda row: float(str(row.get("ts") or 0) or 0))


def format_ts(ts: str) -> str:
    """An epoch string as a readable instant, for the log line about resuming."""
    from tam.core import format_timestamp
    return format_timestamp(ts)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="Channel id; defaults to SLACK_CHANNEL_ID")
    parser.add_argument(
        "--max-messages",
        type=int,
        help="Maximum parent messages to fetch; defaults to SLACK_MAX_MESSAGES (200)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=MAX_PAGE_SIZE,
        help=f"Messages per API page, 1-{MAX_PAGE_SIZE} (default {MAX_PAGE_SIZE})",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help=f"Output path (default {DEFAULT_OUTPUT})")
    parser.add_argument(
        "--all-channels",
        action="store_true",
        help="Export every channel this bot is a member of, one file each, instead of one --channel. "
        "Membership is asked of Slack, so a new /invite is picked up with no config change",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUTPUT.parent,
        help=f"Where --all-channels writes real_<channel>.json (default {DEFAULT_OUTPUT.parent})",
    )
    parser.add_argument(
        "--since-last",
        action="store_true",
        help="Fetch only what is newer than the newest message already in the output file, then merge. "
        "Turns a daily refresh into one API page instead of re-reading the whole history",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    args = parse_args()

    token = os.getenv("SLACK_TOKEN", "").strip()
    channel_id = (args.channel or os.getenv("SLACK_CHANNEL_ID", "")).strip()
    if not token:
        raise SystemExit("Set SLACK_TOKEN in .env (copy .env.example).")
    if not args.all_channels and not channel_id:
        raise SystemExit("Set SLACK_CHANNEL_ID in .env, pass --channel, or use --all-channels.")

    max_messages = args.max_messages if args.max_messages is not None else env_int("SLACK_MAX_MESSAGES", 200)
    if max_messages <= 0:
        raise SystemExit("--max-messages / SLACK_MAX_MESSAGES must be greater than zero.")
    page_size = max(1, min(args.page_size, MAX_PAGE_SIZE))

    check_token_shape(token)
    client = WebClient(token=token)
    try:
        self_id = verify_auth(client)
        # One list of (channel, label, destination), so the single-channel and
        # all-channels paths share every line below rather than drifting apart.
        targets = (
            [(cid, name, args.out_dir / f"real_{cid}.json") for cid, name in member_channels(client)]
            if args.all_channels
            else [(channel_id, channel_id, args.out)]
        )
        if args.all_channels:
            log.info("Bot is a member of %d channel(s)", len(targets))
        for cid, name, out in targets:
            oldest = newest_ts(out) if args.since_last else ""
            if oldest:
                log.info("#%s: only what is newer than %s", name, format_ts(oldest))
            else:
                log.info("#%s: up to %d parent message(s)", name, max_messages)
            fresh = export_channel(client, cid, max_messages, page_size, oldest, self_id)
            if oldest:
                previous = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
                combined = merge_exports(previous, fresh)
                log.info("#%s: +%d new, %d total", name, len(combined) - len(previous), len(combined))
            else:
                combined = list(fresh)
                if not combined:
                    log.warning("#%s: nothing returned — empty channel, or the app is not a member?", name)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("Saved %d parent message(s) to %s", len(combined), out)
    except SlackApiError as error:
        code = str((error.response or {}).get("error", "unknown_error"))
        hint = ERROR_HINTS.get(code, "See https://api.slack.com/methods for this error code.")
        raise SystemExit(f"Slack API error '{code}': {hint}") from error


if __name__ == "__main__":
    main()
