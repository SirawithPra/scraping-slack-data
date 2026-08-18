/**
 * Prove the bot can read the Python pipeline — without Slack in the loop.
 *
 *     TAM_API_URL=http://127.0.0.1:8899 npm run check-api
 *
 * Slack tokens are the slowest thing to get right, and they have nothing to do
 * with whether the two halves agree on a work item. This exercises the whole
 * path the bot uses at boot — fetch, translate, index, search — and prints what
 * came back, so a shape mismatch shows up here instead of as an empty digest in
 * front of an audience.
 *
 * Exits non-zero on failure so it can gate a demo.
 */

import 'dotenv/config';
import { apiConfig, hitToMessage, passesGate, ping, searchViaApi } from '../src/tam-api.js';
import { hydrate, ledger, ledgerOrigin, sortedItems } from '../src/data.js';
import { searchBest } from '../src/search.js';

const cfg = apiConfig();
if (!cfg) {
  console.error('✕ TAM_API_URL ไม่ได้ตั้ง — ไม่มีอะไรให้ตรวจ');
  console.error('  ลอง: TAM_API_URL=http://127.0.0.1:8899 npm run check-api');
  process.exit(2);
}

console.log(`→ pipeline: ${cfg.baseUrl}`);
console.log(`  stale threshold: ${cfg.staleDays} วัน · permalink host: ${cfg.workspace}\n`);

try {
  const p = await ping(cfg);
  console.log(`✓ ตอบแล้ว — ${p.corpus_size} ข้อความ, ${p.topics} topic\n`);
} catch (err) {
  console.error(`✕ ต่อไม่ได้: ${(err as Error).message}`);
  console.error('  server รันอยู่ไหม? python3 -m tam.web.server --records <ไฟล์> --port 8899');
  process.exit(1);
}

let boot;
try {
  boot = await hydrate();
} catch (err) {
  console.error(`✕ hydrate ล้ม: ${(err as Error).message}`);
  process.exit(1);
}
if (ledgerOrigin() !== 'pipeline') {
  console.error('✕ ยังใช้ fixture อยู่ ไม่ได้มาจาก pipeline');
  process.exit(1);
}

const l = boot.ledger;
console.log(`✓ ledger จาก pipeline — สร้างเมื่อ ${l.built_at}, หน้าต่าง ${l.window_days} วัน`);
console.log(`  ${l.items.length} work item จาก pipeline`);
console.log(
  `  ที่บอทเติมเอง: ${l.decisions.length} decision (คนกดบันทึก), ` +
    `${l.standups.length} standup draft (คำนวณจาก item), ` +
    `${l.drifts.length} drift${l.drifts.length === 0 ? ' จาก ledger — การเทียบ ticket จริงอยู่ที่ /api/tracker' : ''}\n`,
);

/**
 * The raw digest, next to the mapped ledger.
 *
 * `evidence_id resolves in messages` is nearly a tautology: the server only
 * considers relations whose endpoints are both inside the topic, and the bot's own
 * fallback id is copied out of the same message list. So it passes while the
 * evidence link points at the wrong message. What the mapper can actually get
 * wrong is checked below, against the untranslated JSON — dropping fields is the
 * failure mode of a translation layer, and a dropped field is invisible from the
 * mapped side.
 */
const raw = await fetch(`${cfg.baseUrl}/api/digest`).then((r) => r.json() as Promise<any>);
const rawTopics: any[] = raw.topics ?? [];
const rawByKey = new Map<string, any>(rawTopics.map((t: any) => [`TAM-${t.key}`, t]));

const MAPPED_TOPIC_KEYS = new Set([
  'key', 'item_id', 'label', 'state', 'evidence', 'evidence_id', 'participants',
  'sources', 'first', 'first_ts', 'last', 'last_ts', 'age_days', 'summary', 'messages',
]);
// What `ApiSummary` declares and the mapper actually reads, plus `key` — the topic
// index repeated inside the summary, with nothing to render. Anything else the
// server sends reaches the type boundary and stops there, which is what the warning
// is for: a field the pipeline computes and the bot silently discards is a claim
// nobody can see. Keep this list in step with ApiTopic/ApiSummary in src/tam-api.ts.
const MAPPED_SUMMARY_KEYS = new Set([
  'key', 'headline', 'detail', 'next_step', 'citations', 'unverified', 'backend',
]);

const unmapped = [...new Set(rawTopics.flatMap((t: any) => Object.keys(t)))].filter(
  (k) => !MAPPED_TOPIC_KEYS.has(k),
);
const unmappedSummary = [
  ...new Set(rawTopics.flatMap((t: any) => Object.keys(t.summary ?? {}))),
].filter((k) => !MAPPED_SUMMARY_KEYS.has(k));

