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

**Drive it with `/meowtam demo`, not from memory.** Eleven beats, fired one at a time. A live
demo that depends on you typing the right thing in the right order in front of judges will
desync.

The step-by-step runbook for demo day — what to open, what to check first, what each beat needs
to be configured, the links to have on screen, and what to do when a beat comes up empty — is
[`../docs/DEMO_RUNBOOK.md`](../docs/DEMO_RUNBOOK.md). This section is the reference; that one is
the thing you follow with a laptop open.

Beats 1–7 are **one morning, on the clock the real jobs run on** — 08:45 the DM, 09:00 the post,
the thread fills, 09:25 the digest, 09:30 the silence check, 10:45 the summary. It is the same
order the `scheduleDaily` calls fire in, so nothing on stage happens before the thing it reads
from and you never have to explain a jump backwards. The sequence matters because the three
claims worth showing are all *about elapsed time*: a morning happens, the same blocker survives
it, and a work item nobody has mentioned all week surfaces on its own.

Beats 8–11 are the rest of the day, which has no clock. They are what a person reaches for when
something happens, ordered by how often that is.

Beats 1 and 4 write **simulated** data — a laptop set up yesterday has no history for "the same
line, three mornings running" to be true of. Every seeded answer is labelled `(จำลอง)` wherever
it is rendered, the summary carries a warning line saying how many of its answers are seeded, and
`/meowtam demo clear` removes all of it. Say the label out loud; it is a better answer than
pretending.

`/mt` is a registered alias for the same handler — use it on stage, it's shorter and Slack's
autocomplete on `/me` can hesitate.

```
/meowtam demo          → which beat is next
/meowtam demo 1        → fire beat 1
/meowtam demo next     → fire the next one
/meowtam demo reset    → back to beat 1 (data untouched)
/meowtam demo clear    → delete every simulated morning
```

| # | Beat | The real thing it stands for | The line to say |
| --- | --- | --- | --- |
| 1 | Writes the previous working mornings into the daily history | — *(seeded; in real life those mornings simply happened)* | "Two mornings already happened. Same person, same blocker, worded differently each day." |
| 2 | **08:45** standup DM, to everyone in `STANDUP_USERS` — pressing **ส่ง** records that day's daily answer | the 08:45 schedule · the **ส่ง** / **ข้ามวันนี้** buttons in the DM | "It doesn't ask what I did. It tells me, I correct it — and the correction *is* my standup. Nobody types it twice." |
| 3 | **09:00** daily post, carrying that blocker forward | `/mt daily post` · the 09:00 schedule | "It doesn't ask what's outstanding. It already knows and puts it at the top." |
| 4 | Simulated answers into today's thread | `/mt daily` → the private form → reply in the thread | "Anyone can type a real answer in the same thread — a real one wins over the placeholder." |
| 5 | **09:25** digest in the channel | `/mt digest` · the 09:25 schedule · **ดูข้อความ** / **ที่มา** on any claim | "Blocked first. Every claim, one click from the message that proves it." |
| 6 | **09:30** work nobody has touched for `TAM_STALE_WORKDAYS` working days | `/mt stale post` (`/mt stale` to look first) · the 09:30 schedule | "Nobody flagged these. Nobody mentioned them at all — that's a different illness, and only a count can find it." |
| 7 | **10:45** thread summary **and** the pending escalation | `/mt daily summary` · the 10:45 schedule | "Third morning running. It says so once, to the channel, and never again for the same line." |
| 8 | The paste modal, prefilled with a chat from a DM | `/mt paste` · ⋯ → **ผูกกับ ticket** on any message | "The decision happened in a DM no token can reach. Paste it, pick the ticket, and it lands in the corpus attached to that ticket. No ticket yet? Leave the picker empty — it is kept and searchable, and the screen says exactly what that costs." |
| 9 | Scope-change message, then a threaded nudge | `/mt drift` → **ดูร่างที่เสนอ** in the card | "The requirement changed here. The ticket didn't. It noticed — and it writes a comment on the real ticket." |
| 10 | Recall with the decision chain | `/mt recall <question>` — or any text that isn't a command | "May we said no BOM. August we changed it. The ticket still says May." |
| 11 | The board | `/mt` · `/mt @someone` · `/mt PROJ-142` · `/mt blocked` | — |

`/meowtam demo` with no argument prints this list with the real command under each beat, so the
tour of the demo is also the tour of the command list — and the question that always follows a
demo ("so how would I actually do that?") is answered on the screen already open.

### Every action, and the beat that shows it

