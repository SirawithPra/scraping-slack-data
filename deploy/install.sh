#!/bin/bash
# Install the three launchd jobs. Idempotent: run it again after editing a template.
#
# Paths are substituted rather than hardcoded because a plist needs absolute paths and
# this repo can live anywhere. Everything is derived from this script's own location,
# so there is nothing to keep in sync by hand.
set -euo pipefail

DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DEPLOY/.." && pwd)"
PIPELINE="$REPO/pipeline"
VENV="$REPO/.venv"
LOGS="$DEPLOY/logs"
AGENTS="$HOME/Library/LaunchAgents"
RECORDS="${TAM_RECORDS:-data/processed/real_all.json}"
BOT="$REPO/slack-bot"
# launchd starts with a bare PATH, so node has to be named absolutely. Resolved here
# rather than hardcoded because Homebrew's prefix differs on Intel and Apple silicon.
NODE="$(command -v node || true)"
NODEBIN="$(dirname "${NODE:-/usr/local/bin/node}")"

# Fail before installing anything, rather than leaving a job that cannot run.
[ -x "$VENV/bin/python" ] || { echo "✕ ไม่เจอ venv ที่ $VENV — สร้างก่อน: python3 -m venv .venv"; exit 1; }
[ -f "$PIPELINE/.env" ]   || { echo "✕ ไม่เจอ $PIPELINE/.env — cp .env.example .env แล้วเติม token"; exit 1; }
[ -f "$PIPELINE/$RECORDS" ] || { echo "✕ ไม่เจอ corpus ที่ $PIPELINE/$RECORDS"; echo "  สร้างก่อนด้วย: cd pipeline && python3 -m tam.ingest.daily"; exit 1; }
grep -q '^SLACK_TOKEN=.\+'      "$PIPELINE/.env" || { echo "✕ SLACK_TOKEN ว่างใน pipeline/.env"; exit 1; }
grep -q '^TAM_ADMIN_TOKEN=.\+'  "$PIPELINE/.env" || echo "⚠  ไม่ได้ตั้ง TAM_ADMIN_TOKEN — daily จะข้ามขั้น rebuild"

# The bot is optional: install the other two and say why it was skipped, rather than
# refusing to install anything because Slack is not set up yet.
BOT_OK=1
[ -n "$NODE" ] || { echo "⚠  ไม่เจอ node — ข้าม com.tam.bot"; BOT_OK=0; }
[ -d "$BOT/node_modules" ] || { echo "⚠  ไม่เจอ $BOT/node_modules — cd slack-bot && npm install แล้วรันสคริปต์นี้อีกครั้ง (ข้าม com.tam.bot)"; BOT_OK=0; }
[ -f "$BOT/.env" ] || { echo "⚠  ไม่เจอ $BOT/.env — ข้าม com.tam.bot"; BOT_OK=0; }
if [ "$BOT_OK" = 1 ]; then
  for key in SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_SIGNING_SECRET; do
    grep -q "^$key=.\+" "$BOT/.env" || { echo "⚠  $key ว่างใน slack-bot/.env — ข้าม com.tam.bot"; BOT_OK=0; }
  done
fi
if [ "$BOT_OK" = 1 ] && ! grep -q '^TAM_API_URL=.\+' "$BOT/.env"; then
  echo "⚠  ไม่ได้ตั้ง TAM_API_URL ใน slack-bot/.env — บอทจะตัดสินใจเองแทนที่จะอ่านจาก pipeline"
fi

mkdir -p "$LOGS" "$AGENTS"

JOBS="com.tam.dashboard com.tam.daily"
[ "$BOT_OK" = 1 ] && JOBS="$JOBS com.tam.bot"

for job in $JOBS; do
  sed -e "s|__VENV__|$VENV|g" -e "s|__PIPELINE__|$PIPELINE|g" \
      -e "s|__LOGS__|$LOGS|g" -e "s|__RECORDS__|$RECORDS|g" \
      -e "s|__NODE__|$NODE|g" -e "s|__NODEBIN__|$NODEBIN|g" -e "s|__BOT__|$BOT|g" \
      "$DEPLOY/$job.plist.template" > "$AGENTS/$job.plist"
  # bootout first so a re-run replaces the definition instead of erroring on a
  # label that is already loaded. It fails when nothing is loaded, which is fine.
  launchctl bootout "gui/$(id -u)/$job" 2>/dev/null || true
  # launchd does not finish tearing a label down before bootout returns, and
  # bootstrapping the same label inside that gap fails with EIO (5). Retry rather than
  # abort: the first run of this script installed nothing at all because of it, and
  # left the previous job booted out.
  for attempt in 1 2 3 4 5; do
    if launchctl bootstrap "gui/$(id -u)" "$AGENTS/$job.plist" 2>/dev/null; then
      echo "  ✓ โหลด $job"
      break
    fi
    if [ "$attempt" = 5 ]; then
      echo "  ✕ โหลด $job ไม่ได้ — ลองเอง: launchctl bootstrap gui/$(id -u) $AGENTS/$job.plist"
      exit 1
    fi
    sleep 2
  done
done

echo
echo "dashboard  → http://localhost:8899   (เปิดใหม่เองถ้าตาย)"
echo "daily      → 08:30 ทุกวัน            (เครื่องหลับอยู่ก็รันเมื่อตื่น)"
if [ "$BOT_OK" = 1 ]; then
  echo "bot        → Socket Mode            (/meowtam ใช้ได้ตลอด ไม่ต้องเปิด terminal)"
  echo "             ตอน login บอทมักล้มสองสามครั้งก่อนต่อได้ เพราะรอ dashboard โหลดโมเดลเสร็จ — ปกติ"
else
  echo "bot        → ข้าม (ดูคำเตือนข้างบน)"
fi
echo "log        → $LOGS/"
echo
echo "เช็ค:      launchctl list | grep com.tam"
echo "รันเดี๋ยวนี้: launchctl kickstart -k gui/$(id -u)/com.tam.daily"
echo "ถอน:       $DEPLOY/uninstall.sh"
