"""Turn text into vectors with a local sentence-transformers model."""

import os

import numpy as np
import torch
from sentence_transformers import SentenceTransformer

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def pick_device() -> str:
    return "cuda" if torch.cuda.is_available() else "cpu"


def hf_offline() -> bool:
    """True when HF_HUB_OFFLINE is set: models must load from the local cache only.

    huggingface_hub reads HF_HUB_OFFLINE once, when it is first imported, which is
    before .env is loaded. Checking it here, at load time, and passing
    local_files_only makes the setting work regardless of import order.
    """
    return os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")


def describe_device(device: str) -> str:
    if device == "cuda":
        return f"cuda ({torch.cuda.get_device_name(0)})"
    return "cpu"


def load_model(model_name: str = MODEL_NAME, device: str | None = None) -> SentenceTransformer:
    return SentenceTransformer(model_name, device=device or pick_device(), local_files_only=hf_offline())


def embed_texts(model: SentenceTransformer, texts: list[str], batch_size: int = 32) -> np.ndarray:
    """Return one float32 row vector per input text, shape (len(texts), dim)."""
    return model.encode(
        texts,
        batch_size=batch_size,
        convert_to_numpy=True,
        show_progress_bar=False,
    ).astype(np.float32)
