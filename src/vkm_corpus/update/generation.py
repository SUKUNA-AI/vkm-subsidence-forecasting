"""Fail-closed maintenance window for a switch across independent stores."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from vkm_corpus.parquet.atomic import create_exclusive, write_bytes
from vkm_corpus.update.contracts import GenerationManifest
from vkm_evidence.contracts import canonical_bytes


class GenerationUnavailable(RuntimeError):
    pass


class GenerationCoordinator:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _path(self, name):
        path = self.root / name
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise GenerationUnavailable("unsafe generation selector")
        return path

    def manifest(self) -> GenerationManifest:
        if self._path("MAINTENANCE").exists():
            raise GenerationUnavailable("generation switch in progress")
        head = self._path("CURRENT").read_text(encoding="ascii").strip()
        return self._load_head(head)

    def _load_head(self, head: str) -> GenerationManifest:
        """Validate a selector before it can become either a candidate or rollback source."""
        if len(head) != 64 or any(c not in "0123456789abcdef" for c in head):
            raise GenerationUnavailable("invalid generation selector")
        manifest = GenerationManifest.model_validate_json(self._path(head + ".json").read_bytes())
        if manifest.sha256 != head:
            raise GenerationUnavailable("generation manifest hash mismatch")
        return manifest

    @staticmethod
    def verify(manifest: GenerationManifest, observed: dict, observed_services: dict | None = None) -> None:
        for component in manifest.components:
            if not component.required:
                continue
            value = observed.get(component.component)
            if value != component.model_dump(mode="json"):
                raise GenerationUnavailable("observed component generation differs")
        if manifest.services or observed_services is not None:
            if observed_services is None or set(observed_services) != {s.service for s in manifest.services}:
                raise GenerationUnavailable("live service identities omitted from generation verification")
            if any(observed_services[s.service] != s.model_dump(mode="json") for s in manifest.services):
                raise GenerationUnavailable("observed service identity differs")

    def status(self, observer) -> dict:
        try:
            manifest = self.manifest()
            observed = observer()
            self.verify(manifest, observed)
            # Recheck selectors after inspection so a request doesn't enter a
            # mixed snapshot while a maintenance lease is being created.
            if self.manifest().sha256 != manifest.sha256:
                raise GenerationUnavailable("generation changed during admission")
            return {"status": "READY", "generation": manifest.sha256}
        except (OSError, ValueError, GenerationUnavailable):
            return {"status": "UNAVAILABLE"}

    def switch(self, candidate: GenerationManifest, observer, apply, restore, *, acceptance) -> dict:
        """Callbacks are operator-owned adapters, never strings from documents.

        apply/restore must drain the service before modifying selectors. A failed
        restore leaves MAINTENANCE in place: failed rollback is never an open gate.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        lease = self._path("MAINTENANCE")
        if not create_exclusive(lease, token):
            raise GenerationUnavailable("another switch/recovery holds maintenance")
        previous = self._path("CURRENT").read_text(encoding="ascii").strip() if self._path("CURRENT").exists() else None
        # This is intentionally outside the apply/restore try block. An invalid
        # previous identity is not a trusted rollback target. Keep maintenance,
        # do not apply the candidate, and do not call restore with guessed state.
        old = self._load_head(previous) if previous is not None else None
        if old is not None:
            self.verify(old, observer())
        write_bytes(self._path("tmp"), self._path(candidate.sha256 + ".json"), canonical_bytes(candidate))
        try:
            apply(candidate)
            self.verify(candidate, observer())
            if acceptance(candidate) is not True:
                raise GenerationUnavailable("post-switch acceptance failed")
            write_bytes(self._path("tmp"), self._path("CURRENT"), (candidate.sha256 + "\n").encode("ascii"), overwrite=True)
        except Exception:
            restore(old)
            if old is not None:
                self.verify(old, observer())
            # No previous generation: operator must keep services stopped.
            if old is None:
                raise GenerationUnavailable("initial switch failed; maintenance retained")
            if lease.read_text(encoding="ascii") != token:
                raise GenerationUnavailable("maintenance owner changed")
            lease.unlink()
            raise
        if lease.read_text(encoding="ascii") != token:
            raise GenerationUnavailable("maintenance owner changed")
        lease.unlink()
        return {"status": "PASS", "generation": candidate.sha256, "previous": previous}
