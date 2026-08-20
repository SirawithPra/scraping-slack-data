/**
 * Render every Block Kit payload offline and sanity-check it against Slack's
 * documented limits. No token, no network.
 *
 *   npm run preview            → validate everything, print a summary
 *   npm run preview -- digest  → also dump that payload as JSON
 *
 * Paste any dumped payload into https://app.slack.com/block-kit-builder to see
 * it rendered. Useful at 3am when you don't want to reinstall the app to check
 * one layout change.
 *
 * Two things this checks that are easy to get wrong:
 *
 *   1. It validates the *hydrated* ledger, the same one the running bot renders
 *      from. Validating the raw fixture instead let a regression through: the
 *      fixture carries hand-written decisions and standups the bot never shows,
 *      so the payloads checked here were payloads the bot cannot produce.
 *   2. It validates worst-case payloads, not just the 8-item fixture. Every limit
 *      in this file is a runtime 400 the moment a real corpus is bigger than the
 *      sample, which is exactly when nobody is watching a terminal.
 */
import { digestBlocks } from '../src/blocks/digest.js';
import { standupDmBlocks } from '../src/blocks/standupDm.js';
import { itemCardBlocks, boardBlocks } from '../src/blocks/itemCard.js';
import { driftNudgeBlocks, driftModal } from '../src/blocks/drift.js';
import { recallBlocks } from '../src/blocks/recall.js';
import { dailyPostBlocks, dailySummaryBlocks, dailyTemplateBlocks, dmAnswerBlocks, pendingEscalationBlocks } from '../src/blocks/daily.js';
import { linkResultBlocks, pastePreviewBlocks, ticketOption } from '../src/blocks/link.js';
import { staleBlocks } from '../src/blocks/stale.js';
import { pasteComment } from '../src/comment.js';
import { staleItems } from '../src/stale.js';
import { todaysSimulatedAnswers } from '../src/demo.js';
import type { PendingStreak } from '../src/daily.js';
import { clamp } from '../src/blocks/common.js';
import { hydrate, ledger, findItem, sortedItems } from '../src/data.js';
import type { StandupDraft, WorkItem } from '../src/types.js';

// Drift has no live source, so `hydrate()` leaves it empty unless asked. Ask
// here: the drift payloads still have to be validated, and the renderer labels
// them as fixture content on screen, which this run also exercises.
process.env.DEMO_FIXTURES ??= '1';

// The bot awaits this before accepting traffic; validating anything else means
// validating a payload the bot never sends.
await hydrate();

const l = ledger();
const drift = l.drifts[0];
const driftItem = drift ? findItem(drift.item_key) : undefined;
const open = sortedItems().filter((i) => i.state !== 'done');

/**
 * Worst-case shapes. The fixture is 8 items; a real channel is not, and the
 * per-item block cost of a board or a standup draft is what crosses Slack's
 * limit. Clone real items so the payloads stay realistic in every field but
 * count.
 */
function manyItems(n: number): WorkItem[] {
  const base = open[0] ?? sortedItems()[0];
  if (!base) return [];
  return Array.from({ length: n }, (_, i) => ({ ...base, key: `TAM-${i + 1}` }));
}

function bigDraft(n: number): StandupDraft {
  const items = manyItems(n);
  return {
    slack_user_id: 'U0DEMOUSER1',
    display_name: 'U0DEMOUSER1',
    yesterday: items.map((i) => ({
      key: i.key,
      headline: i.headline,
      note: i.evidence,
      evidence_id: i.evidence_id,
    })),
    carried_over: items.map((i, idx) => ({ key: i.key, headline: i.headline, stale_days: idx + 1 })),
  };
}

const longHeadline = { ...(open[0] ?? sortedItems()[0]), headline: 'ก'.repeat(6000), key: 'TAM-LONGKEY-1234567890' };

/**
 * A pending streak, without a store behind it.
 *
 * The daily payloads render from records the bot writes at runtime, and a preview run
 * has none — so they are built here from the same shapes `pendingStreaks` returns. The
 * point of checking them is the same as everywhere else in this file: the busiest
 * morning is the one whose post is a 400.
 */
function streak(n: number, over: Partial<PendingStreak> = {}): PendingStreak {
  return {
    user: 'U0DEMOUSER1',
    text: 'Pending รอ requirement หน้า redemption จาก PO',
    tag: 'PO',
    days: n,
    since: '2026-08-14',
    ...over,
  };
}

const manyStreaks = Array.from({ length: 30 }, (_, i) =>
  streak(3 + (i % 5), { text: `Pending รอเรื่องที่ ${i + 1} ${'ก'.repeat(400)}` }),
);

