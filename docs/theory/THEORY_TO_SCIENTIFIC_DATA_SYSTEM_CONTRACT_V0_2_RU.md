# Контракт между исследовательской физикой и научной системой данных

**Проект:** VKM / SKRU-1  
**Версия:** 0.2  
**Статус:** scientific data architecture contract  
**Заменяет:** `THEORY_TO_SCIENTIFIC_DATA_SYSTEM_CONTRACT_V0_1_RU.md`

---

# 0. Главный принцип

Scientific data system должна позволять развивать theory позже, но не должна замораживать сегодняшние research hypotheses как database truth.

Canonical direction:

```text
RAW
→ DOCUMENT OBJECTS
→ REVIEWED EVIDENCE
→ PHYSICAL KNOWLEDGE
→ PHYSICAL WORLDS
→ WORLD REPRESENTATIONS
→ OBSERVATION WORLDS
→ RUNS / DATASETS / VALIDATION
```

---

# 1. Что меняется в v0.2

Ключевые новые различия:

```text
PhysicalWorld
!= WorldRepresentation

PhysicalWorld
!= ObservationWorld

PhysicalEntity
!= Microkernel

PhysicalHypothesis
!= ForecastMethod

WorldUncertainty
!= RepresentationError
```

Data model должна это поддерживать.

---

# 2. Текущий implementation scope

Для Corpus Platform v0 реально строится прежде всего:

```text
DOCUMENT LAYER
```

Будущие scientific layers должны быть extension-compatible, но не обязаны создаваться сейчас.

---

# 3. Пять logical layers

## L0 RAW

Бинарные sources.

## L1 DOCUMENT

Pages/blocks/figures/tables/formulas/bibliography.

## L2 EVIDENCE

Reviewed claims/measurements/experiments/derivations.

## L3 PHYSICAL KNOWLEDGE

Fields/laws/parameters/materials/couplings.

## L4 WORLDS / REPRESENTATIONS / OBSERVATIONS

PhysicalWorld, WorldRepresentation, ObservationWorld, SolverRun, datasets.

---

# 4. Четыре разных graph semantics

Даже если позже все находятся в Neo4j, edge semantics различаются.

## Document/provenance graph

```text
Source
→ Page
→ Figure/Table/Formula
```

## Knowledge/evidence graph

```text
Claim
→ supports
→ Law/Parameter
```

## Physical interaction graph

```text
PhysicalEntity/Field
→ coupling
→ PhysicalEntity/Field
```

## Representation graph

```text
WorldRepresentation
→ Microkernel
→ representation edge
→ Microkernel
```

Observation graph отдельно связывает physical trajectory с ObservationWorld.

---

# 5. Neo4j v0

В первой Corpus Platform строится только document/bibliographic projection.

Nodes:

```text
Work
Source
Page
Block
Figure
Table
Formula
BibliographyEntry
Author
Venue
```

Relations:

```text
Source INSTANCE_OF Work
Work AUTHORED_BY Author
Source HAS_PAGE Page
Page HAS_BLOCK Block
Page HAS_FIGURE Figure
Page HAS_TABLE Table
Page HAS_FORMULA Formula
Work CITES Work
```

Physics graph сейчас не создавать.

---

# 6. Work vs Source

`Work` — intellectual/bibliographic work.

`Source` — конкретный acquired binary file.

Это обязательно для:

- duplicates;
- scans;
- editions;
- preprint vs article;
- reprints.

---

# 7. Stable IDs

Scientific IDs не должны зависеть от Neo4j internal ids или row offsets.

---

# 8. Canonical storage

Canonical structured layer:

```text
Arrow / Parquet
```

DuckDB:

```text
query projection
```

Neo4j:

```text
graph projection
```

OpenSearch:

```text
retrieval projection
```

Все projections rebuildable.

---

# 9. Source

Минимум:

