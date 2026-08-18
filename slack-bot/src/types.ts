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
export type Source = 'slack' | 'meeting' | 'note' | 'youtrack' | 'notion';

export interface Message {
  id: string;
  source: Source;
  /** Display name where we have one, raw Slack id where we don't. Never invent a name. */
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
