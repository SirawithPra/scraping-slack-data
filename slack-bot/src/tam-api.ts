/**
 * Client for the Python pipeline's HTTP API (`python3 -m tam.web.server`).
 *
 * Why this file exists: until now the bot decided what a "work item" was by
 * itself, matching character trigrams in `scripts/build-ledger.ts`, while the
 * Python side decided the same thing by clustering embeddings from a trained
 * model. One channel could therefore produce two different sets of work items.
 * This module makes the Python side the single owner of that definition and
 * leaves the bot as a renderer, which is what it is good at.
 *
 * It is a *hybrid* on purpose, and the split is not arbitrary — it follows
 * which side actually computes the thing:
 *
 *   from the API      work items, states, evidence, timelines, messages,
 *                     summaries, and recall (embeddings + BM25 + rerank)
 *   from ledger.json  decisions, drifts, standup drafts
 *
 * The three kept local have no counterpart in the pipeline yet. Silently
 * emptying them would delete working features, so they are carried over, and
 * `ledgerOrigin()` in data.ts reports which source is live — the bot should never
 * be vague about the provenance of something it renders.
 *
 * With `TAM_API_URL` unset, nothing here runs and the bot behaves exactly as
 * before. That keeps the offline demo path intact.
 */

import type {
  Ledger,
  Message,
  Source,
  State,
  TimelineEvent,
  WorkItem,
} from './types.js';

export interface ApiConfig {
  baseUrl: string;
  /** Workspace host for rebuilt permalinks, e.g. 'myteam.slack.com'. */
  workspace: string;
  /** An 'active' topic whose last activity is older than this reads as stalled. */
  staleDays: number;
  /** Raw cosine the nearest record must reach before recall reports any hit. */
  minCosine: number;
  timeoutMs: number;
}

/**
 * Read a numeric setting from the environment.
 *
 * `Number(x) || fallback` treated a deliberate `0` and a typo'd `20s` the same
 * way as an unset variable: both became the default, silently. That is the worst
 * shape a knob can have here, because `minCosine` decides whether recall reports
 * anything at all and `staleDays` decides the stalled badge — an operator who
 * believes they retuned the gate would be reading claims produced by settings
 * they did not choose. Three cases, all distinguishable: unset → the default,
 * parseable (including 0) → used as given, anything else → throw, because a value
 * nobody can honour is a misconfiguration and not a preference.
 */
export function envNumber(
  name: string,
  fallback: number,
  range?: { min?: number; max?: number },
): number {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === '') return fallback;
  const n = Number(raw.trim());
  if (!Number.isFinite(n)) {
    throw new Error(`${name}="${raw}" ไม่ใช่ตัวเลข — แก้ .env หรือลบออกเพื่อใช้ค่า default ${fallback}`);
  }
  const { min, max } = range ?? {};
  if ((min !== undefined && n < min) || (max !== undefined && n > max)) {
    const bounds = `${min ?? '-∞'}..${max ?? '∞'}`;
    throw new Error(`${name}=${n} อยู่นอกช่วงที่ใช้ได้ (${bounds})`);
  }
  return n;
}

export function apiConfig(): ApiConfig | null {
  const baseUrl = process.env.TAM_API_URL?.trim().replace(/\/+$/, '');
  if (!baseUrl) return null;
  return {
    baseUrl,
    workspace: (process.env.SLACK_WORKSPACE_URL?.trim() || 'https://slack.com').replace(/\/+$/, ''),
    staleDays: envNumber('TAM_STALE_DAYS', 3, { min: 0 }),
    minCosine: envNumber('TAM_MIN_COSINE', 0.45, { min: 0, max: 1 }),
    timeoutMs: envNumber('TAM_API_TIMEOUT_MS', 20_000, { min: 1 }),
  };
}

