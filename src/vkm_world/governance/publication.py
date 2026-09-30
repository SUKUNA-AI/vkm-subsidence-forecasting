"""Validate publication maps and stage complete batches before replacing public files."""
from __future__ import annotations

import json
import math
import os
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import shutil
import tempfile

from vkm_world.governance.leakage import BIBLIO_COLUMN_HINTS, scan


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON object key')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('nonfinite JSON constant is not permitted')


def strict_json(text):
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('nonfinite JSON number is not permitted')
        return number
    return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant, parse_float=finite_float)


def public_json_texts(value):
    """Ordered JSON text runs; array/string boundaries do not hide consecutive quoted words.

    Dictionary keys stay in their original order. Excluded bibliographic values
    break a run, so removing a bibliography cannot join unrelated fragments.
    """
    def fragments(value, bibliography=False):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                is_bibliography = any(hint in key.lower() for hint in BIBLIO_COLUMN_HINTS)
                yield from fragments(child, is_bibliography)
        elif isinstance(value, list):
            for child in value:
                yield from fragments(child, bibliography)
        elif bibliography:
            yield None
        elif isinstance(value, str):
            yield value
        else:
            yield json.dumps(value, allow_nan=False)
    run = []
    for fragment in fragments(value):
        if fragment is None:
            if run:
                yield ' '.join(run)
                run = []
        else:
            run.append(fragment)
    if run:
        yield ' '.join(run)


def relative_path(value: str) -> str:
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or PurePosixPath(value).is_absolute() or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('publication paths must be normalized relative paths')
    return value


def contained_path(root: Path, relative: str) -> Path:
    path = (root/relative_path(relative)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('publication path escapes its root')
    return path


def load_catalogue_map(path: Path) -> dict[str, str]:
    mapping = strict_json(path.read_text(encoding='utf8'))
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError('catalogue map must be a nonempty object')
    for source, target in mapping.items():
        relative_path(source)
        relative_path(target)
        if (Path(source).suffix not in ('.csv', '.json') or Path(target).suffix != Path(source).suffix
                or target.split('/')[0] not in ('evidence', 'catalogues')):
            raise ValueError('catalogue map must name CSV/JSON catalogue outputs')
        if target == 'evidence/PUBLIC_CATALOGUE_MANIFEST.json':
            raise ValueError('catalogue map cannot overwrite the reserved build manifest')
    if len(set(mapping.values())) != len(mapping):
        raise ValueError('catalogue map contains duplicate targets')
    return mapping


@contextmanager
def publication_lock(root: Path):
    """Persistent inode, nonblocking native lock; concurrent publishers fail closed."""
    path = root/'work/publication.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.name == 'posix':
            import fcntl
            acquire = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        elif os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: (stream.seek(0), msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            raise ValueError('native publication locking is unavailable')
        try:
            acquire()
        except OSError as exc:
            raise ValueError('another publication holds the process lock') from exc
        try:
            if (root/'work/publication_recovery.json').exists():
                raise ValueError('publication blocked: an incomplete rollback requires recovery')
            yield
        finally:
            release()


def publish_batch(root: Path, outputs: dict[str, bytes], *, before_replace=None) -> None:
    """All validation and leakage checks precede destination writes.

    Replacements are atomic per file. This is a validation transaction, not a
    filesystem-wide transaction against process termination or hardware failure.
    """
    destinations = {rel: contained_path(root, rel) for rel in outputs}
    if len(set(destinations.values())) != len(destinations):
        raise ValueError('publication outputs alias the same destination')
    if any(path.exists() and not path.is_file() for path in destinations.values()):
        raise ValueError('publication destination is not a regular file')
    with publication_lock(root), tempfile.TemporaryDirectory(prefix='vkm-publication-') as directory:
        stage = Path(directory)
        for rel, data in outputs.items():
            path = stage/rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        problems = scan(stage, files=[stage/rel for rel in outputs])
        if problems:
            raise ValueError(f'publication blocked by {len(problems)} leakage finding(s)')
        replacements, applied, preserved = [], [], set()
        try:
            for rel, destination in destinations.items():
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(prefix='.vkm-publication-', dir=destination.parent)
                os.close(descriptor)
                backup = None
                if destination.exists():
                    descriptor, backup_name = tempfile.mkstemp(prefix='.vkm-backup-', dir=destination.parent)
                    os.close(descriptor)
                    backup = Path(backup_name)
                replacements.append((Path(temporary), destination, backup))
                if backup is not None:
                    shutil.copy2(destination, backup)
                shutil.copyfile(stage/rel, temporary)
            if before_replace is not None:
                before_replace()
            for temporary, destination, backup in replacements:
                os.replace(temporary, destination)
                applied.append((destination, backup))
        except OSError as publication_error:
            unrestored = []
            for destination, backup in reversed(applied):
                try:
                    if backup is None:
                        destination.unlink(missing_ok=True)
                    else:
                        os.replace(backup, destination)
                except OSError:
                    if backup is not None:
                        preserved.add(backup)
                    unrestored.append({'target': destination.relative_to(root.resolve()).as_posix(),
                        'backup': backup.relative_to(root.resolve()).as_posix() if backup else None})
            if unrestored:
                marker = root/'work/publication_recovery.json'
                try:
                    marker.write_text(json.dumps({'schema': 'vkm.publication_recovery/1',
                        'unrestored': unrestored}, indent=2, sort_keys=True) + '\n', encoding='utf8')
                except OSError:
                    pass  # Preserved backups must survive even if the recovery marker cannot be written.
                raise ValueError('publication rollback incomplete; original backups preserved; recovery required') from publication_error
            raise
        finally:
            for temporary, _, backup in replacements:
                temporary.unlink(missing_ok=True)
                if backup is not None and backup not in preserved:
                    backup.unlink(missing_ok=True)
