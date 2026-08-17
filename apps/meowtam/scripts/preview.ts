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
 */
import { digestBlocks } from '../src/blocks/digest.js';
import { standupDmBlocks } from '../src/blocks/standupDm.js';
import { itemCardBlocks, boardBlocks } from '../src/blocks/itemCard.js';
import { driftNudgeBlocks, driftModal } from '../src/blocks/drift.js';
import { recallBlocks } from '../src/blocks/recall.js';
import { ledger, findItem, sortedItems } from '../src/data.js';

const l = ledger();
const drift = l.drifts[0];
const driftItem = drift ? findItem(drift.item_key) : undefined;

const payloads: Record<string, any[]> = {
  digest: digestBlocks(),
  standup: l.standups[0] ? standupDmBlocks(l.standups[0]) : [],
  item: findItem('MOB-142') ? itemCardBlocks(findItem('MOB-142')!) : [],
  board: boardBlocks('งานของ @ake', sortedItems().filter((i) => i.state !== 'done')),
  drift: drift && driftItem ? driftNudgeBlocks(drift, driftItem) : [],
  driftModal: drift && driftItem ? (driftModal(drift, driftItem).blocks as any[]) : [],
  recall: await recallBlocks('ตอนนั้นเราสรุปเรื่อง export encoding ว่ายังไงนะ'),
  recallEmpty: await recallBlocks('qqqzzzxxx wvwvwv jjjkkk'),
};

/** Slack's documented limits. Exceeding any of these is a runtime 400. */
const LIMITS = { blocksPerMessage: 50, sectionText: 3000, headerText: 150, contextElements: 10 };

let failures = 0;

for (const [name, blocks] of Object.entries(payloads)) {
  const problems: string[] = [];

  if (blocks.length > LIMITS.blocksPerMessage) {
    problems.push(`${blocks.length} blocks > ${LIMITS.blocksPerMessage}`);
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
    // A url button with an empty/relative url is a 400 at post time.
    for (const el of [b.accessory, ...(b.elements ?? [])].filter(Boolean)) {
      if (el.type === 'button' && 'url' in el && !/^https?:\/\//.test(el.url ?? '')) {
        problems.push(`block ${i}: button url is not absolute (${el.url})`);
      }
      if (el.type === 'button' && !el.url && !el.action_id) {
        problems.push(`block ${i}: button has neither url nor action_id`);
      }
    }
  });

  const status = problems.length ? '✗' : '✓';
  if (problems.length) failures++;
  console.log(`${status} ${name.padEnd(12)} ${String(blocks.length).padStart(2)} blocks`);
  for (const p of problems) console.log(`    ${p}`);
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
