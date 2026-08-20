/**
 * The daily thread, rendered.
 *
 * Four messages, and each one has a job the others cannot do:
 *
 *   dailyPostBlocks      the morning invitation, carrying yesterday's unfinished
 *                        business so nobody has to scroll for it
 *   dailyTemplateBlocks  the form, privately, to the one person who asked
 *   dailySummaryBlocks   what the thread produced, in the thread, at 10:45
 *   pendingEscalationBlocks   the one case that earns a channel-wide ping
 *
 * Tagging rule, applied in all four: a person is mentioned only where somebody
 * named them. `mentionOf` also declines to render a real `<@U…>` in pseudonym
 * mode, so a demo screenshot cannot leak a colleague's name through a ping.
 *
 * Every list here is capped. Slack rejects a message at 50 blocks, so an
 * uncapped daily means the busiest morning is the one that posts nothing — and
 * the overflow is stated on screen rather than dropped, because a reader cannot
 * tell a short list from a truncated one.
 */

import type { KnownBlock } from '@slack/types';

import type { DailyAnswer, DailyRecord, StandupDraft } from '../types.js';
import { DAILY_EXAMPLE, DAILY_TEMPLATE, composeDailyReply, formatDailyDate, type PendingStreak } from '../daily.js';
import { standupDone, standupPrefill } from '../standups.js';
import { isSimulatedTs, showSimulatedLabels } from '../demo.js';
import { mentionOf } from '../names.js';
import { CMD, clamp, context, divider, esc, header, section } from './common.js';

const MAX_CARRIED = 8;
const MAX_ANSWERS = 12;
const MAX_LINES = 4;

/** `Daily as of 20 August 2026` — the opening line the team asked for, verbatim. */
export function dailyTitle(date: string): string {
  return `Daily as of ${formatDailyDate(date)}`;
}

/** Who a pending line waits on, as a person, a role, or an honest blank. */
function waitingOn(tag: string): string {
  if (!tag) return '_ยังไม่ได้บอกว่ารอใคร_';
  if (tag.toUpperCase() === 'PO') return '*PO* (รอยืนยัน)';
  return mentionOf(tag);
}

/** One carried-over pending line: what it is, who it waits on, how long it has sat. */
function carriedLine(streak: PendingStreak): string {
  const seeded = streak.simulated && showSimulatedLabels() ? ' _(นับรวมวันที่จำลองไว้)_' : '';
  const age = streak.days >= 2 ? ` · _ค้างมา ${streak.days} วัน_${seeded}` : '';
  return `• ${esc(clamp(streak.text, 180))}\n   รอ: ${waitingOn(streak.tag)} · แจ้งโดย ${mentionOf(streak.user)}${age}`;
}

/**
 * The morning post.
 *
 * `previous` and `carried` are what make this more than a reminder: the thread
 * that answered yesterday is one click away, and yesterday's pending lines are
 * repeated *with their owners tagged* so the team reads the standing debt before
 * typing today's news over it.
 */
export function dailyPostBlocks(input: {
  date: string;
  previous?: DailyRecord;
  carried: PendingStreak[];
  summaryAt: string;
}): KnownBlock[] {
  const { date, previous, carried, summaryAt } = input;
  const blocks: KnownBlock[] = [
    header(dailyTitle(date)),
    section(
      `ตอบใน *เธรดของข้อความนี้* ได้เลยครับ — พิมพ์ \`${CMD} daily\` แล้วผมส่งฟอร์มให้ ` +
        '(เห็นคนเดียว) ก๊อปไปกรอกแล้ววางในเธรด',
    ),
    context(`ผมจะสรุปทุกคำตอบในเธรดนี้ตอน ${summaryAt} · ใครยังไม่ตอบจะขึ้นในสรุปด้วย`),
  ];

  if (previous) {
    const when = formatDailyDate(previous.date);
    const link = previous.permalink
      ? `<${previous.permalink}|daily ของ ${when}>`
      : `daily ของ ${when}`;
    const count = previous.answers.length;
    blocks.push(
      divider(),
      section(`*ของเมื่อวาน* → ${link}\n${count ? `มี ${count} คนตอบ` : 'ยังไม่มีใครตอบในเธรดนั้น'}`),
    );
  }

  if (carried.length) {
    blocks.push(
      section(
        `*ที่ยังค้างจากเมื่อวาน (${carried.length})*\n` +
          carried.slice(0, MAX_CARRIED).map(carriedLine).join('\n'),
      ),
    );
    if (carried.length > MAX_CARRIED) {
      blocks.push(context(`อีก ${carried.length - MAX_CARRIED} รายการไม่ได้แสดง — อยู่ในเธรดของเมื่อวาน`));
    }
  } else if (previous) {
    blocks.push(context('เมื่อวานไม่มีใครแจ้งว่าติดอะไรค้างไว้'));
  }

  return blocks;
}

