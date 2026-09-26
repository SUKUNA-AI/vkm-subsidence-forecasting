# Math/physics foundation protocol (wave 1, pre-OGS)

Project: VKM / SKRU-1 (Верхнекамское месторождение; diploma special part «Алгоритм прогнозирования
оседаний земной поверхности на ВКМ на основе маркшейдерских измерений»). The project is being reset to
an evidence-backed 3D+time WorldSpec (WORLD / PROCESS MODEL / OBSERVATION MODEL / VALIDATION separated;
OGS etc. are future solvers, NOT the world). This run is PRE-OGS: no OGS, no ML, no forecasting benchmark,
no calibration on test/evaluator truth. Goal of your stream: a rigorous mathematical foundation +
executable, unit-checked calculation code + honest applicability statements.

## Evidence you may use
- Existing evidence release (read-only): `/home/user/vkm-subsidence-forecasting_resourses/10_physics_evidence/physical_evidence_v1/`
  (claims.jsonl 3019 records with source/page/quote; parameters.csv; creep_law_registry.csv (22 laws);
  layer_property_consolidation.csv; layer_aggregation.csv; engineering_rules.csv; pillar_method_crosscheck.csv;
  evidence_gaps.csv; derived_inputs/lebedeva_2023_table3_1_borehole_roofs.csv; external_sources.csv).
- Corpus page texts: `/home/user/work/corpus/<SOURCE_ID>/pNNNN.txt`; OCR for VKM-SRC-025/037 at
  `/home/user/vkm-subsidence-forecasting_resourses/work/ocr/<SID>/pNNNN.txt`. Source list:
  `/home/user/vkm-subsidence-forecasting_resourses/00_registry/SOURCE_REGISTER.csv`.
- New sweep results appear progressively in `/home/user/work/run/sweep/<SID>/<chunk>/records.jsonl`
  (kinds: formula, rheology_law, parameter, ...). Use them if present; do not wait for them.
- Page rendering for visual checks: `python /home/user/work/run/tools/render_page.py <SID> <page> [dpi]` then Read the PNG.
- Web: you MAY use WebSearch/WebFetch or Firecrawl (load via ToolSearch) to verify the ORIGINAL form of a
  formula (variable definitions, sign conventions, units) — record the URL/DOI. Context7 for library APIs.
- Python venv: `. /home/user/.venv-research/bin/activate` (numpy, scipy, sympy, mpmath, pint, pandas,
  scikit-learn, statsmodels, SALib, gstools, pykrige, pyproj, shapely, matplotlib, pyvista, pydantic, pytest).

## Where to write
- Code: PUBLIC repo `/home/user/vkm-subsidence-forecasting/src/vkm_world/<subpackage>/<module>.py` — ONLY the
  files assigned to you (create `__init__.py` in your subpackage if missing; never edit files owned by others;
  do not touch `src/skru1/`). Pure functions, numpy-vectorised where natural, explicit SI units in docstrings,
  pint-aware helpers where useful (shared unit registry lives in `src/vkm_world/core/units.py`, owned by
  the STRESS/DIMENSIONS stream; others import `from vkm_world.core.units import ureg, Q_`; if it does not exist
  yet, create a minimal one ONLY if you are the stress stream; otherwise use `pint.UnitRegistry()` locally and
  note it).
- Tests: `/home/user/vkm-subsidence-forecasting/tests/world/test_<your_module>.py` (unique basenames). Run them:
  `cd /home/user/vkm-subsidence-forecasting && . /home/user/.venv-research/bin/activate && python -m pytest -q tests/world/test_<...>.py -p no:cacheprovider`. They must pass.
  pytest config: `pythonpath=["src"]` already set in pyproject.
- Registry rows: `/home/user/work/run/math/registry_<STREAM>.csv` with EXACTLY this header:
  `model_id,name_ru,physical_meaning_ru,math_class,equation_plain,equation_latex,variables,domain,initial_conditions,boundary_conditions,assumptions,source_ids,locator,origin,validity_range,site_applicability,numerical_options,worldspec_role,execution_status,dim_test,code_ref,status,notes`
  - math_class ∈ ALGEBRAIC|SYSTEM_ALGEBRAIC|ODE|DAE|PDE_ELLIPTIC|PDE_PARABOLIC|PDE_HYPERBOLIC|PDE_SYSTEM|INTEGRAL_EQUATION|INTEGRO_DIFFERENTIAL|FRACTIONAL|VARIATIONAL|OPERATOR|KERNEL|GREEN_FUNCTION|CONVOLUTION|SERIES|SPECTRAL|STOCHASTIC|STATISTICAL|GEOMETRIC|OPTIMIZATION
  - variables: `symbol:meaning[unit]; ...` (SI units or explicit non-SI as in source)
  - origin ∈ ORIGINAL_SOURCE|CITED_IN_SOURCE|TEXTBOOK_STANDARD|DERIVED_THIS_RUN|EXTERNAL_PRIMARY
  - execution_status ∈ COMPUTED_3D|COMPUTED_2D_SECTION|COMPUTED_LOCAL|ANALYTICAL|SEMI_ANALYTICAL|EMPIRICAL|COUPLED_PROCESS|BOUNDARY_CONDITION|INITIAL_CONDITION|OBSERVATION_OPERATOR|CONSTRAINT_CHECK|CONTEXT_ONLY|RESEARCH_REQUIRED|UNKNOWN
  - status ∈ FACT|DERIVATION|INTERPOLATION|MODEL_CHOICE|ENGINEERING_ASSUMPTION|ANALOGUE|UNKNOWN (status of the equation's applicability to SKRU-1, not of its textbook truth)
  - dim_test: name of the pytest function that checks dimensional consistency, or NA with reason
  - code_ref: `vkm_world.<pkg>.<module>:<function>` or empty
  - quote fields containing commas with standard CSV quoting (use python csv module).
- Narrative: `/home/user/work/run/math/<STREAM>_RU.md` — in RUSSIAN, rigorous, with equations (LaTeX in $$),
  explicit assumptions, applicability to VKM/SKRU-1, what is FACT vs MODEL_CHOICE vs UNKNOWN, numeric results
  of your computations with their inputs and provenance, and "что не доказано".
- Figures/numeric outputs: `/home/user/work/run/math/figs/<STREAM>_*.png` and `/home/user/work/run/math/out/<STREAM>_*.csv|json`.

## Rules
- Every numeric input has provenance (source_id + page/evidence_id) or an explicit status
  (MODEL_CHOICE/ENGINEERING_ASSUMPTION/UNKNOWN). Ranges are not collapsed into points without reason.
- LAB ≠ MASSIF; analogue ≠ SKRU-1; normative ≠ measured; interpolation ≠ observation.
- Do not add mathematical machinery for decoration: for each apparatus say concretely which project
  problem it serves, or mark it CONTEXT_ONLY.
- Dimensional analysis is mandatory for every implemented equation (pint-based tests).
- Final answer: a compact JSON-like summary: files written, n registry rows, key numeric results, open issues.