for (const [where, keys, type] of [
  ['/api/digest topic', unmapped, 'ApiTopic'],
  ['topic.summary', unmappedSummary, 'ApiSummary'],
] as const) {
  if (!keys.length) continue;
  console.warn(`⚠  ${where} ส่ง field ที่ฝั่งบอทไม่ได้อ่าน: ${keys.join(', ')}`);
  console.warn(`   (ถ้ามันสำคัญ ต้องเติมใน ${type} ใน src/tam-api.ts — ตอนนี้ถูกทิ้งเงียบ ๆ)`);
}
if (typeof raw.summariser === 'string') {
  console.log(`  ผู้เขียนสรุปฝั่ง pipeline: ${raw.summariser}`);
}

let missingEvidence = 0;
let wrongEvidence = 0;
let countMismatch = 0;
let danglingCitations = 0;
let danglingTimeline = 0;
let withPermalink = 0;
let totalMessages = 0;

/** The sentence tam-api.ts synthesises when the pipeline has no state change to cite. */
const SYNTHESIZED = 'ยังไม่มีสัญญาณเปลี่ยนสถานะ — ล่าสุด ';

for (const item of sortedItems(l.items)) {
  const ev = item.messages.find((m) => m.id === item.evidence_id);
  if (!ev) missingEvidence++;
  totalMessages += item.messages.length;
  withPermalink += item.messages.filter((m) => m.permalink).length;

  // A synthesised evidence sentence quotes a timestamp. If the id next to it
  // belongs to a different message, the card links a claim to the wrong proof —
  // which is the one thing this product is not allowed to do.
  if (item.evidence.startsWith(SYNTHESIZED)) {
    const quoted = item.evidence.slice(SYNTHESIZED.length).trim();
    if (ev && ev.when !== quoted) {
      wrongEvidence++;
      console.error(`! ${item.key}: หลักฐานบอกเวลา ${quoted} แต่ id ชี้ไปข้อความเวลา ${ev.when}`);
    }
  }

  // The digest's own message count against the messages actually mapped in.
  const rawTopic = rawByKey.get(item.key);
  const rawCount = Number(rawTopic?.messages);
  if (Number.isFinite(rawCount) && rawCount !== item.messages.length) {
    countMismatch++;
    console.error(`! ${item.key}: digest บอก ${rawCount} ข้อความ แต่ mapped มา ${item.messages.length}`);
  }

  // Every id the bot renders as clickable has to resolve to a message it holds.
  const ids = new Set(item.messages.map((m) => m.id));
  const badCites = (item.summary?.citations ?? []).filter((id) => !ids.has(id));
  const badTimeline = item.timeline.map((t) => t.evidence_id).filter((id) => id && !ids.has(id));
  danglingCitations += badCites.length;
  danglingTimeline += badTimeline.length;

  const flag = ev ? ' ' : '!';
  console.log(
    `${flag} ${item.key.padEnd(7)} ${item.state.padEnd(7)} ${String(item.age_days).padStart(4)}d  ` +
      `${item.messages.length} ข้อความ  ${item.headline.slice(0, 46)}`,
  );
  console.log(`          หลักฐาน: ${item.evidence.slice(0, 78)}`);
  if (item.summary) {
    const backend = (item.summary as { backend?: string }).backend ?? 'ไม่ได้บอก';
    const tag = item.summary.unverified ? 'unverified' : `${item.summary.citations.length} citations`;
    console.log(`          สรุป (${backend}, ${tag}): ${item.summary.detail.slice(0, 70)}`);
    if (badCites.length) console.log(`          ! citation ${badCites.length} อันหาไม่เจอใน item`);
  }
  if (badTimeline.length) console.log(`          ! timeline ${badTimeline.length} อันชี้ไปข้อความที่ไม่มี`);
}

console.log(`\n  permalink สร้างได้ ${withPermalink}/${totalMessages} ข้อความ`);
console.log('  (ข้อความจากที่ประชุมไม่มี permalink ตามคาด — ไม่ได้มาจาก Slack)');

const evidenceProblems = missingEvidence + wrongEvidence + countMismatch + danglingCitations + danglingTimeline;
if (evidenceProblems) {
  if (missingEvidence) console.error(`\n✕ ${missingEvidence} item ที่ evidence_id ไม่ตรงกับข้อความใน item นั้น`);
  if (wrongEvidence) console.error(`✕ ${wrongEvidence} item ที่ประโยคหลักฐานกับ id ไม่ใช่ข้อความเดียวกัน`);
  if (countMismatch) console.error(`✕ ${countMismatch} item ที่จำนวนข้อความไม่ตรงกับที่ digest บอก`);
  if (danglingCitations) console.error(`✕ ${danglingCitations} citation ที่กดดูข้อความจริงไม่ได้`);
  if (danglingTimeline) console.error(`✕ ${danglingTimeline} timeline entry ที่กดดูข้อความจริงไม่ได้`);
  process.exit(1);
}
console.log('✓ ทุก item: หลักฐาน, citation และ timeline ชี้ไปข้อความที่มีอยู่จริงใน item');
console.log('✓ จำนวนข้อความต่อ item ตรงกับที่ /api/digest บอก');

