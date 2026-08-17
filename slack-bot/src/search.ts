/**
 * Recall over the ledger.
 *
 * Deliberately dependency-free and offline: no embedding API key, no network,
 * nothing that can fail in front of judges. It is a hybrid of two cheap signals
 * that between them handle Thai and English in the same sentence:
 *
 *   1. Character 3-gram overlap (cosine over tf vectors).
 *      Thai has no spaces between words, so word tokenisation without a Thai
 *      tokeniser produces garbage. Character n-grams sidestep that entirely and
 *      degrade gracefully on English too.
 *   2. Literal term overlap on latin/digit runs (product names, ticket keys,
 *      "Agentforce", "UTF-8"), which n-grams alone under-weight.
 *
 * Plus a mild recency prior, because two equally-matching messages are not
 * equally useful — but it is *mild* on purpose. The whole point of recall is
 * finding the thing from three months ago.
 *
 * If you later want real semantics, swap `score()` for an embedding call and
 * keep everything else. The shape of `Hit` already carries per-stage scores so
 * the UI can keep showing *why* something matched.
 */

import { ledger, itemKeyForMessage } from './data.js';
import { apiConfig, hitToMessage, searchViaApi } from './tam-api.js';
import type { Message } from './types.js';

export interface Hit {
  message: Message;
  item_key?: string;
  score: number;
  /**
   * Per-stage scores, keyed by stage name. Open-ended because the stages differ
   * by engine: the local one reports ngram/terms/recency, the pipeline reports
   * dense/bm25/anchor/thread/…. The UI renders whatever is present so it keeps
   * showing *why* something matched either way.
   */
  why: Record<string, number>;
  /** Literal tokens that matched — rendered as chips so the reader can judge the match. */
  terms: string[];
  /** Which engine produced this hit. Rendered, so the reader is never misled. */
  engine: 'local' | 'pipeline';
}

const NGRAM = 3;

/**
 * Minimum content similarity before a message counts as a hit at all.
 * Tuned against the fixture: real Thai queries land 0.10–0.30, unrelated
 * text lands below 0.04. Raise it if recall feels noisy, lower it if the
 * empty state shows up when it shouldn't.
 */
const CONTENT_FLOOR = 0.04;

function grams(s: string): Map<string, number> {
  const norm = s.toLowerCase().replace(/\s+/g, ' ').trim();
  const out = new Map<string, number>();
  for (let i = 0; i <= norm.length - NGRAM; i++) {
    const g = norm.slice(i, i + NGRAM);
    out.set(g, (out.get(g) ?? 0) + 1);
  }
  return out;
}

function cosine(a: Map<string, number>, b: Map<string, number>): number {
  let dot = 0;
  // Iterate the smaller map — queries are usually shorter than documents.
  const [small, large] = a.size <= b.size ? [a, b] : [b, a];
  for (const [g, av] of small) {
    const bv = large.get(g);
    if (bv) dot += av * bv;
  }
  if (dot === 0) return 0;
  let na = 0;
  let nb = 0;
  for (const v of a.values()) na += v * v;
  for (const v of b.values()) nb += v * v;
  return dot / (Math.sqrt(na) * Math.sqrt(nb));
}

/** Latin words and digit runs of length >= 3. Thai is handled by the n-grams. */
function latinTerms(s: string): Set<string> {
  return new Set(s.toLowerCase().match(/[a-z0-9][a-z0-9._-]{2,}/g) ?? []);
}

function daysBetween(a: string, b: string): number {
  const ms = Math.abs(new Date(a.replace(' ', 'T')).getTime() - new Date(b.replace(' ', 'T')).getTime());
  return ms / 86_400_000;
}

export function search(query: string, k = 8): Hit[] {
  const l = ledger();
  const qGrams = grams(query);
  const qTerms = latinTerms(query);

  const corpus: Array<{ m: Message; item?: string }> = [];
  for (const item of l.items) for (const m of item.messages) corpus.push({ m, item: item.key });
  for (const m of l.unassigned) corpus.push({ m });

  const newest = corpus.reduce((acc, c) => (c.m.when > acc ? c.m.when : acc), l.built_at);

  const hits: Hit[] = corpus.map(({ m, item }) => {
    const ngram = cosine(qGrams, grams(m.text));

    const mTerms = latinTerms(m.text);
    const shared = [...qTerms].filter((t) => mTerms.has(t));
    const terms = qTerms.size ? shared.length / qTerms.size : 0;

    // Half-life of 120 days. Recency *modulates* a content match, it never
    // creates one — otherwise a query matching nothing still scores ~0.1 on
    // every recent message and the empty state never renders.
    const recency = Math.exp(-daysBetween(newest, m.when) / 120);

    const content = 0.65 * ngram + 0.35 * terms;
    const score = content < CONTENT_FLOOR ? 0 : content * (0.9 + 0.1 * recency);
    return {
      message: m,
      item_key: item,
      score,
      why: { ngram, terms, recency },
      terms: shared,
      engine: 'local' as const,
    };
  });

  return hits
    .filter((h) => h.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, k);
}

/**
 * Recall through the Python pipeline when it is configured, else locally.
 *
 * This is the swap the header comment above anticipated: the pipeline scores
 * with a trained embedding model fused with BM25 and Slack's own structural
 * signals, which the trigram engine cannot approximate — it has no notion of
 * two differently-worded messages meaning the same thing.
 *
 * It does **not** quietly fall back to trigrams when the pipeline fails. The two
 * engines answer differently enough that silently swapping them makes recall
 * unexplainable: the same query returns different rows on different days with no
 * way to tell why, and the `why` breakdown on screen would name stages that did
 * not run. A caller that wants the local engine asks for `search()` by name.
 * Errors propagate so the command can say the pipeline is down.
 */
export async function searchBest(query: string, k = 8): Promise<Hit[]> {
  const cfg = apiConfig();
  if (!cfg) return search(query, k);

  const hits = await searchViaApi(cfg, query, k);
  return hits.map((h) => ({
    message: hitToMessage(h, cfg),
    item_key: itemKeyForMessage(h.id),
    score: h.score,
    why: h.why ?? {},
    terms: h.terms ?? [],
    engine: 'pipeline' as const,
  }));
}

/** Decisions whose statement matches the query, for the supersession chain in recall. */
export function searchDecisions(query: string, k = 3) {
  const qGrams = grams(query);
  const qTerms = latinTerms(query);
  return ledger()
    .decisions.map((d) => {
      const mTerms = latinTerms(d.statement);
      const shared = [...qTerms].filter((t) => mTerms.has(t));
      const score =
        0.7 * cosine(qGrams, grams(d.statement)) + 0.3 * (qTerms.size ? shared.length / qTerms.size : 0);
      return { decision: d, score };
    })
    .filter((r) => r.score > 0.04)
    .sort((a, b) => b.score - a.score)
    .slice(0, k);
}
