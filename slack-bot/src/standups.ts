/**
 * Standup drafts, derived from work items instead of read from a fixture.
 *
 * The point of the 08:45 DM is that it does not ask what you did — it says what
 * it already thinks you did, and you correct it. That only works if the draft
 * comes from the same items the digest shows. Reading it from a hand-written
 * fixture made the DM a mock-up of itself.
 *
 * One deliberate restriction: a draft is only built for participants that are
 * real Slack user ids. The pipeline does not resolve ids to display names yet, so
 * a participant recorded as "Alice" cannot be sent a DM and has no draft. Adding
 * name→id guessing here would put the wrong standup in someone's DMs, which is
 * worse than no standup.
 */

import type { StandupDraft, WorkItem } from './types.js';

/** Slack user ids only — the pipeline mixes resolved names and raw ids. */
const SLACK_USER_ID = /^U[A-Z0-9]{6,}$/;

function daysSince(when: string): number {
  const t = new Date(when.replace(' ', 'T')).getTime();
  if (Number.isNaN(t)) return Number.POSITIVE_INFINITY;
  return (Date.now() - t) / 86_400_000;
}

export interface StandupOptions {
  /** Activity inside this window counts as "what you did". */
  recentDays: number;
  /** An open item quieter than this is carried over, with its age shown. */
  staleDays: number;
}

export function buildStandups(items: WorkItem[], opts: StandupOptions): StandupDraft[] {
  const byUser = new Map<string, WorkItem[]>();

  for (const item of items) {
    for (const who of item.participants) {
      if (!SLACK_USER_ID.test(who)) continue;
      const list = byUser.get(who) ?? [];
      list.push(item);
      byUser.set(who, list);
    }
  }

  const drafts: StandupDraft[] = [];

  for (const [userId, theirs] of byUser) {
    const yesterday = theirs
      .filter((i) => daysSince(i.last) <= opts.recentDays)
      .map((i) => ({
        key: i.key,
        headline: i.headline,
        // The evidence sentence is already the computed reason for the state, so
        // it is the honest note: it is what the bot believes and why.
        note: i.evidence,
        evidence_id: i.evidence_id || undefined,
      }));

    const carried = theirs
      .filter((i) => i.state !== 'done' && i.age_days > opts.staleDays)
      .map((i) => ({
        key: i.key,
        headline: i.headline,
        stale_days: Math.round(i.age_days),
      }))
      .sort((a, b) => b.stale_days - a.stale_days);

    // Nothing to correct and nothing to chase is not a standup worth sending.
    if (!yesterday.length && !carried.length) continue;

    drafts.push({
      slack_user_id: userId,
      // No display name is available. Rendering the id is honest; inventing a
      // name is not.
      display_name: userId,
      yesterday,
      carried_over: carried,
    });
  }

  return drafts;
}
