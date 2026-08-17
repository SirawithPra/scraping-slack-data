import type { KnownBlock, View } from '@slack/types';
import type { Drift, WorkItem } from '../types.js';
import { findMessage } from '../data.js';
import { clamp, context, divider, esc, section } from './common.js';

/**
 * Drift detection — the demo's wow beat, and the fix for
 * "requirement changed but nobody updated the ticket".
 *
 * The bot notices a scope-change cue in a thread linked to a ticket whose
 * description has not been edited since. It posts *in that thread*, at the
 * moment the context is still in everyone's head, and offers a diff.
 *
 * It never writes to YouTrack on its own. A human presses Save. That single
 * constraint is what makes the feature adoptable rather than terrifying.
 */
export function driftNudgeBlocks(drift: Drift, item: WorkItem): KnownBlock[] {
  const trigger = findMessage(drift.trigger_id);
  return [
    section(
      `*ดูเหมือนสโคปของ ${item.key} เปลี่ยน แต่ ticket ยังไม่ได้อัปเดต*\n` +
        `เจอคำว่า “${esc(drift.cue)}” ในเธรดนี้` +
        (trigger ? ` เมื่อ ${trigger.when}` : ''),
    ),
    context(
      `ticket description แก้ล่าสุด: *${item.description_updated ?? 'ไม่ทราบ'}*` +
        (item.youtrack_status ? `  ·  สถานะ: ${esc(item.youtrack_status)}` : ''),
    ),
    {
      type: 'actions',
      elements: [
        {
          type: 'button',
          style: 'primary',
          text: { type: 'plain_text', text: 'ดูร่างที่เสนอ' },
          action_id: 'open_drift_modal',
          value: drift.item_key,
        },
        {
          type: 'button',
          text: { type: 'plain_text', text: 'ไม่ใช่การเปลี่ยนสโคป' },
          action_id: 'dismiss_drift',
          value: drift.item_key,
        },
      ],
    } as KnownBlock,
    context('ผมไม่เขียนลง YouTrack เองนะครับ — ต้องมีคนกดยืนยันเสมอ'),
  ];
}

/** Side-by-side-ish diff. Slack has no diff widget, so we fake it with code blocks. */
export function driftModal(drift: Drift, item: WorkItem): View {
  const trigger = findMessage(drift.trigger_id);
  const blocks: KnownBlock[] = [
    section(`*${item.key}* — ${esc(clamp(item.headline, 120))}`),
    context(`ร่างนี้สร้างจากเธรดใน Slack และอ้างอิงข้อความจริง · กด Save แล้วถึงจะเขียนลง YouTrack`),
    divider(),
  ];

  if (trigger) {
    blocks.push(section('*ข้อความที่ทำให้สโคปเปลี่ยน*'));
    blocks.push(section(`>*${esc(trigger.user)}* · ${trigger.when}\n>${esc(trigger.text).replace(/\n/g, '\n>')}`));
    blocks.push(divider());
  }

  blocks.push(section('*ของเดิมใน YouTrack*'));
  blocks.push(section('```\n' + clamp(drift.current_description, 900) + '\n```'));
  blocks.push(section('*ร่างใหม่*'));
  blocks.push({
    type: 'input',
    block_id: 'description',
    label: { type: 'plain_text', text: 'แก้ได้ก่อนบันทึก' },
    element: {
      type: 'plain_text_input',
      action_id: 'value',
      multiline: true,
      initial_value: drift.proposed_description.slice(0, 3000),
    },
  } as KnownBlock);
  blocks.push({
    type: 'input',
    block_id: 'comment',
    optional: true,
    label: { type: 'plain_text', text: 'คอมเมนต์ที่จะแนบไปด้วย' },
    element: {
      type: 'plain_text_input',
      action_id: 'value',
      initial_value: trigger?.permalink
        ? `อัปเดตสโคปตามที่คุยใน Slack: ${trigger.permalink}`
        : 'อัปเดตสโคปตามที่คุยใน Slack',
    },
  } as KnownBlock);

  return {
    type: 'modal',
    callback_id: 'drift_save',
    private_metadata: item.key,
    title: { type: 'plain_text', text: 'อัปเดต ticket' },
    submit: { type: 'plain_text', text: 'บันทึกลง YouTrack' },
    close: { type: 'plain_text', text: 'ยกเลิก' },
    blocks,
  };
}
