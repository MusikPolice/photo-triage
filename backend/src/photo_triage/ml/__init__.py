"""Pluggable ML adapters (embedder, face detector, tagger) and their fakes. They
depend on neither the database nor the API, so the pipeline decides what's stored."""

from photo_triage.ml.clip import ClipEmbedder, FakeClip, ModelMissingError, OnnxClip

__all__ = ["ClipEmbedder", "FakeClip", "ModelMissingError", "OnnxClip"]
