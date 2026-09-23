# Immutable scenario_simulation_v2 release

This freeze is effective at the containing commit named
`data: freeze scenario simulation v2 release`. It is a data-only freeze,
authorized on 2026-09-24, not a model-selection or field-validation decision.

## Authoritative identity

| Artifact | SHA256 of manifest bytes |
| --- | --- |
| `data/scenario_simulation_v2/manifest.json` | `7bb67939314fbcf548686893e6868c90a2303e1303c401377f197e077fec8c13` |
| `artifacts/reconstruction/scenario_constraints_v2/manifest.json` | `96b88b61cf766062c1f831e32b50bdd82441da63c50ce41569a5ffad297de6ef` |
| `artifacts/data_quality/scenario_simulation_v2/finalization_manifest.json` | `f0f36912adc515f3cbf50eb92ba7a13728d2173d31fb322d8efff9568233e2f4` |

The recursive source/config/code/output hashes in these manifests identify the
exact release. Git LFS pointer OIDs identify exact hydrated payload bytes,
which here are the original `.csv.gz` files; hash the hydrated files when
checking the dataset manifest. Do not hash LFS pointer text as dataset content.

Frozen dimensions: 640 observation scenarios; 128 latent-world IDs; 127 distinct
full latent fields; 42 points; 14 profiles; 29 campaigns; 321766 origins and
321766 model windows. Roles: train 122047, calibration 35148, evaluation 133833,
excluded 30738. Numerically bounded scale fraction: 0.984375; empirically
identified real temporal-law fraction: 0. Models executed in this freeze: 0;
legacy holdout labels parsed: 0.

## No retrospective changes

Generator v2, its transitive helper code, constraints v2, both configs, seeds,
mixture, all release artifacts and the evaluator are frozen. No parameter,
distribution, source bound, split, target or observation law may be changed
after viewing model results. Corrections or new hypotheses require a separately
named v3 or a preregistered robustness/sensitivity release with its own manifest
and protocol. Never replace bytes under the v2 release identity. Add documentary
errata separately if needed; retain the original evidence and hashes.

Seven separately handled leveling interval envelopes constrain scale in explicit nominal
windows. Unknown published positions remain unknown; common datum and cross-period
subtraction are not assumed. Unidentified temporal inset, other-site rates and
radar-leveling disagreement do not calibrate ground-leveling dynamics/noise.
The 2016 map is not a global clip. Roof/thermal contamination remains a separate
synthetic reflector stress; pseudo-transitions preserve their latent truth.
All temporal laws remain design assumptions, and observation replicas of one
latent world are dependent. These facts cannot be weakened by future results.

Future model access is restricted to the feature allowlist and causal history.
The synthetic evaluator remains scorer/QA-only; its availability for release QA
does not authorize model workers to read it. Historical Gate B/C labels and the
legacy sealed holdout remain excluded. Reactivation and moving focus remain
outside train/calibration. The target remains the next planned targeted campaign.

## Technical publication and prior evidence

The dataset's `code_commit` and `uncommitted_source_hashes_authoritative` fields,
the earlier report's uncommitted status, its Git inventory and `auto_commit=false`
record the creation-time state. They are preserved as historical evidence, not
silently rewritten with the release commit ID. This freeze document and its
containing Git commit establish the subsequent publication state.

The original 1059-file local integrity inventory includes 17 local-only entries:
12 files of the unrelated v1 B1/IMM benchmark and 5 ignored egg-info files.
All 1059 were checked unchanged before publication. Of 1042 versioned entries,
49 historical environment/worker receipts have CRLF worktree bytes versus LF
Git blobs. They are not v2 dependencies and are not rewritten. A separate 993-file
`artifacts/data_quality/scenario_simulation_v2_freeze/checkout_baseline_inventory.json`
contains the byte-stable subset for clean-checkout verification. Exclusion reasons
are recorded in `checkout_inventory_exclusions.json`. This is a Git
packaging boundary, not deletion or alteration of the original inventory.

The previously untracked v1 data, generator helpers, adapter/test dependencies,
config and protocol accompany this release as the immutable differential baseline.
The v1 benchmark results, execution config and executable runner stay local and
outside this release commit. No old MAE is a v2 result. Only the four explicitly
selected data-only tests from the v1 test module belong to the freeze recheck.

## Reproduction after checkout

For the canonical-byte checkout inventory, clone with `--config core.autocrlf=false`
so historical unpinned text files retain their Git bytes. V2's own hashes are
protected by `.gitattributes` regardless of this setting. Install the recorded
Python environment, hydrate the existing LFS-managed inputs
and the new compressed tables with `git lfs pull`, and run from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts/recheck_scenario_v2_release.py --work work/data_foundation_v2/fresh_freeze_check --baseline artifacts/data_quality/scenario_simulation_v2_freeze/checkout_baseline_inventory.json
```

Choose a new work directory every time. The command verifies manifests, performs
two constraints builds and two dataset builds without overwriting v2, compares
all bytes, independently checks serialized features/targets/windows, runs 36
data/reconstruction tests and 4 v1 data regression tests, and checks the actual
127 distinct full-field signatures. It neither trains nor evaluates predictors.

The original finalizer is retained as historical creation tooling, not a command
to rerun against a frozen audit directory. The new recheck command supersedes
its scratch-path assumptions. Full data reproduction is bitwise-qualified in the
recorded Windows/Python/NumPy/pandas environment; other environments require
verification rather than an assumption of bitwise portability.

Next work remains acceptance of the separate v2 adapter and sequence tensorization
under `SCENARIO_EXPERIMENT_V2_PROTOCOL.md`. This freeze grants no model-run approval.
