import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Ledger, WorkItem, Message, Decision, Drift, State } from './types.js';
import { apiConfig, envNumber, fetchLedger } from './tam-api.js';
import { readDecisions } from './store.js';
import { buildStandups } from './standups.js';
import { displayName } from './names.js';

const here = dirname(fileURLToPath(import.meta.url));
const LEDGER_PATH = resolve(here, '../data/ledger.json');

let cache: Ledger | null = null;
let refresh: { at: string; error: string | null } = { at: '', error: null };

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
 *
 * The fixture is only reachable when nobody asked for the pipeline. Falling back
 * to `fromDisk()` while `TAM_API_URL` is set would answer a command with fixture
 * content under a `pipeline` label — the exact confusion this module exists to
 * prevent — so an unhydrated cache in pipeline mode is an error, not a default.
 */
export function ledger(): Ledger {
  if (!cache) {
    if (apiConfig()) {
      throw new Error(
        'ledger ยังไม่ได้ hydrate และ TAM_API_URL ถูกตั้งไว้ — เรียก await hydrate() ก่อน ' +
          '(ห้าม fallback ไป fixture ตอนที่ขอ pipeline)',
      );
    }
    cache = fromDisk();
  }
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
    refresh = { at: nowStamp(), error: null };
    return { ledger: cache, origin };
  }

  // Fetch first, swap last. Nothing above this line touches `cache`, so a command
  // arriving mid-fetch is served the previous ledger and a failure leaves the last
  // known-good one in place — never the fixture under a 'pipeline' label.
  try {
    const fetched = await fetchLedger(cfg);
    cache = withLocalExtras(fetched, 'pipeline');
    origin = 'pipeline';
    refresh = { at: nowStamp(), error: null };
    return { ledger: cache, origin };
  } catch (err) {
    refresh = { at: nowStamp(), error: (err as Error).message };
    throw err;
  }
}

/** 'YYYY-MM-DD HH:mm' in local time — the same shape every timestamp is rendered in. */
function nowStamp(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * When the ledger in memory was last refreshed, and why the last attempt failed.
 *
 * A failed reload now keeps serving the previous pipeline data rather than
 * discarding it, which is the right call — but only if the surfaces can say the
 * data is older than it looks. Renderers read this; nothing infers it.
 */
export function refreshStatus(): { at: string; error: string | null } {
  return refresh;
}

/**
 * Fill the three collections the pipeline has no counterpart for.
 *
 * `decisions` come from what people actually filed through the message shortcut,
 * *on top of* whatever the source already carried — `scripts/build-ledger.ts`
 * extracts decisions from DECISION_CUES and writes them into ledger.json, and the
 * fixture's supersession chain is one of them, so replacing the list instead of
 * merging it deleted working content. `standups` are derived from the items in
 * hand, so the 08:45 DM and the digest cannot disagree. `drifts` need a ticket
 * system to compare Slack against, and nothing is connected, so there are none —
 * the fixture's drift is only loaded when `DEMO_FIXTURES=1` asks for it
 * explicitly, and the renderer labels it.
 */
function withLocalExtras(base: Ledger, from: Origin): Ledger {
  const cfg = apiConfig();
  const staleDays = cfg?.staleDays ?? envNumber('TAM_STALE_DAYS', 3, { min: 0 });
  const recentDays = envNumber('TAM_RECENT_DAYS', 1.5, { min: 0 });

  const built = buildStandups(base.items, { recentDays, staleDays });

  return {
    ...base,
    decisions: mergeDecisions(base.decisions ?? [], readDecisions()),
    // Derived drafts win when there are any. When there are none — every
    // participant is a display name the pipeline never resolved to an id — the
    // source's own drafts are the only ones that can be delivered, and dropping
    // them silently turned a working DM into no DM at all.
    standups: built.length ? built : (base.standups ?? []),
    drifts: from === 'fixture' && demoFixtures() ? resolvableDrifts(fromDisk().drifts, base.items) : [],
    // The fixture's own items are the only ones it can describe, so its
    // unassigned list is meaningless next to pipeline items.
    unassigned: from === 'fixture' ? base.unassigned : [],
  };
}

/**
 * Union by id, the bot's own file winning — it is the newer statement of the same
 * decision, and the ids already agree (`dec_${message id}` on both sides).
 */
function mergeDecisions(base: Decision[], local: Decision[]): Decision[] {
  const byId = new Map<string, Decision>();
  for (const d of [...base, ...local]) byId.set(d.id, d);
  return [...byId.values()];
}

/**
 * A drift claims a specific message changed the scope of a specific item. Keep
 * only the ones whose two halves are actually in the ledger: with fixture drifts
 * next to pipeline items neither resolves, and a nudge nobody can click through
 * to is exactly the unprovable claim the product forbids.
 */
function resolvableDrifts(drifts: Drift[], items: WorkItem[]): Drift[] {
  const keys = new Set(items.map((i) => i.key.toUpperCase()));
  const ids = new Set(items.flatMap((i) => i.messages.map((m) => m.id)));
  const kept = drifts.filter((d) => keys.has(d.item_key.toUpperCase()) && ids.has(d.trigger_id));
  const dropped = drifts.length - kept.length;
  if (dropped) {
    console.warn(`⚠  ข้าม drift ${dropped} รายการ: item_key/trigger_id ไม่มีใน ledger ที่ใช้อยู่`);
  }
  return kept;
}

/** Opt-in for fixture content that has no live source yet. Off by default. */
export function demoFixtures(): boolean {
  return process.env.DEMO_FIXTURES?.trim() === '1';
}

/**
 * Re-read without restarting. Used by `/meowtam reload` and after `npm run ledger`.
 *
 * It used to clear the cache first, which opened a window — 1 + N HTTP calls wide
 * — in which `ledger()` re-read the fixture while `ledgerOrigin()` still said
 * `pipeline`, and a failed reload left it that way for good. `hydrate()` swaps
 * atomically, so the only thing left to do here is let the error out: the caller
 * has to tell the user the refresh failed and that what they are looking at is
 * the previous build.
 */
export async function reload(): Promise<Ledger> {
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

/**
 * Items where `who` is assignee or participant.
 *
 * Matches the raw Slack id *and* the resolved display name, because those are two
 * spellings of the same person and the user only ever sees one of them. Typing
 * `/meowtam @แนน` after reading แนน's name off the board used to return nothing:
 * the ledger holds `U07…`, the screen said `แนน ก.`, and only the id matched.
 */
export function itemsFor(who: string): WorkItem[] {
  const needle = who.toLowerCase().replace(/^@/, '');
  const matches = (value?: string): boolean => {
    const raw = (value ?? '').toLowerCase();
    return raw === needle || displayName(value).toLowerCase() === needle;
  };
  return sortedItems().filter(
    (i) => i.state !== 'done' && (matches(i.assignee) || i.participants.some(matches)),
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
