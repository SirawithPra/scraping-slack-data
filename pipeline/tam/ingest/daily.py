"""The whole morning refresh, as one command.

    python3 -m tam.ingest.daily
    python3 -m tam.ingest.daily --dry-run
    python3 -m tam.ingest.daily --reindex-url http://127.0.0.1:8899

Three steps have to happen in order every morning, and doing them by hand is the
reason they do not happen: export what is new from every channel the bot was invited
to, merge it into the corpus, and tell the running dashboard to rebuild. Each step
already exists as its own module; this is the driver, so the order and the failure
handling live in one place instead of in somebody's shell history.

Design decisions worth stating, because each one is a way this can go wrong quietly:

* **It stops at the first failure.** A refresh that half-succeeds leaves a corpus
  holding some channels' new messages and not others, and the digest that follows
  looks complete. Better to report step two failed and leave yesterday's corpus
  whole — the dashboard keeps serving the last good build either way.
* **The corpus is only rewritten if the export produced something.** Slack returning
  nothing new is the normal case on a quiet morning, not an error, and rewriting the
  file for it would churn the embedding cache for no reason.
* **Reindex is best-effort and reported.** The dashboard is a separate process that
  may not be running; that is not a reason to fail the ingest that already succeeded.
  Silence would be, so a skipped reindex says so and names the command to run.
* **`--dry-run` prints the plan without calling Slack.** The rate limit is roughly one
  history request per minute, so "what would this do" needs to be answerable without
  spending the budget to find out.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from tam.core import DEFAULT_RECORDS, format_timestamp, read_records, TamDataError
from tam.ingest.export_slack import ERROR_HINTS, check_token_shape, export_channel, member_channels, merge_exports, newest_ts, verify_auth
from tam.ingest.prepare_messages import load_export, merge_records, prepare
from tam.ingest.youtrack import MIN_TICKET_CHARS, YouTrackError, fetch_tickets
from tam.core import write_records
from tam.ingest.quoted import annotate, bot_ids
from tam.ingest.users import load_names

log = logging.getLogger("daily")

DEFAULT_RAW_DIR = Path("data/raw")


def reindex(url: str, token: str) -> str:
    """Ask a running dashboard to rebuild. Returns a line describing what happened."""
    request = urllib.request.Request(
        url.rstrip("/") + "/api/reindex",
        method="POST",
        headers={"X-TAM-Token": token, "Origin": url.rstrip("/")},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.load(response)
        return f"rebuilt: {body.get('records')} record(s) at {body.get('built_at')}"
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        return f"reindex refused ({error.code}): {detail}"
    except OSError as error:
        return f"no dashboard at {url} ({error}) — start one, or reindex later"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR, help=f"Per-channel exports live here (default {DEFAULT_RAW_DIR})")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Corpus to merge into (default {DEFAULT_RECORDS})")
    parser.add_argument("--max-messages", type=int, default=200, help="Cap per channel on a first, non-incremental pass")
    # `or` rather than getenv's fallback: a variable present but empty must mean
    # "unset", which is how every other setting here behaves and what makes it safe to
    # ship .env.example with the name visible and no value. getenv's default only
    # applies when the key is absent, so `TAM_API_URL=` would have made this an empty
    # string and broken the reindex with no message.
    parser.add_argument(
        "--reindex-url",
        default=os.getenv("TAM_API_URL", "").strip() or "http://127.0.0.1:8899",
        help="Dashboard to rebuild afterwards (default http://127.0.0.1:8899)",
    )
    parser.add_argument("--no-reindex", action="store_true", help="Skip the rebuild; just refresh the corpus")
    parser.add_argument("--no-tickets", action="store_true", help="Skip the ticket refresh; Slack only")
    parser.add_argument("--dry-run", action="store_true", help="Say what would happen without calling Slack")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    args = parse_args()

    token = os.getenv("SLACK_TOKEN", "").strip()
    if not token:
        raise SystemExit("SLACK_TOKEN is not set. It needs history scopes plus users:read; see .env.example.")
    check_token_shape(token)
    client = WebClient(token=token)

    try:
        self_id = verify_auth(client)
        channels = member_channels(client)
    except SlackApiError as error:
        code = str((error.response or {}).get("error", "unknown_error"))
        raise SystemExit(f"Slack API error '{code}': {ERROR_HINTS.get(code, 'see https://api.slack.com/methods')}") from error

    log.info("Bot is a member of %d channel(s)", len(channels))
    if args.dry_run:
        print("\nแผนที่จะทำ (ยังไม่เรียก Slack เพิ่ม):")
        for cid, name in channels:
            out = args.raw_dir / f"real_{cid}.json"
            since = newest_ts(out)
            plan = f"ต่อจาก {format_timestamp(since)}" if since else f"ดึงใหม่ทั้งหมด (สูงสุด {args.max_messages})"
            print(f"  #{name:38} {plan}")
        print(f"\n  แล้ว merge เข้า {args.records} และสั่ง rebuild ที่ {args.reindex_url}")
        return

    # ---- step 1: export ----------------------------------------------------
    fresh_total = 0
    for cid, name in channels:
        out = args.raw_dir / f"real_{cid}.json"
        oldest = newest_ts(out)
        try:
            fresh = export_channel(client, cid, args.max_messages, 200, oldest, self_id)
        except SlackApiError as error:
            code = str((error.response or {}).get("error", "unknown_error"))
            raise SystemExit(f"#{name}: Slack API error '{code}': {ERROR_HINTS.get(code, '')}") from error
        previous = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
        combined = merge_exports(previous, fresh) if oldest else list(fresh)
        added = len(combined) - len(previous)
        fresh_total += max(0, added)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("#%s: +%d new, %d total", name, added, len(combined))

    # ---- step 2: the tickets ----------------------------------------------
    # Deliberately before the quiet-morning check and not inside it. A ticket moving
    # from "In progress" to "Ready for test" is news even on a morning when nobody
    # posted, and the whole point of the second source is that it changes when Slack
    # does not — so refreshing tickets only when Slack was busy would hide exactly the
    # cases the comparison exists to find.
    ticket_count = 0
    if not args.no_tickets:
        try:
            issues = fetch_tickets()
            if issues:
                records = [issue.as_record() for issue in issues]
                records = [r for r in records if len(str(r.get("text", "")).strip()) >= MIN_TICKET_CHARS]
                existing = read_records(args.records, include_threads=True) if args.records.exists() else []
                combined, replaced = merge_records(existing, records)
                write_records(args.records, combined)
                ticket_count = len(records)
                log.info("tickets: %d fetched, %d replaced, %d record(s) in corpus", len(records), replaced, len(combined))
        except YouTrackError as error:
            # Not fatal: the Slack half of the refresh is still worth doing, and a
            # tracker that cannot be read has to say so rather than look like agreement.
            print(f"\n  ข้าม ticket: {error}")

    if not fresh_total:
        # The quiet-morning path. Rewriting the corpus for zero new messages would
        # re-embed nothing useful and churn the cache, so stop here and say why —
        # unless the tickets changed, which is its own reason to rebuild.
        print(f"\nไม่มีข้อความใหม่ในทั้ง {len(channels)} ช่อง", end="")
        if not ticket_count:
            print(" — ไม่แตะ corpus และไม่ rebuild")
            return
        print(f" · แต่ดึง ticket มา {ticket_count} ใบ — rebuild ต่อ")

    # ---- step 3: merge the messages into the corpus -----------------------
    bots = bot_ids(load_names())
    merged_count = 0
    for cid, name in channels:
        raw = args.raw_dir / f"real_{cid}.json"
        if not raw.exists():
            continue
        records = annotate(prepare(load_export(raw)), bots=bots)
        existing = read_records(args.records, include_threads=True) if args.records.exists() else []
        combined, replaced = merge_records(existing, records)
        write_records(args.records, combined)
        merged_count = len(combined)
        log.info("#%s: merged, %d replaced, %d record(s) in corpus", name, replaced, merged_count)
    if not merged_count and args.records.exists():
        merged_count = len(read_records(args.records, include_threads=True))

    # ---- step 4: rebuild the dashboard ------------------------------------
    if args.no_reindex:
        print(f"\ncorpus: {merged_count} record(s) · ข้าม rebuild ตามที่สั่ง")
        return
    admin = os.getenv("TAM_ADMIN_TOKEN", "").strip()
    if not admin:
        print(f"\ncorpus: {merged_count} record(s)")
        print("  ข้าม rebuild: ไม่ได้ตั้ง TAM_ADMIN_TOKEN — route ที่เขียนข้อมูลต้องมี token")
        print(f"  สั่งเองได้: curl -X POST -H 'X-TAM-Token: <token>' {args.reindex_url}/api/reindex")
        return
    print(f"\ncorpus: {merged_count} record(s) · {reindex(args.reindex_url, admin)}")


if __name__ == "__main__":
    main()
