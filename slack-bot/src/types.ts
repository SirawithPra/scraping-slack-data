/**
 * The ledger shape. Everything the bot renders comes from here.
 *
 * Design rule that runs through the whole file: any *computed* claim
 * (state, age, counts) must be able to point at the exact message that
 * proves it. That is what `evidence_id` is for, and why every message
 * carries a `permalink`. Never render a claim you cannot link.
 */

export type State = 'blocked' | 'stalled' | 'moving' | 'done';
// `note` is a note somebody typed and pasted in, which is not the same as a transcript:
// on this team a meeting usually leaves hand-written notes rather than a recording, and
// calling those `meeting` would claim a transcript that never existed.
// `slack_paste` is a conversation copied out of a DM or a private group by hand. It is
// kept apart from `slack` for the same reason: an exported message is whole and has a
// permalink, a pasted one is whatever somebody happened to select and has neither.
export type Source = 'slack' | 'slack_paste' | 'meeting' | 'note' | 'youtrack' | 'notion';

export interface Message {
  id: string;
  source: Source;
  /**
   * The Slack user id, or a name for a source that only has one (a meeting
   * transcript's speaker). Stored as-is and resolved at render time by
   * `names.ts` — never pre-rendered here, because the same record has to be able
   * to print as a real name, a pseudonym, or the raw key depending on TAM_NAMES.
   */
  user: string;
  /** Absolute, 'YYYY-MM-DD HH:mm'. The reader is reconciling against their own memory. */
  when: string;
  text: string;
  /** Deep link back to the source. The whole credibility of the product. */
  permalink?: string;
  channel?: string;
}

export interface TimelineEvent {
  when: string;
  kind: 'status_change' | 'blocked' | 'unblocked' | 'scope_change' | 'decision' | 'commit';
  text: string;
  source: Source;
  user: string;
  evidence_id?: string;
}

export interface Decision {
  id: string;
  statement: string;
  when: string;
  user: string;
  source: Source;
  evidence_id: string;
  /** Id of the decision that replaced this one, if any. Drives the recall chain. */
  superseded_by?: string;
  related_items?: string[];
}

export interface WorkItem {
  /** YouTrack issue key where known, else a synthetic 'TAM-n'. */
  key: string;
  headline: string;
  state: State;
  /** Human-readable reason for the state. Rendered verbatim, never paraphrased. */
  evidence: string;
  /** Message that proves `evidence`. Must resolve in `messages`. */
  evidence_id: string;
  /** Days in the current state. */
  age_days: number;
  assignee?: string;
  participants: string[];
  sources: Partial<Record<Source, number>>;
  youtrack_status?: string;
  youtrack_url?: string;
  /** ISO date the YouTrack description was last edited — drives drift detection. */
  description_updated?: string;
  first: string;
  last: string;
  /** Model-written. Always labelled in the UI, never the only thing shown. */
  summary?: {
    detail: string;
    next_step?: string;
    citations: string[];
    /** True when none of the model's citations survived checking. Warn the reader. */
    unverified: boolean;
    /**
     * Which summariser wrote it — 'template' for the rule-based default, otherwise a
     * model id. Absent when the source did not say, which the card must show as
     * unknown rather than guess: 'a model wrote this' is itself a claim.
     */
    backend?: string;
  };
  timeline: TimelineEvent[];
  messages: Message[];
}

/** A detected mismatch between what Slack says and what the ticket says. */
export interface Drift {
  item_key: string;
  /** The message where the scope actually changed. */
  trigger_id: string;
  cue: string;
  detected: string;
  current_description: string;
  proposed_description: string;
}

export interface StandupDraft {
  slack_user_id: string;
  display_name: string;
  /** What the bot already knows you did — the dev corrects rather than recalls. */
  yesterday: Array<{ key: string; headline: string; note: string; evidence_id?: string }>;
  /** The pain-#1 fix: things still open from before, with how long they've sat. */
  carried_over: Array<{ key: string; headline: string; stale_days: number }>;
}

export interface Ledger {
  built_at: string;
  window_days: number;
  corpus_size: number;
  /** Threads the linker could not confidently attach. Shown, never hidden. */
  unassigned: Message[];
  items: WorkItem[];
  decisions: Decision[];
  drifts: Drift[];
  standups: StandupDraft[];
}

/* ------------------------------------------------------------------ *
 * The daily thread. Not part of the ledger: these are answers people
 * typed at the bot's invitation, stored as typed, so tomorrow's post can
 * quote them and a pending line can be counted across days.
 * ------------------------------------------------------------------ */

/** One "Blockers / Pending" line, with whoever it waits on. */
export interface DailyBlocker {
  /** The line as typed, minus the bullet. Never rewritten — it is evidence. */
  text: string;
  /**
   * Who it waits on: a Slack user id, the literal `PO`, or '' when the writer
   * named nobody. Empty is kept rather than guessed — tagging the wrong person
   * every morning is worse than tagging none.
   */
  tag: string;
}

/** One person's reply in a daily thread, as the parser read it. */
export interface DailyAnswer {
  user: string;
  /** `ts` of their reply, so the summary can link back to the words themselves. */
  ts: string;
  done: string[];
  focus: string[];
  blockers: DailyBlocker[];
  /**
   * True when the demo driver wrote this answer instead of a person.
   *
   * Carried on the answer rather than on the day, because a demo morning can hold
   * both: seeded answers to make the streak real, and whatever the people in the
   * room type into the same thread. Every renderer that shows an answer has to
   * label this one — a screenshot of a summary that cannot be told apart from a
   * real morning is the fake confirmation this codebase keeps deleting, with a
   * bigger audience.
   */
  simulated?: boolean;
}

/** One morning's post and everything its thread produced. */
export interface DailyRecord {
  /** 'YYYY-MM-DD' in the scheduling zone. One record per day, and the key. */
  date: string;
  channel: string;
  /** `ts` of the parent post — how the thread is re-read and linked to. */
  ts: string;
  /** Filled when Slack gave us one; the morning link degrades to plain text without it. */
  permalink?: string;
  posted_at: string;
  /** Set when the 10:45 pass ran, so a second run is visible rather than silent. */
  summarised_at?: string;
  answers: DailyAnswer[];
  /**
   * Pending lines already announced in the channel, as `user::normalised text`.
   *
   * Stored because the escalation must fire *once*. Announcing everything over the
   * threshold every morning would tag the same person for the same line for as long
   * as it stays open, which is how a bot earns a mute — and muting it loses the
   * announcements that are new. Kept per person, since two people can be waiting on
   * the same thing and each is their own claim.
   */
  announced?: string[];
  /** Set by the demo driver, so a seeded morning cannot be mistaken for a real one. */
  simulated?: boolean;
}

/**
 * Something the bot has already said in the channel, so it does not say it again.
 *
 * `kind` separates the escalations that have nothing to do with each other — a
 * pending line and a work item going quiet are both "we already told you", but a
 * shared namespace would let one silence the other.
 */
export interface Announcement {
  kind: string;
  key: string;
  at: string;
  note?: string;
}
