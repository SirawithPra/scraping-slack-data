/**
 * What pressing ส่ง on the 08:45 DM records.
 *
 * The failure this guards against is the one it was built to fix, and it is silent
 * in both directions. Before, a DM answer went nowhere: the person had corrected the
 * bot in private and was still listed at 10:45 as not having answered, so they typed
 * the same three sections into the thread — the duplicate work the DM exists to
 * remove. The opposite failure is just as quiet: an answer stored for somebody who
 * filled in nothing, which reads in the summary as a person who reported silence.
 *
 * The format assertions are not cosmetic. The DM and a thread reply have to produce
 * the same shape or the two routes drift, and the tag on a blocker line is what
 * decides who gets mentioned in the channel — so it must come from the same `tagOf`
 * the thread uses, not a second rule that can disagree with it.
 */
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { standupAnswer, standupDone } from '../src/standups.js';
import { DAILY_TEMPLATE, composeDailyReply, headingOf, mergeThreadAnswers, parseDailyReply } from '../src/daily.js';
import { dailyTemplateBlocks } from '../src/blocks/daily.js';
import { standupDmBlocks } from '../src/blocks/standupDm.js';
import { dailyFor, saveDaily, saveDailyAnswer } from '../src/store.js';
import type { DailyAnswer, StandupDraft } from '../src/types.js';

const draft = (over: Partial<StandupDraft> = {}): StandupDraft => ({
  slack_user_id: 'U0AAAAAA',
  display_name: 'U0AAAAAA',
  yesterday: [],
  carried_over: [],
  ...over,
});

beforeEach(() => {
  process.env.TAM_DAILIES_PATH = join(mkdtempSync(join(tmpdir(), 'meowtam-dm-')), 'dailies.json');
});

test('the three boxes become one answer in the daily\'s own format', () => {
  // The boxes, not the draft: `standupDone` decides what the done box *starts* as and
  // the test below pins that, but what is recorded is whatever the person left in it.
  const answer = standupAnswer({
    user: 'U0AAAAAA',
    done: 'REVERAPP-140 — redemption page\nvoucher cleanup',
    today: 'ต่อ REVERAPP-140 หน้า redemption\nเริ่ม flow redeem',
    blocker: 'Pending รอ requirement จาก PO',
  });

  assert.ok(answer);
  assert.deepEqual(answer.done, ['REVERAPP-140 — redemption page', 'voucher cleanup']);
  // A multi-line box is several lines, not one long one.
  assert.deepEqual(answer.focus, ['ต่อ REVERAPP-140 หน้า redemption', 'เริ่ม flow redeem']);
  assert.deepEqual(answer.blockers, [{ text: 'Pending รอ requirement จาก PO', tag: 'PO' }]);
  assert.equal(answer.via, 'dm');
  // No Slack message stands behind it, so no ts is invented for one.
  assert.equal(answer.ts, '');
});

test('a mention in the blocker box is read by the same rule the thread uses', () => {
  const answer = standupAnswer({
    user: 'U0AAAAAA',
    done: '',
    today: '',
    blocker: 'Pending รอ merge PR #128 <@U0BBBBBB>',
  });
  assert.equal(answer?.blockers[0]?.tag, 'U0BBBBBB');

  // The same sentence typed into the thread parses identically — the point of
  // composing through the template rather than building an answer by hand.
  const typed = parseDailyReply(
    ['Blockers / Pending:', 'Pending รอ merge PR #128 <@U0BBBBBB>'].join('\n'),
    'U0AAAAAA',
    '1786699860.031',
  );
  assert.equal(typed.kind, 'answer');
  assert.deepEqual(typed.kind === 'answer' ? typed.answer.blockers : [], answer?.blockers);
});

test('nothing filled in is not an answer of nothing', () => {
  // Someone with no recent activity who presses ส่ง without typing has not reported
  // silence; they have not reported. The summary must be able to tell those apart.
  assert.equal(standupAnswer({ user: 'U0AAAAAA', done: '', today: '', blocker: '' }), undefined);
  assert.equal(standupAnswer({ user: 'U0AAAAAA', done: ' ', today: '   ', blocker: '\n\n' }), undefined);
});

test('one box filled is an answer — the other two are honestly empty', () => {
  const answer = standupAnswer({ user: 'U0AAAAAA', done: '', today: 'ต่อ REVERAPP-152', blocker: '' });
  assert.deepEqual(answer?.done, []);
  assert.deepEqual(answer?.focus, ['ต่อ REVERAPP-152']);
  assert.deepEqual(answer?.blockers, []);
});

