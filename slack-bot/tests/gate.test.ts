/**
 * The relevance gate: recall must be able to answer "nothing matched".
 *
 * It used to be a single cosine floor. That cannot work — `max cosine` over N
 * documents rises with N for any query, so with a thousand records something always
 * looks similar, and across five embedding models the rejection rate for gibberish
 * was 0 of 5 every time. The gate now requires a lexical anchor *and* a dense match,
 * which rejected 4 of 5 while losing 0 of 12 real queries (docs/EXPERIMENTS.md).
 *
 * These tests pin the parts that a future edit could quietly undo: that both signals
 * are required, that a server which does not send them is an error rather than a
 * silent pass, and that a rejected query yields no hits rather than unranked ones.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { passesGate, searchWithRelevance, type ApiConfig } from '../src/tam-api.js';

const cfg: ApiConfig = {
  baseUrl: 'http://127.0.0.1:1',
  workspace: 'https://example.slack.com',
  staleDays: 3,
  minCosine: 0.45,
  timeoutMs: 1000,
  adminToken: '',
  writeTimeoutMs: 1000,
};

test('both signals are required, and each one alone is not enough', () => {
  // A real question, in the corpus's own words.
  assert.equal(passesGate({ lexical: 12.5, dense: 0.80 }, 0.45), true);

  // Gibberish: BM25 scores it exactly 0 because it shares no vocabulary. The cosine
  // is high anyway — this is the measured case that a floor alone cannot reject.
  assert.equal(passesGate({ lexical: 0, dense: 0.92 }, 0.45), false);

  // A word that happens to appear, about something the corpus does not discuss.
  assert.equal(passesGate({ lexical: 8.0, dense: 0.20 }, 0.45), false);

  // Neither.
  assert.equal(passesGate({ lexical: 0, dense: 0.10 }, 0.45), false);
});

test('the dense floor is inclusive, so a query exactly at the floor is kept', () => {
  // Rejecting at equality would make TAM_MIN_COSINE mean something subtly different
  // from what .env.example documents, and the difference only shows up on real data.
  assert.equal(passesGate({ lexical: 1, dense: 0.45 }, 0.45), true);
  assert.equal(passesGate({ lexical: 1, dense: 0.4499 }, 0.45), false);
});

test('any lexical overlap at all counts, however small', () => {
  // The threshold on lexical is deliberately "> 0", not a tuned number: 0 means "no
  // word in common", which is the fact being tested. Anything above it is a real
  // match whose strength the dense signal is there to judge.
  assert.equal(passesGate({ lexical: 0.0001, dense: 0.60 }, 0.45), true);
});

/** A stand-in server, so these tests need no pipeline and no network. */
function serve(body: unknown, status = 200): () => void {
  const real = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })) as typeof fetch;
  return () => {
    globalThis.fetch = real;
  };
}

test('a pipeline that does not send relevance is an error, never a silent pass', async () => {
  // The failure this guards: an older server returns hits with no `relevance`, the
  // gate has nothing to read, and recall goes back to answering every query with
  // five confident rows — with nothing on screen to say the gate stopped working.
  const restore = serve({ hits: [{ rank: 1, score: 0.03, id: 'm1', when: '2026-08-18 10:00', text: 'x', why: {}, terms: [] }] });
  try {
    await assert.rejects(
      searchWithRelevance(cfg, 'anything', 5),
      /relevance/,
      'a missing relevance field must throw',
    );
  } finally {
    restore();
  }
});

test('a query the gate refuses comes back with no hits, even though the server sent some', async () => {
  const hit = { rank: 1, score: 0.0328, id: 'm1', when: '2026-08-18 10:00', text: 'x', why: {}, terms: [] };
  const restore = serve({ hits: [hit], relevance: { lexical: 0, dense: 0.93 } });
  try {
    const out = await searchWithRelevance(cfg, 'qqqzzzxxx wvwvwv', 5);
    assert.equal(out.passed, false);
    assert.deepEqual(out.hits, [], 'hits must be dropped, not merely flagged');
    // The numbers survive so a caller can explain *why* nothing came back.
    assert.deepEqual(out.relevance, { lexical: 0, dense: 0.93 });
  } finally {
    restore();
  }
});

test('a query the gate accepts passes the hits through untouched', async () => {
  const hit = { rank: 1, score: 0.0328, id: 'm1', when: '2026-08-18 10:00', text: 'x', why: {}, terms: [] };
  const restore = serve({ hits: [hit], relevance: { lexical: 14.2, dense: 0.71 } });
  try {
    const out = await searchWithRelevance(cfg, 'profile module bug', 5);
    assert.equal(out.passed, true);
    assert.deepEqual(out.hits, [hit]);
  } finally {
    restore();
  }
});
