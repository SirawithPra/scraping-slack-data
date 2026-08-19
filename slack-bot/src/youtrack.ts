/**
 * The tracker, from the bot's side: search every ticket, and write one comment.
 *
 * Why the ticket picker needed this at all
 * ---------------------------------------
 * The "ผูกกับ ticket" menu used to list work items — the things the pipeline built
 * out of Slack. That set is exactly wrong for linking: a ticket already named in
 * Slack is already linked, and the one somebody is reaching for is the one nobody
 * has typed yet. It was also capped at 100 options with no search box, so past the
 * hundredth work item the menu silently could not reach a ticket at all. This asks
 * YouTrack instead, per keystroke, which is the only source that has all of them.
 *
 * Why writing is a separate switch
 * --------------------------------
 * `addComment` is the one thing here that other people can see and that cannot be
 * taken back quietly. It stays off until `YOUTRACK_WRITE=1`, and it will use
 * `YOUTRACK_WRITE_TOKEN` when given, so the read path can keep a read-only token.
 * A deployment that has not opted in gets a refusal *with the reason*, which the
 * caller shows — the previous behaviour here was a `console.log` under a message
 * claiming the ticket had been updated, and a claim with nothing behind it is the
 * one bug this codebase keeps deleting.
 *
 * Two backends, one interface
 * ---------------------------
 * Direct to YouTrack when the bot has the URL and token, which keeps ticket linking
 * working on a laptop with no pipeline running — the case that matters on demo day.
 * Otherwise through the pipeline's own routes, so a deployment that keeps its
 * YouTrack token in one place does not have to copy it into a second `.env`.
 */

const KEY_QUERY = /^\s*#?([A-Za-z][A-Za-z0-9_]{0,9}-\d+)\s*$/;
const NUMBER_QUERY = /^\s*#?(\d{1,7})\s*$/;

/** Fields YouTrack returns only when asked; without this the response is `$type` and `id`. */
const FIELDS = 'idReadable,summary,resolved,updated,customFields(name,value(name,$type))';

export interface Ticket {
  key: string;
  summary: string;
  state: string;
  resolved: boolean;
  url: string;
  /** Epoch seconds of the last change, for "แตะล่าสุดเมื่อ…" in the picker. */
  updated: number;
}

export type Backend = 'direct' | 'pipeline' | 'none';

export interface TrackerConfig {
  backend: Backend;
  /** YouTrack base url, or the pipeline base url — whichever backend is in play. */
  baseUrl: string;
  token: string;
  /** Projects the picker searches when a channel does not name one. */
  projects: string[];
  canWrite: boolean;
  timeoutMs: number;
}

const env = (k: string) => process.env[k]?.trim() ?? '';

/** `1`, `true`, `yes`, `on` — anything else, including unset, is off. */
function truthy(value: string): boolean {
  return ['1', 'true', 'yes', 'on'].includes(value.toLowerCase());
}

export function trackerConfig(): TrackerConfig {
  const youtrack = env('YOUTRACK_URL').replace(/\/+$/, '');
  const token = env('YOUTRACK_TOKEN');
  const projects = env('YOUTRACK_PROJECTS').split(',').map((p) => p.trim()).filter(Boolean);
  const timeoutMs = Number(env('TAM_API_TIMEOUT_MS')) || 20_000;
  const write = truthy(env('YOUTRACK_WRITE'));

  if (youtrack && token) {
    return { backend: 'direct', baseUrl: youtrack, token: env('YOUTRACK_WRITE_TOKEN') || token, projects, canWrite: write, timeoutMs };
  }
  const pipeline = env('TAM_API_URL').replace(/\/+$/, '');
  if (pipeline) {
    // The pipeline decides for itself whether it may write; the bot cannot know
    // from here, so it does not claim to. `canWrite` here means "there is a route
    // to try", and the refusal comes back from that route with its own reason.
    return { backend: 'pipeline', baseUrl: pipeline, token: env('TAM_ADMIN_TOKEN'), projects, canWrite: Boolean(env('TAM_ADMIN_TOKEN')), timeoutMs };
  }
  return { backend: 'none', baseUrl: '', token: '', projects, canWrite: false, timeoutMs };
}

