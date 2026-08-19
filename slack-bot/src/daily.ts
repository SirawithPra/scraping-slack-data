/**
 * The daily thread: one post each morning, answers in its thread, one summary
 * at 10:45, and yesterday's unfinished business carried into tomorrow's post.
 *
 * Why this is a thread and not a form
 * ----------------------------------
 * The team already types standups into Slack by hand. A modal would move that
 * typing into a place where nobody else can read it, and the value of a standup
 * is that colleagues see it. So the bot posts the invitation, people reply in
 * the thread as they always have, and the parsing happens on what they wrote.
 *
 * Why the answers are re-read instead of listened for
 * --------------------------------------------------
 * Collecting replies with `conversations.replies` at 10:45 needs only the
 * `channels:history` scope the app already has. Subscribing to `message.channels`
 * would need a manifest change, a reinstall, and a bot that receives every
 * message in every channel it sits in — a large permission for a small feature.
 * Re-reading one thread once a morning gets the same answers.
 *
 * What this file does NOT do
 * -------------------------
 * It never rewrites what somebody typed. A "Blockers / Pending" line is stored
 * verbatim because tomorrow's post quotes it back at the channel, and a bot that
 * paraphrases a person's blocker and then tags them for it is putting words in
 * their mouth.
 */

import type { DailyAnswer, DailyBlocker, DailyRecord } from './types.js';

/* ------------------------------------------------------------------ *
 * the clock
 * ------------------------------------------------------------------ */

/**
 * 'YYYY-MM-DD', hour and minute in `tz` — or the host's clock when tz is empty.
 *
 * One clock for the whole bot: the schedules fire off it and the daily record is
 * keyed by its date, so a 10:45 pass and the record it looks for cannot disagree
 * about which day it is.
 */
export function zonedNow(tz = '', now = new Date()): { date: string; hour: number; minute: number } {
  if (!tz) {
    const p = (n: number) => String(n).padStart(2, '0');
    return {
      date: `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`,
      hour: now.getHours(),
      minute: now.getMinutes(),
    };
  }
  // en-CA gives ISO-ordered date parts, which is the cheapest way to get
  // 'YYYY-MM-DD' in another zone without pulling in a date library.
  const [date, time] = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz,
    hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
    .format(now)
    .split(', ');
  const [hh, mm] = (time ?? '00:00').split(':');
  return { date: date ?? '', hour: Number(hh), minute: Number(mm) };
}

/**
 * 'HH:MM' from the environment, with a stated fallback.
 *
 * Invalid input falls back and says so rather than scheduling something at
 * NaN:NaN, which would simply never fire and never explain itself.
 */
export function parseHhMm(value: string, fallback: string, label = 'เวลา'): { hour: number; minute: number } {
  const match = /^(\d{1,2}):(\d{2})$/.exec(value.trim());
  const [h, m] = match ? [Number(match[1]), Number(match[2])] : [-1, -1];
  if (h >= 0 && h <= 23 && m >= 0 && m <= 59) return { hour: h, minute: m };
  if (value.trim()) console.error(`⚠  ${label} "${value}" อ่านไม่ออก (ต้องเป็น HH:MM) — ใช้ ${fallback} แทน`);
  const [fh, fm] = fallback.split(':');
  return { hour: Number(fh), minute: Number(fm) };
}

/** '2026-08-20' → '20 August 2026', the wording the daily post opens with. */
export function formatDailyDate(date: string): string {
  // Noon UTC, so no timezone can move the day across a boundary while formatting.
  const d = new Date(`${date}T12:00:00Z`);
  if (Number.isNaN(d.getTime())) return date;
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: 'UTC', day: 'numeric', month: 'long', year: 'numeric',
  }).format(d);
}

/* ------------------------------------------------------------------ *
 * the template
 * ------------------------------------------------------------------ */

/**
 * The three headings, in the order the parser and the copy both use.
 *
 * Kept as one exported string so `/meowtam daily` hands out exactly the shape
 * `parseDailyReply` reads. The two drifting apart is the failure mode that makes
 * a parser look broken when the instructions were simply out of date.
 */
export const DAILY_TEMPLATE = [
  'Done Yesterday:',
  'Finished ticket #[ID]',
  'Code review for PR #[ID]',
  '',
  'Focus Today:',
  'Focus ticket #[ID]',
  'Fix bug in [Module Name]',
  '',
  'Blockers / Pending:',
  'Pending [เรื่องที่รออยู่] [@คนที่ต้องทำให้ก่อน หรือพิมพ์ PO ถ้ารอ PO ยืนยัน]',
].join('\n');

/** A filled-in example, because a template with brackets still gets pasted with brackets. */
export const DAILY_EXAMPLE = [
  'Done Yesterday:',
  'ปิด REVERAPP-247 แล้ว รอ QA',
  'review PR #128',
  '',
  'Focus Today:',
  'ต่อ REVERAPP-140 หน้า redemption',
  '',
  'Blockers / Pending:',
  'Pending รอ requirement หน้า redemption @jah',
  'Pending รอยืนยัน scope ของ voucher PO',
].join('\n');

