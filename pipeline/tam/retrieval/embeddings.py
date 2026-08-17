"""Local multilingual embeddings with a SHA-256 content cache.

This module is the only place that talks to an embedding model. To swap the
model later (a different Sentence Transformer, or a hosted embedding API),
replace `embed_texts` and leave the rest of the PoC untouched.

Two things here are easy to get wrong and quietly cost accuracy:

* **Instruction prefixes.** Several families were trained with a fixed prefix on
  queries and/or documents and lose several points of recall without it. The
  prefix differs per family, so MODEL_SPECS holds one entry each rather than
  sniffing for a substring.
* **Anisotropy.** Contextual embeddings occupy a narrow cone, so *everything*
  looks 0.8-similar and the ranking loses resolution. `fit_transform` /
  `apply_transform` implement the two standard fixes (mean-centering, and
  all-but-the-top / whitening); see `SpaceTransform`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

# Small, fast, and trained for cross-lingual paraphrase similarity, which is
# exactly "same topic, different wording/language". Override with EMBEDDING_MODEL.
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# Anchored to the package, not to the process: the documented invocation is from
# pipeline/, but a service started anywhere else would silently get a second,
# empty cache under its own cwd and pay the full embedding cost forever with
# nothing on screen to explain it. TAM_CACHE_DIR overrides.
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "processed"
BATCH_SIZE = 32

# Files that decide what a local checkpoint actually outputs. The weights first,
# then the configs, because pooling and normalisation live there rather than in
# the safetensors — a changed 1_Pooling/config.json moves every vector too.
FINGERPRINT_FILES = (
    "model.safetensors",
    "pytorch_model.bin",
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "1_Pooling/config.json",
)

# Handed to instruction-tuned models that want to know what the retrieval task is.
DEFAULT_INSTRUCTION = "Given a Slack message written by a software team, retrieve messages about the same work item"

log = logging.getLogger(__name__)

_model: Any = None  # lazily loaded SentenceTransformer


@dataclass(frozen=True)
class ModelSpec:
    """Everything model-specific about how text reaches the encoder.

    `query_prefix` / `passage_prefix` are prepended verbatim. `encode_kwargs`
    covers models that take the task as an argument instead of a prefix
    (jina-v3 switches LoRA adapters that way).
    """

    query_prefix: str = ""
    passage_prefix: str = ""
    trust_remote_code: bool = False
    query_kwargs: dict[str, Any] = field(default_factory=dict)
    passage_kwargs: dict[str, Any] = field(default_factory=dict)
    note: str = ""


# Matched by substring against the model id, first hit wins, so put the specific
# patterns before the general ones.
MODEL_SPECS: tuple[tuple[str, ModelSpec], ...] = (
    (
        "qwen3-embedding",
        ModelSpec(
            query_prefix=f"Instruct: {DEFAULT_INSTRUCTION}\nQuery: ",
            note="instruction on the query only; documents go in bare",
        ),
    ),
    (
        "e5-",  # multilingual-e5-{small,base,large}, including -instruct
        ModelSpec(query_prefix="query: ", passage_prefix="passage: ", note="both sides are prefixed"),
    ),
    (
        "embeddinggemma",
        ModelSpec(
            query_prefix="task: search result | query: ",
            passage_prefix="title: none | text: ",
            note="Gemma's documented retrieval prompts",
        ),
    ),
    (
        "jina-embeddings-v3",
        ModelSpec(
            trust_remote_code=True,
            query_kwargs={"task": "retrieval.query"},
            passage_kwargs={"task": "retrieval.passage"},
            note="asymmetric LoRA adapters, selected per role",
        ),
    ),
    ("gte-multilingual", ModelSpec(trust_remote_code=True, note="no prefix; needs remote code")),
    ("bge-m3", ModelSpec(note="no prefix by design")),
    ("bge-", ModelSpec(query_prefix="Represent this sentence for searching relevant passages: ", note="English BGE v1.5 style")),
)

DEFAULT_SPEC = ModelSpec(note="plain sentence-similarity model, no prefix")


def quiet_third_party_logs() -> None:
    """Keep our INFO logs readable; the model stack is very chatty at INFO."""
    for name in ("httpx", "httpcore", "huggingface_hub", "transformers", "sentence_transformers", "filelock", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def model_name() -> str:
    """Configured embedding model id."""
    return os.getenv("EMBEDDING_MODEL", "").strip() or DEFAULT_MODEL


def set_model(name: str | None) -> None:
    """Switch models, dropping the loaded one so the next call reloads.

    Lets one process compare several models, and backs the --model flag.
    """
    global _model
    if not name:
        return
    if name != model_name():
        _model = None
    os.environ["EMBEDDING_MODEL"] = name


def text_hash(text: str) -> str:
    """Cache key derived from message content only.

    Slack metadata (user, timestamp, channel) is deliberately excluded so it
    never enters the embedding, and so metadata edits never invalidate a vector.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def model_fingerprint(name: str | None = None) -> str:
    """Short digest of *which build* of the model this is, or "" if unknowable.

    A hub id names an immutable revision, so the id is its own fingerprint. A
    local directory is not: `finetune --out models/finetuned` run twice leaves
    the id identical while every vector the model produces moves, and because a
    fine-tune keeps the base model's width the vector-width guard below cannot
    see it either. So stat the checkpoint instead — size and mtime of the files
    that decide the output. Deliberately not a content hash: reading 450MB of
    safetensors on every run would cost more than re-embedding the whole corpus,
    and stat already changes on every save.
    """
    directory = Path(name or model_name())
    if not directory.is_dir():
        return ""
    parts: list[str] = []
    for relative in FINGERPRINT_FILES:
        candidate = directory / relative
        if candidate.exists():
            stat = candidate.stat()
            parts.append(f"{relative}:{stat.st_size}:{stat.st_mtime_ns}")
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def cache_identity() -> str:
    """What a cached vector was produced by: the model id plus its build."""
    fingerprint = model_fingerprint()
    return f"{model_name()}@{fingerprint}" if fingerprint else model_name()


