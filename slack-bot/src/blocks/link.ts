/**
 * Linking a message to a ticket, and saying what actually happened.
 *
 * This screen used to end with *"เก็บเป็น override ถาวร"* over a `console.log`, and
 * the drift modal next to it ended with *"description ใหม่ถูกเขียนลง YouTrack"* over
 * another one. Both are gone. What replaces them is not a nicer sentence — it is a
 * result object with a field per thing that was attempted, rendered one line each,
 * including the ones that did not happen and why.
 *
 * The ordering is deliberate: the local write first (it is the one that always
 * works), the tracker write second (it depends on a token somebody has to grant),
 * and the rebuilt ledger last (it depends on the pipeline being up). A reader can
 * stop at any line and know exactly how far it got.
 */

import type { KnownBlock } from '@slack/types';

import { context, divider, esc, section } from './common.js';
import type { Ticket } from '../youtrack.js';

/** What one link attempt did, field by field, so nothing is claimed that did not happen. */
export interface LinkResult {
  key: string;
  /** How many messages were attached. One for a shortcut, many for a pasted chat. */
  messages: number;
  /** The corrections file, after the write. Empty when the write itself failed. */
  overridesFile?: string;
  overridesTotal?: number;
  overridesError?: string;
  /** The comment YouTrack stored, if one was asked for and accepted. */
  commentId?: string;
  commentUrl?: string;
  commentError?: string;
  /** What the rebuilt ledger says now: the work item, and how many of these it holds. */
  itemKey?: string;
  inItem?: number;
  rebuildError?: string;
  /**
   * Of the messages just linked, the ones the pipeline cannot see at all, with the
   * reason it gave. A different failure from `inItem` being short: there the link is
   * live and the clustering put the message elsewhere, here nothing was applied and
   * nothing ever will be until the cause is fixed.
   */
  unresolved?: Array<{ record_id: string; why: string }>;
  /** Where the reader can go and look, when we have somewhere to send them. */
  ticketUrl?: string;
  boardUrl?: string;
}

/** `2` → `'2 ข้อความ'`, but silent at one — a single message does not need counting. */
function count(n: number): string {
  return n > 1 ? `${n} ข้อความ` : 'ข้อความนี้';
}

/**
 * The result of a link, as lines a person can check one by one.
 *
 * A failed step is a line, not a thrown error: the local write may well have
 * succeeded while YouTrack refused, and reporting only the exception would lose the
 * half that worked — which is the half that changes what the next digest says.
 */
export function linkResultBlocks(result: LinkResult): KnownBlock[] {
  const blocks: KnownBlock[] = [
    section(`*🔗 ผูก${count(result.messages)}กับ ${esc(result.key)} แล้ว*`),
  ];

  const lines: string[] = [];

  if (result.overridesError) {
    lines.push(`✕ เขียนไฟล์ link override ไม่ได้ — ${esc(result.overridesError)}`);
  } else if (result.overridesFile) {
    lines.push(
      `✓ เขียนลง \`${esc(result.overridesFile)}\` แล้ว` +
        (result.overridesTotal ? ` (ทั้งไฟล์ ${result.overridesTotal} รายการ)` : '') +
        ' · linker ถือเป็น tier สูงสุด ไม่เดาทับ',
    );
  }

  if (result.commentId) {
    lines.push(
      `✓ เขียนคอมเมนต์ลง ${esc(result.key)} จริงแล้ว — comment id \`${esc(result.commentId)}\`` +
        ' (เปิด ticket แล้วหาไอดีนี้เจอ)',
    );
  } else if (result.commentError) {
    // Not an error banner: choosing not to write to a live tracker is a legitimate
    // configuration, and the person still got their link. The reason is verbatim so
    // whoever can change it knows exactly which switch is off.
    lines.push(`— ยังไม่ได้เขียนคอมเมนต์ลง ticket: ${esc(result.commentError)}`);
  }

  if (result.rebuildError) {
    lines.push(`— ยังไม่ได้ build ใหม่: ${esc(result.rebuildError)} · จะเห็นผลรอบถัดไป`);
  } else if (result.itemKey) {
    lines.push(
      `✓ build ใหม่แล้ว — งาน \`${esc(result.itemKey)}\` ตอนนี้มี ${result.inItem ?? 0} ` +
        `จาก ${result.messages} ข้อความนี้อยู่ในนั้น`,
    );
  } else if (result.inItem === 0) {
    // The honest and slightly awkward case: everything was written, the rebuild ran,
    // and the messages still are not in that work item. Saying so beats a green tick.
    lines.push(
      `— build ใหม่แล้ว แต่ยังไม่เห็นข้อความพวกนี้อยู่ใต้ ${esc(result.key)} ` +
        '(ticket นี้อาจยังไม่มีงานในหน้าต่างเวลาที่ digest ดูอยู่)',
    );
  }

  // Last, and phrased as a thing to fix rather than a thing that went wrong: the link
  // is in the file and will apply itself the moment the record exists. Until this was
  // reported, the pipeline logged it and the person here read '🔗 ผูกแล้ว'.
  for (const bad of result.unresolved ?? []) {
    lines.push(`✕ \`${esc(bad.record_id)}\` — ${esc(bad.why)} · การผูกถูกเก็บไว้แล้ว และจะมีผลเองเมื่อข้อความนี้เข้า corpus`);
  }

  blocks.push(section(lines.join('\n') || '_ไม่มีอะไรถูกเขียน_'));

  const buttons = [
    result.ticketUrl
      ? { type: 'button', text: { type: 'plain_text', text: 'เปิด ticket' }, url: result.ticketUrl }
      : undefined,
    result.boardUrl
      ? { type: 'button', text: { type: 'plain_text', text: 'ดูงานนี้ในบอร์ด' }, url: result.boardUrl }
      : undefined,
  ].filter(Boolean);
  if (buttons.length) blocks.push({ type: 'actions', elements: buttons } as KnownBlock);

  return blocks;
}

