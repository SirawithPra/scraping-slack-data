import type { KnownBlock } from '@slack/types';
import { ledger, sortedItems, findMessage } from '../data.js';
import type { WorkItem } from '../types.js';
import {
  STATE_LABEL, clamp, context, days, divider, esc, evidenceButton,
  header, people, section, sourceCounts, ticketButton,
} from './common.js';

/**
 * The 09:25 digest — the screen someone opens five minutes before standup.
 * One job: show what is stuck and why, before the meeting starts.
 *
 * Order is blocked → stalled → moving → done, stalest first within each group.
 * Done collapses to a single line: it is reassurance, not information.
 */

function blockedRow(item: WorkItem): KnownBlock[] {
  const ev = findMessage(item.evidence_id);
  const out: KnownBlock[] = [
    section(
      `*${item.key}*  ${esc(item.headline)}  ·  _${days(item.age_days)}_\n` +
        `${esc(item.evidence)}`,
      ticketButton(item),
    ),
  ];
  if (ev) {
    out.push(context(`💬 *${esc(ev.user)}* · ${ev.when} — “${esc(clamp(ev.text, 160))}”`));
    out.push({
      type: 'actions',
      elements: [
        evidenceButton(ev.id, ev.permalink, 'ดูข้อความที่ติด') as any,
        {
          type: 'button',
          text: { type: 'plain_text', text: 'ไทม์ไลน์' },
          action_id: 'open_item',
          value: item.key,
        },
      ],
    });
  }
  return out;
}

function compactRow(item: WorkItem): KnownBlock {
  const who = item.assignee ? ` · ${esc(item.assignee)}` : '';
  return section(
    `*${item.key}*  ${esc(item.headline)}  ·  _${days(item.age_days)}_${who}\n` +
      `${sourceCounts(item)}${item.youtrack_status ? `  ·  YouTrack: ${esc(item.youtrack_status)}` : ''}`,
    {
      type: 'button',
      text: { type: 'plain_text', text: 'เปิด' },
      action_id: 'open_item',
      value: item.key,
    },
  );
}

export function digestBlocks(): KnownBlock[] {
  const l = ledger();
  const items = sortedItems();
  const blocked = items.filter((i) => i.state === 'blocked');
  const stalled = items.filter((i) => i.state === 'stalled');
  const moving = items.filter((i) => i.state === 'moving');
  const done = items.filter((i) => i.state === 'done');

  const date = l.built_at.slice(0, 10);
  const blocks: KnownBlock[] = [
    header(`Standup · ${date}`),
    context(
      `${items.length} work items · หน้าต่าง ${l.window_days} วัน · จาก ${l.corpus_size} ข้อความ · ` +
        `สร้างเมื่อ ${l.built_at}`,
    ),
  ];

  // Empty digest is a quiet window, not an error. Say so plainly.
  if (items.length === 0) {
    blocks.push(section('*เงียบทั้งหน้าต่างนี้* — ไม่มีงานที่ขยับเลย\nไม่ใช่ error ครับ แค่ไม่มีอะไรเปลี่ยน'));
    return blocks;
  }

  blocks.push(divider());

  if (blocked.length) {
    blocks.push(section(`*${STATE_LABEL.blocked}  (${blocked.length})*`));
    for (const i of blocked) blocks.push(...blockedRow(i));
  } else {
    // Empty blockers is good news. Design it as good news.
    blocks.push(section('*✅ ไม่มีอะไรติด*\nทุกงานเดินได้หมด'));
  }

  if (stalled.length) {
    blocks.push(divider());
    blocks.push(section(`*${STATE_LABEL.stalled}  (${stalled.length})*  _In Progress แต่ไม่มีความเคลื่อนไหว_`));
    for (const i of stalled) blocks.push(compactRow(i));
  }

  if (moving.length) {
    blocks.push(divider());
    blocks.push(section(`*${STATE_LABEL.moving}  (${moving.length})*`));
    for (const i of moving) blocks.push(compactRow(i));
  }

  const tail: string[] = [];
  if (done.length) tail.push(`${STATE_LABEL.done} เมื่อวาน: ${done.map((d) => d.key).join(', ')}`);
  if (l.unassigned.length) tail.push(`❓ ${l.unassigned.length} ข้อความที่ยังจับคู่ ticket ไม่ได้`);
  if (tail.length) {
    blocks.push(divider());
    blocks.push(context(tail.join('  ·  ')));
  }

  // Slack caps a message at 50 blocks. Trim from the tail — the top is the point.
  if (blocks.length > 48) {
    return [...blocks.slice(0, 47), context(`…และอีก ${items.length} items — ดูทั้งหมดด้วย \`/tam\``)];
  }
  return blocks;
}
