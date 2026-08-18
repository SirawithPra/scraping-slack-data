/**
 * `/meowtam silent` — open tickets nobody has touched.
 *
 * This is the half of the board Slack structurally cannot contain. Work nobody is
 * discussing leaves no trace in a chat log, so no amount of reading Slack surfaces it —
 * measured on this workspace, 61 tickets were open and Slack mentioned five. It is also
 * the only finding here that covers everything: drift needs a ticket key somebody typed
 * into a channel and therefore sees about a quarter of the board, while this needs
 * nothing from Slack at all.
 *
 * Two things the copy has to carry, because leaving either out would overstate it:
 * the tracker being unreachable is reported rather than rendered as an empty list, and
 * `mentioned_in_slack` is bounded by the exported window rather than by all of history.
 */

import type { KnownBlock } from '@slack/types';

import { context, section, esc, clamp } from './common.js';

export interface SilentTicket {
  ticket: string;
  state: string;
  url: string;
  summary: string;
  quiet_days: number;
  mentioned_in_slack: boolean;
}

export interface TrackerReport {
  coverage: Record<string, number>;
  drift: Array<{ ticket: string; ticket_state: string; our_state: string; detail: string; ticket_url: string }>;
  silent: SilentTicket[];
  error: string;
  built_at: string;
}

/** Slack's hard cap. The list is trimmed and says how much it trimmed. */
const MAX_ROWS = 12;

export function silentBlocks(report: TrackerReport): KnownBlock[] {
  if (report.error) {
    return [
      section('*อ่าน ticket tracker ไม่ได้*\nที่แสดงว่าง ๆ ไม่ได้หมายความว่าไม่มีงานค้าง — หมายความว่า *ยังไม่รู้*'),
      context(`เหตุผล: ${clamp(esc(report.error), 300)}`),
    ];
  }
  const rows = report.silent;
  if (!rows.length) {
    const open = report.coverage.tracker_open ?? 0;
    return [
      section(`*ไม่มี ticket เปิดค้างที่เงียบเกินเกณฑ์*\nจาก ${open} ticket ที่เปิดอยู่ ทุกอันถูกแตะภายในช่วงที่ถือว่าปกติ`),
    ];
  }

  // Group by how long, because the finding on the real board was not one stale ticket —
  // it was two batches moved on a single day and never picked up again. A flat list
  // hides that; the reader needs to see the shape.
  const shown = rows.slice(0, MAX_ROWS);
  const blocks: KnownBlock[] = [
    section(
      `*ticket เปิดค้างที่ไม่มีใครแตะ — ${rows.length} อัน*\n` +
        `จาก ${report.coverage.tracker_open ?? '?'} ticket ที่เปิดอยู่ · เรียงเงียบนานสุดก่อน`,
    ),
  ];
  for (const row of shown) {
    const talk = row.mentioned_in_slack ? 'มีคนพูดถึงใน Slack' : 'ไม่มีใครพูดถึง';
    blocks.push(section(
      `<${row.url}|*${esc(row.ticket)}*>  \`${esc(row.state)}\`  · เงียบ *${Math.round(row.quiet_days)} วัน* · ${talk}\n` +
        `${esc(clamp(row.summary, 140))}`,
    ));
  }
  if (rows.length > shown.length) {
    blocks.push(context(`ยังมีอีก ${rows.length - shown.length} อัน — ดูครบใน dashboard`));
  }
  blocks.push(context(
    '“ไม่มีใครพูดถึง” นับจากช่วงที่ export มา ไม่ใช่ทั้งประวัติ · ' +
      'นี่คือส่วนที่อ่าน Slack อย่างเดียวมองไม่เห็น เพราะงานที่ไม่มีใครคุยไม่ทิ้งร่องรอยในแชท',
  ));
  return blocks;
}

export function driftBlocks(report: TrackerReport): KnownBlock[] {
  if (report.error) return silentBlocks(report);
  const cov = report.coverage;
  if (!report.drift.length) {
    return [
      section('*Slack กับ ticket ไม่ขัดกัน*'),
      context(
        `ตรวจได้เฉพาะงานที่มี ticket key ใน Slack — ${cov.with_ticket_key ?? 0} จาก ${cov.topics ?? 0} ชิ้น · ` +
          'ความเงียบจากที่เหลือคือไม่มีหลักฐาน ไม่ใช่หลักฐานว่าตรงกัน',
      ),
    ];
  }
  const blocks: KnownBlock[] = [section(`*Slack กับ ticket เล่าไม่ตรงกัน — ${report.drift.length} อัน*`)];
  for (const d of report.drift.slice(0, MAX_ROWS)) {
    blocks.push(section(
      `<${d.ticket_url}|*${esc(d.ticket)}*>  ticket ว่า \`${esc(d.ticket_state)}\` · เราอ่าน Slack ได้ \`${esc(d.our_state)}\`\n` +
        esc(clamp(d.detail, 200)),
    ));
  }
  blocks.push(context(
    `ครอบคลุม ${cov.with_ticket_key ?? 0}/${cov.topics ?? 0} งานที่มี ticket key`,
  ));
  return blocks;
}
