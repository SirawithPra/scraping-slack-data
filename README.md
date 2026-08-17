# ตาม (Tam)

**Reads Slack and meeting notes, merges them into one record per work item, and
tells you what is stuck, why, and which exact message proves it.**

State is computed by rules over typed relations, never by a language model. A
model writes the summary sentence and nothing else, its citations are verified in
code, and a summary left without any is labelled `unverified` on screen.

```
pipeline/     Python — ingest, retrieval, analysis, and the FastAPI dashboard
slack-bot/    TypeScript — Bolt app in Socket Mode
docs/         manual, concept, deck, diagrams
```

Two halves, same level, entered the same way: `cd pipeline` or `cd slack-bot`.
Each is self-contained — its own `.env.example`, its own Slack app manifest, its
own `data/`.

## Start here

| You want | Go to |
| --- | --- |
| Install it and use it | [docs/USER_MANUAL.md](docs/USER_MANUAL.md) — Thai, every command tested |
| See how it works | [docs/architecture.html](docs/architecture.html) — six diagrams |
| The reasoning and the measurements | [pipeline/README.md](pipeline/README.md) |
| A ten-slide summary | [docs/deck.html](docs/deck.html) |

## Run it in two minutes, with no Slack access

A Thai/English sample export is committed, so the whole path works before any
token exists.

```bash
python3 -m venv .venv && source .venv/bin/activate
cd pipeline && python3 -m pip install -r requirements.txt

python3 -m tam.ingest.prepare_messages \
        --raw data/sample/slack_messages.sample.json \
        --out data/processed/sample_messages.json
python3 -m tam.web.server --records data/processed/sample_messages.json --port 8899
```

Then open <http://localhost:8899> — digest, blockers, one work item's timeline,
and a search that shows why each result matched.

## The two halves, joined

The bot can build its own ledger, or read the pipeline's. Reading the pipeline is
the better mode: one side owns what a work item is, and the grouping comes from a
trained embedding model rather than character trigrams.

```bash
cd pipeline   && python3 -m tam.web.server --records data/processed/combined.json --port 8899
cd slack-bot  && TAM_API_URL=http://127.0.0.1:8899 npm run check-api
```

`check-api` exercises the bot's whole boot path with no Slack in the loop and
prints what came back. With `TAM_API_URL` set there is **no fallback**: if the
pipeline cannot answer, the bot refuses to start rather than serve stale fixture
data that looks identical to live.

## What is real, and what is not

| Real | Not connected yet |
| --- | --- |
| Slack export, cleanup, meeting transcripts | YouTrack / Notion reads |
| Retrieval: embeddings + BM25 + structural signals + rerank | Ticket write-back |
| Clustering, typed relations, work-item identity, state and evidence | Drift detection — needs a ticket system to compare against |
| Dashboard, its JSON API, and the bot reading them | Resolving Slack ids to display names |
| Human ticket-link corrections, written to the file the linker reads | |
| Decision log with supersession, written when someone files one | |

Nothing in the third column is faked in the running product. Drift has no live
source, so there are none; the fixture's example loads only under
`DEMO_FIXTURES=1`, and the rendered block then says on screen that it is an
example.

## Privacy

Every model runs locally and nothing leaves the machine unless you set
`SUMMARIZER=claude` yourself. Real exports, derived records, embedding caches,
fine-tuned weights, both `.env` files, and the corrections people write are all
gitignored — what is committed is code plus synthetic samples.
