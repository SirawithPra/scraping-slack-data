# Design brief — paste this into Figma Make or Claude

Everything below is the prompt. It is written to be pasted whole. Real API
responses are in `docs/fixtures/*.json` — attach them if the tool accepts files,
otherwise the inline shapes below are enough.

---

## Build this

A **standup board for a bilingual (Thai/English) software team**. It reads the
team's Slack channel *and* their meeting transcripts, groups every message into
**work items**, and shows which ones are stuck.

The backend already exists and does all the analysis. Design the interface.

The one job of this screen: someone opens it at 9:25am, five minutes before
standup, and needs to know **what is stuck and why** before the meeting starts.
Everything else is secondary to that.

## What makes this different from a normal dashboard

1. **The unit is a work item, not a message and not a channel.** One work item is
   a cluster of messages — typically 5–15 — spanning Slack *and* a meeting. "The
   Android profile bug" is one card, whether it was discussed in a thread on
   Monday or out loud on Tuesday.

2. **Every work item has a state that was computed, not guessed**: `blocked`,
   `resolved`, or `active`. The state comes with `evidence` — a human-readable
   reason and the id of the exact message that proves it. The design must make
   that evidence reachable in one click. This is the product's whole credibility.

3. **Some text is generated, some is derived.** The `summary` object is written
   by a language model; everything else (`state`, `participants`, `sources`,
   `evidence`, timeline) is computed from the data. When `summary.unverified` is
   `true`, none of the model's citations survived checking and the reader must be
   warned. Design that warning. Do not decorate the whole app as "AI-powered" —
   the derived facts are the trustworthy part and should feel like facts.

4. **Provenance is the feature.** A summary that cites 3 message ids should let
   you get to those 3 messages. Design the path.

## Screens

### 1. Digest (home) — the 9:25am screen

A list of work items for a time window (default 3 days), **blocked first**, then
active, then resolved. Each card shows: headline, state, how long it has been in
that state, message count split by source, who is involved, the summary detail,
the next step, and the last 2–3 messages verbatim.

Design decision to make: how many work items fit before scrolling, and whether
resolved items collapse. A real channel produces 5–15 items per window.

### 2. Blockers — the filtered view

Only `state: "blocked"`, longest-stuck first. Fewer items, more room per item:
the evidence sentence and the blocking message should both be visible without a
click. Often empty — design the empty state as a *good* outcome, not an error.

### 3. Work item detail — the timeline

Two parts:
- **Timeline**: dated, typed events. Each row is `when · relation · from-message
  → to-message`. Relations are `resolves`, `blocked_by`, `duplicates`, `answers`,
  `follows_up`. This is a causal chain, not a feed — design it so the direction
  reads. Typically 1–6 rows.
- **All messages** in the item, chronological, with source and author.

### 4. Ground a note — search

A large text input (people paste a whole paragraph of meeting notes, not
keywords), and ranked results. Each result shows source, author, timestamp, the
message, and *why it matched* (`why` gives per-stage scores, `terms` gives the
words that matched literally). Surfacing "why" is optional but valued.

### 5. Add a meeting — upload

File input (.vtt/.srt/.txt/.json), a title, and a start datetime. After upload
the whole index rebuilds, which takes 5–60 seconds — design that wait honestly.

## Real data

```jsonc
// GET /api/digest
{
  "built_at": "2026-08-15 20:44",
  "window_days": 3.0,
  "summariser": "template",          // or "claude"
  "corpus_size": 42,
  "topics": [{
    "key": 1,
    "label": "omega, usd, agentforce add",   // machine-extracted anchors, lowercase, comma-joined
    "state": "blocked",                      // blocked | resolved | active
    "evidence": "blocked since 2026-08-14 16:31 — cue “ยังรอ”",
    "evidence_id": "mtg_20260814-0930-daily-standup_1786699865.000000",
    "participants": ["Nok", "Tim", "U0DEMOUSER2", "U0DEMOUSER1"],
    "sources": { "slack": 8, "meeting": 2 },
    "messages": 10,
    "first": "2026-08-13 21:22",
    "last": "2026-08-14 16:31",
    "age_days": 1.2,
    "summary": {
      "headline": "omega, usd, agentforce add — ติดอยู่ ยังไปต่อไม่ได้",
      "detail": "10 ข้อความ (2 meeting + 8 slack) · ผู้เกี่ยวข้อง: Nok, Tim … ล่าสุด [Tim] ผมคุยกับทาง Omega เมื่อวาน เขาสนใจ Agentforce Add-on ค่อนข้างมาก แต่ขอดู pricing ละเอียดก่อน",
      "next_step": "ต้องมีคนไล่ให้ก่อน ถึงจะไปต่อได้",
      "citations": ["msg_C0DEMOCHAN1_1786630933.931999", "mtg_20260814-0930-daily-standup_1786699865.000000"],
      "unverified": false,
      "backend": "template"
    }
  }]
}

// GET /api/item/{key} → { topic, summary, timeline, messages }
"timeline": [{
  "when": "2026-08-14 16:30",
  "relation": "resolves",
  "from_user": "U0DEMOUSER1", "from_text": "Hey team, we've got a user reporting that our Android app crashes…",
  "to_user": "Ake",           "to_text": "ผม debug แล้วครับ เจอ bug ใน Profile module … fix เสร็จแล้ว รอ patch ขึ้น release",
  "evidence": "cue “เสร็จแล้ว”",
  "also_answers": 4          // this event also answers 4 earlier messages; collapsed
}]

// GET /api/search?q=…&k=10
"hits": [{
  "rank": 1, "score": 0.032, "id": "mtg_…", "source": "meeting", "user": "Tim",
  "when": "2026-08-14 16:31",
  "text": "ผมคุยกับทาง Omega เมื่อวาน เขาสนใจ Agentforce Add-on ค่อนข้างมาก…",
  "why": { "dense": 0.80, "bm25": 0.23, "cross": 1.00 },
  "terms": ["omega", "pricing", "agentforce"]
}]
```

