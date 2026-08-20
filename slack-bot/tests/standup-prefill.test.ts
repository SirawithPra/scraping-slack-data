/**
 * The 08:45 DM says "correct me, don't retype" — these check that the boxes it
 * hands over are actually filled in, and that the blocker box is filled only from
 * sentences the recipient wrote.
 *
 * The second half is the one that matters. Whatever is in the blocker box goes to
 * the channel under this person's name when they press ส่ง, so a blocker inferred
 * from a colleague's message must never arrive prefilled.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { standupDmBlocks } from '../src/blocks/standupDm.js';
import { buildStandups, standupPrefill } from '../src/standups.js';
import type { StandupDraft, WorkItem } from '../src/types.js';

const draft = (over: Partial<StandupDraft> = {}): StandupDraft => ({
  slack_user_id: 'U0AAAAAA',
  display_name: 'U0AAAAAA',
  yesterday: [],
  carried_over: [],
  ...over,
});

const item = (over: Partial<WorkItem> = {}): WorkItem =>
  ({
    key: 'REV-1',
    headline: 'redemption page',
    state: 'blocked',
    evidence: 'ยังรอ …',
    evidence_id: 'm1',
    age_days: 9,
    participants: ['U0AAAAAA'],
    sources: {},
    first: '2026-08-01 09:00',
    last: '2026-08-11 09:00',
    messages: [{ id: 'm1', source: 'slack', user: 'U0AAAAAA', when: '2026-08-11 09:00', text: 'ยังรอ requirement จาก PO อยู่' }],
    ...over,
  }) as WorkItem;

test('today is prefilled from what is still open, stalest first', () => {
  const { today } = standupPrefill(
    draft({
      carried_over: [
        { key: 'REV-2', headline: 'voucher list', stale_days: 4 },
        { key: 'REV-9', headline: 'redemption page', stale_days: 31 },
      ],
    }),
  );
  assert.equal(today, 'ต่อ REV-9 — redemption page\nต่อ REV-2 — voucher list');
});

test('with nothing carried over it offers yesterday instead', () => {
  const { today } = standupPrefill(
    draft({ yesterday: [{ key: 'REV-3', headline: 'export encoding', note: 'ขยับเมื่อวาน' }] }),
  );
  assert.equal(today, 'ต่อ REV-3 — export encoding');
});

test('an empty draft prefills nothing, so the placeholder still asks the question', () => {
  const { today, blocker } = standupPrefill(draft());
  assert.equal(today, undefined);
  assert.equal(blocker, undefined);
});

test('the blocker box carries the sentence the person typed, in the daily format', () => {
  const [built] = buildStandups([item()], { recentDays: 1.5, staleDays: 3 });
  assert.ok(built);
  const { blocker } = standupPrefill(built);
  assert.equal(blocker, 'Pending ยังรอ requirement จาก PO อยู่ (REV-1)');
});

test('a blocker somebody else evidenced is never prefilled', () => {
  const theirs = item({
    messages: [{ id: 'm1', source: 'slack', user: 'U0BBBBBB', when: '2026-08-11 09:00', text: 'อันนี้ติดอยู่นะ' }],
  } as Partial<WorkItem>);
  const [built] = buildStandups([theirs], { recentDays: 1.5, staleDays: 3 });
  assert.ok(built);
  assert.deepEqual(built.blocked, []);
  assert.equal(standupPrefill(built).blocker, undefined);
});

test('the rendered DM puts the prefill in the input, not only in the text above it', () => {
  const blocks = standupDmBlocks(
    draft({ carried_over: [{ key: 'REV-9', headline: 'redemption page', stale_days: 31 }] }),
  ) as any[];
  const today = blocks.find((b) => b.block_id === 'today');
  assert.equal(today.element.initial_value, 'ต่อ REV-9 — redemption page');
  // No self-stated blocker here, so that box stays empty rather than inventing one.
  const blocker = blocks.find((b) => b.block_id === 'blocker');
  assert.equal(blocker.element.initial_value, undefined);
});
