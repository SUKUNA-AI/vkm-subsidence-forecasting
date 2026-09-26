# VKM/SKRU-1 corpus re-sweep protocol (night run 2026-09-25/26)

You are one reader in a full re-inventory of the PRIVATE scientific corpus of the
diploma project «Горные и маркшейдерские работы при разработке Верхнекамского
месторождения» / special part «Алгоритм прогнозирования оседаний земной поверхности
на ВКМ на основе маркшейдерских измерений». The project is being RESET from
"one borehole (No. 75) → reduced 2D Physical World → OGS" to
"all spatial observations → evidence-backed 3D+time WorldSpec → later solver choices".
Your job: read your assigned chunk DEEPLY and extract EVERYTHING that could matter for a
3D+time world of the Verkhnekamsk deposit (ВКМКС) and the SKRU-1 mine, its
information lifecycle and its future physics/observation operators.

## Inputs
- Registered originals (read-only): `/home/user/vkm-subsidence-forecasting_resourses/<canonical_path>`
  (see `00_registry/SOURCE_REGISTER.csv`). NEVER modify anything in either repository.
- Per-page text layer: `/home/user/work/corpus/<SOURCE_ID>/pNNNN.txt` (PDF page numbering, 1-based).
- OCR pages (VKM-SRC-025 book with no text layer; VKM-SRC-037 djvu, 2-up spreads):
  `/home/user/vkm-subsidence-forecasting_resourses/work/ocr/<SOURCE_ID>/pNNNN.txt` (+ `.tsv` word confidences).
- VKM-SRC-023 (Filatova VKR, docx): rendered to `/home/user/work/corpus/VKM-SRC-023/filatova.pdf`
  (115 pages; per-page text `pNNNN.txt` in the same dir; locator = "rendered p.N" + section/figure number);
  markdown with inline image refs `all.md`; images in `media/media/imageN.(png|jpeg)`.
- Existing evidence (previous consolidation, 3019 records) digest for your source:
  `/home/user/work/run/existing/<SOURCE_ID>.tsv`. It is NOT a substitute for reading. Mark
  whether each item you extract duplicates an existing evidence_id.
- Tools (use the venv: `. /home/user/.venv-research/bin/activate`):
  - `python /home/user/work/run/tools/grep_vocab.py <SID> <first> <last>` — domain-vocabulary hit map per page.
  - `python /home/user/work/run/tools/render_page.py <SID> <page> [dpi] [--clip x0,y0,x1,y1]` — renders a page
    (or a fractional clip) to PNG; then LOOK at it with the Read tool. Use 110–150 dpi for overview,
    200–300 dpi + clip for small digits in tables/figures.

## Mandatory reading procedure for your chunk
1. If your chunk contains the title page / table of contents / bibliography: record bibliographic data
   (title, authors, year, publisher/venue, type, pages), the TOC and the chapters relevant to the project.
2. Run grep_vocab over the whole chunk to see where the domain content is.
3. READ every page of your chunk in the text/OCR layer (not only keyword hits). Pages with substantive
   domain content must be read fully; clearly irrelevant pages (e.g. equipment spare-part lists) may be
   skimmed but must be listed as skimmed with the reason.
4. For every table, figure, map, section/cross-section, borehole column, chart and equation that carries
   project-relevant information: render the page and look at it. Numbers from OCR or from tables-as-images
   require visual confirmation; record the check result.
5. Record every bibliography entry of the chunk (if the chunk contains the reference list) as `citation`
   records; for in-text references record which claim relied on which reference number.
6. Write outputs (below) and return a short summary.

