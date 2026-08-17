/**
 * Slack's limits are a 400 at post time, not a warning at build time.
 *
 * `npm run preview` already renders every payload and prints what is over the
 * line; this file asserts the same limits so a regression fails a run rather than
 * printing a ✗ nobody reads, and it presses on the shapes the fixture cannot
 * produce: a 60-item board, a 40-entry standup draft, a 6000-character Thai
 * headline. Every limit here was a real 400 the moment a corpus got bigger than
 * the 8-item demo, which is exactly when nobody is watching a terminal.
 *
 * The last test is the product rule rather than a Slack rule: nothing is rendered
 * as a claim unless it can link to the message that proves it.
 */
import { test, before } from 'node:test';
import assert from 'node:assert/strict';

import { hydrate, findItem, findMessage, ledger, sortedItems } from '../src/data.js';
import { digestBlocks } from '../src/blocks/digest.js';
import { boardBlocks, itemCardBlocks } from '../src/blocks/itemCard.js';
import { standupDmBlocks } from '../src/blocks/standupDm.js';
import { clamp } from '../src/blocks/common.js';
import type { StandupDraft, WorkItem } from '../src/types.js';

/** Slack's documented limits, same numbers scripts/preview.ts checks. */
const LIMITS = {
  blocksPerMessage: 50,
  sectionText: 3000,
  headerText: 150,
  contextElements: 10,
  buttonText: 75,
  modalTitle: 24,
};

before(async () => {
  // No TAM_API_URL: the fixture is the declared source and hydrate() reads it
  // from disk, which is the same ledger the offline demo renders.
  delete process.env.TAM_API_URL;
  await hydrate();
});

