# Portable Path Policy

1. Resolve the project root from the directory containing `AGENTS.md` or from `--root`.
2. Read operational files only through relative paths recorded in `configs/input_manifest.csv` and `configs/source_manifest.csv`.
3. Never hard-code `/mnt/data`, `E:\Диплом`, user home directories or drive letters.
4. A file is `bundled_verified` only when it exists inside the extracted bundle and its size and SHA-256 match the manifest.
5. A stale absolute path is not evidence of file presence.
6. Any missing or mismatched file is a blocking failure.