const dailyRecord = {
  date: '2026-08-20',
  channel: 'C0DEMOCHAN1',
  ts: '1787163026.001199',
  posted_at: '2026-08-20 09:00',
  answers: todaysSimulatedAnswers(['U0DEMOUSER1', 'U0DEMOUSER2', 'U0DEMOUSER3'], '2026-08-20'),
  simulated: true,
};

/** Every silence count at once: whatever the fixture really has, plus a forced worst case. */
const quiet = staleItems(open, { workdays: 0 });
const quietWorst = staleItems(manyItems(40), { workdays: 0 });

const pastedChat = [
  { user: 'U0DEMOUSER1', when: '2026-08-19 14:21', text: 'พี่ครับ หน้า redemption สรุปว่าใช้ voucher code เดิมใช่ไหมครับ' },
  { user: 'U0DEMOUSER2', when: '2026-08-19 14:24', text: 'ใช้เดิมไปก่อนนะ\nPO ยังไม่ยืนยัน format ใหม่' },
  { user: 'U0DEMOUSER1', when: '2026-08-19 14:26', text: 'โอเคครับ งั้นผมทำ REVERAPP-140 ต่อด้วยของเดิม' },
];

/** The full-success shape, comment body and all — this is the screen after `/mt paste`. */
const linked = {
  key: 'REVERAPP-140',
  messages: 3,
  sourceType: 'slack_paste' as const,
  title: 'DM พี่ Natta เรื่อง redemption',
  day: '2026-08-19',
  at: '2026-08-20 08:08',
  overridesFile: 'link_overrides.json',
  overridesTotal: 12,
  commentId: '4-1234',
  commentUrl: 'https://example.youtrack.cloud/issue/REVERAPP-140#focus=Comments-4-1234.0-0',
  commentBody: pasteComment({
    key: 'REVERAPP-140',
    title: 'DM พี่ Natta เรื่อง redemption',
    day: '2026-08-19',
    by: 'U0DEMOUSER1',
    at: '2026-08-20 08:08',
    records: pastedChat,
    itemKey: 'REVERAPP-140',
    inItem: 3,
    boardUrl: 'https://example.invalid/item/REVERAPP-140',
  }),
  ticketUrl: 'https://example.youtrack.cloud/issue/REVERAPP-140',
  itemKey: 'REVERAPP-140',
  inItem: 3,
  boardUrl: 'https://example.invalid/item/REVERAPP-140',
};

