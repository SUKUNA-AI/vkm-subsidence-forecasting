"""Versioned native datasets, independent of the document/OCR registry.

Importing this package does not import GDAL or spreadsheet engines and never opens data.
"""

from .manifest import DatasetVersion, FileMember, Registry, discover, verify_members
from .policy import AccessClass, ExperimentalRole, Policy

__all__ = ["AccessClass", "ExperimentalRole", "Policy", "DatasetVersion", "FileMember", "Registry",
           "discover", "verify_members"]
