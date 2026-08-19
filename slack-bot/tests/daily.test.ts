/**
 * The daily thread's parser, where the whole feature can quietly fail.
 *
 * Two failures are covered here because both were live at some point while this
 * was written, and neither is visible on screen — the message renders perfectly
 * and simply omits the thing it exists to show:
 *
 *   1. `Pending รอ requirement @jah` read as a *heading* rather than an answer,
 *      because it starts with a heading word. Every blockers section then came out
 *      empty, which reads exactly like a team with no blockers.
 *   2. A pasted-but-unfilled template counted as an answer, so the summary said
 *      somebody reported work when they had reported the template back at us.
 *
 * The streak test is the one that authorises tagging a person in the channel, so
 * it pins the rule precisely: consecutive *dailies*, and a day nobody collected
 * is missing data rather than a cleared blocker.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  DAILY_TEMPLATE, formatDailyDate, headingOf, isPlaceholder, newEscalations, normaliseBlocker,
  parseDailyReply, parseHhMm, pendingStreaks, streakKey, tagOf, zonedNow,
} from '../src/daily.js';
import type { DailyRecord } from '../src/types.js';

const FILLED = [
  'Done Yesterday:',
  'ปิด REVERAPP-247 แล้ว รอ QA',
  '- review PR #128',
  '',
  'Focus Today:',
  'ต่อ REVERAPP-140 หน้า redemption',
  '',
  'Blockers / Pending:',
  'Pending รอ requirement หน้า redemption <@U0APPQKTULX>',
  'Pending รอยืนยัน scope ของ voucher PO',
].join('\n');

test('a heading is a heading; a pending line that starts with "Pending" is not', () => {
  assert.equal(headingOf('Blockers / Pending:'), 'blockers');
  assert.equal(headingOf('*Blockers / Pending*'), 'blockers');
  assert.equal(headingOf('Done Yesterday:'), 'done');
  assert.equal(headingOf('• Focus Today'), 'focus');
  // The regression: an answer, not a section break.
  assert.equal(headingOf('Pending รอ requirement หน้า redemption'), undefined);
  assert.equal(headingOf('Pending: รอ PO ยืนยัน scope'), undefined);
  assert.equal(headingOf('ปิด REVERAPP-247 แล้ว'), undefined);
});

test('a filled template parses into three sections, keeping the lines as typed', () => {
  const parsed = parseDailyReply(FILLED, 'U08H0UD5R36', '1787200000.000100');
  assert.equal(parsed.kind, 'answer');
  if (parsed.kind !== 'answer') return;
  const { answer } = parsed;
  assert.deepEqual(answer.done, ['ปิด REVERAPP-247 แล้ว รอ QA', 'review PR #128']);
  assert.deepEqual(answer.focus, ['ต่อ REVERAPP-140 หน้า redemption']);
  assert.equal(answer.blockers.length, 2);
  assert.equal(answer.blockers[0]!.tag, 'U0APPQKTULX');
  assert.equal(answer.blockers[1]!.tag, 'PO');
  // Verbatim: tomorrow's post quotes these back at the channel.
  assert.match(answer.blockers[0]!.text, /^Pending รอ requirement หน้า redemption/);
  assert.equal(answer.user, 'U08H0UD5R36');
  assert.equal(answer.ts, '1787200000.000100');
});

test('the template pasted unchanged is not an answer, and chatter is not either', () => {
  assert.equal(parseDailyReply(DAILY_TEMPLATE, 'U1', '1').kind, 'unfilled');
  assert.equal(parseDailyReply('เดี๋ยวตอบนะครับ กำลังประชุม', 'U1', '1').kind, 'other');
  assert.equal(parseDailyReply('', 'U1', '1').kind, 'other');
});

test('"nothing to report" is an answer — it is not the same as staying silent', () => {
  const parsed = parseDailyReply('Done Yesterday:\n-\nFocus Today:\nต่อ profile\nBlockers / Pending:\nNone', 'U1', '1');
  assert.equal(parsed.kind, 'answer');
  if (parsed.kind !== 'answer') return;
  assert.deepEqual(parsed.answer.done, []);
  assert.deepEqual(parsed.answer.blockers, []);
  assert.deepEqual(parsed.answer.focus, ['ต่อ profile']);
});

test('a tag is only what somebody wrote: a mention, PO, or nothing', () => {
  assert.equal(tagOf('Pending รอ mockup <@U08FR5WDQMB|mild>'), 'U08FR5WDQMB');
  assert.equal(tagOf('Pending รอ PO ยืนยัน'), 'PO');
  assert.equal(tagOf('Pending รอ requirement'), '');
  // Not a false positive on an ordinary word containing the letters.
  assert.equal(tagOf('Pending รอ POST endpoint จาก BE'), '');
});

test('placeholders are recognised so an unfilled line never counts as work done', () => {
  assert.equal(isPlaceholder('Finished ticket #[ID]'), true);
  assert.equal(isPlaceholder('Fix bug in [Module Name]'), true);
  assert.equal(isPlaceholder('Pending xxxx'), true);
  assert.equal(isPlaceholder('ปิด REVERAPP-247'), false);
});

test('the same obstacle written two ways is one obstacle', () => {
  assert.equal(
    normaliseBlocker('Pending รอ requirement หน้า redemption <@U0APPQKTULX>'),
    normaliseBlocker('รอ requirement หน้า redemption'),
  );
  assert.notEqual(normaliseBlocker('รอ requirement หน้า profile'), normaliseBlocker('รอ requirement หน้า redemption'));
});

/** Three mornings, same person, same pending line — plus one day nobody collected. */
function daily(date: string, blockers: string[], opts: { collected?: boolean } = {}): DailyRecord {
  const collected = opts.collected ?? true;
  return {
    date,
    channel: 'C0DEMOCHAN1',
    ts: `${date}.000100`,
    posted_at: `${date} 09:00`,
    summarised_at: collected ? `${date} 10:45` : undefined,
    answers: collected
      ? [{ user: 'U1', ts: `${date}.000200`, done: [], focus: [], blockers: blockers.map((text) => ({ text, tag: 'PO' })) }]
      : [],
  };
}

