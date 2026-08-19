import type { KnownBlock } from '@slack/types';
import { ledger, ledgerOrigin, refreshStatus, sortedItems, findMessage } from '../data.js';
import { apiConfig } from '../tam-api.js';
import type { WorkItem } from '../types.js';
import {
  CMD, STATE_LABEL, bodyText, clamp, context, days, divider, esc, evidenceButton,
  header, section, sourceCounts, ticketButton, who,
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
    out.push(context(`💬 *${who(ev.user)}* · ${ev.when} — “${bodyText(ev.text, 160)}”`));
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
  const owner = item.assignee ? ` · ${who(item.assignee)}` : '';
  return section(
    `*${item.key}*  ${esc(item.headline)}  ·  _${days(item.age_days)}_${owner}\n` +
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
        `สร้างเมื่อ ${l.built_at} · ` +
        // Where the numbers above came from, and how old they are. The digest looks
        // identical whether it was fetched from the pipeline or read from the
        // fixture, and a reader deciding something in standup deserves to know which
        // one they are reading — and to be told when the last refresh failed, since
        // the bot then keeps serving the previous data on purpose.
        (ledgerOrigin() === 'pipeline'
          ? `แหล่ง: pipeline ${esc(apiConfig()?.baseUrl ?? '')}`
          : 'แหล่ง: fixture data/ledger.json') +
        ` · ดึงมาเมื่อ ${refreshStatus().at}` +
        (refreshStatus().error ? ' · ⚠ โหลดใหม่ล่าสุดไม่สำเร็จ ข้อมูลเก่ากว่านี้' : ''),
    ),
  ];

  // Which items made it onto the screen, per block. The overflow footer has to
  // report how many were *left out*, and that is only knowable at the trim.
  const renderedBy: number[] = [0, 0];
  let rendered = 0;
  const push = (...bs: KnownBlock[]) => {
    for (const b of bs) {
      blocks.push(b);
      renderedBy.push(rendered);
    }
  };

  // Empty digest is a quiet window, not an error. Say so plainly.
  if (items.length === 0) {
    push(section('*เงียบทั้งหน้าต่างนี้* — ไม่มีงานที่ขยับเลย\nไม่ใช่ error ครับ แค่ไม่มีอะไรเปลี่ยน'));
    return blocks;
  }

  push(divider());

  if (blocked.length) {
    push(section(`*${STATE_LABEL.blocked}  (${blocked.length})*`));
    for (const i of blocked) {
      rendered++;
      push(...blockedRow(i));
    }
  } else {
    // Empty blockers is good news. Design it as good news.
    push(section('*✅ ไม่มีอะไรติด*\nทุกงานเดินได้หมด'));
  }

  if (stalled.length) {
    push(divider());
    push(section(`*${STATE_LABEL.stalled}  (${stalled.length})*  _In Progress แต่ไม่มีความเคลื่อนไหว_`));
    for (const i of stalled) {
      rendered++;
      push(compactRow(i));
    }
  }

  if (moving.length) {
    push(divider());
    push(section(`*${STATE_LABEL.moving}  (${moving.length})*`));
    for (const i of moving) {
      rendered++;
      push(compactRow(i));
    }
  }

  const tail: string[] = [];
  if (done.length) {
    // The done line names every key, so those items are accounted for even when
    // the trim below cuts the sections above.
    rendered += done.length;
    tail.push(`${STATE_LABEL.done} เมื่อวาน: ${done.map((d) => d.key).join(', ')}`);
  }
  if (l.unassigned.length) tail.push(`❓ ${l.unassigned.length} ข้อความที่ยังจับคู่ ticket ไม่ได้`);
  if (tail.length) {
    push(divider());
    push(context(tail.join('  ·  ')));
  }

  // Slack caps a message at 50 blocks. Trim from the tail — the top is the point.
  // Report what was *omitted*, not the total, and name a command that exists:
  // sending the reader to `/tam` was two errors in one line.
  const KEEP = 47;
  if (blocks.length > KEEP + 1) {
    const omitted = items.length - (renderedBy[KEEP - 1] ?? 0);
    return [
      ...blocks.slice(0, KEEP),
      context(`…อีก ${omitted} items ไม่ได้แสดง — ดูต่อด้วย \`${CMD} blocked\` หรือ \`${CMD} <KEY>\``),
    ];
  }
  return blocks;
}
