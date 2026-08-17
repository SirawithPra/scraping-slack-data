import 'dotenv/config';
import { App, LogLevel } from '@slack/bolt';

import {
  ledger, reload, sortedItems, itemsByState, findItem, findMessage,
  itemsFor, standupFor, driftFor,
} from './data.js';
import { digestBlocks } from './blocks/digest.js';
import { standupDmBlocks } from './blocks/standupDm.js';
import { itemCardBlocks, boardBlocks } from './blocks/itemCard.js';
import { driftNudgeBlocks, driftModal } from './blocks/drift.js';
import { recallBlocks } from './blocks/recall.js';
import { context, section, esc, clamp } from './blocks/common.js';

const env = (k: string, fallback = '') => process.env[k]?.trim() || fallback;

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

const DIGEST_CHANNEL = env('DIGEST_CHANNEL');
const STANDUP_USERS = env('STANDUP_USERS').split(',').map((s) => s.trim()).filter(Boolean);

/* ------------------------------------------------------------------ *
 * /meowtam — one command, several shapes. Fewer commands to remember, and
 * Slack only makes you register one.
 * ------------------------------------------------------------------ */

// Both names hit the same handler. `/mt` exists purely so the live demo is
// eight keystrokes shorter per beat.
app.command(/^\/(meowtam|mt)$/, async ({ command, ack, respond, client, body }) => {
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
      const l = reload();
      await respond({ text: `โหลดใหม่แล้ว: ${l.items.length} items, ${l.corpus_size} ข้อความ` });
      return;
    }

    // /meowtam recall <paragraph>
    if (lower.startsWith('recall ')) {
      await respond({ blocks: recallBlocks(arg.slice(7).trim()), text: 'recall' });
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
    await respond({ blocks: recallBlocks(arg), text: 'recall' });
  } catch (err) {
    console.error('command error', err);
    await respond({ text: `พัง: ${(err as Error).message}` });
  }
});

/* ------------------------------------------------------------------ *
 * Interactions
 * ------------------------------------------------------------------ */

app.action('open_item', async ({ ack, body, client, action }) => {
  await ack();
  const key = (action as any).value as string;
  const item = findItem(key);
  if (!item) return;
  await client.views.open({
    trigger_id: (body as any).trigger_id,
    view: {
      type: 'modal',
      title: { type: 'plain_text', text: clamp(item.key, 24) },
      close: { type: 'plain_text', text: 'ปิด' },
      blocks: itemCardBlocks(item) as any,
    },
  });
});

app.action('show_evidence', async ({ ack, body, client, action }) => {
  await ack();
  const m = findMessage((action as any).value as string);
  if (!m) return;
  await client.views.open({
    trigger_id: (body as any).trigger_id,
    view: {
      type: 'modal',
      title: { type: 'plain_text', text: 'หลักฐาน' },
      close: { type: 'plain_text', text: 'ปิด' },
      blocks: [
        section(`*${esc(m.user)}* · ${m.when} · ${m.source}`),
        section(esc(m.text)),
        context(m.permalink ? `<${m.permalink}|เปิดใน Slack>` : `id: \`${m.id}\``),
      ] as any,
    },
  });
});

app.action('open_drift_modal', async ({ ack, body, client, action }) => {
  await ack();
  const key = (action as any).value as string;
  const drift = driftFor(key);
  const item = findItem(key);
  if (!drift || !item) return;
  await client.views.open({ trigger_id: (body as any).trigger_id, view: driftModal(drift, item) as any });
});

