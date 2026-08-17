/**
 * The seam. Everything the bot renders about a work item is mapped here from a
 * payload the Python half produced, and every bug this file has had was silent:
 * a card that looked right and pointed at the wrong message.
 *
 * `tests/fixtures/pipeline-api.json` is a real recording (see its `_recorded`
 * note), so the wire shape under test is the shape the server actually sends
 * rather than the shape this file wishes for. The variants below are derived from
 * that recording in memory and say so — a `blocked` state, added epochs, a
 * message-less item — because the sample corpus has no blocked topic to record.
 */
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { apiConfig, envNumber, fetchLedger } from '../src/tam-api.js';
import type { Ledger } from '../src/types.js';

const RECORDED = JSON.parse(
  readFileSync(fileURLToPath(new URL('./fixtures/pipeline-api.json', import.meta.url)), 'utf8'),
) as Record<string, any>;

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

const DAY = 86_400_000;
const realFetch = globalThis.fetch;

/** Serve a recorded transcript keyed by path; anything else is a 404, as it would be. */
function serve(transcript: Record<string, unknown>): string[] {
  const seen: string[] = [];
  globalThis.fetch = (async (input: any) => {
    const path = new URL(String(input)).pathname;
    seen.push(path);
    const body = transcript[path];
    if (body === undefined) return new Response('not found', { status: 404 });
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
  }) as typeof fetch;
  return seen;
}

