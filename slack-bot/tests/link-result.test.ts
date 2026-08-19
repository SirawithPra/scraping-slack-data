/**
 * What the link screen is allowed to claim.
 *
 * A link has three outcomes and they need three different sentences. The messages
 * landed in the work item; the link is live but the clustering left the messages in
 * another item; or the pipeline cannot see the messages at all and nothing was applied.
 *
 * Measured on the live corrections file, the third case was 2 of 8 links — a
 * `log.warning` on the pipeline side and '🔗 ผูกแล้ว' on this one. These tests pin
 * that the screen distinguishes the three, because a person who cannot tell them
 * apart has no way to know whether to act.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { linkResultBlocks, type LinkResult } from '../src/blocks/link.js';

const rendered = (result: LinkResult) => JSON.stringify(linkResultBlocks(result));

test('a link that landed says how many of them landed', () => {
  const text = rendered({
    key: 'REV-250', messages: 3, overridesFile: 'data/link_overrides.json',
    overridesTotal: 8, itemKey: 'REV-250', inItem: 3,
  });
  assert.ok(text.includes('3 จาก 3'), 'the count that landed is the claim being made');
  assert.ok(!text.includes('✕'), 'nothing failed, so nothing may be marked failed');
});

test('a link the pipeline cannot apply says so, with the reason it gave', () => {
  const text = rendered({
    key: 'REV-140', messages: 1, overridesFile: 'data/link_overrides.json', overridesTotal: 8,
    inItem: 0,
    unresolved: [{ record_id: 'msg_C0PLAY0001_2.0', why: 'ข้อความอยู่ใน channel C0PLAY0001 ซึ่งถูกตั้งค่าให้ข้าม' }],
  });
  assert.ok(text.includes('msg_C0PLAY0001_2.0'), 'the reader has to know which message');
  assert.ok(text.includes('C0PLAY0001 ซึ่งถูกตั้งค่าให้ข้าม'), 'the cause is the actionable half');
  assert.ok(text.includes('✕'), 'this one did not work and must not read as a success line');
});

test('an unapplied link still reports that the correction was stored', () => {
  /** It is not lost. It applies itself once the record exists, and saying so stops a retry loop. */
  const text = rendered({
    key: 'REV-140', messages: 1, overridesFile: 'data/link_overrides.json', overridesTotal: 8,
    unresolved: [{ record_id: 'msg_C0GONE0001_9.0', why: 'ยังไม่มีข้อความนี้ใน corpus' }],
  });
  assert.ok(text.includes('เขียนลง'), 'the local write happened and is still true');
  assert.ok(text.includes('จะมีผลเองเมื่อข้อความนี้เข้า corpus'));
});

test('landing short of the count is not reported as an unapplied link', () => {
  /** Two different failures. This one is the clustering, and there is nothing to fix. */
  const text = rendered({
    key: 'REV-250', messages: 3, overridesFile: 'data/link_overrides.json',
    overridesTotal: 8, itemKey: 'REV-250', inItem: 2, unresolved: [],
  });
  assert.ok(text.includes('2 จาก 3'));
  assert.ok(!text.includes('✕'), 'a partial landing is not a failed write');
});
