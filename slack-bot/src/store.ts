/**
 * The bot's own writes, on disk.
 *
 * Two shortcuts used to tell the user their action was saved and then save
 * nothing: "ผูกกับ ticket" answered *"เก็บเป็น override ถาวร"* and
 * "บันทึกเป็นการตัดสินใจ" answered *"หาเจอด้วย /meowtam recall ได้ตลอด"*. Both
 * were only messages. A product whose whole claim is that every statement can be
 * checked cannot ship a confirmation that isn't true.
 *
 * Ticket links go to the file the pipeline's linker already reads. Its
 * `load_overrides` accepts "the list-of-events shape a UI would append to", so
 * this is the loop closing where it was designed to: a human corrects a link in
 * Slack, the next linker run treats it as the top tier and stops guessing.
 *
 * Decisions have no pipeline counterpart yet, so they live in the bot's own
 * append-only file. Empty until someone files one — which is the honest starting
 * state, not a bug.
 */

import { copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Announcement, DailyRecord, Decision, Source } from './types.js';

const here = dirname(fileURLToPath(import.meta.url));

/** Where the pipeline's linker looks for human corrections. */
export function overridesPath(): string {
  const configured = process.env.TAM_OVERRIDES_PATH?.trim();
  if (configured) return resolve(configured);
  return resolve(here, '../../pipeline/data/link_overrides.json');
}

export function decisionsPath(): string {
  const configured = process.env.TAM_DECISIONS_PATH?.trim();
  if (configured) return resolve(configured);
  return resolve(here, '../data/decisions.json');
}

/**
 * A store that cannot be parsed is not an empty store.
 *
 * Reading it as empty was safe on a display path and destructive on a write path:
 * the next `saveOverride` wrote back the "empty" list it had just read, deleting
 * every prior correction, and reported success — 'เขียนลง … แล้ว (1 รายการ)'. The
 * two paths need different answers, so the caller says which it is.
 */
export class CorruptStoreError extends Error {
  constructor(readonly path: string, cause: Error) {
    super(`${path} อ่านไม่ได้ (${cause.message}) — ไฟล์ถูกแก้มือหรือเขียนค้างไว้`);
    this.name = 'CorruptStoreError';
  }
}

function readJson<T>(path: string, fallback: T, strict = false): T {
  if (!existsSync(path)) return fallback; // absent really is empty
  try {
    return JSON.parse(readFileSync(path, 'utf8')) as T;
  } catch (err) {
    if (strict) throw new CorruptStoreError(path, err as Error);
    // A corrupt store must not take a *rendering* path down, but it must be visible.
    console.error(`⚠  อ่าน ${path} ไม่ได้ (${(err as Error).message}) — ถือว่าว่าง`);
    return fallback;
  }
}

/**
 * Write via temp file + rename, keeping the previous contents as `.bak`.
 *
 * `renameSync` inside one directory is atomic, so a crash or a full disk leaves
 * either the old file or the new one — never the truncated half that the
 * pipeline's `load_overrides` would then choke on mid-read.
 */
function writeJson(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
  if (existsSync(path)) copyFileSync(path, `${path}.bak`);
  renameSync(tmp, path);
}

// ---------------------------------------------------------------------------
// ticket links → the pipeline's linker
// ---------------------------------------------------------------------------

export interface LinkOverride {
  record_id: string;
  key: string;
  /** Who corrected it, so a wrong override can be traced to a person. */
  by: string;
  at: string;
}

export function readOverrides(strict = false): LinkOverride[] {
  const raw = readJson<unknown>(overridesPath(), [], strict);
  if (Array.isArray(raw)) return raw as LinkOverride[];
  // Also tolerate the plain {record_id: key} map the linker's CLI writes.
  return Object.entries(raw as Record<string, string>).map(([record_id, key]) => ({
    record_id,
    key,
    by: 'unknown',
    at: '',
  }));
}

/** A ticket identity as the linker defines it — `TICKET_PATTERN`, prefix + number. */
const TICKET_KEY = /^[A-Z][A-Z0-9]{1,9}-\d+$/;
/** The synthetic key tam-api.ts mints for a pipeline topic: the cluster's label. */
const TOPIC_KEY = /^TAM-(\d+)$/;