/**
 * The form, to one person, visible to nobody else.
 *
 * Two code blocks on purpose. The bracketed template is the shape the parser
 * reads; the filled example is there because a template handed out with brackets
 * gets pasted back with brackets, and a line still reading `#[ID]` is counted as
 * "did not fill it in" rather than as work done.
 *
 * When the pipeline already knows what this person touched, a third block offers
 * it prefilled — the same inversion the 08:45 DM makes: correcting beats recalling.
 */
export function dailyTemplateBlocks(draft?: StandupDraft): KnownBlock[] {
  const blocks: KnownBlock[] = [
    section('*ฟอร์ม daily* — ก๊อปไปวางในเธรดของโพสต์ daily วันนี้'),
    section('```' + DAILY_TEMPLATE + '```'),
    section('*ตัวอย่างที่กรอกแล้ว*'),
    section('```' + DAILY_EXAMPLE + '```'),
  ];

  if (draft && (draft.yesterday.length || draft.carried_over.length)) {
    // Built by the same two functions that fill the 08:45 DM's three boxes, not by a
    // second list-and-slice written here. This block and that card are meant to be
    // the same form offered two ways; two pieces of code deciding what "prefilled"
    // means is how they stopped being that, one heading and one cap at a time.
    const prefill = standupPrefill(draft);
    const prefilled = composeDailyReply({
      done: standupDone(draft),
      focus: prefill.today ? [prefill.today] : [],
      blockers: prefill.blocker ? [prefill.blocker] : [],
      emptyAs: '-',
    });
    blocks.push(
      section('*หรือเริ่มจากที่ผมเห็นในข้อมูลของคุณ* — แก้ให้ถูกแล้ววางได้เลย'),
      section('```' + prefilled + '```'),
      context('มาจาก Slack + ticket ที่คุณมีชื่ออยู่ — *ไม่ใช่คำพูดของคุณ* ตรวจก่อนวางครับ'),
      context(
        'อันนี้คือของเดียวกับที่อยู่ในสามช่องของ standup DM ตอน 08:45 — ' +
          'ถ้าเช้านี้กด *ส่ง* ในการ์ดนั้นไปแล้ว ถือว่าตอบแล้ว ไม่ต้องวางซ้ำที่นี่',
      ),
    );
  }

  blocks.push(
    context(
      'บรรทัดใน *Blockers / Pending* ให้แท็กคนที่ต้องทำให้ก่อน (`@ชื่อ`) หรือพิมพ์ `PO` ถ้ารอ PO ยืนยัน · ' +
        'ไม่มีอะไรติดก็ใส่ `-` — “ตอบว่าไม่มี” ต่างจาก “ไม่ได้ตอบ”',
    ),
  );
  return blocks;
}

/** One person's answer, folded to the lines that matter in a summary. */
function answerLines(answer: DailyAnswer, opts: { permalinkBase?: string; hideVia?: boolean } = {}): string {
  const { permalinkBase } = opts;
  // An answer with no Slack message behind it gets no link — a permalink built from
  // `sim.2026-08-18.0`, or from the empty ts of a DM answer, would render as a button
  // that goes nowhere, which is worse than no button.
  const link = permalinkBase && answer.ts && !isSimulatedTs(answer.ts)
    ? `<${permalinkBase}${answer.ts.replace('.', '')}|ข้อความ>`
    : '';
  // Not behind `DEMO_SHOW_SIMULATED`: this one is never hidden. It is a true thing
  // about a real answer — this person pressed ส่ง on boxes the bot filled in, in
  // private, instead of writing in the thread — and it is also the honest reason
  // there is no message to click through to.
  const via = answer.via === 'dm' && !opts.hideVia ? ' _(ตอบจาก standup DM)_' : '';
  const tag = (answer.simulated && showSimulatedLabels() ? ' _(จำลอง)_' : '') + via;
  const parts: string[] = [`*${mentionOf(answer.user)}*${tag}${link ? ` · ${link}` : ''}`];
  if (answer.done.length) parts.push(`เสร็จ: ${answer.done.slice(0, MAX_LINES).map((t) => esc(clamp(t, 120))).join(' · ')}`);
  if (answer.focus.length) parts.push(`วันนี้: ${answer.focus.slice(0, MAX_LINES).map((t) => esc(clamp(t, 120))).join(' · ')}`);
  for (const b of answer.blockers.slice(0, MAX_LINES)) {
    parts.push(`⛔ ${esc(clamp(b.text, 160))} — รอ ${waitingOn(b.tag)}`);
  }
  return parts.join('\n');
}

/**
 * A DM answer, put where colleagues can read it.
 *
 * Without this the DM is a private confession. The daily is a thread precisely
 * because the value of a standup is that the team sees it, and an answer that only
 * exists in `dailies.json` until 10:45 leaves the morning looking empty to everyone
 * who scrolls the channel at 09:10 — which is most of the reason somebody would then
 * type it again in the thread, the duplicate work this whole route removes.
 *
 * Posted by the bot, attributed, never as the person. The bot cannot post as a
 * colleague and must not try: a message that looks like it came from a real person is
 * a forgery whatever the intent. So it reads as a report *about* their answer, and
 * whoever wants their own words in the thread can still type them — those win at 10:45.
 */
