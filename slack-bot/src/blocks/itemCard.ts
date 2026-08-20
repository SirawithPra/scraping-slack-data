import type { KnownBlock } from '@slack/types';
import type { WorkItem } from '../types.js';
import { decisionChain, decisionsFor, findMessage, ledgerOrigin, refreshStatus } from '../data.js';
import {
  CMD, STATE_LABEL, bodyText, clamp, context, days, divider, esc,
  evidenceButton, header, people, quote, section, sourceCounts, sourceIcon, ticketButton, who,
} from './common.js';
import { displayName } from '../names.js';

const KIND_ICON: Record<string, string> = {
  status_change: '🔁',
  blocked: '⛔',
  unblocked: '🔓',
  scope_change: '✏️',
  decision: '🧠',
  commit: '⌗',
};

/**
 * How many filed decisions the card shows before pointing at recall.
 *
 * Three is the point past which the card stops being "what was decided" and turns
 * into a second timeline. The rest are not hidden — the count and the command to
 * see them are printed.
 */
const MAX_DECISIONS = 3;

/**
 * How the summary line is labelled.
 *
 * The label has to name the *actual* writer, not "a model" as a figure of speech.
 * The shipped default summariser is `template`, a rule-based sentence assembled
 * from counts and the state cue with no model in the loop — calling that "สรุปโดย
 * โมเดล" credits an LLM for deterministic text and, worse, leaves an operator with
 * no surface anywhere that says whether a model is actually running.
 *
 * The pipeline sends the writer as `summary.backend`. When it is absent (the
 * fixture, or an older server) we say we do not know rather than guess: this is
 * the one line on the card that is not a computed fact, so its provenance is the
 * whole reason it is allowed on screen at all.
 */
function summaryProvenance(summary: WorkItem['summary'] & {}): string {
  const backend = summary.backend?.trim();
  const tail = ' · ข้อมูลด้านบนคำนวณจากข้อมูลจริง';
  if (!backend) return `สรุปอัตโนมัติ — pipeline ไม่ได้บอกว่าใครเขียน${tail}`;
  if (backend === 'template') return `สรุปจากกฎ (template) — ไม่ผ่านโมเดล${tail}`;
  return `สรุปโดยโมเดล (${esc(backend)})${tail}`;
}

/**
 * The full ledger card for one work item.
 *
 * Note the ordering: computed facts first (state, evidence, timeline), the
 * summary last and explicitly labelled with who wrote it. The derived facts are
 * the trustworthy part and should read as facts; the prose is the smallest part
 * of the product and should not be mistaken for the source of truth.
 */
