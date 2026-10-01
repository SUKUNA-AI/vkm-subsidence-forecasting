# PUBLIC offline CPU checks

All Actions jobs use `scripts/run_offline_checks.py`. Python is pinned to 3.13.5.
The `world-integrity` job covers `tests/world`; `corpus-offline` lets pytest discover
every other immediate child of `tests` recursively, including future suites and
its configured `python_files` patterns. These two Linux jobs do not overlap. The
`windows-offline` job repeats the world, engineering, evidence, dataset and pure QGIS suites plus
nightly summary/dossier and backup-manifest checks on Windows 2025. Existing synthetic
API/MCP and job-runner tests run; no PRIVATE corpus or deployed service is opened.
The evidence and dataset fast suites are included in recursive Linux discovery and explicit
Windows selection; a regression check asserts both. The native GDAL rehearsal remains
`qgis_runtime` and is reported as deselected `NOT_RUN`, without installing GIS dependencies.
The second integration tranche additionally selects the pure native-service,
search and loaded-pack protocol tests on Windows. Fake driver/process contracts
run there; the six named loaded-pack filesystem cases need the Linux native
mutation-event fence and are individually accounted as Windows `NOT_RUN`.
The ten exact shared-admission-gate cases in receiver/deployment suites need
POSIX file locking and likewise remain Windows `NOT_RUN`; unrelated cases in
those files still execute. These allowances never apply on Linux.
The nightly MCP smoke contract now covers 49 read tools, including the four evidence
queries. It discovers a permitted record with a bounded list query before reading its
record, dependency page and review packet. User-defined evidence ids and payloads are
not written to the smoke report. An absent backend or missing tool fails; an empty
permitted list leaves dependent calls `SKIP` and the overall smoke `WARN`, never `PASS`.
These synthetic client checks do not qualify a deployed service or scientific evidence.

Run after installing the exact lock files used by the corresponding workflow job:

```bash
python scripts/run_offline_checks.py world-integrity --output ../offline-world
python scripts/run_offline_checks.py corpus-offline --output ../offline-corpus
python scripts/run_offline_checks.py windows-offline --output ../offline-windows
```

The output must be outside the checkout. Each job writes JUnit and `summary.json`;
the world job also writes the public canonical verifier report and wheel smoke log.
For local WSL runs, use a persistent external directory: `/tmp` may be tmpfs and
its reports can disappear when the distribution stops. Keep the log and summary
before ending that runtime; a lost report cannot prove a successful qualification.
Each run creates a fresh external `PYTHONPYCACHEPREFIX` with bytecode writes disabled.
This ignores existing timestamp/size `.pyc` caches without deleting them;
`PYTHONDONTWRITEBYTECODE` alone would still read such caches. Python child processes
inherit the namespace, and isolated `-I` wheel checks receive explicit `-B -X
pycache_prefix=...` flags. A synthetic regression proves that same-size source
replacement with restored mtime imports the new source, including child processes.
Collection failures, no selected tests, no passing tests, failures, xfail/xpass,
and unexpected skips fail the job. Collection skips and runtime skips are recorded
as `NOT_RUN`, with their node and exact reason. Deselected `services`, `gpu`,
`desktop`, `matlab`, `ansys`, and `qgis_runtime` tests are listed separately as
`NOT_RUN`; their markers describe real external prerequisites. PyQGIS runtime
tests remain runnable locally with their marker; pure Python QGIS unit tests run
in the offline corpus job. Two existing GPU parity tests now have GPU markers.

CPU Python dependencies are now required: OpenCV, Snowball and morphology skips
fail the job. Their distribution METADATA was inspected on 01.10.2026 in the
existing Windows core environment and WSL figure/navigation environments:
`opencv-python-headless==5.0.0.93`, `pymorphy3==2.0.6`,
`pymorphy3-dicts-ru==2.4.417150.4580142`, `DAWG2-Python==0.9.0`,
`snowballstemmer==3.1.1`. Those initial observations were installed-distribution
evidence. A separate fresh Linux environment was then qualified on 01.10.2026:
Python 3.13.5, exactly 84 installed distributions, all 84 applicable pins from the
four CPU locks matched, and `uv pip check` exited 0. This qualifies installation
and dependency closure; the complete test workflow and fresh Windows installation
have separate results and must not be inferred from it. The required numpy and
setuptools dependencies retain their existing pins; the optional native DAWG2 fast
extra is not needed. Known missing DjVuLibre,
rsync, Tesseract, or nightly host tools are also recorded locally; Actions installs
these CPU host tools. The allowlist is a module plus exact reason, not a blanket
permission to skip. The glyph-outline PDF test additionally requires OpenCV;
its existing Tesseract condition alone did not declare that prerequisite.
All other skips fail. A partially executed job is
`PASS_WITH_NOT_RUN`, never reported as an entirely passed suite.