Nothing in the bot is reachable only through the demo, and nothing in the demo is a path the
product does not have. Everything with a `—` is real and simply does not need stage time.

| Action | Beat |
| --- | --- |
| `/mt` · `/mt @someone` · `/mt PROJ-142` | 11 |
| `/mt blocked` | 11 *(the digest at 5 already sorts blocked first)* |
| `/mt daily` (private form) | 4 |
| `/mt daily post` \| `post again` | 3 |
| `/mt daily summary` | 7 |
| `/mt digest` | 5 |
| `/mt stale` \| `stale post` | 6 |
| `/mt paste` | 8 |
| `/mt drift` | 9 |
| `/mt silent` \| `quiet` | — *tickets open and untouched for `TAM_SILENT_DAYS`; same `/api/tracker` fetch as beat 9, other half of the report* |
| `/mt recall <question>` · any unrecognised text | 10 |
| `/mt projects` | — *which channel is which project; worth typing before beat 8 if the ticket picker looks wrong* |
| `/mt format` \| `help` \| `template` | — *the format the parser reads; send it to the team, don't demo it* |
| `/mt reload` | — *operator command: re-read the ledger mid-demo without restarting* |
| ⋯ → **ผูกกับ ticket** (any message) | 8 *(same write path: link override → YouTrack comment → rebuild)* |
| ⋯ → **บันทึกเป็นการตัดสินใจ**, and 📌 / 🧠 | — *this is what fills the decision chain beat 10 reads; either way the message ends up wearing 🧠, and the filed decision shows on that work item's card* |
| 🎫 / 🚧 / ✅ reactions | — *hint only, they write nothing, and the ephemeral reply says so* |
| **ส่ง** / **ข้ามวันนี้** in the standup DM | 2 |
| **ดูข้อความ** / **ที่มา** on any claim, **เปิด** / **ดูงานนี้ในบอร์ด** on any card | 5, 11 |
| **ดูร่างที่เสนอ** → the *อัปเดต ticket* modal → **บันทึกลง YouTrack** | 9 |
| **ไม่ใช่การเปลี่ยนสโคป** on a drift card | 9 *(say out loud that dismissals are logged, not stored — the reply says so too)* |
| **อ่านให้ดูก่อน** → **เก็บเข้า corpus แล้วผูกกับ …** (or **เก็บเข้า corpus (ยังไม่ผูก ticket)**) / **ยกเลิก** in the paste flow | 8 |
| Schedules 08:45 · 09:00 · 09:25 · 09:30 · 10:45 (`ENABLE_SCHEDULE=1`) | 2 · 3 · 5 · 6 · 7 — the beats *are* these jobs, fired by hand |

**Beat 6 will not invent anything.** If nothing has actually been quiet for `TAM_STALE_WORKDAYS`
working days, it says so and names the quietest thing there is with its real number. That is the
right behaviour and a good thing to point at.

**Beat 8 is the one that closes the loop.** After you press *เก็บเข้า corpus*, the reply is a
result object, not a congratulation: which file the link went to, the YouTrack comment id (go and
find it on the ticket), and — the line that matters — how many of those pasted messages the
**rebuilt** ledger now holds under that ticket. If the answer is zero it says zero.

**Beat 9 writes to YouTrack for real** when `YOUTRACK_WRITE=1`. It writes a *comment*, never the
description: overwriting a description from a Slack modal destroys whatever the PO wrote there
with no undo. With the switch off, it shows the exact text it would have written and says which
variable is unset — it never claims a write that did not happen.

### Rehearse this

- **Record a backup video by hour 18.** Live Slack demos fail. Venue wifi, rate limits,
  a workspace that logs you out. A recording that plays is worth more than a live demo that
  might not.
- Run `/meowtam demo clear` then `/meowtam demo reset` right before you present, so beat 1
  seeds a fresh history instead of stacking on yesterday's rehearsal.
- Have the digest channel already open on screen, scrolled to the bottom.
- Beat 2 posts to *your* DMs — have that conversation open in a second window or the demo
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
The offline matching is character-trigram based specifically because Thai has no spaces between
words. Word tokenisation without a Thai tokeniser silently produces garbage. Try
`/meowtam recall` with a Thai paragraph. With `TAM_API_URL` set it is the pipeline's multilingual
embeddings answering instead — `BAAI/bge-m3`, whose 8192-token context does not silently truncate
a long thread record the way a 128-token model does
([`../docs/EXPERIMENTS.md`](../docs/EXPERIMENTS.md)).

**"Is this surveilling my team?"**
It reports on tickets, never on people. No per-person message counts, no activity
leaderboards. Nudges go by DM, never to the channel. That is a design rule, not a setting.

