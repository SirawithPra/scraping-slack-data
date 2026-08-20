/**
 * The store must not turn a file it cannot read into an empty file it can.
 *
 * A truncated or hand-edited store read as `[]`, and the next `saveOverride` /
 * `saveDecision` wrote that `[]` back — every prior human correction gone, with
 * 'เขียนลง … แล้ว (1 รายการ)' printed as confirmation. Corruption is exactly the
 * state in which the writes must stop, so the assertion that matters here is not
 * "it throws" but "the bytes on disk are untouched afterwards".
 *
 * Both paths are covered because they need different answers: rendering may fall
 * back to empty (loudly), writing may not.
 */
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import {
  CorruptStoreError, linkerKey, readDecisions, readOverrides, saveDecision, saveOverride,
} from '../src/store.js';

let dir: string;
let decisions: string;
let overrides: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'meowtam-store-'));
  decisions = join(dir, 'decisions.json');
  overrides = join(dir, 'link_overrides.json');
  process.env.TAM_DECISIONS_PATH = decisions;
  process.env.TAM_OVERRIDES_PATH = overrides;
});

const A_DECISION = {
  statement: 'export ใช้ UTF-8 with BOM',
  when: '2026-08-14 09:30',
  user: 'Alice',
  source: 'slack' as const,
  evidence_id: 'msg_C0DEMOCHAN1_1786699860.031',
};

/** The half-written file the pipeline's load_overrides used to choke on too. */
const TRUNCATED = '[{"record_id": "msg_C0DEMOCHAN1_1786699860.031", "key": "ticket:MOB-1';

test('an absent store really is empty', () => {
  assert.deepEqual(readDecisions(), []);
  assert.deepEqual(readOverrides(), []);
  assert.deepEqual(readDecisions(true), []);
});

test('a corrupt store reads as empty on the rendering path', () => {
  writeFileSync(decisions, TRUNCATED, 'utf8');
  assert.deepEqual(readDecisions(), []);
});

test('a corrupt store refuses to be read on the write path', () => {
  writeFileSync(decisions, TRUNCATED, 'utf8');
  assert.throws(() => readDecisions(true), CorruptStoreError);
});

test('saving a decision over a corrupt store leaves the bytes alone', () => {
  writeFileSync(decisions, TRUNCATED, 'utf8');
  assert.throws(() => saveDecision(A_DECISION), CorruptStoreError);
  assert.equal(readFileSync(decisions, 'utf8'), TRUNCATED, 'the unreadable file was overwritten');
});

test('saving an override over a corrupt store leaves the bytes alone', () => {
  writeFileSync(overrides, TRUNCATED, 'utf8');
  assert.throws(() => saveOverride('msg_C0DEMOCHAN1_1786699860.031', 'MOB-142', 'Alice', '2026-08-17 10:00'),
    CorruptStoreError);
  assert.equal(readFileSync(overrides, 'utf8'), TRUNCATED);
});

test('a second save keeps the first one, and keeps a .bak of it', () => {
  saveDecision(A_DECISION);
  saveDecision({ ...A_DECISION, evidence_id: 'msg_C0DEMOCHAN1_1786786200.030', statement: 'เปลี่ยนเป็น UTF-8 without BOM' });

  const stored = readDecisions();
  assert.equal(stored.length, 2, 'the second write replaced the log instead of appending');
  assert.ok(existsSync(`${decisions}.bak`), 'the previous contents were not kept');
  assert.deepEqual(JSON.parse(readFileSync(`${decisions}.bak`, 'utf8')).length, 1);
});

test('supersession is stored in both directions so the chain can be walked', () => {
  const may = saveDecision(A_DECISION);
  const august = saveDecision({
    ...A_DECISION,
    evidence_id: 'msg_C0DEMOCHAN1_1786786200.030',
    statement: 'เปลี่ยนเป็น UTF-8 without BOM',
    supersedes: may.id,
  });

  const stored = readDecisions();
  const prior = stored.find((d) => d.id === may.id);
  assert.equal(prior?.superseded_by, august.id);
  assert.equal(stored.find((d) => d.id === august.id)?.superseded_by, undefined);
});

/**
 * The prior a caller found is usually *not* in this file.
 *
 * app.ts picks it out of the merged ledger — the generated decisions plus this
 * file — so on a real workspace the record being superseded lives in the pipeline's
 * output and has nowhere here to keep a `superseded_by`. That is the whole reason
 * `supersedes_record` exists, and it went unused long enough for the confirmation
 * to promise "recall จะเห็นเป็นสาย" over a file holding one unlinked decision.
 */
test('a prior that lives only in the ledger is carried in, so the chain survives the write', () => {
  const ledgerOnly = {
    id: 'dec_from_the_generated_ledger',
    statement: 'export CSV ไม่ต้องมี BOM',
    when: '2026-05-12 09:42',
    user: 'Sam',
    source: 'meeting' as const,
    evidence_id: 'mtg_20260512-0930_1778000000.000',
    related_items: ['WEB-097'],
  };

  const august = saveDecision({ ...A_DECISION, supersedes: ledgerOnly.id, supersedes_record: ledgerOnly });

  const stored = readDecisions();
  assert.equal(stored.length, 2, 'the prior was not copied into the file that stores the link');
  assert.equal(stored.find((d) => d.id === ledgerOnly.id)?.superseded_by, august.id);
});

test('without the record there is nothing to link to, and the caller can tell', () => {
  const august = saveDecision({ ...A_DECISION, supersedes: 'dec_that_is_nowhere' });
  const stored = readDecisions();
  assert.equal(stored.length, 1, 'a phantom prior must not be invented');
  assert.equal(stored[0]?.id, august.id);
  assert.ok(!stored.some((d) => d.superseded_by), 'nothing may claim a chain that was not written');
});

test('the plain {record_id: key} map the linker CLI writes still reads', () => {
  writeFileSync(overrides, JSON.stringify({ 'msg_C0DEMOCHAN1_1786699860.031': 'ticket:MOB-142' }), 'utf8');
  assert.deepEqual(readOverrides(), [
    { record_id: 'msg_C0DEMOCHAN1_1786699860.031', key: 'ticket:MOB-142', by: 'unknown', at: '' },
  ]);
});

test('an override is written in the namespace the linker actually keys on', () => {
  // linker.py stores `ticket:REV-1421` / `cluster:7`; a bare key matched neither,
  // so the correction was saved, honoured, and pointed at nothing.
  assert.equal(linkerKey('MOB-142'), 'ticket:MOB-142');
  assert.equal(linkerKey('mob-142'), 'ticket:MOB-142');
  assert.equal(linkerKey('TAM-3'), 'cluster:3');
  assert.equal(linkerKey('ticket:MOB-142'), 'ticket:MOB-142');
  assert.equal(linkerKey('  '), '', 'the documented "unlink this message"');
  assert.throws(() => linkerKey('not a key'), /linker จะไม่รู้จัก/);

  saveOverride('msg_C0DEMOCHAN1_1786699860.031', 'MOB-142', 'Alice', '2026-08-17 10:00');
  assert.equal(readOverrides()[0]?.key, 'ticket:MOB-142');
});

test('changing your mind replaces the override rather than appending a contradiction', () => {
  saveOverride('msg_C0DEMOCHAN1_1786699860.031', 'MOB-142', 'Alice', '2026-08-17 10:00');
  const count = saveOverride('msg_C0DEMOCHAN1_1786699860.031', 'WEB-097', 'Alice', '2026-08-17 10:05');
  assert.equal(count, 1);
  assert.equal(readOverrides()[0]?.key, 'ticket:WEB-097');
});
