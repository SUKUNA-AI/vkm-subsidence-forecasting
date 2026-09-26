"""Common base types of every WorldSpec object."""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .provenance import Provenance

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-/+]*$")
SOURCE_ID_RE = re.compile(r"^(VKM-SRC-\d{3}|EXT-SRC-\d{3}|EXTWEB-[A-Za-z0-9\-]+)$")


class WorldObject(BaseModel):
    """Anything the world contains: an id, a human name, provenance and free notes."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="stable identifier, unique within its collection")
    name: str | None = None
    provenance: Provenance
    notes: str | None = None

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError(f"invalid id '{v}' (allowed: letters, digits, _ . : - / +; no spaces)")
        return v


def duplicate_ids(objs) -> list[str]:
    seen, dup = set(), []
    for o in objs:
        if o.id in seen:
            dup.append(o.id)
        seen.add(o.id)
    return dup