```yaml
source_id:
work_id:
sha256:
canonical_path:
source_type:
review_status:
site_scope:
```

---

# 10. Page

```yaml
page_id:
source_id:
page_number:
native_text_status:
ocr_status:
render_artifact:
quality_flags:
processing_run_id:
```

---

# 11. Figure/Table/Formula

Каждый object должен иметь:

```text
object_id
source_id
page_id
bbox
artifact_id
raw extraction
normalized extraction
quality flags
review status
processing provenance
```

---

# 12. Auto extraction status

Автоматическая обработка:

```text
AUTO_EXTRACTED_UNREVIEWED
```

не:

```text
FACT
```

---

# 13. Evidence future layer

Future:

```text
Claim
Measurement
Experiment
Specimen
Derivation
Conflict
Transfer
```

Но Corpus v0 не обязан их автоматически создавать.

---

# 14. PhysicalQuantity

Future first-class entity:

```yaml
quantity_id:
canonical_name:
dimension:
canonical_unit:
tensor_rank:
```

---

# 15. Formula scientific layer

Document Formula object != reviewed scientific Formula.

Document Formula:

```text
OCR/located expression
```

Reviewed Formula:

```text
semantic variables
units
assumptions
validity
provenance
```

Не смешивать.

---

# 16. PhysicalWorld

Future schema:

```yaml
PhysicalWorld:
  world_id:
  world_family:
  geometry_realization:
  geology_realization:
  physical_hypotheses:
  constitutive_laws:
  parameter_sets:
  initial_state:
  boundary_conditions:
  event_history:
  completion_provenance:
```

PhysicalWorld не содержит representation choice.

---

# 17. WorldRepresentation

Future separate entity:

```yaml
WorldRepresentation:
  representation_id:
  world_id:
  representation_type:
  purpose:
  horizon:
  tolerance:
  discretization:
  reduction_method:
  representation_error_model:
  provenance:
```

---

# 18. MicrokernelPartition

```yaml
MicrokernelPartition:
  partition_id:
  representation_id:
  kernel_ids:
  split_merge_history:
  validation_refs:
```

Не parent непосредственно к PhysicalWorld без representation.

---

# 19. PhysicalEntity

Future ontology:

```text
PILLAR
CHAMBER
LAYER
BACKFILL
FAULT
BOREHOLE
BLOCK
PANEL
```

Это evidence/geometry entities.

---

# 20. Microkernel

Future representation entity:

```yaml
Microkernel:
  kernel_id:
  representation_id:
  physical_entity_refs:
  geometry_ref:
  state_schema:
  active_laws:
  port_refs:
```

Это не physical entity.

---

# 21. Mapping

Нужны typed relations:

```text
Microkernel REPRESENTS PhysicalEntity
Microkernel REPRESENTS_PART_OF PhysicalEntity
Microkernel AGGREGATES PhysicalEntity
```

---

# 22. ObservationWorld

Future:

```yaml
ObservationWorld:
  observation_world_id:
  modalities:
  station_geometry:
  epochs:
  covariance_model:
  missingness_model:
  nuisance_parameters:
  available_from_rules:
```

Отдельно от PhysicalWorld.

---

# 23. ObservationDataset

```yaml
ObservationDataset:
  dataset_id:
  observation_world_id:
  type: REAL | SYNTHETIC | SEMI_SYNTHETIC
```

---

# 24. SyntheticObservationDataset

```yaml
SyntheticObservationDataset:
  dataset_id:
  world_id:
  representation_id:
  observation_world_id:
  solver_run_id:
  noise_seed:
  model_discrepancy_ref:
```

---

# 25. SolverRun

```yaml
SolverRun:
  run_id:
  world_id:
  representation_id:
  solver:
  solver_version:
  config_hash:
  input_manifest:
  output_manifest:
```

---

# 26. ForecastMethod

Future entity separate from PhysicalWorld:

```yaml
ForecastMethod:
  method_id:
  family:
  configuration:
  training_lineage:
  calibration_lineage:
```

Normative method/ML predictor не становится PhysicalWorld.

---

# 27. EpistemicRule

Possible future:

```text
TransferRule
CompatibilityRule
ValidationRule
CalibrationRule
```

Это policy entities, не physics.

---

# 28. World-family results

Future reports должны позволять metrics:

```text
per world
per family
across families
```

Не скрывать subjective family weighting.

---

# 29. Prior

Если:

\[
\pi(w)
\]

используется, metadata должна хранить:

```text
prior source
prior rationale
status = MODEL_CHOICE
```

---

# 30. NROY

Future system должна позволить хранить:

```text
NOT_RULED_OUT_YET
RULED_OUT
```

на основе explicit comparison experiment.

---

# 31. D_obs future object

Можно предусмотреть generic validation metric:

```yaml
ComparisonMetric:
  metric_id:
  metric_type:
  covariance_ref:
  observation_world_id:
```

Но не hardcode конкретную Mahalanobis theory в Corpus v0.

---

# 32. ValidationExperiment

Future:

```yaml
ValidationExperiment:
  experiment_id:
  world_sets:
  held_out_families:
  truth_solver:
  method_under_test:
  observation_world:
  calibration_sources:
  validation_sources:
  model_discrepancy:
```

---

# 33. Inverse-crime metadata

System later должна уметь определить:

```text
same generator family?
same solver?
same resolution?
same calibration observations?
```

Но сейчас это only future contract.

---

# 34. Ensemble coverage

Future Experiment type:

```text
ENSEMBLE_COVERAGE
```

с comparison real observations vs synthetic envelope.

---

# 35. Observational indistinguishability

Future:

```text
WORLD_PAIR / FAMILY_PAIR
+
ObservationWorld
+
metric
```

может хранить discrimination results.

---

# 36. Provenance graph

Не смешивать:

```text
scientific provenance
processing provenance
computation provenance
```

---

# 37. Processing provenance

Для Corpus v0:

```text
source_sha
pipeline_version
extractor
model_id
model_revision
config_hash
processing_run_id
```

---

# 38. Scientific provenance

Future:

```text
source/page/table/equation
reviewer
epistemic class
```

---

# 39. Computation provenance

Future:

```text
world
representation
solver
git commit
environment
seed
```

---

# 40. Geometry status

Обязательно сохраняются:

```text
EXACT_COORDINATED
LOCAL_COORDINATES
UNKNOWN_CRS
MAP_DIGITIZED
RELATIVE
SCHEMATIC
UNKNOWN
```

---

# 41. Digitization

Digitized value/geometry:

```text
DERIVATION
```

не source FACT.

---

# 42. Time semantics

Различать:

```text
event_time
measurement_time
processing_time
publication_time
available_from
ingestion_time
```

---

# 43. Calibration lineage

Future:

```text
parameter
CALIBRATED_FROM
dataset
```

---

# 44. Validation lineage

Future:

```text
method
VALIDATED_AGAINST
dataset
```

System должна позволять обнаружить same-data circularity.

---

# 45. ParameterSet

Sample whole compatible parameter sets.

Не собирать independent random numbers из несовместимых experiments.

---

# 46. Source scope

Сохранять existing project scopes.

Не путать:

```text
GENERAL_METHOD
SKRU1_EXACT
OTHER_VKM_SITE
ANALOG
```

---

# 47. Review state

Минимум:

```text
UNSEEN
QUICK_LOOK_ONLY
AUTO_EXTRACTED_UNREVIEWED
RELEVANT_SECTIONS_REVIEWED
FULLY_REVIEWED
```

---

# 48. UNKNOWN

Future UNKNOWN first-class:

```yaml
unknown_id:
subject:
field:
reason:
consequence:
world_axis:
```

UNKNOWN в WorldSpec порождает possible completions.

