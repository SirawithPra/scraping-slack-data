/**
 * Work that has not moved in N *working* days.
 *
 * The daily thread already catches one shape of stuck: the same pending line, from
 * the same person, several mornings running. It cannot catch the other shape — the
 * item nobody mentions at all. A blocker somebody keeps typing is at least being
 * thought about; a work item whose last message was a week ago has fallen out of
 * the conversation entirely, and that is the one that surfaces in a status meeting
 * as "oh, that". Nobody types a pending line for it, so nothing in the standup path
 * can find it. It has to be computed from the messages.
 *
 * Why working days and not days
 * -----------------------------
 * Because a team does not work on Saturday, and counting calendar days makes every
 * Friday item look two days worse by Monday morning without anything having
 * happened. Five calendar days spanning a weekend is three working days of silence,
 * which is normal; five *working* days is a week of nobody touching it, which is
 * not. Getting this wrong in the loud direction is how an escalation earns a mute,
 * and a muted escalation is worse than none.
 *
 * Holidays are not modelled. Thailand has a lot of them and a public-holiday
 * calendar the bot cannot verify would be a source of confident wrong answers; the
 * weekend rule is the part that is true without a lookup table.
 */

import type { WorkItem } from './types.js';

/** Saturday or Sunday in the host's zone — the same clock the schedules fire off. */
export function isWeekend(day: Date): boolean {
  const d = day.getDay();
  return d === 0 || d === 6;
}

/**
 * Working days from `from` to `to`, counting the days *after* `from` up to and
 * including `to`.
 *
 * So a message on Friday, read the following Friday, is five: Mon Tue Wed Thu Fri.
 * Same-day is zero, and a `to` before `from` is zero rather than negative — a clock
 * skew must not turn into an escalation with a nonsense number on it.
 */
export function businessDaysBetween(from: Date, to: Date): number {
  const start = new Date(from.getFullYear(), from.getMonth(), from.getDate());
  const end = new Date(to.getFullYear(), to.getMonth(), to.getDate());
  if (end <= start) return 0;
  let count = 0;
  const cursor = new Date(start);
  while (cursor < end) {
    cursor.setDate(cursor.getDate() + 1);
    if (!isWeekend(cursor)) count += 1;
  }
  return count;
}

/** `'2026-08-20 09:14'` → a Date, or undefined. Never throws on the ledger's own strings. */
export function parseWhen(when: string): Date | undefined {
  const t = new Date(String(when ?? '').replace(' ', 'T')).getTime();
  return Number.isNaN(t) ? undefined : new Date(t);
}

export interface StaleItem {
  item: WorkItem;
  /** Working days since the item's last message. The number the message reports. */
  workdays: number;
  /** The date that count runs from, as the ledger recorded it. */
  since: string;
}

/**
 * Items nobody has touched for `workdays` working days, longest silence first.
 *
 * `done` items are excluded — finished work is supposed to go quiet. Everything
 * else is included regardless of state: a `blocked` item going silent for a week
 * is the most worrying case here, not an exempt one, because "blocked" is a claim
 * somebody made days ago and nobody has revisited.
 *
 * An item with an unreadable `last` is skipped rather than treated as ancient. The
 * whole message rests on a date, and escalating on a date we could not parse would
 * tag a person over a formatting bug.
 */
export function staleItems(
  items: WorkItem[],
  opts: { workdays: number; now?: Date },
): StaleItem[] {
  const now = opts.now ?? new Date();
  const out: StaleItem[] = [];
  for (const item of items) {
    if (item.state === 'done') continue;
    const last = parseWhen(item.last);
    if (!last) continue;
    const workdays = businessDaysBetween(last, now);
    if (workdays >= opts.workdays) out.push({ item, workdays, since: item.last });
  }
  return out.sort((a, b) => b.workdays - a.workdays || a.item.key.localeCompare(b.item.key));
}

/**
 * The key an escalation is remembered by, bucketed so it can fire again later.
 *
 * Not the item key alone: an item that goes quiet for a month would then be
 * announced once and never again, and the reminder is most needed at the point
 * everyone has forgotten it exists. Not the raw day count either, or it would fire
 * every single morning. Bucketing by the threshold means it speaks at 5 working
 * days, again at 10, again at 15 — each time with a bigger number, which is the
 * shape of a fact worth repeating.
 */
export function staleKey(entry: StaleItem, threshold: number): string {
  const bucket = Math.floor(entry.workdays / Math.max(1, threshold));
  return `${entry.item.key}::${bucket * Math.max(1, threshold)}`;
}