/**
 * Translate a work item key into the namespace the linker actually keys on.
 *
 * `linker.py` assigns `ticket:REV-1421` and `cluster:7`, and `Link.ticket` is
 * `key.split(":")[1] if key.startswith("ticket:")`. A bare `MOB-142` written here
 * matched neither, so the override tier attached the message to a work item of
 * one with no ticket — the human's correction was stored, honoured, and pointed
 * nowhere. A pipeline `TAM-3` is the digest's topic key, which *is* the community
 * label the cluster tier uses, so it maps to `cluster:3`.
 *
 * Anything else throws rather than being written: the linker would ignore it, and
 * telling someone their correction is now the top tier when it is not is the
 * fake-confirmation bug this file was written to end.
 */
export function linkerKey(key: string): string {
  const k = key.trim();
  if (!k) return ''; // the linker's documented "unlink this message"
  if (k.includes(':')) return k; // already namespaced (hand-edited or a re-save)
  const topic = TOPIC_KEY.exec(k.toUpperCase());
  if (topic) return `cluster:${topic[1]}`;
  if (TICKET_KEY.test(k.toUpperCase())) return `ticket:${k.toUpperCase()}`;
  throw new Error(
    `“${k}” ไม่ใช่ทั้ง ticket key และ topic key ของ pipeline — linker จะไม่รู้จัก key นี้`,
  );
}

/**
 * Record a human's correction. Last write wins for a given record, so changing
 * your mind replaces rather than appends a contradiction the linker would have
 * to arbitrate.
 *
 * Reads strictly: a store that cannot be parsed must abort the write, because the
 * alternative is overwriting corrections nobody can get back.
 */
export function saveOverride(recordId: string, key: string, by: string, at: string): number {
  const namespaced = linkerKey(key);
  const kept = readOverrides(true).filter((o) => o.record_id !== recordId);
  kept.push({ record_id: recordId, key: namespaced, by, at });
  writeJson(overridesPath(), kept);
  return kept.length;
}

// ---------------------------------------------------------------------------
// decisions
// ---------------------------------------------------------------------------

export function readDecisions(strict = false): Decision[] {
  return readJson<Decision[]>(decisionsPath(), [], strict);
}

export interface NewDecision {
  statement: string;
  when: string;
  user: string;
  source: Source;
  evidence_id: string;
  /** Set when this decision replaces an earlier one, driving the recall chain. */
  supersedes?: string;
  /**
   * The record being superseded, when the caller found it outside this file.
   *
   * The chain is stored on the *prior* decision (`superseded_by`), and the list a
   * caller picks from is the merged one — the generated ledger's decisions plus
   * this file's. A prior that exists only in the ledger has nowhere to keep the
   * link, so pass it here and it is carried into the store verbatim; without it
   * the supersession a human just stated cannot be persisted at all.
   */
  supersedes_record?: Decision;
  related_items?: string[];
}

/**
 * Append a decision and, when it replaces an earlier one, link the pair in both
 * directions so `decisionChain` can walk it. Supersession is the whole point of
 * the decision log — "we said X in May, Y in August" is only answerable if the
 * link is stored, not inferred later.
 *
 * Reads strictly, for the same reason `saveOverride` does: rewriting a file we
 * could not read is how a decision log loses its history.
 */
export function saveDecision(input: NewDecision): Decision {
  const all = readDecisions(true);
  const id = `dec_${input.evidence_id.replace(/[^\w.-]/g, '_')}`;

  const decision: Decision = {
    id,
    statement: input.statement,
    when: input.when,
    user: input.user,
    source: input.source,
    evidence_id: input.evidence_id,
    related_items: input.related_items,
  };

  const kept = all.filter((d) => d.id !== id);
  if (input.supersedes) {
    let prior = kept.find((d) => d.id === input.supersedes);
    if (!prior && input.supersedes_record?.id === input.supersedes) {
      prior = { ...input.supersedes_record };
      kept.push(prior);
    }
    if (prior) prior.superseded_by = id;
    else {
      // Saying "we said X in May, Y in August" and storing only August is the
      // half-write this file exists to prevent, so it is at least reported.
      console.error(
        `⚠  ไม่มี decision ${input.supersedes} ใน ${decisionsPath()} — ` +
          'บันทึก decision ใหม่แล้ว แต่ยังผูกเป็น chain ไม่ได้ (ส่ง supersedes_record มาด้วยถ้ามี)',
      );
    }
  }
  kept.push(decision);
  writeJson(decisionsPath(), kept);
  return decision;
}

