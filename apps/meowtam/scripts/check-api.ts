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
import { apiConfig, ping } from '../src/tam-api.js';
import { hydrate, ledgerOrigin, sortedItems } from '../src/data.js';
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

const boot = await hydrate();
if (boot.error) {
  console.error(`✕ hydrate ล้ม: ${boot.error}`);
  process.exit(1);
}
if (ledgerOrigin() !== 'pipeline') {
  console.error('✕ ยังใช้ fixture อยู่ ไม่ได้มาจาก pipeline');
  process.exit(1);
}

const l = boot.ledger;
console.log(`✓ ledger จาก pipeline — สร้างเมื่อ ${l.built_at}, หน้าต่าง ${l.window_days} วัน`);
console.log(
  `  ${l.items.length} work item · carry-over จาก fixture: ` +
    `${l.decisions.length} decision, ${l.drifts.length} drift, ${l.standups.length} standup\n`,
);

let missingEvidence = 0;
let withPermalink = 0;
let totalMessages = 0;

for (const item of sortedItems(l.items)) {
  const resolves = item.messages.some((m) => m.id === item.evidence_id);
  if (!resolves) missingEvidence++;
  totalMessages += item.messages.length;
  withPermalink += item.messages.filter((m) => m.permalink).length;

  const flag = resolves ? ' ' : '!';
  console.log(
    `${flag} ${item.key.padEnd(7)} ${item.state.padEnd(7)} ${String(item.age_days).padStart(4)}d  ` +
      `${item.messages.length} ข้อความ  ${item.headline.slice(0, 46)}`,
  );
  console.log(`          หลักฐาน: ${item.evidence.slice(0, 78)}`);
  if (item.summary) {
    const tag = item.summary.unverified ? 'unverified' : `${item.summary.citations.length} citations`;
    console.log(`          สรุป (${tag}): ${item.summary.detail.slice(0, 70)}`);
  }
}

console.log(`\n  permalink สร้างได้ ${withPermalink}/${totalMessages} ข้อความ`);
console.log('  (ข้อความจากที่ประชุมไม่มี permalink ตามคาด — ไม่ได้มาจาก Slack)');

if (missingEvidence) {
  console.error(`\n✕ ${missingEvidence} item ที่ evidence_id ไม่ตรงกับข้อความใน item นั้น`);
  process.exit(1);
}
console.log('✓ ทุก item: evidence_id ชี้ไปข้อความที่มีอยู่จริงใน item');

const q = process.argv[2] ?? 'Profile module bug บน Android';
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
const NONSENSE = 'qqqzzzxxx wvwvwv jjjkkk zzzqqq';
console.log(`\n→ calibration — query ที่ไม่มีความหมาย: “${NONSENSE}”`);
const junk = await searchBest(NONSENSE, 5);
if (junk.length === 0) {
  console.log(`✓ gate ทำงาน — ไม่คืนผลลัพธ์ (floor: cosine ${cfg.minCosine})`);
} else {
  console.warn(`✕ gate ไม่ทำงาน — คืน ${junk.length} ผลลัพธ์ให้ query ที่ไม่มีความหมาย`);
  console.warn('  โมเดลที่เสิร์ฟอยู่วางข้อความขยะไว้ใกล้ corpus เกินไป');
  console.warn('  วัดดูเองได้: curl "<url>/api/search?preset=dense&k=1&q=<ขยะ>" แล้วดู score');
  console.warn(`  ถ้าค่าที่ได้สูงกว่า ${cfg.minCosine} ให้เปลี่ยน EMBEDDING_MODEL ฝั่ง server`);
  console.warn('  เป็นโมเดลทั่วไป — อย่าดัน TAM_MIN_COSINE ขึ้นไปกลบอาการ');
}

process.exit(viaPipeline && junk.length === 0 ? 0 : 1);