## What to extract (record kinds)
Write one JSON object per line into `records.jsonl`. Common fields for EVERY record:
```
{"kind": "...", "source_id": "VKM-SRC-0xx", "pdf_page": 12, "printed_page": "10" or null,
 "locator_extra": "Табл. 2.3 / Рис. 1.4 / формула (3.2) / §2.1 / rendered p.N",
 "quote": "<=300 chars verbatim (original language), exact as in the source; for OCR fix nothing>",
 "extraction_method": "TEXT_LAYER|OCR|OCR_VISUALLY_CONFIRMED|VISUAL_READ|DOCX_TEXT|GRAPH_DIGITIZED_APPROX",
 "status": "FACT|DERIVATION|INTERPOLATION|MODEL_CHOICE|ENGINEERING_ASSUMPTION|ANALOGUE|UNKNOWN",
 "evidence_type": "MEASURED|FIELD_OBSERVATION|LAB_TEST|NORMATIVE|DESIGN_VALUE|CALCULATED_BY_AUTHOR|MODEL_CALIBRATED|TEACHING_EXAMPLE|LITERATURE_CITED|INTERPRETATION|DESCRIPTIVE",
 "scope": "SKRU1|SKRU2|SKRU3|SKRU1_SKRU2|BKPRU1|BKPRU2|BKPRU3|BKPRU4|USOLSKY|OTHER_VKM_SITE|VKM_REGIONAL|OTHER_POTASH_SITE|NON_VKM|GENERAL_METHOD|UNSTATED",
 "spatial_support": {"level": "deposit|district|mine|intermine_pillar|mine_field|panel|block|site|working|chamber|pillar|seam|interseam|geological_element|borehole|borehole_interval|gpr_profile|survey_line|benchmark|specimen|point|unstated", "name": "...", "coords": null},
 "temporal_support": {"event_date": null, "measurement_date": null, "publication_year": 2013, "notes": ""},
 "scale": "LAB|MASSIF|CALIBRATED_EFFECTIVE_MODEL|FIELD|DESIGN|NOT_APPLICABLE",
 "confidence": "HIGH|MEDIUM|LOW",
 "existing_evidence_ids": ["EV-..."] or [],
 "is_new": true/false,
 "data": { kind-specific, see below },
 "notes": "..."}
```
Kinds and their `data`:
- `claim` — any substantive statement (geology, process, mechanism, history, method) not better covered
  by a more specific kind. data: {"topic": "...", "claim_ru_or_en": "..."}
