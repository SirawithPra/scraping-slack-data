/**
 * Where the ledger came from, and what the bot is allowed to serve when it
 * cannot get it.
 *
 * The rule the whole module exists for: asking for the pipeline and silently
 * getting three-day-old fixture data is the worst outcome available — the bot
 * keeps answering, the answers look identical, and nobody learns the pipeline is
 * down until a decision has been made on stale numbers. So with `TAM_API_URL`
 * set, the fixture must be unreachable: before hydrate, mid-flight, and after a
 * failed reload.
 *
 * `drift` is the other design rule: it has no live source, so it stays empty
 * unless `DEMO_FIXTURES=1` asks for it, and then the renderer labels it.
 *
 * The tests run in order on purpose — data.ts holds one module-level cache, which
 * is the thing under test.
 */
import { test, before } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { hydrate, ledger, ledgerOrigin, refreshStatus, reload, demoFixtures } from '../src/data.js';
import { driftNudgeBlocks } from '../src/blocks/drift.js';
import { findItem } from '../src/data.js';

/** The committed fixture's own corpus_size — the tell that we are reading it. */
const FIXTURE_CORPUS = 128;
const API_CORPUS = 18;

const API = {
  built_at: '2026-08-17 23:24',
  window_days: 7,
  corpus_size: API_CORPUS,
  topics: [
    {
      key: 0, label: 'api', state: 'blocked',
      evidence: 'blocked since 2025-08-01 05:13 — cue “ยังไม่มา”',
      evidence_id: 'msg_C0SAMPLE01_1754000010.000100',
      participants: ['U01FE'], sources: { slack: 1 },
      first: '2025-08-01 05:13', last: '2025-08-01 05:13', age_days: 381.8,
    },
  ],
};
const ITEM = {
  timeline: [],
  messages: [{
    id: 'msg_C0SAMPLE01_1754000010.000100', when: '2025-08-01 05:13',
    user: 'U01FE', source: 'slack', text: 'sorting หน้า candidate mock ไว้ก่อน api หลังบ้านยังไม่มา',
  }],
};

let failNext = false;

before(() => {
  // The bot's own stores must not be read from a test run.
  const dir = mkdtempSync(join(tmpdir(), 'meowtam-provenance-'));
  process.env.TAM_DECISIONS_PATH = join(dir, 'decisions.json');
  process.env.TAM_OVERRIDES_PATH = join(dir, 'link_overrides.json');
  process.env.SLACK_WORKSPACE_URL = 'https://example.slack.com';
  delete process.env.DEMO_FIXTURES;

  globalThis.fetch = (async (input: any) => {
    if (failNext) throw new Error('ECONNREFUSED 127.0.0.1:8943');
    const path = new URL(String(input)).pathname;
    const body = path === '/api/digest' ? API : ITEM;
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
  }) as typeof fetch;
});

test('pipeline requested but not hydrated is an error, never a quiet fixture read', () => {
  process.env.TAM_API_URL = 'http://127.0.0.1:8943';
  assert.throws(() => ledger(), /ห้าม fallback ไป fixture/);
  delete process.env.TAM_API_URL;
});

test('with no TAM_API_URL the fixture is the declared source and says so', async () => {
  const { origin } = await hydrate();
  assert.equal(origin, 'fixture');
  assert.equal(ledgerOrigin(), 'fixture');
  assert.equal(ledger().corpus_size, FIXTURE_CORPUS);
  assert.equal(refreshStatus().error, null);
});

test('drift stays empty until DEMO_FIXTURES asks for it', async () => {
  assert.equal(demoFixtures(), false);
  await hydrate();
  assert.deepEqual(ledger().drifts, [], 'drift has no live source, so it cannot be non-empty by default');

  process.env.DEMO_FIXTURES = '1';
  await hydrate();
  const drifts = ledger().drifts;
  assert.ok(drifts.length, 'the fixture drift should be loaded when explicitly asked for');

  // …and it is labelled on screen, not only in the boot log.
  const drift = drifts[0]!;
  const item = findItem(drift.item_key);
  assert.ok(item, 'a drift whose item is not in the ledger must not have survived hydrate');
  const rendered = JSON.stringify(driftNudgeBlocks(drift, item));
  assert.match(rendered, /DEMO_FIXTURES=1/);

  delete process.env.DEMO_FIXTURES;
});

test('a drift whose two halves are not both in the ledger is dropped', async () => {
  process.env.DEMO_FIXTURES = '1';
  await hydrate();
  for (const drift of ledger().drifts) {
    assert.ok(findItem(drift.item_key), `drift item_key ${drift.item_key} is not in the ledger`);
    // A nudge nobody can click through to is the unprovable claim the product forbids.
    const item = findItem(drift.item_key)!;
    assert.ok(item.messages.some((m) => m.id === drift.trigger_id), `trigger ${drift.trigger_id} is dangling`);
  }
  delete process.env.DEMO_FIXTURES;
});

test('hydrating from the pipeline reports pipeline, and keeps the API empty where it is empty', async () => {
  process.env.TAM_API_URL = 'http://127.0.0.1:8943';
  process.env.DEMO_FIXTURES = '1'; // even asked for, drift must not follow pipeline items
  const { origin } = await hydrate();

  assert.equal(origin, 'pipeline');
  assert.equal(ledgerOrigin(), 'pipeline');
  assert.equal(ledger().corpus_size, API_CORPUS);
  assert.deepEqual(ledger().drifts, [], 'fixture drifts next to pipeline items resolve to nothing');
  assert.deepEqual(ledger().unassigned, [], 'the API does not expose unplaced threads yet');
  delete process.env.DEMO_FIXTURES;
});

test('a failed reload keeps the previous pipeline build and records why', async () => {
  failNext = true;
  await assert.rejects(reload(), /ECONNREFUSED/);
  failNext = false;

  assert.equal(ledger().corpus_size, API_CORPUS, 'the fixture was served under a pipeline label');
  assert.equal(ledgerOrigin(), 'pipeline');
  assert.match(refreshStatus().error ?? '', /ECONNREFUSED/, 'the caller has nothing to tell the user with');
});

test('a successful reload clears the recorded failure', async () => {
  await reload();
  assert.equal(refreshStatus().error, null);
  assert.equal(ledger().corpus_size, API_CORPUS);
});