// Skip flags when picking the recall query. Without this, `check-api --strict-gate`
// searched for the literal string "--strict-gate" — a query that legitimately matches
// nothing, so the calibration section then reported on a probe nobody chose.
const q = process.argv.slice(2).find((a) => !a.startsWith('-')) ?? 'Profile module bug บน Android';
console.log(`\n→ recall ผ่าน pipeline: “${q}”`);
const hits = await searchBest(q, 5);
if (!hits.length) {
  console.error('✕ ไม่ได้ผลลัพธ์เลย');
  process.exit(1);
}
for (const h of hits) {
  const why = Object.entries(h.why)
    .map(([k, v]) => `${k} ${v.toFixed(2)}`)
    .join(' · ');
  console.log(`  ${h.score.toFixed(3)}  [${h.engine}] ${h.item_key ?? '—'}  ${h.message.text.slice(0, 58)}`);
  if (why) console.log(`         ${why}`);
}

const viaPipeline = hits.every((h) => h.engine === 'pipeline');
console.log(
  viaPipeline
    ? '\n✓ ทุกผลลัพธ์มาจาก pipeline (embeddings) ไม่ใช่ trigram ในเครื่อง'
    : '\n✕ บางผลลัพธ์ยัง fallback ไป trigram',
);

/**
 * Calibration: a query that means nothing must return nothing.
 *
 * This is the check worth running before a demo, because it fails on the model
 * rather than the code. A collapsed embedding space puts gibberish as close to
 * the corpus as a real question, the cosine gate can then never separate them,
 * and recall answers every query with five confident-looking rows.
 */
/**
 * Several probes, not one. A single gibberish string makes this check a coin
 * flip: `qqqzzzxxx …` scores 0.21 against a 42-record corpus and 0.48 against a
 * 27-record one, because the nearest neighbour of nonsense gets closer as the
 * corpus gets smaller and the tokenizer maps repeated-character runs somewhere
 * unhelpful. What matters is not one verdict but the *margin* — how far the worst
 * nonsense sits below the best real query — so the check measures and prints it.
 */
const NONSENSE_PROBES = [
  'qqqzzzxxx wvwvwv jjjkkk zzzqqq',
  'zxqv frobnicate wibble plumbus grommet',
  'ฟฟฟกกก ผผผ ฃฃฃ ฅฅฅ',
  // The one that defeats a lexical gate, kept in on purpose. ๆ and ฯ are real Thai
  // punctuation that occurs in ordinary messages, so BM25 finds a genuine match and
  // the gate lets it through. Leaving it out would make this check report a clean
  // pass while a known hole stayed open; the run should say what it cannot do.
  'ๆๆๆ ฯฯฯ ฤฤฤ ฅฅฅ',
];
const NONSENSE = NONSENSE_PROBES[0]!;
console.log('\n→ calibration — วัดว่า gate แยก query ขยะออกจาก query จริงได้จริงไหม');

/** Both absolute relevance signals for one probe, as the server computes them. */
async function relevanceOf(api: typeof cfg & {}, probe: string): Promise<{ lexical: number; dense: number }> {
  const url = `/api/search?q=${encodeURIComponent(probe)}&k=1`;
  const res = await fetch(new URL(url, api.baseUrl), { signal: AbortSignal.timeout(api.timeoutMs) });
  if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
  const body = (await res.json()) as { relevance?: { lexical: number; dense: number } };
  if (!body.relevance) throw new Error(`${url} → ไม่มี relevance (pipeline เก่า) — gate ทำงานไม่ได้`);
  return body.relevance;
}