---

## Commands

| Command | What it does |
| --- | --- |
| `/meowtam` | your board |
| `/meowtam daily` | the form, privately · `daily post` posts today's · `daily summary` runs the 10:45 collection now |
| `/meowtam blocked` | what is blocked right now |
| `/meowtam stale` | work nobody has touched for `TAM_STALE_WORKDAYS` working days · `stale post` announces it in the channel |
| `/meowtam paste` | keep a chat copied out of a DM — parse preview first, then store and rebuild · with a ticket it also writes the override and a comment; the picker may be left empty, and then only the corpus is written |
| `/meowtam projects` | which channels are which project, and what this one is mapped to |
| `/meowtam digest` | the 09:25 screen on demand |
| `/meowtam drift` \| `silent` | where Slack and the tracker disagree, and which tickets went quiet |
| `/meowtam recall <คำถาม>` | search with the decision chain |
| `/meowtam demo …` | the demo driver — see above |

Message shortcuts (the `⋯` menu on any message): **ผูกกับ ticket**, **บันทึกเป็นการตัดสินใจ**.
📌 and 🧠 file a decision too.

### Linking a message to a ticket

The picker searches **YouTrack itself**, per keystroke, not the work items the pipeline built.
That distinction is the whole fix: a ticket already named in Slack is already linked, and the one
you are reaching for is the one nobody has typed yet. The old menu offered work items, capped at
100 with no search box, so past the hundredth item a ticket was simply unreachable.

Type a key (`REVERAPP-140`), a bare number (`140`, when the channel names one project), or words
from the title. Set `TAM_CHANNEL_PROJECTS` and the picker opens on that channel's project
instead of the whole tracker — and the pipeline's linker uses the same map to prefer the
channel's own project when a message names two tickets.

Pressing **ผูก** does four things and reports each one separately, including the ones that
failed: writes the correction to the file the linker reads as its top tier, writes a comment on
the ticket (when `YOUTRACK_WRITE=1`) with the Slack permalink in it so the link is two-way,
rebuilds the pipeline index, and then says how many of those messages the rebuilt ledger actually
holds under that ticket.

---

## What is real and what is faked

Say this plainly if asked — judges reward knowing the difference.

| Real | Faked / simulated |
| --- | --- |
| Slack app, commands, modals, threads, emoji actions | The daily history beats 1 and 4 seed — labelled everywhere, removed by `/meowtam demo clear` |
| **YouTrack read**: the ticket picker searches the live tracker, every ticket, per keystroke | Meeting transcripts (fixture only — Teams Graph needs a tenant toggle, see below) |
| **YouTrack write**: a real comment, with its comment id returned — needs `YOUTRACK_WRITE=1` | The 08:45 / 09:25 schedules (off by default; `ENABLE_SCHEDULE=1` turns them on) |
| Clustering, state detection, evidence linking, the working-day silence count | |
| Pasting a DM into the corpus and attaching it to a ticket, verified against the rebuilt ledger | |
| Recall, decision chains, supersession | |

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
  tam-api.ts        client for the Python pipeline's HTTP API, the shape translation,
                    and the two-signal relevance gate (passesGate)
  store.ts          the bot's own writes: ticket-link overrides and the decision log
  standups.ts       standup drafts derived from work items, not read from a fixture
  daily.ts          the daily thread: the clock, the parser, the pending-streak count
  demo.ts           the seeded morning, so a time-based claim can be shown at all
  projects.ts       which channels are which project (TAM_CHANNEL_PROJECTS)
  youtrack.ts       ticket search per keystroke, and the one call that writes a comment
  stale.ts          working-day silence: the count, and what counts as already said
  search.ts         recall: the pipeline when TAM_API_URL is set, else the local
                    trigram + literal-term hybrid, no API key
  app.ts            commands, actions, modals, the demo driver
  blocks/
    common.ts       shared renderers — state labels, evidence buttons, Thai-safe truncation
    digest.ts       the 09:25 screen
    standupDm.ts    the 08:45 DM
    itemCard.ts     one work item, full detail
    drift.ts        the nudge and the diff modal
    daily.ts        the morning post, the form, the 10:45 summary, the escalation
    link.ts         the ticket picker options, the paste preview, and the link result
    stale.ts        work nobody has touched for N working days
    recall.ts       search results and decision chains
scripts/
  export-slack.ts   real channel history via bot token
  build-ledger.ts   messages → work items, states, drifts, decisions
  preview.ts        render every payload offline, validate against Slack's limits
  check-api.ts      prove the bot can read the pipeline and re-measure the gate,
                    with no Slack in the loop
