/**
 * raw-slack.json → ledger.json
 *
 * Groups messages into work items, computes a state for each with the evidence
 * that proves it, and detects scope drift and decisions.
 *
 * The honest description of what this is: a cue-list and clustering pass, not a
 * language model. That is a deliberate choice, not a shortcut —
 *   · it runs offline in under a second, so nothing can fail in front of judges
 *   · the rules are readable and editable, so when it is wrong you can see why
 *   · a cue list you can read beats a prompt you can only re-roll
 * The LLM's proper job here is the one-line prose summary, and that is the part
 * the UI labels as generated and never presents as fact.
 *
 *   npm run ledger
 */
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Ledger, WorkItem, Message, State, TimelineEvent } from '../src/types.js';

const here = dirname(fileURLToPath(import.meta.url));
const RAW = resolve(here, '../data/raw-slack.json');
const OUT = resolve(here, '../data/ledger.json');

if (!existsSync(RAW)) {
  console.error('ไม่เจอ data/raw-slack.json — รัน `npm run export` ก่อน');
  process.exit(1);
}

interface RawMsg {
  id: string; channel: string; channel_name: string; user: string; user_id: string;
  ts: string; when: string; text: string; thread_ts?: string; permalink?: string; reactions?: string[];
}

const raw: RawMsg[] = JSON.parse(readFileSync(RAW, 'utf8'));

/* ── Cue lists ──────────────────────────────────────────────────────
 * The highest-leverage tuning knob in the whole system. Grow these from
 * real misses, not from imagination. Thai first — that is what your team
 * actually writes in.
 * ------------------------------------------------------------------ */

const BLOCKED_CUES = [
  'ยังรอ', 'รออยู่', 'ติดอยู่', 'ติดตรง', 'ยังไปต่อไม่ได้', 'ยังไม่ได้', 'ยังไม่มี',
  'ขอ', 'รอ api', 'รอ design', 'รอ review', 'ยังไม่เสร็จ', 'มีปัญหา', 'ทำไม่ได้',
  'blocked', 'blocker', 'waiting on', 'waiting for', 'stuck', 'cannot', "can't proceed",
  'need help', 'depends on', 'on hold',
];

const RESOLVED_CUES = [
  'เสร็จแล้ว', 'เรียบร้อย', 'ปิดได้', 'merge แล้ว', 'deploy แล้ว', 'fix แล้ว', 'ผ่านแล้ว',
  'done', 'merged', 'deployed', 'resolved', 'fixed', 'shipped', 'closed', 'lgtm',
];

const SCOPE_CUES = [
  'เปลี่ยนเป็น', 'ขอแก้', 'ขอเปลี่ยน', 'ไม่เอาแล้ว', 'เพิ่มอีก', 'ตัดออก', 'สโคปเปลี่ยน',
  'requirement เปลี่ยน', 'ลูกค้าขอ',
  'actually', 'instead of', 'scope change', 'changed requirement', 'new requirement',
  'let\'s not', 'drop that', 'also need',
];

const DECISION_CUES = [
  'สรุปว่า', 'ตกลงว่า', 'เอาแบบนี้', 'ใช้แบบ', 'ตัดสินใจ', 'final',
  'we\'ll go with', 'decision', 'agreed', 'let\'s go with', 'final call',
];

const TICKET_RE = /\b([A-Z][A-Z0-9]{1,9}-\d+)\b/g;

const hit = (text: string, cues: string[]) => {
  const t = text.toLowerCase();
  return cues.find((c) => t.includes(c.toLowerCase()));
};

/* ── Clustering ─────────────────────────────────────────────────────
 * Precedence, highest confidence first:
 *   1. explicit ticket key in the message
 *   2. thread inheritance — replies inherit the parent's link
 *   3. character-trigram similarity to an existing cluster
 * Anything below the floor goes to `unassigned`, which is *shown* in the
 * digest, never silently dropped.
 * ------------------------------------------------------------------ */

const SIM_FLOOR = 0.12;

function grams(s: string): Set<string> {
  const n = s.toLowerCase().replace(/\s+/g, ' ');
  const out = new Set<string>();
  for (let i = 0; i <= n.length - 3; i++) out.add(n.slice(i, i + 3));
  return out;
}
function jaccard(a: Set<string>, b: Set<string>): number {
  if (!a.size || !b.size) return 0;
  let inter = 0;
  const [small, large] = a.size <= b.size ? [a, b] : [b, a];
  for (const g of small) if (large.has(g)) inter++;
  return inter / (a.size + b.size - inter);
}

interface Cluster { key: string; msgs: RawMsg[]; grams: Set<string>; explicit: boolean }

const clusters: Cluster[] = [];
const threadToCluster = new Map<string, Cluster>();
const unassigned: RawMsg[] = [];
let synthetic = 0;

