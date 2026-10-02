"""Local sentence embeddings (fastembed / ONNX, CPU only - no data leaves the machine).

Configure with environment variables:
  KB_EMBED_MODEL   fastembed model name (default BAAI/bge-small-en-v1.5)
  KB_MODEL_CACHE   where model files are downloaded/cached (default ./models)
"""

import hashlib
import os
from pathlib import Path

import numpy as np

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
ROOT = Path(__file__).resolve().parent.parent


def embedding_text(requirement: str, parent_text: str | None) -> str:
    """What gets embedded for a record: the requirement, prefixed by the lead-in it sits under."""
    return f"{parent_text} {requirement}" if parent_text else requirement


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Embedder:
    def __init__(self, model_name: str | None = None, cache_dir: str | None = None):
        self.model_name = model_name or os.environ.get("KB_EMBED_MODEL", DEFAULT_MODEL)
        self.cache_dir = cache_dir or os.environ.get("KB_MODEL_CACHE", str(ROOT / "models"))
        self._model = None

    def _load(self):
        if self._model is None:
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            from fastembed import TextEmbedding  # heavy import; only when actually embedding
            self._model = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
        return self._model

    def embed(self, texts: list[str], batch_size: int = 64) -> np.ndarray:
        """Unit-normalized float32 vectors, one row per text (so dot product = cosine similarity)."""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        vectors = np.array(list(self._load().embed(texts, batch_size=batch_size)), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.where(norms == 0, 1, norms)