/** Line the operator sees at boot, so the store is never a mystery. */
export function storeSummary(): string {
  const dailies = readDailies();
  const answers = dailies.reduce((n, d) => n + d.answers.length, 0);
  return (
    `${readDecisions().length} decision, ${readOverrides().length} link override, ` +
    `${dailies.length} daily (${answers} คำตอบ), ${readAnnouncements().length} ที่ประกาศไปแล้ว`
  );
}

// ---------------------------------------------------------------------------
// the daily thread
// ---------------------------------------------------------------------------

export function dailiesPath(): string {
  const configured = process.env.TAM_DAILIES_PATH?.trim();
  if (configured) return resolve(configured);
  return resolve(here, '../data/dailies.json');
}

export function readDailies(strict = false): DailyRecord[] {
  const rows = readJson<DailyRecord[]>(dailiesPath(), [], strict);
  // Oldest first, so "the day before" is the entry before this one and a streak
  // walks backwards from the end. Callers should not have to know the file's order.
  return [...rows].sort((a, b) => a.date.localeCompare(b.date));
}

/**
 * Write one day's record, replacing any earlier version of the same date.
 *
 * Keyed by date rather than appended because the record is edited twice in a
 * morning — once when the post goes out, again when the 10:45 pass adds the
 * answers — and two rows for one day would make "yesterday's blockers" depend on
 * which row was read.
 *
 * Reads strictly, for the same reason `saveOverride` does: rewriting a file we
 * could not parse is how the history disappears.
 */
export function saveDaily(record: DailyRecord): DailyRecord[] {
  const kept = readDailies(true).filter((d) => d.date !== record.date);
  kept.push(record);
  kept.sort((a, b) => a.date.localeCompare(b.date));
  writeJson(dailiesPath(), kept);
  return kept;
}

// ---------------------------------------------------------------------------
// what has already been said out loud
// ---------------------------------------------------------------------------

export function announcementsPath(): string {
  const configured = process.env.TAM_ANNOUNCEMENTS_PATH?.trim();
  if (configured) return resolve(configured);
  return resolve(here, '../data/announcements.json');
}

export function readAnnouncements(strict = false): Announcement[] {
  return readJson<Announcement[]>(announcementsPath(), [], strict);
}

/**
 * Has this exact thing been announced to the channel before?
 *
 * The daily thread keeps its own equivalent inside the day's record, because a
 * pending line belongs to the morning it was typed. A stale work item belongs to
 * no particular morning, so it needs a store that outlives one — otherwise the
 * only way not to repeat yesterday's escalation is to never restart the bot.
 */
export function wasAnnounced(kind: string, key: string): boolean {
  return readAnnouncements().some((a) => a.kind === kind && a.key === key);
}

/**
 * Record that something was said in the channel. Written *after* Slack accepted
 * the post, for the same reason the daily path does it in that order: marking it
 * announced before the post can fail is how the one message that mattered gets
 * silently swallowed.
 */
export function markAnnounced(kind: string, keys: string[], at: string, note = ''): number {
  if (!keys.length) return readAnnouncements().length;
  const all = readAnnouncements(true);
  const already = new Set(all.filter((a) => a.kind === kind).map((a) => a.key));
  for (const key of keys) {
    if (already.has(key)) continue;
    all.push({ kind, key, at, note });
    already.add(key);
  }
  writeJson(announcementsPath(), all);
  return all.length;
}

/**
 * Replace the whole file. The one operation `saveDaily` cannot express, because it
 * merges by date and therefore cannot *remove* a morning.
 *
 * Only the demo cleanup needs it, and that is the point of naming it so plainly: a
 * daily that disappears takes a streak's evidence with it, so this must never be
 * the convenient way to write one record.
 */
export function replaceDailies(records: DailyRecord[]): DailyRecord[] {
  const sorted = [...records].sort((a, b) => a.date.localeCompare(b.date));
  writeJson(dailiesPath(), sorted);
  return sorted;
}

/** Today's record, or undefined — the 10:45 pass has nothing to read without it. */
export function dailyFor(date: string): DailyRecord | undefined {
  return readDailies().find((d) => d.date === date);
}

/** The most recent record strictly before `date`. This is "yesterday's daily". */
export function dailyBefore(date: string): DailyRecord | undefined {
  const earlier = readDailies().filter((d) => d.date < date);
  return earlier[earlier.length - 1];
}