for (const m of raw) {
  const keys = [...m.text.matchAll(TICKET_RE)].map((x) => x[1]!);

  // 1. explicit
  if (keys.length) {
    const key = keys[0]!;
    let c = clusters.find((x) => x.key === key);
    if (!c) {
      c = { key, msgs: [], grams: new Set(), explicit: true };
      clusters.push(c);
    }
    c.explicit = true;
    c.msgs.push(m);
    for (const g of grams(m.text)) c.grams.add(g);
    if (m.thread_ts) threadToCluster.set(m.thread_ts, c);
    else threadToCluster.set(m.ts, c);
    continue;
  }

  // 2. thread inheritance
  const parent = m.thread_ts ? threadToCluster.get(m.thread_ts) : undefined;
  if (parent) {
    parent.msgs.push(m);
    for (const g of grams(m.text)) parent.grams.add(g);
    continue;
  }

  // 3. similarity
  const g = grams(m.text);
  let best: Cluster | undefined;
  let bestScore = 0;
  for (const c of clusters) {
    const s = jaccard(g, c.grams);
    if (s > bestScore) { bestScore = s; best = c; }
  }
  if (best && bestScore >= SIM_FLOOR) {
    best.msgs.push(m);
    for (const x of g) best.grams.add(x);
    if (!m.thread_ts) threadToCluster.set(m.ts, best);
    continue;
  }

  // Long messages that match nothing are probably a new topic; short ones
  // ("ok", "ครับ") are almost certainly noise and belong in unassigned.
  if (m.text.length > 60) {
    const c: Cluster = { key: `TAM-${++synthetic}`, msgs: [m], grams: g, explicit: false };
    clusters.push(c);
    threadToCluster.set(m.thread_ts ?? m.ts, c);
  } else {
    unassigned.push(m);
  }
}

/* ── State ─────────────────────────────────────────────────────────
 * Read the messages newest-first and stop at the first cue that settles
 * it. A "done" said after a "blocked" wins, which is the behaviour you
 * want — the last word is the current state.
 * ------------------------------------------------------------------ */

const NOW = new Date(raw.at(-1)?.when.replace(' ', 'T') ?? Date.now());
const STALE_DAYS = 3;

function ageDays(when: string): number {
  return (NOW.getTime() - new Date(when.replace(' ', 'T')).getTime()) / 86_400_000;
}

function toMessage(m: RawMsg): Message {
  return {
    id: m.id, source: 'slack', user: m.user, when: m.when,
    text: m.text, permalink: m.permalink, channel: m.channel_name,
  };
}

function headlineOf(msgs: RawMsg[]): string {
  // The longest of the first three messages is usually the one that states
  // the problem; later messages are replies to it.
  const candidate = [...msgs.slice(0, 3)].sort((a, b) => b.text.length - a.text.length)[0];
  const line = (candidate?.text ?? '').split('\n')[0] ?? '';
  return line.length > 90 ? line.slice(0, 88) + '…' : line || '(ไม่มีหัวข้อ)';
}

const items: WorkItem[] = [];
const decisions: Ledger['decisions'] = [];
const drifts: Ledger['drifts'] = [];

for (const c of clusters) {
  const msgs = [...c.msgs].sort((a, b) => Number(a.ts) - Number(b.ts));
  const last = msgs.at(-1)!;

  let state: State = 'moving';
  let evidence = '';
  let evidenceId = last.id;

  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i]!;
    const r = hit(m.text, RESOLVED_CUES);
    if (r) { state = 'done'; evidence = `เสร็จแล้ว ${m.when} — คำที่จับได้ “${r}”`; evidenceId = m.id; break; }
    const b = hit(m.text, BLOCKED_CUES);
    if (b) { state = 'blocked'; evidence = `ติดตั้งแต่ ${m.when} — คำที่จับได้ “${b}”`; evidenceId = m.id; break; }
  }

  const idle = ageDays(last.when);
  if (state === 'moving' && idle >= STALE_DAYS) {
    state = 'stalled';
    evidence = `ไม่มีความเคลื่อนไหวตั้งแต่ ${last.when} — ${Math.round(idle)} วัน`;
    evidenceId = last.id;
  }
  if (!evidence) evidence = `มีความเคลื่อนไหวล่าสุด ${last.when}`;

  const timeline: TimelineEvent[] = [];
  for (const m of msgs) {
    const s = hit(m.text, SCOPE_CUES);
    if (s) timeline.push({ when: m.when, kind: 'scope_change', text: m.text.slice(0, 160), source: 'slack', user: m.user, evidence_id: m.id });
    const d = hit(m.text, DECISION_CUES);
    if (d) {
      timeline.push({ when: m.when, kind: 'decision', text: m.text.slice(0, 160), source: 'slack', user: m.user, evidence_id: m.id });
      decisions.push({
        id: `dec_${m.id}`, statement: m.text.slice(0, 300), when: m.when,
        user: m.user, source: 'slack', evidence_id: m.id, related_items: [c.key],
      });
    }
    const b = hit(m.text, BLOCKED_CUES);
    if (b) timeline.push({ when: m.when, kind: 'blocked', text: m.text.slice(0, 160), source: 'slack', user: m.user, evidence_id: m.id });
  }

  const participants = [...new Set(msgs.map((m) => m.user))];

  items.push({
    key: c.key,
    headline: headlineOf(msgs),
    state,
    evidence,
    evidence_id: evidenceId,
    age_days: Number(idle.toFixed(1)),
    assignee: participants[0],
    participants,
    sources: { slack: msgs.length },
    first: msgs[0]!.when,
    last: last.when,
    timeline,
    messages: msgs.map(toMessage),
  });

  // Drift: the scope changed in Slack and this is a real ticket. In the full
  // build we'd compare against YouTrack's description_updated; here every
  // explicit ticket with a scope cue is a candidate.
  const scopeEvent = timeline.filter((t) => t.kind === 'scope_change').at(-1);
  if (scopeEvent && c.explicit) {
    drifts.push({
      item_key: c.key,
      trigger_id: scopeEvent.evidence_id!,
      cue: hit(scopeEvent.text, SCOPE_CUES) ?? 'scope',
      detected: scopeEvent.when,
      current_description: '(ดึงจาก YouTrack — ยังไม่ได้ต่อ API)',
      proposed_description: scopeEvent.text,
    });
  }
}