/* ------------------------------------------------------------------ *
 * the parser
 * ------------------------------------------------------------------ */

type Section = 'done' | 'focus' | 'blockers';

/** Headings people actually type, including the Thai ones, without a colon. */
const EXACT_HEADINGS: Record<string, Section> = {
  'done': 'done',
  'done yesterday': 'done',
  'yesterday': 'done',
  'เมื่อวาน': 'done',
  'เมื่อวานทำอะไร': 'done',
  'focus': 'focus',
  'focus today': 'focus',
  'today': 'focus',
  'วันนี้': 'focus',
  'วันนี้ทำอะไร': 'focus',
  'blockers': 'blockers',
  'blocker': 'blockers',
  'pending': 'blockers',
  'blockers pending': 'blockers',
  'ติดอะไร': 'blockers',
  'ที่ติด': 'blockers',
};

/**
 * Which section this line opens, if any.
 *
 * The subtle case is `Pending รอ requirement …`, an *answer* whose first word is
 * also a heading word. So a heading is either one of the exact phrases above, or
 * a line that ends in a colon. Without that rule every pending line silently
 * became an empty heading and the blockers section always read as empty — the
 * one section the whole feature exists for.
 */
export function headingOf(raw: string): Section | undefined {
  const stripped = raw.trim().replace(/^[*_~>•\-\d.\s]+/, '').replace(/[*_~`]/g, '');
  const endsWithColon = /:\s*$/.test(stripped);
  const words = stripped
    .replace(/:\s*$/, '')
    .replace(/\s*\/\s*/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
  if (!words) return undefined;
  const exact = EXACT_HEADINGS[words];
  if (exact) return exact;
  if (!endsWithColon) return undefined;
  if (words.startsWith('done')) return 'done';
  if (words.startsWith('focus')) return 'focus';
  if (words.startsWith('blocker') || words.startsWith('pending')) return 'blockers';
  return undefined;
}

/** '-', 'none', 'ไม่มี' — an answer of "nothing", which is not the same as no answer. */
const NOTHING = /^(-{1,3}|–|—|\.|none|n\/a|na|no|nope|nothing|ไม่มี|ไม่มีครับ|ไม่มีค่ะ|เคลียร์|clear)$/i;

/**
 * A line still carrying the template's own brackets, or its `xxxx`.
 *
 * Counted separately from a filled answer: somebody who pastes the template and
 * posts it unchanged has not reported anything, and the summary saying they did
 * would be the fake-confirmation bug in a new place.
 */
export function isPlaceholder(line: string): boolean {
  const t = line.trim();
  if (!t) return true;
  if (/\bx{3,}\b/i.test(t)) return true;
  // A bracketed span that is still one of the template's own hints.
  return /\[(\s*)(id|finished|module name|date month year|เรื่องที่รออยู่|@คนที่ต้องทำให้ก่อน[^\]]*|tag[^\]]*)(\s*)\]/i.test(t);
}

/** Strip the bullet, keep the words. */
function unbullet(line: string): string {
  return line.trim().replace(/^[*_~>•\-–—\d).\s]+/, '').trim();
}

/**
 * Who a pending line waits on: a mention, or the literal `PO`.
 *
 * Only these two, and nothing inferred. "Pending รอ requirement" with no name is
 * stored with an empty tag and rendered as "ยังไม่ได้ระบุว่ารอใคร" — which is a
 * true statement about the line, where guessing an owner would not be.
 */
export function tagOf(line: string): string {
  const mention = /<@([UW][A-Z0-9]{2,})(?:\|[^>]*)?>/.exec(line);
  if (mention) return mention[1] ?? '';
  if (/(^|[^A-Za-z])PO([^A-Za-z]|$)/.test(line)) return 'PO';
  return '';
}

export type DailyParse =
  | { kind: 'answer'; answer: DailyAnswer }
  /** Headings present, every line still the template. Reported, not counted. */
  | { kind: 'unfilled' }
  /** Ordinary thread chatter. Not everything in the thread is a standup. */
  | { kind: 'other' };

/**
 * Read one thread reply as a daily answer.
 *
 * Tolerant on purpose: bullets optional, headings in either language, colons
 * optional on the known phrases, and any order. The one thing it will not do is
 * invent a section — a reply with no heading at all is chatter, and counting it
 * as an answer would make "who answered" meaningless.
 */
export function parseDailyReply(text: string, user: string, ts: string): DailyParse {
  const lines = String(text ?? '').split(/\r?\n/);
  const done: string[] = [];
  const focus: string[] = [];
  const blockers: DailyBlocker[] = [];

  let section: Section | undefined;
  let sawHeading = false;
  let sawPlaceholder = false;

  for (const raw of lines) {
    const heading = headingOf(raw);
    if (heading) {
      section = heading;
      sawHeading = true;
      continue;
    }
    if (!section) continue; // preamble such as "สรุปของผมครับ"
    const line = unbullet(raw);
    if (!line) continue;
    if (NOTHING.test(line)) continue; // an explicit "nothing", already counted by sawHeading
    if (isPlaceholder(line)) {
      sawPlaceholder = true;
      continue;
    }
    if (section === 'done') done.push(line);
    else if (section === 'focus') focus.push(line);
    else blockers.push({ text: line, tag: tagOf(raw) });
  }

  if (!sawHeading) return { kind: 'other' };
  if (!done.length && !focus.length && !blockers.length && sawPlaceholder) return { kind: 'unfilled' };
  return { kind: 'answer', answer: { user, ts, done, focus, blockers } };
}

/* ------------------------------------------------------------------ *
 * pending that will not go away
 * ------------------------------------------------------------------ */

/**
 * The comparable form of a pending line.
 *
 * Mentions and a leading "pending" are dropped so the same obstacle written
 * "Pending รอ requirement @jah" one day and "รอ requirement" the next still
 * counts as the same thing. Everything else is kept: two different waits on the
 * same person are two different blockers.
 */
export function normaliseBlocker(text: string): string {
  return String(text ?? '')
    .replace(/<@[UW][A-Z0-9]{2,}(?:\|[^>]*)?>/g, ' ')
    .toLowerCase()
    .replace(/^\s*pending\b[:\s]*/i, ' ')
    .replace(/[#*_~`"'’“”(),.:;!?\-–—]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export interface PendingStreak {
  user: string;
  /** The line as last typed, so the announcement quotes the person, not a summary. */
  text: string;
  tag: string;
  /** Consecutive dailies this has appeared in, counting the newest one. */
  days: number;
  /** The date of the oldest daily in that run. */
  since: string;
  /**
   * True when any morning in the run was written by the demo driver.
   *
   * The count is real — the same function counts it either way — but what it is a
   * count *of* is different, and a card that says "ค้างมา 3 วัน" without this can
   * be screenshotted as evidence of three mornings that never happened.
   */
  simulated?: boolean;
}

/**
 * Pending lines that keep coming back, longest run first.
 *
 * Counted in *dailies*, not calendar days: a team that skips the weekend has not
 * resolved anything by Monday, and resetting the count because Saturday had no
 * post would quietly forgive exactly the item worth chasing. Days whose thread
 * was never collected are skipped for the same reason — no answers is missing
 * data, not a cleared blocker.
 */
export function pendingStreaks(records: DailyRecord[]): PendingStreak[] {
  const collected = records.filter((r) => r.summarised_at || r.answers.length);
  const latest = collected[collected.length - 1];
  if (!latest) return [];

  const streaks: PendingStreak[] = [];
  for (const answer of latest.answers) {
    for (const blocker of answer.blockers) {
      const key = normaliseBlocker(blocker.text);
      if (!key) continue;
      let days = 1;
      let since = latest.date;
      let simulated = Boolean(answer.simulated ?? latest.simulated);
      for (let i = collected.length - 2; i >= 0; i--) {
        const day = collected[i]!;
        const match = day.answers.find(
          (a) => a.user === answer.user && a.blockers.some((b) => normaliseBlocker(b.text) === key),
        );
        if (!match) break;
        days += 1;
        since = day.date;
        simulated ||= Boolean(match.simulated ?? day.simulated);
      }
      streaks.push({ user: answer.user, text: blocker.text, tag: blocker.tag, days, since, simulated });
    }
  }
  return streaks.sort((a, b) => b.days - a.days || a.user.localeCompare(b.user));
}

/** The key an announcement is remembered by: this person, this obstacle. */
export function streakKey(streak: { user: string; text: string }): string {
  return `${streak.user}::${normaliseBlocker(streak.text)}`;
}

/**
 * Streaks that have crossed the threshold and have not been announced before.
 *
 * Not simply `days >= pendingDays`: that set only grows, so the same line would be
 * announced with the same person tagged every morning until they closed it. And not
 * `days === pendingDays` either — a morning whose thread was never collected makes
 * a streak jump from 2 to 4 and skip the equality, quietly never announcing the
 * item that sat longest. Remembering what was announced handles both.
 */
export function newEscalations(
  records: DailyRecord[],
  streaks: PendingStreak[],
  pendingDays: number,
): PendingStreak[] {
  const already = new Set(records.flatMap((r) => r.announced ?? []));
  return streaks.filter((s) => s.days >= pendingDays && !already.has(streakKey(s)));
}

/** Everyone who replied to a daily, in the order they first answered. */
export function answered(record: DailyRecord | undefined): string[] {
  return [...new Set((record?.answers ?? []).map((a) => a.user))];
}
