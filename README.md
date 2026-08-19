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
| Use it day to day — standup, meeting notes, correcting it | [docs/DAILY_USE.md](docs/DAILY_USE.md) — Thai |
| See how it works | [docs/architecture.html](docs/architecture.html) — five flow diagrams plus the folder layout |
| The reasoning and the measurements | [pipeline/README.md](pipeline/README.md) |
| Which model was chosen and why the fine-tunes lost | [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md) — Thai, every number re-measured |
| A ten-slide summary | [docs/deck.html](docs/deck.html) |
| Demo it | [docs/DEMO_RUNBOOK.md](docs/DEMO_RUNBOOK.md) — Thai; `./demo.sh` drives the whole thing |

## Demo day

Both halves already run as login-time jobs on the demo machine
([deploy/](deploy/README.md)), so demo day is three commands rather than two
terminals kept alive by hand:

```bash
./demo.sh reset      # clear what a rehearsal left behind (snapshots it first)
./demo.sh up         # make sure the dashboard and the bot are running
./demo.sh share      # optional: a public URL so the room can open the dashboard itself
```

`./demo.sh` on its own prints one screen: is the dashboard answering, is the bot
running, is it reading the pipeline or its own fixture, are the scheduled jobs off,
how much rehearsal data is left, and which URLs are live. The beats themselves are
driven from Slack with `/mt demo` — see the runbook.

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

It prints `Ready: 29 record(s), 4 topic(s), 2 blocked` before it serves anything.
Then open <http://localhost:8899> — digest, blockers, one work item's timeline
across Slack *and* the meeting, and a search that shows why each result matched.

`--days 3650` is not a typo. The digest window defaults to 7 days and the
committed Slack export is dated 2025-08-01, so a narrow window shows the meeting
and nothing else. Point `--records` at a real export and `--days 7` is the value
you want.

Budget for the first run: the default embedding model `BAAI/bge-m3` is a 2.2 GB
download into `~/.cache/huggingface`, once. After that it is cached, and so are the
embeddings themselves.

## The two halves, joined

The bot can build its own ledger, or read the pipeline's. Reading the pipeline is
the better mode: one side owns what a work item is, and the grouping comes from a
multilingual embedding model (`BAAI/bge-m3`, the code default) rather than
character trigrams.

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
prints what came back: four work items, every evidence id and citation resolved
inside its own item, permalinks rebuilt for the 18 Slack messages and correctly
none for the 9 meeting utterances. With `TAM_API_URL` set there is **no
fallback**: if the pipeline cannot answer, the bot refuses to start rather than
serve stale fixture data that looks identical to live.

## The relevance gate: two signals, not a threshold

`check-api`'s last step measures the gate that decides whether recall answers at
all. It reads two **absolute** numbers that `/api/search` returns as
`relevance`, and both have to fire:

| signal | rule | what it catches |
| --- | --- | --- |
| `lexical` — raw BM25 of the best-matching record | `> 0` | nonsense shares no vocabulary with the corpus, so BM25 scores it exactly `0.00` |
| `dense` — raw cosine of the nearest record | `>= TAM_MIN_COSINE` | a real question asked in words the corpus does not use, which BM25 alone would throw away |

Cosine alone — the old mechanism — cannot do this, and the fault was the
mechanism rather than the model. `max cosine` over N documents rises with N for
*any* query, so past a few hundred records something always looks similar. Here is
the calibration block from a 936-record private export served with `BAAI/bge-m3`:

```text
  bm25   0.00 · cos 0.597  · filtered      "qqqzzzxxx wvwvwv jjjkkk zzzqqq"
  bm25   0.00 · cos 0.457  · filtered      "zxqv frobnicate wibble plumbus grommet"
  bm25   0.00 · cos 0.578  · filtered      "ฟฟฟกกก ผผผ ฃฃฃ ฅฅฅ"
  bm25  11.06 · cos 0.731  ✕ passes gate   "ๆๆๆ ฯฯฯ ฤฤฤ ฅฅฅ"
```

(The verdict labels are translated — the script itself prints Thai; the full
untranslated block is in [docs/USER_MANUAL.md](docs/USER_MANUAL.md) §7.6.)

All four clear a 0.45 cosine floor (0.457 – 0.731), so cosine would have admitted
every one of them; three score BM25 `0.00` and the pair rejects them. Six real
queries taken from the same corpus were all kept — the gate lost none of them.

**Three of four, not four — and the run says so out loud.** `ๆ` and `ฯ` are
ordinary Thai punctuation that occurs in real messages, so that probe has a
genuine lexical match. It is the edge of the mechanism, not a misconfiguration,
and it stays in the probe list on purpose: a check that quietly dropped it would
report a clean pass over a hole that is still open.

It is tempting to blame corpus size for any of this. Measured, size is not the
driver — subsampling one corpus barely moves the worst nonsense score, while
corpora of *similar size and different content* move it a lot. So the floor is a
property of one corpus and one model together; there is no default that is right
everywhere, which is why `check-api` re-measures both signals where it runs
instead of trusting a number printed in a README. The subsample table, the five
embedding models compared, the cross-encoder reranker that was tried as a gate and
rejected, and the two locally fine-tuned models that lost are all in
[docs/EXPERIMENTS.md](docs/EXPERIMENTS.md).

The exit code answers only "is the integration sound?" — did the pipeline answer,
do items resolve, does recall come from embeddings. Calibration prints either way
and does not fail the run: the block above exits `0` with a `⚠ 3/4` warning.
`--strict-gate` folds calibration back into the exit code for CI against a real
corpus, and the same run then exits `1`. Pass a query *before* the flag —
`npm run check-api -- "<query>" --strict-gate` — because the first positional
argument is read as the recall query, so `-- --strict-gate` on its own makes the
flag itself the query and the run fails for the wrong reason.

A pipeline too old to send `relevance` makes the bot throw rather than pass
everything through: a gate that silently stops gating is the failure this whole
mechanism exists to prevent.

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
