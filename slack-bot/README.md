# Meowtam 🐾

**รู้ว่าอะไรติด ก่อนเข้า standup**

*เหมียว + ตาม* — the cat that follows your work around. Says something when it notices
something, otherwise stays quiet.

A Slack-native work ledger. It reads Slack and meeting notes, merges them into
one record per work item, and tells you what is stuck, why, and which exact message proves it.
YouTrack states are hand-set in the fixture and the write-back is mocked — see
[What is real and what is faked](#what-is-real-and-what-is-faked).

Built for a 24-hour hackathon. Out of the box it runs entirely offline against a fixture —
nothing in the demo path touches a network except Slack itself. Point it at the Python
pipeline with `TAM_API_URL` when you want the real clustering (see
[Reading from the pipeline](#reading-from-the-pipeline)).

---

## Setup — about 10 minutes

### 1. Create the Slack app (3 min)

Go to <https://api.slack.com/apps> → **Create New App** → **From an app manifest** → pick your
workspace → paste the contents of `slack-app-manifest.yaml`.

Then:

- **Basic Information → App-Level Tokens → Generate**, scope `connections:write`. Copy the `xapp-…`.
- **Install App → Install to Workspace**. Copy the Bot User OAuth Token, `xoxb-…`.
- **Basic Information → App Credentials**. Copy the Signing Secret.

### 2. Configure (2 min)

```bash
cp .env.example .env
```

Fill in `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`.

Then `DIGEST_CHANNEL` — the channel **ID**, not the name. Get it from the channel's
**View channel details → About**, at the bottom. Looks like `C0DEMOCHAN1`.

Invite the bot: `/invite @Meowtam` in that channel.

### 3. Run (1 min)

```bash
npm install
npm start
```

You should see:

```
🐾 Meowtam พร้อมแล้ว — 8 work items, 128 ข้อความ
```

Type `/meowtam` in Slack. If it answers, you're done.

### 4. Swap in your real channel data (4 min, optional but worth it)

Judges recognising their own messages is worth more than any feature.

```bash
# EXPORT_CHANNELS=C0DEMOCHAN1,C0ABC123 in .env, then:
npm run export     # pulls history with the bot token you already have
npm run ledger     # clusters it into work items with states and evidence
```

`npm run ledger` prints an unassigned rate at the end. Under 25% is fine for a demo.
Over that, lower `SIM_FLOOR` in `scripts/build-ledger.ts` and re-run.

`/meowtam reload` re-reads the ledger without restarting — safe to run mid-demo.

> The alternative is the admin workspace export (Settings & administration → Workspace
> settings → Import/Export Data). It needs admin rights and only covers public channels.
> The bot-token route above is faster and works on private channels the bot is in.

---

## The demo

**Drive it with `/meowtam demo`, not from memory.** Five beats, fired one at a time. A live demo
that depends on you typing the right thing in the right order in front of judges will desync.

**Rehearse and present with `DEMO_FIXTURES=1 npm start`.** Beat 3 needs it. Drift detection
compares Slack against a ticket system and none is connected, so with the flag off there is no
drift to show and beat 3 answers with an explanation instead of the nudge. With the flag on, the
fixture's drift loads and the rendered block carries a visible
`⚠ ตัวอย่างจาก fixture — ยังไม่ได้ต่อ ticket system จริง` label, which is the honest way to demo
it: say the label out loud, it is a better answer than pretending.

`/mt` is a registered alias for the same handler — use it on stage, it's shorter and Slack's
autocomplete on `/me` can hesitate.

```
/meowtam demo          → which beat is next
/meowtam demo 1        → fire beat 1
/meowtam demo next     → fire the next one
/meowtam demo reset
```

| Beat | Command | What lands | The line to say |
| --- | --- | --- | --- |
| 1 | `/meowtam demo 1` | 08:45 DM in **your DMs** | "It doesn't ask what I did. It tells me, and I correct it." |
| 2 | `/meowtam demo 2` | 09:25 digest in the channel | "Blocked first. Every claim, one click from the message that proves it." |
| 3 | `/meowtam demo 3` | Scope-change message, then a threaded nudge (needs `DEMO_FIXTURES=1`) | "The requirement changed here. The ticket didn't. It noticed." |
| 4 | `/meowtam demo 4` | Recall with the decision chain | "May we said no BOM. August we changed it. The ticket still says May." |
| 5 | `/meowtam demo 5` | The board | — |

Beat 3 is the one that wins the room. Click **ดูร่างที่เสนอ** and let them see the proposed
YouTrack diff, then say the important sentence: **it never writes on its own — a human always
presses save.** (Started without `DEMO_FIXTURES=1`? Beat 3 will tell you so rather than post
nothing — but fix it before you present, not on stage.)

### Rehearse this

- **Record a backup video by hour 18.** Live Slack demos fail. Venue wifi, rate limits,
  a workspace that logs you out. A recording that plays is worth more than a live demo that
  might not.
- Run `/meowtam demo reset` right before you present.
- Have the digest channel already open on screen, scrolled to the bottom.
- Beat 1 posts to *your* DMs — have that conversation open in a second window or the demo
  stalls while you navigate.

### Questions you will get

**"How is this different from Geekbot?"**
Geekbot collects answers. This one already knows the answers and asks you to correct them —
and it reads YouTrack, so it can tell you your ticket disagrees with your Slack thread.

**"Does the AI hallucinate?"**
The state, dates, participants and evidence are computed by rules you can read in
`scripts/build-ledger.ts` — no model involved. Only the one-line summary is written for you, the
card names which backend wrote it (`สรุปจากกฎ (template)` for the shipped default, which is a
rule-based sentence with no model in it at all; `สรุปโดยโมเดล (claude)` when one is configured),
and when a model's citations fail verification the UI says so (see MOB-142 in the demo — it
renders the unverified warning deliberately).

**"What about Thai?"**
The matching is character-trigram based specifically because Thai has no spaces between
words. Word tokenisation without a Thai tokeniser silently produces garbage. Try
`/meowtam recall` with a Thai paragraph.

**"Is this surveilling my team?"**
It reports on tickets, never on people. No per-person message counts, no activity
leaderboards. Nudges go by DM, never to the channel. That is a design rule, not a setting.

---

## What is real and what is faked

Say this plainly if asked — judges reward knowing the difference.

| Real | Faked / mocked |
| --- | --- |
| Slack app, commands, modals, threads, emoji actions | YouTrack write-back (logs to console; the exact call site is marked in `src/app.ts`) |
| Clustering, state detection, evidence linking | YouTrack read (states in the fixture are hand-set) |
| Recall, decision chains, supersession | Meeting transcripts (fixture only — Teams Graph needs a tenant toggle, see below) |
| Runs on your real exported channel history | The 08:45 / 09:25 schedules (off by default; `ENABLE_SCHEDULE=1` turns them on) |

**Teams transcripts** need `OnlineMeetingTranscript.Read.All`, an application access policy,
**and** — since 31 July 2026 — a tenant-level toggle
(`Set-CsTeamsMeetingConfiguration -EnableGraphTranscriptAccess $true`). Without it every call
returns 403. If someone asks why meetings aren't live: that's why, and it's an IT ticket, not
a code problem.

---

## Layout

```
src/
  types.ts          the ledger shape — read this first
  data.ts           loading, sorting, the decision-chain walk
  tam-api.ts        client for the Python pipeline's HTTP API, and the shape translation
  store.ts          the bot's own writes: ticket-link overrides and the decision log
  standups.ts       standup drafts derived from work items, not read from a fixture
  search.ts         recall: trigram + literal-term hybrid, no API key
  app.ts            commands, actions, modals, the demo driver
  blocks/
    common.ts       shared renderers — state labels, evidence buttons, Thai-safe truncation
    digest.ts       the 09:25 screen
    standupDm.ts    the 08:45 DM
    itemCard.ts     one work item, full detail
    drift.ts        the nudge and the diff modal
    recall.ts       search results and decision chains
scripts/
  export-slack.ts   real channel history via bot token
  build-ledger.ts   messages → work items, states, drifts, decisions
  preview.ts        render every payload offline, validate against Slack's limits
  check-api.ts      prove the bot can read the pipeline, with no Slack in the loop
tests/              `npm test` — Slack's limits, the shape translation, the store
data/
  ledger.fixture.json  the committed fixture — anonymised, safe for a public repo
  ledger.json          what the bot reads: seeded from the fixture, overwritten by
                       `npm run ledger`, and gitignored so real channel history
                       never lands in a commit
```

```bash
npm test                   # Slack's limits, the shape translation, and the store
npm run preview            # validate every Block Kit payload, no Slack needed
npm run preview -- digest  # dump one payload for app.slack.com/block-kit-builder
npm run typecheck
```

### Reading from the pipeline

By default the bot builds its own ledger from `data/ledger.json` and touches no network but
Slack. Set `TAM_API_URL` and the Python half becomes the owner of what a work item is —
work items, states, evidence, timelines and recall all come from `tam.web.server`, while
decisions, drifts and standup drafts stay local because the pipeline has no counterpart for
them yet. There is **no fallback**: if the pipeline is asked for and cannot answer, the bot
refuses to start rather than serve fixture data that looks identical to live.

```bash
TAM_API_URL=http://127.0.0.1:8899 npm run check-api   # fetch, translate, index, search
```

`.env.example` documents `TAM_API_URL`, `TAM_STALE_DAYS`, `TAM_MIN_COSINE`, `DEMO_FIXTURES` and
the rest line by line; `../pipeline/README.md` covers the server side.

## Design rules that are not negotiable

1. **No claim without a link.** Every computed statement carries the message that proves it.
2. **State is icon + word, never colour alone.** Colour-blind readers, and Slack themes.
3. **Absolute timestamps.** `2026-08-14 16:31`, not "2 days ago" — the reader is reconciling
   against their own memory of the week.
4. **Derived facts and generated prose look different.** The facts are the product.
5. **Never write to a ticket unattended.** A human presses save, always.
6. **Report on tickets, never on people.**
7. **Never `letter-spacing` on Thai**, and truncate by line count, not characters.
