# SKRU-1: порядок работы

Текущая карта репозитория — [CANONICAL_RESEARCH_STATE_RU.md](docs/CANONICAL_RESEARCH_STATE_RU.md).
Historical baseline: Gate B3 / `ce8e57e`. Current data: scenario v2.1 и
representation v2.1 R1. R1 — дизайн будущего сравнения; R2 — authoritative
source/data/physics audit. Исторические scores не переносятся на новые данные.

До изменений прочитайте документы, перечисленные в [AGENTS.md](AGENTS.md).
Проверка входов и frozen hashes: `python scripts/verify_canonical_repository.py`.
Временные результаты создаются только в `work/`; первичные данные и ZIP
не изменяются на месте. Запуски моделей требуют отдельной задачи и протокола.

v2.1 поддержан с ограничениями как factorial stress benchmark. Атрибуция
доноров неоднозначна, reflector_seasonal — pure stress, доли temporal families
заданы инженерно. Physical world readiness — WEAK. Это не полевая валидация.

Старые B/C конфигурации, receipts и source inventories, где они сохранены,
описывают соответствующие исторические commits. Source resources пока
остаются здесь; перенос в private resources repository — отдельная задача.
