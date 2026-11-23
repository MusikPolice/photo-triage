"""CLIP's text encoder in the API, for search (`photo_triage.api.search`)."""

import threading

from photo_triage.ml import ClipEmbedder, FakeClip, OnnxClip
from photo_triage.settings import Settings

API_THREADS = 1
"""ONNX Runtime's threads for a query: the app has one CPU (plan §6.11)."""


class TextEncoder:
    """CLIP's text encoder for `settings`, loaded on first use, once."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._embedder: ClipEmbedder | None = None

    def embedder(self) -> ClipEmbedder:
        """Raises `ModelMissingError` if the export isn't in `MODELS_DIR`; the next
        call tries again, so copying it there needs no restart."""
        with self._lock:
            if self._embedder is None:
                settings = self._settings
                if settings.worker_fake_clip:
                    self._embedder = FakeClip(settings.clip_model)
                else:
                    self._embedder = OnnxClip(settings.models_dir, settings.clip_model, API_THREADS)
            return self._embedder
