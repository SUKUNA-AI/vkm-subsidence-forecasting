"""Confidentiality and experimental role are independent, explicit declarations."""
from dataclasses import dataclass
from vkm_corpus.contracts.access_vocab import DataAccessClass as AccessClass, ExperimentalRole


@dataclass(frozen=True)
class Policy:
    access_class: AccessClass
    experimental_role: ExperimentalRole
    decision_ref: str

    def __post_init__(self):
        object.__setattr__(self, "access_class", AccessClass(self.access_class))
        object.__setattr__(self, "experimental_role", ExperimentalRole(self.experimental_role))
        if not self.decision_ref.strip():
            raise ValueError("policy needs an owner decision reference")

    def require(self, destination: str = "local") -> None:
        """Gate content inspection/conversion. Sealed evaluators use a separate protocol.

        RESTRICTED needs a dedicated approval workflow; this general intake cannot bypass it.
        Permission to view TARGET never establishes independent/blind validation.
        """
        if destination not in {"local", "cloud", "public"}:
            raise ValueError("destination must be local, cloud or public")
        if self.access_class in {AccessClass.RESTRICTED, AccessClass.SEALED} or \
                self.experimental_role is ExperimentalRole.TEST_SEALED:
            raise PermissionError("sealed/restricted data is outside general dataset intake")
        if destination == "public" and self.access_class is not AccessClass.PUBLIC:
            raise PermissionError("dataset is not public")
        if destination == "cloud" and self.access_class is AccessClass.PRIVATE_LOCAL_ONLY:
            raise PermissionError("dataset is local-only")

    def as_dict(self) -> dict:
        return {"access_class": self.access_class.value, "experimental_role": self.experimental_role.value,
                "decision_ref": self.decision_ref}