/* ── Supersession ───────────────────────────────────────────────────
 * Two decisions about the same thing, later one wins. Similarity over the
 * statements, which is crude but catches the case that matters: "we said
 * UTF-8 without BOM in May, we said with BOM in August".
 * ------------------------------------------------------------------ */

for (let i = 0; i < decisions.length; i++) {
  for (let j = i + 1; j < decisions.length; j++) {
    const a = decisions[i]!, b = decisions[j]!;
    if (a.superseded_by) continue;
    if (jaccard(grams(a.statement), grams(b.statement)) > 0.25 && b.when > a.when) {
      a.superseded_by = b.id;
    }
  }
}

/* ── Standup drafts ─────────────────────────────────────────────────
 * What each person touched most recently, plus what of theirs has gone
 * quiet. The second list is the whole point — nobody volunteers it.
 * ------------------------------------------------------------------ */

const byUser = new Map<string, { id: string; name: string }>();
for (const m of raw) if (m.user_id) byUser.set(m.user_id, { id: m.user_id, name: m.user });

const standups: Ledger['standups'] = [...byUser.values()].map(({ id, name }) => {
  const mine = items.filter((i) => i.participants.includes(name));
  return {
    slack_user_id: id,
    display_name: name,
    yesterday: mine
      .filter((i) => ageDays(i.last) <= 2 && i.state !== 'done')
      .slice(0, 3)
      .map((i) => {
        const lastMine = [...i.messages].reverse().find((m) => m.user === name);
        return {
          key: i.key,
          headline: i.headline,
          note: lastMine ? `คุณบอกว่า: ${lastMine.text.slice(0, 140)}` : i.evidence,
          evidence_id: lastMine?.id,
        };
      }),
    carried_over: mine
      .filter((i) => i.state === 'stalled' || (i.state === 'blocked' && i.age_days >= STALE_DAYS))
      .slice(0, 3)
      .map((i) => ({ key: i.key, headline: i.headline, stale_days: i.age_days })),
  };
}).filter((s) => s.yesterday.length || s.carried_over.length);

const ledger: Ledger = {
  built_at: raw.at(-1)?.when ?? new Date().toISOString().slice(0, 16).replace('T', ' '),
  window_days: 3,
  corpus_size: raw.length,
  unassigned: unassigned.map(toMessage),
  items: items.sort((a, b) => b.age_days - a.age_days),
  decisions,
  drifts,
  standups,
};

writeFileSync(OUT, JSON.stringify(ledger, null, 2));

const count = (s: State) => items.filter((i) => i.state === s).length;
console.log(`✓ data/ledger.json`);
console.log(`  ${items.length} work items — ⛔ ${count('blocked')} · ⏸ ${count('stalled')} · ▶ ${count('moving')} · ✅ ${count('done')}`);
console.log(`  ${decisions.length} decisions (${decisions.filter((d) => d.superseded_by).length} ถูกแทนที่) · ${drifts.length} drifts`);
console.log(`  ${unassigned.length} ข้อความจับคู่ไม่ได้ (${((unassigned.length / raw.length) * 100).toFixed(0)}%)`);
console.log(`\n  ตรวจ unassigned rate ก่อนขึ้นเดโม — ถ้าเกิน 25% ให้ลด SIM_FLOOR ใน scripts/build-ledger.ts`);