tests/              `npm test` — 53 tests: Slack's limits (blocks), the shape
                    translation (tam-api), where the ledger came from (provenance),
                    the store, the recall gate (gate), and what git tracks
                    (tracked-data). fixtures/pipeline-api.json is a recorded response
data/
  ledger.fixture.json  the committed fixture — anonymised, safe for a public repo
  ledger.json          what the bot reads: seeded from the fixture, overwritten by
                       `npm run ledger`, and gitignored so real channel history
                       never lands in a commit
  decisions.json       the decision log, written on demand and gitignored; absent
                       until someone files one (`TAM_DECISIONS_PATH` moves it)
  dailies.json         one record per morning: the post, the answers people typed
                       verbatim, and which escalations were already sent. Gitignored —
                       it is what colleagues wrote about their own work
  announcements.json   what has already been said in the channel, so a restart does
                       not repeat yesterday's escalation
```

```bash
npm test                   # 53 tests: Slack's limits, the shape translation, the
                           # store, the recall gate, and what git tracks
npm run preview            # validate every Block Kit payload, no Slack needed
npm run preview -- digest  # dump one payload for app.slack.com/block-kit-builder
npm run typecheck          # tsc --noEmit; prints nothing when it is clean
```

### Reading from the pipeline

By default the bot builds its own ledger from `data/ledger.json` and touches no network but
Slack. Set `TAM_API_URL` and the Python half becomes the owner of what a work item is —
work items, states, evidence, timelines and recall all come from `tam.web.server`, while
decisions, drifts and standup drafts stay local because the pipeline has no counterpart for
them yet. There is **no fallback**: if the pipeline is asked for and cannot answer, the bot
refuses to start rather than serve fixture data that looks identical to live.

#### Recall: one call, then a gate on two signals

One `GET /api/search?q=…&k=…` per query, not two. The response carries a top-level
`relevance: {lexical, dense}` — the raw BM25 and the raw cosine of the best-matching record,
both absolute — and `passesGate()` in `src/tam-api.ts` requires **both** of:

| condition | what it catches |
| --- | --- |
| `lexical > 0` | nonsense. It shares no vocabulary with the corpus, so BM25 scores it exactly `0.000` |
| `dense >= TAM_MIN_COSINE` | a stray shared token that means nothing close to the query. Dense is also the signal that lets a genuinely re-worded question rank at all, which BM25 alone cannot do |

A refused query returns no hits at all, which is how recall says *nothing matched* instead of
five confident-looking rows. Nothing else in the response can say it: the fused `score` is
rank-derived — measured against the live corpus, rank 1 of `qqqzzzxxx wvwvwv jjjkkk zzzqqq`
scored **0.0328** while rank 1 of a real question scored **0.0297**, so the nonsense scored
*higher* — and every per-hit `why` is min-max normalised inside its own result set, so `dense`
on the top hit reads `1.00` either way.

The cosine floor alone was the old mechanism and it could not work: max cosine over N documents
rises with N for any query, so on ~1,000 records something always looks similar, and across five
embedding models a floor rejected **0 of 5** nonsense probes. Both signals together reject 4 of 5
and lose 0 of 12 real queries. The one that still passes is a run of Thai punctuation (ๆ ฯ) that
really does occur in messages, so its lexical match is genuine. Tables and reasoning:
[`../docs/EXPERIMENTS.md`](../docs/EXPERIMENTS.md) — this README does not restate them, so the two
cannot drift. A pipeline too old to send `relevance` makes the bot throw rather than pass
everything.

```bash
TAM_API_URL=http://127.0.0.1:8899 npm run check-api      # fetch, translate, index, search
TAM_API_URL=… npm run check-api -- "คำถามของคุณ"          # …with your own recall query
TAM_API_URL=… npm run check-api -- "คำถามของคุณ" --strict-gate   # calibration counts in the exit code
```

`check-api` prints `bm25` and `cos` for every probe, real and nonsense, and keeps the two verdicts
apart: the exit code answers *is the integration sound* — pipeline reachable, every claim's
evidence resolving, recall coming from embeddings and not local trigrams — while gate calibration
is reported and does not fail the run unless `--strict-gate` is passed, because the right floor is
a property of a corpus and a model rather than of this code. The query is `process.argv[2]`, so put
it **before** the flag: `npm run check-api -- --strict-gate` on its own searches for the literal
string `--strict-gate` and dies with no results.

#### Environment

Every variable, with the value `.env.example` ships. Empty means the code default applies; the
reasoning for each one is in `.env.example` line by line, and `../pipeline/README.md` covers the
server side.

| variable | shipped | what it does |
| --- | --- | --- |
| `SLACK_BOT_TOKEN` | — | `xoxb-…`, from OAuth & Permissions |
| `SLACK_APP_TOKEN` | — | `xapp-…` with `connections:write`; Socket Mode needs it |
| `SLACK_SIGNING_SECRET` | — | Basic Information → App Credentials |
| `DIGEST_CHANNEL` | — | channel **ID** the digest posts to; the bot must be a member |
| `EXPORT_CHANNELS` | — | comma-separated channel IDs for `npm run export` |
| `EXPORT_DAYS` | `14` | how far back the export walks (unset → 120) |
| `STANDUP_USERS` | — | comma-separated user IDs that get the 08:45 DM |
| `ENABLE_SCHEDULE` | — | `1` arms the 08:45 / 09:25 schedules; empty leaves them off |
| `TAM_SCHEDULE_TZ` | — | IANA zone for those schedules; empty means the host clock |
| `TAM_API_URL` | — | pipeline base URL. Empty = offline against `data/ledger.json` |
| `SLACK_WORKSPACE_URL` | — | permalink host (unset → `https://slack.com`, which interstitials) |
| `TAM_STALE_DAYS` | `3` | an active item quiet longer than this renders as stalled |
| `TAM_MIN_COSINE` | `0.45` | the `dense` half of the recall gate — see above |
| `TAM_API_TIMEOUT_MS` | `20000` | per-request budget; a timeout at boot refuses to start |
| `TAM_OVERRIDES_PATH` | — | where ticket-link corrections are written (unset → `../pipeline/data/link_overrides.json`) |
| `TAM_DECISIONS_PATH` | — | the decision log (unset → `data/decisions.json`) |
| `TAM_RECENT_DAYS` | `1.5` | activity inside this window counts as "what you did" |
| `TAM_NAMES` | — | `slack` \| `pseudonym` \| `id`. Empty picks `slack` when the name cache exists, else `pseudonym`. **Set it to the same value as `pipeline/.env`**, or the dashboard and Slack name the same person differently |
| `TAM_NAMES_PATH` | — | the id → name mapping both halves read (unset → `../pipeline/data/user_names.json`, gitignored). Written by `python3 -m tam.ingest.users --fetch`; `/meowtam reload` re-reads it |
| `DEMO_FIXTURES` | — | `1` loads the fixture's drift for the drift beat, labelled in the UI |
| `YOUTRACK_URL` / `YOUTRACK_TOKEN` | — | the bot searches the tracker directly for the ticket picker. Empty → it goes through the pipeline instead (needs `TAM_API_URL` + `TAM_ADMIN_TOKEN`) |
| `YOUTRACK_PROJECTS` | — | which projects the picker searches when the channel is not mapped |
| `YOUTRACK_WRITE` | — | `1` lets the bot write a real comment on a ticket. Off means it says so, with the reason, and never claims otherwise |
| `YOUTRACK_WRITE_TOKEN` | — | a token that may comment; falls back to `YOUTRACK_TOKEN`, but only once `YOUTRACK_WRITE` is on |
| `TAM_CHANNEL_PROJECTS` | — | `REVERAPP (Rever App)=C0ABC,#reverapp-qa; MOB=C0GHI` — which channels are which project. **Same variable and syntax in `pipeline/.env`** |
| `TAM_ADMIN_TOKEN` | — | required by `/meowtam paste` and the reindex after a link; the pipeline prints one at boot when unset |
| `TAM_API_WRITE_TIMEOUT_MS` | `300000` | budget for the write routes, which re-embed the corpus |
| `TAM_STALE_WORKDAYS` | `5` | working days of silence before a work item is raised in the channel |
| `TAM_ANNOUNCEMENTS_PATH` | — | what has already been announced (unset → `data/announcements.json`) |

## Design rules that are not negotiable

1. **No claim without a link.** Every computed statement carries the message that proves it.
2. **State is icon + word, never colour alone.** Colour-blind readers, and Slack themes.
3. **Absolute timestamps.** `2026-08-14 16:31`, not "2 days ago" — the reader is reconciling
   against their own memory of the week.
4. **Derived facts and generated prose look different.** The facts are the product.
5. **Never write to a ticket unattended.** A human presses save, always — and writing is off
   until somebody sets `YOUTRACK_WRITE=1`. What gets written is a *comment*, never the
   description: overwriting a description from a modal destroys what the PO wrote, with no undo.
6. **Report on tickets, never on people.**
7. **Never `letter-spacing` on Thai**, and truncate by line count, not characters.
