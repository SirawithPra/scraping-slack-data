#!/bin/bash
# Install the two launchd jobs. Idempotent: run it again after editing a template.
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

# Fail before installing anything, rather than leaving a job that cannot run.
[ -x "$VENV/bin/python" ] || { echo "✕ ไม่เจอ venv ที่ $VENV — สร้างก่อน: python3 -m venv .venv"; exit 1; }
[ -f "$PIPELINE/.env" ]   || { echo "✕ ไม่เจอ $PIPELINE/.env — cp .env.example .env แล้วเติม token"; exit 1; }
[ -f "$PIPELINE/$RECORDS" ] || { echo "✕ ไม่เจอ corpus ที่ $PIPELINE/$RECORDS"; echo "  สร้างก่อนด้วย: cd pipeline && python3 -m tam.ingest.daily"; exit 1; }
grep -q '^SLACK_TOKEN=.\+'      "$PIPELINE/.env" || { echo "✕ SLACK_TOKEN ว่างใน pipeline/.env"; exit 1; }
grep -q '^TAM_ADMIN_TOKEN=.\+'  "$PIPELINE/.env" || echo "⚠  ไม่ได้ตั้ง TAM_ADMIN_TOKEN — daily จะข้ามขั้น rebuild"

mkdir -p "$LOGS" "$AGENTS"

for job in com.tam.dashboard com.tam.daily; do
  sed -e "s|__VENV__|$VENV|g" -e "s|__PIPELINE__|$PIPELINE|g" \
      -e "s|__LOGS__|$LOGS|g" -e "s|__RECORDS__|$RECORDS|g" \
      "$DEPLOY/$job.plist.template" > "$AGENTS/$job.plist"
  # bootout first so a re-run replaces the definition instead of erroring on a
  # label that is already loaded. It fails when nothing is loaded, which is fine.
  launchctl bootout "gui/$(id -u)/$job" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$job.plist"
  echo "  ✓ โหลด $job"
done

echo
echo "dashboard  → http://localhost:8899   (เปิดใหม่เองถ้าตาย)"
echo "daily      → 08:30 ทุกวัน            (เครื่องหลับอยู่ก็รันเมื่อตื่น)"
echo "log        → $LOGS/"
echo
echo "เช็ค:      launchctl list | grep com.tam"
echo "รันเดี๋ยวนี้: launchctl kickstart -k gui/$(id -u)/com.tam.daily"
echo "ถอน:       $DEPLOY/uninstall.sh"
