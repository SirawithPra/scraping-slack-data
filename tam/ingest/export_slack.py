"""Export a Slack channel's messages and thread replies to data/raw/slack_messages.json.

Raw message text is stored verbatim; cleaning happens in prepare_messages.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

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


def verify_auth(client: WebClient) -> None:
    """Confirm the token works before paging, and log what it is connected to."""
    response = call_slack(client.auth_test)
    log.info(
        "Authenticated as %s in workspace %s",
        response.get("user") or response.get("bot_id") or "unknown",
        response.get("team") or "unknown",
    )


def next_cursor(response: Any) -> str | None:
    return (response.get("response_metadata") or {}).get("next_cursor") or None


def fetch_parent_messages(
    client: WebClient, channel_id: str, max_messages: int, page_size: int
) -> list[dict[str, Any]]:
    """Page through conversations.history until max_messages is reached."""
    messages: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(messages) < max_messages:
        response = call_slack(
            client.conversations_history,
            channel=channel_id,
            limit=min(page_size, max_messages - len(messages)),
            cursor=cursor,
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
    client: WebClient, channel_id: str, max_messages: int, page_size: int
) -> list[dict[str, Any]]:
    parents = fetch_parent_messages(client, channel_id, max_messages, page_size)
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
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    args = parse_args()

    token = os.getenv("SLACK_TOKEN", "").strip()
    channel_id = (args.channel or os.getenv("SLACK_CHANNEL_ID", "")).strip()
    if not token or not channel_id:
        raise SystemExit("Set SLACK_TOKEN and SLACK_CHANNEL_ID in .env (copy .env.example).")

    max_messages = args.max_messages if args.max_messages is not None else env_int("SLACK_MAX_MESSAGES", 200)
    if max_messages <= 0:
        raise SystemExit("--max-messages / SLACK_MAX_MESSAGES must be greater than zero.")
    page_size = max(1, min(args.page_size, MAX_PAGE_SIZE))

    check_token_shape(token)
    log.info("Exporting up to %d parent message(s) from %s", max_messages, channel_id)
    client = WebClient(token=token)
    try:
        verify_auth(client)
        exported = export_channel(client, channel_id, max_messages, page_size)
    except SlackApiError as error:
        code = str((error.response or {}).get("error", "unknown_error"))
        hint = ERROR_HINTS.get(code, "See https://api.slack.com/methods for this error code.")
        raise SystemExit(f"Slack API error '{code}': {hint}") from error

    if not exported:
        log.warning("No messages returned. Is the channel empty, or is the app not a member?")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Saved %d parent message(s) to %s", len(exported), args.out)


if __name__ == "__main__":
    main()