export function dmAnswerBlocks(answers: DailyAnswer[]): KnownBlock[] {
  const shown = answers.slice(0, MAX_ANSWERS);
  return [
    section(
      `*ตอบมาทาง standup DM${answers.length > 1 ? ` · ${answers.length} คน` : ''}*\n` +
        // `hideVia`: the heading above already says where these came from, and
        // repeating it on every line reads as a warning rather than a source note.
        shown.map((a) => answerLines(a, { hideVia: true })).join('\n\n'),
    ),
    context(
      answers.length > shown.length
        ? `อีก ${answers.length - shown.length} คนอยู่ในสรุปตอนสาย · เพิ่มหรือแก้ได้ด้วยการพิมพ์ในเธรดนี้ ของในเธรดชนะเสมอ`
        : 'เพิ่มหรือแก้ได้ด้วยการพิมพ์ในเธรดนี้ — ของที่พิมพ์ในเธรดชนะของที่ส่งมาทาง DM เสมอ',
    ),
  ];
}

/**
 * The 10:45 summary, posted into the same thread it read.
 *
 * `expected` is the configured roster, so "who has not answered" is a list of
 * people the bot was told to expect — never everyone in the channel, which would
 * turn a standup into a public attendance sheet for people who never opted in.
 * Missing names are stated plainly and not tagged: a nudge every morning to
 * everyone who was in a meeting is how a bot gets muted.
 */
export function dailySummaryBlocks(input: {
  record: DailyRecord;
  expected: string[];
  unfilled: string[];
  streaks: PendingStreak[];
  pendingDays: number;
}): KnownBlock[] {
  const { record, expected, unfilled, streaks, pendingDays } = input;
  const who = new Set(record.answers.map((a) => a.user));
  const missing = expected.filter((u) => !who.has(u));
  const blockers = record.answers.flatMap((a) => a.blockers);

  const simulated = record.answers.filter((a) => a.simulated).length;
  const blocks: KnownBlock[] = [
    section(`*สรุป ${dailyTitle(record.date)}*\n${record.answers.length} คนตอบ · ${blockers.length} เรื่องที่ติด/รออยู่`),
  ];

  // Said at the top, not in a footnote. The whole value of this summary is that the
  // reader can trust it describes a morning that happened; a seeded one that reads
  // identically to a real one would spend that trust to make a demo look better.
  if (simulated && showSimulatedLabels()) {
    blocks.push(
      context(
        `⚠️ ${simulated} จาก ${record.answers.length} คำตอบนี้เป็นข้อมูลจำลองของเดโม ไม่ใช่คำตอบจริงของคน — ` +
          'ลบออกได้ด้วย `/meowtam demo clear`',
      ),
    );
  }

  if (!record.answers.length) {
    blocks.push(section('_ยังไม่มีใครตอบในเธรดนี้_ — ผมไม่ได้สรุปอะไรขึ้นมาเอง'));
  }

  for (const answer of record.answers.slice(0, MAX_ANSWERS)) {
    blocks.push(section(answerLines(answer)));
  }
  if (record.answers.length > MAX_ANSWERS) {
    blocks.push(context(`อีก ${record.answers.length - MAX_ANSWERS} คนไม่ได้แสดงที่นี่ — อ่านในเธรดได้`));
  }

  const stuck = streaks.filter((s) => s.days >= pendingDays);
  if (stuck.length) {
    blocks.push(
      divider(),
      section(
        `*ค้างเกิน ${pendingDays} วัน (${stuck.length})*\n` +
          stuck.slice(0, MAX_CARRIED).map(carriedLine).join('\n'),
      ),
    );
  }

  if (missing.length || unfilled.length) {
    const lines: string[] = [];
    if (missing.length) lines.push(`ยังไม่ได้ตอบ: ${missing.map((u) => esc(mentionOf(u))).join(', ')}`);
    if (unfilled.length) lines.push(`วางฟอร์มมาแต่ยังไม่ได้กรอก: ${unfilled.map((u) => esc(mentionOf(u))).join(', ')}`);
    blocks.push(context(lines.join(' · ')));
  }

  return blocks;
}

/**
 * The escalation, and the only message here that pings the channel.
 *
 * It fires on a measured fact — the same line, from the same person, in N
 * dailies running — rather than on a guess about importance. That threshold is
 * why it can tag somebody without becoming noise: if it is wrong, the person can
 * point at the three mornings it came from.
 */
export function pendingEscalationBlocks(streaks: PendingStreak[], pendingDays: number): KnownBlock[] {
  return [
    section(
      `*ค้างมา ${pendingDays} วันขึ้นไป และยังไม่ขยับ*\n` +
        streaks.slice(0, MAX_CARRIED).map(carriedLine).join('\n'),
    ),
    context(
      `นับจากบรรทัดเดิมที่ถูกแจ้งซ้ำใน daily ${pendingDays} รอบติดกัน (ตั้งแต่ ${
        streaks[0]?.since ?? '-'
      }) · ถ้าเคลียร์แล้ว ไม่ต้องใส่ในรอบถัดไปแล้วอันนี้จะหายไปเอง`,
    ),
  ];
}
