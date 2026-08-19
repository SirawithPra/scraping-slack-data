/**
 * A morning that has already happened, so the thing being demonstrated is visible.
 *
 * The problem this solves is specific. Two of this bot's claims are *about time*:
 * "the same pending line, three mornings running" and "nobody has touched this work
 * item in five working days". Neither can be shown on a laptop that was set up
 * yesterday — there is no history for them to be true of, so the demo either shows
 * an empty section or somebody stands there asserting that it would fire eventually.
 *
 * So the history is written down. `seedPendingStreak` fills in the previous working
 * mornings with answers, one of which carries the same blocker every day from the
 * same person. Today's post then carries it forward as "ค้างมา N วัน" for real —
 * counted by the same `pendingStreaks` that counts a real week, from records in the
 * same file, and the escalation fires because the threshold is genuinely crossed.
 *
 * Every seeded answer is flagged `simulated`, every renderer says so, and
 * `clearSimulated` takes them all back out. The line this must not cross: a demo
 * that produces a screenshot indistinguishable from a real morning. Nothing here is
 * worth that, and this file is one flag away from being the thing the rest of the
 * codebase exists to delete.
 */

import type { DailyAnswer, DailyRecord } from './types.js';
import { isWeekend } from './stale.js';
import { readDailies, replaceDailies, saveDaily } from './store.js';