/**
 * The YouTrack query for what somebody typed into the picker.
 *
 * Three shapes, because people type three things and only one is prose. `REV-1421`
 * and a bare `1421` are identities — the person already knows the ticket, and a
 * text search for those digits buries it under everything whose description
 * contains them. A bare number only resolves when exactly one project is in scope,
 * which is what the channel→project map is for. Everything else goes to YouTrack as
 * free text, scoped to the projects in play.
 *
 * An empty query is not an error: the picker opens before anyone types, and the
 * useful answer then is the project's most recently touched tickets.
 */
export function searchQuery(text: string, projects: string[] = []): string {
  const typed = (text ?? '').trim();
  const key = KEY_QUERY.exec(typed);
  if (key) return `issue id: ${(key[1] ?? '').toUpperCase()}`;
  const number = NUMBER_QUERY.exec(typed);
  if (number && projects.length === 1) return `issue id: ${(projects[0] ?? '').toUpperCase()}-${number[1]}`;
  const scope = projects.length ? `project: ${projects.join(', ')}` : '';
  return [scope, typed, 'sort by: updated desc'].filter(Boolean).join(' ');
}

async function json(url: string, init: RequestInit, timeoutMs: number): Promise<any> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...init, signal: ctl.signal });
    const body = await res.text();
    let parsed: any = {};
    try {
      parsed = body ? JSON.parse(body) : {};
    } catch {
      parsed = { raw: body };
    }
    if (!res.ok) {
      const detail = parsed?.detail ?? parsed?.error ?? parsed?.error_description ?? body.slice(0, 200);
      const err = new Error(`HTTP ${res.status} — ${detail}`);
      (err as any).status = res.status;
      throw err;
    }
    return parsed;
  } finally {
    clearTimeout(timer);
  }
}

/** YouTrack names its state field whatever it likes; it is found by type, never by name. */
function stateOf(row: any): string {
  for (const field of row?.customFields ?? []) {
    const value = field?.value;
    if (value && typeof value === 'object' && String(value.$type ?? '').endsWith('StateBundleElement')) {
      return String(value.name ?? '');
    }
  }
  return '';
}

function toTicket(row: any, base: string): Ticket {
  const key = String(row?.idReadable ?? '');
  return {
    key,
    summary: String(row?.summary ?? ''),
    state: stateOf(row),
    resolved: Boolean(row?.resolved),
    url: key ? `${base}/issue/${key}` : '',
    updated: Number(row?.updated ?? 0) / 1000,
  };
}

export class TrackerOff extends Error {}

/**
 * Tickets matching what somebody typed, most recently touched first.
 *
 * `projects` narrows the search to the channel's own project. Passing none is not
 * "search nothing" — it falls back to `YOUTRACK_PROJECTS`, and with neither set
 * YouTrack searches everything the token can see, which is slow but correct.
 *
 * A query that matches nothing returns `[]`. YouTrack answers `issue id:` for a
 * key that does not exist with HTTP 400, and somebody halfway through typing a
 * number must see "ยังไม่เจอ", not an error.
 */
export async function searchTickets(
  text: string,
  opts: { projects?: string[]; limit?: number; cfg?: TrackerConfig } = {},
): Promise<Ticket[]> {
  const cfg = opts.cfg ?? trackerConfig();
  const limit = opts.limit ?? 50;
  const projects = opts.projects?.length ? opts.projects : cfg.projects;

  if (cfg.backend === 'none') {
    throw new TrackerOff(
      'ยังไม่ได้ต่อ YouTrack — ตั้ง YOUTRACK_URL + YOUTRACK_TOKEN ให้บอท หรือ TAM_API_URL ให้ยิงผ่าน pipeline',
    );
  }
  if (cfg.backend === 'pipeline') {
    const url =
      `${cfg.baseUrl}/api/tickets/search?q=${encodeURIComponent(text ?? '')}` +
      `&project=${encodeURIComponent(projects.join(','))}&limit=${limit}`;
    const body = await json(url, {}, cfg.timeoutMs);
    if (body?.error) throw new TrackerOff(String(body.error));
    return (body?.issues ?? []) as Ticket[];
  }

  const query = searchQuery(text, projects);
  const url = `${cfg.baseUrl}/api/issues?query=${encodeURIComponent(query)}&fields=${FIELDS}&$top=${limit}`;
  try {
    const rows = await json(url, { headers: { Authorization: `Bearer ${cfg.token}`, Accept: 'application/json' } }, cfg.timeoutMs);
    return (Array.isArray(rows) ? rows : []).map((row) => toTicket(row, cfg.baseUrl));
  } catch (err) {
    if ((err as any)?.status === 400) return []; // a key that does not exist: "no match", not a fault
    throw err;
  }
}

