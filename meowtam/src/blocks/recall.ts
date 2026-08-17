import type { KnownBlock } from '@slack/types';
import { search, searchDecisions } from '../search.js';
import { decisionChain, findMessage } from '../data.js';
import { SOURCE_ICON, clamp, context, divider, esc, header, section } from './common.js';

/**
 * Recall — the fix for "we decided this three months ago and now it's changed,
 * and we can't find it".
 *
 * Two things Slack search cannot do, and both are on screen here:
 *   1. It searches across sources, with no time cutoff and no recency bias
 *      strong enough to bury the thing from May.
 *   2. It knows about *supersession* — that a decision was later replaced —
 *      and shows the chain rather than a pile of equally-plausible hits.
 *
 * Scores are raw (~0.03–0.4). Never render them as a percentage or a progress
 * bar; they are not calibrated and dressing them up would be a lie.
 */
export function recallBlocks(query: string): KnownBlock[] {
  const decisions = searchDecisions(query);
  const hits = search(query, 6);

  const blocks: KnownBlock[] = [
    header('Recall'),
    context(`ค้นจาก: “${esc(clamp(query, 200))}”`),
  ];

  if (!decisions.length && !hits.length) {
    blocks.push(
      section(
        '*ไม่เจออะไรที่เกี่ยวข้อง*\nลองวางข้อความยาวขึ้น — ระบบนี้ทำงานกับย่อหน้าเต็ม ๆ ได้ดีกว่าคำเดี่ยว ๆ',
      ),
    );
    return blocks;
  }

  for (const { decision } of decisions) {
    const chain = decisionChain(decision);
    if (chain.length < 1) continue;
    blocks.push(divider());
    blocks.push(
      section(
        chain.length > 1
          ? `*🧠 การตัดสินใจ — เปลี่ยนมาแล้ว ${chain.length} ครั้ง*`
          : '*🧠 การตัดสินใจ*',
      ),
    );
    chain.forEach((d, idx) => {
      const isCurrent = idx === chain.length - 1;
      const m = findMessage(d.evidence_id);
      const tag = isCurrent ? '  ← *ปัจจุบัน*' : '  ~(ถูกแทนที่แล้ว)~';
      blocks.push(
        section(
          `*${d.when}* · ${SOURCE_ICON[d.source]} ${esc(d.user)}${tag}\n“${esc(clamp(d.statement, 300))}”`,
          m?.permalink
            ? { type: 'button', text: { type: 'plain_text', text: 'ที่มา' }, url: m.permalink }
            : undefined,
        ),
      );
      if (d.related_items?.length) {
        blocks.push(context(`เกี่ยวกับ: ${d.related_items.join(', ')}`));
      }
    });
  }

  if (hits.length) {
    blocks.push(divider());
    blocks.push(section(`*ข้อความที่เกี่ยวข้อง (${hits.length})*`));
    for (const h of hits) {
      const m = h.message;
      blocks.push(
        section(
          `${SOURCE_ICON[m.source]} *${esc(m.user)}* · ${m.when}` +
            (h.item_key ? ` · ${h.item_key}` : '') +
            `\n${esc(clamp(m.text, 260))}`,
          m.permalink
            ? { type: 'button', text: { type: 'plain_text', text: 'เปิด' }, url: m.permalink }
            : undefined,
        ),
      );
      // Showing *why* it matched is what separates this from a black box.
      const why =
        `ตรงกัน: n-gram ${h.why.ngram.toFixed(2)} · คำตรงตัว ${h.why.terms.toFixed(2)} · ` +
        `ความใหม่ ${h.why.recency.toFixed(2)} · รวม ${h.score.toFixed(3)}`;
      blocks.push(context(h.terms.length ? `${why}\nคำที่ตรง: ${h.terms.map((t) => `\`${t}\``).join(' ')}` : why));
    }
  }

  return blocks.slice(0, 48);
}
