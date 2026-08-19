/**
 * Standup drafts, derived from work items instead of read from a fixture.
 *
 * The point of the 08:45 DM is that it does not ask what you did — it says what
 * it already thinks you did, and you correct it. That only works if the draft
 * comes from the same items the digest shows. Reading it from a hand-written
 * fixture made the DM a mock-up of itself.
 *
 * One deliberate restriction: a draft is only built for participants that are
 * real Slack user ids. A participant recorded as "Alice" — a meeting transcript's
 * speaker — has no id to address a DM to, so they get no draft. Resolving ids to
 * names (which `names.ts` now does for the *header*) does not help here and must
 * not be confused with it: going the other way, name→id, would be guesswork, and
 * the wrong standup in somebody's DMs is worse than no standup.
 *
 * That restriction stays, but it no longer happens quietly. A corpus whose
 * participants are all display names produces zero drafts, which looks exactly
 * like a corpus where nobody has anything to report — so the names that were
 * skipped are counted, logged, and readable through `skippedParticipants()`. The
 * honest answer is "N people have no draft because the pipeline never resolved
 * their id", not an empty list.
 */

import type { StandupDraft, WorkItem } from './types.js';

/**
 * Slack user ids only — the pipeline mixes resolved names and raw ids.
 * `W…` is an Enterprise Grid id and just as real as `U…`; rejecting it was an
 * accident, not the deliberate restriction above.
 */
const SLACK_USER_ID = /^[UW][A-Z0-9]{6,}$/;

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

let skipped: string[] = [];

/**
 * Participants the last build could not address, newest build only.
 *
 * Rendered next to the standup count so "2 drafts" never hides "and 4 people we
 * cannot DM". Empty is the good case.
 */
export function skippedParticipants(): string[] {
  return [...skipped];
}

/**
 * The one sentence that made this blocked, out of a message that may be a whole
 * morning's paste.
 *
 * The pipeline detects the state line by line and quotes the line it matched:
 * `blocked since … — คนกรอกเองว่า "ท่าสองไปท่าหน้าแยก … mkt ต้องรอ"`. That quote is
 * what belongs in a blocker box. Taking the message instead put a 40-line sprint
 * recap — headings and all — into a field that is one button press from the channel.
 *
 * Falls back to the message's first non-empty line when the evidence carries no
 * quote, which is the shape a fixture and an older pipeline both produce.
 */
function statedBlocker(evidence: string, said: string): string {
  const quoted = evidence.match(/[“"]([^”"]{4,})[”"]/);
  if (quoted?.[1]) return quoted[1].trim();
  return said.split(/\r?\n/).map((l) => l.trim()).find(Boolean) ?? said.trim();
}

export function buildStandups(items: WorkItem[], opts: StandupOptions): StandupDraft[] {
  const byUser = new Map<string, WorkItem[]>();
  const unaddressable = new Set<string>();

  for (const item of items) {
    for (const who of item.participants) {
      if (!SLACK_USER_ID.test(who)) {
        unaddressable.add(who);
        continue;
      }
      const list = byUser.get(who) ?? [];
      list.push(item);
      byUser.set(who, list);
    }
  }

  skipped = [...unaddressable].sort();
  if (skipped.length) {
    console.warn(
      `⚠  ไม่มี standup draft ให้ ${skipped.length} คน (${skipped.join(', ')}) — ` +
        'pipeline เก็บเป็นชื่อ ไม่ใช่ Slack id จึง DM ไม่ได้ และจะไม่เดาให้',
    );
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

    // Only what this person said with their own hands. `evidence_id` resolves inside
    // the item's own messages, so the author is knowable here without reaching for
    // the ledger — and an item blocked by somebody else's sentence is dropped rather
    // than attributed, because this list is what the DM offers to send to the channel.
    const blocked = theirs
      .filter((i) => i.state === 'blocked')
      .map((i) => ({ item: i, said: i.messages.find((m) => m.id === i.evidence_id) }))
      .filter((pair) => pair.said?.user === userId)
      .map(({ item, said }) => ({
        key: item.key,
        headline: item.headline,
        text: statedBlocker(item.evidence, said!.text),
        evidence_id: item.evidence_id || undefined,
      }));

    // Nothing to correct and nothing to chase is not a standup worth sending.
    if (!yesterday.length && !carried.length) continue;

    drafts.push({
      slack_user_id: userId,
      // No display name is available. Rendering the id is honest; inventing a
      // name is not.
      display_name: userId,
      yesterday,
      carried_over: carried,
      blocked,
    });
  }

  return drafts;
}
