"""Dependency-free access vocabulary shared with vendor GIS runtimes."""
from enum import StrEnum


class DataAccessClass(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE_CLOUD_ALLOWED = "PRIVATE_CLOUD_ALLOWED"
    PRIVATE_LOCAL_ONLY = "PRIVATE_LOCAL_ONLY"
    RESTRICTED = "RESTRICTED"
    SEALED = "SEALED"


class ExperimentalRole(StrEnum):
    INPUT = "INPUT"
    CALIBRATION = "CALIBRATION"
    VALIDATION = "VALIDATION"
    TEST_SEALED = "TEST_SEALED"
    TARGET = "TARGET"
    UNKNOWN = "UNKNOWN"