/** 'YYYY-MM-DD' for a Date, in the host's zone — the same shape `zonedNow` returns. */
function isoDate(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/**
 * The `count` working days before `today`, oldest first.
 *
 * Weekends are skipped for the same reason `pendingStreaks` counts dailies rather
 * than calendar days: a team that does not post on Saturday has not resolved
 * anything by Monday, and a seeded Saturday would be a morning that never existed.
 */
export function previousWorkdays(today: string, count: number): string[] {
  const start = new Date(`${today}T12:00:00`);
  const out: string[] = [];
  const cursor = new Date(start);
  while (out.length < count) {
    cursor.setDate(cursor.getDate() - 1);
    if (!isWeekend(cursor)) out.unshift(isoDate(cursor));
  }
  return out;
}

/**
 * The blocker that will not go away, written slightly differently each morning.
 *
 * Deliberately not copy-pasted text. `normaliseBlocker` drops mentions and a leading
 * "Pending" precisely so that a person who types the same obstacle three different
 * ways still counts as one streak, and a demo built from identical strings would
 * demonstrate string equality rather than the thing that was actually built.
 */
const RECURRING = [
  'Pending รอ requirement หน้า redemption จาก PO',
  'รอ requirement หน้า redemption จาก PO',
  'Pending: รอ requirement หน้า redemption จาก PO อยู่',
];

/** Ordinary blockers that clear the next day — the contrast that makes a streak mean something. */
const PASSING = [
  'Pending รอ merge PR #128',
  'Pending รอ staging deploy',
  'Pending รอ design ไฟล์ export',
];

const DONE = [
  'ปิด REVERAPP-247 แล้ว รอ QA verify',
  'review PR #128 ให้ทีม BE',
  'แก้ bug หน้า voucher list',
  'เขียน test ของ redemption flow',
];

const FOCUS = [
  'ต่อ REVERAPP-140 หน้า redemption',
  'ทำ REVERAPP-152 ต่อ',
  'ตาม requirement หน้า redemption',
  'เก็บงาน QA ที่ค้าง',
];

const pick = <T,>(list: T[], n: number): T => list[n % list.length] as T;

export interface SeedOptions {
  /** Today, 'YYYY-MM-DD'. The seeded mornings are the working days before it. */
  today: string;
  channel: string;
  /** Who answers. The first person is the one whose blocker repeats. */
  users: string[];
  /** How many mornings to write. One less than the threshold makes today the Nth. */
  mornings: number;
}

/**
 * Write the previous mornings, with one blocker running through all of them.
 *
 * Returns the dates written. Existing records for those dates are replaced, which
 * is what `saveDaily` does anyway — running the demo twice must not produce two
 * versions of Tuesday and make "the morning before this one" depend on read order.
 *
 * `ts` is synthetic and says so. A seeded answer has no Slack message behind it, so
 * the summary's "ข้อความ" link would point at nothing; the renderer checks for this
 * prefix and renders no link rather than a dead one.
 */
export function seedPendingStreak(opts: SeedOptions): string[] {
  const { today, channel, users, mornings } = opts;
  const roster = users.filter(Boolean);
  if (!roster.length) return [];
  const dates = previousWorkdays(today, Math.max(0, mornings));

  dates.forEach((date, index) => {
    const answers: DailyAnswer[] = roster.slice(0, 3).map((user, seat) => ({
      user,
      ts: `sim.${date}.${seat}`,
      done: [pick(DONE, index + seat)],
      focus: [pick(FOCUS, index + seat)],
      blockers:
        seat === 0
          ? [{ text: pick(RECURRING, index), tag: 'PO' }]
          : index % 2 === seat % 2
            ? [{ text: pick(PASSING, index + seat), tag: '' }]
            : [],
      simulated: true,
    }));
    const record: DailyRecord = {
      date,
      channel,
      // No parent post exists for a seeded morning. Empty rather than invented: a
      // fake `ts` here would send `conversations.replies` at a message that is not
      // there, and the error would look like Slack being broken.
      ts: '',
      posted_at: `${date} 09:00`,
      summarised_at: `${date} 10:45`,
      answers,
      simulated: true,
    };
    saveDaily(record);
  });
  return dates;
}

/**
 * Answers for today's thread, as if the team had already replied.
 *
 * Separate from `seedPendingStreak` because today is different: today's post is a
 * real Slack message in a real thread, so these are merged with whatever people
 * actually type rather than replacing it. The first person carries the recurring
 * line, which is what pushes the streak over the threshold.
 */
export function todaysSimulatedAnswers(users: string[], date: string): DailyAnswer[] {
  return users.filter(Boolean).slice(0, 3).map((user, seat) => ({
    user,
    ts: `sim.${date}.${seat}`,
    done: [pick(DONE, seat + 2)],
    focus: [pick(FOCUS, seat + 2)],
    blockers:
      seat === 0
        ? [{ text: RECURRING[0] as string, tag: 'PO' }]
        : seat === 1
          ? [{ text: 'Pending รอ BE เปิด endpoint /redeem ให้ก่อน', tag: '' }]
          : [],
    simulated: true,
  }));
}

/**
 * Whether the "(จำลอง)" labels are printed where the room can see them.
 *
 * Off by default, which is a deliberate reversal and worth being precise about.
 * Seeded rows are still flagged `simulated` in the file, `demo clear` still removes
 * exactly them, and the ephemeral replies only the presenter sees still say what was
 * seeded — none of that is behind this switch. What the switch hides is the label on
 * the *audience's* screen, so a demo of a morning looks like a morning.
 *
 * What that costs, stated plainly because the switch is one line and the cost is not:
 * with labels off, the summary and the daily thread attribute sentences to named
 * colleagues who did not write them, and nothing on screen says so. The presenter is
 * then the only thing standing between an audience and a wrong belief — which is
 * fine when the presenter says it out loud, and is not fine when a screenshot
 * travels. Say it out loud.
 *
 *   DEMO_SHOW_SIMULATED=1   labels on, the state this shipped in
 *   unset                   labels off
 */
export function showSimulatedLabels(): boolean {
  return (process.env.DEMO_SHOW_SIMULATED ?? '').trim() === '1';
}

/** True for a `ts` this file minted, so a renderer can decline to link it. */
export function isSimulatedTs(ts: string): boolean {
  return String(ts ?? '').startsWith('sim.');
}

/**
 * Take every seeded morning back out, and strip seeded answers from real ones.
 *
 * A demo that cannot be undone is a demo that quietly becomes the data. Days whose
 * whole record was seeded are dropped; a real day that had simulated answers merged
 * into it keeps the day and loses the answers.
 */
export function clearSimulated(): { days: number; answers: number } {
  const all = readDailies();
  let days = 0;
  let answers = 0;
  const kept: DailyRecord[] = [];
  for (const record of all) {
    // A seeded morning has no parent post; a real morning that had answers merged
    // into it keeps the day and loses only the answers.
    if (record.simulated && !record.ts) {
      days += 1;
      answers += record.answers.length;
      continue;
    }
    const real = record.answers.filter((a) => !a.simulated);
    if (real.length !== record.answers.length) {
      answers += record.answers.length - real.length;
      const { simulated, ...rest } = record;
      kept.push({ ...rest, answers: real });
    } else {
      kept.push(record);
    }
  }
  replaceDailies(kept);
  return { days, answers };
}