const payloads: Record<string, any[]> = {
  digest: digestBlocks(),
  standup: l.standups[0] ? standupDmBlocks(l.standups[0]) : [],
  item: findItem('MOB-142') ? itemCardBlocks(findItem('MOB-142')!) : itemCardBlocks(sortedItems()[0]!),
  board: boardBlocks('งานของ @bob', open),
  drift: drift && driftItem ? driftNudgeBlocks(drift, driftItem) : [],
  driftModal: drift && driftItem ? (driftModal(drift, driftItem).blocks as any[]) : [],
  recall: await recallBlocks('ตอนนั้นเราสรุปเรื่อง export encoding ว่ายังไงนะ'),
  recallEmpty: await recallBlocks('qqqzzzxxx wvwvwv jjjkkk'),
  // ── worst case ───────────────────────────────────────────────────────────
  dailyPost: dailyPostBlocks({ date: '2026-08-20', carried: [streak(3)], summaryAt: '10:45' }),
  dailyForm: dailyTemplateBlocks(l.standups[0]),
  dailySummary: dailySummaryBlocks({
    record: dailyRecord as any,
    expected: ['U0DEMOUSER1', 'U0DEMOUSER2'],
    unfilled: ['U0DEMOUSER3'],
    streaks: [streak(3)],
    pendingDays: 3,
  }),
  dmAnswer: dmAnswerBlocks([(dailyRecord as any).answers[0]]),
  pendingEscalation: pendingEscalationBlocks([streak(3)], 3),
  stale: staleBlocks(quiet, 5),
  staleEmpty: staleBlocks([], 5),
  linkResult: linkResultBlocks(linked),
  // The half-failed shape, which is what actually renders on a laptop with no write
  // token — and the one a "success" template would render as a lie.
  linkPartial: linkResultBlocks({
    key: 'REVERAPP-140',
    messages: 1,
    sourceType: 'slack',
    at: '2026-08-20 08:08',
    overridesFile: 'link_overrides.json',
    overridesTotal: 12,
    commentError: 'YOUTRACK_WRITE ยังไม่ได้เปิด — บอทจะไม่เขียนคอมเมนต์ลง ticket จริง',
    rebuildError: 'ยังไม่ได้ตั้ง TAM_API_URL',
  }),
  // The lake shape: kept in the corpus, no ticket chosen, so every ticket-shaped line
  // has to be absent rather than blank — and the linker's own guess is on screen as a
  // guess. This is the payload that would render a lie if the renderer forgot the case.
  linkKept: linkResultBlocks({
    key: '',
    messages: 3,
    sourceType: 'slack_paste',
    title: 'DM พี่ Natta เรื่อง redemption',
    day: '2026-08-19',
    at: '2026-08-20 08:08',
    corpusSize: 1495,
    placed: [{ key: 'REVERAPP-140', count: 1 }],
  }),
  pasteKept: pastePreviewBlocks({
    title: 'DM พี่ Natta เรื่อง redemption',
    day: '2026-08-19',
    key: '',
    records: pastedChat.map((r) => ({ ...r })),
    skipped: [],
    actionValue: 'pdemo2',
  }),
  pastePreview: pastePreviewBlocks({
    title: 'DM พี่ Natta เรื่อง redemption',
    day: '2026-08-19',
    key: 'REVERAPP-140',
    records: Array.from({ length: 12 }, (_, i) => ({
      user: 'Aim Sirawith',
      when: '2026-08-19 14:21',
      text: `ข้อความที่ ${i + 1} ${'ก'.repeat(300)}`,
    })),
    skipped: ['ก้อนที่อ่านไม่ออก'],
    actionValue: 'pdemo1',
  }),
  // ── worst case ───────────────────────────────────────────────────────────
  boardWorst: boardBlocks('บอร์ดรวม', manyItems(60)),
  standupWorst: standupDmBlocks(bigDraft(40)),
  itemWorst: itemCardBlocks(longHeadline as WorkItem),
  dailyPostWorst: dailyPostBlocks({ date: '2026-08-20', carried: manyStreaks, summaryAt: '10:45' }),
  dailySummaryWorst: dailySummaryBlocks({
    record: {
      ...dailyRecord,
      answers: Array.from({ length: 40 }, (_, i) => ({
        user: `U0DEMOUSER${i}`,
        ts: `sim.2026-08-20.${i}`,
        done: ['ก'.repeat(400)],
        focus: ['ก'.repeat(400)],
        blockers: [{ text: 'ก'.repeat(400), tag: 'PO' }],
        simulated: true,
      })),
    } as any,
    expected: Array.from({ length: 40 }, (_, i) => `U0DEMOUSER${i}`),
    unfilled: [],
    streaks: manyStreaks,
    pendingDays: 3,
  }),
  // Every seat in the roster answering by DM before the post goes out, each with the
  // longest line the store will hold — the shape `postDaily` carries into the thread.
  dmAnswerWorst: dmAnswerBlocks(
    Array.from({ length: 20 }, (_, i) => ({
      user: `U0DEMOUSER${i}`,
      ts: '',
      done: ['ก'.repeat(400)],
      focus: ['ก'.repeat(400)],
      blockers: [{ text: 'ก'.repeat(400), tag: 'PO' }],
      via: 'dm' as const,
    })),
  ),
  pendingEscalationWorst: pendingEscalationBlocks(manyStreaks, 3),
  staleWorst: staleBlocks(quietWorst, 5),
};

// digestBlocks() reads the shared cache rather than taking items, so the only way
// to exercise its trim path is to swap the items for a worst case and put the real
// ones straight back.
const realItems = l.items;
try {
  l.items = manyItems(60);
  payloads.digestWorst = digestBlocks();
} finally {
  l.items = realItems;
}

/** Modal-only payloads get Slack's modal cap; everything else is a message. */
const MODAL_ONLY = new Set(['driftModal']);

/** Slack's documented limits. Exceeding any of these is a runtime 400. */
const LIMITS = {
  blocksPerMessage: 50,
  blocksPerModal: 100,
  sectionText: 3000,
  headerText: 150,
  contextElements: 10,
  /** app.ts builds a modal title from an item key. Slack truncates at 24 — silently. */
  modalTitle: 24,
  buttonText: 75,
  optionText: 75,
};

let failures = 0;

/**
 * The ticket picker returns options, not blocks, so nothing above reaches it — and
 * Slack rejects the *whole view* if one label is over 75 characters, which a real
 * ticket summary crosses without trying.
 */
