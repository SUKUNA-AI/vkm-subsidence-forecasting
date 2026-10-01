"""Access permissions and experimental roles are independent contracts.

Callers must apply policy before retrieval, counts, previews, and exports. A role
never grants access; seeing a target never makes it an independent blind test.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from vkm_corpus.contracts.access_vocab import DataAccessClass, ExperimentalRole


class AccessContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    principal: str = Field(min_length=1, max_length=200)
    execution: str = Field(pattern="^(LOCAL|CLOUD)$")
    granted_classes: frozenset[DataAccessClass] = frozenset({DataAccessClass.PUBLIC})
    allow_targets: bool = False

    @field_serializer("granted_classes", when_used="json")
    def _ordered_classes(self, value):
        return sorted(v.value for v in value)


class ResourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Unknown historical classification must be resolved explicitly, not PUBLIC.
    access_class: DataAccessClass = DataAccessClass.RESTRICTED
    experimental_role: ExperimentalRole = ExperimentalRole.UNKNOWN
    policy_version: str = Field(min_length=1)
    authority: str = Field(min_length=1)
    restrictions: tuple[str, ...] = ()

    def permits(self, context: AccessContext) -> bool:
        if self.access_class not in context.granted_classes:
            return False
        if context.execution == "CLOUD" and self.access_class in {
            DataAccessClass.PRIVATE_LOCAL_ONLY, DataAccessClass.SEALED,
        }:
            return False
        if self.experimental_role in {ExperimentalRole.TARGET, ExperimentalRole.TEST_SEALED}:
            return context.allow_targets
        return True

    def require(self, context: AccessContext) -> None:
        if not self.permits(context):
            # No resource name, value, or target description in the denial.
            raise PermissionError("RESOURCE_POLICY_DENIED")

    def preserves(self, parent: "ResourcePolicy") -> bool:
        """Derived records may restrict access, never silently widen it."""
        rank = {DataAccessClass.PUBLIC: 0, DataAccessClass.PRIVATE_CLOUD_ALLOWED: 1,
                DataAccessClass.PRIVATE_LOCAL_ONLY: 2, DataAccessClass.RESTRICTED: 3,
                DataAccessClass.SEALED: 4}
        if rank[self.access_class] < rank[parent.access_class]:
            return False
        if parent.access_class == DataAccessClass.PRIVATE_LOCAL_ONLY and self.access_class == DataAccessClass.RESTRICTED:
            return False  # RESTRICTED can be explicitly granted to cloud clients.
        if not set(parent.restrictions).issubset(self.restrictions):
            return False
        if parent.experimental_role in {ExperimentalRole.TARGET, ExperimentalRole.TEST_SEALED}:
            return self.experimental_role == parent.experimental_role
        return True
