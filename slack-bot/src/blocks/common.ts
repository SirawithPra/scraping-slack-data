import type { KnownBlock } from '@slack/types';
import type { State, Message, WorkItem, Source } from '../types.js';

/**
 * State is icon + word, never colour alone — colour-blind readers and Slack's
 * light/dark themes both make a bare coloured dot useless.
 */
export const STATE_LABEL: Record<State, string> = {
  blocked: '⛔ BLOCKED',
  stalled: '⏸ STALLED',
  moving: '▶️ MOVING',
  done: '✅ DONE',
};

export const SOURCE_ICON: Record<Source, string> = {
  slack: '💬',
  meeting: '🎙',
  youtrack: '🎫',
  notion: '📄',
};

/**
 * Icon for a source the pipeline named. The lookup is deliberately not a bare
 * index: the ingest side already emits `slack_thread`, which is not in `Source`,
 * and `SOURCE_ICON[that]` renders the literal string "undefined" next to a count
 * that is otherwise a computed fact. An unknown source gets a neutral bullet —
 * the count stays visible and nothing is claimed about where it came from.
 */
export function sourceIcon(s: string): string {
  return SOURCE_ICON[s as Source] ?? '•';
}

/**
 * The registered slash commands. app.ts builds its command regex from this list
 * and the Block Kit copy interpolates `CMD`, so renaming the command cannot
 * leave a footer pointing at a command Slack does not know.
 */
export const COMMANDS = ['meowtam', 'mt'] as const;
export const CMD = `/${COMMANDS[0]}`;

/** Slack hard-limits a section's text to 3000 chars. Truncate on a whitespace
 *  boundary only when one exists — Thai has no word spaces, so a naive
 *  word-boundary cut can lop off most of a Thai sentence.
 *
 *  The budget includes the ellipsis. Callers pass Slack's own limit — a modal
 *  title is exactly 24 — so returning max+1 chars would be a rejected view. */
export function clamp(s: string, max = 280): string {
  if (s.length <= max) return s;
  if (max <= 1) return s.slice(0, max);
  const cut = s.slice(0, max - 1);
  const sp = cut.lastIndexOf(' ');
  // Only respect the space if it is near the end; otherwise hard-cut.
  return (sp > max * 0.7 ? cut.slice(0, sp) : cut).trimEnd() + '…';
}

export function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** '3.1 วัน' — plural-free, which Thai wants anyway. */
export function days(n: number): string {
  return `${n < 10 ? n.toFixed(1) : Math.round(n)} วัน`;
}

/** Participants: real names as-is, raw Slack ids as code chips. Never fake an avatar or a name. */
export function people(list: string[]): string {
  return list.map((p) => (/^U[A-Z0-9]{6,}$/.test(p) ? `\`${p}\`` : p)).join(', ');
}

export function divider(): KnownBlock {
  return { type: 'divider' };
}

export function section(text: string, accessory?: any): KnownBlock {
  const b: any = { type: 'section', text: { type: 'mrkdwn', text: text.slice(0, 2999) } };
  if (accessory) b.accessory = accessory;
  return b as KnownBlock;
}

export function context(text: string): KnownBlock {
  return { type: 'context', elements: [{ type: 'mrkdwn', text: text.slice(0, 2999) }] };
}

export function header(text: string): KnownBlock {
  // plain_text header, max 150 chars. No emoji-only headers — screen readers skip them.
  return { type: 'header', text: { type: 'plain_text', text: text.slice(0, 150), emoji: true } };
}

/**
 * The evidence link. This is the single most important component in the app:
 * it is the one-click path from a computed claim to the message that proves it.
 * If there is no permalink we say so rather than rendering a dead button.
 */
export function evidenceButton(messageId: string, permalink?: string, label = 'ดูข้อความ') {
  return permalink
    ? { type: 'button', text: { type: 'plain_text', text: label, emoji: true }, url: permalink }
    : {
        type: 'button',
        text: { type: 'plain_text', text: label, emoji: true },
        action_id: 'show_evidence',
        value: messageId,
      };
}

export function quote(m: Message): string {
  return `>${sourceIcon(m.source)} *${esc(m.user)}* · ${m.when}\n>${esc(clamp(m.text, 240)).replace(/\n/g, '\n>')}`;
}

/** '💬 8 · 🎙 2 · 🎫 3' — counts are computed facts, so they get to look like facts. */
export function sourceCounts(item: WorkItem): string {
  return Object.entries(item.sources)
    .filter(([, n]) => (n ?? 0) > 0)
    .map(([s, n]) => `${sourceIcon(s)} ${n}`)
    .join(' · ');
}

export function ticketButton(item: WorkItem) {
  return item.youtrack_url
    ? { type: 'button', text: { type: 'plain_text', text: 'YouTrack' }, url: item.youtrack_url }
    : undefined;
}
