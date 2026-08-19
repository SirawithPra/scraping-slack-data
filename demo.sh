#!/bin/bash
# One entry point for demo day: check, reset, start, share, stop.
#
# Everything here already existed as a documented command — `deploy/install.sh` keeps
# the dashboard and the bot alive under launchd, `docs/DEMO_RUNBOOK.md` spells out the
# reset, `cloudflared` publishes a local port. What did not exist was one place that
# does them in the right order and says, on one screen, whether the demo is ready.
# Typing five commands correctly under stage lights is the failure this removes.
#
#   ./demo.sh            สถานะทุกอย่างในหน้าจอเดียว
#   ./demo.sh reset      ล้างของรอบซ้อม (สำรองไว้ก่อนเสมอ)
#   ./demo.sh up         ให้แน่ใจว่า dashboard + bot รันอยู่
#   ./demo.sh restart [bot|dashboard]   รีสตาร์ท (ไม่ใส่ = ทั้งคู่ · dashboard ใช้เวลาหลายนาที)
#   ./demo.sh share      เปิด URL สาธารณะให้คนอื่นเข้าดู dashboard
#   ./demo.sh unshare    ปิด URL นั้น
#   ./demo.sh restore    เอาข้อมูลก่อน reset กลับมา
#   ./demo.sh snapshot [--fresh]        เก็บสถานะตอนนี้ (ปกติ reset เก็บให้อยู่แล้ว)
#   ./demo.sh logs [dashboard|bot|tunnel]
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE="$REPO/pipeline"
BOT="$REPO/slack-bot"
LOGS="$REPO/deploy/logs"
SNAP="$REPO/.demo-snapshot"
PORT="${TAM_PORT:-8899}"
RECORDS="${TAM_RECORDS:-data/processed/real_all.json}"
HEALTH="http://127.0.0.1:$PORT/api/health"
GUI="gui/$(id -u)"

mkdir -p "$LOGS"

