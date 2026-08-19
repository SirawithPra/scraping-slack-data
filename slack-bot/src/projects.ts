/**
 * Which channels are which project — the one structural fact nobody can infer.
 *
 * Everything else in this bot is derived: state comes from cues in the messages,
 * work items come from clustering, links come from ticket keys people typed. None
 * of that can recover the thing a team knows without thinking about it — that
 * `#reverapp-dev` and `#rvr-qa` are one project and `#mobile` is another, and that
 * their names not matching means nothing.
 *
 * Stating it buys three concrete things, which is why it is worth a variable:
 *
 *   the ticket picker   opens on that channel's project instead of the whole tracker
 *   the linker          prefers the channel's own project when a message names two
 *                       tickets (pipeline side — `tam.core.channel_projects`)
 *   every card          can say which project a channel belongs to, in words a
 *                       reader recognises, rather than repeating the channel name
 *
 * Same variable name and same syntax as the pipeline reads, for the same reason
 * `store.ts` and `server.py` resolve the overrides file identically: two halves
 * that must agree should not each be configured.
 *
 *   TAM_CHANNEL_PROJECTS=REVERAPP (Rever App)=C0ABC,C0DEF,#reverapp-qa; MOB=C0GHI
 *
 * Grouped by project rather than keyed by channel because that is the direction the
 * fact runs — "these channels are the same project" — and because the channel-keyed
 * form makes the many-to-one case a repetition you can get subtly wrong.
 */

/** `#name` entries, kept apart: only Slack can resolve one, so only this side reads them. */
export interface ProjectMap {
  byChannel: Map<string, string>;
  byName: Map<string, string>;
  labels: Map<string, string>;
  projects: string[];
}

const GROUP = /^\s*([A-Za-z][A-Za-z0-9_]{0,19})\s*(?:\(([^)]*)\))?\s*=\s*([\s\S]*)$/;

let cached: ProjectMap | undefined;

export function readProjects(raw = process.env.TAM_CHANNEL_PROJECTS ?? ''): ProjectMap {
  const byChannel = new Map<string, string>();
  const byName = new Map<string, string>();
  const labels = new Map<string, string>();
  const order: string[] = [];

  for (const group of raw.split(';')) {
    if (!group.trim()) continue;
    const m = GROUP.exec(group);
    if (!m) {
      // Logged, not thrown. This is a convenience that sharpens several things; a
      // typo in it must not stop the bot that works fine without it.
      console.error(`⚠  TAM_CHANNEL_PROJECTS: อ่านกลุ่มนี้ไม่ออก "${group.trim()}" (รูปแบบ: PROJ=C0ABC,C0DEF)`);
      continue;
    }
    const project = (m[1] ?? '').toUpperCase();
    if (m[2]?.trim()) labels.set(project, m[2].trim());
    if (!order.includes(project)) order.push(project);
    for (const entry of (m[3] ?? '').split(',')) {
      const value = entry.trim();
      if (!value) continue;
      if (value.startsWith('#')) byName.set(value.toLowerCase(), project);
      else byChannel.set(value, project);
    }
  }
  return { byChannel, byName, labels, projects: order };
}

/** Parsed once. Re-read with `resetProjects()` in tests. */
export function projectMap(): ProjectMap {
  if (!cached) cached = readProjects();
  return cached;
}

export function resetProjects(): void {
  cached = undefined;
}

/**
 * The project a channel belongs to, by id or by `#name`.
 *
 * Both, because the two halves of a Slack payload carry different things: a slash
 * command knows `channel_name`, a message shortcut usually knows only the id, and
 * a team writing this variable by hand will reach for whichever they can see in
 * the app. Returns '' when unmapped, which every caller treats as "no scope" —
 * never as "no project exists".
 */
export function projectOf(channel?: { id?: string; name?: string } | string): string {
  const map = projectMap();
  if (typeof channel === 'string') {
    return map.byChannel.get(channel) ?? map.byName.get(`#${channel.replace(/^#/, '').toLowerCase()}`) ?? '';
  }
  const byId = channel?.id ? map.byChannel.get(channel.id) : undefined;
  if (byId) return byId;
  const name = channel?.name?.replace(/^#/, '').toLowerCase();
  return (name ? map.byName.get(`#${name}`) : undefined) ?? '';
}

/** The project as a person calls it — the label if one was given, else the key. */
export function labelOf(project: string): string {
  if (!project) return '';
  const key = project.toUpperCase();
  return projectMap().labels.get(key) ?? key;
}

/** Every channel and `#name` mapped to this project. Used to say *why* a scope applied. */
export function channelsOf(project: string): string[] {
  const key = project.toUpperCase();
  const map = projectMap();
  return [
    ...[...map.byChannel.entries()].filter(([, p]) => p === key).map(([c]) => c),
    ...[...map.byName.entries()].filter(([, p]) => p === key).map(([c]) => c),
  ];
}

/** The line the operator reads at boot, so an unset map is visible rather than assumed. */
export function describeProjects(): string {
  const map = projectMap();
  if (!map.projects.length) {
    return 'channel→project: ยังไม่ได้ตั้ง (TAM_CHANNEL_PROJECTS) — ตัวเลือก ticket จะค้นทุกโปรเจกต์';
  }
  return (
    'channel→project: ' +
    map.projects
      .map((p) => `${labelOf(p)} [${p}] ${channelsOf(p).length} ช่อง`)
      .join(' · ')
  );
}
