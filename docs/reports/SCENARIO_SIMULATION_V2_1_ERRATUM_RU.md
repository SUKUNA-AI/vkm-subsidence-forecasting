# Scenario simulation v2.1 — DATA ERRATUM V2-REP-001

Status: **PASS**. Новый dataset `SKRU1_SCENARIO_SIMULATION_V2_1` supersedes v2
для будущих experiments. Frozen v2 и commit ffcf869 не изменены. До обнаружения
ошибки benchmark на v2 не запускался. Модели, scoring, legacy holdouts: 0.

Причина: в v2 `int((calendar_days / 365.25) * 365.25)` усекал floating-point
погрешность вниз. V2.1 получает integer days непосредственно из разницы дат;
rate использует days/365.25. round() не применяется.

Проверены все 321766 origins. Исправлены ровно 31283 ячейки
`days_since_previous_observation`, каждая +1; оставшиеся 290483 неизменны.
Elapsed-day mismatches: 0. Affected sample IDs точно совпали с сохранённым
`artifacts/data_quality/scenario_representation_v2/elapsed_days_mismatches.csv.gz`.
Остальные 15 predictors, IDs/dates, rate, midpoint acceleration, uncertainty,
profile aggregates и campaign types совпадают точно. Unexpected differences: 0.

Все таблицы scenario/point/campaign, causal history, sequence windows, split
assignments, observed targets и оба evaluator truth payloads побайтово идентичны.
Из 20 artifacts: 14 BYTE_IDENTICAL, 3 EXPECTED_CHANGED (model feature, его
distribution summary и новые validation checks), 3 SEMANTICALLY_IDENTICAL_REVERSIONED
(card/config/manifest). Детали: `artifacts/data_quality/scenario_simulation_v2_1/output_parity.csv`.

Mean observed-history gap: 204.671997662898 → 204.769220489424 days;
std (ddof=1): 111.528536707548 → 111.593136095263 days.
Все остальные distribution sections идентичны. Семь доноров Мусихина, Babayants
stress semantics, seeds, mixture, laws, missingness/noise, geometry, target/split
semantics и allowlist сохранены. 640 scenarios, 128 world IDs / 127 distinct fields,
42 points, 14 profiles, 29 campaigns; outer roles 122047/35148/133833/30738.

Выполнены две независимые генерации в work/scenario_v2_1_erratum/build01 и build02,
затем генерация финального release из пустого destination. Все 20 файлов, включая
manifest, совпали byte-for-byte во всех трёх каталогах. 51 data-only test passed
(15 новых, 36 существующих). Все 1307 первоначальных файлов сохранили hashes,
включая BLOCKED reports/evidence и representation draft.

Команды воспроизведения (повтор требует новых scratch destinations):

```powershell
.venv/Scripts/python.exe scripts/generate_scenario_v2_1.py --output work/scenario_v2_1_erratum/build01
.venv/Scripts/python.exe scripts/generate_scenario_v2_1.py --output work/scenario_v2_1_erratum/build02
.venv/Scripts/python.exe scripts/generate_scenario_v2_1.py
.venv/Scripts/python.exe scripts/validate_scenario_v2_1_erratum.py --output work/scenario_v2_1_erratum/differential_final.json
.venv/Scripts/python.exe scripts/finalize_scenario_v2_1_erratum.py
```

Old manifest: `7bb67939314fbcf548686893e6868c90a2303e1303c401377f197e077fec8c13`.
New manifest: `a268cd7800e1320a1cc61172f95cf458c7ac7434237b32b288f23a83a97a2c95`.
Constraints manifest: `96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef`.

Machine authority: `artifacts/data_quality/scenario_simulation_v2_1/correction_receipt.json`.
Representation acceptance разрешено продолжить только на v2.1; benchmark остаётся
отдельным заданием. Commit/push здесь не выполнялись.
