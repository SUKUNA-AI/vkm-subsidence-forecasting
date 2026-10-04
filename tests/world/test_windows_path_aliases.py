"""Native namespace aliases; containment checks remain independent of host OS."""
from pathlib import PureWindowsPath

import pytest

from vkm_world.core.io import _comparison_path


@pytest.mark.parametrize("base,child,allowed", [
    (r"C:\repo", r"\\?\C:\repo\work\validation", True),
    (r"\\?\C:\repo", r"c:\REPO\work\validation", True),
    (r"C:\repo", r"\\?\C:\repo2\work", False),
    (r"C:\repo", r"\\?\D:\repo\work", False),
    (r"\\server\share\repo", r"\\?\UNC\server\share\repo\work", True),
    (r"\\?\UNC\server\share\repo", r"\\SERVER\share\repo\work", True),
    (r"\\server\share\repo", r"\\?\UNC\other\share\repo\work", False),
    (r"\\server\share\repo", r"\\?\UNC\server\other\repo\work", False),
    (r"C:\repo", r"\\?\Volume{12345678-1234-1234-1234-123456789abc}\repo\work", False),
    (r"C:\repo", r"\\.\C:\repo\work", False),
])
def test_resolved_namespace_aliases_preserve_exact_root_boundary(base, child, allowed):
    root, target = _comparison_path(PureWindowsPath(base)), _comparison_path(PureWindowsPath(child))
    assert target.is_relative_to(root) is allowed
    if allowed:
        assert target.relative_to(root).as_posix() == "work/validation" or target.relative_to(root).as_posix() == "work"


def test_alias_comparison_preserves_native_io_path_and_literal_name():
    original = PureWindowsPath(r"\\?\C:\repo\literal. ")
    assert _comparison_path(original).name == "literal. "
    assert str(original).startswith("\\\\?\\")