/** Everything preview.ts checks, as one assertion helper. */
function checkLimits(name: string, blocks: any[], cap = LIMITS.blocksPerMessage): void {
  assert.ok(blocks.length <= cap, `${name}: ${blocks.length} blocks > ${cap}`);
  blocks.forEach((b, i) => {
    const where = `${name} block ${i} (${b.type})`;
    if (b.type === 'section') {
      assert.ok(b.text || b.fields, `${where}: section with neither text nor fields`);
      assert.ok((b.text?.text?.length ?? 0) <= LIMITS.sectionText, `${where}: section text too long`);
    }
    if (b.type === 'header') {
      assert.ok(b.text.text.length <= LIMITS.headerText, `${where}: header ${b.text.text.length} chars`);
    }
    if (b.type === 'context') {
      assert.ok(b.elements.length <= LIMITS.contextElements, `${where}: too many context elements`);
    }
    for (const el of [b.accessory, ...(b.elements ?? []), b.element].filter(Boolean)) {
      if (el.type !== 'button') continue;
      assert.ok(el.url || el.action_id, `${where}: button with neither url nor action_id`);
      if ('url' in el) assert.match(el.url ?? '', /^https?:\/\//, `${where}: button url is not absolute`);
      assert.ok((el.text?.text?.length ?? 0) <= LIMITS.buttonText, `${where}: button label too long`);
    }
  });
}

function manyItems(n: number): WorkItem[] {
  const base = sortedItems()[0];
  assert.ok(base, 'the fixture must contain at least one work item');
  return Array.from({ length: n }, (_, i) => ({ ...base, key: `TAM-${i + 1}` }));
}

function bigDraft(n: number): StandupDraft {
  const items = manyItems(n);
  return {
    slack_user_id: 'U0DEMOUSER1',
    display_name: 'U0DEMOUSER1',
    yesterday: items.map((i) => ({ key: i.key, headline: i.headline, note: i.evidence, evidence_id: i.evidence_id })),
    carried_over: items.map((i, idx) => ({ key: i.key, headline: i.headline, stale_days: idx + 1 })),
  };
}

test('the digest fits in one Slack message', () => {
  checkLimits('digest', digestBlocks());
});

test('a 60-item digest trims to fit and reports what it left out, not the total', () => {
  // digestBlocks() reads the shared cache rather than taking items, so the only
  // way to reach its trim path is to swap the items and put the real ones back —
  // the same seam scripts/preview.ts uses for its worst-case payload.
  const l = ledger();
  const real = l.items;
  try {
    l.items = manyItems(60);
    const blocks = digestBlocks();
    checkLimits('digest (60 items)', blocks);

    const footer = blocks.at(-1) as any;
    const reported = Number(/อีก (\d+) items/.exec(footer.elements[0].text)?.[1]);
    assert.ok(Number.isFinite(reported), 'the trim footer must name a count');

    // Count the items that actually made it onto the screen, independently of the
    // renderedBy bookkeeping: sending the reader to look for 60 when 43 are shown
    // is the bug this footer used to have.
    const shown = new Set(
      [...JSON.stringify(blocks.slice(0, -1)).matchAll(/TAM-(\d+)/g)].map((m) => m[1]),
    );
    assert.ok(shown.size > 0 && shown.size < 60, `${shown.size} of 60 items rendered`);
    assert.equal(reported, 60 - shown.size, 'the footer count does not match what was omitted');
  } finally {
    l.items = real;
  }
});

test('every item card fits, including one with a 6000-character Thai headline', () => {
  for (const item of sortedItems()) checkLimits(`item ${item.key}`, itemCardBlocks(item));

  // Thai has no word spaces, so a truncation that looks for one has nowhere to cut.
  const monster = { ...sortedItems()[0]!, key: 'TAM-LONGKEY-1234567890', headline: 'ก'.repeat(6000) };
  checkLimits('item (long headline)', itemCardBlocks(monster));
});

test('a 60-item board is capped and says how many it dropped', () => {
  const items = manyItems(60);
  const blocks = boardBlocks('บอร์ดรวม', items);
  checkLimits('board (60 items)', blocks);

  const rows = blocks.filter((b: any) => b.type === 'section').length;
  const footer = blocks.at(-1) as any;
  assert.match(footer.elements[0].text, new RegExp(`อีก ${items.length - rows} งานไม่ได้แสดง`),
    'the footer must report what was omitted, not the total');
});

test('an empty board is good news, not an error', () => {
  checkLimits('board (empty)', boardBlocks('งานของ @bob', []));
});

test('a 40-entry standup draft fits, and the overflow is stated on screen', () => {
  const draft = bigDraft(40);
  const blocks = standupDmBlocks(draft);
  checkLimits('standup (40 entries)', blocks);
  assert.ok(
    blocks.some((b: any) => JSON.stringify(b).includes('และอีก')),
    'a draft the reader cannot tell is incomplete is worse than a short one',
  );
});

test('carried_over is ordered stalest-first so the cap drops the freshest', () => {
  const draft = bigDraft(40);
  draft.carried_over[0]!.key = 'TAM-STALEST';
  draft.carried_over[0]!.stale_days = 999;
  const text = JSON.stringify(standupDmBlocks(draft));
  assert.ok(text.includes('TAM-STALEST'), 'the most overdue item was dropped by the cap');
});

test('clamp() never returns more characters than it was given room for', () => {
  for (const max of [1, 2, 3, LIMITS.modalTitle, 80]) {
    for (const s of ['A'.repeat(300), 'ก'.repeat(300), 'word '.repeat(60), 'TAM-LONGKEY-1234567890']) {
      assert.ok(clamp(s, max).length <= max, `clamp(${max}) overflowed on ${s.slice(0, 8)}…`);
    }
  }
  assert.equal(clamp('สั้น', 24), 'สั้น', 'short strings must be left alone');
  assert.equal(clamp('A'.repeat(24), 24), 'A'.repeat(24), 'a string exactly at the limit is not truncated');
});

test('every claim the fixture renders can link to the message that proves it', () => {
  const l = ledger();
  assert.ok(l.items.length, 'nothing to check means nothing is proven');
  for (const item of l.items) {
    assert.ok(findMessage(item.evidence_id), `${item.key}: evidence_id ${item.evidence_id} resolves to nothing`);
    for (const event of item.timeline) {
      if (event.evidence_id) {
        assert.ok(findMessage(event.evidence_id), `${item.key}: timeline evidence ${event.evidence_id} is dangling`);
      }
    }
  }
  for (const d of l.decisions) {
    assert.ok(findMessage(d.evidence_id) || d.evidence_id.startsWith('mtg_'),
      `decision ${d.id}: evidence_id ${d.evidence_id} resolves to nothing`);
  }
  for (const drift of l.drifts) assert.ok(findItem(drift.item_key), `drift for unknown item ${drift.item_key}`);
});
