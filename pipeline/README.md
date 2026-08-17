# Slack Semantic Search PoC

Proof of concept for one question: **can Slack messages written in different
styles and languages, but about the same software-development topic, be found
again?**

Cosine similarity over embeddings was the first answer. It is no longer the only
one here — cosine is symmetric, blind to exact strings like `REV-1421`, and
cannot use the fact that two messages sit in the same thread five minutes apart.
Each of those is a different kind of evidence, and this repo now combines them.

```text
Slack channel
→ export messages + threads       (tam/ingest/export_slack.py)
→ data/raw/slack_messages.json
→ clean / normalize               (tam/ingest/prepare_messages.py)
→ data/processed/messages.json
│
├─ retrieval ─────────────────────────────  (tam/retrieval/retrieve.py)
│    dense cosine       embeddings.py  meaning, across languages
│  + BM25               lexical.py     exact ids, names, error codes
│  + anchor overlap     signals.py     shared concrete strings
│  + thread/time/author signals.py     Slack's own structure
│  → fuse (RRF or z-score)  fusion.py
│  → cross-encoder rerank   rerank.py
│  → Top 10 related messages
│
├─ topics ────────────────────────────────  (tam/analysis/graph.py)
│    one graph, every signal → Louvain communities → clusters
│
├─ work-item identity ────────────────────  (tam/analysis/linker.py)
│    ticket key > thread > cluster consensus, each link naming its evidence
│
└─ typed relations ───────────────────────  (tam/analysis/relations.py)
     resolves / blocked_by / duplicates / answers / follows_up, directed
```

Every model runs locally and nothing is sent to an API, unless you explicitly set
`SUMMARIZER=claude`.

## Three surfaces, one idea

The repo grew two front ends over that pipeline. They are deliberately separate
processes — a crash in one cannot take the other down — and they do not import
each other:

| Surface | Stack | What it is for | Entry point |
| --- | --- | --- | --- |
| **CLI** | Python | Building and measuring the pipeline itself | `python3 -m tam.retrieval.retrieve` |
| **Dashboard** | Python · FastAPI | Reading the day: digest, blockers, one work item's timeline | `python3 -m tam.web.server` |
| **Slack bot** | TypeScript · Bolt | Where the work is actually discussed | `cd ../slack-bot && npm start` |

**The seam is closed.** Set `TAM_API_URL` and the bot stops deciding what a work
item is: items, states, evidence, timelines and recall all come from
`tam.web.server`, so the trained embedding model — not character trigrams — is
what groups and ranks. Leave it unset and the bot runs offline against its own
fixture exactly as before.

```bash
# terminal 1
python3 -m tam.web.server --records data/processed/sample_combined.json --days 3650 --port 8899
# terminal 2
cd ../slack-bot && TAM_API_URL=http://127.0.0.1:8899 npm run check-api
```