/** One ticket as a picker option: the key, its state, and enough title to recognise it. */
export function ticketOption(ticket: Ticket): {
  text: { type: 'plain_text'; text: string };
  value: string;
} {
  // Slack caps an option label at 75 characters and rejects the whole view if any
  // one of them is over — which is why this clamps rather than trusting summaries.
  const mark = ticket.resolved ? '✅' : '○';
  const state = ticket.state ? ` [${ticket.state}]` : '';
  const label = `${mark} ${ticket.key}${state} · ${ticket.summary}`;
  return {
    text: { type: 'plain_text', text: label.length > 75 ? `${label.slice(0, 74)}…` : label },
    value: ticket.key,
  };
}

/**
 * What the paste parser made of a chat, before any of it is stored.
 *
 * The browser path answers this with a preview page for a stated reason: the paste
 * format is a heuristic over something nobody documents, and a misread paste looks
 * exactly like a short conversation. A modal has no second screen, so the preview
 * comes back as a message and the person presses again — same discipline, different
 * surface.
 */
export function pastePreviewBlocks(input: {
  title: string;
  day: string;
  key: string;
  records: Array<{ user: string; when: string; text: string }>;
  skipped: string[];
  actionValue: string;
}): KnownBlock[] {
  const { title, day, key, records, skipped } = input;
  const shown = records.slice(0, 8);
  const blocks: KnownBlock[] = [
    section(
      `*📋 อ่านแชทที่วางได้ ${records.length} ข้อความ*\n` +
        `“${esc(title)}” · วันที่ ${esc(day)} · จะผูกเข้ากับ *${esc(key)}*`,
    ),
    section(
      shown
        .map((r) => `>*${esc(r.user)}* · ${esc(r.when)}\n>${esc(r.text.slice(0, 180)).replace(/\n/g, '\n>')}`)
        .join('\n'),
    ),
  ];
  if (records.length > shown.length) {
    blocks.push(context(`อีก ${records.length - shown.length} ข้อความไม่ได้แสดงที่นี่`));
  }
  if (skipped.length) {
    blocks.push(
      context(
        `ข้าม ${skipped.length} ก้อนที่ไม่รู้ว่าใครพูด — ถ้าอันไหนสำคัญ ให้ copy ใหม่โดยเริ่มที่ชื่อคนพูด`,
      ),
    );
  }
  blocks.push(
    divider(),
    {
      type: 'actions',
      elements: [
        {
          type: 'button',
          style: 'primary',
          text: { type: 'plain_text', text: `เก็บเข้า corpus แล้วผูกกับ ${key}` },
          action_id: 'paste_confirm',
          value: input.actionValue,
        },
        {
          type: 'button',
          text: { type: 'plain_text', text: 'ยกเลิก' },
          action_id: 'paste_cancel',
          value: 'cancel',
        },
      ],
    } as KnownBlock,
    context(
      'ยังไม่ได้เก็บอะไรเลยจนกว่าจะกดปุ่มซ้าย — ตัวอ่านแชทเป็น heuristic ' +
        'แชทที่อ่านผิดหน้าตาเหมือนแชทสั้น ๆ เลยต้องให้คนดูก่อน',
    ),
  );
  return blocks;
}
