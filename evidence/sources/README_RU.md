# Источники: соответствие legacy-ID и реестра PRIVATE

`legacy_source_id_map.csv` сопоставляет ID старого PUBLIC-стека с каноническими ID PRIVATE-реестра
`00_registry/SOURCE_REGISTER.csv`. Старые ID: `SRC01`–`SRC11` из `configs/source_manifest.csv` и `SUP01` из
`configs/supplementary_source_manifest.csv`, оба на legacy-commit `d54025d`. Новые ID — `VKM-SRC-xxx`.
Сопоставление выполнено по точному совпадению SHA-256 и размера файла. Путей PRIVATE в таблице нет.

| Поле | Значение |
|---|---|
| `legacy_source_id`, `legacy_manifest` | старый ID и manifest, где он объявлен (`<путь>@<commit>`) |
| `legacy_file_name` | имя файла в старом `inputs/sources/**` (retired-дерево; байты живут в PRIVATE) |
| `legacy_authority_class`, `legacy_site_scope` | классификация старого стека (историческая; канон — реестр PRIVATE) |
| `vkm_src_id`, `sha256`, `size_bytes` | канонический ID и идентичность файла |
| `register_source_class`, `register_evidence_scope` | классификация из реестра PRIVATE на момент сборки |
| `match_method` | `sha256_exact+size` |

Входы: `configs/source_manifest.csv` (SHA-256 `8c9de321be4871e63f8300b2a38eaaea546e02cc24ad0ee1f0b20637e3eb5ceb`)
и `configs/supplementary_source_manifest.csv` (`9ed4885c5e4607fc6445ef4c01c35ab3a1ca70a81dc5301b9aee4a54f0a251ef`),
оба на legacy-commit `d54025d`. Третий вход — реестр PRIVATE: `SOURCE_REGISTER.csv`, SHA-256
`0d63373ff2f4e32cef6d23f016e07cfad4227c8fcb78ec42a1733cacbea428db` на 26.09.2026.

Пересборка и сверка:

```bash
VKM_RESOURCES_ROOT=<private checkout> python scripts/build_evidence_from_legacy.py sources [--check]
```

`scripts/verify_canonical_repository.py` при заданном `VKM_RESOURCES_ROOT` сверяет каждую строку
(`vkm_src_id` + `sha256`) с реестром PRIVATE (проверка `private:public_catalogues`).