export function itemCardBlocks(item: WorkItem): KnownBlock[] {
  const ev = findMessage(item.evidence_id);

  const blocks: KnownBlock[] = [
    header(`${item.key} · ${clamp(item.headline, 100)}`),
    section(
      `*${STATE_LABEL[item.state]}*  ·  _${days(item.age_days)}_\n${esc(item.evidence)}`,
      ticketButton(item),
    ),
  ];

  if (ev) {
    blocks.push(context(`หลักฐาน: ${sourceIcon(ev.source)} *${who(ev.user)}* · ${ev.when}`));
    blocks.push({
      type: 'actions',
      elements: [evidenceButton(ev.id, ev.permalink) as any],
    } as KnownBlock);
  }

  blocks.push(
    context(
      `${sourceCounts(item)}  ·  ${item.first} → ${item.last}` +
        (item.assignee ? `  ·  รับผิดชอบ: ${who(item.assignee)}` : '') +
        (item.youtrack_status ? `  ·  YouTrack: ${esc(item.youtrack_status)}` : ''),
    ),
  );
  blocks.push(context(`เกี่ยวข้อง: ${people(item.participants)}`));

  if (item.timeline.length) {
    blocks.push(divider());
    blocks.push(section('*ไทม์ไลน์*'));
    for (const t of item.timeline.slice(0, 10)) {
      const m = t.evidence_id ? findMessage(t.evidence_id) : undefined;
      const link = m?.permalink ? ` <${m.permalink}|↗>` : '';
      blocks.push(
        context(
          `${KIND_ICON[t.kind] ?? '•'} *${t.when}*  ${sourceIcon(t.source)} ${who(t.user)} — ${bodyText(t.text, 200)}${link}`,
        ),
      );
    }
  }

  // What was *decided*, as opposed to what happened. These are filed by a human
  // through 🧠 or the message menu; until now the only way to read one back was to
  // guess a recall query close enough to its wording, so a decision attached to this
  // exact item was invisible on the card describing that item.
  const decisions = decisionsFor(item.key);
  if (decisions.length) {
    blocks.push(divider());
    blocks.push(section('*🧠 การตัดสินใจที่บันทึกไว้*'));
    for (const d of decisions.slice(0, MAX_DECISIONS)) {
      const src = findMessage(d.evidence_id);
      blocks.push(
        section(
          `*${esc(d.when)}* · ${sourceIcon(d.source)} ${who(d.user)}\n“${bodyText(d.statement, 240)}”`,
          src?.permalink
            ? { type: 'button', text: { type: 'plain_text', text: 'ที่มา' }, url: src.permalink }
            : undefined,
        ),
      );
      // Say an older version exists rather than printing it. The card is the current
      // answer; recall is the surface built to show the chain, and duplicating it here
      // would put a superseded statement back on screen next to the live one.
      const chain = decisionChain(d);
      if (chain.length > 1) {
        blocks.push(context(`เปลี่ยนมาแล้ว ${chain.length} ครั้ง — ดูของเดิมด้วย \`${CMD} recall\``));
      }
    }
    const omitted = decisions.length - MAX_DECISIONS;
    if (omitted > 0) {
      blocks.push(context(`…อีก ${omitted} รายการที่ไม่ได้แสดง — \`${CMD} recall\``));
    }
  }

  const recent = item.messages.slice(-3);
  if (recent.length) {
    blocks.push(divider());
    blocks.push(section('*ข้อความล่าสุด*'));
    for (const m of recent) blocks.push(section(quote(m)));
  }

  if (item.summary) {
    blocks.push(divider());
    if (item.summary.unverified) {
      // The model's citations failed verification. Warn, but keep showing the
      // derived facts above — those are still good.
      blocks.push(
        section(
          '*⚠️ สรุปนี้ตรวจสอบไม่ผ่าน*\nอ้างอิงที่โมเดลให้มาไม่ตรงกับข้อความจริง — อ่านเป็นการเดา ไม่ใช่ข้อเท็จจริง\n' +
            'ข้อมูลด้านบน (สถานะ, ไทม์ไลน์, หลักฐาน) คำนวณจากข้อมูลจริง ยังเชื่อได้',
        ),
      );
    } else {
      blocks.push(context(summaryProvenance(item.summary)));
    }
    blocks.push(section(esc(clamp(item.summary.detail, 600))));
    if (item.summary.next_step) blocks.push(section(`*ขั้นต่อไป:* ${esc(item.summary.next_step)}`));

    // The citations behind the prose, minus the ones already quoted above — the
    // rule-based backend cites the last three messages, which are the same three.
    // A citation the reader cannot open is the same as no citation, so unresolvable
    // ids are dropped rather than printed as bare strings.
    const shown = new Set(recent.map((m) => m.id));
    const cited = item.summary.citations
      .filter((id) => !shown.has(id))
      .map(findMessage)
      .filter((m): m is NonNullable<typeof m> => Boolean(m))
      .slice(0, 3);
    if (cited.length) {
      blocks.push(context('อ้างอิงของสรุปที่ยังไม่ได้แสดงข้างบน:'));
      blocks.push({
        type: 'actions',
        elements: cited.map(
          (m) => evidenceButton(m.id, m.permalink, clamp(`${displayName(m.user) || m.user} · ${m.when}`, 70)) as any,
        ),
      } as KnownBlock);
    }
  }

  return blocks;
}

/**
 * Compact "my board" / "@someone's board" list.
 *
 * One block per item, and Slack rejects the whole message at 50 — so the board
 * is capped and the reader is told what is missing and how to narrow it. Items
 * arrive blocked-first, stalest-first, so the cap drops the least urgent rows.
 */
const MAX_BOARD_ROWS = 40;

export function boardBlocks(title: string, items: WorkItem[]): KnownBlock[] {
  if (!items.length) {
    return [header(title), section('*ไม่มีงานค้าง* 🎉\nไม่มีอะไรเปิดค้างอยู่เลย')];
  }
  const blocks: KnownBlock[] = [
    header(title),
    context(
      `${items.length} งานที่ยังเปิดอยู่ · นิ่งนานสุดขึ้นก่อน · ` +
        `จาก ${ledgerOrigin() === 'pipeline' ? 'pipeline' : 'fixture'} (${refreshStatus().at})` +
        (refreshStatus().error ? ' · ⚠ โหลดใหม่ล่าสุดไม่สำเร็จ ข้อมูลเก่ากว่านี้' : ''),
    ),
  ];
  for (const i of items.slice(0, MAX_BOARD_ROWS)) {
    blocks.push(
      section(
        `${STATE_LABEL[i.state]}  *${i.key}*  ${esc(clamp(i.headline, 80))}  ·  _${days(i.age_days)}_\n` +
          `${esc(clamp(i.evidence, 160))}`,
        { type: 'button', text: { type: 'plain_text', text: 'เปิด' }, action_id: 'open_item', value: i.key },
      ),
    );
  }
  const omitted = items.length - MAX_BOARD_ROWS;
  if (omitted > 0) {
    blocks.push(
      context(
        `…อีก ${omitted} งานไม่ได้แสดง (Slack จำกัด 50 block ต่อข้อความ) — ` +
          `แคบลงด้วย \`${CMD} blocked\` หรือ \`${CMD} <KEY>\``,
      ),
    );
  }
  return blocks;
}
