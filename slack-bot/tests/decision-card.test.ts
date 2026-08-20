/**
 * A filed decision has to be findable without knowing it exists.
 *
 * Until this card section, `decisions.json` was written by two affordances and
 * read by exactly one — `/meowtam recall`, matched on the *wording* of the
 * statement. So the only way to read back "what did we decide about WEB-097" was
 * to already remember roughly how it was phrased. The item card knows the item
 * key, and the decision carries `related_items`, so the join was there the whole
 * time and nobody performed it.
 *
 * The assertion that matters is the second one: a superseded decision must not
 * appear on the card. Printing May's answer next to August's is worse than
 * printing neither, because both look equally current.
 */
import { test, before } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { decisionsFor, findItem, hydrate, ledger, refreshDecisions } from '../src/data.js';
import { itemCardBlocks } from '../src/blocks/itemCard.js';

/** Every mrkdwn string in a block payload, flattened — sections and contexts alike. */
function textOf(blocks: any[]): string {
  return blocks
    .map((b) => b.text?.text ?? (b.elements ?? []).map((e: any) => e.text ?? '').join(' '))
    .join('\n');
}

/**
 * Just the decision section: from its heading to the next divider. Scoped on
 * purpose — the card's *ข้อความล่าสุด* block quotes the raw messages, May's
 * wording among them, and asserting over the whole card would only prove that the
 * message it came from is still on screen, which is not the claim being tested.
 */
function decisionSection(blocks: any[]): any[] {
  const start = blocks.findIndex((b) => (b.text?.text ?? '').includes('การตัดสินใจที่บันทึกไว้'));
  if (start < 0) return [];
  const rest = blocks.slice(start + 1);
  const end = rest.findIndex((b) => b.type === 'divider');
  return end < 0 ? rest : rest.slice(0, end);
}

before(async () => {
  delete process.env.TAM_API_URL;
  // The fixture's chain is the subject here, so the operator's own decisions.json
  // must not be merged in on top of it.
  const store = mkdtempSync(join(tmpdir(), 'meowtam-decision-card-'));
  process.env.TAM_DECISIONS_PATH = join(store, 'decisions.json');
  await hydrate();
});

test('the fixture chain is a chain — May superseded by August, both on WEB-097', () => {
  const all = ledger().decisions;
  const may = all.find((d) => d.id === 'dec_may_encoding');
  const aug = all.find((d) => d.id === 'dec_aug_encoding');
  assert.ok(may && aug, 'fixture no longer carries the encoding decisions this file tests');
  assert.equal(may.superseded_by, aug.id);
  assert.deepEqual(may.related_items, ['WEB-097']);
});

test('decisionsFor returns only the live decision, and matches the key case-insensitively', () => {
  const live = decisionsFor('WEB-097');
  assert.deepEqual(live.map((d) => d.id), ['dec_aug_encoding']);
  assert.deepEqual(decisionsFor('web-097').map((d) => d.id), ['dec_aug_encoding']);
  assert.deepEqual(decisionsFor('PAY-088'), [], 'an item with no filed decision must get none');
});

test('the item card prints the current decision and not the one it replaced', () => {
  const item = findItem('WEB-097');
  assert.ok(item);
  const blocks = itemCardBlocks(item) as any[];
  const section = decisionSection(blocks);

  assert.ok(section.length, 'no decision section on an item that has a filed decision');
  assert.match(textOf(section), /UTF-8 with BOM/, 'August is the answer and must be on the card');
  assert.ok(
    !textOf(section).includes('ไม่ต้องมี BOM'),
    'May was superseded — showing it beside August is the confusion this section exists to remove',
  );
  // The reader is told history exists without being shown it here.
  assert.match(textOf(section), /เปลี่ยนมาแล้ว 2 ครั้ง/);
});

test('an item with no filed decision grows no empty section', () => {
  const item = findItem('PAY-088');
  assert.ok(item);
  assert.ok(!textOf(itemCardBlocks(item) as any[]).includes('การตัดสินใจที่บันทึกไว้'));
});

/**
 * Filing one decision used to delete every other one from memory.
 *
 * The write path did `ledger().decisions = readDecisions()` — the bot's own file,
 * not the merge — so the pipeline's decisions dropped out of recall and off every
 * card the moment somebody filed their first one, and only came back on reload.
 */
test('refreshing after a write keeps the decisions the ledger itself carried', () => {
  const before = ledger().decisions.map((d) => d.id).sort();
  refreshDecisions();
  assert.deepEqual(ledger().decisions.map((d) => d.id).sort(), before);
  assert.deepEqual(decisionsFor('WEB-097').map((d) => d.id), ['dec_aug_encoding']);
});
