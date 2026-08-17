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
  boardWorst: boardBlocks('บอร์ดรวม', manyItems(60)),
  standupWorst: standupDmBlocks(bigDraft(40)),
  itemWorst: itemCardBlocks(longHeadline as WorkItem),
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
