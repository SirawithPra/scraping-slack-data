/**
 * Pull real channel history with the bot token you already have.
 *
 * Faster and lower-friction than the admin workspace export: no admin
 * approval, no zip, works on private channels the bot is in, and runs in
 * about ten seconds. This is the recommended path.
 *
 *   1. Invite the bot:  /invite @tam   in each channel
 *   2. Set EXPORT_CHANNELS=C0123,C0456 in .env
 *   3. npm run export
 *
 * Bot scopes needed:
 *   channels:history, groups:history   (read messages)
 *   channels:read,    groups:read      (channel names)
 *   users:read                         (map U0123 → display names)
 *
 * Output: data/raw-slack.json — feed it to `npm run ledger`.
 */
import 'dotenv/config';
import { WebClient } from '@slack/web-api';
import { writeFileSync, mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(here, '../data/raw-slack.json');

const token = process.env.SLACK_BOT_TOKEN?.trim();
const channels = (process.env.EXPORT_CHANNELS ?? '').split(',').map((s) => s.trim()).filter(Boolean);
const DAYS = Number(process.env.EXPORT_DAYS ?? 120);

if (!token) throw new Error('SLACK_BOT_TOKEN missing in .env');
if (!channels.length) throw new Error('EXPORT_CHANNELS missing in .env (comma-separated channel IDs)');

const web = new WebClient(token);
const oldest = String(Math.floor(Date.now() / 1000) - DAYS * 86400);

/** Resolve U0123 → a real display name once, so the ledger never shows raw ids it could have resolved. */
const userCache = new Map<string, string>();
async function userName(id?: string): Promise<string> {
  if (!id) return 'unknown';
  if (userCache.has(id)) return userCache.get(id)!;
  try {
    const r = await web.users.info({ user: id });
    const p: any = r.user?.profile ?? {};
    // Prefer what people actually call each other over the legal name.
    const name = p.display_name || p.real_name || (r.user as any)?.name || id;
    userCache.set(id, name);
    return name;
  } catch {
    // Genuinely unresolvable — keep the raw id. Never invent a name.
    userCache.set(id, id);
    return id;
  }
}

interface RawMsg {
  id: string;
  channel: string;
  channel_name: string;
  user: string;
  user_id: string;
  ts: string;
  when: string;
  text: string;
  thread_ts?: string;
  permalink?: string;
  reactions?: string[];
}

function fmt(ts: string): string {
  const d = new Date(Number(ts.split('.')[0]) * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function fetchChannel(channel: string): Promise<RawMsg[]> {
  let name = channel;
  try {
    const info = await web.conversations.info({ channel });
    name = (info.channel as any)?.name ?? channel;
  } catch {
    /* keep the id */
  }

  const out: RawMsg[] = [];
  let cursor: string | undefined;

  do {
    const res = await web.conversations.history({ channel, oldest, limit: 200, cursor });
    for (const m of (res.messages ?? []) as any[]) {
      if (m.subtype && m.subtype !== 'thread_broadcast') continue; // skip joins/leaves/bot noise
      if (!m.text) continue;

      const base: RawMsg = {
        id: `msg_${channel}_${m.ts}`,
        channel,
        channel_name: name,
        user: await userName(m.user),
        user_id: m.user ?? '',
        ts: m.ts,
        when: fmt(m.ts),
        text: m.text,
        reactions: (m.reactions ?? []).map((r: any) => r.name),
      };
      try {
        const link = await web.chat.getPermalink({ channel, message_ts: m.ts });
        base.permalink = link.permalink;
      } catch {
        /* permalink is nice-to-have */
      }
      out.push(base);

      // Threads carry most of the real signal — a linked ticket lives in the
      // parent and every reply inherits it.
      if (m.reply_count) {
        const t = await web.conversations.replies({ channel, ts: m.ts, limit: 200 });
        for (const r of (t.messages ?? []).slice(1) as any[]) {
          if (!r.text) continue;
          out.push({
            id: `msg_${channel}_${r.ts}`,
            channel,
            channel_name: name,
            user: await userName(r.user),
            user_id: r.user ?? '',
            ts: r.ts,
            when: fmt(r.ts),
            text: r.text,
            thread_ts: m.ts,
            reactions: (r.reactions ?? []).map((x: any) => x.name),
          });
        }
      }
    }
    cursor = (res.response_metadata as any)?.next_cursor || undefined;
    if (cursor) await new Promise((r) => setTimeout(r, 1200)); // Slack tier-3 rate limit
  } while (cursor);

  return out;
}

const all: RawMsg[] = [];
for (const c of channels) {
  process.stdout.write(`ดึง ${c} … `);
  const msgs = await fetchChannel(c);
  console.log(`${msgs.length} ข้อความ`);
  all.push(...msgs);
}

all.sort((a, b) => Number(a.ts) - Number(b.ts));
mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(OUT, JSON.stringify(all, null, 2));
console.log(`\n✓ ${all.length} ข้อความ → data/raw-slack.json`);
console.log(`  ต่อไป: npm run ledger`);
