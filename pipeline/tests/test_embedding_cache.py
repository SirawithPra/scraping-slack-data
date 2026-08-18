"""What a cached vector was produced by.

The cache used to be keyed on the model *id*, and the `model` field written into
the npz was never read back. `finetune --out models/syn_finetuned` run twice
therefore left the id identical while every vector the model produced moved, so
the second run served the first run's vectors — cosine taken across two different
spaces, with nothing on screen to say so. A fine-tune keeps the base model's
width, so the vector-width guard could not see it either.

Nothing here loads a model: the fingerprint is a stat of the checkpoint files, and
the cache round trip is numpy. The fake checkpoint below only has to contain the
files `FINGERPRINT_FILES` names.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tam.retrieval import embeddings


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Never read or write the real data/processed/ cache from a test."""
    monkeypatch.setenv("TAM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    return tmp_path


def checkpoint(root: Path, *, weights: bytes, config: str) -> Path:
    """A directory shaped enough like a SentenceTransformer save to be stat-able."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "model.safetensors").write_bytes(weights)
    (root / "config.json").write_text(config, encoding="utf-8")
    return root


def test_hub_id_keeps_its_existing_cache_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hub id names an immutable revision, so its filename must not change.

    data/processed/ already holds files under the pre-fix names; renaming them
    would silently re-embed the whole corpus on the next run.

    The model is pinned here rather than read from the default: this asserts a
    property of hub ids, and hardcoding whichever model happens to be default makes
    the test fail on the next model change for no reason of its own.
    """
    hub_id = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    monkeypatch.setenv("EMBEDDING_MODEL", hub_id)
    assert embeddings.model_fingerprint(hub_id) == ""
    assert embeddings.cache_identity() == hub_id
    assert embeddings.cache_path().name == f"embeddings_{hub_id.replace('/', '_')}.npz"


def test_the_default_model_is_a_hub_id_with_a_stable_cache_name() -> None:
    """Whatever the default is, it must not fingerprint or move its cache per run.

    A local checkpoint path as the shipped default would give every clone a
    different cache filename and a fingerprint that changes when the file is
    touched — which is right for a model you retrain, and wrong for a default.
    """
    assert embeddings.model_fingerprint(embeddings.DEFAULT_MODEL) == ""
    assert "/" in embeddings.DEFAULT_MODEL, "the default should be a hub id, not a path"


def test_retraining_in_place_changes_the_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = checkpoint(tmp_path / "models" / "syn_finetuned", weights=b"\x00" * 64, config='{"hidden_size": 384}')
    monkeypatch.setenv("EMBEDDING_MODEL", str(local))

    first = embeddings.model_fingerprint()
    first_path = embeddings.cache_path()
    assert first, "a local checkpoint must fingerprint to something"
    assert first in embeddings.cache_identity()

    # The same --out path, trained again: identical id, different weights.
    checkpoint(local, weights=b"\x01" * 96, config='{"hidden_size": 384, "note": "run 2"}')

    assert embeddings.model_fingerprint() != first
    assert embeddings.cache_path() != first_path, "run 2 must not land on run 1's cache file"


def test_pooling_config_alone_moves_the_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pooling and normalisation live in the configs, not in the safetensors."""
    local = checkpoint(tmp_path / "models" / "pooled", weights=b"\x00" * 64, config="{}")
    (local / "1_Pooling").mkdir()
    (local / "1_Pooling" / "config.json").write_text('{"pooling_mode_mean_tokens": true}', encoding="utf-8")
    monkeypatch.setenv("EMBEDDING_MODEL", str(local))
    before = embeddings.model_fingerprint()

    (local / "1_Pooling" / "config.json").write_text('{"pooling_mode_cls_token": true}', encoding="utf-8")
    assert embeddings.model_fingerprint() != before


def test_cache_written_by_another_build_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """Reading a stale file must miss loudly, not mix two spaces."""
    local = checkpoint(tmp_path / "models" / "syn_finetuned", weights=b"\x00" * 64, config="{}")
    monkeypatch.setenv("EMBEDDING_MODEL", str(local))

    path = embeddings.cache_path()
    vector = np.ones(4, dtype=np.float32) / 2.0
    embeddings._write_cache(path, {embeddings.text_hash("ยังรอ API"): vector})
    assert embeddings._read_cache(path), "a cache written by this build must read back"

    checkpoint(local, weights=b"\x01" * 96, config='{"note": "run 2"}')
    with caplog.at_level("WARNING"):
        assert embeddings._read_cache(path) == {}
    assert "not" in caplog.text and "re-embedding" in caplog.text


def test_legacy_cache_without_a_fingerprint_still_hits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files written before the fix hold no `fingerprint`, and only ever held hub
    models — refusing them would re-embed every existing corpus for nothing."""
    monkeypatch.setenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
    path = embeddings.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = embeddings.text_hash("passage: ยังรอ API")
    with path.open("wb") as handle:
        np.savez_compressed(
            handle,
            hashes=np.array([key], dtype=np.str_),
            embeddings=np.stack([np.ones(4, dtype=np.float32)]),
            model=np.array("intfloat/multilingual-e5-base", dtype=np.str_),
        )
    assert list(embeddings._read_cache(path)) == [key]


def test_key_is_the_string_the_model_actually_sees(monkeypatch: pytest.MonkeyPatch) -> None:
    """E5 prefixes both sides, so a prefix change has to miss the cache."""
    monkeypatch.setenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
    assert embeddings.prepared_text("ยังรอ API", "passage") == "passage: ยังรอ API"
    assert embeddings.prepared_text("ยังรอ API", "query") == "query: ยังรอ API"
    assert embeddings.text_hash(embeddings.prepared_text("ยังรอ API", "passage")) != embeddings.text_hash("ยังรอ API")


def test_cache_dir_is_anchored_to_the_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting the server from anywhere but pipeline/ must not create a second,
    empty cache under that cwd and re-embed the corpus with nothing to explain it."""
    monkeypatch.delenv("TAM_CACHE_DIR", raising=False)
    assert embeddings.cache_dir().is_absolute()
    assert embeddings.cache_dir() == Path(embeddings.__file__).resolve().parents[2] / "data" / "processed"
