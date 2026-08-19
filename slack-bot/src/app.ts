import 'dotenv/config';
import { basename } from 'node:path';
import { App, LogLevel } from '@slack/bolt';

import {
  ledger, hydrate, reload, ledgerOrigin, demoFixtures, sortedItems, itemsByState, findItem,
  findMessage, itemKeyForMessage, itemsFor, standupFor, driftFor,
} from './data.js';
import { apiConfig, fetchTracker } from './tam-api.js';
import { PostRefused, describePolicy, guardPosting, readPolicy } from './postguard.js';
import {
  dailyBefore, dailyFor, decisionsPath, markAnnounced, overridesPath, readDailies, readDecisions,
  saveDaily, saveDecision, saveOverride, storeSummary, wasAnnounced,
} from './store.js';
import {
  DailyParse, formatDailyDate, newEscalations, parseDailyReply, parseHhMm, pendingStreaks,
  streakKey, zonedNow,
} from './daily.js';
import { dailyPostBlocks, dailySummaryBlocks, dailyTemplateBlocks, dailyTitle, pendingEscalationBlocks } from './blocks/daily.js';
import { digestBlocks } from './blocks/digest.js';
import { standupDmBlocks, standupPrefill } from './blocks/standupDm.js';
import { itemCardBlocks, boardBlocks } from './blocks/itemCard.js';
import { driftNudgeBlocks, driftModal } from './blocks/drift.js';
import { recallBlocks } from './blocks/recall.js';
import { formatBlocks } from './blocks/format.js';
import { driftBlocks, silentBlocks } from './blocks/tracker.js';
import { linkResultBlocks, pastePreviewBlocks, ticketOption, type LinkResult } from './blocks/link.js';
import { staleBlocks } from './blocks/stale.js';
import { staleItems, staleKey } from './stale.js';
import { clearSimulated, seedPendingStreak, seededSummary, showSimulatedLabels, todaysSimulatedAnswers } from './demo.js';
import { channelsOf, describeProjects, labelOf, projectMap, projectOf } from './projects.js';
import { TrackerOff, addComment, describeTracker, searchTickets, trackerConfig } from './youtrack.js';
import { COMMANDS, CMD, bodyText, context, section, esc, clamp, who } from './blocks/common.js';
import { describeNames, mentionOf, resetNames } from './names.js';
import type { DailyAnswer } from './types.js';
import { pasteChat, reindex } from './tam-api.js';

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
 * The daily thread. Defaults to the digest channel so a team that has
 * configured one channel does not have to configure a second, and to
 * 09:00 / 10:45 — the summary time the team asked for, and a post time
 * far enough ahead of it to be worth answering.
 * ------------------------------------------------------------------ */
const DAILY_CHANNEL = env('DAILY_CHANNEL', DIGEST_CHANNEL);
const DAILY_AT = parseHhMm(env('TAM_DAILY_AT'), '09:00', 'TAM_DAILY_AT');
const DAILY_SUMMARY_AT = parseHhMm(env('TAM_DAILY_SUMMARY_AT'), '10:45', 'TAM_DAILY_SUMMARY_AT');
/** A pending line repeated this many dailies running gets announced in the channel. */
const PENDING_DAYS = Math.max(2, Number(env('TAM_DAILY_PENDING_DAYS', '3')) || 3);

/**
 * Working days of silence before a work item is raised in the channel.
 *
 * Working days, not days: five calendar days across a weekend is three days of
 * silence, which is ordinary, and an escalation that cannot tell those apart fires
 * every Monday about nothing. See `stale.ts` for why holidays are not modelled.
 */
const STALE_WORKDAYS = Math.max(1, Number(env('TAM_STALE_WORKDAYS', '5')) || 5);

