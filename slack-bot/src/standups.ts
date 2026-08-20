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

import type { DailyAnswer, StandupDraft, WorkItem } from './types.js';
import { composeDailyReply, parseDailyReply } from './daily.js';
import { clamp } from './blocks/common.js';

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

/* ------------------------------------------------------------------ *
 * What a filled-in daily looks like before anybody types
 *
 * Two surfaces offer it and they have to offer the same thing: the 08:45 DM, whose
 * three boxes arrive prefilled, and `/meowtam daily`, whose form is copied into the
 * thread. A person who answers one and a person who answers the other must be
 * writing the same lines under the same headings, or "answer either one" is a
 * promise the product cannot keep. So the lines are decided once, here.
 * ------------------------------------------------------------------ */

/**
 * Lines of `yesterday` the DM shows and records.
 *
 * One number for both because they must not disagree. A card that renders five items
 * and stores eight reports work the person never saw it claim; one that stores five
 * and renders eight loses what they read and confirmed. `standupDmBlocks` renders
 * exactly this many and says "…และอีก N งาน" about the rest.
 */
export const MAX_YESTERDAY = 5;
/** Prefilled lines per box. Three fits without scrolling in a DM. */
const MAX_PREFILL = 3;

/**
 * What the two boxes start with.
 *
 * The header of this DM says the bot already knows and only wants corrections, and
 * for a year the boxes under it were empty — so the person read "you don't have to
 * retype it" and then retyped it. Prefilling is what that sentence promised.
 *
 * The shape is the daily template's, not a new one (`DAILY_TEMPLATE` in daily.ts):
 * `ต่อ KEY — headline` for today, `Pending …` for a blocker. Somebody who fills a
 * daily thread and somebody who answers this DM are then writing the same lines, and
 * `parseDailyReply` reads both — a second format would be a second thing to teach.
 *
 * Today's box may be a proposal ("carry on with what is still open"); the blocker box
 * may not. It is prefilled only from sentences this person typed, because pressing
 * *ส่ง* posts it into the channel under their name — see `blocked` in StandupDraft.
 */
/** `REVERAPP-140` — a key a person can look up, as opposed to a cluster id like `c23053d`. */
export const TICKET_KEY = /^[A-Z][A-Z0-9]+-\d+$/;

/**
 * One line of the "today" box, or nothing.
 *
 * A cluster id is not a name. `ต่อ c5a3d6b — (ยังไม่มีคำร่วมที่ชัดพอจะตั้งชื่อ)` is a
 * line nobody can act on or even look up, and offering it as a suggestion asks the
 * reader to delete it — which is more work than the empty box it replaced. So a
 * ticket keeps its key, a cluster is named by its keywords alone, and a cluster the
 * pipeline could not name at all is left out.
 */
function namedLine(key: string, headline: string): string | undefined {
  const name = clamp(headline.trim(), 60);
  if (TICKET_KEY.test(key)) return `${key} — ${name}`;
  if (!name || name.startsWith('(')) return undefined;
  return name;
}

function todayLine(key: string, headline: string): string | undefined {
  const named = namedLine(key, headline);
  return named ? `ต่อ ${named}` : undefined;
}

/**
 * What this DM claims the person did — the "Done Yesterday" of the answer it records.
 *
 * Capped at `MAX_YESTERDAY` on purpose, and at the same number the card renders. What
 * gets stored under somebody's name has to be what was on their screen when they
 * pressed ส่ง; recording the two extra items behind "…และอีก N งาน" would be the bot
 * reporting work the person never saw it claim.
 *
 * Key and headline only. `note` is the pipeline's evidence sentence — its reason for
 * believing the item moved — and is the bot's sentence, not this person's.
 */
export function standupDone(draft: StandupDraft): string[] {
  return draft.yesterday
    .slice(0, MAX_YESTERDAY)
    .map((y) => namedLine(y.key, y.headline))
    .filter((line): line is string => Boolean(line));
}

export function standupPrefill(draft: StandupDraft): { today?: string; blocker?: string } {
  // Tickets before clusters, then stalest first: the lines a person can act on are
  // the ones worth the three slots.
  const open = [...draft.carried_over]
    .sort((a, b) => {
      const byKind = Number(TICKET_KEY.test(b.key)) - Number(TICKET_KEY.test(a.key));
      return byKind || b.stale_days - a.stale_days;
    })
    .map((c) => todayLine(c.key, c.headline))
    .filter((line): line is string => Boolean(line))
    .slice(0, MAX_PREFILL);
  // Nothing carried over: offer yesterday's work instead, which is the other honest
  // guess at "today". Neither is offered when there is nothing at all — an empty box
  // with its placeholder asks the question; a box holding `-` answers it wrongly.
  const fallback = draft.yesterday
    .map((y) => todayLine(y.key, y.headline))
    .filter((line): line is string => Boolean(line))
    .slice(0, MAX_PREFILL);
  const today = open.length ? open : fallback;

  const blocker = (draft.blocked ?? [])
    .slice(0, MAX_PREFILL)
    // Same reason as `todayLine`: a ticket key helps whoever reads it in the channel,
    // a cluster id is noise nobody can look up.
    .map((b) => `Pending ${clamp(b.text, 120)}${TICKET_KEY.test(b.key) ? ` (${b.key})` : ''}`);

  return {
    today: today.length ? today.join('\n') : undefined,
    blocker: blocker.length ? blocker.join('\n') : undefined,
  };
}

/**
 * What pressing ส่ง records — one daily answer, in the daily's own format.
 *
 * The DM used to be a dead end: the boxes were read, the blocker was announced, and
 * the "วันนี้" box went nowhere. Anyone who answered here still had to type the
 * same three sections into the thread or be listed as not having answered — which is
 * the duplicate work this DM exists to remove.
 *
 * So the answer is written in `DAILY_TEMPLATE`'s shape and read back with
 * `parseDailyReply`. Not for tidiness: it is the only way the two routes cannot drift.
 * A tag rule, a bullet convention or a placeholder check added to the thread parser
 * reaches a DM answer on the same day, because there is one parser and one format.
 *
 * Returns nothing when there would be nothing to record. An empty answer stored is a
 * person marked as having answered with silence, which reads worse in the summary
 * than the truth — that they have not answered yet.
 */
export function standupAnswer(input: {
  user: string;
  done: string;
  today: string;
  blocker: string;
}): DailyAnswer | undefined {
  // All three sections come from boxes the person could edit. The draft decides what
  // they *start* as (`standupDone`, `standupPrefill`) and nothing more: what gets
  // stored under somebody's name in a summary the room reads has to be text they had
  // the chance to correct, not the bot's reading of Slack passed off as their report.
  const text = composeDailyReply({
    done: [input.done],
    focus: [input.today],
    blockers: [input.blocker],
  });
  // `ts` is empty, not invented: there is no Slack message behind this answer, and a
  // plausible-looking ts would send `conversations.replies` — or a permalink — at a
  // message that does not exist. Renderers check it before offering a link.
  const parsed = parseDailyReply(text, input.user, '');
  if (parsed.kind !== 'answer') return undefined;
  const { done, focus, blockers } = parsed.answer;
  if (!done.length && !focus.length && !blockers.length) return undefined;
  return { ...parsed.answer, via: 'dm' };
}
