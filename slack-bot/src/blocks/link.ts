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

import { context, divider, esc, fromMarkdown, section, sourceIcon } from './common.js';
import { SOURCE_TEXT } from '../comment.js';
import type { Source } from '../types.js';
import type { Ticket } from '../youtrack.js';

/** What one link attempt did, field by field, so nothing is claimed that did not happen. */
export interface LinkResult {
  /**
   * The ticket everything on this screen was attached to.
   *
   * Empty when a pasted chat was kept without one — the lake case. Nothing is written
   * to a tracker and no override exists for it, so every line below that names a
   * ticket has to be skipped rather than rendered with a blank where the key goes.
   */
  key: string;
  /** How many messages were attached. One for a shortcut, many for a pasted chat. */
  messages: number;
  /** The corrections file, after the write. Empty when the write itself failed. */
  overridesFile?: string;
  overridesTotal?: number;
  overridesError?: string;
  /** The comment YouTrack stored, if one was asked for and accepted. */
  commentId?: string;
  /** The comment's own anchor, so 'เปิดคอมเมนต์' opens the comment and not the ticket. */
  commentUrl?: string;
  /**
   * The comment body as it was sent. Rendered back on screen because "comment id
   * `7-729`" answers a question nobody asked — the reader wants to know what the bot
   * said on the ticket under their name, and the only honest way to tell them is to
   * show it.
   */
  commentBody?: string;
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
  /**
   * What conversation this was and where it came from.
   *
   * The report named the ticket and counted the messages and said nothing about which
   * chat, from which day, of which kind — so two links to the same ticket produced two
   * identical screens. `sourceType` is the distinction that matters most: a message the
   * bot read in a channel it can see has a permalink behind it, and a closed chat
   * somebody pasted has none and is only as complete as what they highlighted.
   */
  title?: string;
  day?: string;
  sourceType?: Source;
  /** When the write happened, so the line is a record and not just an assertion. */
  at?: string;
  /** The corpus after the write, for the case where the corpus is the only thing written. */
  corpusSize?: number;
  /**
   * Where the *linker's own guess* put these messages, when nobody chose a ticket.
   *
   * Read back from the rebuilt ledger, one entry per work item that ended up holding
   * some of them. It is deliberately not rendered as a link that was made: a guessed
   * tier loses to an override, and telling somebody their chat is "on REVERAPP-162"
   * when the next rebuild may move it is the kind of claim this screen exists to stop.
   */
  placed?: Array<{ key: string; count: number }>;
}

/**
 * `2` → `' 2 ข้อความ '`, but silent at one — a single message does not need counting.
 *
 * The spaces are part of the value, not sloppiness. Thai has no word spaces, so the
 * header read `ผูก3 ข้อความกับ REVERAPP-251` with the digit welded to the verb; a
 * numeral inside Thai prose needs breathing room on both sides, and the one-message
 * form needs none because there is no numeral.
 */