def cache_dir() -> Path:
    """Where cache files live; TAM_CACHE_DIR wins over the package default."""
    override = os.getenv("TAM_CACHE_DIR", "").strip()
    return Path(override).expanduser() if override else CACHE_DIR


def cache_path() -> Path:
    """One cache file per model *build*.

    Per model alone was not enough: two builds of the same local path share an
    id, and mixing their vectors means taking cosine across two different spaces
    (see `model_fingerprint`). A hub id fingerprints to "", so its filename is
    unchanged and existing caches keep hitting.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", cache_identity())
    return cache_dir() / f"embeddings_{slug}.npz"


def model_spec(name: str | None = None) -> ModelSpec:
    """How this model wants its input prepared."""
    lowered = (name or model_name()).lower()
    for pattern, spec in MODEL_SPECS:
        if pattern in lowered:
            return spec
    return DEFAULT_SPEC


def prepared_text(text: str, role: str = "passage") -> str:
    """The exact string handed to the model, prefix included.

    Families differ: E5 prefixes both sides, Qwen3 puts an instruction on the
    query only, jina-v3 uses an argument instead of a prefix, and plain
    sentence-similarity models want the text untouched. MODEL_SPECS decides.
    """
    spec = model_spec()
    return (spec.query_prefix if role == "query" else spec.passage_prefix) + text


def _load_model() -> Any:
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer  # heavy import

        name = model_name()
        spec = model_spec(name)
        log.info("Loading embedding model %s (the first run downloads it)", name)
        if spec.note:
            log.info("  input handling: %s", spec.note)
        _model = SentenceTransformer(name, trust_remote_code=spec.trust_remote_code)
    return _model


def embed_texts(texts: Sequence[str], *, role: str = "passage") -> np.ndarray:
    """Return one L2-normalised row per text.

    Normalising here means cosine similarity is a plain dot product downstream.
    `role` is "passage" for stored messages and "query" for a search string;
    it decides which prefix and which encode arguments the model gets.
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    spec = model_spec()
    vectors = _load_model().encode(
        [prepared_text(text, role) for text in texts],
        batch_size=BATCH_SIZE,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=len(texts) > BATCH_SIZE,
        **(spec.query_kwargs if role == "query" else spec.passage_kwargs),
    )
    return np.asarray(vectors, dtype=np.float32)


def _read_cache(path: Path) -> dict[str, np.ndarray]:
    if not path.exists():
        return {}
    try:
        with np.load(path, allow_pickle=False) as data:
            hashes = [str(value) for value in data["hashes"]]
            vectors = data["embeddings"]
            # Written since the fingerprint fix; absent in older files, which by
            # construction only ever held hub models (fingerprint "").
            stored = str(data["model"]) if "model" in data else ""
            if "fingerprint" in data:
                stored = f"{stored}@{data['fingerprint']}" if str(data["fingerprint"]) else stored
    except (OSError, ValueError, KeyError) as error:
        log.warning("Ignoring unreadable embedding cache %s (%s)", path, error)
        return {}
    if vectors.ndim != 2 or len(hashes) != len(vectors):
        log.warning("Ignoring malformed embedding cache %s", path)
        return {}
    if stored and stored != cache_identity():
        # A miss, loudly: these vectors came out of a different model build, and
        # silently mixing them with freshly encoded ones means comparing two
        # spaces. This is what the unused `model` field was evidently for.
        log.warning(
            "Embedding cache %s was written by %s, not %s; re-embedding rather than mixing two spaces",
            path, stored, cache_identity(),
        )
        return {}
    return dict(zip(hashes, vectors))


