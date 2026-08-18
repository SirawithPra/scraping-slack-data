#!/bin/bash
# Remove both jobs. Touches nothing else: no data, no .env, no logs.
set -euo pipefail
AGENTS="$HOME/Library/LaunchAgents"
for job in com.tam.dashboard com.tam.daily; do
  launchctl bootout "gui/$(id -u)/$job" 2>/dev/null && echo "  ✓ หยุด $job" || echo "  · $job ไม่ได้โหลดอยู่"
  rm -f "$AGENTS/$job.plist"
done
echo
echo "ถอนแล้ว — ข้อมูล, .env และ log ยังอยู่ครบ"