function count(n: number): string {
  return n > 1 ? ` ${n} ข้อความ ` : 'ข้อความนี้';
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
    section(
      result.key
        ? `*🔗 ผูก${count(result.messages)}กับ ${esc(result.key)} แล้ว*`
        : `*🗂 เก็บ${count(result.messages)}เข้า corpus แล้ว — ยังไม่ผูก ticket*`,
    ),
  ];

  // What was linked, before what happened to it. Named first because it is the half a
  // reader can verify from memory — they were in that chat five minutes ago.
  const src = result.sourceType ? SOURCE_TEXT[result.sourceType] : undefined;
  const what = [
    src ? `${sourceIcon(result.sourceType as string)} ${esc(src.label)}` : undefined,
    result.title ? `“${esc(result.title)}”` : undefined,
    result.day ? `คุยกันวันที่ ${esc(result.day)}` : undefined,
  ].filter(Boolean);
  if (what.length) blocks.push(context(what.join('  ·  ')));

  const lines: string[] = [];

  if (!result.key) {
    // The whole truth about this write, including the two things that did not happen:
    // there is no override to beat the linker with and no comment on any ticket. Both
    // are what somebody choosing to skip the picker is choosing, and they should read
    // it here rather than discover it when nobody on the ticket has seen the chat.
    lines.push(
      '✓ เก็บเข้า corpus แล้ว' +
        (result.corpusSize ? ` (ทั้ง corpus ${result.corpusSize} ข้อความ)` : '') +
        ' · ไม่มี link override และไม่มีคอมเมนต์ไปที่ ticket ไหน เพราะยังไม่ได้เลือก',
    );
  } else if (result.overridesError) {
    lines.push(`✕ เขียนไฟล์ link override ไม่ได้ — ${esc(result.overridesError)}`);
  } else if (result.overridesFile) {
    lines.push(
      `✓ เขียนลง \`${esc(result.overridesFile)}\` แล้ว` +
        (result.overridesTotal ? ` (ทั้งไฟล์ ${result.overridesTotal} รายการ)` : '') +
        ' · linker ถือเป็น tier สูงสุด ไม่เดาทับ',
    );
  }

  if (result.commentId) {
    // The id stays — it is the receipt — but it is no longer the whole claim, and it is
    // no longer something the reader has to go hunting for: `commentUrl` anchors it.
    lines.push(
      `✓ เขียนคอมเมนต์ลง ${esc(result.key)} จริงแล้ว` +
        (result.at ? ` เมื่อ ${esc(result.at)}` : '') +
        ` — comment id \`${esc(result.commentId)}\``,
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
  } else if (!result.key) {
    for (const p of result.placed ?? []) {
      lines.push(
        `— build ใหม่แล้ว · linker เดาเอาเองว่า ${p.count} ข้อความเข้ากับ \`${esc(p.key)}\` ` +
          '(ชั้นเดา ไม่ใช่คนสั่ง — ผูกทับได้ทีหลัง)',
      );
    }
    const loose = result.messages - (result.placed ?? []).reduce((n, p) => n + p.count, 0);
    if (loose > 0) {
      // Not "the digest will count them": the bot's ledger carries no unassigned list
      // when it reads from the pipeline, so those messages appear on no card at all.
      // `recall` searches the corpus itself, which is why it is the honest answer here.
      lines.push(
        `— อีก ${loose} ข้อความไม่ได้อยู่ใต้งานไหน · จะไม่ขึ้นในการ์ดหรือ digest ` +
          'แต่ `recall` ค้นเจอ เพราะมันค้นทั้ง corpus',
      );
    }
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

  // Said on the screen that created it, not in a manual: there is no button anywhere
  // that takes a kept-but-unlinked chat and puts it on a ticket. The way out is to
  // paste it again with a ticket chosen, and the ids are content hashes, so the same
  // chat under the same title replaces itself instead of arriving twice.
  if (!result.key) {
    blocks.push(
      context(
        'ยังไม่มีปุ่มผูกทีหลังนะครับ — ถ้าจะให้ไปโผล่บน ticket ให้วางแชทเดิมอีกครั้ง ' +
          'ใช้ชื่อแชทกับวันเดิม แล้วเลือก ticket · ข้อความเดิมถูกทับด้วย id เดิม ไม่เพิ่มซ้ำ',
      ),
    );
  }

  // The comment itself, quoted. This is the answer to "อ่านไม่รู้เรื่องว่าเขียนไปว่าอะไร":
  // whatever else the report claims, this is the text now sitting on the ticket under
  // the reader's name, and they can see it without leaving Slack.
  if (result.commentId && result.commentBody) {
    blocks.push(divider());
    blocks.push(section(`*ข้อความที่เขียนลง ${esc(result.key)}*`));
    // Not prefixed with `>`. The comment body already quotes the chat it carries, and a
    // second layer of quoting renders as a literal `>` in front of every speaker.
    blocks.push(section(fromMarkdown(result.commentBody, 2400)));
  }

  const buttons = [
    result.commentUrl
      ? {
          type: 'button',
          style: 'primary',
          text: { type: 'plain_text', text: 'เปิดคอมเมนต์ที่เพิ่งเขียน' },
          url: result.commentUrl,
        }
      : undefined,
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
  /** The chosen ticket, or empty for a chat being kept without one. */
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
        `“${esc(title)}” · วันที่ ${esc(day)} · ` +
        (key
          ? `จะผูกเข้ากับ *${esc(key)}*`
          : 'จะเก็บไว้เฉย ๆ *ยังไม่ผูก ticket* — ค้นด้วย `recall` เจอ แต่ไม่มีคอมเมนต์ไปโผล่บน ticket ไหน') +
        '\n' +
        // Said before storing, not after: the person is about to put a private
        // conversation somewhere other people read, and the kind of record it becomes is
        // part of what they are agreeing to.
        `เก็บเป็นประเภท *${esc(SOURCE_TEXT.slack_paste.label)}* — ` +
        'แยกจากข้อความในห้องที่บอทอ่านเองได้ เพราะแชทนี้ไม่มีลิงก์ Slack ให้กดกลับไปดู',
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
          text: {
            type: 'plain_text',
            text: key ? `เก็บเข้า corpus แล้วผูกกับ ${key}` : 'เก็บเข้า corpus (ยังไม่ผูก ticket)',
          },
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