def _write_cache(path: Path, cache: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename: savez_compressed rewrites the whole
    # archive each time, so a crash or a full disk part-way through would
    # otherwise leave a truncated file where a good cache used to be.
    temporary = path.with_suffix(".npz.tmp")
    with temporary.open("wb") as handle:
        np.savez_compressed(
            handle,
            hashes=np.array(list(cache), dtype=np.str_),
            embeddings=np.stack(list(cache.values())),
            model=np.array(model_name(), dtype=np.str_),
            fingerprint=np.array(model_fingerprint(), dtype=np.str_),
        )
    os.replace(temporary, path)


def embed_with_cache(texts: Sequence[str], *, use_cache: bool = True, role: str = "passage", prune: bool = False) -> np.ndarray:
    """Embed texts, reusing cached vectors for content that has not changed.

    `prune` is for callers that hand over a *whole* corpus and therefore own the
    file: without it the cache is only ever a union of everything ever embedded,
    so vectors for edited or deleted messages stay on disk forever and every
    rebuild recompresses them. A caller embedding a subset must leave it off.
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    # Key on the string the model actually sees, so a prefix change misses the cache.
    hashes = [text_hash(prepared_text(text, role)) for text in texts]
    unique: dict[str, str] = {}
    for key, text in zip(hashes, texts):
        unique.setdefault(key, text)

    path = cache_path()
    cache = _read_cache(path) if use_cache else {}
    missing = [key for key in unique if key not in cache]

    if missing:
        log.info(
            "Embedding %d unique text(s); %d already cached",
            len(missing),
            len(unique) - len(missing),
        )
        vectors = embed_texts([unique[key] for key in missing], role=role)
        if cache and vectors.shape[1] != next(iter(cache.values())).shape[0]:
            # Same model name, different vector width: the cache is unusable.
            log.warning("Cached vectors have the wrong width for %s; rebuilding", model_name())
            cache = {}
            missing = list(unique)
            vectors = embed_texts([unique[key] for key in missing], role=role)
        cache.update(zip(missing, vectors))
    else:
        log.info("Reused %d cached embedding(s) from %s", len(unique), path)

    stale = [key for key in cache if key not in unique] if prune else []
    for key in stale:
        del cache[key]

    if use_cache and (missing or stale):
        _write_cache(path, cache)
        log.info(
            "Embedding cache now holds %d vector(s) at %s%s",
            len(cache), path, f" ({len(stale)} dropped as no longer in the corpus)" if stale else "",
        )

    return np.stack([cache[key] for key in hashes])


@dataclass(frozen=True)
class SpaceTransform:
    """A fitted post-processing of the embedding space.

    Cosine on raw transformer output is distorted by anisotropy: the vectors sit
    in a narrow cone around a common direction, so unrelated pairs already score
    ~0.8 and the useful signal is squeezed into the last two decimals. All three
    methods below attack that, cheaply and without training:

    * ``center`` — subtract the corpus mean. Removes the common direction only.
    * ``abtt``   — all-but-the-top: centre, then project out the top `drop`
                   principal components. Robust on small corpora, which is why
                   it is the default choice here.
    * ``whiten`` — centre and rescale every principal axis to unit variance, so
                   no direction dominates. Strongest, but it needs *more messages
                   than dimensions*: with 33 messages and 768 dimensions the
                   covariance is rank-deficient, whitening drives every pair
                   towards orthogonal, and the pairwise spread collapses to
                   ~0.00 — measured on this corpus, not assumed. `shrinkage`
                   softens that; a corpus larger than the embedding width fixes
                   it. Below that, use ``abtt``.

    `mean` is always applied; `components` is the projection applied after it.
    """

    method: str
    mean: np.ndarray
    components: np.ndarray | None = None

    @property
    def label(self) -> str:
        return self.method


def fit_transform(matrix: np.ndarray, method: str = "none", *, drop: int = 1, shrinkage: float = 0.05) -> SpaceTransform:
    """Fit a space transform on the corpus. Queries must reuse the same one.

    Fitting on the corpus and applying to the query is deliberate: the corpus is
    what defines "a common direction here", and a single query cannot estimate it.
    """
    if method not in {"none", "center", "abtt", "whiten"}:
        raise ValueError(f"Unknown transform {method!r}; use none, center, abtt or whiten.")
    dim = matrix.shape[1] if matrix.ndim == 2 and matrix.size else 0
    if method == "none" or not dim:
        return SpaceTransform("none", np.zeros(dim, dtype=np.float32))

    mean = matrix.mean(axis=0).astype(np.float32)
    if method == "center":
        return SpaceTransform("center", mean)

    centered = matrix - mean
    # SVD of the centred matrix, not of an explicit covariance: same principal
    # axes, and it stays well conditioned when messages are fewer than dimensions.
    _, singular, right = np.linalg.svd(centered, full_matrices=False)

    if method == "abtt":
        drop = max(0, min(drop, right.shape[0] - 1))
        if not drop:
            return SpaceTransform("center", mean)
        top = right[:drop]
        # I - VᵀV removes the dominant directions and keeps the space's shape.
        with np.errstate(all="ignore"):  # Accelerate BLAS flag noise; see cosine_scores
            projection = (np.eye(dim, dtype=np.float32) - top.T @ top).astype(np.float32)
        if not np.isfinite(projection).all():
            raise ValueError("all-but-the-top projection is not finite.")
        return SpaceTransform("abtt", mean, projection)

    if len(matrix) <= dim:
        # With fewer messages than dimensions the covariance is rank-deficient:
        # every retained axis gets scaled to the same variance, the points end up
        # equidistant on a sphere, and every pairwise similarity collapses to one
        # number. Measured, not theoretical — a 33-message corpus takes the score
        # spread to 0.000. Whitening needs a real export; abtt does not.
        log.warning(
            "Whitening %d message(s) in %d dimensions is rank-deficient and will flatten the space. "
            "Use transform 'abtt' below roughly %d messages.",
            len(matrix), dim, dim,
        )
    variance = (singular**2) / max(1, len(matrix) - 1)
    # Shrink towards the mean variance so near-zero axes are not amplified into noise.
    floor = shrinkage * float(variance.mean()) if variance.size else 1.0
    scale = 1.0 / np.sqrt(np.maximum(variance, max(floor, 1e-8)))
    projection = (right.T * scale).astype(np.float32)  # PCA-whitening
    return SpaceTransform("whiten", mean, projection)


def apply_transform(matrix: np.ndarray, transform: SpaceTransform | None) -> np.ndarray:
    """Apply a fitted transform and re-normalise, so cosine stays a dot product."""
    if transform is None or transform.method == "none" or not matrix.size:
        return matrix
    single = matrix.ndim == 1
    rows = matrix.reshape(1, -1) if single else matrix
    with np.errstate(all="ignore"):  # Accelerate BLAS flag noise; see cosine_scores
        moved = rows - transform.mean
        if transform.components is not None:
            moved = moved @ transform.components
        norms = np.linalg.norm(moved, axis=1, keepdims=True)
    moved = (moved / np.maximum(norms, 1e-12)).astype(np.float32)
    return moved[0] if single else moved


def csls_scores(scores: np.ndarray, hubness: np.ndarray, *, weight: float = 1.0) -> np.ndarray:
    """Cross-domain Similarity Local Scaling: penalise records that sit near everything.

    In a cross-lingual space a few messages become *hubs* — nearest neighbour to
    unrelated queries purely because they sit in a dense region. CSLS subtracts
    each record's own mean similarity to its k nearest neighbours, so a record
    has to be closer to *this* query than it is to the crowd. `hubness` comes
    from `hubness_penalty` and is a property of the corpus, not the query.
    """
    return scores - weight * hubness


def hubness_penalty(matrix: np.ndarray, neighbours: int = 10) -> np.ndarray:
    """Mean similarity of each record to its `neighbours` nearest others."""
    if len(matrix) < 2:
        return np.zeros(len(matrix), dtype=np.float32)
    with np.errstate(all="ignore"):
        similarity = matrix @ matrix.T
    np.fill_diagonal(similarity, -np.inf)  # a record is not its own neighbour
    neighbours = max(1, min(neighbours, len(matrix) - 1))
    top = np.partition(-similarity, neighbours - 1, axis=1)[:, :neighbours]
    return (-top).mean(axis=1).astype(np.float32)


def cosine_scores(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Cosine similarity of every row against `query`; both are already normalised."""
    # NumPy 2.x on macOS links Apple Accelerate, whose vectorised sgemm raises
    # spurious divide-by-zero/overflow/invalid flags even for clean float32 input.
    # The flags are ignored and the result checked instead, so a genuinely broken
    # cache still fails loudly.
    with np.errstate(all="ignore"):
        scores = matrix @ query
    if not np.isfinite(scores).all():
        raise ValueError(
            f"Similarity scores are not finite. Delete {cache_path()} and run again."
        )
    return scores


def cosine_top_k(query: np.ndarray, matrix: np.ndarray, top_k: int) -> tuple[np.ndarray, np.ndarray]:
    """Rank rows of `matrix` against `query`; both are already normalised.

    Returns (indices of the best `top_k` rows, all similarity scores).
    """
    scores = cosine_scores(query, matrix)
    top_k = max(1, min(top_k, len(scores)))
    if top_k == len(scores):
        ranked = np.argsort(-scores)
    else:
        candidates = np.argpartition(-scores, top_k)[:top_k]
        ranked = candidates[np.argsort(-scores[candidates])]
    return ranked, scores
