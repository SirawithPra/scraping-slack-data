import type { KnownBlock } from '@slack/types';
import type { StandupDraft } from '../types.js';
import { findMessage } from '../data.js';
import { CMD, clamp, context, divider, esc, header, section } from './common.js';

/**
 * The 08:45 DM — the heart of the pitch.
 *
 * A normal standup bot asks "what did you do yesterday?" and the dev has to
 * remember. This one shows them what it already knows, from their own messages
 * and commits, and asks them to *correct* it. Recall is the expensive part;
 * correction is cheap. That inversion is the whole idea, and it is also the
 * fix for "I forget what ended last week" — the bot didn't forget.
 *
 * `carried_over` is the pain-#1 fix: work still open from before that nobody
 * would have mentioned unless asked by name.
 *
 * Both lists are capped. A `yesterday` entry costs two blocks and a
 * `carried_over` entry one, and Slack rejects the whole message at 50 blocks —
 * so an unbounded draft means the busiest person is the one who gets no DM at
 * all. The overflow is stated on screen rather than dropped quietly: a draft the
 * reader cannot tell is incomplete is worse than a short one.
 */
const MAX_YESTERDAY = 5;
const MAX_CARRIED = 5;
/** Prefilled lines per box. Three fits without scrolling in a DM. */
const MAX_PREFILL = 3;

/**
 * What the two boxes start with.
 *
 * The header of this DM says the bot already knows and only wants corrections, and
 * for a year the boxes under it were empty — so the person read "you don't have to
 * retype it" and then retyped it. Prefilling is what that sentence promised.
 *
 * The shape is the daily template's, not a new one (`DAILY_TEMPLATE` in daily.ts):
 * `ต่อ KEY — headline` for today, `Pending …` for a blocker. Somebody who fills a
 * daily thread and somebody who answers this DM are then writing the same lines, and
 * `parseDailyReply` reads both — a second format would be a second thing to teach.
 *
 * Today's box may be a proposal ("carry on with what is still open"); the blocker box
 * may not. It is prefilled only from sentences this person typed, because pressing
 * *ส่ง* posts it into the channel under their name — see `blocked` in StandupDraft.
 */
/** `REVERAPP-140` — a key a person can look up, as opposed to a cluster id like `c23053d`. */
const TICKET_KEY = /^[A-Z][A-Z0-9]+-\d+$/;

/**
 * One line of the "today" box, or nothing.
 *
 * A cluster id is not a name. `ต่อ c5a3d6b — (ยังไม่มีคำร่วมที่ชัดพอจะตั้งชื่อ)` is a
 * line nobody can act on or even look up, and offering it as a suggestion asks the
 * reader to delete it — which is more work than the empty box it replaced. So a
 * ticket keeps its key, a cluster is named by its keywords alone, and a cluster the
 * pipeline could not name at all is left out.
 */
function todayLine(key: string, headline: string): string | undefined {
  const name = clamp(headline.trim(), 60);
  if (TICKET_KEY.test(key)) return `ต่อ ${key} — ${name}`;
  if (!name || name.startsWith('(')) return undefined;
  return `ต่อ ${name}`;
}

export function standupPrefill(draft: StandupDraft): { today?: string; blocker?: string } {
  // Tickets before clusters, then stalest first: the lines a person can act on are
  // the ones worth the three slots.
  const open = [...draft.carried_over]
    .sort((a, b) => {
      const byKind = Number(TICKET_KEY.test(b.key)) - Number(TICKET_KEY.test(a.key));
      return byKind || b.stale_days - a.stale_days;
    })
    .map((c) => todayLine(c.key, c.headline))
    .filter((line): line is string => Boolean(line))
    .slice(0, MAX_PREFILL);
  // Nothing carried over: offer yesterday's work instead, which is the other honest
  // guess at "today". Neither is offered when there is nothing at all — an empty box
  // with its placeholder asks the question; a box holding `-` answers it wrongly.
  const fallback = draft.yesterday
    .map((y) => todayLine(y.key, y.headline))
    .filter((line): line is string => Boolean(line))
    .slice(0, MAX_PREFILL);
  const today = open.length ? open : fallback;

  const blocker = (draft.blocked ?? [])
    .slice(0, MAX_PREFILL)
    // Same reason as `todayLine`: a ticket key helps whoever reads it in the channel,
    // a cluster id is noise nobody can look up.
    .map((b) => `Pending ${clamp(b.text, 120)}${TICKET_KEY.test(b.key) ? ` (${b.key})` : ''}`);

  return {
    today: today.length ? today.join('\n') : undefined,
    blocker: blocker.length ? blocker.join('\n') : undefined,
  };
}

