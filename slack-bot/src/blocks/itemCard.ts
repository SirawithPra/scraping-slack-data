import type { KnownBlock } from '@slack/types';
import type { WorkItem } from '../types.js';
import { findMessage } from '../data.js';
import {
  STATE_LABEL, SOURCE_ICON, clamp, context, days, divider, esc,
  evidenceButton, header, people, quote, section, sourceCounts, ticketButton,
} from './common.js';

const KIND_ICON: Record<string, string> = {
  status_change: '🔁',
  blocked: '⛔',
  unblocked: '🔓',
  scope_change: '✏️',
  decision: '🧠',
  commit: '⌗',
};

/**
 * The full ledger card for one work item.
 *
 * Note the ordering: computed facts first (state, evidence, timeline), the
 * model-written summary last and explicitly labelled. The derived facts are the
 * trustworthy part and should read as facts; the prose is the smallest part of
 * the product and should not be mistaken for the source of truth.
 */
export function itemCardBlocks(item: WorkItem): KnownBlock[] {
  const ev = findMessage(item.evidence_id);

  const blocks: KnownBlock[] = [
    header(`${item.key} · ${clamp(item.headline, 100)}`),
    section(
      `*${STATE_LABEL[item.state]}*  ·  _${days(item.age_days)}_\n${esc(item.evidence)}`,
      ticketButton(item),
    ),
  ];

  if (ev) {
    blocks.push(context(`หลักฐาน: ${SOURCE_ICON[ev.source]} *${esc(ev.user)}* · ${ev.when}`));
    blocks.push({
      type: 'actions',
      elements: [evidenceButton(ev.id, ev.permalink) as any],
    } as KnownBlock);
  }

  blocks.push(
    context(
      `${sourceCounts(item)}  ·  ${item.first} → ${item.last}` +
        (item.assignee ? `  ·  รับผิดชอบ: ${esc(item.assignee)}` : '') +
        (item.youtrack_status ? `  ·  YouTrack: ${esc(item.youtrack_status)}` : ''),
    ),
  );
  blocks.push(context(`เกี่ยวข้อง: ${people(item.participants)}`));

  if (item.timeline.length) {
    blocks.push(divider());
    blocks.push(section('*ไทม์ไลน์*'));
    for (const t of item.timeline.slice(0, 10)) {
      const m = t.evidence_id ? findMessage(t.evidence_id) : undefined;
      const link = m?.permalink ? ` <${m.permalink}|↗>` : '';
      blocks.push(
        context(
          `${KIND_ICON[t.kind] ?? '•'} *${t.when}*  ${SOURCE_ICON[t.source]} ${esc(t.user)} — ${esc(clamp(t.text, 200))}${link}`,
        ),
      );
    }
  }

  const recent = item.messages.slice(-3);
  if (recent.length) {
    blocks.push(divider());
    blocks.push(section('*ข้อความล่าสุด*'));
    for (const m of recent) blocks.push(section(quote(m)));
  }

  if (item.summary) {
    blocks.push(divider());
    if (item.summary.unverified) {
      // The model's citations failed verification. Warn, but keep showing the
      // derived facts above — those are still good.
      blocks.push(
        section(
          '*⚠️ สรุปนี้ตรวจสอบไม่ผ่าน*\nอ้างอิงที่โมเดลให้มาไม่ตรงกับข้อความจริง — อ่านเป็นการเดา ไม่ใช่ข้อเท็จจริง\n' +
            'ข้อมูลด้านบน (สถานะ, ไทม์ไลน์, หลักฐาน) คำนวณจากข้อมูลจริง ยังเชื่อได้',
        ),
      );
    } else {
      blocks.push(context('สรุปโดยโมเดล · ข้อมูลด้านบนคำนวณจากข้อมูลจริง'));
    }
    blocks.push(section(esc(clamp(item.summary.detail, 600))));
    if (item.summary.next_step) blocks.push(section(`*ขั้นต่อไป:* ${esc(item.summary.next_step)}`));
  }

  return blocks;
}

/** Compact "my board" / "@someone's board" list. */
export function boardBlocks(title: string, items: WorkItem[]): KnownBlock[] {
  if (!items.length) {
    return [header(title), section('*ไม่มีงานค้าง* 🎉\nไม่มีอะไรเปิดค้างอยู่เลย')];
  }
  const blocks: KnownBlock[] = [header(title), context(`${items.length} งานที่ยังเปิดอยู่ · นิ่งนานสุดขึ้นก่อน`)];
  for (const i of items) {
    blocks.push(
      section(
        `${STATE_LABEL[i.state]}  *${i.key}*  ${esc(clamp(i.headline, 80))}  ·  _${days(i.age_days)}_\n` +
          `${esc(clamp(i.evidence, 160))}`,
        { type: 'button', text: { type: 'plain_text', text: 'เปิด' }, action_id: 'open_item', value: i.key },
      ),
    );
  }
  return blocks;
}
