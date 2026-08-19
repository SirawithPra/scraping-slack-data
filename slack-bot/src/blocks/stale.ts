/**
 * Work nobody has mentioned for a week of working days, said once, in the channel.
 *
 * This is the second of the two channel-wide messages this bot is allowed to send,
 * and it earns that the same way the first one does: by resting on a count anybody
 * can check rather than on a judgement about importance. The claim is "the last
 * message on this item was on <date>, which is N working days ago", and every card
 * carries the button that opens that message.
 *
 * Nobody is tagged. The pending escalation tags a person because a person wrote the
 * line and named who they were waiting on; here there is no such statement — an item
 * going quiet is a fact about the team, and picking somebody to hold responsible for
 * it from the participant list would be the bot inventing an owner.
 */

import type { KnownBlock } from '@slack/types';

import type { StaleItem } from '../stale.js';
import { STATE_LABEL, clamp, context, esc, evidenceButton, people, section } from './common.js';

const MAX_ITEMS = 8;

/** `REVERAPP-140 · หน้า redemption` with its state and how long it has been quiet. */
function staleLine(entry: StaleItem): string {
  const item = entry.item;
  const owner = item.assignee || item.participants[0];
  return (
    `*${esc(item.key)}* · ${esc(clamp(item.headline, 120))}\n` +
    `${STATE_LABEL[item.state]} · เงียบมา *${entry.workdays} วันทำการ* (ข้อความล่าสุด ${esc(entry.since)})` +
    (owner ? `\nคนที่อยู่ในเรื่องนี้: ${esc(people(item.participants.slice(0, 4)))}` : '')
  );
}

/**
 * The escalation card.
 *
 * `threshold` is printed rather than hard-coded into the copy: the number is a
 * setting, and a message that says "5 วันทำการ" while the bot is configured for 3
 * is a small lie that costs the whole card its credibility.
 */
export function staleBlocks(entries: StaleItem[], threshold: number): KnownBlock[] {
  if (!entries.length) {
    return [
      section(`*✅ ไม่มีงานที่เงียบเกิน ${threshold} วันทำการ*`),
      context('นับจากข้อความล่าสุดของแต่ละงาน ไม่รวมเสาร์อาทิตย์ · งานที่ปิดแล้วไม่ถูกนับ'),
    ];
  }

  const blocks: KnownBlock[] = [
    section(
      `*⏳ ไม่มีใครแตะมาเกิน ${threshold} วันทำการ (${entries.length} งาน)*\n` +
        'ไม่ใช่ว่าใครแจ้งว่าติด — คือไม่มีข้อความถึงงานพวกนี้เลย ซึ่งเป็นคนละอาการกัน',
    ),
  ];

  for (const entry of entries.slice(0, MAX_ITEMS)) {
    const evidence = entry.item.messages.find((m) => m.id === entry.item.evidence_id) ?? entry.item.messages.at(-1);
    blocks.push({
      type: 'section',
      text: { type: 'mrkdwn', text: staleLine(entry) },
      accessory: evidence
        ? (evidenceButton(evidence.id, evidence.permalink, 'ข้อความล่าสุด') as any)
        : undefined,
    } as KnownBlock);
  }

  if (entries.length > MAX_ITEMS) {
    blocks.push(context(`อีก ${entries.length - MAX_ITEMS} งานไม่ได้แสดงที่นี่ — ดูทั้งหมดด้วย \`/meowtam stale\``));
  }

  blocks.push(
    context(
      `นับเป็นวันทำการ ไม่รวมเสาร์อาทิตย์ (ไม่ได้หักวันหยุดนักขัตฤกษ์) · ` +
        'แจ้งครั้งเดียวต่องาน แล้วจะเงียบไปจนกว่าจะเงียบต่ออีกรอบเท่าเดิม',
    ),
  );
  return blocks;
}