export function standupDmBlocks(draft: StandupDraft): KnownBlock[] {
  const blocks: KnownBlock[] = [
    header('สรุปของคุณเมื่อวาน'),
    context('ผมดึงมาจาก Slack + YouTrack ให้แล้ว — *แก้ได้ถ้าไม่ถูก* ไม่ต้องพิมพ์ใหม่ทั้งหมด'),
    divider(),
  ];

  if (draft.yesterday.length === 0) {
    blocks.push(section('_เมื่อวานไม่เจอความเคลื่อนไหวของคุณเลย — ถ้าทำอะไรอยู่ เขียนข้างล่างได้ครับ_'));
  }

  for (const y of draft.yesterday.slice(0, MAX_YESTERDAY)) {
    const ev = y.evidence_id ? findMessage(y.evidence_id) : undefined;
    blocks.push(
      section(`*${y.key}*  ${esc(y.headline)}\n${esc(y.note)}`, ev?.permalink
        ? { type: 'button', text: { type: 'plain_text', text: 'ดูข้อความ' }, url: ev.permalink }
        : undefined),
    );
    if (ev) blocks.push(context(`💬 ${ev.when} · จาก ${esc(ev.source)}`));
  }
  const moreYesterday = draft.yesterday.length - MAX_YESTERDAY;
  if (moreYesterday > 0) {
    blocks.push(context(`…และอีก ${moreYesterday} งานที่คุณขยับเมื่อวาน — ดูทั้งหมดด้วย \`${CMD}\``));
  }

  if (draft.carried_over.length) {
    blocks.push(divider());
    blocks.push(section('*⏸ ค้างจากก่อนหน้านี้ — ยังไม่ขยับ*'));
    // Stalest first, so a cap drops the freshest rather than the most overdue.
    const carried = [...draft.carried_over].sort((a, b) => b.stale_days - a.stale_days);
    for (const c of carried.slice(0, MAX_CARRIED)) {
      blocks.push(
        context(`*${c.key}*  ${esc(clamp(c.headline, 80))} — นิ่งมา *${Math.round(c.stale_days)} วัน*`),
      );
    }
    const moreCarried = carried.length - MAX_CARRIED;
    if (moreCarried > 0) {
      blocks.push(context(`…และอีก ${moreCarried} งานที่ค้างอยู่ — ดูทั้งหมดด้วย \`${CMD}\``));
    }
  }

  blocks.push(divider());
  const prefill = standupPrefill(draft);
  blocks.push({
    type: 'input',
    block_id: 'today',
    optional: true,
    label: { type: 'plain_text', text: 'วันนี้ทำอะไร' },
    element: {
      type: 'plain_text_input',
      action_id: 'value',
      multiline: true,
      // `initial_value` only when there is something to put in it: Slack renders an
      // empty string as a filled-in answer of nothing, and the placeholder — which is
      // the instruction — disappears behind it.
      ...(prefill.today ? { initial_value: prefill.today } : {}),
      placeholder: { type: 'plain_text', text: 'ต่อจากเมื่อวานได้เลย ไม่ต้องยาว' },
    },
  } as KnownBlock);
  if (prefill.today) {
    blocks.push(
      context(
        'เติมให้จากงานที่ยังไม่ปิด — *เป็นข้อเสนอ ไม่ใช่คำพูดของคุณ* ลบทิ้งหรือพิมพ์ทับได้เลย',
      ),
    );
  }
  blocks.push({
    type: 'input',
    block_id: 'blocker',
    optional: true,
    label: { type: 'plain_text', text: 'มีอะไรติดไหม' },
    element: {
      type: 'plain_text_input',
      action_id: 'value',
      multiline: true,
      ...(prefill.blocker ? { initial_value: prefill.blocker } : {}),
      placeholder: { type: 'plain_text', text: 'ว่างไว้ได้ถ้าไม่มี' },
    },
  } as KnownBlock);
  blocks.push(
    context(
      prefill.blocker
        ? 'บรรทัดนี้คือข้อความที่ *คุณเขียนเอง* แล้วงานยังไม่ขยับ — เคลียร์แล้วลบทิ้งได้ · ' +
            'ถ้ายังติด เติม `@คนที่ต้องทำให้ก่อน` หรือ `PO` ต่อท้าย ผมจะได้รู้ว่ารอใคร'
        : 'รูปแบบเดียวกับ daily: `Pending [เรื่องที่รออยู่] [@คน หรือ PO]` · ไม่มีก็ว่างไว้',
    ),
  );

  blocks.push({
    type: 'actions',
    elements: [
      {
        type: 'button',
        style: 'primary',
        text: { type: 'plain_text', text: 'ส่ง' },
        action_id: 'standup_submit',
        value: draft.slack_user_id,
      },
      {
        type: 'button',
        text: { type: 'plain_text', text: 'ข้ามวันนี้' },
        action_id: 'standup_skip',
        value: draft.slack_user_id,
      },
    ],
  } as KnownBlock);

  blocks.push(context('ส่งภายใน 09:15 · ถ้าไม่ส่ง ผมจะใช้ที่ดึงมาให้ข้างบนแทน ไม่มีใครโดนทวงในห้อง'));
  return blocks;
}