async function get<T>(cfg: ApiConfig, path: string): Promise<T> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), cfg.timeoutMs);
  try {
    const res = await fetch(`${cfg.baseUrl}${path}`, { signal: ctl.signal });
    if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
    return (await res.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// shape translation
// ---------------------------------------------------------------------------

/** `msg_C0DEMOCHAN1_1786630932.425409` → channel + Slack's `p…` timestamp. */
const SLACK_ID = /^msg_([A-Z0-9]+)_(\d+\.\d+)$/;

function permalinkFor(id: string, workspace: string): string | undefined {
  const m = SLACK_ID.exec(id);
  const channel = m?.[1];
  const ts = m?.[2];
  if (!channel || !ts) return undefined; // meeting utterances (mtg_…) have no Slack message
  return `${workspace}/archives/${channel}/p${ts.replace('.', '')}`;
}

function channelFor(id: string): string | undefined {
  return SLACK_ID.exec(id)?.[1];
}

/** The pipeline stamps source on records; fall back to the id prefix. */
function sourceFor(id: string, given?: unknown): Source {
  if (given === 'slack' || given === 'meeting' || given === 'youtrack' || given === 'notion') {
    return given;
  }
  return id.startsWith('mtg_') ? 'meeting' : 'slack';
}

/**
 * Timekeeping across the seam.
 *
 * `when` on the wire is a *display* string — 'YYYY-MM-DD HH:mm', no offset, no
 * seconds — because the pipeline formats it with a naive `datetime.fromtimestamp`.
 * Reparsing it means guessing which timezone produced it, and Node guesses this
 * process's own. On one machine that is right; a bot container defaulting to UTC
 * against a pipeline on Asia/Bangkok is off by the whole offset, which is enough
 * to flip a `stalled` badge (measured: 4.05 days vs 3.46 for the same bytes).
 *
 * So: use a real instant whenever the server sends one (`last_ts` on a topic,
 * `ts` on a message — epoch seconds), and when it does not, say so once instead
 * of quietly computing from an assumption.
 */
let warnedNoEpoch = false;

function noteMissingEpoch(): void {
  if (warnedNoEpoch) return;
  warnedNoEpoch = true;
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  console.warn(
    `⚠  pipeline ไม่ได้ส่ง epoch (last_ts/ts) มาด้วย — คิดเวลาจากสตริง 'YYYY-MM-DD HH:mm' ` +
      `โดยถือว่า pipeline อยู่ timezone เดียวกับ bot (${zone}); ถ้าไม่ใช่ stalled จะเพี้ยนเท่าส่วนต่างโซน`,
  );
}

function daysSince(when: string, ts?: number): number {
  if (typeof ts === 'number' && Number.isFinite(ts)) return (Date.now() - ts * 1000) / 86_400_000;
  noteMissingEpoch();
  const t = new Date(when.replace(' ', 'T')).getTime();
  if (Number.isNaN(t)) return 0;
  return (Date.now() - t) / 86_400_000;
}

/**
 * The pipeline has three states (active | blocked | resolved); the bot renders
 * four. `stalled` is the one with no counterpart, so it is derived here from how
 * long the topic has been quiet rather than invented upstream. This is the only
 * place the bot adds a judgement of its own, and `TAM_STALE_DAYS` tunes it.
 */
function stateFor(
  apiState: string,
  last: string,
  lastTs: number | undefined,
  staleDays: number,
): State {
  if (apiState === 'blocked') return 'blocked';
  if (apiState === 'resolved') return 'done';
  return daysSince(last, lastTs) > staleDays ? 'stalled' : 'moving';
}

const KIND_BY_RELATION: Record<string, TimelineEvent['kind']> = {
  blocked_by: 'blocked',
  resolves: 'unblocked',
  duplicates: 'status_change',
  follows_up: 'status_change',
  answers: 'status_change',
};

interface ApiTopic {
  key: number;
  /**
   * Stable across rebuilds — a ticket key the item's messages mention, or a hash of
   * its earliest message. `key` is the Louvain cluster index, which is a size rank:
   * it names a different work item after the next build, so nothing that outlives a
   * build (a Slack card, a bookmark, a human's saved correction) may key on it.
   */
  item_id: string;
  label: string;
  state: string;
  evidence: string;
  evidence_id: string;
  participants: string[];
  sources: Record<string, number>;
  first: string;
  last: string;
  /** Epoch seconds for `last`, when the server sends it. See daysSince. */
  last_ts?: number;
  age_days: number;
  summary?: ApiSummary | null;
}

interface ApiSummary {
  /** The summariser's own one-liner. Prefer it over the TF-IDF cluster label. */
  headline?: string;
  detail: string;
  next_step?: string;
  citations: string[];
  unverified: boolean;
  /** Which summariser wrote it — 'template' (rules) or a model id. Renders as provenance. */
  backend?: string;
}

interface ApiMessage {
  id: string;
  when: string;
  /** Epoch seconds, when the server sends it. See daysSince. */
  ts?: number;
  user?: string | null;
  source?: string | null;
  text: string;
}

interface ApiItemDetail {
  timeline: ApiTimelineEntry[];
  messages: ApiMessage[];
  summary?: ApiSummary | null;
}

interface ApiTimelineEntry {
  relation: string;
  when: string;
  to_id?: string;
  to_text?: string;
  to_user?: string | null;
  evidence?: string;
  /** How many further messages collapsed onto this same event. */
  also_answers?: number;
}

function toMessage(m: ApiMessage, cfg: ApiConfig): Message {
  return {
    id: m.id,
    source: sourceFor(m.id, m.source),
    // Never invent a display name: the pipeline does not resolve ids yet, so a
    // raw U… is the honest thing to render.
    user: m.user?.trim() || 'unknown',
    when: m.when,
    text: m.text,
    permalink: permalinkFor(m.id, cfg.workspace),
    channel: channelFor(m.id),
  };
}

function toTimeline(entries: ApiTimelineEntry[]): TimelineEvent[] {
  return entries
    .filter((e) => e.to_id && e.to_text)
    .map((e) => {
      let text = e.evidence ? `${e.to_text} — ${e.evidence}` : (e.to_text as string);
      // The pipeline collapses several relations onto one event and reports how
      // many; its own HTML says so (server.py:237). Dropping the count made a row
      // that stands for five messages look like a row that stands for one.
      const also = e.also_answers ?? 0;
      if (also > 0) text += ` · ตอบข้อความก่อนหน้าอีก ${also} ข้อความ`;
      return {
        when: e.when,
        kind: KIND_BY_RELATION[e.relation] ?? 'status_change',
        text,
        source: sourceFor(e.to_id as string),
        user: e.to_user?.trim() || 'unknown',
        evidence_id: e.to_id,
      };
    });
}

/**
 * The state clause the template summariser appends to its headline. Slack renders
 * the state from `STATE_LABEL` already, so leaving it in double-prints it.
 */
const HEADLINE_STATE_CLAUSE =
  / — (?:ติดอยู่ ยังไปต่อไม่ได้|blocked — not moving|ปิดแล้ว|resolved|กำลังดำเนินอยู่|in progress)$/;

function headlineFor(topic: ApiTopic, summary: ApiSummary | null): string {
  const written = summary?.headline?.trim() ?? '';
  if (!written) return topic.label;
  return written.replace(HEADLINE_STATE_CLAUSE, '').trim() || topic.label;
}

/**
 * The newest message in an item.
 *
 * `/api/item` returns messages oldest-first by true timestamp (digest.py sorts
 * `topic.records`), so the last element is the newest. Comparing the `when`
 * *strings* instead — as this used to — ties every message inside the same minute,
 * and `>` then kept the earliest of them: on live data two of five items anchored
 * their "latest activity" evidence on the topic's opening message. Prefer the
 * epoch when the server sends one; otherwise trust the order it documents, and
 * never reparse a display string to sort by.
 */
function newestIndex(messages: ApiMessage[]): number {
  if (!messages.length) return -1;
  const epochs = messages.map((m) => (typeof m.ts === 'number' && Number.isFinite(m.ts) ? m.ts : NaN));
  if (epochs.every((t) => !Number.isNaN(t))) {
    return epochs.reduce((best, t, i) => (t > (epochs[best] as number) ? i : best), 0);
  }
  return messages.length - 1;
}

function toWorkItem(topic: ApiTopic, detail: ApiItemDetail, cfg: ApiConfig): WorkItem {
  const summary = detail.summary ?? topic.summary ?? null;
  const apiMessages = detail.messages ?? [];
  const messages = apiMessages.map((m) => toMessage(m, cfg));

  // The pipeline only produces an evidence sentence for a *state change* —
  // blocked or resolved. An active topic has none, and the bot renders the
  // evidence line verbatim, so passing the empty string through would print a
  // blank claim. Anchor those on the newest message instead: "nothing has
  // changed state, here is the last thing that happened" is both true and
  // clickable, which is the bar every rendered claim has to clear.
  let evidence = topic.evidence?.trim() ?? '';
  let evidenceId = topic.evidence_id?.trim() ?? '';
  if (!evidence || !evidenceId) {
    const newest = messages[newestIndex(apiMessages)];
    if (newest) {
      evidence = evidence || `ยังไม่มีสัญญาณเปลี่ยนสถานะ — ล่าสุด ${newest.when}`;
      evidenceId = evidenceId || newest.id;
    }
  }

  return {
    // The pipeline's stable id, not its cluster index. The index is a size rank, so
    // keying on it means a rebuild silently reattaches every saved human correction
    // to whichever item happens to be that size next time. item_id is either the
    // ticket the item's messages mention (REV-1421) or a content hash (c30a929);
    // both are already distinct from anything the bot would mint, so no prefix.
    key: topic.item_id,
    // The summariser writes a headline naming the item; the label is the raw
    // TF-IDF cluster string ('street, sales dashboard, react'). The pipeline's own
    // HTML prefers the written one (server.py:147), so the bot must too, or the
    // two halves of the product name the same work item differently.
    headline: headlineFor(topic, summary),
    state: stateFor(topic.state, topic.last, topic.last_ts, cfg.staleDays),
    evidence,
    evidence_id: evidenceId,
    age_days: Math.round(topic.age_days * 10) / 10,
    participants: topic.participants ?? [],
    sources: (topic.sources ?? {}) as WorkItem['sources'],
    first: topic.first,
    last: topic.last,
    summary: summary
      ? {
          detail: summary.detail,
          next_step: summary.next_step || undefined,
          citations: summary.citations ?? [],
          unverified: Boolean(summary.unverified),
          backend: summary.backend || undefined,
        }
      : undefined,
    timeline: toTimeline(detail.timeline ?? []),
    messages,
  };
}

// ---------------------------------------------------------------------------
// public surface
// ---------------------------------------------------------------------------

export interface ApiSearchHit {
  rank: number;
  score: number;
  id: string;
  source?: string | null;
  user?: string | null;
  when: string;
  text: string;
  why: Record<string, number>;
  terms: string[];
}

/**
 * Fetch the digest, then each topic's detail. The digest gives a message
 * *count*; the bot renders actual messages, so the per-item call is required
 * rather than an optimisation. Requests run in parallel — the corpora this
 * targets are tens of topics, not thousands.
 *
 * A failed detail call is fatal, deliberately. It used to be caught per item and
 * turned into `{timeline: [], messages: []}`, which kept the digest's claims — a
 * state, an age, `💬 8 · 🎙 2` — on an item holding none of the messages they
 * describe, with an `evidence_id` that resolves to nothing. The card then renders
 * a blocked claim with no path to the message that proves it, which is the one
 * thing this product must never do. Reachable without contrivance: the timeout is
 * shared across all N calls, and a re-cluster between the digest and the item
 * calls 404s them. Failing loudly hands it to the boot handler in app.ts.
 */
export async function fetchLedger(cfg: ApiConfig): Promise<Ledger> {
  warnedNoEpoch = false;

  const digest = await get<{
    built_at: string;
    window_days: number;
    corpus_size: number;
    topics: ApiTopic[];
  }>(cfg, '/api/digest');

  const topics = digest.topics ?? [];
  const details = await Promise.all(
    topics.map((t) => get<ApiItemDetail>(cfg, `/api/item/${t.key}`)),
  );

  const items = topics.map((t, i) => toWorkItem(t, details[i] as ApiItemDetail, cfg));

  // Same invariant from the other side: an item with no messages cannot prove
  // its own state, so it must not reach a renderer at all. check-api.ts already
  // treats an unresolvable evidence_id as a hard failure; the boot path should
  // not be laxer than the checker.
  const empty = items.filter((i) => i.messages.length === 0).map((i) => i.key);
  if (empty.length) {
    throw new Error(
      `/api/item ไม่มีข้อความให้ ${empty.join(', ')} — item ที่ไม่มีข้อความพิสูจน์ state ตัวเองไม่ได้`,
    );
  }

  return {
    built_at: digest.built_at,
    window_days: digest.window_days,
    corpus_size: digest.corpus_size,
    // The pipeline does not expose unplaced threads over HTTP yet. Empty is the
    // truthful value here; do not backfill it from the fixture, or the bot would
    // claim the API said something it did not.
    unassigned: [],
    items,
    // data.ts fills these three; the pipeline has no counterpart for them.
    decisions: [],
    drifts: [],
    standups: [],
  };
}

/**
 * Recall through the pipeline: embeddings + BM25 + signals, not trigrams.
 *
 * Two calls, because they answer different questions and only one of them has a
 * calibrated answer:
 *
 *   preset=hybrid  ranks well (RRF over dense + BM25 + structural signals)
 *   preset=dense   returns a raw cosine, which is the only number here that
 *                  means anything on its own
 *
 * The hybrid `score` is rank-derived, so it cannot say whether anything matched at
 * all. Measured on the 42-record corpus, a nonsense query and a good one both come
 * back at exactly 0.032787 — identical, because rank 1 is rank 1 either way.
 * `why.dense` is no better; it is normalised so the top hit is always 1.00.
 * Without a gate, recall answers every query with five confident rows, which is
 * exactly the black-box behaviour this product exists to avoid.
 *
 * So: ask `dense` how close the nearest record actually is, and if nothing is
 * close, report nothing without ranking anything.
 *
 * The two calls run in sequence, not in parallel. Issuing them together made the
 * server load its embedding model twice at once — FastAPI runs sync endpoints in
 * a threadpool, and on a cold server both requests raced the lazy load — which
 * doubled peak memory and killed the process. Sequential also means a query that
 * fails the gate never pays for the ranking call.
 *
 * The gate is only as good as the model. pipeline/README.md's "Known limitations"
 * holds the measurement — one place, so the two documents cannot drift. Its
 * headline, via preset=dense on the 42-record corpus at the default 0.45 floor:
 *
 *   model                          gibberish   'Android Profile bug fixed?'
 *   paraphrase-multilingual-MiniLM     0.388       0.847      separable
 *   models/syn_finetuned               0.738       0.838      not separable
 *
 * The fine-tune pulled everything together, gibberish included, leaving 0.10 with
 * the floor below both. Serve the general model unless a fine-tune has been
 * checked against queries that should match nothing — and never raise the floor to
 * hide it. The margin also shrinks with the corpus: `npm run check-api` scores
 * three gibberish probes and prints each one, because on a small corpus the answer
 * flips depending on which string you picked.
 */
export async function searchViaApi(
  cfg: ApiConfig,
  query: string,
  k: number,
): Promise<ApiSearchHit[]> {
  const q = encodeURIComponent(query);

  const nearest = await get<{ hits: ApiSearchHit[] }>(cfg, `/api/search?q=${q}&k=1&preset=dense`);
  const top = nearest.hits?.[0]?.score ?? 0;
  if (top < cfg.minCosine) return [];

  const ranked = await get<{ hits: ApiSearchHit[] }>(cfg, `/api/search?q=${q}&k=${k}`);
  return ranked.hits ?? [];
}

export function hitToMessage(hit: ApiSearchHit, cfg: ApiConfig): Message {
  return toMessage(
    { id: hit.id, when: hit.when, user: hit.user, source: hit.source, text: hit.text },
    cfg,
  );
}

/** One-line probe used at boot so a misconfigured URL fails loudly, not later. */
export async function ping(cfg: ApiConfig): Promise<{ corpus_size: number; topics: number }> {
  const d = await get<{ corpus_size: number; topics: unknown[] }>(cfg, '/api/digest');
  return { corpus_size: d.corpus_size, topics: (d.topics ?? []).length };
}