let junkPassed = 0;
let realRejected = 0;
try {
  for (const probe of NONSENSE_PROBES) {
    const r = await relevanceOf(cfg, probe);
    const pass = passesGate(r, cfg.minCosine);
    if (pass) junkPassed += 1;
    console.log(`  bm25 ${r.lexical.toFixed(2).padStart(6)} · cos ${r.dense.toFixed(3)}  ${pass ? '✕ ผ่าน gate' : '· ถูกกรอง'}  “${probe}”`);
  }
  // Several real queries, not one. A floor has to clear the *weakest* genuine
  // query, not the strongest — comparing gibberish against one well-chosen query
  // makes any floor between them look safe while it silently rejects the ordinary
  // ones. The item labels come from the corpus itself, so they are real questions
  // about this data whatever the data is.
  const realProbes = [q, ...ledger().items.map((i) => i.headline).filter(Boolean)].slice(0, 6);
  for (const probe of realProbes) {
    const r = await relevanceOf(cfg, probe);
    const pass = passesGate(r, cfg.minCosine);
    if (!pass) realRejected += 1;
    console.log(`  bm25 ${r.lexical.toFixed(2).padStart(6)} · cos ${r.dense.toFixed(3)}  ${pass ? '· ผ่าน' : '✕ ถูกกรองทิ้ง'}  (จริง) “${probe.slice(0, 40)}”`);
  }
  console.log(`  กฎ: bm25 > 0 และ cos >= ${cfg.minCosine} · ขยะที่หลุด ${junkPassed}/${NONSENSE_PROBES.length} · query จริงที่เสีย ${realRejected}/${realProbes.length}`);
} catch (err) {
  console.error(`✕ calibration วัดไม่ได้ — pipeline ตอบไม่ได้: ${(err as Error).message}`);
  process.exit(1);
}

// Call the API path directly rather than through searchBest(). searchBest returns
// the local trigram engine's results when TAM_API_URL is unset, and that engine
// also returns nothing for gibberish — so an empty result from it would prove
// nothing about the cosine gate. Going straight at the API exercises the gate
// itself, and an unreachable pipeline fails this check loudly instead of quietly
// looking like a pass. (searchBest does *not* fall back on error while the API is
// configured, and must not be made to: see the no-fallback rule in search.ts.)
let junk: Awaited<ReturnType<typeof searchBest>> = [];
try {
  const raw = await searchViaApi(cfg, NONSENSE, 5);
  junk = raw.map((h) => ({
    message: hitToMessage(h, cfg),
    score: h.score,
    why: h.why ?? {},
    terms: h.terms ?? [],
    engine: 'pipeline' as const,
  }));
} catch (err) {
  console.error(`✕ calibration ตรวจไม่ได้ — pipeline ตอบไม่ได้: ${(err as Error).message}`);
  process.exit(1);
}

// The exit code answers "is the integration sound?" — did the pipeline answer, do
// items resolve, does recall come from embeddings. Whether a *floor* separates
// nonsense from real queries is a property of the model and the corpus, not of the
// seam, and on a 27-record sample the answer is legitimately no. Conflating the two
// made the documented quickstart exit 1 while everything it was checking worked.
// `--strict-gate` puts calibration back in the exit code, for a real corpus in CI.
const strictGate = process.argv.includes('--strict-gate');
const gateHolds = junkPassed === 0 && realRejected === 0;
if (gateHolds && junk.length === 0) {
  console.log(`✓ gate ทำงาน — กรองขยะได้ครบ ${NONSENSE_PROBES.length}/${NONSENSE_PROBES.length} และไม่เสีย query จริง`);
} else if (junkPassed > 0 && realRejected === 0) {
  console.warn(`⚠ gate กรองขยะได้ ${NONSENSE_PROBES.length - junkPassed}/${NONSENSE_PROBES.length} — ยังมี ${junkPassed} ตัวหลุด แต่ไม่เสีย query จริงเลย`);
  console.warn('  ตัวที่หลุดมักเป็นอักขระไทยซ้ำ ๆ (ๆ ฯ) ซึ่งมีอยู่ใน corpus จริง จึงมีคำตรงกันจริง');
  console.warn('  นั่นคือขอบเขตของกลไก ไม่ใช่การตั้งค่าผิด — ดู docs/EXPERIMENTS.md');
} else {
  console.warn(`✕ gate ตัด query จริงทิ้ง ${realRejected} อัน — แย่กว่าปล่อยขยะผ่าน`);
  console.warn(`  ลด TAM_MIN_COSINE (ตอนนี้ ${cfg.minCosine}) หรือเช็คว่า corpus มีเรื่องนั้นจริงไหม`);
  console.warn('  อย่าดันขึ้นเพื่อไล่ขยะ — เสีย query จริงคือความเสียหายที่คนใช้เห็น');
}
if (!gateHolds) {
  console.warn(strictGate
    ? '  --strict-gate เปิดอยู่ ข้อนี้จึงนับเป็น fail'
    : '  (ข้อนี้ไม่ทำให้ exit code เป็น 1 — ใส่ --strict-gate ถ้าต้องการให้ fail)');
}

const integrationOk = viaPipeline;
const calibrationOk = gateHolds && junk.length === 0;
process.exit(integrationOk && (calibrationOk || !strictGate) ? 0 : 1);