test('what the done box starts as is what the card showed, and is editable from there', () => {
  // The prefill is the bot's reading of Slack. It arrives in a box precisely so that
  // it is a suggestion the person can overwrite, rather than a claim filed under
  // their name that they never had a chance to see.
  const lines = standupDone(draft({
    yesterday: [
      { key: 'REVERAPP-140', headline: 'redemption page', note: 'moved' },
      { key: 'c5a3d6b', headline: 'voucher cleanup', note: 'moved' },
    ],
  }));
  // A ticket keeps its key; a cluster is named by its words. Both are lookup-able,
  // which is the whole difference between a line somebody can act on and noise.
  assert.deepEqual(lines, ['REVERAPP-140 — redemption page', 'voucher cleanup']);
  const answer = standupAnswer({ user: 'U0AAAAAA', done: lines.join('\n'), today: '', blocker: '' });
  assert.deepEqual(answer?.done, lines);
});

test('only what the card showed is recorded as done', () => {
  // The DM renders five items and then says "…และอีก N งาน". Recording the ones
  // behind that line would report work this person never saw the bot claim.
  const many = Array.from({ length: 8 }, (_, i) => ({
    key: `REVERAPP-${100 + i}`,
    headline: `งาน ${i}`,
    note: 'moved',
  }));
  assert.equal(standupDone(draft({ yesterday: many })).length, 5);
});

test('a cluster with no name is left out rather than offered as a line', () => {
  assert.deepEqual(
    standupDone(draft({ yesterday: [{ key: 'c23053d', headline: '(ยังไม่มีคำร่วมที่ชัดพอจะตั้งชื่อ)', note: '' }] })),
    [],
  );
});

test('composeDailyReply writes every heading, so an empty section stays a fact', () => {
  const text = composeDailyReply({ focus: ['ต่อ REVERAPP-140'] });
  assert.match(text, /^Done Yesterday:/);
  assert.ok(text.includes('Blockers / Pending:'));
  const parsed = parseDailyReply(text, 'U0AAAAAA', '');
  assert.equal(parsed.kind, 'answer');
});

/* ------------------------------------------------------------------ *
 * where it lands
 * ------------------------------------------------------------------ */

const answerOf = (user: string, focus: string): DailyAnswer =>
  ({ user, ts: '', done: [], focus: [focus], blockers: [], via: 'dm' });

test('answering at 08:45 creates the day even though the post is at 09:00', () => {
  const record = saveDailyAnswer({
    date: '2026-08-20',
    channel: 'C0DEMOCHAN1',
    answer: answerOf('U0AAAAAA', 'ต่อ REVERAPP-140'),
  });
  // No thread yet, and none claimed: an invented ts here would send the 10:45 pass
  // at a message that does not exist.
  assert.equal(record.ts, '');
  assert.equal(record.posted_at, '');
  assert.equal(dailyFor('2026-08-20')?.answers.length, 1);
});

test('a second press replaces that person and leaves everyone else alone', () => {
  saveDailyAnswer({ date: '2026-08-20', channel: 'C0DEMOCHAN1', answer: answerOf('U0AAAAAA', 'ของเก่า') });
  saveDailyAnswer({ date: '2026-08-20', channel: 'C0DEMOCHAN1', answer: answerOf('U0BBBBBB', 'ของคนอื่น') });
  const record = saveDailyAnswer({
    date: '2026-08-20',
    channel: 'C0DEMOCHAN1',
    answer: answerOf('U0AAAAAA', 'แก้แล้ว'),
  });
  assert.equal(record.answers.length, 2);
  assert.deepEqual(record.answers.find((a) => a.user === 'U0AAAAAA')?.focus, ['แก้แล้ว']);
  assert.deepEqual(record.answers.find((a) => a.user === 'U0BBBBBB')?.focus, ['ของคนอื่น']);
});

test('answering after the post joins the thread\'s day instead of starting a new one', () => {
  saveDaily({
    date: '2026-08-20',
    channel: 'C0DEMOCHAN1',
    ts: '1786699860.031',
    posted_at: '2026-08-20 09:00',
    answers: [],
  });
  const record = saveDailyAnswer({
    date: '2026-08-20',
    channel: 'C0DEMOCHAN1',
    answer: answerOf('U0AAAAAA', 'ต่อ REVERAPP-140'),
  });
  // The thread it would be summarised into is not lost by the merge.
  assert.equal(record.ts, '1786699860.031');
  assert.equal(record.answers.length, 1);
});

/* ------------------------------------------------------------------ *
 * what the 10:45 pass keeps
 * ------------------------------------------------------------------ */

