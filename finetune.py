"""Fine-tune the encoder on this channel's own threads.

Every other module works with a model trained on the general web. This one adapts
it to a specific team's vocabulary, and it is the only change here with no
ceiling: swapping encoders buys a few points, but a model that has *seen* how
this channel talks about "the sorting API" learns something no public checkpoint
contains.

The training signal is free and already in the export. Two messages in one thread
are about the same work item, so they are a positive pair:

    anchor   = one message
    positive = another message from the same thread
    negatives = every other message in the batch (in-batch negatives)

That is MultipleNegativesRankingLoss, the standard contrastive objective for
retrieval. It needs no negative mining and no hand labelling — the batch supplies
the negatives, and the loss pushes the anchor towards its thread-mate and away
from everything else at once.

Two honest limits:

* **Size.** Below a few hundred pairs this overfits rather than learns; the run
  refuses by default under `--min-pairs`. A single Slack channel usually needs
  months of history, or several channels, to be worth training on.
* **Evaluation.** The held-out split is by *thread*, never by message. Splitting
  by message would put one half of a conversation in train and the other in test,
  and the score would be meaningless.

    python3 finetune.py --dry-run          # how many pairs the corpus yields
    python3 finetune.py --epochs 2
    python3 evaluate.py --model models/finetuned --presets dense hybrid
"""

from __future__ import annotations

import argparse
import logging
import random
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv

from embeddings import model_name, model_spec, prepared_text, quiet_third_party_logs, set_model
from semantic_search import DEFAULT_RECORDS, load_records
from weak_labels import thread_groups

DEFAULT_OUTPUT = Path("models/finetuned")
# Below this the run is fitting noise. Not a hard law, but the point where a
# held-out score stops meaning anything on chat data.
MIN_PAIRS = 200
HOLDOUT_FRACTION = 0.15
SEED = 7

log = logging.getLogger("finetune")


def build_pairs(records: Sequence[dict[str, Any]], *, max_per_thread: int = 20) -> list[tuple[str, str, str]]:
    """(thread, anchor, positive) for every ordered pair inside a thread.

    The thread id travels with the pair so the split can be made by thread. Long
    threads are capped: a 60-message thread would otherwise contribute 1770 pairs
    and drown every other conversation in the channel.
    """
    pairs: list[tuple[str, str, str]] = []
    for thread, members in thread_groups(list(records)).items():
        texts = [str(record["text"]) for record in members]
        if len(texts) < 2:
            continue
        if len(texts) > max_per_thread:
            texts = texts[:max_per_thread]
        for position, anchor in enumerate(texts):
            for positive in texts[position + 1 :]:
                pairs.append((thread, anchor, positive))
    return pairs


def split_by_thread(
    pairs: Sequence[tuple[str, str, str]], fraction: float = HOLDOUT_FRACTION
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Hold out whole threads, never individual pairs.

    Splitting by pair would leave the same conversation on both sides, and the
    model would be scored on messages it had already been shown.
    """
    threads = sorted({thread for thread, _, _ in pairs})
    random.Random(SEED).shuffle(threads)
    held = set(threads[: max(1, int(len(threads) * fraction))]) if len(threads) > 1 else set()
    train = [pair for pair in pairs if pair[0] not in held]
    test = [pair for pair in pairs if pair[0] in held]
    return train, test


def to_examples(pairs: Sequence[tuple[str, str, str]]) -> Any:
    """Pairs as a Dataset, with the model's own prefixes applied.

    The prefixes matter: E5 and Qwen are trained with them, so training without
    them and searching with them would teach the model one convention and query it
    with another.
    """
    from datasets import Dataset

    spec = model_spec()
    if spec.query_kwargs or spec.passage_kwargs:
        log.warning(
            "%s selects its task by an encode argument, which this trainer does not pass. "
            "Fine-tune a prefix-based model instead.", model_name()
        )
    return Dataset.from_dict(
        {
            "anchor": [prepared_text(anchor, "query") for _, anchor, _ in pairs],
            "positive": [prepared_text(positive, "passage") for _, _, positive in pairs],
        }
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS, help=f"Prepared records (default {DEFAULT_RECORDS})")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT, help=f"Where to save the model (default {DEFAULT_OUTPUT})")
    parser.add_argument("--model", help="Base model to fine-tune; overrides EMBEDDING_MODEL")
    parser.add_argument("--epochs", type=int, default=2, help="Training epochs (default 2)")
    parser.add_argument("--batch-size", type=int, default=16, help="Batch size — also the number of in-batch negatives (default 16)")
    parser.add_argument("--learning-rate", type=float, default=2e-5, help="Learning rate (default 2e-5)")
    parser.add_argument("--max-per-thread", type=int, default=20, help="Messages used per thread (default 20)")
    parser.add_argument("--min-pairs", type=int, default=MIN_PAIRS, help=f"Refuse to train below this many pairs (default {MIN_PAIRS})")
    parser.add_argument("--dry-run", action="store_true", help="Report the pairs the corpus yields and stop")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    quiet_third_party_logs()
    load_dotenv()
    args = parse_args()
    set_model(args.model)

    records = load_records(args.records, include_threads=False)
    pairs = build_pairs(records, max_per_thread=args.max_per_thread)
    train_pairs, test_pairs = split_by_thread(pairs)
    threads = len({thread for thread, _, _ in pairs})

    print(f"\n{len(pairs)} training pair(s) from {threads} thread(s) over {len(records)} message(s)")
    print(f"  train {len(train_pairs)} pair(s) · held out {len(test_pairs)} pair(s), split by thread")
    print(f"  base model {model_name()}")

    if args.dry_run:
        print("\nDry run, nothing trained.")
        return
    if len(train_pairs) < args.min_pairs:
        raise SystemExit(
            f"\nOnly {len(train_pairs)} training pair(s); below {args.min_pairs} this overfits instead of "
            "learning. Export more history or more channels, then run again. "
            "Override with --min-pairs if you want to see it run anyway."
        )

    from sentence_transformers import SentenceTransformer, SentenceTransformerTrainer, SentenceTransformerTrainingArguments
    from sentence_transformers.losses import MultipleNegativesRankingLoss

    spec = model_spec()
    model = SentenceTransformer(model_name(), trust_remote_code=spec.trust_remote_code)
    training_args = SentenceTransformerTrainingArguments(
        output_dir=str(args.out / "checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        warmup_steps=0.1,  # a float here means a ratio; warmup_ratio is gone in Transformers v5
        logging_steps=20,
        save_strategy="no",  # only the final model is wanted; checkpoints are large
        report_to=[],
    )
    trainer = SentenceTransformerTrainer(
        model=model,
        args=training_args,
        train_dataset=to_examples(train_pairs),
        eval_dataset=to_examples(test_pairs) if test_pairs else None,
        loss=MultipleNegativesRankingLoss(model),
    )
    trainer.train()

    args.out.mkdir(parents=True, exist_ok=True)
    model.save(str(args.out))
    log.info("Saved the fine-tuned model to %s", args.out)
    print(f"\nCompare it against the base model on the same labels:")
    print(f"  python3 compare_models.py --models {model_name()} {args.out}")
    print(f"  python3 evaluate.py --model {args.out} --presets dense hybrid")
    print("\nIf it does not beat the base model, the corpus was too small — that is the usual outcome")
    print("on one channel, and it is information, not a failure.")


if __name__ == "__main__":
    main()