- `parameter` — any number with meaning. data: {"variable": "density|youngs_modulus|poisson_ratio|ucs|tensile_strength|cohesion|friction_angle|creep_param_<name>|thickness|depth|elevation|chamber_width|pillar_width|extraction_ratio|loading_degree|backfill_ratio|subsidence_max|subsidence_rate|angle_of_draw|boundary_angle|vp|vs|permittivity|conductivity|temperature|permeability|K0_lambda|stress|...", "symbol": "...", "value": "as printed", "value_point": x|null, "value_min": x|null, "value_max": x|null, "unit": "as printed", "lithology": "...", "layer_unit": "...", "n_samples": null, "conditions": "..."}
- `formula` — every equation / law / method formula. data: {"name": "...", "equation_plain": "exact form, ascii/unicode", "equation_latex": "...", "variables": [{"symbol":"", "meaning":"", "unit":""}], "equation_number": "(3.2)", "original_or_cited": "ORIGINAL|CITED|DERIVED_BY_AUTHOR", "cited_ref": "[12]", "domain": "rheology|elasticity|strength|subsidence|stress|mining_geometry|backfill|hydro|thermal|gpr|seismic|insar|levelling|geodesy|statistics|other", "validity": "...", "legibility": "CLEAR|PARTIAL|ILLEGIBLE"}
- `rheology_law` — any creep/relaxation/long-term strength law with its calibration. data: {"law_family": "Norton|power_law_time|exponential|hereditary_Abel|Maxwell|Kelvin_Voigt|Burgers|generalized|viscoplastic_Bingham|Perzyna|Duvaut_Lions|other", "equation_plain": "...", "parameters": [{"symbol":"","value":"","unit":""}], "calibration_range": "stress/time/temperature", "material": "...", "scale": "..."}
- `borehole` — EVERY borehole mentioned anywhere (text, table, figure, map). data: {"borehole_id_as_printed": "...", "aliases": [], "borehole_type": "exploration|hydrogeological|oil|underground|geotechnical|observation|unknown", "mine_or_area": "...", "coords": {"x": null, "y": null, "crs_as_printed": null}, "collar_elevation_m": null, "depth_m": null, "date": null, "picks": [{"unit": "...", "top_depth_m": null, "bottom_depth_m": null, "top_abs_m": null, "bottom_abs_m": null, "thickness_m": null}], "lithology_notes": "", "core": null, "lab_data": null, "logging": {"gamma": null, "acoustic": null, "vp": null, "other": null}, "figure_or_table": "...", "usable_for_3d": "YES|PARTIAL|NO", "why": "..."}
- `horizon_pick` — a single stratigraphic depth/elevation/thickness at a location if not attached to a named borehole record. data like one pick + location.
- `stratigraphic_unit` — definition of a unit (name, abbreviation, age, lithology, typical thickness range, ordering, where). data: {"unit": "...", "abbrev": "...", "above": "...", "below": "...", "thickness_range_m": "...", "lithology": "...", "notes": ""}
- `mining_geometry` — chambers, pillars, panels, blocks, seams mined, extraction ratio, dimensions, layouts, protective pillars. data: {"object": "...", "mine": "...", "panel_block": "...", "seam": "...", "chamber_width_m": null, "pillar_width_m": null, "chamber_height_m": null, "chamber_length_m": null, "extraction_ratio": null, "depth_m": null, "other": {}}
- `chronology_event` — any dated event: construction, shaft sinking, start/end of mining (mine/panel/seam/block), backfill, accident, flooding, monitoring campaign, publication of data, normative document in force. data: {"event": "...", "event_type": "construction|shaft|mining_start|mining_end|backfill|accident|flooding|hydro_change|monitoring_campaign|survey|normative|publication|other", "date_start": "YYYY[-MM[-DD]]", "date_end": null, "object": "..."}
- `backfill` — backfill material/volume/ratio/delay/compaction/properties. data: {"material": "", "method": "", "fill_ratio": null, "delay": null, "density": null, "compaction": null, "other": {}}
- `monitoring_observation` — any survey/monitoring data or description: levelling lines, benchmarks, profile lines, GNSS, InSAR, hydrostatic levelling, underground stations, convergence. data: {"method": "levelling|profile_line|GNSS|InSAR|hydrostatic|underground_station|convergence|GPR|seismic|borehole_log|other", "observable": "", "line_or_point_ids": [], "dates": "", "precision": "", "datum_crs": "", "site": "", "values": ""}
- `stress_measurement` — in-situ stress / lateral pressure coefficient / orientation. data: {"method": "", "depth_m": null, "sigma_v": null, "sigma_h": null, "lambda_or_K0": null, "orientation": null, "site": ""}
- `hydro` / `thermal` / `geophysics` / `gpr` / `seismic` — process/property/observation data of that domain. data: free dict with values+units.
- `spatial_entity` — named spatial objects (mines, fields, panels, blocks, pillars, lines, profiles, districts, faults, folds, geodynamic zones, protective pillars) with any location info. data: {"entity_type": "", "name": "", "parent": "", "location": "", "dimensions": ""}
- `coordinate_system` — any CRS/datum/height-system/axis convention info. data: {"system": "", "details": ""}
- `subsidence_method` — any method to compute subsidence/deformation (influence function, trough profile, angles, time function, normative). data: {"method": "", "equations_ref": "", "inputs": [], "assumptions": "", "calibration": "", "applicability": ""}
- `engineering_rule` — normative/design rule (admissible spans, pillar criteria, protective thickness...). data: {"rule": "", "applies_to": "", "values": ""}
- `process_mechanism` — any physical process described as acting (creep, damage, interface slip, dissolution, flooding, thermal, dynamic events...) with its causal role. data: {"process": "", "causal_path": "", "evidence_strength": ""}
- `citation` — bibliography entry. data: {"ref_number": "", "citation_text": "full entry as printed", "authors": "", "year": "", "title": "", "venue": "", "type": "article|book|dissertation|normative|report|web|other", "cited_for": "what this source uses it for (if determinable)"}
- `visual_check` — any page element whose value/attribution needs or received visual verification, or is not checkable. data: {"element": "Рис. 3.2 / Табл. 1", "issue_type": "VISUAL_NOT_CHECKABLE|OCR_ONLY|OCR_SKIMMED|TABLE_FROM_IMAGE|MAP_ATTRIBUTION|LABEL_ERROR|UNCLEAR_MINE_ATTRIBUTION|UNIT_DOUBT|EQUATION_ILLEGIBLE|GRAPH_DIGITIZED|FIGURE_REPRODUCED_FROM_OTHER_SOURCE|NUMBER_CONFLICT|OK_CONFIRMED", "existing_value": "", "text_layer_value": "", "ocr_value": "", "visual_value": "", "result": "", "affected_evidence_ids": []}
- `conflict_note` — contradiction with another source/claim or internal inconsistency. data: {"what": "", "with": "", "values": ""}
- `open_question` — something the project needs but the source leaves unknown. data: {"question": "", "why_it_matters": ""}

