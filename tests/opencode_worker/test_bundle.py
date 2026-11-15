"""Include portable OpenCode contracts in existing repository CPU CI.

This imports stdlib unittest cases only. No actual OpenCode/model/MCP dependency
is started; native qualification remains a separate explicitly invoked command.
"""
import importlib.util
from pathlib import Path
import sys
import unittest

_SUITE = Path(__file__).resolve().parents[2] / 'tools/vkm_opencode_worker/tests'
for _path in sorted(_SUITE.glob('test_*.py')):
    _name = 'vkm_opencode_bundle_' + _path.stem
    _spec = importlib.util.spec_from_file_location(_name, _path)
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _module
    _spec.loader.exec_module(_module)
    for _label, _value in vars(_module).items():
        if isinstance(_value, type) and issubclass(_value, unittest.TestCase) and _value.__module__ == _name:
            globals()[_path.stem + '_' + _label] = _value

# pytest's unittest collector also recognizes private class aliases. Do not
# expose the final loop value as a second copy of the same TestCase family.
del _name, _path, _spec, _module, _label, _value
