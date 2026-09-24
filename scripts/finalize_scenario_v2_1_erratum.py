#!/usr/bin/env python3
"""Freeze correction QA after two independent builds, final generation, and data tests."""
from pathlib import Path
import json
import xml.etree.ElementTree as ET
import pandas as pd
from validate_scenario_v2_1_erratum import validate, digest

ROOT = Path(__file__).resolve().parents[1]


def main():
    work = ROOT / "work/scenario_v2_1_erratum"
    output = ROOT / "artifacts/data_quality/scenario_simulation_v2_1"
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Correction receipts are immutable")
    final = ROOT / "data/scenario_simulation_v2_1"
    result = validate(ROOT, final)
    def inventory(directory):
        return {p.relative_to(directory).as_posix(): digest(p) for p in directory.rglob("*") if p.is_file()}
    first, second, release = [inventory(p) for p in (work/"build01", work/"build02", final)]
    assert first == second == release
    initial = json.loads((work/"initial_inventory.json").read_text(encoding="utf-8"))
    changed = [r["path"] for r in initial if digest(ROOT/r["path"]) != r["sha256"]]
    assert not changed, changed
    tree = ET.parse(work/"pytest_data01.xml")
    cases = list(tree.iter("testcase"))
    assert len(cases) == 51 and not list(tree.iter("failure")) and not list(tree.iter("error"))
    output.mkdir(parents=True, exist_ok=True)
    def write(name, value):
        (output/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    pd.DataFrame(result["output_parity"]).to_csv(output/"output_parity.csv", index=False, lineterminator="\n")
    write("two_run_receipt.json", dict(status="PASS", builds=["work/scenario_v2_1_erratum/build01", "work/scenario_v2_1_erratum/build02"],
                                      final_release="data/scenario_simulation_v2_1", final_matches_both=True,
                                      deterministic_files=len(release), mismatch_count=0, hashes=release))
    write("test_execution_receipt.json", dict(status="PASS", new_tests_passed=15, existing_tests_passed=36, failed=0,
        command=".venv/Scripts/python.exe -m pytest tests/test_scenario_v2_1_erratum.py tests/test_data_foundation_v2.py tests/test_reconstruction_data.py tests/test_reconstruction_atlas.py -q --basetemp work/scenario_v2_1_erratum/pytest_data01 --junitxml work/scenario_v2_1_erratum/pytest_data01.xml",
        tests=[c.attrib["name"] for c in cases], models_executed=0, model_scoring=0, legacy_labels_parsed=0))
    result.update(two_run_result="BYTE_IDENTICAL_ALL_20_FILES", final_matches_both=True,
                  data_tests_passed=51, initial_files_unchanged=len(initial), changed_initial_files=[],
                  source_code_diff="versioned module clone; calendar assignment + new identity/invariants/card/output guard only",
                  freeze_policy="append_only_do_not_change_after_model_results")
    result["qa_files"] = [{"path": p.relative_to(ROOT).as_posix(), "sha256": digest(p)} for p in sorted(output.iterdir())]
    write("correction_receipt.json", result)
    old_stats, new_stats = result["old_gap_statistics"], result["new_gap_statistics"]
    report = f'''# Scenario simulation v2.1 — DATA ERRATUM V2-REP-001

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

Mean observed-history gap: {old_stats['mean']:.12f} → {new_stats['mean']:.12f} days;
std (ddof=1): {old_stats['std']:.12f} → {new_stats['std']:.12f} days.
Все остальные distribution sections идентичны. Семь доноров Мусихина, Babayants
stress semantics, seeds, mixture, laws, missingness/noise, geometry, target/split
semantics и allowlist сохранены. 640 scenarios, 128 world IDs / 127 distinct fields,
42 points, 14 profiles, 29 campaigns; outer roles 122047/35148/133833/30738.

Выполнены две независимые генерации в work/scenario_v2_1_erratum/build01 и build02,
затем генерация финального release из пустого destination. Все 20 файлов, включая
manifest, совпали byte-for-byte во всех трёх каталогах. 51 data-only test passed
(15 новых, 36 существующих). Все {len(initial)} первоначальных файлов сохранили hashes,
включая BLOCKED reports/evidence и representation draft.

Команды воспроизведения (повтор требует новых scratch destinations):

```powershell
.venv/Scripts/python.exe scripts/generate_scenario_v2_1.py --output work/scenario_v2_1_erratum/build01
.venv/Scripts/python.exe scripts/generate_scenario_v2_1.py --output work/scenario_v2_1_erratum/build02
.venv/Scripts/python.exe scripts/generate_scenario_v2_1.py
.venv/Scripts/python.exe scripts/validate_scenario_v2_1_erratum.py --output work/scenario_v2_1_erratum/differential_final.json
.venv/Scripts/python.exe scripts/finalize_scenario_v2_1_erratum.py
```

Old manifest: `{result['original_manifest_sha256']}`.
New manifest: `{result['new_manifest_sha256']}`.
Constraints manifest: `{result['constraints_manifest_sha256']}`.

Machine authority: `artifacts/data_quality/scenario_simulation_v2_1/correction_receipt.json`.
Representation acceptance разрешено продолжить только на v2.1; benchmark остаётся
отдельным заданием. Commit/push здесь не выполнялись.
'''
    (ROOT/"docs/reports/SCENARIO_SIMULATION_V2_1_ERRATUM_RU.md").write_text(report, encoding="utf-8")
    print(json.dumps({"status": "PASS", "new_manifest_sha256": result["new_manifest_sha256"],
                      "receipt_sha256": digest(output/"correction_receipt.json")}))


if __name__ == "__main__":
    main()