A fresh native Windows environment uses the available Python 3.13.13, separately
from the hosted job's Python 3.13.5. Its initial `pip check` exposed two missing
Windows dependencies of MCP/psycopg. The CPU lock now additionally pins
[`pywin32==312`](https://pypi.org/project/pywin32/312/) and `tzdata==2026.3`, both
with `sys_platform == "win32"`; the former has the official CPython 3.13 x64 wheel,
the latter was observed in the existing Windows environment. The new environment
has 87 matching distributions and passes `pip check`; Linux still has 84 applicable
pins. All three Actions jobs now run `pip check` after installation with `--no-deps`.
This does not replace either platform's actual test result.

The FIFO input and directory-fsync-failure cases are recorded as `NOT_RUN` on Windows
only: the underlying POSIX primitives are unavailable. Production receiver binding
explicitly requires Linux filesystem change-time semantics and a native file watch, so its 26 named cases
plus eight exact policy/CanonStore/NAV binding challenger cases
are also Windows `NOT_RUN`; the default-profile and unsupported-OS rejection tests
still run. These allowances are scoped to exact test ids and reasons and are not
allowed on Linux. Missing
Windows symlink privileges still fail the qualification; the runner does not
enable Developer Mode, change privileges, or turn those missing checks into PASS.

The 01.10.2026 integration baseline qualified the bytes committed as
`ecf25c6a3fedca4e04a001320ec29319b623bbdb`:

| Local gate | Result | Explicit gaps |
|---|---|---|
| Linux 3.13.5 corpus-offline | 2305 PASS, zero failures/skips; 958.04 s | 33 external cases deselected; `PASS_WITH_NOT_RUN` |
| Linux 3.13.5 world-integrity | 525 PASS; installed wheel PASS; no verifier object failures | 7 unavailable tag anchors and 2 PRIVATE-dependent comparisons `NOT_RUN` |
| Windows 3.13.13 windows-offline | 1142 PASS, 1 FAIL, 34 skips, 6 deselected; 90.10 s; job `FAIL` | One symlink creation WinError 1314 and 7 unexpected privilege skips; 27 expected OS-specific `NOT_RUN` |

Both corpus/Windows runs proved identical before/after bytes for 738
code/test/schema/lock/workflow files, with manifest SHA-256
`84b3e82d1bbeb717737ed807a06906b3b80f5eb4424264e5cdc37cc15d82aa07`.
Git HEAD changed during the Linux run; those bytes did not. Raw JUnit, summary,
environment and per-file hash manifests are retained outside the checkout.
This is a baseline for that code state; subsequent integration changes need new
qualification. Hosted Python 3.13.5 Windows and all required Actions checks remain
separate gates. This baseline does not establish production or scientific readiness.

The second tranche is not covered by those baseline counts or the 738-file hash.
Its shadow-acceptance harness has a separate synthetic qualification: 45 tests
passed on Linux 3.13.5 and 45 on Windows 3.13.13, with no skips. Those tests exercise
the actual private ASGI/MCP wiring and synthetic failure receipts; no live
CORE/EDGE endpoint, model process or production switch was qualified.
The final broad workflow must be repeated after the integration source freeze.

`scripts/export_evidence_schema.py` publishes 29 deterministic JSON contracts,
including the frozen Phase-1 migration inputs, plan and trusted operator approval.
The nine additive contracts cover dataset versions/catalogues/source-version
links, service identity, deployment profile, shadow acceptance plan, typed
scientific-use context, historical read context and native serving profile. The
dataset version schema describes `DatasetVersion.as_dict()` and preserves the
existing `vkm-dataset-version-v1` discriminator; it does not rename serialized
manifests or change their hashes. Runtime Python validation still enforces
cross-field invariants, containment, fresh hashes, native identity and policy.
JSON Schema validation alone establishes neither data admission nor scientific
readiness. The mandatory published-schema tests compare exact generated bytes;
`python scripts/export_evidence_schema.py --check` is the read-only manual check.

The existing WorldSpec, corpus and service lock files retain their pins. Additional
CPU test packages have exact versions in `requirements/offline-test.lock.txt`, with
their committed freeze/receipt sources. Torch, model downloads, Ansys, MATLAB and
live services are not installed or started. Action revisions were resolved from
the respective upstream release tags and are pinned to full commit SHAs.
The actions declare Node24 explicitly. No insecure old-Node override is enabled.
UTF-8 is explicit for all jobs and subprocesses. Synthetic Git fixtures isolate
line-ending conversion and inherited repository redirects; PRIVATE byte-exact
attributes and frozen hashes are unchanged. Missing symlink/locking capability is
not allowed to become a passed Windows test.

The nightly CLI accepts an injected aware-UTC clock through its Python entry point;
production invocations continue using the current clock. Tests move the wall clock
by 0, 7, 30 and 60 days and verify deterministic injected results. Freshness still
uses the production 26/36-hour thresholds, and receipts more than five minutes in
the future or without a recognised terminal verdict fail.

The world job checks frozen identities directly from Git objects without downloading
LFS payloads. Missing objects, unavailable legacy branches, or mismatched frozen
identities fail even when the general verifier treats them as nonblocking.
Missing tag anchors remain explicit `NOT_RUN` gaps. The public catalogue map and
manifest check must run and pass; PRIVATE-dependent catalogue comparisons are
reported as `NOT_RUN`. The runner never creates tags
or modifies frozen references. PRIVATE source/catalogue synchronization requires
a separate authorized environment and is outside this job's scope.

The package smoke copies source into an external temporary directory, builds a
wheel, installs it without dependencies into another external directory, and
imports all packaged root modules under Python isolation from outside the checkout.
Every actual file matched by package-data declarations must be present with the
same SHA-256. Empty declarations, currently MATLAB entry files and Ansys ladder
JSON, are reported as `NO_SOURCE_FILES`; absent source files are not invented.