---

# 49. Corpus v0 не должен создавать speculative worlds

Document ingestion не должен автоматически интерпретировать unknowns и генерировать worlds.

---

# 50. OpenSearch

Retrieval projection.

Не canonical truth.

Search result возвращает stable IDs, по которым можно вернуться в canonical data.

---

# 51. Neo4j extension model

Позже document nodes могут получить новые relations:

```text
Claim SUPPORTED_BY Page
Measurement EXTRACTED_FROM Table
Law DEFINED_BY Formula
```

и затем:

```text
PhysicalWorld USES Law
WorldRepresentation REPRESENTS PhysicalWorld
```

Старые document nodes не перестраиваются.

---

# 52. Несколько logical graphs в одном Neo4j

Даже в одной physical DB различать labels/edge types:

```text
DOCUMENT
EVIDENCE
PHYSICS
REPRESENTATION
OBSERVATION
PROVENANCE
```

Не нужно отдельной database на каждый logical layer.

---

# 53. Rebuildability

Document graph полностью rebuildable из canonical layer.

Future scientific graph должен быть rebuildable из reviewed canonical scientific records.

---

# 54. MCP layer

MCP должен отдавать semantic operations, не raw DB shell.

Corpus v0:

```text
get_source
get_page
get_figure
get_formula
search_text
get_citations
rerank_text
rerank_visual
trace_document_provenance
```

Future tools могут добавляться без изменения document layer.

---

# 55. CAD/vector layer

Native vector extraction сохраняется как derived artifact with provenance.

AutoCAD/Civil/QGIS outputs не заменяют original source.

---

# 56. Formula implementation future

Связь:

```text
ReviewedFormula
→ FormulaImplementation
```

не нужна в Corpus v0, но schema должна быть extension-friendly.

---

# 57. Unit contract future

Reviewed scientific formulas/measurements имеют units/dimensions.

Document OCR formulas — ещё нет.

---

# 58. Research candidates не database truth

Не создавать production nodes:

```text
T0_PROVEN
T1_PROVEN
G1_TRUE
```

пока нет review/result.

Если later нужно хранить:

```yaml
ResearchStatement:
  status: CANDIDATE | TESTED | REJECTED | SUPPORTED
```

---

# 59. World-0

Future record:

```text
PhysicalWorld family
+
WorldRepresentation type = MESO_LAYER_OVERBURDEN_SURFACE
```

Не превращать World-0 model в единственный world schema.

---

# 60. Data architecture implementation order

Сейчас:

1. document contracts;
2. processing provenance;
3. Parquet;
4. DuckDB;
5. Neo4j document projection;
6. OpenSearch;
7. MCP.

Потом:

8. evidence;
9. physical knowledge;
10. worlds;
11. representations;
12. observations;
13. solver runs.

---

# 61. Anti-patterns

Не делать:

```text
one giant Graph node schema
one universal Relation(type="related")
one table with JSON everything
embedding = canonical truth
microkernel = physical object
world = solver run
world = observation dataset
method = world
```

---

# 62. v0.2 status

```text
Document graph first                    ACCEPTED
PhysicalWorld separation                ACCEPTED_DRAFT
WorldRepresentation separation          ACCEPTED_DRAFT
ObservationWorld separation             ACCEPTED_DRAFT
PhysicalEntity vs Microkernel separation ACCEPTED_DRAFT
Research theory in production schema    PROHIBITED_FOR_NOW
Future extension points                 ACCEPTED
```

---

# 63. Короткая canonical chain

```text
Source
↓
DocumentObject
↓
ReviewedEvidence
↓
PhysicalKnowledge
↓
PhysicalWorld
↓
WorldRepresentation
↓
SolverRun
↓
PhysicalTrajectory
↓
ObservationWorld
↓
ObservationDataset
↓
Validation / Forecast
```

Эта цепочка является главным data-contract v0.2.
