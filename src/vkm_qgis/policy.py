"""Logical path references; mutations only inside WORK or PRIVATE, never PUBLIC."""
from __future__ import annotations
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from vkm_qgis.errors import ToolFailure

NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,62}$')
WRITE_SUFFIXES = {'.gpkg', '.qgz', '.qgs', '.json', '.geojson', '.tif', '.tiff', '.png'}
READ_SUFFIXES = WRITE_SUFFIXES | {'.jpg', '.jpeg'}

class Paths:
    def __init__(self, public: Path, work: Path, private: Path | None = None):
        self.roots = {'public': public.resolve(), 'work': work.resolve()}
        if private:
            self.roots['private'] = private.resolve()
        if self.roots['work'] == self.roots['public']:
            raise ToolFailure('PATH_POLICY', 'WORK root cannot equal PUBLIC root')

    @classmethod
    def from_env(cls):
        public = Path(os.environ.get('VKM_PUBLIC_ROOT', Path.cwd()))
        work = Path(os.environ.get('VKM_WORK', public / 'work'))
        private = os.environ.get('VKM_RESOURCES_ROOT')
        return cls(public, work, Path(private) if private else None)

    @classmethod
    def from_config(cls, config: dict):
        return cls(Path(config['public']), Path(config['work']), Path(config['private']) if config.get('private') else None)

    def config(self):
        return {k: str(v) for k, v in self.roots.items()}

    def resolve(self, ref: str, *, write: bool = False, exists: bool = False, suffixes=None) -> Path:
        if not isinstance(ref, str) or not ref or '\\' in ref or any(c in ref for c in '\r\n\0'):
            raise ToolFailure('PATH_POLICY', 'Use a non-empty relative POSIX path reference')
        root_name, sep, rel = ref.partition(':')
        if not sep:
            root_name, rel = 'work', ref
        if root_name not in self.roots or not rel or ':' in rel or PureWindowsPath(rel).is_absolute() or PurePosixPath(rel).is_absolute():
            raise ToolFailure('PATH_POLICY', 'Unknown root or absolute path rejected')
        if any(p in {'..', '.'} for p in rel.split('/')) or any(c in rel for c in '<>"|?*'):
            raise ToolFailure('PATH_POLICY', 'Path traversal or non-portable filename rejected')
        if write and root_name == 'public':
            raise ToolFailure('PATH_POLICY', 'GIS outputs remain WORK/PRIVATE')
        path = (self.roots[root_name] / rel).resolve()
        if not path.is_relative_to(self.roots[root_name]):
            raise ToolFailure('PATH_POLICY', 'Resolved path escapes its declared root')
        allowed = suffixes if suffixes is not None else WRITE_SUFFIXES if write else READ_SUFFIXES
        if path.suffix.lower() not in allowed:
            raise ToolFailure('PATH_POLICY', 'Unsupported file extension')
        if exists and not path.is_file():
            raise ToolFailure('NOT_FOUND', 'Input file does not exist', details={'ref': ref})
        return path

    def new_output(self, ref: str, *, overwrite: bool = False, suffixes=None) -> Path:
        path = self.resolve(ref, write=True, suffixes=suffixes)
        if path.exists() and not overwrite:
            raise ToolFailure('OUTPUT_EXISTS', 'Output already exists; choose another path')
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

def layer_name(name: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name) or name.lower().startswith(('gpkg_', 'sqlite_', '_vkm')):
        raise ToolFailure('INVALID_NAME', 'Layer/field names must be portable ASCII identifiers')
    return name
