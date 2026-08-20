/**
 * What Meowtam writes on a ticket, in a shape somebody who has never heard of
 * Meowtam can read.
 *
 * Both comments this bot writes were built inline at their call sites, and both read
 * like log lines. The paste one, verbatim:
 *
 *     แนบบทสนทนาจาก Slack: “การ deploy ขึ้น sit” (2026-08-18) — 3 ข้อความ
 *     เก็บเข้า corpus ของ Meowtam แล้ว โดย U08H0UD5R36 เมื่อ 2026-08-20 08:08
 *
 * Every fault in that is a fault for whoever opens the ticket next week. `U08H0UD5R36`
 * is not a colleague's name — the bot has resolved ids to names on every screen since
 * `names.ts`, and this was the one surface still shipping the raw id, to the tracker,
 * where it is permanent. "3 ข้อความ" does not say what the three messages *said*,
 * which is the only reason to attach them at all. "corpus ของ Meowtam" names an
 * internal store and offers no way to open it. And nothing on the comment separates a
 * message the bot read out of a channel it can see — which has a Slack permalink —
 * from a closed chat a person had to select and paste, which has none and never will.
 *
 * So a comment from here carries five things, in this order: what kind of attachment
 * this is, where the conversation came from and what that implies, who attached it and
 * when, the conversation itself, and a link to everywhere the reader can go next. The
 * transcript is what earns the comment its place on the ticket — a summary of a chat
 * the reader cannot check is worth less than the chat.
 *
 * Markdown, because YouTrack renders comment bodies as Markdown. The label lines are a
 * bullet list rather than plain lines for a mechanical reason: a single newline
 * between two Markdown paragraphs collapses, so six consecutive `**label:** value`
 * lines would arrive on the ticket as one run-on sentence.
 */

import { displayName, namesInText } from './names.js';
import type { Source } from './types.js';

/** One message as it will be quoted on the ticket. */
export interface Said {
  user: string;
  when: string;
  text: string;
}

/**
 * How much of a chat goes onto the ticket.
 *
 * A pasted scroll can be fifty messages, and a fifty-message comment is one somebody
 * scrolls past. Twelve is roughly a screen; the rest are counted, not hidden, and the
 * whole thing is in the corpus behind the board link either way.
 */
const QUOTED_MESSAGES = 12;
/** One message's worth of text. Long enough for a paragraph, short of a pasted stack trace. */
const QUOTED_CHARS = 600;

/**
 * Where a source came from, spelled out for a reader outside this codebase.
 *
 * `slack` and `slack_paste` are the pair that has to be distinguishable. They are the
 * same product and completely different provenance: one is a message the bot read
 * through the API in a channel it was invited to, the other is a closed conversation —
 * a DM, a private group, another workspace, somebody's phone — that no token the team
 * can grant will reach, so a person selected it by hand. Only the first has a
 * permalink, and only the second is as complete as whatever somebody happened to
 * highlight. A reader weighing the quote needs to know which one they are looking at.
 */
export const SOURCE_TEXT: Record<Source, { label: string; note?: string }> = {
  slack: {
    label: 'Slack — ห้องที่บอทอ่านได้เอง',
    note: 'ข้อความถูกดึงผ่าน Slack API จากห้องที่บอทถูกเชิญเข้าไป จึงมีลิงก์กลับไปที่ข้อความจริงให้กด',
  },
  slack_paste: {
    label: 'Slack — แชทปิด ที่คนวางเข้ามาเอง',
    note:
      'แชทนี้อยู่ใน DM / ห้องปิด / workspace อื่น ที่ token ของบอทเข้าไม่ถึง ' +
      'จึงไม่มีลิงก์ Slack ให้กด และสิ่งที่แนบมาคือช่วงที่คนเลือกก๊อป ไม่ใช่ทั้งห้อง',
  },
  meeting: { label: 'ประชุม — จากไฟล์ถอดเสียง' },
  note: { label: 'โน้ตที่คนพิมพ์เข้ามาเอง' },
  youtrack: { label: 'ticket ใน YouTrack' },
  notion: { label: 'หน้าใน Notion' },
};

function sourceText(source: Source): { label: string; note?: string } {
  return SOURCE_TEXT[source] ?? { label: String(source) };
}

/**
 * `U08H0UD5R36` → `แนน ก. (U08H0UD5R36)`.
 *
 * Both halves on purpose. The name is what a colleague recognises; the id is what
 * somebody searching Slack or the corpus a month from now can actually paste into a
 * search box, and it is the only one of the two that is stable.
 */
export function person(userId: string): string {
  const id = String(userId ?? '').trim();
  if (!id) return 'ไม่รู้ว่าใคร';
  const name = displayName(id);
  return name && name !== id ? `${name} (\`${id}\`)` : `\`${id}\``;
}

