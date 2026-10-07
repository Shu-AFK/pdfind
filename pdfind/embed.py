"""Embedding model wrapper (ONNX via fastembed — no PyTorch)."""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import numpy as np

# fastembed defaults to /tmp/fastembed_cache, which is tmpfs on this system:
# the model would be re-downloaded after every reboot and be unavailable
# offline. Keep it somewhere that survives.
CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME", "~/.cache")
).expanduser() / "fastembed"

_MODEL = None
_MODEL_NAME = None


class Embedder:
    def __init__(self, model_name: str, threads: int | None = None):
        global _MODEL, _MODEL_NAME
        if _MODEL is None or _MODEL_NAME != model_name:
            from fastembed import TextEmbedding  # imported lazily: ~1s
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with warnings.catch_warnings():
                # fastembed warns that sentence-transformers models now use
                # mean pooling instead of CLS. Mean pooling is what these
                # models were trained with, so this is the correct behaviour.
                warnings.filterwarnings("ignore", message=".*mean pooling.*")
                _MODEL = TextEmbedding(model_name, threads=threads,
                                       cache_dir=str(CACHE_DIR))
            _MODEL_NAME = model_name
        self.model = _MODEL
        self.name = model_name
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = int(self.embed_passages(["dimension probe"]).shape[1])
        return self._dim

    @staticmethod
    def _normalize(arr: np.ndarray) -> np.ndarray:
        arr = np.asarray(arr, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.maximum(norms, 1e-12)

    def embed_passages(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = list(self.model.embed(texts, batch_size=batch_size))
        return self._normalize(np.vstack(vecs))

    def embed_query(self, text: str) -> np.ndarray:
        """Uses the model's query-side prompt (bge prepends an instruction)."""
        vec = next(iter(self.model.query_embed([text])))
        return self._normalize(np.asarray(vec, dtype=np.float32)[None, :])[0]
