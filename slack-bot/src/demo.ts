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
  'Pending: *รอ requirement หน้า redemption จาก PO*',
];

/**
 * What the matcher can and cannot absorb, which decides what may go in that list.
 *
 * `normaliseBlocker` drops mentions, a leading "Pending", punctuation and case. It
 * does not know synonyms and does not tolerate an extra word: the previous third
 * variant ended "…จาก PO อยู่" and normalised to a different key, so a run of four
 * mornings or more quietly counted two streaks of one instead of one of three — the
 * demo's own claim, broken by the demo's own data, in the branch nobody rehearses
 * (`TAM_DAILY_PENDING_DAYS` above 3). The variants now differ only in decoration,
 * which is exactly what the matcher handles, and `demo.test.ts` pins that they share
 * a key so the next line added here cannot reintroduce it.
 */

/**
 * The seeded mornings, written out day by day instead of cycled from four lists.
 *
 * The cycled version put nearly the same two sentences under every name on every
 * morning, and a reader watching three identical mornings scroll past cannot tell
 * which repetition is the point. The whole claim rests on one line coming back while
 * everything around it moves, so everything around it has to move: seat 0 ships
 * something different each day and keeps waiting on the same requirement, seat 1
 * raises a blocker on Monday and clears it on Tuesday — the contrast that makes a
 * streak mean anything — and seat 2 never blocks at all.
 *
 * Newest last. `seedPendingStreak` takes the last N entries before today, so raising
 * `TAM_DAILY_PENDING_DAYS` reaches further back into the script rather than repeating
 * the same morning; past the end it falls back to the oldest entry, which is a duller
 * demo but never a wrong one.
 */
interface SeededAnswer {
  done: string[];
  focus: string[];
  /** Absent means this person had nothing blocking that morning. */
  blocker?: { text: string; tag: string };
}

const MORNINGS: SeededAnswer[][] = [
  [
    {
      done: ['ต่อหน้า voucher list ของ REVERAPP-140 — ส่วน UI เสร็จแล้ว'],
      focus: ['เริ่ม flow redeem ต่อจาก voucher list'],
      blocker: { text: RECURRING[0] as string, tag: 'PO' },
    },
    {
      done: ['review PR #128 ให้ทีม BE'],
      focus: ['ทำ REVERAPP-152 ต่อ'],
      blocker: { text: 'Pending รอ merge PR #128', tag: '' },
    },
    {
      done: ['เขียน test ของ redemption flow 6 เคส'],
      focus: ['ตาม requirement หน้า redemption กับ PO'],
    },
  ],
  [
    {
      done: ['แก้ bug หน้า voucher list ตามที่ QA แจ้ง 3 ข้อ'],
      focus: ['เตรียม flow redeem ไว้ก่อน พอ requirement มาจะได้ต่อได้เลย'],
      blocker: { text: RECURRING[1] as string, tag: 'PO' },
    },
    {
      // Monday's blocker, gone by Tuesday. Nobody is asked to notice it; it is there
      // so that "ค้างมา 3 วัน" on the other line reads as a finding, not as the format.
      done: ['merge PR #128 แล้ว ขึ้น staging ให้ QA ลอง'],
      focus: ['เก็บ bug ที่ QA แจ้งไว้เมื่อวาน'],
    },
    {
      done: ['เขียน test เพิ่มอีก 4 เคส คลุม error ของ redeem'],
      focus: ['ช่วยดู QA ของ voucher list'],
    },
  ],
];

/** Today, in the same shape — seat 0 is on the third morning of the same wait. */
const TODAY: SeededAnswer[] = [
  {
    done: ['ปิด REVERAPP-152 แล้ว รอ QA verify'],
    focus: ['ต่อ REVERAPP-140 หน้า redemption'],
    // The third wording of the same wait. Three mornings, three sentences, one
    // streak — which is the only way to show that the counter is matching the
    // obstacle rather than the string.
    blocker: { text: RECURRING[2] as string, tag: 'PO' },
  },
  {
    done: ['เก็บ bug จาก QA รอบเช้า 2 ข้อ'],
    focus: ['ต่อ REVERAPP-152 ส่วน sync กับ BE'],
    blocker: { text: 'Pending รอ BE เปิด endpoint /redeem ให้ก่อน', tag: '' },
  },
  {
    done: ['เตรียม test data สำหรับ redeem'],
    focus: ['เก็บงาน QA ที่ค้าง'],
  },
];

/** The script for one seeded morning, oldest entry reused when the run is longer. */
function morningOf(index: number, total: number): SeededAnswer[] {
  const from = MORNINGS.length - (total - index);
  return MORNINGS[Math.max(0, Math.min(from, MORNINGS.length - 1))] as SeededAnswer[];
}

/** What the demo will say it fabricated, for the reply only the presenter sees. */
export function seededSummary(mornings: number, people: number): string {
  return (
    `${mornings} เช้า × ${people} คน — คนแรกติดเรื่องเดิม (requirement หน้า redemption จาก PO) ทุกเช้า ` +
    'เขียนคนละสำนวน · คนที่สองติดคนละเรื่องแล้วเคลียร์ได้วันถัดมา · งานที่ทำเสร็จเปลี่ยนทุกวัน'
  );
}

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
    const script = morningOf(index, dates.length);
    const answers: DailyAnswer[] = roster.slice(0, 3).map((user, seat) => {
      const line = script[Math.min(seat, script.length - 1)] as SeededAnswer;
      return {
        user,
        ts: `sim.${date}.${seat}`,
        done: [...line.done],
        focus: [...line.focus],
        // Seat 0 carries the recurring line whatever the script says, because the
        // streak is the claim; the rest keep whatever that morning gave them.
        blockers:
          seat === 0
            ? [{ text: RECURRING[index % RECURRING.length] as string, tag: 'PO' }]
            : line.blocker
              ? [{ ...line.blocker }]
              : [],
        simulated: true,
      };
    });
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
  return users.filter(Boolean).slice(0, 3).map((user, seat) => {
    const line = TODAY[Math.min(seat, TODAY.length - 1)] as SeededAnswer;
    return {
      user,
      ts: `sim.${date}.${seat}`,
      done: [...line.done],
      focus: [...line.focus],
      blockers: line.blocker ? [{ ...line.blocker }] : [],
      simulated: true,
    };
  });
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
