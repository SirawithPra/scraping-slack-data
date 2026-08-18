import 'dotenv/config';
import { basename } from 'node:path';
import { App, LogLevel } from '@slack/bolt';

import {
  ledger, hydrate, reload, ledgerOrigin, demoFixtures, sortedItems, itemsByState, findItem,
  findMessage, itemKeyForMessage, itemsFor, standupFor, driftFor,
} from './data.js';
import { apiConfig } from './tam-api.js';
import { describePolicy, guardPosting, readPolicy } from './postguard.js';
import {
  decisionsPath, overridesPath, readDecisions, saveDecision, saveOverride, storeSummary,
} from './store.js';
import { digestBlocks } from './blocks/digest.js';
import { standupDmBlocks } from './blocks/standupDm.js';
import { itemCardBlocks, boardBlocks } from './blocks/itemCard.js';
import { driftNudgeBlocks, driftModal } from './blocks/drift.js';
import { recallBlocks } from './blocks/recall.js';
import { COMMANDS, CMD, context, section, esc, clamp } from './blocks/common.js';

const env = (k: string, fallback = '') => process.env[k]?.trim() || fallback;

/** 'YYYY-MM-DD HH:mm' — absolute, matching every timestamp the product renders. */
function stamp(d = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * The timestamp of a Slack message, from the `ts` the payload already carries.
 *
 * A decision has to be dated when it was *said*, not when someone got around to
 * filing it: recall renders these dates as the chain ("we said X in May, Y in
 * August"), so stamping now() next to a message from May makes the one claim the
 * decision log exists to support false.
 */
function stampOfMessage(ts?: string): string | undefined {
  const seconds = Number(ts);
  if (!Number.isFinite(seconds) || seconds <= 0) return undefined;
  return stamp(new Date(seconds * 1000));
}

/** A path the operator can act on, without publishing the host's directory layout into Slack. */
function shortPath(p: string): string {
  return basename(p);
}

/**
 * The record id the pipeline gave this Slack message.
 *
 * `tam.ingest.prepare_messages` keys Slack records `msg_<channel>_<ts>`, so the
 * id is reconstructible from what a shortcut payload already carries — no lookup
 * table, and it stays correct after a reindex.
 */
function recordIdFor(shortcut: { channel?: { id?: string }; message?: { ts?: string } }): string {
  const channel = shortcut.channel?.id;
  const ts = shortcut.message?.ts;
  return channel && ts ? `msg_${channel}_${ts}` : '';
}

// Check before constructing the App — Bolt throws its own opaque error on a
// missing token, and at 3am you want to be told which env var is missing.
const missing = ['SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'SLACK_SIGNING_SECRET'].filter((k) => !env(k));
if (missing.length) {
  console.error(`\n✗ ขาด env: ${missing.join(', ')}`);
  console.error('  คัดลอก .env.example เป็น .env แล้วเติมให้ครบ');
  console.error('  (ถ้าอยากดู payload เฉย ๆ โดยไม่ต่อ Slack: npm run preview)\n');
  process.exit(1);
}

const app = new App({
  token: env('SLACK_BOT_TOKEN'),
  signingSecret: env('SLACK_SIGNING_SECRET'),
  socketMode: true,
  appToken: env('SLACK_APP_TOKEN'),
  logLevel: LogLevel.INFO,
});

// A stray promise anywhere must be a log line, not an outage. Node's default for
// an unhandled rejection is to kill the process, and this bot has no supervisor:
// losing it at 08:45 means nobody gets a standup and nobody knows why.
// Before start() resolves, "still running" is a lie — the boot path exits on
// failure, and this is the one line an operator reads when a boot fails.
let running = false;
// Install the posting allowlist before any listener can run. Bolt gives every handler
// this same WebClient, so one wrap covers all of them — including the scheduled jobs.
const postPolicy = readPolicy();
guardPosting(app.client, postPolicy);

process.on('unhandledRejection', (reason) => {
  const state = running ? 'บอทยังทำงานต่อ' : 'ยังไม่ได้เริ่ม — ดู error ด้านล่าง';
  console.error(`⚠  unhandled rejection (${state}):`, reason);
});
app.error(async (err) => {
  console.error('⚠  bolt error:', err);
});

const DIGEST_CHANNEL = env('DIGEST_CHANNEL');
const STANDUP_USERS = env('STANDUP_USERS').split(',').map((s) => s.trim()).filter(Boolean);

/* ------------------------------------------------------------------ *
 * /meowtam — one command, several shapes. Fewer commands to remember, and
 * Slack only makes you register one.
 * ------------------------------------------------------------------ */

// Both names hit the same handler. `/mt` exists purely so the live demo is
// eight keystrokes shorter per beat. The names live in blocks/common.ts because
// the Block Kit copy points people at them too.
app.command(new RegExp(`^/(${COMMANDS.join('|')})$`), async ({ command, ack, respond, client, body }) => {
  await ack();
  const arg = command.text.trim();
  const lower = arg.toLowerCase();

  try {
    // /meowtam          → my board
    if (!arg) {
      const me = command.user_name;
      const items = itemsFor(me).length ? itemsFor(me) : itemsFor(command.user_id);
      await respond({ blocks: boardBlocks(`งานของ @${me}`, items), text: 'งานของคุณ' });
      return;
    }

    // /meowtam demo …     → the demo driver (see below)
    if (lower.startsWith('demo')) {
      await runDemo(arg.slice(4).trim(), { client, respond, channel: command.channel_id, user: command.user_id });
      return;
    }

    // /meowtam blocked
    if (lower === 'blocked' || lower === 'block') {
      const blocked = itemsByState('blocked');
      await respond({
        blocks: blocked.length
          ? boardBlocks('ที่ติดอยู่ตอนนี้', blocked)
          : [section('*✅ ไม่มีอะไรติด*\nทุกงานเดินได้หมด — ข่าวดีครับ')],
        text: 'blocked',
      });
      return;
    }

    // /meowtam digest
    if (lower === 'digest' || lower === 'standup') {
      await respond({ blocks: digestBlocks(), text: 'digest' });
      return;
    }

    // /meowtam reload     → re-read ledger.json without restarting mid-demo
    if (lower === 'reload') {
      const l = await reload();
      const from = ledgerOrigin() === 'pipeline' ? 'pipeline' : 'fixture';
      await respond({ text: `โหลดใหม่แล้ว (${from}): ${l.items.length} items, ${l.corpus_size} ข้อความ` });
      return;
    }

    // /meowtam recall <paragraph>
    if (lower.startsWith('recall ')) {
      await respond({ blocks: await recallBlocks(arg.slice(7).trim()), text: 'recall' });
      return;
    }

    // /meowtam @someone
    if (arg.startsWith('@')) {
      const who = arg.slice(1).split(/\s/)[0] ?? '';
      await respond({ blocks: boardBlocks(`งานของ @${who}`, itemsFor(who)), text: `งานของ ${who}` });
      return;
    }

    // /meowtam PROJ-142
    const item = findItem(arg.split(/\s/)[0] ?? '');
    if (item) {
      await respond({ blocks: itemCardBlocks(item), text: `${item.key} ${item.headline}` });
      return;
    }

    // Anything else is treated as a recall query. People will type questions;
    // failing with "unknown command" would be hostile.
    await respond({ blocks: await recallBlocks(arg), text: 'recall' });
  } catch (err) {
    // The detail goes to the server log, not into Slack: a fetch failure's message
    // carries the internal TAM_API_URL, and the person who typed a slash command
    // can do nothing with a stack trace anyway.
    console.error('command error', err);
    await respond({ text: 'พังตอนประมวลผลคำสั่งครับ — รายละเอียดอยู่ใน log ฝั่งเซิร์ฟเวอร์' });
  }
});

/* ------------------------------------------------------------------ *
 * Interactions
 * ------------------------------------------------------------------ */

/**
 * Open a modal, or stack it on the one already open.
 *
 * Every one of these actions is reachable from inside a modal — the item card
 * embeds the evidence button, and the digest's card is itself a modal — and
 * `views.open` from a view interaction is not the supported call: it replaces the
 * card the reader was looking at instead of stacking on it. Branch on whether the
 * interaction came from a view.
 */
async function showModal(client: any, body: any, view: any) {
  const args = { trigger_id: body.trigger_id, view };
  if (body.view) await client.views.push(args);
  else await client.views.open(args);
}

app.action('open_item', async ({ ack, body, client, action }) => {
  await ack();
  const key = (action as any).value as string;
  const item = findItem(key);
  if (!item) return;
  await showModal(client, body, {
    type: 'modal',
    title: { type: 'plain_text', text: clamp(item.key, 24) },
    close: { type: 'plain_text', text: 'ปิด' },
    blocks: itemCardBlocks(item) as any,
  });
});

app.action('show_evidence', async ({ ack, body, client, action }) => {
  await ack();
  const m = findMessage((action as any).value as string);
  if (!m) return;
  await showModal(client, body, {
    type: 'modal',
    title: { type: 'plain_text', text: 'หลักฐาน' },
    close: { type: 'plain_text', text: 'ปิด' },
    blocks: [
      section(`*${esc(m.user)}* · ${m.when} · ${m.source}`),
      section(esc(m.text)),
      context(m.permalink ? `<${m.permalink}|เปิดใน Slack>` : `id: \`${m.id}\``),
    ] as any,
  });
});

app.action('open_drift_modal', async ({ ack, body, client, action }) => {
  await ack();
  const key = (action as any).value as string;
  const drift = driftFor(key);
  const item = findItem(key);
  if (!drift || !item) return;
  await showModal(client, body, driftModal(drift, item) as any);
});

app.action('dismiss_drift', async ({ ack, respond, action }) => {
  await ack();
  // Dismissals are signal, not noise — in the real build they tune the cue list.
  // Nothing persists them yet, so the reply says exactly that. Answering "บันทึก
  // ไว้แล้ว" for a console.log is the same fake confirmation store.ts was written
  // to delete, and it would be believed.
  console.log('drift dismissed for', (action as any).value);
  await respond({
    replace_original: false,
    text:
      'โอเค ปิดให้แล้วครับ — แต่ยังไม่ได้เก็บไว้ถาวร (ยังไม่มีที่เก็บ dismissal) ' +
      'ตอนนี้แค่ขึ้น log ฝั่งเซิร์ฟเวอร์ ยังไม่ได้เอาไปปรับ cue อัตโนมัติ',
  });
});

app.view('drift_save', async ({ ack, view, client, body }) => {
  await ack();
  const key = view.private_metadata;
  const desc = view.state.values['description']?.['value']?.value ?? '';
  const comment = view.state.values['comment']?.['value']?.value ?? '';

  // ── Real implementation ───────────────────────────────────────────
  // POST /api/issues/{key} { description: desc }
  // POST /api/issues/{key}/comments { text: comment }
  // Auth: Bearer <YouTrack permanent token>
  // Left mocked for the hackathon so the demo never depends on YouTrack
  // being reachable from the venue wifi.
  console.log('[mock youtrack write]', key, { desc: desc.slice(0, 120), comment });

  await client.chat.postMessage({
    channel: (body as any).user.id,
    text: `อัปเดต ${key} แล้ว`,
    blocks: [
      section(`*✅ อัปเดต ${key} แล้ว*\ndescription ใหม่ถูกเขียนลง YouTrack พร้อมลิงก์กลับมาที่เธรดใน Slack`),
      context('เดโมนี้ยังไม่ได้ต่อ YouTrack จริง — จุดที่จะเขียนอยู่ใน `src/app.ts` เห็นชัดเจน'),
    ] as any,
  });
});

app.action('standup_submit', async ({ ack, body, client, respond }) => {
  await ack();
  const vals = (body as any).state?.values ?? {};
  const today = vals['today']?.['value']?.value ?? '';
  const blocker = vals['blocker']?.['value']?.value ?? '';
  const user = (body as any).user.id;

  await respond({
    replace_original: true,
    text: 'ส่งแล้ว',
    blocks: [
      section('*✅ ส่งแล้ว ขอบคุณครับ*'),
      ...(today ? [section(`*วันนี้:* ${esc(today)}`)] : []),
      ...(blocker ? [section(`*⛔ ติด:* ${esc(blocker)}`)] : []),
      context('จะไปโผล่ใน digest ตอน 09:25 — ไม่ต้องพูดซ้ำในห้องประชุม'),
    ] as any,
  });

  if (blocker && DIGEST_CHANNEL) {
    await client.chat.postMessage({
      channel: DIGEST_CHANNEL,
      text: `blocker ใหม่จาก <@${user}>`,
      blocks: [section(`*⛔ blocker ใหม่* จาก <@${user}>\n${esc(blocker)}`)] as any,
    });
  }
});

app.action('standup_skip', async ({ ack, respond }) => {
  await ack();
  await respond({
    replace_original: true,
    text: 'ข้ามแล้ว',
    blocks: [section('ข้ามวันนี้แล้วครับ — ผมจะใช้ที่ดึงมาให้แทน ไม่มีใครโดนทวงในห้อง')] as any,
  });
});

/* ------------------------------------------------------------------ *
 * Message shortcuts — the correction affordances. Any dev can use them,
 * not just the PO. If only the PO can fix links, you have rebuilt the
 * original problem with extra steps.
 * ------------------------------------------------------------------ */

/** Why a message could not be linked. Same wording wherever the reason differs. */
function cannotLinkBlocks(reason: string) {
  return [section('*ผูกไม่ได้*'), context(reason)] as any;
}

// Slack's own ceiling for a static_select. The old cap of 20 was self-imposed and
// silently made items 21+ unlinkable, which is the same class of bug as a
// truncated board with no notice.
const MAX_TICKET_OPTIONS = 100;

app.shortcut('link_to_ticket', async ({ ack, shortcut, client }) => {
  await ack();
  const items = sortedItems();

  // A static_select with zero options is an invalid view: Slack rejects it and the
  // user sees nothing happen at all. Say why instead.
  if (!items.length) {
    await client.chat.postMessage({
      channel: (shortcut as any).user.id,
      text: 'ผูกไม่ได้',
      blocks: cannotLinkBlocks(
        'ยังไม่มี work item ให้ผูกเลย — pipeline ยังไม่ได้จัดกลุ่มอะไร ลองรัน prepare/digest ก่อน',
      ),
    });
    return;
  }

  await client.views.open({
    trigger_id: (shortcut as any).trigger_id,
    view: {
      type: 'modal',
      callback_id: 'link_save',
      private_metadata: recordIdFor(shortcut as any),
      title: { type: 'plain_text', text: 'ผูกกับ ticket' },
      submit: { type: 'plain_text', text: 'ผูก' },
      close: { type: 'plain_text', text: 'ยกเลิก' },
      blocks: [
        section(`>${esc(clamp((shortcut as any).message?.text ?? '', 300))}`),
        {
          type: 'input',
          block_id: 'key',
          label: { type: 'plain_text', text: 'Ticket key' },
          element: {
            type: 'static_select',
            action_id: 'value',
            options: items
              .slice(0, MAX_TICKET_OPTIONS)
              .map((i) => ({
                text: { type: 'plain_text', text: clamp(`${i.key} · ${i.headline}`, 70) },
                value: i.key,
              })),
          },
        },
      ] as any,
    },
  });
});

app.view('link_save', async ({ ack, view, client, body }) => {
  await ack();
  const key = view.state.values['key']?.['value']?.selected_option?.value;
  const recordId = (view.private_metadata || '').trim();
  const who = (body as any).user.id as string;

  if (!key || !recordId) {
    await client.chat.postMessage({
      channel: who,
      text: 'ผูกไม่ได้',
      blocks: cannotLinkBlocks(
        'ไม่รู้ว่าเป็นข้อความไหน — เกิดขึ้นเมื่อข้อความไม่ได้อยู่ใน corpus ที่ประมวลผลแล้ว ลองรัน export/prepare ใหม่',
      ),
    });
    return;
  }

  // A real write, to the file the pipeline's linker reads as its top tier.
  let total: number;
  try {
    total = saveOverride(recordId, key, who, stamp());
  } catch (err) {
    // The errno and the absolute path go to the log; Slack gets the file name.
    console.error('saveOverride failed', err);
    await client.chat.postMessage({
      channel: who,
      text: 'บันทึกไม่สำเร็จ',
      blocks: [
        section('*บันทึกไม่สำเร็จ*'),
        context(`เขียนไฟล์ ${esc(shortPath(overridesPath()))} ไม่ได้ — รายละเอียดอยู่ใน log ฝั่งเซิร์ฟเวอร์`),
      ] as any,
    });
    return;
  }

  await client.chat.postMessage({
    channel: who,
    text: `ผูกกับ ${key} แล้ว`,
    blocks: [
      section(`*🔗 ผูกกับ ${key} แล้ว*`),
      context(
        `เขียนลง \`${esc(shortPath(overridesPath()))}\` แล้ว (${total} รายการ) · ` +
          'linker จะถือเป็น tier สูงสุดในรอบถัดไป ไม่เดาทับ',
      ),
    ] as any,
  });
});

app.shortcut('mark_decision', async ({ ack, shortcut, client }) => {
  await ack();
  const s = shortcut as any;
  const msg = s.message;
  const who = s.user.id as string;
  const recordId = recordIdFor(s);
  const statement = (msg?.text ?? '').trim();

  if (!statement) {
    await client.chat.postMessage({
      channel: who,
      text: 'บันทึกไม่ได้',
      blocks: [section('*บันทึกไม่ได้ — ข้อความว่าง*')] as any,
    });
    return;
  }

  // If a decision already exists for this work item, the new one replaces it and
  // the pair is linked, which is what makes `recall` able to show the chain.
  const itemKey = itemKeyForMessage(recordId);
  const prior = itemKey
    ? ledger().decisions.filter((d) => d.related_items?.includes(itemKey) && !d.superseded_by).at(-1)
    : undefined;

  let saved;
  try {
    saved = saveDecision({
      statement: clamp(statement, 600),
      // The message's own timestamp, not now(). Falls back to now() only when the
      // payload carries no usable ts, and then it is the only date available.
      when: stampOfMessage(msg?.ts) ?? stamp(),
      user: msg?.user ?? who,
      source: 'slack',
      evidence_id: recordId,
      supersedes: prior?.id,
      related_items: itemKey ? [itemKey] : undefined,
    });
  } catch (err) {
    console.error('saveDecision failed', err);
    await client.chat.postMessage({
      channel: who,
      text: 'บันทึกไม่สำเร็จ',
      blocks: [
        section('*บันทึกไม่สำเร็จ*'),
        context(`เขียนไฟล์ ${esc(shortPath(decisionsPath()))} ไม่ได้ — รายละเอียดอยู่ใน log ฝั่งเซิร์ฟเวอร์`),
      ] as any,
    });
    return;
  }

  // Refresh only the collection that changed. The decision log is the bot's own
  // file, so re-fetching every topic from the pipeline to observe a local write
  // was both wasteful and the thing that could throw past this handler — leaving
  // the decision on disk and the person who filed it told nothing at all.
  ledger().decisions = readDecisions();

  await client.chat.postMessage({
    channel: who,
    text: 'บันทึกเป็นการตัดสินใจแล้ว',
    blocks: [
      section('*🧠 บันทึกเป็นการตัดสินใจแล้ว*'),
      section(`>${esc(clamp(statement, 300))}`),
      context(
        `id \`${esc(saved.id)}\` · ลงวันที่ ${esc(saved.when)} (เวลาของข้อความ ไม่ใช่เวลาที่กด)` +
          ` · เขียนลง \`${esc(shortPath(decisionsPath()))}\` แล้ว` +
          (prior ? ` · แทน \`${esc(prior.id)}\` — recall จะเห็นเป็นสาย` : '') +
          ` · หาเจอด้วย \`${CMD} recall\``,
      ),
    ] as any,
  });
});

/* ------------------------------------------------------------------ *
 * Emoji as input. Zero friction, and a team that already reacts to
 * everything will use this far more than a slash command.
 * ------------------------------------------------------------------ */

/** 📌 and 🧠 file a real decision. Everything else is a hint, and says so. */
const DECISION_EMOJI: Record<string, string> = {
  pushpin: '📌 บันทึกเป็นการตัดสินใจ',
  brain: '🧠 บันทึกเป็นการตัดสินใจ',
};

/**
 * Emoji with no write behind them.
 *
 * These used to answer "Meowtam บันทึกไว้ให้แล้ว" and write nothing. State is
 * computed from the messages by rules — an emoji cannot set it, and pretending it
 * did would put a claim on screen with nothing behind it. So each one points at
 * the affordance that does work.
 */
const EMOJI_HINT: Record<string, string> = {
  ticket: `🎫 อยากผูกกับ ticket — ใช้เมนู *ผูกกับ ticket* ที่ข้อความนั้น (⋯) แล้วเลือก work item · หรือ \`${CMD} <KEY>\``,
  construction: `🚧 สถานะคำนวณจากข้อความด้วยกฎ อีโมจิเปลี่ยนไม่ได้ — ถ้าติดจริง เขียนในเธรดว่าติดอะไร แล้วเช็คด้วย \`${CMD} blocked\``,
  white_check_mark: `✅ สถานะคำนวณจากข้อความด้วยกฎ อีโมจิเปลี่ยนไม่ได้ — ปิดที่ ticket แล้ว digest รอบถัดไปจะเห็นเอง (\`${CMD} digest\`)`,
};

/**
 * The text of the message someone reacted to.
 *
 * A `reaction_added` payload carries only the channel and the ts, and a decision
 * without its statement is not a decision. Try the corpus first (already resolved,
 * no API call), then the channel, then the thread — `conversations.history` does
 * not return replies.
 */
async function reactedMessage(
  client: any,
  recordId: string,
  channel: string,
  ts: string,
): Promise<{ text: string; user?: string } | undefined> {
  const known = findMessage(recordId);
  if (known?.text) return { text: known.text, user: known.user };

  const pick = (list: any[] | undefined) => list?.find((m: any) => m.ts === ts && m.text);
  try {
    const hist = await client.conversations.history({ channel, latest: ts, oldest: ts, inclusive: true, limit: 1 });
    const m = pick(hist.messages);
    if (m) return { text: m.text, user: m.user };
  } catch (err) {
    console.error('conversations.history failed', err);
  }
  try {
    const rep = await client.conversations.replies({ channel, ts, limit: 200 });
    const m = pick(rep.messages);
    if (m) return { text: m.text, user: m.user };
  } catch (err) {
    console.error('conversations.replies failed', err);
  }
  return undefined;
}

app.event('reaction_added', async ({ event, client }) => {
  const label = DECISION_EMOJI[event.reaction];
  const hint = EMOJI_HINT[event.reaction];
  if (!label && !hint) return;

  const target = event.item as any;
  if (target?.type !== 'message' || !target.channel || !target.ts) return;

  const reply = (blocks: any[], text: string) =>
    client.chat.postEphemeral({ channel: target.channel, user: event.user, text, blocks });

  if (hint) {
    await reply([context(hint)], hint);
    return;
  }
  if (!label) return;

  const recordId = `msg_${target.channel}_${target.ts}`;
  const msg = await reactedMessage(client, recordId, target.channel, target.ts);
  if (!msg) {
    await reply(
      [
        section('*ยังบันทึกไม่ได้*'),
        context('อ่านข้อความนั้นไม่ได้ (นอกช่องที่ผมอยู่ หรือไม่มีข้อความ) — ใช้เมนู *บันทึกเป็นการตัดสินใจ* ที่ข้อความแทน'),
      ],
      'ยังบันทึกไม่ได้',
    );
    return;
  }

  const itemKey = itemKeyForMessage(recordId);
  const prior = itemKey
    ? ledger().decisions.filter((d) => d.related_items?.includes(itemKey) && !d.superseded_by).at(-1)
    : undefined;

  let saved;
  try {
    saved = saveDecision({
      statement: clamp(msg.text, 600),
      when: stampOfMessage(target.ts) ?? stamp(),
      user: msg.user ?? event.user,
      source: 'slack',
      evidence_id: recordId,
      supersedes: prior?.id,
      related_items: itemKey ? [itemKey] : undefined,
    });
  } catch (err) {
    console.error('saveDecision from reaction failed', err);
    await reply(
      [
        section('*บันทึกไม่สำเร็จ*'),
        context(`เขียนไฟล์ ${esc(shortPath(decisionsPath()))} ไม่ได้ — รายละเอียดอยู่ใน log ฝั่งเซิร์ฟเวอร์`),
      ],
      'บันทึกไม่สำเร็จ',
    );
    return;
  }

  ledger().decisions = readDecisions();

  await reply(
    [
      section(`*${label} แล้ว*`),
      context(
        `id \`${esc(saved.id)}\` · ลงวันที่ ${esc(saved.when)} · เขียนลง \`${esc(shortPath(decisionsPath()))}\`` +
          (prior ? ` · แทน \`${esc(prior.id)}\`` : '') +
          ` · หาเจอด้วย \`${CMD} recall\``,
      ),
    ],
    label,
  );
});

/* ------------------------------------------------------------------ *
 * The demo driver.
 *
 * A live demo that depends on you typing the right thing in the right
 * order in front of judges will desync. This fires each beat on request,
 * in order, so the narrative is a button press rather than a memory test.
 *
 *   /meowtam demo         → what beat is next
 *   /meowtam demo 1..5    → fire that beat
 *   /meowtam demo next    → fire the next one
 *   /meowtam demo reset
 * ------------------------------------------------------------------ */

let beat = 0;

const BEATS = ['standup DM (08:45)', 'digest (09:25)', 'drift nudge', 'recall', 'my board'];

async function runDemo(
  arg: string,
  ctx: { client: any; respond: any; channel: string; user: string },
) {
  const { client, respond, channel, user } = ctx;

  if (arg === 'reset') {
    beat = 0;
    await respond({ text: 'รีเซ็ตเดโมแล้ว — beat ถัดไปคือ 1. ' + BEATS[0] });
    return;
  }
  if (!arg || arg === 'status') {
    await respond({
      text:
        `beat ถัดไป: *${beat + 1}. ${BEATS[beat] ?? 'จบแล้ว'}*\n` +
        BEATS.map((b, i) => `${i + 1}. ${b}${i < beat ? ' ✓' : ''}`).join('\n'),
    });
    return;
  }

  const n = arg === 'next' ? beat + 1 : parseInt(arg, 10);
  if (!n || n < 1 || n > BEATS.length) {
    await respond({ text: `beat 1–${BEATS.length} เท่านั้น` });
    return;
  }
  beat = n;

  switch (n) {
    case 1: {
      // The 08:45 DM. Goes to whoever ran the command so it always lands.
      // Drafts are derived from the live items, so this is your real standup
      // when you are a participant. Another person's draft is not substituted.
      const draft = standupFor(user);
      if (!draft) {
        return respond({
          text:
            'ยังไม่มี standup draft ให้คุณ — draft สร้างจาก work item ที่คุณเป็น participant\n' +
            `ตอนนี้มี draft ${ledger().standups.length} คน (${ledger().standups.map((s) => s.slack_user_id).join(', ') || 'ไม่มีเลย'})\n` +
            'ถ้า pipeline ยังไม่แปลง user id เป็นชื่อ draft จะมีเฉพาะคนที่ถูกบันทึกเป็น U… ในข้อความ',
        });
      }
      await client.chat.postMessage({
        channel: user,
        text: 'สรุปของคุณเมื่อวาน',
        blocks: standupDmBlocks({ ...draft, slack_user_id: user }) as any,
      });
      await respond({ text: '▶ beat 1 — ส่ง standup DM แล้ว ดูใน DM ของคุณ' });
      return;
    }
    case 2: {
      await client.chat.postMessage({
        channel: DIGEST_CHANNEL || channel,
        text: 'Standup digest',
        blocks: digestBlocks() as any,
      });
      await respond({ text: '▶ beat 2 — โพสต์ digest แล้ว' });
      return;
    }
    case 3: {
      const drift = ledger().drifts[0];
      const item = drift ? findItem(drift.item_key) : undefined;
      if (!drift || !item) {
        return respond({
          text: demoFixtures()
            ? 'ไม่มี drift ใน fixture'
            : 'ยังไม่มี drift — การตรวจ drift ต้องเทียบ Slack กับ ticket system ซึ่งยังไม่ได้ต่อ\n' +
              'ถ้าจะโชว์ตัวอย่างในเดโม ตั้ง DEMO_FIXTURES=1 แล้ว block จะติดป้ายว่าเป็นตัวอย่าง',
        });
      }
      // Post the "requirement changed" message first, then the nudge as a
      // threaded reply — that ordering is what sells it.
      const trigger = findMessage(drift.trigger_id);
      const posted = await client.chat.postMessage({
        channel: DIGEST_CHANNEL || channel,
        text: trigger?.text ?? 'scope change',
        blocks: [
          section(`*${esc(trigger?.user ?? 'dev')}*\n${esc(trigger?.text ?? '')}`),
        ] as any,
      });
      await new Promise((r) => setTimeout(r, 1200));
      await client.chat.postMessage({
        channel: DIGEST_CHANNEL || channel,
        thread_ts: posted.ts,
        text: 'สโคปเปลี่ยนแต่ ticket ยังไม่อัปเดต',
        blocks: driftNudgeBlocks(drift, item) as any,
      });
      await respond({ text: '▶ beat 3 — drift nudge อยู่ในเธรดแล้ว' });
      return;
    }
    case 4: {
      const q = 'ตอนนั้นเราสรุปเรื่อง export encoding ว่ายังไงนะ';
      await client.chat.postMessage({
        channel: DIGEST_CHANNEL || channel,
        text: 'recall',
        blocks: (await recallBlocks(q)) as any,
      });
      await respond({ text: `▶ beat 4 — recall: “${q}”` });
      return;
    }
    case 5: {
      const items = sortedItems().filter((i) => i.state !== 'done').slice(0, 6);
      await respond({ blocks: boardBlocks('บอร์ดรวม', items), text: 'board' });
      return;
    }
  }
}

/* ------------------------------------------------------------------ *
 * Optional real schedules. Off by default: during a demo you drive
 * everything with /meowtam demo, and a stray cron firing mid-pitch is the
 * kind of thing that ruins a demo.
 * ------------------------------------------------------------------ */

/** 'YYYY-MM-DD', 'HH', 'mm' in the scheduling zone — the host's unless told otherwise. */
const SCHEDULE_TZ = env('TAM_SCHEDULE_TZ');

function localNow(): { date: string; hour: number; minute: number } {
  const d = new Date();
  if (!SCHEDULE_TZ) {
    const p = (n: number) => String(n).padStart(2, '0');
    return {
      date: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`,
      hour: d.getHours(),
      minute: d.getMinutes(),
    };
  }
  // en-CA gives ISO-ordered date parts, which is the cheapest way to get
  // 'YYYY-MM-DD' in another zone without pulling in a date library.
  const [date, time] = new Intl.DateTimeFormat('en-CA', {
    timeZone: SCHEDULE_TZ,
    hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
    .format(d)
    .split(', ');
  const [hh, mm] = (time ?? '00:00').split(':');
  return { date: date ?? '', hour: Number(hh), minute: Number(mm) };
}

/**
 * Fire `fn` once a day at hour:minute.
 *
 * Two things this has to get right, and the minute-equality version got both
 * wrong. First, the promise: an unawaited async callback inside a timer is an
 * unhandled rejection, and one `channel_not_found` from a deactivated user in
 * STANDUP_USERS took the whole bot down — Bolt's error handling does not reach
 * a timer. Second, the clock: setInterval's delay is a floor, not a ceiling, so
 * a laptop sleeping through :45 or one slow tick meant the day's digest never
 * fired and said nothing about it. Fire on "we are past the time and have not run
 * today" instead, which is late rather than missing, and cannot double-fire.
 */
function scheduleDaily(name: string, hour: number, minute: number, fn: () => Promise<void>) {
  const boot = localNow();
  // Booting after the time is not a missed run to catch up on — starting the bot
  // in the evening must not post this morning's digest. Only a tick lost inside a
  // running process is recovered.
  let lastRun = boot.hour * 60 + boot.minute >= hour * 60 + minute ? boot.date : '';
  let running = false;

  const tick = () => {
    const now = localNow();
    const due = now.hour * 60 + now.minute >= hour * 60 + minute;
    if (!due || lastRun === now.date || running) return;
    lastRun = now.date;
    running = true;
    const late = now.hour * 60 + now.minute - (hour * 60 + minute);
    console.log(`⏰ ${name} — ${now.date} ${now.hour}:${String(now.minute).padStart(2, '0')}${late ? ` (ช้า ${late} นาที)` : ''}`);
    void fn()
      .catch((err) => console.error(`⏰ ${name} ล้ม —`, err))
      .finally(() => {
        running = false;
      });
  };

  setInterval(tick, 60_000);
  console.log(
    `⏰ ตั้ง ${name} แล้ว (${SCHEDULE_TZ || 'เวลาเครื่องนี้'})` +
      (lastRun ? ' — วันนี้เลยเวลาไปแล้ว เริ่มรอบถัดไปพรุ่งนี้' : ''),
  );
}

/**
 * Re-read before a scheduled post. The cache is filled once at boot, so without
 * this a bot left running for a week posts boot-day data every morning with only
 * `built_at` in the footer hinting at it. A failed refresh is not a reason to skip
 * the post — the footer still names the source and the date it was built.
 */
async function refreshForSchedule(name: string) {
  try {
    await reload();
  } catch (err) {
    console.error(`⏰ ${name} — โหลดข้อมูลใหม่ไม่ได้ ใช้ของเดิมที่มีในหน่วยความจำ:`, err);
  }
}

if (env('ENABLE_SCHEDULE') === '1') {
  scheduleDaily('standup DM 08:45', 8, 45, async () => {
    await refreshForSchedule('standup DM 08:45');
    for (const uid of STANDUP_USERS) {
      const draft = standupFor(uid);
      if (!draft) continue;
      // One bad user id must not cost everyone else their standup.
      try {
        await app.client.chat.postMessage({
          channel: uid,
          text: 'สรุปของคุณเมื่อวาน',
          blocks: standupDmBlocks(draft) as any,
        });
      } catch (err) {
        console.error(`⏰ standup DM ส่งให้ ${uid} ไม่ได้ —`, err);
      }
    }
  });
  scheduleDaily('digest 09:25', 9, 25, async () => {
    if (!DIGEST_CHANNEL) return;
    await refreshForSchedule('digest 09:25');
    await app.client.chat.postMessage({
      channel: DIGEST_CHANNEL,
      text: 'Standup digest',
      blocks: digestBlocks() as any,
    });
  });
}

// Load before accepting traffic, so the first command never races the fetch.
// hydrate() throws when TAM_API_URL is set and the pipeline cannot answer: a bot
// that starts anyway would serve stale fixture data that looks identical to live.
try {
  await hydrate();
} catch (err) {
  const cfg0 = apiConfig();
  console.error(`✕ อ่านจาก pipeline ไม่ได้: ${(err as Error).message}`);
  console.error(`  TAM_API_URL=${cfg0?.baseUrl} — server รันอยู่ไหม?`);
  // sample_combined.json, not combined.json: the latter is gitignored, so on a
  // fresh clone the suggested command would fail on a missing file instead.
  console.error('  cd pipeline && python3 -m tam.web.server --records data/processed/sample_combined.json --days 3650 --port 8899');
  console.error('  หรือลบ TAM_API_URL ออกถ้าจะรันกับ fixture');
  process.exit(1);
}

// Same reason as the env check above: a rejected start printed a raw Bolt stack
// trace, and 'invalid_auth' twelve frames deep is not what you want to read at 3am.
try {
  await app.start();
  running = true;
} catch (err) {
  console.error(`✕ เปิด socket กับ Slack ไม่ได้: ${(err as Error).message}`);
  console.error('  เช็ค SLACK_BOT_TOKEN (xoxb-) และ SLACK_APP_TOKEN (xapp-, scope connections:write)');
  console.error('  (ถ้าอยากดู payload เฉย ๆ โดยไม่ต่อ Slack: npm run preview)');
  process.exit(1);
}

const l = ledger();
const cfg = apiConfig();
const src = ledgerOrigin() === 'pipeline' ? `pipeline ${cfg?.baseUrl}` : 'fixture data/ledger.json';
console.log(`🐾 Meowtam พร้อมแล้ว — ${l.items.length} work items, ${l.corpus_size} ข้อความ`);
console.log(`   แหล่งข้อมูล: ${src}`);
console.log(`   ${describePolicy(postPolicy)}`);
console.log(`   เขียนเอง: ${storeSummary()}`);
console.log(
  `   standup draft ${l.standups.length} คน · drift ${l.drifts.length}` +
    (l.drifts.length === 0 && !demoFixtures() ? ' (ยังไม่ได้ต่อ ticket system)' : '') +
    (demoFixtures() ? ' · DEMO_FIXTURES=1 เปิดอยู่ — drift เป็นตัวอย่าง' : ''),
);
console.log(`   ลอง: /meowtam demo   (beats: ${BEATS.join(' → ')})`);
