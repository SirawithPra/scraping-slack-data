import type { KnownBlock } from '@slack/types';
import type { StandupDraft } from '../types.js';
import { findMessage } from '../data.js';
import { clamp, context, divider, esc, header, section } from './common.js';

/**
 * The 08:45 DM — the heart of the pitch.
 *
 * A normal standup bot asks "what did you do yesterday?" and the dev has to
 * remember. This one shows them what it already knows, from their own messages
 * and commits, and asks them to *correct* it. Recall is the expensive part;
 * correction is cheap. That inversion is the whole idea, and it is also the
 * fix for "I forget what ended last week" — the bot didn't forget.
 *
 * `carried_over` is the pain-#1 fix: work still open from before that nobody
 * would have mentioned unless asked by name.
 */
export function standupDmBlocks(draft: StandupDraft): KnownBlock[] {
  const blocks: KnownBlock[] = [
    header('สรุปของคุณเมื่อวาน'),
    context('ผมดึงมาจาก Slack + YouTrack ให้แล้ว — *แก้ได้ถ้าไม่ถูก* ไม่ต้องพิมพ์ใหม่ทั้งหมด'),
    divider(),
  ];

  if (draft.yesterday.length === 0) {
    blocks.push(section('_เมื่อวานไม่เจอความเคลื่อนไหวของคุณเลย — ถ้าทำอะไรอยู่ เขียนข้างล่างได้ครับ_'));
  }

  for (const y of draft.yesterday) {
    const ev = y.evidence_id ? findMessage(y.evidence_id) : undefined;
    blocks.push(
      section(`*${y.key}*  ${esc(y.headline)}\n${esc(y.note)}`, ev?.permalink
        ? { type: 'button', text: { type: 'plain_text', text: 'ดูข้อความ' }, url: ev.permalink }
        : undefined),
    );
    if (ev) blocks.push(context(`💬 ${ev.when} · จาก ${esc(ev.source)}`));
  }

  if (draft.carried_over.length) {
    blocks.push(divider());
    blocks.push(section('*⏸ ค้างจากก่อนหน้านี้ — ยังไม่ขยับ*'));
    for (const c of draft.carried_over) {
      blocks.push(
        context(`*${c.key}*  ${esc(clamp(c.headline, 80))} — นิ่งมา *${Math.round(c.stale_days)} วัน*`),
      );
    }
  }

  blocks.push(divider());
  blocks.push({
    type: 'input',
    block_id: 'today',
    optional: true,
    label: { type: 'plain_text', text: 'วันนี้ทำอะไร' },
    element: {
      type: 'plain_text_input',
      action_id: 'value',
      multiline: true,
      placeholder: { type: 'plain_text', text: 'ต่อจากเมื่อวานได้เลย ไม่ต้องยาว' },
    },
  } as KnownBlock);
  blocks.push({
    type: 'input',
    block_id: 'blocker',
    optional: true,
    label: { type: 'plain_text', text: 'มีอะไรติดไหม' },
    element: {
      type: 'plain_text_input',
      action_id: 'value',
      multiline: true,
      placeholder: { type: 'plain_text', text: 'ว่างไว้ได้ถ้าไม่มี' },
    },
  } as KnownBlock);

  blocks.push({
    type: 'actions',
    elements: [
      {
        type: 'button',
        style: 'primary',
        text: { type: 'plain_text', text: 'ส่ง' },
        action_id: 'standup_submit',
        value: draft.slack_user_id,
      },
      {
        type: 'button',
        text: { type: 'plain_text', text: 'ข้ามวันนี้' },
        action_id: 'standup_skip',
        value: draft.slack_user_id,
      },
    ],
  } as KnownBlock);

  blocks.push(context('ส่งภายใน 09:15 · ถ้าไม่ส่ง ผมจะใช้ที่ดึงมาให้ข้างบนแทน ไม่มีใครโดนทวงในห้อง'));
  return blocks;
}
