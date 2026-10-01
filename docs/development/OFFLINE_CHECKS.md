# PUBLIC offline CPU checks

Both Actions jobs use `scripts/run_offline_checks.py`. Python is pinned to 3.13.5.
The `world-integrity` job covers `tests/world`; `corpus-offline` lets pytest discover
every other immediate child of `tests` recursively, including future suites and
its configured `python_files` patterns. They do not overlap. Existing synthetic
API/MCP and job-runner tests run; no PRIVATE corpus or deployed service is opened.

Run after installing the exact lock files used by the corresponding workflow job:

```bash
python scripts/run_offline_checks.py world-integrity --output ../offline-world
python scripts/run_offline_checks.py corpus-offline --output ../offline-corpus
```

The output must be outside the checkout. Each job writes JUnit and `summary.json`;
the world job also writes the public canonical verifier report and wheel smoke log.
Collection failures, no selected tests, no passing tests, failures, xfail/xpass,
and unexpected skips fail the job. Collection skips and runtime skips are recorded
as `NOT_RUN`, with their node and exact reason. Deselected `services`, `gpu`,
`desktop`, `matlab`, `ansys`, and `qgis_runtime` tests are listed separately as
`NOT_RUN`; their markers describe real external prerequisites. PyQGIS runtime
tests remain runnable locally with their marker; pure Python QGIS unit tests run
in the offline corpus job. Two existing GPU parity tests now have GPU markers.

The only allowed CPU Python dependency skips are existing OpenCV tests, the
Snowball retrieval-pipeline module, and morphology tests in the three NAV modules.
The committed receipt records OpenCV runtime 5.0.0 but does not establish an exact
wheel-distribution version. Snowball and the pymorphy3 dependency closure have no
committed exact pins. They remain `NOT_RUN` until those pins are established.
No unverified version is added merely to remove a skip. Known missing DjVuLibre,
rsync, Tesseract, or nightly host tools are also recorded locally; Actions installs
these CPU host tools. The allowlist is a module plus exact reason, not a blanket
permission to skip. The glyph-outline PDF test additionally requires OpenCV;
its existing Tesseract condition alone did not declare that prerequisite.
The missing-OpenCV allowance for this test is restricted to its exact node ID.
All other skips fail. A partially executed job is
`PASS_WITH_NOT_RUN`, never reported as an entirely passed suite.

The existing WorldSpec, corpus and service lock files retain their pins. Additional
CPU test packages have exact versions in `requirements/offline-test.lock.txt`, with
their committed freeze/receipt sources. Torch, model downloads, Ansys, MATLAB and
live services are not installed or started. Action revisions were resolved from
the respective upstream release tags and are pinned to full commit SHAs.

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