export interface WrittenComment {
  key: string;
  id: string;
  url: string;
}

/**
 * Write one comment on one ticket, and return the id YouTrack gave it.
 *
 * The id is the point of the return value. A write whose only evidence is that
 * nothing threw is indistinguishable from the mock this replaces, so the caller
 * gets something a reader can go and find on the ticket itself.
 */
export async function addComment(key: string, text: string, cfg = trackerConfig()): Promise<WrittenComment> {
  const issue = (key ?? '').trim().toUpperCase();
  const body = (text ?? '').trim();
  if (!issue) throw new TrackerOff('ไม่ได้บอกว่า ticket ไหน');
  if (!body) throw new TrackerOff('คอมเมนต์ว่างเปล่า — ไม่เขียนลง ticket');
  if (cfg.backend === 'none') {
    throw new TrackerOff('ยังไม่ได้ต่อ YouTrack — คอมเมนต์ยังไม่ถูกเขียน');
  }
  if (cfg.backend === 'direct' && !cfg.canWrite) {
    throw new TrackerOff(
      'YOUTRACK_WRITE ยังไม่ได้เปิด — บอทจะไม่เขียนคอมเมนต์ลง ticket จริง ' +
        '(ตั้ง YOUTRACK_WRITE=1 และใส่ YOUTRACK_WRITE_TOKEN ที่มีสิทธิ์คอมเมนต์)',
    );
  }
  if (cfg.backend === 'pipeline') {
    if (!cfg.token) throw new TrackerOff('ต้องมี TAM_ADMIN_TOKEN ถึงจะให้ pipeline เขียนคอมเมนต์ให้ได้');
    const res = await json(
      `${cfg.baseUrl}/api/ticket/${encodeURIComponent(issue)}/comment`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-TAM-Token': cfg.token },
        body: JSON.stringify({ text: body }),
      },
      cfg.timeoutMs,
    ).catch((err: Error) => {
      // 503 here is "this deployment does not write", which is a configuration
      // answer and belongs in front of the person as itself.
      throw new TrackerOff(err.message.replace(/^HTTP \d+ — /, ''));
    });
    return { key: issue, id: String(res?.id ?? ''), url: String(res?.url ?? '') };
  }

  const res = await json(
    `${cfg.baseUrl}/api/issues/${encodeURIComponent(issue)}/comments?fields=id,text,created`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${cfg.token}`,
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ text: body }),
    },
    cfg.timeoutMs,
  );
  return { key: issue, id: String(res?.id ?? ''), url: `${cfg.baseUrl}/issue/${issue}` };
}

/** The boot line: which backend, which projects, and whether writing is on. */
export function describeTracker(cfg = trackerConfig()): string {
  if (cfg.backend === 'none') return 'ticket: ยังไม่ได้ต่อ YouTrack — เมนูผูก ticket จะใช้ work item เท่าที่มี';
  const where = cfg.backend === 'direct' ? cfg.baseUrl : `ผ่าน pipeline ${cfg.baseUrl}`;
  const scope = cfg.projects.length ? cfg.projects.join(', ') : 'ทุกโปรเจกต์ที่ token เห็น';
  const write =
    cfg.backend === 'direct'
      ? cfg.canWrite
        ? 'เขียนคอมเมนต์ได้ (YOUTRACK_WRITE=1)'
        : 'อ่านอย่างเดียว (YOUTRACK_WRITE ยังไม่เปิด)'
      : cfg.canWrite
        ? 'เขียนผ่าน pipeline ได้ถ้า pipeline เปิดไว้'
        : 'อ่านอย่างเดียว (ไม่มี TAM_ADMIN_TOKEN)';
  return `ticket: ${where} · ${scope} · ${write}`;
}