(That corpus is the committed sample; build it in two commands under [Try it
without Slack credentials](#try-it-without-slack-credentials), or point
`--records` at your own export and drop `--days`.)

`check-api` exercises the whole boot path without Slack in the loop and prints
what came back. Decisions, standup drafts and drift stay on the bot's side,
because the pipeline has no counterpart for them yet — see [Reading from the
pipeline](#reading-from-the-pipeline).

## Layout

```text
tam/                    the Python package — import it, or run any module with -m
├── core.py             records in, matches out: load_records, embed_records, search
├── ingest/             Slack export, markup cleanup, meeting transcripts
├── retrieval/          embeddings · lexical · signals · fusion · rerank · retrieve
├── analysis/           graph · relations · linker · digest · summarize
├── evaluation/         evaluate · weak_labels · compare_models · finetune
├── report/             visualize (Plotly HTML) · report_th (plain Thai)
└── web/                server.py — the FastAPI dashboard

../slack-bot/           the Slack bot, a separate npm project
├── src/                app.ts + Block Kit builders per surface
├── scripts/            export-slack → raw-slack.json → build-ledger → ledger.json
├── data/               ledger.fixture.json is committed; ledger.json is generated
├── tests/              npm test
└── slack-app-manifest.yaml

data/       raw/ (private) · processed/ (generated) · sample/ (committed, runs offline)
fixtures/   committed API response fixtures (digest, blockers, item, search)
models/     fine-tuned weights, generated and gitignored
tests/      python3 -m pytest, from here
slack-app-manifest.json   read-only Slack app for the exporter — history scopes only

../docs/    design brief, concept doc, user manual, deck, diagrams
```

Nothing sits at the repo root any more. Every module is reachable as
`python3 -m tam.<area>.<module>`, `--help` works on all 22 of them, and
`python3 -m pytest` from `pipeline/` runs the Python tests.

**New here?** [../docs/USER_MANUAL.md](../docs/USER_MANUAL.md) is the install-and-use
guide in Thai: prerequisites, both Slack apps, every command, and a
troubleshooting table. This README is the reasoning behind the design and the
measurements behind the claims. [../docs/deck.html](../docs/deck.html) is the ten-slide
summary.

**Start here:**

```bash
python3 -m tam.retrieval.retrieve -q "bug ใน Profile module แก้แล้วยัง" --explain
python3 -m tam.retrieval.retrieve --list-presets
python3 -m tam.evaluation.evaluate --presets dense hybrid hybrid-rerank full
```

## Setup

macOS. Use `python3`, never `python`.

> On this machine plain `python3` is Xcode's Python 3.9. The venv below is built
> with Homebrew's Python 3.10 because the ML wheels are better supported there.
> After activating, `python3` means the venv's 3.10, so every command below works.

```bash
python3.10 -m venv .venv          # or: python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

```bash
cp .env.example .env
# fill in SLACK_TOKEN and SLACK_CHANNEL_ID
```

## Run

```bash
python3 -m tam.ingest.export_slack           # Slack  -> data/raw/slack_messages.json
python3 -m tam.ingest.prepare_messages       # clean  -> data/processed/messages.json
python3 -m tam.core        # interactive search, top 10
```

Refreshing later, once meetings have been merged into that corpus, use
`--merge-into data/processed/messages.json` instead of the plain second line.
`--out` onto a corpus that holds meeting records refuses rather than overwrite
them, and names the flag that does what you meant.

Then type a Thai / English / mixed message. This paste is the committed sample
corpus prepared as in [Try it without Slack
credentials](#try-it-without-slack-credentials), so it is reproducible:

```text
Search:
> FE sorting เสร็จแล้วแต่ยังรอ BE API

Top Matches:

1. 0.80
   FE done, waiting for API
   user=U05QA  time=2025-08-01 06:06  thread=1754003200.001600  id=msg_C0SAMPLE01_1754003200.001600
```

Read the top hit twice: a Thai/English query, and the message it found is
English. The query's closest *Thai* paraphrase — `sorting หน้า candidate mock
ไว้ก่อน api หลังบ้านยังไม่มา` — scores **0.31** and sits at the bottom of the
ranking, on the edge of the ten hits `--top-k` prints by default. 0.80 against
0.31 for the same meaning in two languages is the cross-lingual limitation
measured at the bottom of this file, not a rounding error, and it is why the
pipeline below stops being dense-only.

Useful flags:

```bash
python3 -m tam.core --top-k 10
python3 -m tam.core -q "BE sorting API พร้อมแล้ว"   # one-shot, no prompt
python3 -m tam.core --include-threads               # also match whole threads
python3 -m tam.core --no-cache                      # ignore the embedding cache

python3 -m tam.ingest.export_slack --channel C0123ABCDEF --max-messages 100
python3 -m tam.ingest.prepare_messages --raw data/raw/slack_messages.json
```

### Try it without Slack credentials

A small Thai/English sample export is committed, so the pipeline can be checked
before any real token exists. Writing to its own path keeps a real export intact:

```bash
# Slack sample alone — 18 messages kept of 23, 22 searchable records
python3 -m tam.ingest.prepare_messages --raw data/sample/slack_messages.sample.json \
                            --out data/processed/sample_messages.json
python3 -m tam.core --records data/processed/sample_messages.json \
                           -q "FE sorting เสร็จแล้วแต่ยังรอ BE API"
python3 -m tam.evaluation.evaluate --records data/processed/sample_messages.json \
                    --eval-file data/eval_queries.example.json

# the same sample plus the committed standup transcript — 27 searchable records
python3 -m tam.ingest.prepare_messages --raw data/sample/slack_messages.sample.json \
                            --out data/processed/sample_combined.json
python3 -m tam.ingest.meetings --transcript data/sample/standup.vtt \
                    --title "Daily standup" --started 2026-08-14T09:30 \
                    --merge-into data/processed/sample_combined.json
python3 -m tam.web.server --records data/processed/sample_combined.json --days 3650 --port 8899
```

Two files rather than one because they answer different questions: the Slack-only
corpus is what the retrieval numbers below are quoted against, and the combined
one is what makes a work item span two sources. `--merge-into` needs a corpus that
already exists, which is why the prepare step is repeated for the second file.

**`--days 3650` is deliberate.** The digest window defaults to 7 days and the
committed Slack export is dated 2025-08-01, so a narrow window shows only the
meeting utterances and the digest, blockers and item pages come up nearly empty.
Widen it for the sample; use `--days 7` on a live export. The wide window is
honest rather than flattering — every item still prints its real age, so the
sample's Slack items read as more than a year stale, which they are.

## Slack token and scopes

Create a Slack app, install it to the workspace, and use its **Bot User OAuth
Token** (`xoxb-...`) or a user token (`xoxp-...`).

Slack hands out several token shapes and only some can read messages:

| Prefix | What it is | Works here |
| --- | --- | --- |
| `xoxb-` | Bot User OAuth Token (OAuth & Permissions page) | **yes** |
| `xoxp-` | User OAuth Token | **yes** |
| `xoxe.xoxb-` / `xoxe.xoxp-` | Rotating access token (expires ~12h) | yes, until it expires |
| `xoxe-` | Refresh / app-configuration token | no — `apps.*` manifest APIs only |
| `xapp-` | App-level token | no — Socket Mode only |

`tam/ingest/export_slack.py` rejects the last two up front, and calls `auth.test`
before paging so a bad token fails in one request instead of mid-export.

Fastest way to create the app with the right scopes: api.slack.com/apps →
**Create New App** → **From a manifest** → pick the workspace → paste
[slack-app-manifest.json](slack-app-manifest.json) → Create →
**Install to Workspace**, then copy the `xoxb-` token. That manifest also sets
`token_rotation_enabled: false`, so the token stays valid instead of expiring
every 12 hours.

| Need | Scope |
| --- | --- |
| Read a **public** channel's history and threads | `channels:history` |
| Read a **private** channel's history and threads | `groups:history` |
| Read a DM / group DM | `im:history` / `mpim:history` |

Both `conversations.history` and `conversations.replies` are covered by those
scopes. A bot token also has to be **invited to the channel**:

```text
/invite @your-app
```

Get `SLACK_CHANNEL_ID` from Slack: channel name → About → Channel ID (`C...`).

## Embedding model

Default: **`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`**
(384 dimensions, 458 MB, runs on CPU).

Why this one for the first experiment:

- Multilingual, including Thai, in one shared vector space, so a Thai message and
  its English paraphrase land near each other without translating anything.
- Trained on a **paraphrase** objective, which matches the task here — comparing
  message to message ("same topic, different wording"), not a question to a document.
- Small and fast enough to embed a few hundred messages on a laptop in seconds,
  and free (no API cost) for the hackathon.

### The model catalog

```bash
python3 -m tam.evaluation.compare_models --catalog
```

| Model | Size · dim · ctx | Notes |
| --- | --- | --- |
| `paraphrase-multilingual-MiniLM-L12-v2` | 118M · 384 · 128 | the fast baseline, and the default |
| `paraphrase-multilingual-mpnet-base-v2` | 278M · 768 · 128 | older, stronger baseline |
| `intfloat/multilingual-e5-base` | 278M · 768 · 512 | needs `query:` / `passage:` prefixes |
| `intfloat/multilingual-e5-large` | 560M · 1024 · 512 | straight upgrade on e5-base |
| `BAAI/bge-m3` | 568M · 1024 · 8192 | also yields sparse + ColBERT vectors |
| `Qwen/Qwen3-Embedding-0.6B` | 595M · 1024 · 32k | instruction-aware, top of MTEB multilingual |
| `google/embeddinggemma-300m` | 308M · 768 · 2048 | smallest of the modern multilingual set |
| `Alibaba-NLP/gte-multilingual-base` | 305M · 768 · 8192 | **broken on transformers 5.x** (see below) |
| `jinaai/jina-embeddings-v3` | 572M · 1024 · 8192 | query/passage LoRA; same remote-code risk |

The last two ship custom modelling code loaded with `trust_remote_code`. On
transformers 5.15 `gte-multilingual-base` crashes inside its shared
`Alibaba-NLP/new-impl` module (`rope_cos[position_ids]` indexes a RoPE table with
uninitialised ids) on both MPS and CPU. Verified on this machine, not assumed.
They stay in the catalog so a later transformers release can be retried.

Every family wants its input prepared differently, and getting this wrong costs
several points of recall silently. `MODEL_SPECS` in [embeddings.py](tam/retrieval/embeddings.py)
holds one entry per family — E5 prefixes both sides, Qwen3 puts an instruction on
the query only, EmbeddingGemma has its own two prompts, jina-v3 selects a LoRA
adapter by argument, BGE-M3 and GTE want the text untouched.

### Comparing models, transforms, and CSLS

```bash
python3 -m tam.evaluation.compare_models                       # 5 models × 5 post-processings + fusion
python3 -m tam.evaluation.compare_models --transforms none abtt --no-csls
python3 -m tam.evaluation.compare_models --models BAAI/bge-m3 intfloat/multilingual-e5-large
open output/model_comparison.html
```

Three knobs vary independently because they fix different things:

- **model** — what the text becomes.
- **space transform** — *anisotropy*. Raw transformer output sits in a narrow
  cone, so unrelated pairs already score ~0.8 and the ranking has little room
  left. `center` subtracts the corpus mean, `abtt` (all-but-the-top) also projects
  out the dominant principal components, `whiten` rescales every axis.
- **CSLS** — *hubness*. A few messages sit near everything and get retrieved for
  unrelated queries; CSLS subtracts each record's own neighbourhood density.

A **Reciprocal Rank Fusion** row merges the models' rankings. RRF combines
*ranks*, not scores, which matters because the models disagree on scale — e5 puts
everything in 0.80–1.00, so averaging raw cosine would let it dominate.

Any single run can also switch model without editing `.env`:

```bash
python3 -m tam.core --model intfloat/multilingual-e5-base -q "…"
python3 -m tam.evaluation.evaluate --model sentence-transformers/paraphrase-multilingual-mpnet-base-v2
```

Models are cached by Hugging Face under `~/.cache/huggingface/hub` — about 2.6 GB
for the two the defaults need, `paraphrase-multilingual-MiniLM-L12-v2` (458 MB)
plus the `bge-reranker-v2-m3` cross-encoder (2.1 GB, and only downloaded the first
time a run actually reranks). Every further entry in the catalog above adds
0.5–4.3 GB; the whole catalog measures 13 GB on this machine, so pull them one at
a time. Each model keeps its own embedding cache under
`data/processed/`, so switching re-downloads and re-embeds nothing. Set
`HF_HUB_OFFLINE=1` in `.env` once they are downloaded to skip Hugging Face
network checks entirely — measured 12.7s → 4.2s per run.

### Which corpus every number below came from

Read this once and the rest of the measurements stop being ambiguous.

Every measured table from here down was produced on **a private 33-message Slack
export with 4 hand-labelled queries** — `data/processed/messages.json` and
`data/eval_queries.json`. Both are gitignored, because both are real workspace
text, so **a clone cannot reproduce these tables**. They are quoted as what this
machine actually produced and nothing more.

The committed sample is a different, smaller corpus: 18 of 23 messages kept, 22
searchable records, 27 with the standup transcript merged in. Where the gap
changes a conclusion — fine-tuning pair counts, the relevance gate — both numbers
are given inline. Where it only changes digits, the number quoted is the private
export's.

Measuring anything yourself needs a label file, and only the example is committed
(`data/eval_queries*.json` is gitignored for the same reason):

```bash
cp data/eval_queries.example.json data/eval_queries.json   # then edit the ids
```

The example's ids point at the committed sample, so it works against
`data/processed/sample_messages.json` with no editing at all.

### Measured model comparison

Five models on the private 33-message export and its 4 labelled queries
(`python3 -m tam.evaluation.compare_models --transforms none abtt whiten --no-csls`):

| Model | dim | nDCG@10 | thread separation | score spread |
| --- | --- | --- | --- | --- |
| `paraphrase-multilingual-MiniLM-L12-v2` | 384 | 0.89 | 1.49 | 0.146 |
| `paraphrase-multilingual-mpnet-base-v2` | 768 | 0.89 | 1.25 | 0.144 |
| `intfloat/multilingual-e5-base` | 768 | 0.88 | 1.55 | 0.036 |
| `intfloat/multilingual-e5-large` | 1024 | 0.88 | **1.85** | 0.035 |
| `BAAI/bge-m3` | 1024 | 0.88 | 1.71 | 0.090 |
| **RRF fusion of all five** | — | 0.89 | n/a | n/a |

- *thread separation* = `(mean same-thread − mean cross-thread) / sd(cross-thread)`,
  over every pair. Computed on all 528 pairs of those 33 messages rather than on
  4 queries, so it is the trustworthy column.
- *score spread* = sd of every pairwise similarity. It measures how much of the
  0–1 range a model actually uses.

**The nDCG column separates nothing.** Four queries cannot: every model is inside
0.01 of every other, which is one message changing place. That is not a finding
about the models, it is a finding about the label set — see
[weak_labels.py](tam/evaluation/weak_labels.py).

**The separation column does separate them**, and it ranks `e5-large` (1.85) >
`bge-m3` (1.71) > `e5-base` (1.55) > MiniLM (1.49) > mpnet (1.25).

**The spread column is the trap.** Both e5 models sit at 0.035 — they compress
every pair into a band roughly 0.80–1.00 wide, so the ranking is decided in the
third decimal and no threshold can answer "is this related at all". MiniLM has
4× the range. A model can win on separation and still be the harder one to build
a product on.

### Do the space transforms help?

On this corpus, **no** — and the reason is worth stating:

| Variant | nDCG@10 | spread |
| --- | --- | --- |
| MiniLM, raw | **0.89** | 0.146 |
| MiniLM, abtt (drop 1) | 0.77 | 0.160 |
| MiniLM, whiten | 0.83 | **0.000** |
| e5-large, raw | **0.88** | 0.035 |
| e5-large, abtt (drop 1) | 0.85 | 0.144 |
| e5-large, whiten | 0.66 | 0.000 |

`whiten` drives the spread to exactly 0.000. With 33 messages and 384+ dimensions
the covariance is rank-deficient: every retained axis is scaled to the same
variance, the points end up equidistant on a sphere, and every pairwise
similarity collapses to one number. `fit_transform` now logs a warning when the
corpus is smaller than the vector width.

`abtt` behaves sensibly — it takes e5-large's spread from 0.035 to 0.144, a 4×
gain in resolution — but costs accuracy here because one dropped component is a
large fraction of the signal in a 33-message corpus. **Both transforms need a
real export before they can be judged.** They are kept, off by default.

Swap models with `EMBEDDING_MODEL` in `.env` or `--model` on any script. All
model access is behind `embed_texts()` in [embeddings.py](tam/retrieval/embeddings.py), so
moving to a hosted embedding API later means replacing that one function.

## How the embedding cache works

- Cache key is `sha256(message text)`. Text only — no user, timestamp, channel,
  or thread id ever reaches the model or the key.
- Vectors live in `data/processed/embeddings_<model-name>.npz`, one file per
  model, so switching models can never mix vector widths.
- On each run every record's hash is looked up; only new or edited text is
  embedded, and the cache file is rewritten with the additions.
- Unchanged messages therefore cost nothing on a rerun. Edited text produces a
  new hash and gets re-embedded; the stale entry is simply never read again.
- Queries you type are **not** cached (they are one-off, and caching them would
  grow the file for no benefit).
- `--no-cache` recomputes everything. Deleting the `.npz` file is always safe.

## Beyond cosine: the retrieval pipeline

Cosine is one kind of evidence. [retrieve.py](tam/retrieval/retrieve.py) composes several, and
`--preset` names each combination so the same pipeline can be searched with,
evaluated, and charted.

```bash
python3 -m tam.retrieval.retrieve --list-presets
python3 -m tam.retrieval.retrieve --records data/processed/sample_messages.json \
        -q "BE sorting API พร้อมแล้วหรือยัง" --preset full --explain
python3 -m tam.retrieval.retrieve --records data/processed/sample_messages.json \
        --related msg_C0SAMPLE01_1754000010.000100
```

| Preset | What it adds | Why |
| --- | --- | --- |
| `dense` | cosine only | the original PoC, the baseline to beat |
| `lexical` | BM25 only | shows what exact-term matching alone finds |
| `dense-abtt` / `dense-whiten` | space transform | fights anisotropy |
| `dense-csls` | hubness penalty | stops "any update?" ranking for everything |
| `hybrid` | dense + BM25, RRF | **the highest value per line of code** |
| `hybrid-anchors` | + shared ticket ids, identifiers, names | pins the same work item |
| `hybrid-jaccard` | + neighbourhood overlap rerank | uses shared context, not one score |
| `hybrid-rerank` | + cross-encoder over top 50 | **the biggest quality jump** |
| `full` | all of it | the ceiling, and the slowest |
| `related` | + thread / time / author | message → message, not text → message |

`--explain` prints each stage's contribution per hit, plus the BM25 terms and the
anchors that actually matched. The output of the `full` line above:

```text
1. 18.000
   BE sorting API พร้อมแล้ว ลอง integrate ได้เลยที่ /api/v1/candidates?sort=score
   user=U03BE  time=2025-08-01 05:21  thread=1754000500.000500  id=msg_C0SAMPLE01_1754000500.000500
   why: dense=1.00  bm25=1.00  anchors=1.00  cross=0.99
   matched terms: พร้อม, be, api, sorting, แล้ว
```

The components are per-stage, so a preset only reports the stages it runs: the
same query under `--preset hybrid` prints `why: dense=1.00  bm25=1.00` and
nothing else, because `hybrid` is `rrf[dense=1, bm25=1]` — check with
`--list-presets` before reading a component that cannot be there. The leading
number is also on a different scale between presets: rerank presets emit
RRF-with-rerank sums (18.000 here), plain fused presets emit RRF scores (0.033).

**Why each stage exists**

- **BM25** ([lexical.py](tam/retrieval/lexical.py)) catches what embeddings blur: `REV-1421`,
  `getUserProfile`, `Omega, Inc.`, error codes. These carry little semantic
  content but are near-proof of the same work item. Thai has no spaces between
  words, so tokenisation uses PyThaiNLP's `newmm` when installed and overlapping
  character n-grams otherwise.
- **Anchors** ([signals.py](tam/retrieval/signals.py)) are the same idea as a symmetric,
  IDF-weighted overlap rather than a query-document score.
- **Thread / time / author** ([signals.py](tam/retrieval/signals.py)) are the signals Slack
  hands over for free. Thread membership is *ground truth*, not a guess. Time
  matters because "fixed, deploying now" has no topic at all — its timestamp does.
- **Cross-encoder** ([rerank.py](tam/retrieval/rerank.py)) reads query and message together in
  one forward pass. It is the only stage that can tell "the sorting API is ready"
  from "waiting on the sorting API" — same words, opposite meaning, near-identical
  embeddings. It cannot be indexed, so it runs over the top 50 only.
- **Fusion** ([fusion.py](tam/retrieval/fusion.py)) is RRF by default because the stages score
  on wildly different scales. `zscore` keeps magnitudes when that is wanted.

**Some alternatives are not worth trying.** Vectors are L2-normalised, so
Euclidean distance and dot product are monotone transforms of cosine — they
produce *exactly* the same ranking. Changing the metric there is a no-op.

### Measured preset comparison

`python3 -m tam.evaluation.evaluate --presets dense hybrid hybrid-rerank full`, on the
private 33-message export and its 4 hand-labelled queries:

| Preset | nDCG@3 | nDCG@10 | MRR | MAP | worst first hit |
| --- | --- | --- | --- | --- | --- |
| `dense` | 0.88 | **0.89** | **1.00** | 0.85 | **1** |
| `hybrid` | **0.93** | **0.89** | **1.00** | **0.86** | **1** |
| `hybrid-rerank` | 0.81 | 0.85 | 0.83 | 0.85 | 3 |
| `full` | 0.81 | 0.85 | 0.83 | 0.85 | 3 |

**Adding BM25 helps at k=3** (0.88 → 0.93), which is the expected result: exact
terms sharpen the top of the list. **The cross-encoder makes it worse here**, and
that is not the usual outcome — on a 33-message corpus every candidate is already
in the top 50, so reranking cannot filter anything, it can only reorder, and one
query's answer slipping from rank 1 to rank 3 costs more than any gain. Reranking
earns its keep when there are thousands of candidates to cut down to fifty.

Both statements are one message changing place. Neither is evidence — and the
committed sample, which anyone can run, reverses the second one outright:

```text
python3 -m tam.evaluation.evaluate --records data/processed/sample_messages.json \
        --eval-file data/eval_queries.example.json --presets dense hybrid hybrid-rerank full

dense           nDCG@3 0.79   nDCG@10 0.84   MAP 0.80
hybrid          nDCG@3 0.76   nDCG@10 0.85   MAP 0.79
hybrid-rerank   nDCG@3 0.84   nDCG@10 0.89   MAP 0.86
full            nDCG@3 0.84   nDCG@10 0.89   MAP 0.86
```

Two corpora of about the same size, and they disagree about which preset wins.
That is what "too small to conclude anything" looks like from the inside, and it
is the point of the section below.

## Topics, not top-k

Ranking answers "what is closest to this". It cannot answer "what are the
distinct topics in this channel". That is a question about the whole set, and a
threshold on a pairwise score is a bad way to ask it.

```bash
python3 -m tam.analysis.graph --clusters data/processed/clusters.json
open output/graph.html
```

[graph.py](tam/analysis/graph.py) builds one sparse graph whose edges combine dense
similarity, thread membership, temporal proximity and shared anchors, then runs
Louvain community detection. On the private 33-message export — 33 nodes, 93
edges:

```text
  #  size  1-thread  label
  0     9      33%  usd, tim, sales cloud
  1     9      56%  omega, technology, svp
  2     8     100%  profile, android, service cloud
  3     7      43%  street, react, ramen

modularity 0.587    ARI 0.463  NMI 0.689 against Slack threads
```

Cluster names are the anchors the members share most distinctively — no model
wrote them. Modularity above 0.30 means real block structure. ARI and NMI score
the clustering against Slack threads, which are ground truth it was never shown.

## Relations that are not similarity

`sim(A, B) == sim(B, A)`, so cosine can say two messages are related and can never
say *how*. The relations a team needs are directed and typed:

```bash
python3 -m tam.analysis.relations --relations data/processed/relations.json
python3 -m tam.analysis.relations --method nli --typed-only
```

[relations.py](tam/analysis/relations.py) types every candidate pair as `resolves`,
`blocked_by`, `duplicates`, `answers`, `follows_up` or `same_topic`, with the
earlier message pointing at the later one — in a channel it is the later message
that answers or resolves.

- `--method rules` (default) uses cue phrases in Thai and English plus message
  order. No download, and every decision names the cue that produced it, so a
  wrong relation is fixable rather than mysterious.
- `--method nli` scores each relation as a hypothesis about the pair with a
  multilingual NLI cross-encoder. It catches paraphrases the cue lists miss, and
  explains nothing beyond a number.

## Fine-tuning on your own threads

The only change here with no ceiling. Swapping encoders buys a few points; a
model that has *seen* how this channel says "the sorting API" learns something no
public checkpoint contains.

```bash
python3 -m tam.evaluation.finetune --dry-run          # how many pairs the corpus yields
python3 -m tam.evaluation.finetune --epochs 2
python3 -m tam.evaluation.evaluate --model models/finetuned --presets dense hybrid
```

The training signal is already in the export: two messages in one thread are a
positive pair, and the rest of the batch supplies the negatives
(`MultipleNegativesRankingLoss`). No hand labelling. The held-out split is by
**thread**, never by message — splitting by message would put one half of a
conversation in train and the other in test.

It refuses to run below 200 pairs, and neither small corpus here comes close: the
committed sample yields 8 pairs from 4 threads, and the private 33-message export
45 pairs from 5 threads (42 after the held-out split). Under 200 it fits noise
instead of learning, and that refusal is the point — the measured run below used a
927-message chat, which is what it takes.

### Measured: it generalises, but check on held-out threads

On a 927-message Thai work chat (2984 pairs, 2 epochs, batch 32, ~1 min on a
laptop CPU), against thread separation — the metric computed over *every* pair
rather than a few queries.

That chat is committed as `data/sample/synthetic_work_chat.json` (1000 synthetic
rows, no real workspace content), so this table is reproducible from a clone:

```bash
python3 -m tam.ingest.prepare_messages --raw data/sample/synthetic_work_chat.json \
        --out data/processed/syn.json      # Kept 927/1000, 77 threads
python3 -m tam.evaluation.finetune --records data/processed/syn.json --epochs 2
```

| | all pairs | held-out threads only |
| --- | --- | --- |
| `paraphrase-multilingual-MiniLM-L12-v2` | 0.11 | 0.06 |
| the same model, fine-tuned | **0.64** | **0.33** |

Read the second column, not the first. The all-pairs figure includes the threads
the model trained on, and roughly half the headline gain is memorisation of
those. On the 11 threads it never saw, separation still rose 0.06 → 0.33 — the
model learned what "same work item" looks like in this team's vocabulary, not
just which sentences it had been shown. That gap is the whole reason
`split_by_thread` exists; a fine-tune reported on all pairs will always look
better than it is.

## The standup prototype

A meeting is not a second pipeline. `meetings.py` turns a transcript into the
**same record shape** `prepare_messages.py` emits — one utterance per record, the
whole meeting as one `thread_ts` — and from there every module above works
untouched: the meeting is clustered, searched, and related to Slack with no new
code. A meeting is a conversation that happened out loud.

`--merge-into` merges into a corpus that already exists, so the Slack side is
prepared first — otherwise the "combined" file is a meeting on its own:

```bash
python3 -m tam.ingest.prepare_messages --raw data/sample/slack_messages.sample.json \
                    --out data/processed/sample_combined.json
python3 -m tam.ingest.meetings --transcript data/sample/standup.vtt --title "Daily standup" \
                    --started 2026-08-14T09:30 --merge-into data/processed/sample_combined.json

python3 -m tam.analysis.digest    --records data/processed/sample_combined.json --days 3650  # what moved
python3 -m tam.analysis.digest    --records data/processed/sample_combined.json --blockers   # what is stuck
python3 -m tam.analysis.summarize --records data/processed/sample_combined.json --days 3650  # in prose
python3 -m tam.web.server         --records data/processed/sample_combined.json --days 3650  # the web app
```

That corpus is 27 searchable records and produces 5 topics, 1 of them blocked,
and one work item whose sources are `slack` *and* `meeting` — which is the whole
claim of this section, reproducible from a clone. (`--days 3` works too, but the
committed Slack export is a year old, so a 3-day window shows the meeting alone.)

The payoff is that a work item spans both sources. On this machine's larger
private corpus — the 33-message export with the same transcript merged in — the
Android Profile bug clusters as **8 Slack messages + 2 meeting utterances**, and
its timeline reads:

```text
c30a929 · #2 profile, android, service cloud   [resolved]
resolved on 2026-08-14 16:30 — cue "เสร็จแล้ว"

  2026-08-13 21:22  resolves   [slack]   "…I found a bug in the Profile module…"
  2026-08-14 16:30  resolves   [meeting] "ผม debug แล้วครับ … fix เสร็จแล้ว รอ patch ขึ้น release"
```

Both rows are `resolves`, and that is deliberate. An earlier version of this paste
showed a `follows_up` chase between them, on the words "ตอนนี้สถานะเป็นยังไง". The
cue list behind it was bare high-frequency words — `still`, `status`, `eta`, Thai
`ยังไง` ("how") — which typed ordinary narration as a status chase; every
`follows_up` this corpus produced came from one standup transcript. The cues are
now chase phrases with a 48-character window, and this corpus produces none. Fewer
rows that are all true beats more rows where one is invented.

**One tuning note that came out of this.** A Slack thread is *one* topic — people
open a new thread for a new subject. A meeting is deliberately *several*: one
standup covers the Android bug, the Omega deal and Q4 reviews in fifteen minutes,
and they all share a `thread_ts`. Weighting a meeting's thread like a Slack
thread collapsed the whole meeting into one cluster, so `EdgeWeights` carries a
separate, much smaller `meeting_thread` weight and meeting utterances lean on
wording, anchors and adjacency in time instead.

### What is derived and what is generated

Everything factual — which work item, who is on it, whether it is blocked, and
the message that proves it — is computed by `digest.py` from the typed relations.
`summarize.py` only writes the sentence. A wrong adjective is cosmetic; a wrong
*state* is a bad standup, so the state never passes through a model.

```bash
SUMMARIZER=template python3 -m tam.analysis.summarize   # default: no model, no network
SUMMARIZER=claude   python3 -m tam.analysis.summarize   # Anthropic API writes the prose
```

The backend switches on an env var exactly like `EMBEDDING_MODEL`, and
`template` is the default so the promise at the top of this README still holds
unless you opt out of it. Whichever backend runs, **every summary must cite the
message ids it used and the citations are verified in code** — an id that is not
in that work item is dropped, and a summary left with none is flagged
`unverified` in the UI. A model can write a confident sentence about a message
that does not exist; it cannot fake an id that is in the corpus.

## The dashboard

`tam/web/server.py` is a FastAPI app that serves both HTML and JSON from the same
computed digest — the HTML is generated in Python, so there is no build step, no
`node_modules`, and no separate frontend to keep in sync.

```bash
python3 -m tam.web.server --records data/processed/sample_combined.json --days 3650 --port 8899
```

It prints what it built before it serves anything, which is the fastest way to
tell whether your corpus is actually loaded. On the committed sample:

```text
INFO Building index from data/processed/sample_combined.json
INFO Reused 27 cached embedding(s) from data/processed/embeddings_….npz
INFO Ready: 27 record(s), 5 topic(s), 1 blocked, summariser template
```

It also prints the token every write route needs (see `POST /api/reindex` below);
set `TAM_ADMIN_TOKEN` to keep the same one across restarts.

The bind is loopback only. `--host` with anything non-loopback refuses to start
unless you add `--expose`, and `--expose` refuses without `TAM_ADMIN_TOKEN` set —
`/upload` and `/api/reindex` write the corpus, so reaching them from the network
should be a decision, and the token that guards them should be one you chose
rather than one printed at startup.

| Page | Shows |
| --- | --- |
| `/` | The digest: what moved, per work item, most recent first |
| `/blockers` | Only what is stuck, with the message that proves it |
| `/item/{key}` | One work item — full timeline across Slack and meetings. `{key}` is the stable `item_id` (a ticket key, or `c30a929`); the cluster rank still resolves but is not stable across rebuilds |
| `/search` | Ground a note: paste a sentence, get the messages behind it |
| `/upload` | Drop a `.vtt` / `.srt` transcript and merge it into the corpus |

The same data is available as JSON, so the bot or any other client can read it
without scraping HTML:

```bash
curl localhost:8899/api/digest          # every work item, with evidence ids
curl localhost:8899/api/blockers
curl localhost:8899/api/item/c30a929    # {key} is the stable item_id from /api/digest
curl localhost:8899/api/item/1          # the cluster rank still works, but names a
                                        # different item after the next rebuild
curl "localhost:8899/api/search?q=Android&k=10"
curl "localhost:8899/api/search?q=Android&k=1&preset=dense"   # raw cosine, the only calibrated number
curl localhost:8899/api/health

# the one write route: re-read records + rebuild, no restart. It needs the token
# the server printed at startup, because it changes what everyone else is reading.
curl -X POST -H "X-TAM-Token: $TAM_ADMIN_TOKEN" localhost:8899/api/reindex
```

Startup cost is embedding the corpus. It reuses `data/processed/embeddings_*.npz`
when the model and texts match, so a second start is seconds rather than minutes
— see [How the embedding cache works](#how-the-embedding-cache-works).

### Pointing it at real data

There is nothing to configure. `--records` takes any file
`tam.ingest.prepare_messages` or `tam.ingest.meetings` produced:

```bash
# Slack only
python3 -m tam.ingest.export_slack
python3 -m tam.ingest.prepare_messages
python3 -m tam.web.server --records data/processed/messages.json

# Slack + meetings in one corpus, which is what the digest is for. The meeting
# merges into the file prepare_messages just wrote — --merge-into needs it to exist.
python3 -m tam.ingest.meetings --transcript standup.vtt --title "Daily standup" \
                    --started 2026-08-14T09:30 --merge-into data/processed/messages.json
python3 -m tam.web.server --records data/processed/messages.json

# Every Slack refresh after that merges too, or it would drop the meetings:
python3 -m tam.ingest.export_slack
python3 -m tam.ingest.prepare_messages --merge-into data/processed/messages.json
```

With no Slack access at all, the committed sample runs the whole thing — see
[Try it without Slack credentials](#try-it-without-slack-credentials).

## The Slack bot

[../slack-bot/](../slack-bot/) is where the pipeline's ideas meet the place work
is actually discussed. It is a Bolt app in **Socket Mode**, which matters
practically: no public URL, no ngrok, no inbound firewall rule. It dials out to
Slack, so it runs from a laptop.

```bash
cd ../slack-bot
cp .env.example .env     # SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_SIGNING_SECRET
npm install
npm start
```

Create the app by pasting
[../slack-bot/slack-app-manifest.yaml](../slack-bot/slack-app-manifest.yaml) into
api.slack.com/apps → **From an app manifest**. That sets every scope, command and
shortcut at once; a missing scope surfaces as a confusing runtime error hours
later, so do not hand-configure them.

### What it does in Slack

| In Slack | What happens |
| --- | --- |
| `/meowtam` or `/mt` | The board — every work item, blocked first |
| `/meowtam blocked` | Only what is stuck |
| `/meowtam digest` | The standup digest, on demand |
| `/meowtam recall <ข้อความ>` | Search, including Thai, plus the decision chain behind a topic |
| `/meowtam MOB-142` | One work item by key — `MOB-142` exists in the committed ledger; with `TAM_API_URL` set the keys are `TAM-0`…`TAM-4` |
| `/meowtam @someone` | What that person is on |
| Message shortcut **ผูกกับ ticket** | Attach any message to a work item |
| Message shortcut **บันทึกเป็นการตัดสินใจ** | File it in the decision log, findable via `recall` |
| Reaction on a message | `reaction_added` is an input — emoji as a command |
| Scheduled | 08:45 standup DM, 09:25 channel digest — **off unless `ENABLE_SCHEDULE=1`** |

The schedules are off by default on purpose: a stray `npm start` should never
post into a real channel.

### Running it on your real channel

```bash
# EXPORT_CHANNELS=C0DEMOCHAN1,… and DIGEST_CHANNEL in .env, then:
npm run export     # channel history via the bot token you already have
npm run ledger     # → data/ledger.json: work items, states, evidence
```

`npm run ledger` reports an unassigned rate; under 25% is healthy.
`/meowtam reload` re-reads the ledger without a restart. Remember `/invite
@Meowtam` — a bot token cannot read a channel it is not in.

[../slack-bot/README.md](../slack-bot/README.md) has the rest: the demo
choreography, which parts are real versus mocked, and the design rules.

### Reading from the pipeline

Two processes that both read Slack will eventually disagree about what a work
item is, and the one the team sees is then a coin flip. `TAM_API_URL` makes the
Python side the only owner of that definition:

```bash
python3 -m tam.web.server --records data/processed/sample_combined.json --days 3650 --port 8899
cd ../slack-bot
TAM_API_URL=http://127.0.0.1:8899 npm run check-api    # prove it, no Slack needed
TAM_API_URL=http://127.0.0.1:8899 npm start
```

The bot then prints its source at boot and on `/meowtam reload`, so nobody has to
guess which half answered.

| Comes from the API | Filled in on the bot's side |
| --- | --- |
| work items, states, evidence, ages | decisions and their supersession chains |
| timelines, messages, summaries | standup drafts |
| recall (embeddings + BM25 + signals) | drift detections and proposed diffs |

The right-hand column has no counterpart in the pipeline yet, and each of the
three is filled from a different place — none of them from the fixture:

- **decisions** are read from the append-only file people write through the
  message shortcut (`TAM_DECISIONS_PATH`, default `data/decisions.json`), merged
  with any the ledger already carried. On a fresh clone the list is empty until
  somebody files one; `check-api` prints the count.
- **standup drafts** are *computed* from the items in hand, so the 08:45 DM and
  the 09:25 digest cannot disagree about the same work item.
- **drift** has no live source at all — it needs a ticket system to compare Slack
  against, and nothing is connected, so it is empty. The fixture's example loads
  only under `DEMO_FIXTURES=1`, and the renderer says on screen that it is one.

Two translations happen in [tam-api.ts](../slack-bot/src/tam-api.ts), and both
are worth knowing about because they are the bot adding something the pipeline
did not say:

- **The state names differ.** The pipeline has `active`, `blocked`, `resolved`;
  the board has `blocked`, `stalled`, `moving`, `done`, in that order. The mapping
  is `blocked → blocked`, `resolved → done`, `active → moving`, and `active` whose
  last activity is older than `TAM_STALE_DAYS` → `stalled`. Only `stalled` is new
  information; the other two are renames, which is why `/api/digest` and the board
  can look like they disagree when they do not.
- **Evidence for an active item.** The pipeline only writes an evidence sentence
  for a state *change*. Rather than render a blank claim, active items are
  anchored on their newest message, which keeps the "every claim is clickable"
  rule intact.

Permalinks are rebuilt from message ids (`msg_<channel>_<ts>` → an
`/archives/…` link), so evidence buttons work even though the pipeline does not
store them. Meeting utterances have no Slack message and correctly get none.

## Evaluation

```bash
# evaluate defaults to data/eval_queries.json, which is gitignored — copy the
# committed example, whose ids already match the committed sample:
cp data/eval_queries.example.json data/eval_queries.json
python3 -m tam.evaluation.evaluate --records data/processed/sample_messages.json \
        --presets dense hybrid hybrid-rerank full
python3 -m tam.evaluation.evaluate --eval-file data/eval_queries.weak.json --per-query
```

Four metrics, because Recall@K alone hides where in the list the answer landed:

| Metric | Answers | Reaches 1.00? |
| --- | --- | --- |
| **Recall@K** | did we find them at all | no — capped by label count |
| **nDCG@K** | did we find them *near the top* | yes — normalised per query |
| **MRR** | how far do they scroll before something useful | yes |
| **MAP** | did we find *all* of them, or just one | yes |

**Compare on nDCG.** Recall treats "answer at rank 1" and "answer at rank 9" as
identical; nDCG does not, and it is normalised against a perfect ordering of the
same labels, so a low K is not automatically capped. Recall@1 cannot exceed
`1 / len(relevant_ids)` — the table prints the ceiling row so this cannot be
misread.

### The measurement problem, and the fix

Four hand-labelled queries over 33 messages cannot separate two pipelines: one
message changing place moves Recall@1 by 0.25. `evaluate.py` prints a warning
saying so whenever there are fewer than 20 queries.

```bash
python3 -m tam.evaluation.weak_labels                 # threads → a labelled set, free
python3 -m tam.evaluation.evaluate --eval-file data/eval_queries.weak.json --presets dense hybrid
```

[weak_labels.py](tam/evaluation/weak_labels.py) takes the labels Slack already contains: one
message from a thread becomes the query, its thread-mates become the relevant
set, and the message itself is excluded from its own ranking. It scales with the
export and regenerates whenever the corpus does — which is also its limit: a small
export has few threads, so it produces few cases and `evaluate.py` still warns that
there are too few. The remedy is a larger export, not another pass of this tool.

It measures "given one message, can the pipeline find the rest of its
conversation" — a proxy, easier than a real query in vocabulary and harder in
specificity. **Read weak-label numbers as relative**, and keep a small
hand-labelled set alongside to check the two agree on which pipeline wins.

## Charts

Numbers like `Recall@1 = 0.44` are hard to read. `visualize.py` renders the same
results as a self-contained HTML page (Plotly is inlined, so it works offline):

```bash
python3 -m tam.report.visualize
python3 -m tam.report.visualize --query "เปลี่ยนชื่อไฟล์ตอน export" --top-k 10
open output/report.html
```

Four views, in the order they answer the question:

1. **Top matches** — one bar per message, length = cosine to the query, hover for
   the full text, plus a table view.
2. **Recall by query** — a query × K grid; 1.00 means every labelled message was found.
3. **Message map** — embeddings flattened to 2-D, coloured by similarity to the
   query. A sketch only; the axis labels show how little variance survives.
4. **Same-thread vs different-thread similarity** — the sanity check. Messages in
   one thread share a topic by definition, so their scores should sit to the right
   of unrelated pairs. On real Thai chat data they do **not** (see limitations).

### Plain-Thai version

`visualize.py` reports metrics; `report_th.py` reports the same run in Thai, in
counts rather than ratios — "เจอ 7 จาก 10 ข้อความที่ควรเจอ" instead of
`Recall@10 = 0.70` — for teammates who do not work with retrieval metrics:

```bash
python3 -m tam.report.report_th
open output/report_th.html
```

It shows one real search with each hit marked ✓ / ○ against the labels, a
found-vs-missed bar per query, and how much deeper results help. Note it counts
**micro** (total found ÷ total labelled) while `evaluate.py` reports the **macro**
mean of per-query recall, so the two differ on the same run: on the private
33-message export `report_th` renders `เจอข้อความที่ควรเจอ 16 จาก 19` (84%) where
`evaluate --presets dense` reports R@10 0.88. Same hits, two averages — a query
with 8 labels pulls the micro figure around and the macro one not at all.

## Files

| File | Role |
| --- | --- |
| [export_slack.py](tam/ingest/export_slack.py) | Paginated `conversations.history` + `conversations.replies`, 429-aware |
| [prepare_messages.py](tam/ingest/prepare_messages.py) | Slack markup cleanup, noise filtering, message + thread records |
| **Retrieval** | |
| [embeddings.py](tam/retrieval/embeddings.py) | `embed_texts()`, per-family prefixes, SHA-256 cache, space transforms, CSLS |
| [lexical.py](tam/retrieval/lexical.py) | BM25 with a Thai-aware tokenizer, and `matched_terms` for explanations |
| [signals.py](tam/retrieval/signals.py) | Anchors (ticket ids, identifiers, names) + thread / time / author signals |
| [fusion.py](tam/retrieval/fusion.py) | RRF, z-score fusion, neighbourhood (k-reciprocal) rerank |
| [rerank.py](tam/retrieval/rerank.py) | Cross-encoder reranking over the top candidates |
| [retrieve.py](tam/retrieval/retrieve.py) | **The pipeline.** Composes every stage; `--preset`, `--explain`, `--related` |
| [core.py](tam/core.py) | The original dense-only search; still the simplest entry point |
| **Relations** | |
| [graph.py](tam/analysis/graph.py) | Message graph + Louvain communities, scored against threads with ARI/NMI |
| [relations.py](tam/analysis/relations.py) | Typed directed relations, by cue rules or by NLI |
| **Measurement** | |
| [evaluate.py](tam/evaluation/evaluate.py) | Recall / nDCG / MRR / MAP over presets |
| [weak_labels.py](tam/evaluation/weak_labels.py) | Turns threads into a labelled eval set, no hand labelling |
| [compare_models.py](tam/evaluation/compare_models.py) | Models × space transforms × CSLS + Reciprocal Rank Fusion |
| [finetune.py](tam/evaluation/finetune.py) | Contrastive fine-tune on same-thread pairs |
| **Standup / meetings** | |
| [meetings.py](tam/ingest/meetings.py) | Transcript (VTT / SRT / `Name:` lines / JSON) → the same records Slack produces |
| [digest.py](tam/analysis/digest.py) | Work items, blocked/resolved state, blockers, timelines — all derived, no LLM |
| [summarize.py](tam/analysis/summarize.py) | Prose for a work item; `SUMMARIZER=template` (offline) or `claude` |
| [server.py](tam/web/server.py) | FastAPI prototype: digest, blockers, item timeline, grounding search, upload |
| **Reports** | |
| [visualize.py](tam/report/visualize.py) | Plotly HTML report of a search run, into `output/` |
| [report_th.py](tam/report/report_th.py) | Plain-Thai version of the results, for non-ML readers |
| `data/sample/slack_messages.sample.json` · `standup.vtt` | Committed Thai/English sample export and transcript — the offline quickstart |
| `data/sample/synthetic_work_chat.json` | 1000 synthetic rows (927 kept, 77 threads) — the only committed corpus big enough for fine-tuning's 200-pair floor |
| **Slack bot (TypeScript)** | |
| [app.ts](../slack-bot/src/app.ts) | Bolt app: slash commands, shortcuts, modals, scheduled digest |
| [src/blocks/](../slack-bot/src/blocks/) | Block Kit builders per surface (digest, item card, drift, recall) |
| [src/search.ts](../slack-bot/src/search.ts) | Recall: trigram + literal-term hybrid, Thai-safe, no API key |
| [scripts/](../slack-bot/scripts/) | `export-slack.ts` → `raw-slack.json` → `build-ledger.ts` → `data/ledger.json` |
| [slack-app-manifest.yaml](../slack-bot/slack-app-manifest.yaml) | Every scope, command and shortcut in one paste |
| **Config** | |
| [.env.example](.env.example) | Every variable the Python side reads, and what it costs you |
| [../slack-bot/.env.example](../slack-bot/.env.example) | Every variable the bot reads, and what each one changes |
| [slack-app-manifest.json](slack-app-manifest.json) | Read-only Slack app for the exporter — history scopes only |

## Known limitations

- **Recall's relevance gate depends on the served model.** The hybrid score is
  rank-derived (RRF), so it cannot express "nothing matched" *at all*: the top hit
  scores `1/61 + 1/61 = 0.0328` for a real question and for
  `qqqzzzxxx wvwvwv jjjkkk zzzqqq` alike, because rank 1 in both stages is rank 1
  in both stages whatever the query was. Recall therefore gates on a raw cosine
  from the `dense` preset (`/api/search?preset=dense&k=1`), and that only works if
  the model puts gibberish far from the corpus. Measured against the same
  gibberish string `check-api` uses, on the 42-record Slack+meeting corpus, gate at
  its default `TAM_MIN_COSINE=0.45`:

  | model | gibberish | `Android Profile bug fixed?` | separable at 0.45 |
  | --- | --- | --- | --- |
  | `paraphrase-multilingual-MiniLM-L12-v2` | 0.388 | 0.847 | yes |
  | `models/syn_finetuned` | **0.738** | 0.838 | no |

  The fine-tuned model pulled everything together, gibberish included: 0.10 apart
  with the floor below both, so no threshold survives.

  The single-probe number above is also too kind to the general model. `check-api`
  scores three gibberish strings and several real queries, and the Thai one is the
  one that hurts: on this same 42-record corpus `ฟฟฟกกก ผผผ ฃฃฃ ฅฅฅ` reaches 0.581
  while the weakest genuine query — an item's own label, `street, sales dashboard,
  react` — reaches only 0.473. They overlap, so **no floor separates them on this
  corpus either**, and 0.45 lets that one probe through. A floor has to clear the
  weakest real question, not the strongest, which is why the check measures both
  and refuses to suggest a number when they cross. It is a property of the served
  model, not of the wiring, and the fix is a better model, never a higher
  `TAM_MIN_COSINE`.
- **Brute-force search.** Every query scores every record with a NumPy dot
  product. Fine for thousands of messages, not for millions — that is what a
  vector database would be for later.
- **Token truncation.** The default model truncates at 128 word pieces, so long
  concatenated thread records are only partly represented. Thread records also
  cap at 20 replies (`MAX_THREAD_REPLIES`).
- **Thread records duplicate their messages.** They are excluded from search by
  default; `--include-threads` turns them on and results then contain both the
  individual message and the thread that contains it.
- **Slack rate limits.** Apps created after 2025-05-29 that are not Marketplace
  apps get a much stricter `conversations.history` tier (roughly 1 request per
  minute, ~15 messages per request). Exporting 200 messages can then take
  several minutes; the script sleeps for `Retry-After` and keeps going.
- **User ids are not resolved.** Results show `U01FE`, not a display name; that
  needs `users:read` and a lookup table.
- **Noise filtering is a word list.** `NOISE_WORDS` in `prepare_messages.py`
  drops standalone acknowledgements (`ครับ`, `โอเค`, `ok`, `555`, emoji-only). Anything
  matching `TECHNICAL_RE` is always kept, so `BE pending` and `deploy UAT แล้ว`
  survive. It is deliberately conservative and does not detect sarcasm, quotes,
  or long off-topic chat.
- **No reactions, files, attachments, or edit history** are exported.
- **Cosine scores are not calibrated.** 0.6 in one channel is not comparable to
  0.6 in another; treat the ranking as the signal, not the absolute number.
- **The corpus and the label set are both too small to conclude anything about
  models.** 33 messages and 4 labelled queries in the private export; 18 messages
  and the same 4 in the committed sample. Every pipeline lands within noise of
  every other on nDCG, the space transforms are actively degenerate at this size,
  and the two corpora do not even agree on which preset wins. Nothing in the
  measured tables above should be quoted as a general result; they are quoted here
  as what this machine actually produced, on the corpus each table names.
- **On short Thai chat messages the model matches register, not topic.** This is
  the biggest finding from running on a real Thai dev channel (53 messages).
  Same-thread pairs scored mean 0.275 / median 0.248; different-thread pairs
  scored 0.257 / 0.245 — no useful separation, even though same-thread messages
  are the same topic by definition. Examples: `ให้ FE ช่วยเปลี่ยนชื้อได้ไหม` ↔
  `เปลี่ยนชื่อที่ไฟล์ได้ แต่เหมือนจะมีรูป…` (same thread, same topic) scored **0.06**,
  while `เส้นไหนนะครับ` ↔ `เส้นนี้ๆ` (different threads, unrelated) scored **0.86**.
  Short Thai messages full of particles look alike to this model regardless of
  meaning. Retrieval still works when the query is descriptive and the target
  message carries content — first-hit-rank was 1 for all 5 labelled queries, and
  one query pulled related messages from three threads a month apart. What to try
  next: a stronger multilingual model, embedding thread context instead of lone
  short replies, or dropping very short non-technical messages from the corpus.
- **Deictic messages are unreachable by design.** `ตรงนี้ใครต้องเปนคนแก้นะคะ`,
  `เส้นนี้ๆ`, `ของพี่ไม่เป็นนะ` carry their meaning in a screenshot or in the reader's
  head, not in text. No embedding can retrieve them from a topical query; they were
  deliberately left out of the labelled sets.
- **Cross-lingual pairs score lower than same-language pairs.** Measured on the
  committed sample, and the paste under [Run](#run) is this: the query
  `FE sorting เสร็จแล้วแต่ยังรอ BE API` scored the English `FE done, waiting for
  API` at 0.80 and put it first, while its closest Thai paraphrase scored 0.31 and
  landed last — barely inside the default top 10.
  Retrieval across languages works, yet an English near-match can outrank a Thai
  exact-match. If the real data is Thai-heavy, compare
  `paraphrase-multilingual-mpnet-base-v2` with `evaluate.py` before trusting the order.
- **Single channel per export.** Re-running overwrites
  `data/raw/slack_messages.json` — the *export* has no incremental mode. The
  *prepare* step does: `prepare_messages --merge-into` unions by record id, and
  plain `--out` now refuses rather than overwrite a corpus holding meeting records.
