"""Versions that enter processing signatures.

``PIPELINE_VERSION`` is the version of the *processing contract* (what a stage produces from a given input), not a git
commit: a refactoring that does not change outputs keeps it, a change of extraction/normalisation semantics bumps it.
Together with extractor id/version, model revision and config hash it forms the idempotency signature, so an
unchanged page is never processed (or OCR-ed) twice.
"""
PIPELINE_VERSION = "0.1.0"
