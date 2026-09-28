"""Content-addressed artifact store of the document pipeline (agent C).

Blobs live under ``$VKM_DATA_ROOT/artifacts/<kind_dir>/<hh>/<hh>/<sha256>.<ext>`` and are addressed by
``artifact_id = "sha256:<hex>"`` (the hash of the stored bytes). A blob is written once and never modified. Artifacts
that are cheap and deterministic to regenerate (page renders for layout) may be registered without bytes
(``materialization = NOT_STORED_REPRODUCIBLE``) together with a ``recipe`` that says how to reproduce them (H-23).
The index rows (``ArtifactRecord``) are handed to the canonical writer with the source commit.
"""
from vkm_corpus.artifacts.store import ArtifactRecord, ArtifactStore, artifact_id_of, sha256_hex

__all__ = ["ArtifactRecord", "ArtifactStore", "artifact_id_of", "sha256_hex"]
