"""Text → embedding (local).

Uses `sentence-transformers`. Default model: `BAAI/bge-small-en-v1.5`
(384-dim, ~135 MB, fast on CPU and Apple-Silicon MPS). Override via
env var `AGENT_EMBEDDING_MODEL`.

If sentence-transformers can't load (offline first run, missing deps),
falls back to a deterministic SHA-based embedder of the SAME dimension
so retrieval still functions (recency-only, no semantics).
"""
from __future__ import annotations

import hashlib
import os
import struct
from functools import lru_cache
from typing import Sequence

# Silence HF Hub warnings. Model is already cached locally; HF_HUB_OFFLINE
# prevents network calls, HF_HUB_VERBOSITY suppresses the unauthenticated-
# request warning that fires even in offline mode.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")

DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_DIM = 1024  # bge-m3 dim; recomputed at load time when the model loads


def model_name() -> str:
    return os.environ.get("AGENT_EMBEDDING_MODEL", DEFAULT_MODEL)


@lru_cache(maxsize=1)
def _model():
    try:
        import torch  # type: ignore
        from sentence_transformers import SentenceTransformer  # type: ignore



        # fp16 on Apple Silicon MPS / CUDA; fp32 on CPU (fp16 is slower there).
        device = (
            "mps" if torch.backends.mps.is_available()
            else "cuda" if torch.cuda.is_available()
            else "cpu"
        )
        kwargs = {}
        if device != "cpu":
            kwargs["model_kwargs"] = {"torch_dtype": torch.float16}
        m = SentenceTransformer(model_name(), device=device, **kwargs)
        return m
    except Exception as e:  # ImportError, OSError (offline), CUDA mismatch, etc.
        print(f"[embed] WARN: could not load {model_name()}: {e}. "
              f"Falling back to deterministic SHA embedder.")
        return None


@lru_cache(maxsize=1)
def embedding_dim() -> int:
    """Dimension of the active embedder. Determined at first load."""
    m = _model()
    if m is None:
        return DEFAULT_DIM
    try:
        return int(m.get_sentence_embedding_dimension())
    except Exception:
        return DEFAULT_DIM


def _local_embed(text: str, dim: int) -> list[float]:
    """Deterministic, dimension-correct, semantically-meaningless fallback."""
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        h = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        for i in range(0, len(h), 4):
            chunk = h[i : i + 4]
            if len(chunk) < 4:
                break
            v = struct.unpack("<I", chunk)[0] / 0xFFFFFFFF
            out.append(v * 2 - 1)
            if len(out) >= dim:
                break
        counter += 1
    return out


def _normalize(vec: list[float]) -> list[float]:
    s = sum(x * x for x in vec) ** 0.5
    return [x / s for x in vec] if s > 0 else vec


def embed(text: str, *, input_type: str = "document") -> list[float]:
    """Embed a single string. input_type is accepted for API compatibility but
    unused locally (bge-style models don't differentiate at this level)."""
    m = _model()
    if m is None:
        return _local_embed(text, embedding_dim())
    vec = m.encode(text, normalize_embeddings=True, show_progress_bar=False)
    return list(map(float, vec))


def embed_batch(texts: Sequence[str], *, input_type: str = "document") -> list[list[float]]:
    if not texts:
        return []
    m = _model()
    if m is None:
        return [_local_embed(t, embedding_dim()) for t in texts]
    vecs = m.encode(list(texts), normalize_embeddings=True, show_progress_bar=False)
    return [list(map(float, v)) for v in vecs]


def serialize(vec: list[float]) -> bytes:
    """Pack as little-endian float32 (the format vec0 expects)."""
    return struct.pack(f"<{len(vec)}f", *vec)


def using_local_model() -> bool:
    return _model() is not None
