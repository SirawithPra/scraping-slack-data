import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Ledger, WorkItem, Message, Decision, State } from './types.js';
import { apiConfig, fetchLedger } from './tam-api.js';
import { readDecisions } from './store.js';
import { buildStandups } from './standups.js';

const here = dirname(fileURLToPath(import.meta.url));
const LEDGER_PATH = resolve(here, '../data/ledger.json');

let cache: Ledger | null = null;

/** Where the ledger currently in memory came from. Rendered, never guessed at. */
export type Origin = 'fixture' | 'pipeline';
let origin: Origin = 'fixture';
export function ledgerOrigin(): Origin {
  return origin;
}

function fromDisk(): Ledger {
  return JSON.parse(readFileSync(LEDGER_PATH, 'utf8')) as Ledger;
}

/**
 * Synchronous by design. Every renderer and Block Kit builder calls this, so
 * making it async would turn a one-file change into a rewrite. `hydrate()` does
 * the awaiting once at boot and fills this cache; reads stay cheap afterwards.
 */
export function ledger(): Ledger {
  if (!cache) cache = fromDisk();
  return cache;
}

/**
 * Load the ledger from the Python pipeline when `TAM_API_URL` is set.
 *
 * **No fallback.** Asking for the pipeline and silently getting three-day-old
 * fixture data is the worst outcome available: the bot keeps answering, the
 * answers look identical, and nobody learns the pipeline is down until a
 * decision has already been made on stale numbers. If the pipeline was
 * requested and cannot answer, this throws and the bot does not start.
 *
 * With `TAM_API_URL` unset there is nothing to be confused about — the fixture
 * is the declared source, and the boot line says so.
 */
export async function hydrate(): Promise<{ ledger: Ledger; origin: Origin }> {
  const cfg = apiConfig();

  if (!cfg) {
    cache = withLocalExtras(fromDisk(), 'fixture');
    origin = 'fixture';
    return { ledger: cache, origin };
  }

  const fetched = await fetchLedger(cfg);
  cache = withLocalExtras(fetched, 'pipeline');
  origin = 'pipeline';
  return { ledger: cache, origin };
}

/**
 * Fill the three collections the pipeline has no counterpart for.
 *
 * `decisions` come from what people actually filed through the message shortcut.
 * `standups` are derived from the items in hand, so the 08:45 DM and the digest
 * cannot disagree. `drifts` need a ticket system to compare Slack against, and
 * nothing is connected, so there are none — the fixture's drift is only loaded
 * when `DEMO_FIXTURES=1` asks for it explicitly, and the renderer labels it.
 */
function withLocalExtras(base: Ledger, from: Origin): Ledger {
  const cfg = apiConfig();
  const staleDays = cfg?.staleDays ?? (Number(process.env.TAM_STALE_DAYS ?? 3) || 3);
  const recentDays = Number(process.env.TAM_RECENT_DAYS ?? 1.5) || 1.5;

  return {
    ...base,
    decisions: readDecisions(),
    standups: buildStandups(base.items, { recentDays, staleDays }),
    drifts: demoFixtures() ? fromDisk().drifts : [],
    // The fixture's own items are the only ones it can describe, so its
    // unassigned list is meaningless next to pipeline items.
    unassigned: from === 'fixture' ? base.unassigned : [],
  };
}

/** Opt-in for fixture content that has no live source yet. Off by default. */
export function demoFixtures(): boolean {
  return process.env.DEMO_FIXTURES?.trim() === '1';
}

/** Re-read without restarting. Used by `/meowtam reload` and after `npm run ledger`. */
export async function reload(): Promise<Ledger> {
  cache = null;
  return (await hydrate()).ledger;
}

/** Which work item a message belongs to — powers item chips on recall hits. */
export function itemKeyForMessage(id: string): string | undefined {
  for (const item of ledger().items) {
    if (item.messages.some((m) => m.id === id)) return item.key;
  }
  return undefined;
}

const STATE_ORDER: Record<State, number> = { blocked: 0, stalled: 1, moving: 2, done: 3 };

/** Digest order: blocked first, then stalled, then moving, then done. Within a state, stalest first. */
export function sortedItems(items = ledger().items): WorkItem[] {
  return [...items].sort(
    (a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || b.age_days - a.age_days,
  );
}

export function itemsByState(state: State): WorkItem[] {
  return sortedItems().filter((i) => i.state === state);
}

export function findItem(key: string): WorkItem | undefined {
  const k = key.trim().toUpperCase();
  return ledger().items.find((i) => i.key.toUpperCase() === k);
}

export function findMessage(id: string): Message | undefined {
  for (const item of ledger().items) {
    const hit = item.messages.find((m) => m.id === id);
    if (hit) return hit;
  }
  return ledger().unassigned.find((m) => m.id === id);
}

/** Items where `who` is assignee or participant. Matches display name or raw Slack id. */
export function itemsFor(who: string): WorkItem[] {
  const needle = who.toLowerCase().replace(/^@/, '');
  return sortedItems().filter(
    (i) =>
      i.state !== 'done' &&
      (i.assignee?.toLowerCase() === needle ||
        i.participants.some((p) => p.toLowerCase() === needle)),
  );
}

export function standupFor(slackUserId: string) {
  return ledger().standups.find((s) => s.slack_user_id === slackUserId);
}

export function driftFor(itemKey: string) {
  return ledger().drifts.find((d) => d.item_key.toUpperCase() === itemKey.toUpperCase());
}

/**
 * Walk a decision to the end of its supersession chain, oldest first.
 * This is what makes recall answer "we decided X 3 months ago, what is it now?"
 */
export function decisionChain(seed: Decision): Decision[] {
  const all = ledger().decisions;
  const chain: Decision[] = [];

  // Walk backwards to the origin first, guarding against cycles in hand-edited data.
  let head: Decision | undefined = seed;
  const seenBack = new Set<string>();
  while (head && !seenBack.has(head.id)) {
    seenBack.add(head.id);
    const prev: Decision | undefined = all.find((d) => d.superseded_by === head!.id);
    if (!prev) break;
    head = prev;
  }

  const seen = new Set<string>();
  let cursor: Decision | undefined = head;
  while (cursor && !seen.has(cursor.id)) {
    seen.add(cursor.id);
    chain.push(cursor);
    cursor = cursor.superseded_by ? all.find((d) => d.id === cursor!.superseded_by) : undefined;
  }
  return chain;
}
