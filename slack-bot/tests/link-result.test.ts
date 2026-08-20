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

import { linkResultBlocks, pastePreviewBlocks, type LinkResult } from '../src/blocks/link.js';
import { pasteComment } from '../src/comment.js';

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

/* ------------------------------------------------------------------ *
 * What the screen says about the write, as opposed to that there was one.
 *
 * The report passed every test above and was still unreadable in the room. It named
 * the ticket, counted the messages, and ended on "comment id `7-729` (เปิด ticket แล้ว
 * หาไอดีนี้เจอ)" — so a presenter could not say which chat had just been attached, of
 * what kind, at what time, or what the bot had written on the ticket in their name,
 * and the one actionable thing on screen was an instruction to go hunting.
 * ------------------------------------------------------------------ */

test('the report names the conversation and its kind, not only the ticket', () => {
  const text = rendered({
    key: 'REVERAPP-251', messages: 3, sourceType: 'slack_paste',
    title: 'การ deploy ขึ้น sit', day: '2026-08-18',
    overridesFile: 'data/link_overrides.json', overridesTotal: 8, itemKey: 'REVERAPP-251', inItem: 3,
  });
  assert.ok(text.includes('การ deploy ขึ้น sit'), 'which chat was attached is the half the reader can verify');
  assert.ok(text.includes('2026-08-18'), 'and from which day — two links to one ticket looked identical without it');
  assert.ok(text.includes('แชทปิด'), 'a pasted private chat is a different claim from an exported message');
});

test('a linked channel message is not labelled as a private chat', () => {
  const text = rendered({ key: 'REVERAPP-140', messages: 1, sourceType: 'slack', overridesFile: 'f', overridesTotal: 1 });
  assert.ok(!text.includes('แชทปิด'), 'this one has a permalink behind it and must not borrow the caveat');
});

test('the comment that was written is shown, and reachable in one press', () => {
  const body = pasteComment({
    key: 'REVERAPP-251', title: 'การ deploy ขึ้น sit', day: '2026-08-18',
    by: 'U0DEMOUSER1', at: '2026-08-20 08:08',
    records: [{ user: 'U0DEMOUSER2', when: '2026-08-18 14:24', text: 'ใช้ voucher code เดิมไปก่อน' }],
  });
  const text = rendered({
    key: 'REVERAPP-251', messages: 1, sourceType: 'slack_paste', at: '2026-08-20 08:08',
    commentId: '7-729', commentBody: body,
    commentUrl: 'https://yt.example.com/issue/REVERAPP-251#focus=Comments-7-729.0-0',
    ticketUrl: 'https://yt.example.com/issue/REVERAPP-251',
  });
  assert.ok(text.includes('ใช้ voucher code เดิมไปก่อน'), 'what the bot wrote under the reader’s name is shown to them');
  assert.ok(text.includes('2026-08-20 08:08'), 'when it was written is part of the record');
  assert.ok(text.includes('focus=Comments-7-729'), 'the button opens the comment, not the top of the ticket');
  assert.ok(!text.includes('เปิด ticket แล้วหาไอดีนี้เจอ'), 'a scavenger hunt is not an affordance');
  assert.ok(!text.includes('###'), 'the ticket’s Markdown is rendered, not pasted through');
});

test('a comment that was refused shows no comment body', () => {
  /** The one way this screen could start lying again: rendering the text we meant to send. */
  const text = rendered({
    key: 'REVERAPP-140', messages: 1, overridesFile: 'f', overridesTotal: 1,
    commentError: 'YOUTRACK_WRITE ยังไม่ได้เปิด',
  });
  assert.ok(text.includes('YOUTRACK_WRITE ยังไม่ได้เปิด'));
  assert.ok(!text.includes('ข้อความที่เขียนลง'), 'nothing was written, so nothing may be shown as written');
});

/* ------------------------------------------------------------------ *
 * A chat kept without a ticket.
 *
 * The paste modal used to refuse a submission with no ticket, which meant a
 * conversation worth keeping had to wait for somebody to decide where it belonged —
 * often for a ticket that did not exist yet. Keeping it unlinked is now allowed, and
 * the danger moves to this screen: every line it has ever rendered assumed a ticket,
 * and the failure mode is a report that reads like a link because it is the only
 * shape the renderer knew.
 * ------------------------------------------------------------------ */

test('a chat kept with no ticket does not report itself as a link', () => {
  const text = rendered({ key: '', messages: 3, sourceType: 'slack_paste', corpusSize: 1495 });
  assert.ok(text.includes('ยังไม่ผูก ticket'), 'the headline states the thing that did not happen');
  assert.ok(!text.includes('🔗'), 'no link was made, so the link icon may not appear');
  assert.ok(text.includes('1495'), 'the corpus is the only place anything was written, so its size is the receipt');
  assert.ok(
    text.includes('ไม่มี link override'),
    'the two absences a person is choosing must be on the screen that made the choice',
  );
});

test('an unlinked paste says how to finish the job, because no button does it', () => {
  /** There is no affordance that attaches a kept chat to a ticket later. Saying so is the affordance. */
  const text = rendered({ key: '', messages: 2, corpusSize: 1494 });
  assert.ok(text.includes('วางแชทเดิมอีกครั้ง'), 'the way out is stated where it is needed');
  assert.ok(text.includes('ไม่เพิ่มซ้ำ'), 'and the reason re-pasting is safe, or nobody will do it');
});

test('the linker’s own grouping is reported as a guess, and the remainder is counted', () => {
  const text = rendered({
    key: '', messages: 5, corpusSize: 1500, placed: [{ key: 'REVERAPP-162', count: 2 }],
  });
  assert.ok(text.includes('REVERAPP-162'), 'where the build put them is worth knowing');
  assert.ok(text.includes('ไม่ใช่คนสั่ง'), 'a guessed tier must not read as the override it loses to');
  assert.ok(text.includes('อีก 3 ข้อความ'), 'the messages it placed nowhere are counted, not omitted');
});

test('an unlinked paste claims no ticket comment and no landing count', () => {
  const text = rendered({ key: '', messages: 1, corpusSize: 9 });
  assert.ok(!text.includes('คอมเมนต์ลง ticket:'), 'nothing was refused, because nothing was attempted');
  assert.ok(!text.includes('ยังไม่เห็นข้อความพวกนี้อยู่ใต้'), 'that line names a ticket, and there is none');
});

test('the preview promises what the button will actually do, with or without a ticket', () => {
  const of = (key: string) =>
    JSON.stringify(
      pastePreviewBlocks({
        title: 'DM พี่ Natta', day: '2026-08-18', key,
        records: [{ user: 'Natta', when: '2026-08-18 14:24', text: 'ใช้ voucher code เดิมไปก่อน' }],
        skipped: [], actionValue: 'p1',
      }),
    );
  assert.ok(of('REVERAPP-251').includes('ผูกกับ REVERAPP-251'), 'a chosen ticket is named on the button');
  const none = of('');
  assert.ok(none.includes('ยังไม่ผูก ticket'), 'and its absence is named just as plainly');
  assert.ok(!none.includes('จะผูกเข้ากับ'), 'the preview must not promise a link it will not make');
});