/**
 * Slack's wire syntax unwrapped, so a quote on a ticket reads as a sentence.
 *
 * A message body from the API is not the text somebody typed. A mention is
 * `<@U08H0UD5R36>`, a channel is `<#C0ABC|dev>`, a link is `<https://…|ดูที่นี่>`. Left
 * alone, all of that lands on the ticket as angle-bracket noise with an id inside —
 * which is the same unreadability the raw author id had, moved into the quote.
 * `namesInText` then does the last step and turns the ids into names.
 */
function readable(text: string): string {
  return namesInText(
    String(text ?? '')
      .replace(/<@([UWB][A-Z0-9]+)(\|[^>]*)?>/g, (_m, id: string) => `@${id}`)
      .replace(/<#[A-Z0-9]+\|([^>]+)>/g, '#$1')
      .replace(/<(https?:[^|>]+)\|([^>]+)>/g, '$2 ($1)')
      .replace(/<(https?:[^|>]+)>/g, '$1')
      .replace(/<!(here|channel|everyone)>/g, '@$1'),
  );
}

function cut(text: string, limit: number): string {
  const one = String(text ?? '').trim();
  return one.length <= limit ? one : `${one.slice(0, limit - 1)}…`;
}

/**
 * The messages as Markdown blockquotes.
 *
 * The blank `>` under each speaker line is load-bearing: without it Markdown folds
 * the attribution and the first line of the message into one paragraph, and the quote
 * reads as though the speaker's name were part of what they said.
 */
function quoted(records: Said[]): string {
  return records
    .slice(0, QUOTED_MESSAGES)
    .map((r) => {
      const body = cut(readable(r.text), QUOTED_CHARS).replace(/\n/g, '\n> ');
      return `> **${displayName(r.user) || r.user}** · ${r.when}\n>\n> ${body}`;
    })
    .join('\n\n');
}

/** Distinct speakers, so the header can say "3 ข้อความ จาก 2 คน" and mean it. */
function speakers(records: Said[]): number {
  return new Set(records.map((r) => String(r.user))).size;
}

/**
 * A url worth writing onto a ticket, or nothing.
 *
 * The board link is built from `TAM_API_URL`, which on a laptop is
 * `http://localhost:8000`. An ephemeral Slack button pointing there is useful — the
 * person pressing it is sitting at that laptop. A comment on a real ticket is neither
 * ephemeral nor theirs: `http://localhost:8000/item/REVERAPP-251` on a ticket other
 * people read is a dead link that outlives the demo. So a private host is dropped here
 * and the comment simply says less, which is the honest failure of the two.
 */
export function sharableUrl(url?: string): string | undefined {
  if (!url) return undefined;
  let host: string;
  try {
    host = new URL(url).hostname.toLowerCase();
  } catch {
    return undefined;
  }
  if (host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local')) return undefined;
  if (host === '::1' || host === '[::1]' || host === '0.0.0.0') return undefined;
  if (/^127\./.test(host) || /^10\./.test(host) || /^192\.168\./.test(host)) return undefined;
  if (/^172\.(1[6-9]|2\d|3[01])\./.test(host)) return undefined;
  return url;
}

/** `[text](url)` only when there is a url — a bare label pretending to be a link is worse than no link. */
function link(text: string, url?: string): string | undefined {
  return url ? `[${text}](${url})` : undefined;
}

const FOOTER =
  '*เขียนโดยบอท Meowtam · เขียนเป็นคอมเมนต์เท่านั้น ' +
  'ไม่ได้แก้ description, state หรือ field ใด ๆ ของ ticket นี้*';

function assemble(parts: Array<string | undefined>): string {
  return parts.filter((p) => p && p.trim()).join('\n\n');
}

/**
 * The comment for a chat somebody pasted out of a conversation the bot cannot read.
 *
 * `boardUrl` is the one link this comment can offer and it matters more than usual:
 * there is no Slack permalink to give — that is the whole reason the chat arrived by
 * paste — so the board is the only place the reader can see the conversation in
 * context, next to the rest of the work item.
 */
export function pasteComment(input: {
  key: string;
  title: string;
  day: string;
  by: string;
  at: string;
  records: Said[];
  itemKey?: string;
  boardUrl?: string;
  /** How many of these the rebuilt ledger actually filed under the work item. */
  inItem?: number;
}): string {
  const { records } = input;
  const src = sourceText('slack_paste');
  const people = speakers(records);
  const hidden = Math.max(0, records.length - QUOTED_MESSAGES);

  const facts = [
    `* **ที่มา:** ${src.label} (\`slack_paste\`)`,
    src.note ? `* **ข้อควรรู้:** ${src.note}` : undefined,
    `* **หัวข้อที่คนตั้งไว้:** “${input.title}”`,
    `* **วันที่คุยกัน:** ${input.day}`,
    `* **แนบมา:** ${records.length} ข้อความ${people > 1 ? ` จาก ${people} คน` : ''}`,
    `* **ใครแนบ:** ${person(input.by)}`,
    `* **แนบเมื่อ:** ${input.at}`,
  ];

  const next = [
    `* เก็บเข้าคลังข้อความของ Meowtam แล้ว — ค้นเจอด้วย \`/mt recall\` และขึ้นใต้งานนี้ในบอร์ด`,
    input.inItem !== undefined && input.itemKey
      ? `* บอร์ดจัดให้ ${input.inItem} จาก ${records.length} ข้อความนี้อยู่ใต้งาน \`${input.itemKey}\``
      : undefined,
    link(`เปิดงาน ${input.itemKey ?? input.key} ในบอร์ด Meowtam`, input.boardUrl)
      ? `* ${link(`เปิดงาน ${input.itemKey ?? input.key} ในบอร์ด Meowtam`, input.boardUrl)}`
      : undefined,
  ];

  return assemble([
    `### 📋 แนบบทสนทนาจาก Slack — “${input.title}”`,
    facts.filter(Boolean).join('\n'),
    '#### บทสนทนาที่แนบมา',
    quoted(records),
    hidden ? `_อีก ${hidden} ข้อความไม่ได้ยกมาไว้ที่นี่ — ครบทั้งหมดอยู่ในบอร์ด_` : undefined,
    '#### ต่อจากนี้',
    next.filter(Boolean).join('\n'),
    '---',
    FOOTER,
  ]);
}

/**
 * The comment for one or more messages linked to a ticket from the ⋯ menu.
 *
 * `permalink` is why writing to the tracker is worth doing at all: it makes the link
 * two-way, so a person reading the ticket can reach the conversation instead of being
 * told one exists.
 */
export function linkComment(input: {
  key: string;
  by: string;
  at: string;
  /** What the person typed in the modal. Their sentence leads, because it is the reason. */
  note: string;
  messages: number;
  source?: Source;
  records?: Said[];
  permalink?: string;
  itemKey?: string;
  boardUrl?: string;
}): string {
  const src = sourceText(input.source ?? 'slack');
  const records = input.records ?? [];

  const facts = [
    `* **ที่มา:** ${src.label} (\`${input.source ?? 'slack'}\`)`,
    src.note ? `* **ข้อควรรู้:** ${src.note}` : undefined,
    `* **ผูกมา:** ${input.messages} ข้อความ`,
    `* **ใครผูก:** ${person(input.by)}`,
    `* **ผูกเมื่อ:** ${input.at}`,
  ];

  const next = [
    link('เปิดข้อความนี้ใน Slack', input.permalink),
    link(`เปิดงาน ${input.itemKey ?? input.key} ในบอร์ด Meowtam`, input.boardUrl),
  ].filter(Boolean);

  return assemble([
    `### 💬 ผูกข้อความใน Slack เข้ากับ ${input.key}`,
    input.note.trim() ? `**เหตุผลจากคนที่ผูก:** ${input.note.trim()}` : undefined,
    facts.filter(Boolean).join('\n'),
    records.length ? '#### ข้อความที่ผูก' : undefined,
    records.length ? quoted(records) : undefined,
    next.length ? '#### ต่อจากนี้' : undefined,
    next.length ? next.map((l) => `* ${l}`).join('\n') : undefined,
    '---',
    FOOTER,
  ]);
}

/**
 * The comment for a scope drift: the thread and the ticket disagree, and this is what
 * Slack thinks the scope now is.
 *
 * Still a comment and still never a description edit. Overwriting a ticket's
 * description from a Slack modal destroys whatever a PO wrote there, with no undo and
 * no record of what was lost — so the proposal goes in beside the original and its
 * owner applies it, or does not.
 */
export function driftComment(input: {
  key: string;
  by: string;
  at: string;
  /** Why the reporter thinks it drifted. */
  note: string;
  /** The scope as the Slack thread has it. */
  scope: string;
  boardUrl?: string;
}): string {
  const facts = [
    `* **ที่มา:** เทียบเธรดใน Slack (${sourceText('slack').label}) กับ description ของ ticket นี้`,
    `* **ใครเสนอ:** ${person(input.by)}`,
    `* **เสนอเมื่อ:** ${input.at}`,
    `* **สถานะ:** ข้อเสนอ — **ยังไม่ได้แก้ description ให้** เจ้าของ ticket อ่านเทียบแล้วตัดสินใจเอง`,
  ];
  const board = link(`เปิดงาน ${input.key} ในบอร์ด Meowtam`, input.boardUrl);

  return assemble([
    `### ⚠️ สโคปในเธรด Slack กับ ticket นี้ไม่ตรงกัน`,
    `**เรื่องที่ไม่ตรง:** ${input.note.trim() || 'สโคปในเธรดกับ ticket ไม่ตรงกัน'}`,
    facts.join('\n'),
    '#### สโคปที่คุยกันใน Slack',
    input.scope.trim()
      ? input.scope
          .trim()
          .split('\n')
          .map((l) => `> ${l}`)
          .join('\n')
      : '_ไม่ได้กรอกมา_',
    board ? `#### ต่อจากนี้\n* ${board}` : undefined,
    '---',
    FOOTER,
  ]);
}