ok()    { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()   { printf '  \033[31m✕\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$1"; }
title() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# launchd is how this machine actually runs both halves (deploy/install.sh). When a job
# is loaded, start and restart go through launchctl — spawning a second copy would fight
# KeepAlive over the port and the loser would be restarted forever.
loaded()  { launchctl list 2>/dev/null | grep -q "	$1\$"; }
pid_of()  { launchctl list 2>/dev/null | awk -v j="$1" '$3==j {print $1}'; }

health_json() { curl -sf --max-time 5 "$HEALTH" 2>/dev/null; }

# One field out of /api/health without jq, which is not installed everywhere.
field() { python3 -c 'import json,sys;print(json.load(sys.stdin).get(sys.argv[1],""))' "$1" 2>/dev/null; }

count_of() { python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "$1" 2>/dev/null || echo '?'; }

tunnel_url()   { [ -f "$LOGS/tunnel.url" ] && cat "$LOGS/tunnel.url"; }
tunnel_alive() { [ -f "$LOGS/tunnel.pid" ] && kill -0 "$(cat "$LOGS/tunnel.pid")" 2>/dev/null; }

wait_health() {
  # The dashboard loads a 2.1 GB embedding model before it answers: minutes on a cold
  # start, instant on a warm one. Poll rather than sleep a fixed guess.
  local tries=${1:-60}
  printf '  รอ dashboard ตอบ'
  for _ in $(seq "$tries"); do
    if health_json >/dev/null; then printf ' พร้อม\n'; return 0; fi
    printf '.'; sleep 3
  done
  printf ' ไม่ตอบ\n'
  return 1
}

cmd_status() {
  title "dashboard (port $PORT)"
  local json
  json=$(health_json)
  if [ -n "$json" ]; then
    ok "ตอบแล้ว — $(printf '%s' "$json" | field records) ข้อความ · $(printf '%s' "$json" | field topics) กลุ่มงาน · build เมื่อ $(printf '%s' "$json" | field built_at)"
    ok "corpus: $(printf '%s' "$json" | field records_path) · หน้าต่าง digest $(printf '%s' "$json" | field window_days) วัน"
    [ "$(printf '%s' "$json" | field rebuilding)" = "True" ] && warn "กำลัง build ใหม่อยู่ — รอให้เสร็จก่อนเดโม"
  else
    bad "ไม่ตอบที่ $HEALTH — สั่ง ./demo.sh up"
  fi
  if loaded com.tam.dashboard; then ok "launchd: com.tam.dashboard (pid $(pid_of com.tam.dashboard))"
  else warn "ไม่ได้ลง launchd — ./demo.sh up จะรันแบบ nohup ให้แทน (ดู deploy/README.md)"; fi

  title "bot"
  if loaded com.tam.bot && [ "$(pid_of com.tam.bot)" != "-" ]; then
    ok "launchd: com.tam.bot (pid $(pid_of com.tam.bot))"
  elif pgrep -f 'tsx src/app.ts' >/dev/null; then
    ok "รันอยู่ (ไม่ผ่าน launchd) pid $(pgrep -f 'tsx src/app.ts' | head -1)"
  else
    bad "ไม่ได้รันอยู่ — สั่ง ./demo.sh up"
  fi
  if grep -q '^TAM_API_URL=.\+' "$BOT/.env" 2>/dev/null; then ok "บอทอ่านจาก pipeline (TAM_API_URL ตั้งแล้ว)"
  else warn "TAM_API_URL ว่าง — บอทจะใช้ fixture ของตัวเอง ไม่ใช่ข้อมูลจริง"; fi
  if grep -q '^ENABLE_SCHEDULE=1' "$BOT/.env" 2>/dev/null; then warn "ENABLE_SCHEDULE=1 — งานตามเวลาจะยิงเองระหว่างเดโม"
  else ok "งานตามเวลาปิดอยู่ — ทุกอย่างยิงด้วย /mt demo"; fi

  title "ข้อมูลที่ค้างจากรอบซ้อม"
  local n
  for f in dailies announcements decisions; do
    n=$(count_of "$BOT/data/$f.json")
    if [ "$n" = "0" ]; then ok "$f.json ว่าง"; else warn "$f.json มี $n รายการ — ./demo.sh reset ถ้าจะเริ่มใหม่"; fi
  done

  title "URL"
  echo "  dashboard   http://127.0.0.1:$PORT/"
  if tunnel_alive; then ok "สาธารณะ    $(tunnel_url)"; else echo "  สาธารณะ     ยังไม่เปิด — ./demo.sh share"; fi
  echo
}

# The snapshot is taken once and then left alone. Reset runs more than once — before
# a rehearsal, then again between rehearsals — and every run after the first has
# nothing worth keeping in front of it: the files hold seeded mornings by then. Copying
# those over the snapshot replaces the one thing it exists for, the state from before
# any of this started, with the state this script just created. That happened once at
# 05:44 on 20 Aug 2026 and cost the real dailies; `--fresh` is now the only way to
# overwrite, and it keeps the old one beside it rather than dropping it.
take_snapshot() {
  mkdir -p "$SNAP"
  cp "$BOT"/data/dailies.json "$BOT"/data/announcements.json "$BOT"/data/decisions.json "$SNAP/" 2>/dev/null
  cp "$PIPELINE"/data/link_overrides.json "$SNAP/" 2>/dev/null
  cp "$PIPELINE/$RECORDS" "$SNAP/" 2>/dev/null
}

cmd_snapshot() {
  if [ -f "$SNAP/dailies.json" ] && [ "${1:-}" != "--fresh" ]; then
    warn "มี snapshot อยู่แล้ว (เก็บเมื่อ $(stat -f '%Sm' -t '%d %b %H:%M' "$SNAP/dailies.json"))"
    echo "     ตัวนั้นคือสถานะ 'ก่อนเริ่มซ้อม' ซึ่งเป็นตัวที่อยากได้กลับ จึงไม่ทับให้"
    echo "     จะเก็บของตอนนี้แทนจริง ๆ: ./demo.sh snapshot --fresh (ของเดิมย้ายไป .demo-snapshot/prev/)"
    return 0
  fi
  if [ -f "$SNAP/dailies.json" ]; then
    rm -rf "$SNAP/prev"
    mkdir -p "$SNAP/prev"
    cp "$SNAP"/*.json "$SNAP/prev/" 2>/dev/null
    ok "ย้าย snapshot เดิมไป .demo-snapshot/prev/"
  fi
  take_snapshot
  ok "เก็บ snapshot ใหม่แล้วที่ .demo-snapshot/"
}

cmd_reset() {
  title "สำรองก่อน"
  if [ -f "$SNAP/dailies.json" ]; then
    ok "ใช้ snapshot เดิมที่มีอยู่ (เก็บเมื่อ $(stat -f '%Sm' -t '%d %b %H:%M' "$SNAP/dailies.json")) — ไม่ทับ"
    echo "     นั่นคือของก่อนเริ่มซ้อม · ถ้าอยากเก็บของตอนนี้แทน: ./demo.sh snapshot --fresh"
  else
    take_snapshot
    ok "คัดลอกไว้ที่ .demo-snapshot/ (gitignored) — เอากลับด้วย ./demo.sh restore"
  fi

  title "ล้าง"
  for f in dailies announcements decisions; do
    local before
    before=$(count_of "$BOT/data/$f.json")
    printf '[]\n' > "$BOT/data/$f.json"
    ok "$f.json: $before → 0"
  done
  echo
  echo "  เหลืออีกสองอย่างที่สั่งจากตรงนี้ไม่ได้ ต้องพิมพ์ใน Slack:"
  echo "    /mt demo clear    ลบคำตอบจำลองที่ค้างในเธรดของจริง"
  echo "    /mt demo reset    ตัวนับ beat กลับไป 1"
  echo "  ส่วนข้อความที่บอทโพสต์ไปแล้ว กับคอมเมนต์ใน YouTrack ต้องลบเอง"
  echo
}

cmd_restore() {
  [ -d "$SNAP" ] || { bad "ไม่มี .demo-snapshot/ — ยังไม่เคยสั่ง ./demo.sh reset"; exit 1; }
  title "เอาของเดิมกลับ"
  for f in dailies announcements decisions; do
    [ -f "$SNAP/$f.json" ] && cp "$SNAP/$f.json" "$BOT/data/$f.json" && ok "$f.json"
  done
  [ -f "$SNAP/link_overrides.json" ] && cp "$SNAP/link_overrides.json" "$PIPELINE/data/link_overrides.json" && ok "link_overrides.json"
  if [ -f "$SNAP/$(basename "$RECORDS")" ]; then
    cp "$SNAP/$(basename "$RECORDS")" "$PIPELINE/$RECORDS" && ok "corpus"
    local token
    token=$(grep '^TAM_ADMIN_TOKEN=' "$PIPELINE/.env" 2>/dev/null | cut -d= -f2-)
    if [ -n "$token" ]; then
      curl -sf -X POST -H "X-TAM-Token: $token" "http://127.0.0.1:$PORT/api/reindex" >/dev/null \
        && ok "สั่ง build index ใหม่แล้ว" || warn "สั่ง build ใหม่ไม่สำเร็จ — ./demo.sh restart แทน"
    else
      warn "ไม่มี TAM_ADMIN_TOKEN — corpus กลับมาแล้วแต่ index ยังเป็นของเก่า ให้ ./demo.sh restart"
    fi
  fi
  echo
}

start_dashboard() {
  if loaded com.tam.dashboard; then
    launchctl kickstart "$GUI/com.tam.dashboard" >/dev/null 2>&1
  else
    ( cd "$PIPELINE" && nohup "$REPO/.venv/bin/python" -m tam.web.server \
        --records "$RECORDS" --days 7 --port "$PORT" >> "$LOGS/dashboard.log" 2>&1 & )
  fi
}

start_bot() {
  if loaded com.tam.bot; then
    launchctl kickstart "$GUI/com.tam.bot" >/dev/null 2>&1
  else
    ( cd "$BOT" && nohup npm start >> "$LOGS/bot.log" 2>&1 & )
  fi
}

cmd_up() {
  title "dashboard"
  if health_json >/dev/null; then
    ok "รันอยู่แล้ว ไม่ต้องเปิดใหม่"
  else
    start_dashboard
    wait_health 60 || { bad "ไม่ขึ้น — ./demo.sh logs dashboard"; exit 1; }
  fi

  title "bot"
  if { loaded com.tam.bot && [ "$(pid_of com.tam.bot)" != "-" ]; } || pgrep -f 'tsx src/app.ts' >/dev/null; then
    ok "รันอยู่แล้ว"
  else
    start_bot
    sleep 4
    if pgrep -f 'tsx src/app.ts' >/dev/null; then ok "เปิดแล้ว"; else bad "ไม่ขึ้น — ./demo.sh logs bot"; exit 1; fi
  fi

  cmd_status
}

# Restarting the bot is a second; restarting the dashboard is minutes, because it
# re-reads a 2.1 GB embedding model before it answers anything. They are separable for
# that reason alone: a change to the bot's code should not cost the corpus its index.
cmd_restart() {
  local what="${1:-all}"
  title "รีสตาร์ท ($what)"
  if [ "$what" = all ] || [ "$what" = bot ]; then
    if loaded com.tam.bot; then launchctl kickstart -k "$GUI/com.tam.bot" >/dev/null 2>&1; ok "bot"
    else pkill -f 'tsx src/app.ts'; start_bot; ok "bot (nohup)"; fi
  fi
  if [ "$what" = all ] || [ "$what" = dashboard ]; then
    if loaded com.tam.dashboard; then
      launchctl kickstart -k "$GUI/com.tam.dashboard" >/dev/null 2>&1
      ok "dashboard — โหลดโมเดลใหม่ ใช้เวลาหลายนาที"
      wait_health 100 || bad "dashboard ยังไม่ตอบ — ./demo.sh logs dashboard"
    fi
  fi
  sleep 2
  cmd_status
}

cmd_share() {
  command -v cloudflared >/dev/null || { bad "ไม่มี cloudflared — brew install cloudflared"; exit 1; }
  health_json >/dev/null || { bad "dashboard ยังไม่ตอบ — ./demo.sh up ก่อน"; exit 1; }
  if tunnel_alive; then ok "เปิดอยู่แล้ว: $(tunnel_url)"; return 0; fi

  printf '\n\033[33m⚠ URL นี้เปิดให้ใครก็ได้ที่มีลิงก์\033[0m — หน้า dashboard มีข้อความ Slack จริงของทีม\n'
  printf '  ไม่มีรหัสผ่านกั้นฝั่งอ่าน (route ที่เขียนข้อมูลยังต้องใช้ token เหมือนเดิม)\n'
  printf '  ถ้าไม่อยากให้ชื่อจริงขึ้นจอ ตั้ง TAM_NAMES=pseudonym ทั้งสองฝั่งแล้ว ./demo.sh restart\n'
  printf '  ปิดเมื่อจบด้วย ./demo.sh unshare\n\n'

  : > "$LOGS/tunnel.log"
  nohup cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >> "$LOGS/tunnel.log" 2>&1 &
  echo $! > "$LOGS/tunnel.pid"

  printf '  กำลังขอ URL'
  local url=''
  for _ in $(seq 30); do
    url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGS/tunnel.log" | head -1)
    [ -n "$url" ] && break
    printf '.'; sleep 1
  done
  echo
  [ -n "$url" ] || { bad "ขอ URL ไม่ได้ — ./demo.sh logs tunnel"; exit 1; }
  echo "$url" > "$LOGS/tunnel.url"

  # The log is the authority on whether the tunnel is up, not a curl from this machine:
  # a laptop behind a filtering resolver cannot resolve the fresh hostname for a minute
  # or two while the rest of the world already can. Curl is the second opinion, not the
  # verdict — reporting "ไม่ติด" for a tunnel that is serving fine is the worse error.
  if grep -q 'Registered tunnel connection' "$LOGS/tunnel.log"; then
    ok "อุโมงค์ต่อกับ Cloudflare แล้ว ($(grep -oE 'location=[a-z0-9]+' "$LOGS/tunnel.log" | head -1 | cut -d= -f2))"
  fi
  if curl -sf --max-time 20 "$url/api/health" >/dev/null; then
    ok "ลองเรียกจากเครื่องนี้แล้ว เข้าได้"
  else
    warn "เครื่องนี้ยังเรียก URL ตัวเองไม่ได้ (DNS ของชื่อใหม่ยังไม่ถึง) — เปิดจากมือถือหรือเน็ตอื่นดูก่อนสรุปว่าพัง"
  fi
  printf '\n  \033[1m%s\033[0m\n\n' "$url"
}

cmd_unshare() {
  if tunnel_alive; then
    kill "$(cat "$LOGS/tunnel.pid")" 2>/dev/null
    ok "ปิด URL สาธารณะแล้ว"
  else
    ok "ไม่มี URL เปิดอยู่"
  fi
  rm -f "$LOGS/tunnel.pid" "$LOGS/tunnel.url"
}

cmd_logs() {
  case "${1:-dashboard}" in
    bot)    tail -40 -f "$LOGS/bot.log" ;;
    tunnel) tail -40 -f "$LOGS/tunnel.log" ;;
    *)      tail -40 -f "$LOGS/dashboard.log" ;;
  esac
}

case "${1:-status}" in
  status|check) cmd_status ;;
  reset)        cmd_reset ;;
  restore)      cmd_restore ;;
  snapshot)     cmd_snapshot "${2:-}" ;;
  up|start)     cmd_up ;;
  restart)      cmd_restart "${2:-all}" ;;
  share)        cmd_share ;;
  unshare)      cmd_unshare ;;
  logs)         cmd_logs "${2:-dashboard}" ;;
  *)            sed -n '10,18p' "$0" | sed 's/^#  \{0,1\}//'; exit 1 ;;
esac