Note the awkward real values a mockup would never invent, and design for them:
- `label` is machine-extracted keywords, lowercase, sometimes `"(no shared anchor)"`.
- `participants` mixes readable names (`"Tim"`, `"Nok"`) with raw Slack ids
  (`"U0DEMOUSER1"`) in the same list.
- `score` is ~0.03, not a percentage. Never render it as a progress bar or a %.
- `evidence` contains typographic quotes around a Thai fragment.
- `detail` is one long unbroken string, often 200–400 characters.

## States that must be designed

| State | When |
| --- | --- |
| `blocked` / `resolved` / `active` | Every card. Must be distinguishable **without colour** — icon + word, not a coloured dot alone. |
| `unverified: true` | The model's citations failed verification. Warn, but still show the derived facts. |
| Empty digest | Quiet window, nothing moved. Not an error. |
| Empty blockers | Nothing stuck — this is *good news*, design it as such. |
| Index rebuilding | API returns 503 for up to a minute after an upload. |
| `label: "(no shared anchor)"` | The cluster has no distinctive shared string. Fall back to the summary headline. |
| Very long `detail` / `text` | Up to ~2000 characters. Must truncate gracefully with a way to expand. |
| One work item, 40+ messages | The detail page must stay navigable. |

## Hard constraints

**Thai typography — most of the content is Thai.**
- Font stack must include a Thai face: `"IBM Plex Sans Thai"`, `"Noto Sans Thai"`,
  `"Sarabun"`, or `"Sukhumvit Set"`. A Latin-only font silently falls back and
  looks broken.
- Thai has **no spaces between words**, so lines break at arbitrary points. Never
  rely on `text-overflow: ellipsis` at a word boundary; never set a fixed
  character count for truncation.
- Thai stacks vowels and tone marks above and below the baseline. Use
  `line-height: 1.6–1.75`, more than you would for Latin. Tight leading collides.
- **Never apply `letter-spacing` to Thai** — it detaches vowel marks from their
  consonants. Never uppercase Thai; there is no uppercase.
- Thai and English sit in the same sentence constantly ("รอ API sorting อยู่").
  The two must look deliberate together, at the same optical size.

**Accessibility and honesty**
- State is never colour alone (icon + label).
- Text on any coloured surface at ≥ 4.5:1.
- Timestamps are absolute (`2026-08-14 16:31`), not "2 days ago" alone — the
  reader is reconciling with their own memory of the week.
- Light and dark mode, both selected deliberately.

**Responsive**: usable at 390px (someone checks it on the way to standup) and at
1440px. The digest is the mobile-critical screen.

## Do not build

- A chat interface. Nobody talks to this.
- Sparklines, gauges, donut charts, or any metric the API does not return. There
  is no "team velocity" here.
- "✨ AI" chrome, glowing borders, typing animations. The derived facts are the
  product; the generated prose is the smallest part of it.
- Purple-gradient-on-dark SaaS aesthetic, or Inter/Roboto as the display face.
- A settings panel. There are no settings.
- Fake avatars for `U0DEMOUSER1`. Design for ids that are not names.

## Output

A React + Tailwind implementation, one component per screen, reading from the
JSON shapes above (mock the fetch with the fixture data). Ship the empty,
loading, and error states as real components, not TODOs. Dark mode via CSS custom
properties, not duplicated class lists.