test('a streak counts consecutive dailies, and a day nobody collected does not clear it', () => {
  const records = [
    daily('2026-08-17', ['Pending รอ PO ยืนยัน scope']),
    daily('2026-08-18', [], { collected: false }), // laptop asleep: missing data
    daily('2026-08-19', ['Pending รอ PO ยืนยัน scope']),
    daily('2026-08-20', ['รอ PO ยืนยัน scope', 'Pending รอ mockup']),
  ];
  const streaks = pendingStreaks(records);
  const scope = streaks.find((s) => s.text.includes('scope'));
  assert.ok(scope, 'the repeated line must be found');
  assert.equal(scope!.days, 3);
  assert.equal(scope!.since, '2026-08-17');
  // A line seen for the first time today is one day old, not three.
  assert.equal(streaks.find((s) => s.text.includes('mockup'))!.days, 1);
});

test('a blocker somebody stopped reporting drops out of the count', () => {
  const records = [
    daily('2026-08-18', ['Pending รอ PO ยืนยัน scope']),
    daily('2026-08-19', ['Pending อย่างอื่น']),
    daily('2026-08-20', ['Pending รอ PO ยืนยัน scope']),
  ];
  assert.equal(pendingStreaks(records).find((s) => s.text.includes('scope'))!.days, 1);
});

test('the date in the title is the one the team asked for', () => {
  assert.equal(formatDailyDate('2026-08-20'), '20 August 2026');
  assert.equal(formatDailyDate('2026-01-01'), '1 January 2026');
});

test('a bad time falls back instead of scheduling nothing at NaN:NaN', () => {
  assert.deepEqual(parseHhMm('10:45', '09:00'), { hour: 10, minute: 45 });
  assert.deepEqual(parseHhMm('', '10:45'), { hour: 10, minute: 45 });
  assert.deepEqual(parseHhMm('10.45', '10:45'), { hour: 10, minute: 45 });
  assert.deepEqual(parseHhMm('99:99', '09:00'), { hour: 9, minute: 0 });
});

test('the clock reads the scheduling zone, not the host', () => {
  // 2026-08-20T18:30Z is already the 21st in Bangkok (UTC+7).
  const at = new Date('2026-08-20T18:30:00Z');
  assert.deepEqual(zonedNow('Asia/Bangkok', at), { date: '2026-08-21', hour: 1, minute: 30 });
  assert.deepEqual(zonedNow('UTC', at), { date: '2026-08-20', hour: 18, minute: 30 });
});

test('the channel announcement fires once, not every morning after', () => {
  const before = [
    daily('2026-08-18', ['Pending รอ PO ยืนยัน scope']),
    daily('2026-08-19', ['Pending รอ PO ยืนยัน scope']),
    daily('2026-08-20', ['Pending รอ PO ยืนยัน scope']),
  ];
  const first = newEscalations(before, pendingStreaks(before), 3);
  assert.equal(first.length, 1, 'the third morning announces it');

  // The morning it was announced remembers that it was.
  const announced = before.map((d, i) =>
    i === before.length - 1 ? { ...d, announced: first.map(streakKey) } : d,
  );
  const withTomorrow = [...announced, daily('2026-08-21', ['Pending รอ PO ยืนยัน scope'])];
  assert.deepEqual(
    newEscalations(withTomorrow, pendingStreaks(withTomorrow), 3),
    [],
    'a fourth morning must not tag the same person for the same line again',
  );
});

test('a streak that skipped a collected morning still gets announced', () => {
  // Days 1 and 2 collected, day 3 missed entirely, days 4-5 collected: the run is
  // 3 by the equality test's reckoning only by accident, so pin the real behaviour —
  // crossing the threshold at all is enough, exactly once.
  const records = [
    daily('2026-08-17', ['Pending รอ PO']),
    daily('2026-08-18', ['Pending รอ PO']),
    daily('2026-08-19', [], { collected: false }),
    daily('2026-08-20', ['Pending รอ PO']),
  ];
  const streaks = pendingStreaks(records);
  assert.equal(streaks[0]!.days, 3);
  assert.equal(newEscalations(records, streaks, 3).length, 1);
});