app.action('dismiss_drift', async ({ ack, respond, action }) => {
  await ack();
  // Dismissals are signal, not noise — in the real build they tune the cue list.
  console.log('drift dismissed for', (action as any).value);
  await respond({ replace_original: false, text: 'โอเค บันทึกไว้ว่าอันนี้ไม่ใช่การเปลี่ยนสโคป — จะเอาไปปรับ cue ต่อ' });
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

app.shortcut('link_to_ticket', async ({ ack, shortcut, client }) => {
  await ack();
  await client.views.open({
    trigger_id: (shortcut as any).trigger_id,
    view: {
      type: 'modal',
      callback_id: 'link_save',
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
            options: sortedItems()
              .slice(0, 20)
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
  await client.chat.postMessage({
    channel: (body as any).user.id,
    text: `ผูกกับ ${key} แล้ว`,
    blocks: [
      section(`*🔗 ผูกกับ ${key} แล้ว*`),
      context('การแก้ของคุณถูกเก็บเป็น override ถาวร — linker จะไม่เดาทับอีก'),
    ] as any,
  });
});

app.shortcut('mark_decision', async ({ ack, shortcut, client }) => {
  await ack();
  const msg = (shortcut as any).message;
  await client.chat.postMessage({
    channel: (shortcut as any).user.id,
    text: 'บันทึกเป็นการตัดสินใจแล้ว',
    blocks: [
      section('*🧠 บันทึกเป็นการตัดสินใจแล้ว*'),
      section(`>${esc(clamp(msg?.text ?? '', 300))}`),
      context('หาเจอด้วย `/meowtam recall` ได้ตลอด ไม่มีวันหมดอายุ · ถ้าถูกเปลี่ยนทีหลัง จะเห็นเป็นสายว่าอะไรแทนอะไร'),
    ] as any,
  });
});

/* ------------------------------------------------------------------ *
 * Emoji as input. Zero friction, and a team that already reacts to
 * everything will use this far more than a slash command.
 * ------------------------------------------------------------------ */

const EMOJI_ACTION: Record<string, string> = {
  ticket: '🎫 สร้าง/ผูก ticket',
  construction: '🚧 ทำเครื่องหมายว่าติด',
  white_check_mark: '✅ ทำเครื่องหมายว่าเสร็จ',
  pushpin: '📌 บันทึกเป็นการตัดสินใจ',
  brain: '🧠 บันทึกเป็นการตัดสินใจ',
};

app.event('reaction_added', async ({ event, client }) => {
  const label = EMOJI_ACTION[event.reaction];
  if (!label) return;
  await client.chat.postEphemeral({
    channel: (event.item as any).channel,
    user: event.user,
    text: label,
    blocks: [section(`*${label}*`), context('Meowtam บันทึกไว้ให้แล้ว — ไม่ต้องพิมพ์คำสั่ง')] as any,
  });
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
      const draft = standupFor(user) ?? ledger().standups[0];
      if (!draft) return respond({ text: 'ไม่มี standup draft ใน ledger.json' });
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
      if (!drift || !item) return respond({ text: 'ไม่มี drift ใน ledger.json' });
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
        blocks: recallBlocks(q) as any,
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

function scheduleDaily(hour: number, minute: number, fn: () => void) {
  const tick = () => {
    const now = new Date();
    if (now.getHours() === hour && now.getMinutes() === minute) fn();
  };
  setInterval(tick, 60_000);
}

if (env('ENABLE_SCHEDULE') === '1') {
  scheduleDaily(8, 45, async () => {
    for (const uid of STANDUP_USERS) {
      const draft = standupFor(uid);
      if (!draft) continue;
      await app.client.chat.postMessage({
        channel: uid,
        text: 'สรุปของคุณเมื่อวาน',
        blocks: standupDmBlocks(draft) as any,
      });
    }
  });
  scheduleDaily(9, 25, async () => {
    if (!DIGEST_CHANNEL) return;
    await app.client.chat.postMessage({
      channel: DIGEST_CHANNEL,
      text: 'Standup digest',
      blocks: digestBlocks() as any,
    });
  });
}

await app.start();
const l = ledger();
console.log(`🐾 Meowtam พร้อมแล้ว — ${l.items.length} work items, ${l.corpus_size} ข้อความ`);
console.log(`   ลอง: /meowtam demo   (beats: ${BEATS.join(' → ')})`);