const hhmm = (t: { hour: number; minute: number }) =>
  `${String(t.hour).padStart(2, '0')}:${String(t.minute).padStart(2, '0')}`;

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
      await runDemo(arg.slice(4).trim(), {
        client, respond,
        channel: command.channel_id,
        channelName: command.channel_name,
        user: command.user_id,
        triggerId: (body as any).trigger_id,
      });
      return;
    }

    // /meowtam daily            → the form, and only the person who asked sees it.
    //                              `respond` on a slash command is ephemeral by
    //                              default, which is exactly what was asked for.
    // /meowtam daily post        → post today's daily now (`post again` replaces it)
    // /meowtam daily summary     → run the 10:45 collection now
    if (lower === 'daily' || lower.startsWith('daily ')) {
      const sub = lower.slice(5).trim();
      if (!sub) {
        await respond({
          blocks: dailyTemplateBlocks(standupFor(command.user_id)) as any,
          text: 'ฟอร์ม daily',
        });
        return;
      }
      if (sub === 'post' || sub === 'post again' || sub === 'again') {
        await respond({ text: await postDaily(client, { force: sub !== 'post' }) });
        return;
      }
      if (sub === 'summary' || sub === 'summarise' || sub === 'summarize' || sub === 'สรุป') {
        await respond({ text: await summariseDaily(client) });
        return;
      }
      await respond({
        text:
          `ใช้ได้: \`${CMD} daily\` (ฟอร์ม เห็นคนเดียว) · ` +
          `\`${CMD} daily post\` (โพสต์ของวันนี้) · \`${CMD} daily summary\` (สรุปเธรดเดี๋ยวนี้)`,
      });
      return;
    }

    // /meowtam paste   → attach a DM or a private group to a ticket
    if (lower === 'paste' || lower === 'แนบแชท' || lower.startsWith('paste ')) {
      await openPasteModal(client, (body as any).trigger_id, {
        channel: command.channel_id,
        channelName: command.channel_name,
      });
      return;
    }

    // /meowtam stale [post]  → work nobody has touched for N working days
    if (lower === 'stale' || lower.startsWith('stale ')) {
      const sub = lower.slice(5).trim();
      if (sub === 'post' || sub === 'announce' || sub === 'ประกาศ') {
        await respond({ text: await announceStale(client, { channel: DIGEST_CHANNEL || command.channel_id, force: true }) });
        return;
      }
      const entries = staleItems(sortedItems(), { workdays: STALE_WORKDAYS });
      await respond({
        blocks: staleBlocks(entries, STALE_WORKDAYS) as any,
        text: `เงียบเกิน ${STALE_WORKDAYS} วันทำการ`,
      });
      return;
    }

    // /meowtam projects  → what the channel→project map says, from inside Slack
    if (lower === 'projects' || lower === 'project' || lower === 'โปรเจกต์') {
      const map = projectMap();
      const here = projectOf({ id: command.channel_id, name: command.channel_name });
      const lines = map.projects.length
        ? map.projects.map((key) => `• *${esc(labelOf(key))}* \`${esc(key)}\` — ${channelsOf(key).map((c) => (c.startsWith('#') ? esc(c) : `<#${c}>`)).join(', ')}`)
        : ['_ยังไม่ได้ตั้ง_ — ตัวเลือก ticket จะค้นทุกโปรเจกต์ใน YOUTRACK_PROJECTS'];
      await respond({
        text: 'channel → project',
        blocks: [
          section(`*แต่ละห้องคือโปรเจกต์อะไร*\n${lines.join('\n')}`),
          context(
            (here
              ? `ห้องนี้ = *${esc(labelOf(here))}* — เมนูผูก ticket จะค้นเฉพาะโปรเจกต์นี้`
              : 'ห้องนี้ยังไม่ได้แม็ป — เมนูผูก ticket จะค้นทุกโปรเจกต์') +
              ' · ตั้งที่ `TAM_CHANNEL_PROJECTS` ทั้งใน slack-bot/.env และ pipeline/.env (ค่าเดียวกัน)',
          ),
        ] as any,
      });
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

    // /meowtam silent | drift  → the ticket side, which Slack cannot contain
    if (lower === 'silent' || lower === 'quiet' || lower === 'drift') {
      const cfg = apiConfig();
      if (!cfg) {
        await respond({ text: 'ต้องตั้ง TAM_API_URL ให้บอทอ่านจาก pipeline ก่อน — ข้อมูล ticket มาจากฝั่งนั้น' });
        return;
      }
      const report = await fetchTracker(cfg);
      const blocks = lower === 'drift' ? driftBlocks(report) : silentBlocks(report);
      await respond({ blocks: blocks as any, text: lower === 'drift' ? 'drift' : 'ticket ที่เงียบ' });
      return;
    }

    // /meowtam format    → the form the analysis can actually read
    if (lower === 'format' || lower === 'help' || lower === 'template') {
      await respond({ blocks: formatBlocks(), text: 'รูปแบบที่ระบบอ่านได้' });
      return;
    }

    // /meowtam reload     → re-read ledger.json without restarting mid-demo
    if (lower === 'reload') {
      // Names too: somebody who just ran `tam.ingest.users --fetch` expects the
      // next command to show the real names without restarting the bot.
      resetNames();
      const l = await reload();
      const from = ledgerOrigin() === 'pipeline' ? 'pipeline' : 'fixture';
      await respond({
        text: `โหลดใหม่แล้ว (${from}): ${l.items.length} items, ${l.corpus_size} ข้อความ · ${describeNames()}`,
      });
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
      section(`*${who(m.user)}* · ${m.when} · ${m.source}`),
      section(bodyText(m.text)),
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
  const who = (body as any).user.id as string;

  // This used to be `console.log('[mock youtrack write]')` under a message that said
  // "description ใหม่ถูกเขียนลง YouTrack". It now writes, or it says it did not and
  // why — the two things it must never do again are write silently and claim falsely.
  //
  // A comment, not a description edit. Overwriting a ticket's description from a
  // Slack modal destroys whatever a PO wrote there, with no undo and no record of
  // what was lost; the proposed text goes in as a comment where the ticket's owner
  // can read it beside the original and apply it themselves.
  const proposal = [
    comment.trim() || 'สโคปในเธรดกับ ticket ไม่ตรงกัน',
    '',
    'สโคปที่คุยกันใน Slack (ข้อเสนอ ยังไม่ได้แก้ description ให้):',
    desc.trim(),
    '',
    `— เสนอจาก Slack โดย ${who} เมื่อ ${stamp()}`,
  ].join('\n');

  let blocks;
  try {
    const written = await addComment(key, proposal);
    blocks = [
      section(`*✅ เขียนลง ${esc(key)} แล้ว*`),
      section(
        `เขียนเป็น *คอมเมนต์* ไม่ได้ทับ description เดิม — comment id \`${esc(written.id)}\`\n` +
          'เจ้าของ ticket อ่านเทียบกับของเดิมแล้วค่อยแก้เองได้',
      ),
      { type: 'actions', elements: [
        { type: 'button', text: { type: 'plain_text', text: 'เปิด ticket' }, url: written.url },
      ] },
    ];
  } catch (err) {
    const why = err instanceof TrackerOff ? err.message : (err as Error).message;
    console.error('drift_save — เขียน YouTrack ไม่ได้:', err);
    blocks = [
      section(`*ยังไม่ได้เขียนลง ${esc(key)}*`),
      context(esc(why)),
      section(`ข้อความที่จะเขียน (ก๊อปไปวางเองได้):\n\`\`\`${esc(clamp(proposal, 1500))}\`\`\``),
    ];
  }

  await replyTo(client, who, { text: `อัปเดต ${key}`, blocks: blocks as any });
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
    // "ใหม่" has to be earned. The box arrives prefilled with what this person
    // already said about an item that has not moved, so a blocker submitted exactly
    // as proposed is the same obstacle a day older — announcing it as news teaches
    // the channel to skim past the word.
    const draft = standupFor(user);
    const asProposed = draft ? standupPrefill(draft).blocker === blocker : false;
    const label = asProposed ? 'ยังติดเรื่องเดิม' : 'blocker ใหม่';
    await client.chat.postMessage({
      channel: DIGEST_CHANNEL,
      // `mentionOf`, not `<@id>` written by hand: a real mention renders the
      // person's real name client-side, which would put a real name back on the
      // screen in exactly the mode (TAM_NAMES=pseudonym) that exists to keep it off.
      text: `${label}จาก ${mentionOf(user)}`,
      blocks: [section(`*⛔ ${label}* จาก ${mentionOf(user)}\n${esc(blocker)}`)] as any,
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

/**
 * Reply to the person who just pressed something, and never fail silently.
 *
 * Every one of these paths ends in a DM to the person who invoked it, and `postguard`
 * refuses a DM to anybody outside the allowlist — correctly, since that allowlist is
 * what stops a misconfigured bot DMing a workspace. But the refusal surfaces as a
 * thrown error inside a Bolt listener, which Bolt logs and swallows: the person
 * presses ผูก, everything works, the correction is written, and *nothing appears*.
 * On a laptop that has not set STANDUP_USERS that is the entire experience of the
 * feature, and it looks exactly like a bug in the feature rather than in one line of
 * `.env`.
 *
 * So the refusal is caught here and printed as the fix, with the id to add. The work
 * itself has already happened by the time this runs — this is the report, and losing
 * the report must not look like losing the work.
 */
async function replyTo(client: any, user: string, message: { text: string; blocks?: any }): Promise<boolean> {
  try {
    await client.chat.postMessage({ channel: user, ...message });
    return true;
  } catch (err) {
    if (err instanceof PostRefused) {
      console.error(
        `✕ ทำงานเสร็จแล้วแต่ตอบกลับ ${user} ไม่ได้ — ${err.message}\n` +
          `  แก้: เพิ่ม ${user} ลง SLACK_POST_ALLOWLIST (หรือ STANDUP_USERS) ใน slack-bot/.env แล้ว restart`,
      );
      return false;
    }
    throw err;
  }
}

/** Why a message could not be linked. Same wording wherever the reason differs. */
function cannotLinkBlocks(reason: string) {
  return [section('*ผูกไม่ได้*'), context(reason)] as any;
}

/**
 * Slack's ceiling on options in one response. Not a self-imposed cap this time: an
 * `external_select` may return at most 100, and unlike the old static list this is
 * the top 100 *of a query* rather than the first 100 of everything, so the ticket
 * somebody is looking for is reachable by typing more of it.
 */
const MAX_TICKET_OPTIONS = 100;

/**
 * The ticket picker's options, per keystroke.
 *
 * This used to be a fixed list of work items — the things the pipeline built out of
 * Slack. That is exactly the wrong set to offer: a ticket already named in Slack is
 * already linked, and the one a person is reaching for is the one nobody has typed
 * yet. It was also capped at 100 with no search box, so past the hundredth work item
 * a ticket was simply unreachable.
 *
 * So it asks the tracker, scoped to whatever project this channel is. The fallback
 * to work items stays for the case where no tracker is configured at all, and it
 * says which of the two the reader is looking at — a picker that silently degrades
 * to a different source is a picker that quietly cannot find things.
 *
 * `private_metadata` carries the channel, because the options request does not: Slack
 * sends the view, not the conversation it was opened from, and without it the scoping
 * this whole feature is for would be lost at exactly the moment it is needed.
 */
app.options('ticket_lookup', async ({ options, ack, body }) => {
  const typed = String((options as any).value ?? '');
  let meta: LinkMeta;
  try {
    meta = JSON.parse(String((body as any).view?.private_metadata || '{}'));
  } catch {
    meta = {};
  }
  const project = meta.project ?? '';

  try {
    const tickets = await searchTickets(typed, { projects: project ? [project] : undefined, limit: MAX_TICKET_OPTIONS });
    if (tickets.length) {
      await ack({ options: tickets.map(ticketOption) as any });
      return;
    }
    // An empty result from a working tracker is a real answer, and an empty options
    // list renders as "No results" with no explanation. One disabled-looking row says
    // which project was searched, which is usually the thing that is wrong.
    await ack({
      options: [
        {
          text: { type: 'plain_text', text: clamp(`ไม่เจอ “${typed}” ใน ${project || 'ทุกโปรเจกต์ที่ตั้งไว้'}`, 75) },
          value: '__none__',
        },
      ] as any,
    });
  } catch (err) {
    const why = err instanceof TrackerOff ? err.message : `ค้น ticket ไม่ได้: ${(err as Error).message}`;
    console.error('ticket_lookup —', err);
    // Falling back to work items rather than showing nothing: the corpus does hold
    // some ticket keys, and half a picker beats an empty one. The first row says
    // which source these came from so nobody reads a short list as the whole tracker.
    const items = sortedItems().filter((i) => /^[A-Z][A-Z0-9]{1,9}-\d+$/.test(i.key));
    const matched = typed
      ? items.filter((i) => `${i.key} ${i.headline}`.toLowerCase().includes(typed.toLowerCase()))
      : items;
    await ack({
      options: [
        { text: { type: 'plain_text', text: clamp(`⚠ ${why}`, 75) }, value: '__none__' },
        ...matched.slice(0, MAX_TICKET_OPTIONS - 1).map((i) => ({
          text: { type: 'plain_text', text: clamp(`${i.key} · ${i.headline}`, 75) },
          value: i.key,
        })),
      ] as any,
    });
  }
});

/** What a link modal has to remember between opening and being submitted. */
interface LinkMeta {
  /** The corpus id of the message being linked. Absent for the paste flow. */
  record?: string;
  channel?: string;
  project?: string;
  /** The paste flow's own fields, carried the same way. */
  paste?: boolean;
}

/** The picker block, shared by "ผูกกับ ticket" and the paste modal. */
function ticketPicker(project: string) {
  return {
    type: 'input',
    block_id: 'key',
    label: { type: 'plain_text', text: 'Ticket' },
    element: {
      type: 'external_select',
      action_id: 'ticket_lookup',
      // Zero, so the list is already populated when the modal opens: the project's
      // most recently touched tickets are usually the answer, and making somebody
      // type before seeing anything hides that.
      min_query_length: 0,
      placeholder: {
        type: 'plain_text',
        text: clamp(project ? `ค้นใน ${labelOf(project)} — พิมพ์เลขหรือคำในชื่อ` : 'พิมพ์ ticket key หรือคำในชื่อ', 150),
      },
    },
  };
}

/** The scope line under the picker, so nobody has to guess what is being searched. */
function scopeContext(channelProject: string) {
  const cfg = trackerConfig();
  if (cfg.backend === 'none') {
    return context('⚠ ยังไม่ได้ต่อ YouTrack — รายการที่เห็นมาจาก work item ที่ pipeline สร้าง ไม่ใช่ ticket ทั้งหมด');
  }
  return context(
    channelProject
      ? `ค้นเฉพาะโปรเจกต์ *${esc(labelOf(channelProject))}* เพราะห้องนี้ถูกตั้งไว้ว่าเป็นโปรเจกต์นี้ (TAM_CHANNEL_PROJECTS)`
      : 'ค้นทุกโปรเจกต์ที่ตั้งไว้ใน YOUTRACK_PROJECTS · ตั้ง TAM_CHANNEL_PROJECTS ให้ห้องนี้แล้วจะแคบลงและตรงขึ้น',
  );
}

app.shortcut('link_to_ticket', async ({ ack, shortcut, client }) => {
  await ack();
  const s = shortcut as any;
  const recordId = recordIdFor(s);

  if (!recordId) {
    await replyTo(client, s.user.id, {
      text: 'ผูกไม่ได้',
      blocks: cannotLinkBlocks('อ่านไม่ออกว่าเป็นข้อความไหน (payload ไม่มี channel/ts) — ลองใหม่จากเมนู ⋯ ที่ข้อความ'),
    });
    return;
  }

  const project = projectOf({ id: s.channel?.id, name: s.channel?.name });
  const meta: LinkMeta = { record: recordId, channel: s.channel?.id, project };

  await client.views.open({
    trigger_id: s.trigger_id,
    view: {
      type: 'modal',
      callback_id: 'link_save',
      private_metadata: JSON.stringify(meta),
      title: { type: 'plain_text', text: 'ผูกกับ ticket' },
      submit: { type: 'plain_text', text: 'ผูก' },
      close: { type: 'plain_text', text: 'ยกเลิก' },
      blocks: [
        section(`>${esc(clamp(s.message?.text ?? '', 300))}`),
        ticketPicker(project),
        scopeContext(project),
        {
          type: 'input',
          block_id: 'comment',
          optional: true,
          label: { type: 'plain_text', text: 'คอมเมนต์ที่จะเขียนลง ticket' },
          element: {
            type: 'plain_text_input',
            action_id: 'value',
            multiline: true,
            initial_value: 'ผูกกับบทสนทนาใน Slack',
          },
        },
        context(
          trackerConfig().canWrite
            ? 'ข้อความนี้จะถูกเขียนเป็นคอมเมนต์ใน YouTrack จริง พร้อมลิงก์กลับมาที่ข้อความนี้ — ลบทิ้งถ้าไม่อยากให้เขียน'
            : '⚠ ตอนนี้ยังเขียนคอมเมนต์ลง YouTrack ไม่ได้ (ยังไม่ได้เปิด YOUTRACK_WRITE) — การผูกจะถูกเก็บฝั่งเราอย่างเดียว',
        ),
      ] as any,
    },
  });
});

/**
 * Everything one link attempt does, in the order that keeps the report honest.
 *
 * Local write, then tracker write, then rebuild. Each step records its own outcome
 * on the result instead of throwing, because they fail independently: the corrections
 * file can be written while YouTrack refuses the token, and reporting only the first
 * exception would hide the half that succeeded — the half that changes what tomorrow's
 * digest says.
 *
 * The rebuild is not an optimisation. The linker reads the corrections file when it
 * runs, so without this the person is told their message is attached to a ticket while
 * every screen that could show it still says otherwise, until some later reindex.
 */
async function performLink(input: {
  recordIds: string[];
  key: string;
  by: string;
  comment: string;
  permalink?: string;
}): Promise<LinkResult> {
  const { recordIds, key, by, comment } = input;
  const result: LinkResult = { key, messages: recordIds.length };

  try {
    let total = 0;
    for (const id of recordIds) total = saveOverride(id, key, by, stamp());
    result.overridesFile = shortPath(overridesPath());
    result.overridesTotal = total;
  } catch (err) {
    console.error('saveOverride failed', err);
    result.overridesError = (err as Error).message;
  }

  if (comment.trim()) {
    try {
      const written = await addComment(
        key,
        // The permalink is the whole point of writing to the tracker: it makes the
        // link two-way, so somebody reading the ticket can reach the conversation.
        `${comment.trim()}${input.permalink ? `\n\n${input.permalink}` : ''}\n\n— ผูกจาก Slack โดย ${by} เมื่อ ${stamp()}`,
      );
      result.commentId = written.id;
      result.commentUrl = written.url;
      result.ticketUrl = written.url;
    } catch (err) {
      result.commentError = err instanceof TrackerOff ? err.message : (err as Error).message;
    }
  }

  const cfg = apiConfig();
  if (cfg) {
    try {
      await reindex(cfg);
      await reload();
      const item = findItem(key);
      if (item) {
        result.itemKey = item.key;
        result.inItem = item.messages.filter((m) => recordIds.includes(m.id)).length;
      } else {
        result.inItem = 0;
      }
      // Asked of the pipeline, not inferred here. "Not in the item" and "not in the
      // corpus" look identical from this side and need opposite things done about
      // them, and only the pipeline knows which of the two just happened.
      const report = await fetchTracker(cfg);
      const mine = new Set(recordIds);
      result.unresolved = report.unresolved_links
        .filter((bad) => mine.has(bad.record_id))
        .map((bad) => ({ record_id: bad.record_id, why: bad.why }));
    } catch (err) {
      result.rebuildError = (err as Error).message;
    }
  } else {
    result.rebuildError = 'ยังไม่ได้ตั้ง TAM_API_URL — บอทอ่านจาก fixture อยู่ จึง build ใหม่ให้ไม่ได้';
  }

  if (!result.ticketUrl) {
    const item = findItem(key);
    if (item?.youtrack_url) result.ticketUrl = item.youtrack_url;
  }
  if (cfg && result.itemKey) result.boardUrl = `${cfg.baseUrl}/item/${encodeURIComponent(result.itemKey)}`;
  return result;
}

app.view('link_save', async ({ ack, view, client, body }) => {
  await ack();
  const key = view.state.values['key']?.['ticket_lookup']?.selected_option?.value;
  const comment = view.state.values['comment']?.['value']?.value ?? '';
  const who = (body as any).user.id as string;
  let meta: LinkMeta = {};
  try {
    meta = JSON.parse(view.private_metadata || '{}');
  } catch {
    meta = {};
  }

  if (!key || key === '__none__' || !meta.record) {
    await replyTo(client, who, {
      text: 'ผูกไม่ได้',
      blocks: cannotLinkBlocks(
        !meta.record
          ? 'ไม่รู้ว่าเป็นข้อความไหน — เกิดขึ้นเมื่อข้อความไม่ได้อยู่ใน corpus ที่ประมวลผลแล้ว ลองรัน export/prepare ใหม่'
          : 'ยังไม่ได้เลือก ticket (แถวที่ขึ้นว่า “ไม่เจอ” เลือกไม่ได้ — พิมพ์คำอื่นหรือเลข ticket ดูครับ)',
      ),
    });
    return;
  }

  const permalink = meta.channel
    ? await permalinkFor(client, meta.channel, meta.record.replace(/^msg_[A-Z0-9]+_/, ''))
    : undefined;
  const result = await performLink({ recordIds: [meta.record], key, by: who, comment, permalink });

  await replyTo(client, who, { text: `ผูกกับ ${key} แล้ว`, blocks: linkResultBlocks(result) as any });
});

/* ------------------------------------------------------------------ *
 * Attaching a private conversation.
 *
 * The single biggest hole in reading only public channels: the conversation
 * that decides something often happens in a DM, a private group, or another
 * workspace, and no token the team can grant will reach it. What a person can
 * always do is select those messages, press ⌘C, and paste them — so that is
 * the affordance, and the pipeline already has the parser for Slack's
 * clipboard format.
 *
 * Two presses, not one. The parser is a heuristic over a format nobody
 * documents, and a misread paste looks exactly like a short conversation, so
 * nothing is stored until somebody has seen what it made of the text.
 * ------------------------------------------------------------------ */

/** Pastes waiting for their second press, keyed by the id in the button's value. */
const pendingPastes = new Map<string, { chat: string; title: string; day: string; key: string; user: string; at: number }>();

/** Long enough to read a preview, short enough that a forgotten paste does not linger. */
const PASTE_TTL_MS = 30 * 60_000;

function rememberPaste(entry: { chat: string; title: string; day: string; key: string; user: string }): string {
  const now = Date.now();
  for (const [id, held] of pendingPastes) {
    if (now - held.at > PASTE_TTL_MS) pendingPastes.delete(id);
  }
  const id = `p${now.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  pendingPastes.set(id, { ...entry, at: now });
  return id;
}

async function openPasteModal(
  client: any,
  triggerId: string,
  ctx: { channel?: string; channelName?: string; sample?: string },
) {
  const project = projectOf({ id: ctx.channel, name: ctx.channelName });
  const meta: LinkMeta = { channel: ctx.channel, project, paste: true };
  const today = localNow().date;
  await client.views.open({
    trigger_id: triggerId,
    view: {
      type: 'modal',
      callback_id: 'paste_save',
      private_metadata: JSON.stringify(meta),
      title: { type: 'plain_text', text: 'แนบแชทเข้ากับ ticket' },
      submit: { type: 'plain_text', text: 'อ่านให้ดูก่อน' },
      close: { type: 'plain_text', text: 'ยกเลิก' },
      blocks: [
        section(
          '*วางแชทจาก DM หรือห้องที่บอทเข้าไม่ถึง*\n' +
            'เลือกข้อความใน Slack แล้ว ⌘C มาวางตรงนี้ได้เลย — ผมอ่านชื่อคนกับเวลาออกเอง',
        ),
        ticketPicker(project),
        scopeContext(project),
        {
          type: 'input',
          block_id: 'title',
          label: { type: 'plain_text', text: 'แชทนี้คือแชทอะไร' },
          element: {
            type: 'plain_text_input',
            action_id: 'value',
            placeholder: { type: 'plain_text', text: 'เช่น DM พี่ Natta เรื่อง redemption' },
          },
        },
        {
          type: 'input',
          block_id: 'day',
          label: { type: 'plain_text', text: 'วันที่แชทนี้เริ่ม' },
          element: { type: 'datepicker', action_id: 'value', initial_date: today },
        },
        context(
          'ต้องบอกวัน เพราะคลิปบอร์ดของ Slack มีแต่เวลา ไม่มีวันที่ — ' +
            'ถ้าไม่บอก แชทเมื่อวันศุกร์จะไปนั่งอยู่คนละสัปดาห์กับข้อความที่มันเกี่ยวข้องด้วย',
        ),
        {
          type: 'input',
          block_id: 'chat',
          label: { type: 'plain_text', text: 'แชทที่ก๊อปมา' },
          element: {
            type: 'plain_text_input',
            action_id: 'value',
            multiline: true,
            ...(ctx.sample ? { initial_value: ctx.sample } : {}),
            // One line: a placeholder is capped at 150 characters and a newline inside
            // one is not worth finding out about from a 400 on stage.
            placeholder: { type: 'plain_text', text: 'ชื่อคนพูด  [2:21 PM] แล้วขึ้นบรรทัดใหม่เป็นเนื้อความ' },
          },
        },
      ] as any,
    },
  });
}

app.view('paste_save', async ({ ack, view, client, body }) => {
  const who = (body as any).user.id as string;
  const key = view.state.values['key']?.['ticket_lookup']?.selected_option?.value ?? '';
  const chat = view.state.values['chat']?.['value']?.value ?? '';
  const title = view.state.values['title']?.['value']?.value ?? '';
  const day = view.state.values['day']?.['value']?.selected_date ?? localNow().date;

  if (!key || key === '__none__') {
    // Answering inside the modal rather than closing it and DMing: the person still
    // has their paste in the box, and losing it to a validation error would be the
    // kind of thing nobody tries twice.
    await ack({
      response_action: 'errors',
      errors: { key: 'เลือก ticket ก่อนครับ (แถวที่ขึ้นว่า “ไม่เจอ” เลือกไม่ได้)' },
    } as any);
    return;
  }
  const cfg = apiConfig();
  if (!cfg) {
    await ack({
      response_action: 'errors',
      errors: { chat: 'ยังไม่ได้ตั้ง TAM_API_URL — บอทเก็บแชทเข้า corpus เองไม่ได้ ต้องมี pipeline' },
    } as any);
    return;
  }
  await ack();

  try {
    const preview = await pasteChat(cfg, { chat, title, day, dryRun: true });
    const id = rememberPaste({ chat, title, day, key, user: who });
    await replyTo(client, who, {
      text: `อ่านแชทได้ ${preview.records.length} ข้อความ`,
      blocks: pastePreviewBlocks({
        title: preview.title,
        day: preview.day,
        key,
        records: preview.records,
        skipped: preview.skipped ?? [],
        actionValue: id,
      }) as any,
    });
  } catch (err) {
    console.error('paste preview failed', err);
    await replyTo(client, who, {
      text: 'อ่านแชทไม่ได้',
      blocks: [
        section('*อ่านแชทที่วางมาไม่ได้*'),
        context(esc((err as Error).message)),
        context('รูปแบบที่อ่านออกคือ `ชื่อคน  [2:21 PM]` แล้วขึ้นบรรทัดใหม่เป็นเนื้อความ — ก๊อปให้เริ่มที่ชื่อคนพูด'),
      ] as any,
    });
  }
});

app.action('paste_cancel', async ({ ack, respond, action }) => {
  await ack();
  pendingPastes.delete(String((action as any).value ?? ''));
  await respond({ replace_original: true, text: 'ยกเลิกแล้ว — ยังไม่ได้เก็บอะไรลง corpus' });
});

app.action('paste_confirm', async ({ ack, respond, action, body }) => {
  await ack();
  const id = String((action as any).value ?? '');
  const held = pendingPastes.get(id);
  const who = (body as any).user.id as string;
  if (!held) {
    await respond({
      replace_original: false,
      text: 'แชทก้อนนี้หมดอายุแล้ว (เก็บไว้ 30 นาที) — วางใหม่อีกครั้งด้วย `' + CMD + ' paste`',
    });
    return;
  }
  pendingPastes.delete(id);
  const cfg = apiConfig();
  if (!cfg) {
    await respond({ replace_original: false, text: 'ยังไม่ได้ตั้ง TAM_API_URL — เก็บให้ไม่ได้' });
    return;
  }

  await respond({ replace_original: true, text: 'กำลังเก็บเข้า corpus แล้ว build ใหม่ — สักครู่ครับ' });

  const result: LinkResult = { key: held.key, messages: 0 };
  try {
    // One call does the corpus write, the link overrides and the rebuild, in that
    // order and under one lock. Doing it from here in three calls would leave a
    // window where the corpus holds the messages and the linker has not been told
    // where they belong — which the digest would render as an unassigned thread.
    const stored = await pasteChat(cfg, { chat: held.chat, title: held.title, day: held.day, linkKey: held.key, by: who });
    result.messages = stored.records.length;
    result.overridesFile = shortPath(overridesPath());
    result.overridesTotal = stored.linked;
    result.itemKey = stored.item_key || undefined;
    result.inItem = stored.in_item ?? 0;
    await reload();
  } catch (err) {
    console.error('paste ingest failed', err);
    await respond({
      replace_original: false,
      text: 'เก็บไม่สำเร็จ',
      blocks: [section('*เก็บแชทไม่สำเร็จ*'), context(esc((err as Error).message))] as any,
    });
    return;
  }

  if (held.key) {
    try {
      const written = await addComment(
        held.key,
        `แนบบทสนทนาจาก Slack: “${held.title}” (${held.day}) — ${result.messages} ข้อความ\n` +
          `เก็บเข้า corpus ของ Meowtam แล้ว โดย ${who} เมื่อ ${stamp()}`,
      );
      result.commentId = written.id;
      result.ticketUrl = written.url;
    } catch (err) {
      result.commentError = err instanceof TrackerOff ? err.message : (err as Error).message;
    }
  }
  if (result.itemKey) result.boardUrl = `${cfg.baseUrl}/item/${encodeURIComponent(result.itemKey)}`;

  await respond({ replace_original: false, text: `แนบเข้า ${held.key} แล้ว`, blocks: linkResultBlocks(result) as any });
});

/* ------------------------------------------------------------------ *
 * Work that has gone quiet. See stale.ts for why it is counted in
 * working days and why nobody gets tagged.
 * ------------------------------------------------------------------ */

async function announceStale(client: any, opts: { channel: string; force?: boolean }): Promise<string> {
  const entries = staleItems(sortedItems(), { workdays: STALE_WORKDAYS });
  if (!entries.length) return `ไม่มีงานที่เงียบเกิน ${STALE_WORKDAYS} วันทำการ`;

  const fresh = opts.force ? entries : entries.filter((e) => !wasAnnounced('stale', staleKey(e, STALE_WORKDAYS)));
  if (!fresh.length) {
    return `มี ${entries.length} งานที่เงียบเกิน ${STALE_WORKDAYS} วันทำการ แต่ประกาศไปหมดแล้ว (\`${CMD} stale\` ดูได้)`;
  }
  if (!opts.channel) return 'ยังไม่ได้ตั้งช่องให้ประกาศ (DIGEST_CHANNEL)';

  await client.chat.postMessage({
    channel: opts.channel,
    text: `งานที่เงียบเกิน ${STALE_WORKDAYS} วันทำการ`,
    blocks: staleBlocks(fresh, STALE_WORKDAYS) as any,
  });
  // After Slack accepted it, never before: marking it announced up front is how the
  // one message that mattered gets swallowed by a failed post.
  markAnnounced('stale', fresh.map((e) => staleKey(e, STALE_WORKDAYS)), stamp(), `${STALE_WORKDAYS} วันทำการ`);
  return `ประกาศ ${fresh.length} งานที่เงียบเกิน ${STALE_WORKDAYS} วันทำการแล้ว`;
}

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
 *   /meowtam demo 1..N    → fire that beat
 *   /meowtam demo next    → fire the next one
 *   /meowtam demo reset   → back to beat 1 (does not touch data)
 *   /meowtam demo clear   → remove every simulated morning this wrote
 *
 * The beats run on the clock the real jobs run on, and in that exact
 * order: 08:45 the DM, 09:00 the daily post, the thread fills up, 09:25
 * the digest, 09:30 the silence check, 10:45 the summary. Read the
 * `scheduleDaily` calls at the bottom of this file and you are reading
 * this list again. That is the point — nothing on stage happens before
 * the thing it reads from, and nobody has to explain a jump backwards.
 *
 * The previous order put the 08:45 DM at beat 7, after two channel posts
 * it precedes in real life. It demonstrated the same features and told a
 * story of a morning that cannot happen.
 *
 * The first seven beats are that morning, in sequence, because the three
 * claims worth showing are all *about elapsed time*: a morning happens,
 * the same blocker survives it, and a work item nobody has mentioned all
 * week surfaces on its own. Beats 8–11 are the rest of the day, which has
 * no clock — they are the things a person reaches for when something
 * happens, in the order of how often that is.
 *
 * Beats 1 and 4 write simulated data, flagged as such everywhere it is
 * rendered and removable with `demo clear`. Everything else runs the real
 * code path against whatever data is actually there — beat 8 really does
 * ingest a chat, and beat 9 writes a real comment into YouTrack.
 * ------------------------------------------------------------------ */

let beat = 0;

/**
 * A beat, and the thing it stands in for on an ordinary morning.
 *
 * `real` is not a caption. It is what somebody types — or what fires on its
 * own with `ENABLE_SCHEDULE=1` — to get exactly what the beat just produced,
 * and `demo status` prints it beside every beat. Anyone who watches the demo
 * has therefore also read the command list, and the first question after a
 * demo ("so how do I actually do that?") is answered on the screen they are
 * already looking at.
 */
interface Beat {
  title: string;
  /** The command a person types on an ordinary morning to get this. */
  real: string;
  /** When it happens by itself, in words. */
  when: string;
  /** Which screen it lands on — a channel, a DM, a thread, or nobody but the caller. */
  where: string;
  /**
   * What is fabricated here, in the presenter's own words.
   *
   * Every beat carries one, including the eight that fabricate nothing, because "this
   * part is real" is the sentence a judge most wants and a presenter most often
   * forgets to say. Beats 1 and 4 replace it at runtime with what they actually
   * seeded — counts, names, and the line that repeats.
   */
  fake: string;
}

/** `<#C0…>` renders as the channel's name client-side; an unset channel says so. */
const roomOf = (id: string) => (id ? `<#${id}>` : 'ห้องที่พิมพ์คำสั่ง');
const DAILY_ROOM = roomOf(DAILY_CHANNEL || DIGEST_CHANNEL);
const DIGEST_ROOM = roomOf(DIGEST_CHANNEL);

const BEATS: Beat[] = [
  { title: 'เตรียมเช้าที่ผ่านมา — เขียนประวัติ daily ย้อนหลัง (จำลอง)', real: 'ไม่มี — ของจริงคือเช้าที่ผ่านไปเองจริง ๆ', when: 'ไม่ต้องยิง — เช้าพวกนั้นผ่านไปเองอยู่แล้ว', where: 'ไฟล์ dailies.json ไม่ได้ลง Slack', fake: 'ทั้ง beat นี้คือการจำลอง' },
  { title: '08:45 · standup DM — ผมร่างของเมื่อวานให้ ไม่ได้ถาม', real: 'ยิงเอง 08:45 (ENABLE_SCHEDULE=1) · ในการ์ดกดปุ่ม “ส่ง” หรือ “ข้ามวันนี้”', when: 'ทุกเช้า 08:45', where: 'DM ของแต่ละคนใน STANDUP_USERS', fake: 'ไม่จำลอง — draft มาจาก work item จริงที่คนนั้นมีชื่ออยู่' },
  // The two configurable times are read, not typed: `TAM_DAILY_AT` moves the post
  // and the beat list has to move with it, or the demo announces a time the bot
  // does not keep.
  { title: `${hhmm(DAILY_AT)} · โพสต์ daily ในห้อง — ยกยอดที่ค้างขึ้นหัว`, real: `${CMD} daily post`, when: `ทุกเช้า ${hhmm(DAILY_AT)}`, where: DAILY_ROOM, fake: 'โพสต์เป็นของจริง · บรรทัด “ค้างจากเมื่อวาน” นับจากเช้าที่ beat 1 จำลองไว้' },
  { title: 'ทีมตอบในเธรด daily (จำลอง)', real: `${CMD} daily → ได้ฟอร์มเห็นคนเดียว แล้วตอบในเธรด`, when: 'ระหว่างเช้า ทีมพิมพ์กันเอง', where: 'เธรดของโพสต์ daily', fake: 'คำตอบของวันนี้ทั้งหมด' },
  { title: '09:25 · digest ลงห้อง — ที่ติดขึ้นก่อน', real: `${CMD} digest`, when: 'ทุกเช้า 09:25', where: DIGEST_ROOM, fake: 'ไม่จำลอง — อ่านจาก pipeline ตรง ๆ ทั้งการ์ด' },
  { title: `09:30 · งานที่ไม่มีใครแตะเกิน ${STALE_WORKDAYS} วันทำการ → ประกาศในห้อง`, real: `${CMD} stale post (ดูเงียบ ๆ ก่อน: ${CMD} stale)`, when: 'ทุกเช้า 09:30 หลัง digest', where: DIGEST_ROOM, fake: 'ไม่จำลอง — นับจากวันที่ของข้อความล่าสุดจริง ไม่มีงานเงียบพอก็ไม่ประกาศ' },
  { title: `${hhmm(DAILY_SUMMARY_AT)} · สรุปเธรด + ประกาศเรื่องที่ค้างติดกัน ${PENDING_DAYS} เช้า`, real: `${CMD} daily summary`, when: `ทุกเช้า ${hhmm(DAILY_SUMMARY_AT)}`, where: `ในเธรด daily · ถ้ามีเรื่องค้างครบ ${PENDING_DAYS} เช้า ประกาศอีกข้อความใน ${DAILY_ROOM}`, fake: 'การสรุปกับการนับเป็นของจริง · สิ่งที่ถูกสรุปคือคำตอบจาก beat 1 กับ beat 4' },
  { title: 'แชทจาก DM → ผูกเข้า ticket → build ใหม่', real: `${CMD} paste (หรือเมนู ⋯ ที่ข้อความ → ผูกกับ ticket)`, when: 'ตอนไหนก็ได้ ที่มีบทสนทนาใน DM ต้องเก็บ', where: 'ฟอร์มกับผลลัพธ์เห็นคนเดียว · ของที่เขียนจริงคือ corpus + link override + คอมเมนต์บน ticket', fake: 'แชทที่ใส่มาให้ในฟอร์มเป็นตัวอย่าง (ลบแล้ววางของจริงได้) · การเก็บเข้า corpus เป็นของจริง' },
  { title: 'สโคปเปลี่ยนแต่ ticket ไม่เปลี่ยน → เขียนคอมเมนต์ลง ticket', real: `${CMD} drift แล้วกด “ดูร่างที่เสนอ” ในการ์ด`, when: 'ตอนไหนก็ได้ · ทุกครั้งที่เทียบ Slack กับ ticket', where: `${DIGEST_ROOM} · คอมเมนต์ไปโผล่บน ticket จริงเมื่อ YOUTRACK_WRITE=1`, fake: 'ไม่จำลอง — เทียบกับ ticket จริง (ถ้าเป็น drift จาก fixture การ์ดจะติดป้ายบอกเอง)' },
  { title: 'recall — ค้นด้วยความหมาย พร้อมสายการตัดสินใจ', real: `${CMD} recall <คำถาม> (พิมพ์อะไรที่ไม่ใช่คำสั่งก็ถือเป็น recall)`, when: 'ตอนไหนก็ได้ ตอนนึกไม่ออกว่าสรุปกันไว้ว่าอะไร', where: `${DIGEST_ROOM} (ถ้าพิมพ์เอง จะเห็นคนเดียว)`, fake: 'ไม่จำลอง — ค้นจาก corpus จริง คำถามเป็นตัวที่ตั้งไว้ให้' },
  { title: 'บอร์ดรวม', real: `${CMD} (ของตัวเอง) · ${CMD} @ชื่อ · ${CMD} <TICKET-123> · ${CMD} blocked`, when: 'ตอนไหนก็ได้', where: 'เห็นคนเดียว ไม่รบกวนห้อง', fake: 'ไม่จำลอง — งานจริงทั้งหมด' },
];

/**
 * A chat as Slack's clipboard really produces it, for the paste beat.
 *
 * Copied from the format `tam.ingest.slack_paste` was written against, furniture
 * and all — the `6 replies` marker, the clock glued to the body, the bare clock
 * for a second message from the same person. A tidy sample would demonstrate a
 * parser nobody needs.
 */
const SAMPLE_PASTE = [
  'Aim Sirawith  [2:21 PM]',
  'พี่ครับ หน้า redemption ที่คุยกันเมื่อวาน สรุปว่าใช้ voucher code เดิมใช่ไหมครับ',
  '[2:22 PM]',
  'คือถ้าเปลี่ยน format ผมต้องแก้ทั้ง BE ด้วย',
  'jah natta  [2:24 PM]',
  'ใช้เดิมไปก่อนนะ PO ยังไม่ยืนยัน format ใหม่',
  '[2:25 PM]',
  'ถ้าจะเปลี่ยนจริงเดี๋ยวเปิด ticket ใหม่ให้ ไม่ต้องแก้ของเดิม',
  'Aim Sirawith  [2:26 PM]',
  'โอเคครับ งั้นผมทำ REVERAPP-140 ต่อด้วยของเดิม',
].join('\n');

async function runDemo(
  arg: string,
  ctx: { client: any; respond: any; channel: string; channelName?: string; user: string; triggerId?: string },
) {
  const { client, channel, user } = ctx;
  const roster = STANDUP_USERS.length ? STANDUP_USERS : [user];

  /**
   * Which beat's reply is being written, so every one of them can end the same way.
   *
   * "▶ beat 6 — ประกาศ 2 งานที่เงียบเกิน 5 วันทำการแล้ว" tells the presenter what just
   * happened and nothing about the product: when this fires on an ordinary Tuesday,
   * which screen it lands on, what a person would type to get it without a demo
   * driver. Those three are the questions the room asks, and the reply is ephemeral —
   * only the presenter sees it — so it can carry the answers as a teleprompter
   * instead of leaving them to memory.
   */
  let firing = 0;
  let seeded = '';
  const respond = async (msg: any) => {
    const current = firing ? BEATS[firing - 1] : undefined;
    if (!current) return ctx.respond(msg);
    const tail = [
      '━━ ถ้าไม่ได้เดโม อันนี้คือ',
      `เกิดตอน: ${current.when}`,
      `ขึ้นที่: ${current.where}`,
      `สั่งเอง: ${current.real}`,
      // Last line and the one a judge's question lands on. `seeded` is what the beat
      // actually fabricated this run — counts, names, the line that repeats — and the
      // static `fake` is the fallback, which for eight of the eleven beats is the
      // sentence "nothing here is fabricated".
      `จำลองอะไร: ${seeded || current.fake}`,
    ].join('\n');
    if (Array.isArray(msg?.blocks)) {
      return ctx.respond({ ...msg, blocks: [...msg.blocks, context(tail)] });
    }
    return ctx.respond({ ...msg, text: `${msg?.text ?? ''}\n\n${tail}` });
  };

  if (arg === 'reset') {
    beat = 0;
    await respond({ text: 'รีเซ็ตเดโมแล้ว — beat ถัดไปคือ 1. ' + BEATS[0]?.title + ' (ข้อมูลจำลองยังอยู่ ลบด้วย `' + CMD + ' demo clear`)' });
    return;
  }
  if (arg === 'clear') {
    const removed = clearSimulated();
    beat = 0;
    await respond({
      text:
        `ลบข้อมูลจำลองแล้ว: ${removed.days} เช้า, ${removed.answers} คำตอบ · ` +
        'ที่เหลือในไฟล์คือของจริงล้วน',
    });
    return;
  }
  if (!arg || arg === 'status') {
    await respond({
      text:
        `beat ถัดไป: *${beat + 1}. ${BEATS[beat]?.title ?? 'จบแล้ว'}*\n` +
        // The morning is beats 1–7 in clock order; the rest of the day has no
        // clock. Saying so in the list is cheaper than saying it out loud, and it
        // stops "why is stale before the summary?" from being asked on stage.
        '_1–7 คือเช้าหนึ่งเช้าเรียงตามนาฬิกา · 8–11 คือของที่หยิบใช้ตอนไหนก็ได้_\n' +
        BEATS.map(
          (b, i) => `${i + 1}. ${b.title}${i < beat ? ' ✓' : ''}\n    ↳ ของจริง: ${b.real}`,
        ).join('\n'),
    });
    return;
  }

  const n = arg === 'next' ? beat + 1 : parseInt(arg, 10);
  if (!n || n < 1 || n > BEATS.length) {
    await respond({ text: `beat 1–${BEATS.length} เท่านั้น (หรือ \`next\` / \`reset\` / \`clear\`)` });
    return;
  }
  beat = n;
  firing = n;
  seeded = '';

  switch (n) {
    case 1: {
      // The mornings before this one. `PENDING_DAYS - 1` of them, so today's post is
      // exactly the Nth in the run and the threshold is crossed by the demo rather
      // than already sitting past it — the point is watching it tip over.
      const dates = seedPendingStreak({
        today: localNow().date,
        channel: DAILY_CHANNEL || channel,
        users: roster,
        mornings: PENDING_DAYS - 1,
      });
      if (!dates.length) {
        await respond({ text: 'ไม่มีคนให้จำลอง — ตั้ง STANDUP_USERS ก่อน' });
        return;
      }
      seeded = seededSummary(dates.length, Math.min(roster.length, 3));
      await respond({
        text:
          `▶ beat 1 — เขียนประวัติ daily ${dates.length} เช้าไว้แล้ว (${dates.join(', ')})\n` +
          `ทุกเช้ามีบรรทัดเดิมจาก ${mentionOf(roster[0] ?? '')} ว่ารอ requirement หน้า redemption จาก PO ` +
          'เขียนคนละแบบทุกวัน เพื่อให้เห็นว่าตัวนับจับได้ว่าเป็นเรื่องเดียวกัน\n' +
          '⚠️ ข้อมูลจำลอง ติดป้ายไว้ทุกที่ที่แสดง · ลบด้วย `' + CMD + ' demo clear`',
      });
      return;
    }
    case 2: {
      // The real 08:45 job, fired now: everybody in STANDUP_USERS, not just whoever
      // typed the command. Showing one person their own DM demonstrates the block;
      // it does not demonstrate the morning, which is the thing being demoed.
      // Falls back to the caller when no roster is configured, so the beat still
      // does something on a fresh machine.
      const targets = STANDUP_USERS.length ? STANDUP_USERS : [user];
      const sent: string[] = [];
      const noDraft: string[] = [];
      const failed: string[] = [];
      for (const uid of targets) {
        const draft = standupFor(uid);
        if (!draft) {
          noDraft.push(uid);
          continue;
        }
        // One bad id must not cost the others their standup — same reason the real
        // schedule catches per user rather than around the loop.
        try {
          await client.chat.postMessage({
            channel: uid,
            text: 'สรุปของคุณเมื่อวาน',
            blocks: standupDmBlocks({ ...draft, slack_user_id: uid }) as any,
          });
          sent.push(uid);
        } catch (err) {
          console.error(`demo 2 — DM ${uid} ไม่ได้:`, err);
          failed.push(uid);
        }
      }
      const lines = [`▶ beat 2 — ส่ง standup DM แล้ว ${sent.length}/${targets.length} คน`];
      if (sent.length) lines.push(`ส่งแล้ว: ${sent.map((u) => mentionOf(u)).join(', ')}`);
      if (noDraft.length) {
        lines.push(
          `ไม่มี draft: ${noDraft.map((u) => mentionOf(u)).join(', ')} — draft สร้างจาก work item ` +
            'ที่คนนั้นเป็น participant แบบ Slack id (ตอนนี้มี draft ' +
            `${ledger().standups.length} คน)`,
        );
      }
      if (failed.length) {
        lines.push(
          `ส่งไม่ได้: ${failed.map((u) => mentionOf(u)).join(', ')} — ดู log ` +
            '(ปลายทางต้องอยู่ใน allowlist ที่ขึ้นตอนบูตด้วย)',
        );
      }
      await respond({ text: lines.join('\n') });
      return;
    }
    case 3: {
      await respond({ text: `▶ beat 3 — ${await postDaily(client)}` });
      return;
    }
    case 4: {
      const today = localNow().date;
      const record = dailyFor(today);
      if (!record) {
        await respond({ text: 'ยังไม่มีโพสต์ daily ของวันนี้ — สั่ง beat 3 ก่อน' });
        return;
      }
      const answers = todaysSimulatedAnswers(roster, today);
      // Stored, not posted as somebody else. The bot cannot post as a colleague and
      // must not try: a message that looks like it came from a real person is a
      // forgery whatever the intent. It goes in the record, labelled, and the
      // summary at beat 7 reads it from there.
      saveDaily({
        ...record,
        answers: [...record.answers.filter((a) => !answers.some((sim) => sim.user === a.user)), ...answers],
      });
      if (record.ts) {
        // With labels on this block says what it is. With them off it says what a
        // collected thread says, and the attribution to named people stays either
        // way — which is the part `DEMO_SHOW_SIMULATED` costs, and the reason the
        // ephemeral reply below still spells it out to whoever pressed the button.
        const labelled = showSimulatedLabels();
        await client.chat.postMessage({
          channel: record.channel,
          thread_ts: record.ts,
          text: labelled ? 'คำตอบจำลองของเดโม' : 'คำตอบในเธรดตอนนี้',
          blocks: [
            section(
              (labelled ? '*⚠️ คำตอบจำลองสำหรับเดโม* — ไม่ใช่ข้อความของใครจริง ๆ\n' : '*คำตอบที่เก็บได้ตอนนี้*\n') +
                answers
                  .map((a) => `*${mentionOf(a.user)}*: ${esc(a.focus[0] ?? '-')}${a.blockers.length ? `\n⛔ ${esc(a.blockers[0]?.text ?? '')}` : ''}`)
                  .join('\n'),
            ),
            context(
              labelled
                ? 'ใครอยากตอบจริงพิมพ์ในเธรดนี้ได้เลย — ของจริงจะทับของจำลองของคนคนนั้นตอนสรุป'
                : 'ตอบเพิ่มในเธรดนี้ได้เลย — ตอนสรุปผมอ่านของทุกคนที่พิมพ์ไว้',
            ),
          ] as any,
        });
      }
      // The lines themselves, not a count: the presenter is about to be asked what
      // exactly was made up, and reading it off the screen beats remembering it.
      seeded =
        `คำตอบของวันนี้ ${answers.length} คน — ` +
        answers
          .map((a) => `${mentionOf(a.user)}: ${a.focus[0] ?? '-'}${a.blockers[0] ? ` ⛔ ${a.blockers[0].text}` : ''}`)
          .join(' · ');
      await respond({
        text:
          `▶ beat 4 — ใส่คำตอบจำลอง ${answers.length} คนเข้า daily ของวันนี้แล้ว · ` +
          `${mentionOf(roster[0] ?? '')} ยังติดเรื่องเดิมเป็นเช้าที่ ${PENDING_DAYS}`,
      });
      return;
    }
    case 5: {
      await client.chat.postMessage({
        channel: DIGEST_CHANNEL || channel,
        text: 'Standup digest',
        blocks: digestBlocks() as any,
      });
      await respond({ text: '▶ beat 5 — โพสต์ digest แล้ว' });
      return;
    }
    case 6: {
      const entries = staleItems(sortedItems(), { workdays: STALE_WORKDAYS });
      if (!entries.length) {
        // No invention. If nothing is that quiet, say so and show the quietest thing
        // there is with its real number — a fabricated stale item would be the one
        // claim on stage that nobody could check.
        const quietest = sortedItems()
          .filter((i) => i.state !== 'done')
          .sort((a, b) => (a.last < b.last ? -1 : 1))[0];
        await respond({
          text:
            `▶ beat 6 — ไม่มีงานไหนเงียบถึง ${STALE_WORKDAYS} วันทำการ จึงไม่ประกาศ (ไม่ได้ปั้นขึ้นมา)\n` +
            (quietest
              ? `งานที่เงียบที่สุดตอนนี้คือ ${quietest.key} ข้อความล่าสุด ${quietest.last}`
              : 'ยังไม่มีงานที่ยังไม่ปิดเลย') +
            `\nปรับเกณฑ์ได้ที่ TAM_STALE_WORKDAYS (ตอนนี้ ${STALE_WORKDAYS})`,
        });
        return;
      }
      await respond({ text: `▶ beat 6 — ${await announceStale(client, { channel: DIGEST_CHANNEL || channel, force: true })}` });
      return;
    }
    case 7: {
      await respond({ text: `▶ beat 7 — ${await summariseDaily(client)}` });
      return;
    }
    case 8: {
      if (!ctx.triggerId) {
        await respond({ text: 'beat 8 ต้องเปิด modal — สั่งจาก slash command เท่านั้น' });
        return;
      }
      await openPasteModal(client, ctx.triggerId, {
        channel,
        channelName: ctx.channelName,
        sample: SAMPLE_PASTE,
      });
      await respond({
        text:
          '▶ beat 8 — เปิดฟอร์มแนบแชทให้แล้ว (ใส่ตัวอย่างแชทจาก DM ไว้ให้ ลบทิ้งแล้ววางของจริงได้)\n' +
          'เลือก ticket → กด “อ่านให้ดูก่อน” → ผมจะอ่านให้ดูก่อนว่าแยกคนพูดถูกไหม แล้วค่อยกดเก็บ\n' +
          'ตอนเก็บ: เข้า corpus → เขียน link override → build ใหม่ → บอกว่าตอนนี้อยู่ใต้ ticket ไหนจริง ๆ',
      });
      return;
    }
    case 9: {
      const drift = ledger().drifts[0];
      const item = drift ? findItem(drift.item_key) : undefined;
      if (!drift || !item) {
        // There are two drift sources and they are not interchangeable. The ledger's
        // drift powers the threaded nudge below and only ever exists in fixture mode;
        // the tracker comparison reads real tickets and exists whenever YouTrack is
        // configured. Telling somebody to set DEMO_FIXTURES=1 while TAM_API_URL is
        // set was advice that cannot work — fixture drifts name fixture messages, so
        // `resolvableDrifts` drops every one of them against pipeline items. So use
        // the source that is actually populated, and say which one that was.
        const cfg = apiConfig();
        const report = cfg ? await fetchTracker(cfg) : undefined;
        if (report?.drift?.length) {
          await client.chat.postMessage({
            channel: DIGEST_CHANNEL || channel,
            text: 'Slack กับ ticket ไม่ตรงกัน',
            blocks: driftBlocks(report) as any,
          });
          return respond({
            text:
              `▶ beat 9 — โพสต์ drift จริงจาก ticket แล้ว (${report.drift.length} เรื่อง)\n` +
              'อันนี้มาจากการเทียบ Slack กับ YouTrack ไม่ใช่ fixture — แต่เป็นการ์ดรวม ไม่ใช่ nudge ในเธรด ' +
              'เพราะ nudge ต้องรู้ว่าข้อความไหนทำให้สโคปเปลี่ยน ซึ่งมีแค่ใน ledger',
          });
        }
        return respond({
          text: [
            'ยังไม่มี drift ให้โชว์ — มีสองแหล่ง และตอนนี้ว่างทั้งคู่:',
            `• drift ใน ledger (ตัวที่ทำ nudge ในเธรดได้): ${ledger().drifts.length} รายการ` +
              (ledgerOrigin() === 'pipeline'
                ? ' — อ่านจาก pipeline อยู่ ซึ่งไม่มี drift ให้ และ `DEMO_FIXTURES=1` ช่วยไม่ได้ที่นี่ ' +
                  '(drift ใน fixture อ้างข้อความของ fixture กดตามไม่ได้ จึงถูกตัดออก) ' +
                  'จะใช้ต้องเอา TAM_API_URL ออกให้บอทกลับไปใช้ ledger ของตัวเอง'
                : ' — ตั้ง `DEMO_FIXTURES=1` แล้ว restart'),
            `• การเทียบกับ ticket (\`${CMD} drift\`): ${
              cfg ? `ต่อ pipeline อยู่ แต่ไม่พบเรื่องที่ไม่ตรงกัน` : 'ยังไม่ได้ตั้ง TAM_API_URL'
            }`,
          ].join('\n'),
        });
      }
      // Post the "requirement changed" message first, then the nudge as a
      // threaded reply — that ordering is what sells it.
      const trigger = findMessage(drift.trigger_id);
      const posted = await client.chat.postMessage({
        channel: DIGEST_CHANNEL || channel,
        text: trigger?.text ?? 'scope change',
        blocks: [
          section(`*${who(trigger?.user ?? 'dev')}*\n${bodyText(trigger?.text ?? '')}`),
        ] as any,
      });
      await new Promise((r) => setTimeout(r, 1200));
      await client.chat.postMessage({
        channel: DIGEST_CHANNEL || channel,
        thread_ts: posted.ts,
        text: 'สโคปเปลี่ยนแต่ ticket ยังไม่อัปเดต',
        blocks: driftNudgeBlocks(drift, item) as any,
      });
      await respond({ text: '▶ beat 9 — drift nudge อยู่ในเธรดแล้ว' });
      return;
    }
    case 10: {
      const q = 'ตอนนั้นเราสรุปเรื่อง export encoding ว่ายังไงนะ';
      await client.chat.postMessage({
        channel: DIGEST_CHANNEL || channel,
        text: 'recall',
        blocks: (await recallBlocks(q)) as any,
      });
      await respond({ text: `▶ beat 10 — recall: “${q}”` });
      return;
    }
    case 11: {
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

const localNow = () => zonedNow(SCHEDULE_TZ);

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

/* ------------------------------------------------------------------ *
 * The daily thread.
 *
 * Three moving parts, and the reason each is where it is:
 *
 *   postDaily        posts the invitation and *records the ts*. Without the
 *                    record, the 10:45 pass has no thread to read and tomorrow
 *                    has nothing to link back to.
 *   summariseDaily   re-reads that thread, stores what people typed, replies in
 *                    the thread, and — only when a line has been repeated
 *                    PENDING_DAYS times — posts once in the channel.
 *   both return a line instead of throwing, so a schedule logs what happened
 *                    and a slash command can show the same words to a person.
 * ------------------------------------------------------------------ */

/** A permalink is a nicety, so failing to get one must not lose the post. */
async function permalinkFor(client: any, channel: string, ts: string): Promise<string | undefined> {
  try {
    const res = await client.chat.getPermalink({ channel, message_ts: ts });
    return typeof res.permalink === 'string' ? res.permalink : undefined;
  } catch (err) {
    console.error('daily — ขอ permalink ไม่ได้ (ลิงก์ย้อนกลับของพรุ่งนี้จะเป็นข้อความเปล่า):', err);
    return undefined;
  }
}

async function postDaily(client: any, opts: { force?: boolean } = {}): Promise<string> {
  if (!DAILY_CHANNEL) {
    return 'ยังไม่ได้ตั้ง DAILY_CHANNEL (หรือ DIGEST_CHANNEL) — ไม่รู้จะโพสต์ที่ไหน';
  }
  const today = localNow().date;
  const existing = dailyFor(today);
  // Two daily posts in one morning split the thread, and the answers with it.
  if (existing && !opts.force) {
    return (
      `วันนี้โพสต์ daily ไปแล้ว${existing.permalink ? ` → ${existing.permalink}` : ''}` +
      ` · ถ้าจะโพสต์ใหม่ (เธรดเดิมจะไม่ถูกอ่านอีก): \`${CMD} daily post again\``
    );
  }

  const previous = dailyBefore(today);
  // Streaks are computed over every collected daily, not just yesterday's, so
  // "ค้างมา 3 วัน" on the morning post is a count of mornings and not an estimate.
  const carried = pendingStreaks(readDailies().filter((d) => d.date < today));

  const posted = await client.chat.postMessage({
    channel: DAILY_CHANNEL,
    text: dailyTitle(today),
    blocks: dailyPostBlocks({
      date: today,
      previous,
      carried,
      summaryAt: hhmm(DAILY_SUMMARY_AT),
    }) as any,
  });

  const ts = String(posted.ts ?? '');
  const permalink = await permalinkFor(client, DAILY_CHANNEL, ts);
  // answers start empty even when re-posting: they belong to the thread that was
  // read, and this is a different thread.
  saveDaily({ date: today, channel: DAILY_CHANNEL, ts, permalink, posted_at: stamp(), answers: [] });

  return (
    `โพสต์ ${dailyTitle(today)} แล้ว` +
    (carried.length ? ` · ยกยอดที่ค้างจากเมื่อวาน ${carried.length} เรื่อง` : '') +
    ` · จะสรุปในเธรดตอน ${hhmm(DAILY_SUMMARY_AT)}`
  );
}

async function summariseDaily(client: any): Promise<string> {
  const today = localNow().date;
  const record = dailyFor(today);
  if (!record) return `ยังไม่มีโพสต์ daily ของวันนี้ — สั่ง \`${CMD} daily post\` ก่อน`;

  const replies = await client.conversations.replies({
    channel: record.channel,
    ts: record.ts,
    limit: 200,
  });
  // [0] is the parent post itself.
  const messages: any[] = (replies.messages ?? []).slice(1);

  const answers: DailyAnswer[] = [];
  const unfilled: string[] = [];
  for (const message of messages) {
    const user = String(message.user ?? '');
    // The bot's own summary lands in this thread too. Reading it back as somebody's
    // standup is the same feedback loop the exporter has to skip TAM_SELF_USER for.
    if (!user || message.bot_id || message.subtype) continue;
    const parsed: DailyParse = parseDailyReply(String(message.text ?? ''), user, String(message.ts ?? ''));
    if (parsed.kind === 'answer') {
      // Last reply wins: people correct themselves further down the thread, and the
      // correction is the answer they meant.
      const at = answers.findIndex((a) => a.user === user);
      if (at >= 0) answers[at] = parsed.answer;
      else answers.push(parsed.answer);
    } else if (parsed.kind === 'unfilled' && !unfilled.includes(user)) {
      unfilled.push(user);
    }
  }

  // Simulated answers survive a real collection, unless that person really replied.
  // The demo seeds a morning and the people in the room type into the same thread;
  // dropping the seeded ones here would silently reset the streak the demo is about,
  // and overwriting a real reply with a seeded one would be worse still — so a real
  // answer always wins, and only the seats nobody filled keep their placeholder.
  const answered = new Set(answers.map((a) => a.user));
  const kept = record.answers.filter((a) => a.simulated && !answered.has(a.user));
  const merged = [...kept, ...answers];
  const updated = { ...record, answers: merged, summarised_at: stamp() };
  saveDaily(updated);

  const history = readDailies();
  const streaks = pendingStreaks(history);
  // Decided before the summary is posted, because the summary's own "ค้างเกิน N วัน"
  // section lists everything chronic while the channel message must only carry what
  // is new — the two say different things on purpose.
  const stuck = newEscalations(history, streaks, PENDING_DAYS);
  await client.chat.postMessage({
    channel: record.channel,
    thread_ts: record.ts,
    text: `สรุป ${dailyTitle(record.date)}`,
    blocks: dailySummaryBlocks({
      record: updated,
      expected: STANDUP_USERS,
      unfilled,
      streaks,
      pendingDays: PENDING_DAYS,
    }) as any,
  });

  // The one channel-wide ping in this feature, and it needs a measured reason:
  // the same line, from the same person, in PENDING_DAYS dailies running — and it
  // fires once per obstacle, not once per morning for as long as it is open.
  if (stuck.length) {
    await client.chat.postMessage({
      channel: record.channel,
      text: `ค้างมา ${PENDING_DAYS} วันแล้ว`,
      blocks: pendingEscalationBlocks(stuck, PENDING_DAYS) as any,
    });
    // Recorded only after Slack accepted it: a failed post that marked itself
    // announced would silence the one message the threshold exists to send.
    saveDaily({
      ...updated,
      announced: [...new Set([...(updated.announced ?? []), ...stuck.map(streakKey)])],
    });
  }

  return (
    `สรุปในเธรดแล้ว: ${answers.length} คนตอบ` +
    (kept.length ? ` (+${kept.length} คำตอบจำลองของเดโม)` : '') +
    (unfilled.length ? ` · ${unfilled.length} คนวางฟอร์มมาแต่ยังไม่กรอก` : '') +
    (stuck.length ? ` · ประกาศเรื่องที่ค้างเกิน ${PENDING_DAYS} วัน ${stuck.length} เรื่อง` : '')
  );
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
  scheduleDaily(`daily post ${hhmm(DAILY_AT)}`, DAILY_AT.hour, DAILY_AT.minute, async () => {
    await refreshForSchedule('daily post');
    console.log(`⏰ daily post — ${await postDaily(app.client)}`);
  });
  scheduleDaily(`daily summary ${hhmm(DAILY_SUMMARY_AT)}`, DAILY_SUMMARY_AT.hour, DAILY_SUMMARY_AT.minute, async () => {
    console.log(`⏰ daily summary — ${await summariseDaily(app.client)}`);
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
  // After the digest, not before: the digest is the day's news, and an item nobody
  // has mentioned in a week is not news — it is the thing you notice once the news
  // is out of the way. Deduplicated across restarts by the announcements store, so
  // this runs every morning and speaks only when something has newly gone quiet.
  scheduleDaily('stale 09:30', 9, 30, async () => {
    if (!DIGEST_CHANNEL) return;
    console.log(`⏰ stale 09:30 — ${await announceStale(app.client, { channel: DIGEST_CHANNEL })}`);
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
console.log(`   ${describeNames()}`);
console.log(`   ${describePolicy(postPolicy)}`);
// The allowlist blocks DMs as well as channel posts, which is right — and means the
// person driving a demo gets no reply to their own button presses unless they are on
// it. That is invisible from the line above, so it is said outright.
if (!STANDUP_USERS.length && !env('SLACK_POST_ALLOWLIST')) {
  console.log(
    '   ⚠ ยังไม่มี user id ใน allowlist — ปุ่ม/เมนูจะทำงานจริงแต่ตอบกลับเข้า DM ไม่ได้\n' +
      '     ใส่ id ของคนที่จะเดโมลง STANDUP_USERS หรือ SLACK_POST_ALLOWLIST',
  );
}
console.log(`   เขียนเอง: ${storeSummary()}`);
console.log(`   ${describeProjects()}`);
console.log(`   ${describeTracker()}`);
console.log(`   งานเงียบเกิน ${STALE_WORKDAYS} วันทำการแล้วประกาศ (\`${CMD} stale\`)`);
console.log(
  `   daily: ${DAILY_CHANNEL || 'ยังไม่ได้ตั้งช่อง (DAILY_CHANNEL/DIGEST_CHANNEL)'}` +
    ` · โพสต์ ${hhmm(DAILY_AT)} · สรุปในเธรด ${hhmm(DAILY_SUMMARY_AT)} · ค้างเกิน ${PENDING_DAYS} วันแล้วประกาศ` +
    // The one thing an operator actually needs told here: nothing above fires on its
    // own until this is set, and that is invisible from the times alone.
    (env('ENABLE_SCHEDULE') === '1' ? '' : ' — ยังไม่ยิงเอง (ENABLE_SCHEDULE ว่าง) สั่งมือ: ' + CMD + ' daily post'),
);
console.log(
  `   standup draft ${l.standups.length} คน · drift ${l.drifts.length}` +
    // This counter is the ledger's drift, which is a different thing from the ticket
    // comparison and stays empty until somebody records one. Saying "ticket system not
    // connected" here was true when the ledger was the only source and became a lie the
    // day /api/tracker landed — the board would report no ticket source while
    // `/meowtam drift` was reading 195 of them.
    (l.drifts.length === 0 && !demoFixtures() ? ' (จาก ledger — เทียบ ticket จริงที่ /meowtam silent|drift)' : '') +
    (demoFixtures() ? ' · DEMO_FIXTURES=1 เปิดอยู่ — drift เป็นตัวอย่าง' : ''),
);
console.log(`   ลอง: /meowtam demo   (beats: ${BEATS.map((b) => b.title).join(' → ')})`);
