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
| See how it works | [docs/architecture.html](docs/architecture.html) — five flow diagrams plus the folder layout |
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
        --out data/processed/sample_combined.json
python3 -m tam.ingest.meetings --transcript data/sample/standup.vtt \
        --title "Daily standup" --started 2026-08-14T09:30 \
        --merge-into data/processed/sample_combined.json
python3 -m tam.web.server --records data/processed/sample_combined.json \
        --days 3650 --port 8899
```

It prints `Ready: 27 record(s), 5 topic(s), 1 blocked` before it serves anything.
Then open <http://localhost:8899> — digest, blockers, one work item's timeline
across Slack *and* the meeting, and a search that shows why each result matched.

`--days 3650` is not a typo. The digest window defaults to 7 days and the
committed Slack export is dated 2025-08-01, so a narrow window shows the meeting
and nothing else. Point `--records` at a real export and `--days 7` is the value
you want.

## The two halves, joined

The bot can build its own ledger, or read the pipeline's. Reading the pipeline is
the better mode: one side owns what a work item is, and the grouping comes from a
trained embedding model rather than character trigrams.

Two terminals, because the first one stays in the foreground:

```bash
# terminal 1
cd pipeline
python3 -m tam.web.server --records data/processed/sample_combined.json --days 3650 --port 8899

# terminal 2
cd slack-bot
TAM_API_URL=http://127.0.0.1:8899 npm run check-api
```

`check-api` exercises the bot's whole boot path with no Slack in the loop and
prints what came back: five work items, every evidence id and citation resolved
inside its own item, permalinks rebuilt for the 18 Slack messages and correctly
none for the 9 meeting utterances. With `TAM_API_URL` set there is **no
fallback**: if the pipeline cannot answer, the bot refuses to start rather than
serve stale fixture data that looks identical to live.

Its last step measures the relevance gate, and **on this 27-record sample the gate
does not hold**. Three gibberish queries are scored against the corpus:

```text
  0.481  ✕ passes the gate  "qqqzzzxxx wvwvwv jjjkkk zzzqqq"
  0.210  · filtered out     "zxqv frobnicate wibble plumbus grommet"
  0.767  ✕ passes the gate  "ฟฟฟกกก ผผผ ฃฃฃ ฅฅฅ"
  0.726  · a real query     "Profile module bug บน Android"
```

Thai gibberish scores *above* a genuine query, so no floor separates them here.
The check then scores several real queries too — because a floor has to clear the
*weakest* genuine question, not the strongest — and on the 42-record private export
the picture is the same shape rather than better: gibberish tops out at 0.581 while
the weakest real query sits at 0.473, so the two overlap and raising the floor
would start throwing away real questions. That is a property of the served
embedding model, not of the wiring, and the response is a better model, never a
higher `TAM_MIN_COSINE`.

The exit code answers only "is the integration sound?", so this reports loudly
without failing the run; `--strict-gate` folds it back in, for CI against a real
corpus.

## What is real, and what is not

| Real | Not connected yet |
| --- | --- |
| Slack export, cleanup, meeting transcripts | YouTrack / Notion reads |
| Retrieval: embeddings + BM25 + structural signals + rerank | Ticket write-back |
| Clustering, typed relations, work-item identity, state and evidence | Drift detection — needs a ticket system to compare against |
| Dashboard, its JSON API, and the bot reading them | Resolving Slack ids to display names |
| Human ticket-link corrections, written to the file the linker reads | |
| Decision log with supersession, written when someone files one | |

Nothing in the right-hand column is faked in the running product. Drift has no
live source, so there are none; the fixture's example loads only under
`DEMO_FIXTURES=1`, and the rendered block then says on screen that it is an
example.

## Privacy

Every model runs locally and nothing leaves the machine unless you set
`SUMMARIZER=claude` yourself. Real exports, derived records, embedding caches,
fine-tuned weights, both `.env` files, the ledger `npm run ledger` builds, and the
corrections people write are all gitignored — what is committed is code plus
synthetic samples.

## License

MIT — see [LICENSE](LICENSE). Clone it, run it, fork it.