## Scientific rules (non-negotiable)
- UNKNOWN stays UNKNOWN; no data ≠ zero; mean ≠ profile; interpolation ≠ observation.
- Borehole 75 ≠ SKRU-1; one borehole ≠ deposit; lab specimen ≠ massif; neighbouring mine ≠ SKRU-1;
  normative value ≠ measurement; model output ≠ measurement; GPR ≠ surface subsidence; InSAR ≠ levelling.
- Record scope honestly: a number measured at BKPRU-4 is OTHER_VKM_SITE/BKPRU4, not SKRU1.
- Teaching/individual-assignment numbers are TEACHING_EXAMPLE, never MEASURED.
- Never "fix" a quote. If OCR is garbled, give OCR text and your visual reading separately.
- Distinguish the source's own result vs. something it cites (LITERATURE_CITED + cited_ref).
- Mine names: СКРУ-1 (= СКПРУ-1, Соликамский калийный рудоуправляющий/рудник №1), СКРУ-2, СКРУ-3,
  БКПРУ-1..4 (Березники), Усольский калийный комбинат, Ново-Соликамский участок. Watch for figure
  reproductions whose mine attribution is ambiguous (MAP_ATTRIBUTION / UNCLEAR_MINE_ATTRIBUTION).

## Outputs (write ONLY into your chunk directory)
`/home/user/work/run/sweep/<SOURCE_ID>/<CHUNK_ID>/`
1. `records.jsonl` — all records (typically 40–300 per chunk for content-rich chunks; do not pad).
2. `coverage.json`:
```
{"source_id": "", "chunk_id": "", "pages_assigned": "a-b",
 "pages_read_fully": [..], "pages_skimmed": [{"pages": "..", "reason": ".."}], "pages_unreadable": [..],
 "pages_rendered_and_viewed": [..],
 "method": "TEXT_LAYER|OCR|MIXED|DOCX", "chapters_covered": ["..."],
 "bibliographic": {"title": "", "authors": "", "year": "", "type": "", "venue_publisher": "", "total_pages": null} (if title/colophon in chunk, else null),
 "toc": ["..."] or null,
 "features": {"tables": n, "figures": n, "formulas": n, "maps": n, "cross_sections": n, "borehole_columns_or_lists": n, "bibliography_entries": n},
 "domains": ["geology","stratigraphy","tectonics","hydrogeology","lab_mechanics","rheology","stress","mining_technology","backfill","subsidence","monitoring","insar","gpr","seismic","thermal","geodesy","mathematics","history","equipment","normative"],
 "mine_site_scope": ["SKRU1", ...],
 "summary_ru": "8-20 sentences in Russian: what this chunk contains that matters for the 3D+time world",
 "underestimated_before": "what the previous consolidation missed or underweighted in this chunk (compare with existing digest)",
 "open_questions": ["..."]}
```
3. Return (final answer) ONLY a compact JSON summary: {"source_id","chunk_id","n_records","n_new","n_boreholes","n_formulas","n_citations","n_visual_checks","key_findings":[<=8 short strings],"blockers":[...]}.
