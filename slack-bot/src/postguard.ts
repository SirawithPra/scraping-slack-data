/**
 * A hard allowlist on where this bot may leave a visible message.
 *
 * The bot runs against a real company workspace. `chat.postMessage` writes something
 * permanent that everyone in the channel sees, and there are nineteen call sites; a
 * twentieth added next month would inherit no protection at all if each site checked
 * for itself. So the check lives in one place that cannot be bypassed: the WebClient
 * method every one of them ends up calling.
 *
 * What is guarded and what is not, deliberately:
 *
 *   chat.postMessage    GUARDED. Permanent, visible to the whole channel.
 *   chat.postEphemeral  not guarded — visible only to the person who just invoked the
 *                       command, leaves nothing behind, and blocking it would make
 *                       `/meowtam` silently do nothing in a channel someone typed it
 *                       in, which reads as a broken bot rather than a safe one.
 *   views.open / push   not guarded — a modal is shown to one person and posts nothing.
 *
 * The allowlist defaults to what is already configured (DIGEST_CHANNEL plus
 * STANDUP_USERS), so the safe thing happens with no extra setup. An empty allowlist
 * means *nothing may be posted*, which is the right default for a fresh clone: a
 * misconfigured bot should be silent, not loud in the wrong room.
 */

import type { WebClient } from '@slack/web-api';

export interface PostPolicy {
  allowed: Set<string>;
  /** Where each entry came from, so the refusal message can explain itself. */
  source: string;
}

/** Ids that may receive a posted message: channels `C…`/`G…`, users `U…`, DMs `D…`. */
export function readPolicy(env: NodeJS.ProcessEnv = process.env): PostPolicy {
  const explicit = (env.SLACK_POST_ALLOWLIST ?? '').trim();
  if (explicit) {
    return { allowed: new Set(ids(explicit)), source: 'SLACK_POST_ALLOWLIST' };
  }
  const digest = ids(env.DIGEST_CHANNEL ?? '');
  const standup = ids(env.STANDUP_USERS ?? '');
  return {
    allowed: new Set([...digest, ...standup]),
    source: 'DIGEST_CHANNEL + STANDUP_USERS (ไม่ได้ตั้ง SLACK_POST_ALLOWLIST)',
  };
}

/**
 * Pull ids out of a setting, tolerating the comments people leave beside them.
 *
 * `.env` files here carry trailing notes — `C0ABC123 #team-name` — and dotenv keeps
 * inline comments only when a space precedes the `#`. Parsing for the id shape rather
 * than splitting on whitespace means a note in either position cannot turn into a
 * channel id that silently fails to match, or worse, matches nothing and disables the
 * allowlist.
 */
function ids(value: string): string[] {
  return value.match(/\b[CGDUW][A-Z0-9]{6,}\b/g) ?? [];
}

export class PostRefused extends Error {}

/**
 * Wrap a client so every `chat.postMessage` is checked. Returns the same object: Bolt
 * hands the identical WebClient to every listener, so patching it once covers the
 * scheduled jobs, the command handlers, the shortcuts, and anything added later.
 */
export function guardPosting(client: WebClient, policy: PostPolicy): WebClient {
  const chat = client.chat as unknown as {
    postMessage: (args: { channel?: string; [k: string]: unknown }) => Promise<unknown>;
    __guarded?: boolean;
  };
  if (chat.__guarded) return client; // idempotent: two calls must not nest the check
  const original = chat.postMessage.bind(client.chat);

  chat.postMessage = async (args) => {
    const target = String(args?.channel ?? '');
    if (!policy.allowed.has(target)) {
      const list = [...policy.allowed].join(', ') || '(ว่าง — ห้ามโพสต์ที่ไหนเลย)';
      throw new PostRefused(
        `ปฏิเสธการโพสต์ไป ${target || '(ไม่ระบุ channel)'} — ไม่อยู่ใน allowlist [${list}] ` +
          `ที่มาจาก ${policy.source}. ถ้าตั้งใจจะโพสต์ที่นั่นจริง เพิ่ม id ลง SLACK_POST_ALLOWLIST`,
      );
    }
    return original(args);
  };
  chat.__guarded = true;
  return client;
}

/** One line for the boot banner, so the operator sees the blast radius before traffic. */
export function describePolicy(policy: PostPolicy): string {
  const list = [...policy.allowed];
  if (!list.length) return 'โพสต์ได้: ไม่มีที่ไหนเลย — ยังไม่ตั้ง DIGEST_CHANNEL/STANDUP_USERS';
  return `โพสต์ได้เฉพาะ: ${list.join(', ')}  (${policy.source})`;
}