/** 'YYYY-MM-DD HH:mm' in *this* process's zone — the shape the pipeline sends. */
function localStamp(at: number): string {
  const d = new Date(at);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

beforeEach(() => {
  process.env.TAM_API_URL = 'http://127.0.0.1:8943';
  process.env.SLACK_WORKSPACE_URL = 'https://example.slack.com';
  delete process.env.TAM_STALE_DAYS;
  delete process.env.TAM_MIN_COSINE;
  delete process.env.TAM_API_TIMEOUT_MS;
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

function cfg() {
  const c = apiConfig();
  assert.ok(c, 'TAM_API_URL is set, so apiConfig must produce a config');
  return c;
}

async function load(transcript: Record<string, unknown> = RECORDED): Promise<Ledger> {
  serve(transcript);
  return fetchLedger(cfg());
}

// ---------------------------------------------------------------------------
// mapping the recording
// ---------------------------------------------------------------------------

test('every topic is keyed by the pipeline\'s stable id, with its messages attached', async () => {
  const l = await load();
  // Read the expectation off the recording rather than hardcoding the hashes: the
  // point is that the key is whatever the pipeline called the item, never the
  // cluster rank, which names a different item after the next rebuild.
  const ids = RECORDED['/api/digest'].topics.map((t: { item_id: string }) => t.item_id);
  assert.ok(ids.every((id: string) => id && !/^\d+$/.test(id)), `item_id missing or rank-like: ${ids}`);
  assert.deepEqual(l.items.map((i) => i.key), ids);
  assert.deepEqual(l.items.map((i) => i.messages.length), [6, 3]);
  assert.equal(l.corpus_size, 18);
  assert.equal(l.built_at, RECORDED['/api/digest'].built_at);
});

test('the pipeline owns decisions/drifts/standups in name only — the API has none', async () => {
  const l = await load();
  // data.ts fills these three. Backfilling them here would claim the API said
  // something it did not.
  assert.deepEqual([l.decisions, l.drifts, l.standups, l.unassigned], [[], [], [], []]);
});

test('the written headline wins over the TF-IDF label, minus the duplicated state clause', async () => {
  const l = await load();
  // recorded: 'api — ปิดแล้ว' and '(no shared anchor) — กำลังดำเนินอยู่'; Slack
  // renders the state from STATE_LABEL already, so leaving it in double-prints it.
  assert.equal(l.items[0]?.headline, 'api');
  assert.equal(l.items[1]?.headline, '(no shared anchor)');
});

test('permalinks are rebuilt from the id and the configured workspace', async () => {
  const l = await load();
  assert.equal(
    l.items[0]?.messages[0]?.permalink,
    'https://example.slack.com/archives/C0SAMPLE01/p1754000010000100',
  );
  assert.equal(l.items[0]?.messages[0]?.channel, 'C0SAMPLE01');
});

test('a meeting utterance gets no Slack permalink instead of a broken one', async () => {
  const transcript = clone(RECORDED);
  const item = transcript['/api/item/3'];
  item.messages[0].id = 'mtg_20260814-0930-daily-standup_1786699865.000000';
  item.messages[0].source = null; // as the server sends it for a record with no stamp
  const l = await load(transcript);
  const mapped = l.items[1]?.messages[0];
  assert.equal(mapped?.source, 'meeting', 'the id prefix is the fallback when nothing is stamped');
  assert.equal(mapped?.permalink, undefined, 'an mtg_ id has no Slack message to link to');
});

test('the source the pipeline stamped wins over the id prefix', async () => {
  const transcript = clone(RECORDED);
  transcript['/api/item/3'].messages[0].source = 'youtrack';
  const l = await load(transcript);
  assert.equal(l.items[1]?.messages[0]?.source, 'youtrack');
});

test('a resolves relation maps to unblocked, and the collapsed count is kept', async () => {
  const transcript = clone(RECORDED);
  transcript['/api/item/0'].timeline[0].also_answers = 4;
  const l = await load(transcript);
  const event = l.items[0]?.timeline[0];
  assert.equal(event?.kind, 'unblocked');
  assert.equal(event?.evidence_id, 'msg_C0SAMPLE01_1754000500.000500');
  assert.match(event?.text ?? '', /ตอบข้อความก่อนหน้าอีก 4 ข้อความ/);
});

test('a blocked_by relation maps to blocked', async () => {
  const transcript = clone(RECORDED);
  transcript['/api/item/0'].timeline[0].relation = 'blocked_by';
  const l = await load(transcript);
  assert.equal(l.items[0]?.timeline[0]?.kind, 'blocked');
});

// ---------------------------------------------------------------------------
// every rendered claim must point at a message
// ---------------------------------------------------------------------------

test('an item with no evidence is anchored on its newest message, not left blank', async () => {
  const l = await load();
  const active = l.items[1];
  // Recorded: topic 3 has evidence: "" and evidence_id: "". The bot renders the
  // evidence line verbatim, so passing the empty string through prints a blank claim.
  assert.equal(RECORDED['/api/item/3'].topic.evidence, '');
  assert.match(active?.evidence ?? '', /ยังไม่มีสัญญาณเปลี่ยนสถานะ/);
  assert.equal(active?.evidence_id, 'msg_C0SAMPLE01_1754004000.001800');
  assert.ok(active?.messages.some((m) => m.id === active.evidence_id), 'evidence must resolve in messages');
});

test('the newest message is chosen by instant, not by comparing display strings', async () => {
  // Three messages inside one minute render the same 'when'. Comparing the
  // strings with `>` ties them and keeps the *earliest*, which anchored live
  // items on their opening message.
  const transcript = clone(RECORDED);
  const messages = transcript['/api/item/3'].messages;
  messages.forEach((m: any, i: number) => {
    m.when = '2025-08-01 06:00';
    m.ts = 1754002800 + i;
  });
  transcript['/api/digest'].topics[1].evidence = '';
  transcript['/api/digest'].topics[1].evidence_id = '';

  const l = await load(transcript);
  assert.equal(l.items[1]?.evidence_id, messages[2].id, 'kept an older message as "latest activity"');
});

test('an item the API returns no messages for is fatal, not rendered empty', async () => {
  const transcript = clone(RECORDED);
  transcript['/api/item/3'].messages = [];
  const id = RECORDED['/api/digest'].topics[1].item_id;
  await assert.rejects(load(transcript), new RegExp(`ไม่มีข้อความให้ ${id}`));
});

test('a failed detail call fails the whole hydrate rather than emptying one item', async () => {
  const transcript = clone(RECORDED);
  delete transcript['/api/item/3'];
  await assert.rejects(load(transcript), /HTTP 404/);
});

// ---------------------------------------------------------------------------
// timekeeping across the seam
// ---------------------------------------------------------------------------

test('stalled is derived from the epoch when the server sends one', async () => {
  // The pipeline formats `when` with a naive local datetime. A bot container on
  // UTC reading a pipeline on Asia/Bangkok mis-measures the age by the whole
  // offset, which is enough to flip this badge. When an epoch is on the wire it
  // is the only thing that may decide.
  const transcript = clone(RECORDED);
  const topic = transcript['/api/digest'].topics[1];
  topic.state = 'active';
  topic.last_ts = (Date.now() - 2.9 * DAY) / 1000;
  topic.last = localStamp(Date.now() - 3.4 * DAY); // as if rendered in another zone
  process.env.TAM_STALE_DAYS = '3';

  const l = await load(transcript);
  assert.equal(l.items[1]?.state, 'moving', 'the display string decided instead of the epoch');
});

test('with no epoch on the wire the string is used, and the guess is announced once', async () => {
  const transcript = clone(RECORDED);
  const topic = transcript['/api/digest'].topics[1];
  topic.state = 'active';
  topic.last = localStamp(Date.now() - 4 * DAY);
  process.env.TAM_STALE_DAYS = '3';

  const warnings: string[] = [];
  const realWarn = console.warn;
  console.warn = (...args: unknown[]) => void warnings.push(args.join(' '));
  try {
    const l = await load(transcript);
    assert.equal(l.items[1]?.state, 'stalled');
  } finally {
    console.warn = realWarn;
  }
  assert.ok(
    warnings.some((w) => w.includes('epoch')),
    'a timezone assumption was made silently — the recording has no last_ts, so this is the live path',
  );
});

test('blocked and resolved come from the pipeline and are never re-derived', async () => {
  const transcript = clone(RECORDED);
  transcript['/api/digest'].topics[0].state = 'blocked';
  // Fresh activity must not talk a blocked item into 'moving'.
  transcript['/api/digest'].topics[0].last_ts = Date.now() / 1000;
  const l = await load(transcript);
  assert.equal(l.items[0]?.state, 'blocked');

  const resolved = await load();
  assert.equal(resolved.items[0]?.state, 'done', 'recorded state is resolved');
});

// ---------------------------------------------------------------------------
// configuration knobs
// ---------------------------------------------------------------------------

test('envNumber distinguishes unset, zero, and a typo', () => {
  delete process.env.TAM_MIN_COSINE;
  assert.equal(envNumber('TAM_MIN_COSINE', 0.45, { min: 0, max: 1 }), 0.45);

  process.env.TAM_MIN_COSINE = '0';
  assert.equal(envNumber('TAM_MIN_COSINE', 0.45, { min: 0, max: 1 }), 0, 'a deliberate 0 became the default');

  process.env.TAM_MIN_COSINE = '20s';
  assert.throws(() => envNumber('TAM_MIN_COSINE', 0.45, { min: 0, max: 1 }), /ไม่ใช่ตัวเลข/);

  process.env.TAM_MIN_COSINE = '1.5';
  assert.throws(() => envNumber('TAM_MIN_COSINE', 0.45, { min: 0, max: 1 }), /นอกช่วงที่ใช้ได้/);

  delete process.env.TAM_MIN_COSINE;
});

test('apiConfig is null exactly when nobody asked for the pipeline', () => {
  delete process.env.TAM_API_URL;
  assert.equal(apiConfig(), null);
  process.env.TAM_API_URL = '   ';
  assert.equal(apiConfig(), null);
  process.env.TAM_API_URL = 'http://127.0.0.1:8943///';
  assert.equal(apiConfig()?.baseUrl, 'http://127.0.0.1:8943', 'trailing slashes would double up in every path');
});
