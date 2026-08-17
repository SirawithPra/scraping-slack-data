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

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Decision, Source } from './types.js';

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

function readJson<T>(path: string, fallback: T): T {
  if (!existsSync(path)) return fallback;
  try {
    return JSON.parse(readFileSync(path, 'utf8')) as T;
  } catch (err) {
    // A corrupt store must not take the bot down, but it must be visible.
    console.error(`⚠  อ่าน ${path} ไม่ได้ (${(err as Error).message}) — ถือว่าว่าง`);
    return fallback;
  }
}

function writeJson(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
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

export function readOverrides(): LinkOverride[] {
  const raw = readJson<unknown>(overridesPath(), []);
  if (Array.isArray(raw)) return raw as LinkOverride[];
  // Also tolerate the plain {record_id: key} map the linker's CLI writes.
  return Object.entries(raw as Record<string, string>).map(([record_id, key]) => ({
    record_id,
    key,
    by: 'unknown',
    at: '',
  }));
}

/**
 * Record a human's correction. Last write wins for a given record, so changing
 * your mind replaces rather than appends a contradiction the linker would have
 * to arbitrate.
 */
export function saveOverride(recordId: string, key: string, by: string, at: string): number {
  const kept = readOverrides().filter((o) => o.record_id !== recordId);
  kept.push({ record_id: recordId, key, by, at });
  writeJson(overridesPath(), kept);
  return kept.length;
}

// ---------------------------------------------------------------------------
// decisions
// ---------------------------------------------------------------------------

export function readDecisions(): Decision[] {
  return readJson<Decision[]>(decisionsPath(), []);
}

export interface NewDecision {
  statement: string;
  when: string;
  user: string;
  source: Source;
  evidence_id: string;
  /** Set when this decision replaces an earlier one, driving the recall chain. */
  supersedes?: string;
  related_items?: string[];
}

/**
 * Append a decision and, when it replaces an earlier one, link the pair in both
 * directions so `decisionChain` can walk it. Supersession is the whole point of
 * the decision log — "we said X in May, Y in August" is only answerable if the
 * link is stored, not inferred later.
 */
export function saveDecision(input: NewDecision): Decision {
  const all = readDecisions();
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
    const prior = kept.find((d) => d.id === input.supersedes);
    if (prior) prior.superseded_by = id;
  }
  kept.push(decision);
  writeJson(decisionsPath(), kept);
  return decision;
}

/** Line the operator sees at boot, so the store is never a mystery. */
export function storeSummary(): string {
  return `${readDecisions().length} decision, ${readOverrides().length} link override`;
}