test('a thread reply wins; a DM answer nobody replaced survives', () => {
  const dm = answerOf('U0AAAAAA', 'จาก DM');
  const other = answerOf('U0BBBBBB', 'ก็จาก DM');
  const typed: DailyAnswer = {
    user: 'U0AAAAAA', ts: '1786699860.031', done: [], focus: ['พิมพ์เองในเธรด'], blockers: [],
  };

  const { merged, kept } = mergeThreadAnswers([dm, other], [typed]);
  assert.equal(merged.length, 2);
  // The person who typed is represented by what they typed, once.
  assert.deepEqual(merged.filter((a) => a.user === 'U0AAAAAA').map((a) => a.focus), [['พิมพ์เองในเธรด']]);
  // The person who only answered the DM is still counted as having answered.
  assert.deepEqual(kept.map((a) => a.user), ['U0BBBBBB']);
});

test('re-running the summary does not delete the answers it cannot re-read', () => {
  // The regression this exists for: every 10:45 pass reads the thread again, and a
  // rule keyed on "was it in the thread" quietly drops DM and seeded answers the
  // second time — the day looks emptier the more often it is summarised.
  const seeded: DailyAnswer = {
    user: 'U0CCCCCC', ts: 'sim.2026-08-20.0', done: [], focus: ['จำลอง'], blockers: [], simulated: true,
  };
  const first = mergeThreadAnswers([answerOf('U0AAAAAA', 'จาก DM'), seeded], []);
  const second = mergeThreadAnswers(first.merged, []);
  assert.deepEqual(second.merged.map((a) => a.user), ['U0AAAAAA', 'U0CCCCCC']);
});

/* ------------------------------------------------------------------ *
 * one form, offered two ways
 * ------------------------------------------------------------------ */

const withWork = draft({
  yesterday: [{ key: 'REVERAPP-140', headline: 'redemption page', note: 'moved' }],
  carried_over: [{ key: 'REVERAPP-152', headline: 'voucher list', stale_days: 4 }],
});

test('the DM has one box per daily section, in the template\'s order and named after it', () => {
  // "ตอบ DM แทนได้" only holds if the two look like the same form. Three boxes whose
  // ids are the parser's three sections, labelled with the headings a person will
  // also see in `/meowtam daily`, is what makes that recognisable rather than a claim
  // in the docs.
  const inputs = standupDmBlocks(withWork).filter((b: any) => b.type === 'input');
  assert.deepEqual(inputs.map((b: any) => b.block_id), ['done', 'today', 'blocker']);
  const labels = inputs.map((b: any) => b.label.text);
  assert.ok(labels[0]?.includes('Done Yesterday'), labels[0]);
  assert.ok(labels[1]?.includes('Focus Today'), labels[1]);
  assert.ok(labels[2]?.includes('Blockers / Pending'), labels[2]);
  // Every heading the parser knows has a box. A section with no box would be a
  // section only one of the two routes can answer.
  for (const heading of ['Done Yesterday', 'Focus Today', 'Blockers / Pending']) {
    assert.ok(DAILY_TEMPLATE.includes(heading), heading);
  }
});

test('both routes hand over the same prefilled lines', () => {
  // The regression this prevents: somebody edits one surface's prefill — a cap, a
  // sort, a `ต่อ` prefix — and the DM and the pasted form quietly start proposing
  // different work to the same person on the same morning.
  const boxes = standupDmBlocks(withWork)
    .filter((b: any) => b.type === 'input')
    .map((b: any) => b.element.initial_value ?? '');
  // The form carries three code blocks — the bracketed template, a filled example,
  // and this person's prefill. Only the last is about them, so it is found by the
  // heading that introduces it rather than by guessing at contents.
  const texts = dailyTemplateBlocks(withWork).map((b: any) => b.text?.text ?? '');
  const intro = texts.findIndex((t: string) => t.includes('หรือเริ่มจากที่ผมเห็นในข้อมูลของคุณ'));
  assert.notEqual(intro, -1, 'the daily form no longer offers a prefilled block');
  const pasted = texts[intro + 1];

  for (const line of boxes.join('\n').split('\n').filter(Boolean)) {
    assert.ok(pasted.includes(line), `หายไปจากฟอร์ม daily: ${line}`);
  }
  // And the reverse: the pasted block invents nothing the boxes do not hold.
  const invented = pasted
    .replace(/```/g, '')
    .split('\n')
    .map((l: string) => l.trim())
    .filter((l: string) => l && l !== '-' && !headingOf(l));
  for (const line of invented) {
    assert.ok(boxes.join('\n').includes(line), `ฟอร์ม daily มีบรรทัดที่ DM ไม่มี: ${line}`);
  }
});
