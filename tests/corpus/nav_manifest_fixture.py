"""A complete file-identity manifest for synthetic NAV fixtures (no corpus data)."""
import json

import pyarrow.parquet as pq

from vkm_corpus.navigation.manifest import MANIFEST_FORMAT, sha256_file


def write_nav_manifest(directory, snapshot_id):
    datasets = {}
    for path in sorted(directory.glob("*.parquet")):
        table = pq.read_table(path)
        datasets[path.stem] = {"path": path.name, "rows": table.num_rows, "columns": table.schema.names,
                               "sha256": sha256_file(path)}
    manifest = {"format": MANIFEST_FORMAT,
                "snapshot": {"snapshot_id": snapshot_id, "manifest_sha256": "ab" * 32,
                             "pipeline_version": "synthetic"}, "datasets": datasets}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest
