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

export function apiConfig(): ApiConfig | null {
  const baseUrl = process.env.TAM_API_URL?.trim().replace(/\/+$/, '');
  if (!baseUrl) return null;
  return {
    baseUrl,
    workspace: (process.env.SLACK_WORKSPACE_URL?.trim() || 'https://slack.com').replace(/\/+$/, ''),
    staleDays: Number(process.env.TAM_STALE_DAYS ?? 3) || 3,
    minCosine: Number(process.env.TAM_MIN_COSINE ?? 0.45) || 0.45,
    timeoutMs: Number(process.env.TAM_API_TIMEOUT_MS ?? 20_000) || 20_000,
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

function daysSince(when: string): number {
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
function stateFor(apiState: string, last: string, staleDays: number): State {
  if (apiState === 'blocked') return 'blocked';
  if (apiState === 'resolved') return 'done';
  return daysSince(last) > staleDays ? 'stalled' : 'moving';
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
  label: string;
  state: string;
  evidence: string;
  evidence_id: string;
  participants: string[];
  sources: Record<string, number>;
  first: string;
  last: string;
  age_days: number;
  summary?: ApiSummary | null;
}

interface ApiSummary {
  detail: string;
  next_step?: string;
  citations: string[];
  unverified: boolean;
}

interface ApiMessage {
  id: string;
  when: string;
  user?: string | null;
  source?: string | null;
  text: string;
}

const EMPTY_DETAIL: {
  timeline: ApiTimelineEntry[];
  messages: ApiMessage[];
  summary?: ApiSummary | null;
} = { timeline: [], messages: [], summary: null };

interface ApiTimelineEntry {
  relation: string;
  when: string;
  to_id?: string;
  to_text?: string;
  to_user?: string | null;
  evidence?: string;
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
    .map((e) => ({
      when: e.when,
      kind: KIND_BY_RELATION[e.relation] ?? 'status_change',
      text: e.evidence ? `${e.to_text} — ${e.evidence}` : (e.to_text as string),
      source: sourceFor(e.to_id as string),
      user: e.to_user?.trim() || 'unknown',
      evidence_id: e.to_id,
    }));
}

function toWorkItem(
  topic: ApiTopic,
  detail: { timeline: ApiTimelineEntry[]; messages: ApiMessage[]; summary?: ApiSummary | null },
  cfg: ApiConfig,
): WorkItem {
  const summary = detail.summary ?? topic.summary ?? null;
  const messages = (detail.messages ?? []).map((m) => toMessage(m, cfg));

  // The pipeline only produces an evidence sentence for a *state change* —
  // blocked or resolved. An active topic has none, and the bot renders the
  // evidence line verbatim, so passing the empty string through would print a
  // blank claim. Anchor those on the newest message instead: "nothing has
  // changed state, here is the last thing that happened" is both true and
  // clickable, which is the bar every rendered claim has to clear.
  let evidence = topic.evidence?.trim() ?? '';
  let evidenceId = topic.evidence_id?.trim() ?? '';
  if (!evidence || !evidenceId) {
    const newest = messages.reduce<Message | undefined>(
      (acc, m) => (!acc || m.when > acc.when ? m : acc),
      undefined,
    );
    if (newest) {
      evidence = evidence || `ยังไม่มีสัญญาณเปลี่ยนสถานะ — ล่าสุด ${newest.when}`;
      evidenceId = evidenceId || newest.id;
    }
  }

  return {
    // The pipeline keys topics by integer. Prefix so the key never collides with
    // a real YouTrack issue key, and so `/meowtam TAM-3` reads as deliberate.
    key: `TAM-${topic.key}`,
    headline: topic.label,
    state: stateFor(topic.state, topic.last, cfg.staleDays),
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
 */
export async function fetchLedger(cfg: ApiConfig): Promise<Ledger> {
  const digest = await get<{
    built_at: string;
    window_days: number;
    corpus_size: number;
    topics: ApiTopic[];
  }>(cfg, '/api/digest');

  const topics = digest.topics ?? [];
  const details = await Promise.all(
    topics.map((t) =>
      get<{ timeline: ApiTimelineEntry[]; messages: ApiMessage[]; summary?: ApiSummary | null }>(
        cfg,
        `/api/item/${t.key}`,
      ).catch(() => ({ timeline: [], messages: [], summary: t.summary ?? null })),
    ),
  );

  return {
    built_at: digest.built_at,
    window_days: digest.window_days,
    corpus_size: digest.corpus_size,
    // The pipeline does not expose unplaced threads over HTTP yet. Empty is the
    // truthful value here; do not backfill it from the fixture, or the bot would
    // claim the API said something it did not.
    unassigned: [],
    items: topics.map((t, i) => toWorkItem(t, details[i] ?? EMPTY_DETAIL, cfg)),
    // data.ts fills these three; the pipeline has no counterpart for them.
    decisions: [],
    drifts: [],
    standups: [],
  };
}

/**
 * Recall through the pipeline: embeddings + BM25 + signals, not trigrams.
 *
 * Two calls, in parallel, because they answer different questions and only one
 * of them has a calibrated answer:
 *
 *   preset=hybrid  ranks well (RRF over dense + BM25 + structural signals)
 *   preset=dense   returns a raw cosine, which is the only number here that
 *                  means anything on its own
 *
 * The hybrid `score` is rank-derived, so it cannot say whether anything matched
 * at all — measured on this corpus, a nonsense query scores 0.0301 and a good
 * one 0.0306. `why.dense` is no better; it is normalised so the top hit is
 * always 1.00. Without a gate, recall answers every query with five confident
 * rows, which is exactly the black-box behaviour this product exists to avoid.
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
 * The gate is only as good as the model. Measured on the sample corpus:
 *
 *   model                          nonsense   real (EN)   real (TH)
 *   paraphrase-multilingual-MiniLM     0.375       0.723       0.771
 *   models/syn_finetuned               0.743       0.725       0.869
 *
 * The fine-tuned model puts nonsense *above* a genuine English query, so no
 * threshold separates them and the gate silently stops working. That is a
 * property of the model, not of this code — serve a general model unless a
 * fine-tune has been checked against queries that should match nothing.
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