for (const option of [
  { key: 'REVERAPP-140', summary: 'ก'.repeat(400), state: 'In Progress', resolved: false, url: '', updated: 0 },
  { key: 'R-1', summary: '', state: '', resolved: true, url: '', updated: 0 },
].map(ticketOption)) {
  const label = option.text.text;
  if (!label.length || label.length > LIMITS.optionText) {
    console.log(`✗ ticketOption  label ${label.length} chars (limit ${LIMITS.optionText}, and 0 is also invalid)`);
    failures += 1;
  }
}
if (!failures) console.log('✓ ticketOption ทุกป้ายอยู่ในลิมิต 75 ตัวอักษร');

for (const [name, blocks] of Object.entries(payloads)) {
  const problems: string[] = [];
  const blockCap = MODAL_ONLY.has(name) ? LIMITS.blocksPerModal : LIMITS.blocksPerMessage;

  if (blocks.length > blockCap) {
    problems.push(`${blocks.length} blocks > ${blockCap}`);
  }

  blocks.forEach((b: any, i) => {
    if (b.type === 'section' && b.text?.text?.length > LIMITS.sectionText) {
      problems.push(`block ${i}: section text ${b.text.text.length} > ${LIMITS.sectionText}`);
    }
    if (b.type === 'header' && b.text?.text?.length > LIMITS.headerText) {
      problems.push(`block ${i}: header text ${b.text.text.length} > ${LIMITS.headerText}`);
    }
    if (b.type === 'context' && b.elements?.length > LIMITS.contextElements) {
      problems.push(`block ${i}: ${b.elements.length} context elements > ${LIMITS.contextElements}`);
    }
    if (b.type === 'section' && !b.text && !b.fields) {
      problems.push(`block ${i}: section with neither text nor fields`);
    }
    for (const el of [b.accessory, ...(b.elements ?? []), b.element].filter(Boolean)) {
      // A url button with an empty/relative url is a 400 at post time.
      if (el.type === 'button' && 'url' in el && !/^https?:\/\//.test(el.url ?? '')) {
        problems.push(`block ${i}: button url is not absolute (${el.url})`);
      }
      if (el.type === 'button' && !el.url && !el.action_id) {
        problems.push(`block ${i}: button has neither url nor action_id`);
      }
      if (el.type === 'button' && (el.text?.text?.length ?? 0) > LIMITS.buttonText) {
        problems.push(`block ${i}: button text ${el.text.text.length} > ${LIMITS.buttonText}`);
      }
      for (const opt of el.options ?? []) {
        if ((opt.text?.text?.length ?? 0) > LIMITS.optionText) {
          problems.push(`block ${i}: option text ${opt.text.text.length} > ${LIMITS.optionText}`);
        }
      }
      if (el.type === 'static_select' && !(el.options ?? []).length) {
        problems.push(`block ${i}: static_select with no options — Slack rejects the view`);
      }
    }
  });

  const status = problems.length ? '✗' : '✓';
  if (problems.length) failures++;
  console.log(`${status} ${name.padEnd(12)} ${String(blocks.length).padStart(2)} blocks`);
  for (const p of problems) console.log(`    ${p}`);
}

/**
 * Invariants the payloads above cannot show, because the offending string is
 * built in app.ts rather than in a block. clamp() is the one everything else
 * trusts: a modal title is the one call site sitting exactly on a hard limit,
 * so an off-by-one there is a silently rejected view.
 */
const invariants: Array<[string, boolean]> = [
  ['clamp() never exceeds max', clamp('A'.repeat(300), LIMITS.modalTitle).length <= LIMITS.modalTitle],
  ['clamp() never exceeds max (Thai)', clamp('ก'.repeat(300), LIMITS.modalTitle).length <= LIMITS.modalTitle],
  ['clamp() never exceeds max (spaces)', clamp('word '.repeat(60), LIMITS.modalTitle).length <= LIMITS.modalTitle],
  ['modal title from a long item key fits', clamp(longHeadline.key, LIMITS.modalTitle).length <= LIMITS.modalTitle],
  ['clamp() leaves short strings alone', clamp('สั้น', 24) === 'สั้น'],
];

for (const [what, ok] of invariants) {
  if (!ok) failures++;
  console.log(`${ok ? '✓' : '✗'} ${what}`);
}

const want = process.argv[2];
if (want && payloads[want]) {
  console.log('\n--- paste into Block Kit Builder ---');
  console.log(JSON.stringify({ blocks: payloads[want] }, null, 2));
}

console.log(
  failures ? `\n✗ ${failures} payload(s) มีปัญหา` : '\n✓ ทุก payload ผ่าน Slack limits',
);
process.exit(failures ? 1 : 0);
