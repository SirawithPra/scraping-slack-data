import type { KnownBlock } from '@slack/types';
import type { StandupDraft } from '../types.js';
import { findMessage } from '../data.js';
// The three boxes are rendered here and decided in `standups.ts`, next to the draft
// they are filled from — the daily form reads the same two functions, which is the
// only way the DM and the pasted form can stay the same form.
import { MAX_YESTERDAY, standupDone, standupPrefill } from '../standups.js';
import { CMD, clamp, context, divider, esc, header, section } from './common.js';

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
 *
 * Both lists are capped. A `yesterday` entry costs two blocks and a
 * `carried_over` entry one, and Slack rejects the whole message at 50 blocks —
 * so an unbounded draft means the busiest person is the one who gets no DM at
 * all. The overflow is stated on screen rather than dropped quietly: a draft the
 * reader cannot tell is incomplete is worse than a short one.
 */
const MAX_CARRIED = 5;

export function standupDmBlocks(draft: StandupDraft): KnownBlock[] {
  const blocks: KnownBlock[] = [
    header('สรุปของคุณเมื่อวาน'),
    context('ผมดึงมาจาก Slack + YouTrack ให้แล้ว — *แก้ได้ถ้าไม่ถูก* ไม่ต้องพิมพ์ใหม่ทั้งหมด'),
    divider(),
  ];

  if (draft.yesterday.length === 0) {
    blocks.push(section('_เมื่อวานไม่เจอความเคลื่อนไหวของคุณเลย — ถ้าทำอะไรอยู่ เขียนข้างล่างได้ครับ_'));
  }

  for (const y of draft.yesterday.slice(0, MAX_YESTERDAY)) {
    const ev = y.evidence_id ? findMessage(y.evidence_id) : undefined;
    blocks.push(
      section(`*${y.key}*  ${esc(y.headline)}\n${esc(y.note)}`, ev?.permalink
        ? { type: 'button', text: { type: 'plain_text', text: 'ดูข้อความ' }, url: ev.permalink }
        : undefined),
    );
    if (ev) blocks.push(context(`💬 ${ev.when} · จาก ${esc(ev.source)}`));
  }
  const moreYesterday = draft.yesterday.length - MAX_YESTERDAY;
  if (moreYesterday > 0) {
    blocks.push(context(`…และอีก ${moreYesterday} งานที่คุณขยับเมื่อวาน — ดูทั้งหมดด้วย \`${CMD}\``));
  }

  if (draft.carried_over.length) {
    blocks.push(divider());
    blocks.push(section('*⏸ ค้างจากก่อนหน้านี้ — ยังไม่ขยับ*'));
    // Stalest first, so a cap drops the freshest rather than the most overdue.
    const carried = [...draft.carried_over].sort((a, b) => b.stale_days - a.stale_days);
    for (const c of carried.slice(0, MAX_CARRIED)) {
      blocks.push(
        context(`*${c.key}*  ${esc(clamp(c.headline, 80))} — นิ่งมา *${Math.round(c.stale_days)} วัน*`),
      );
    }
    const moreCarried = carried.length - MAX_CARRIED;
    if (moreCarried > 0) {
      blocks.push(context(`…และอีก ${moreCarried} งานที่ค้างอยู่ — ดูทั้งหมดด้วย \`${CMD}\``));
    }
  }

  blocks.push(divider());
  const prefill = standupPrefill(draft);
  const done = standupDone(draft).join('\n');
  // Said before the boxes, because the boxes are the thing it explains. Three boxes,
  // in the daily template's order, labelled with the daily template's headings: the
  // two forms have to be recognisably one form or "answer either one" is a promise
  // this DM cannot keep, and the person answers twice to be sure.
  blocks.push(
    section(`*กรอกสามช่องนี้ = ตอบ daily ของวันนี้* — หัวข้อเดียวกับฟอร์ม \`${CMD} daily\``),
  );
  blocks.push({
    type: 'input',
    block_id: 'done',
    optional: true,
    label: { type: 'plain_text', text: 'เมื่อวานทำอะไร · Done Yesterday' },
    element: {
      type: 'plain_text_input',
      action_id: 'value',
      multiline: true,
      ...(done ? { initial_value: done } : {}),
      placeholder: { type: 'plain_text', text: 'ที่ปิดไป ที่ review ให้คนอื่น ที่คุยจนได้ข้อสรุป' },
    },
  } as KnownBlock);
  if (done) {
    blocks.push(
      context(
        'เติมให้จากงานที่คุณขยับเมื่อวาน — *เป็นสิ่งที่ผมเห็น ไม่ใช่คำพูดของคุณ* ' +
          'ที่ค้างในช่องนี้คือที่จะถูกบันทึกไว้ ไม่ถูกก็ลบหรือพิมพ์ทับเลย',
      ),
    );
  }
  blocks.push({
    type: 'input',
    block_id: 'today',
    optional: true,
    label: { type: 'plain_text', text: 'วันนี้ทำอะไร · Focus Today' },
    element: {
      type: 'plain_text_input',
      action_id: 'value',
      multiline: true,
      // `initial_value` only when there is something to put in it: Slack renders an
      // empty string as a filled-in answer of nothing, and the placeholder — which is
      // the instruction — disappears behind it.
      ...(prefill.today ? { initial_value: prefill.today } : {}),
      placeholder: { type: 'plain_text', text: 'ต่อจากเมื่อวานได้เลย ไม่ต้องยาว' },
    },
  } as KnownBlock);
  if (prefill.today) {
    blocks.push(
      context(
        'เติมให้จากงานที่ยังไม่ปิด — *เป็นข้อเสนอ ไม่ใช่คำพูดของคุณ* ลบทิ้งหรือพิมพ์ทับได้เลย',
      ),
    );
  }
  blocks.push({
    type: 'input',
    block_id: 'blocker',
    optional: true,
    label: { type: 'plain_text', text: 'มีอะไรติดไหม · Blockers / Pending' },
    element: {
      type: 'plain_text_input',
      action_id: 'value',
      multiline: true,
      ...(prefill.blocker ? { initial_value: prefill.blocker } : {}),
      placeholder: { type: 'plain_text', text: 'ว่างไว้ได้ถ้าไม่มี' },
    },
  } as KnownBlock);
  blocks.push(
    context(
      prefill.blocker
        ? 'บรรทัดนี้คือข้อความที่ *คุณเขียนเอง* แล้วงานยังไม่ขยับ — เคลียร์แล้วลบทิ้งได้ · ' +
            'ถ้ายังติด เติม `@คนที่ต้องทำให้ก่อน` หรือ `PO` ต่อท้าย ผมจะได้รู้ว่ารอใคร'
        : 'รูปแบบเดียวกับ daily: `Pending [เรื่องที่รออยู่] [@คน หรือ PO]` · ไม่มีก็ว่างไว้',
    ),
  );

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

  // What the button does, said where the thumb is. The boxes above are in the daily's
  // format because pressing ส่ง *is* answering the daily — a person who does not know
  // that types the same three sections into the thread an hour later, which is the
  // duplicate work this DM was built to remove.
  blocks.push(
    context(
      'กด “ส่ง” = ตอบ daily ของวันนี้เลย ไม่ต้องพิมพ์ในเธรดซ้ำ · ' +
        'ส่งภายใน 09:15 · ไม่ส่งก็ไม่มีใครแท็กทวง แต่ชื่อจะขึ้นในสรุปว่ายังไม่ตอบ',
    ),
  );
  return blocks;
}
