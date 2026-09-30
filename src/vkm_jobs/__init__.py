"""vkm_jobs — shared job layer of the engineering-tool MCP servers (``vkm-matlab``, ``vkm-ansys``).

Stable API (engineering-tools plan §2; keep backwards compatible — other servers build on it):

* Root — :class:`vkm_jobs.roots.SimRoot` (``VKM_SIM_ROOT``: absolute ASCII path, no spaces, fixed local disk,
  outside the PUBLIC/PRIVATE clones and the canonical data root). Layout ``jobs/<job_id>/{job.json, status.json,
  spawn.json, receipt.json, cancel.request, runner.lock, in/, work/, out/, logs/}``, ``locks/``, ``sessions/``,
  ``cache/``, ``logs/``, ``tools/``, ``matlab/session/``. Path jail: :func:`vkm_jobs.roots.clean_relpath`,
  :func:`vkm_jobs.roots.resolve_inside` → ``PATH_OUTSIDE_ROOT``.
* Job ids — ``<APP>-<UTC YYYYMMDDTHHMMSSZ>-<8 hex>``, APP ∈ MATLAB, MAPDL, MECH, WB, DPF, OSL, PY
  (:func:`vkm_jobs.spec.new_job_id`).
* Spec — :class:`vkm_jobs.spec.JobSpec` (``vkm.sim_job/1``): ``argv`` (real paths, stays in the root), ``cwd``,
  ``env`` (names only in the receipt), ``timeout_s`` (:func:`vkm_jobs.spec.clamp_timeout`, table ``TIMEOUTS``),
  ``queue_timeout_s``, ``checks`` (:func:`vkm_jobs.spec.validate_check`), ``outputs`` / ``hash_exclude`` /
  ``cleanup_on_success`` globs, ``success_exit_codes``, ``log_files`` + error / warning / licence regexes, ``progress``
  hook ``{module, function, args}`` (called by the runner every 5 s with the job dir, returns a dict), ``gate``
  ``{pid_files, image}`` (wait while a live process owns one of those pid files), ``params``
  (:func:`vkm_jobs.spec.validate_param`: value + epistemic status + source_ref), ``model_choices``, ``app_info``,
  ``git``, ``logical_roots`` (``{"<MATLAB_ROOT>": path}`` for redaction), ``meta``.
* Server side — :class:`vkm_jobs.service.JobsService`: ``draft(app)`` → :class:`vkm_jobs.service.JobDraft`
  (``copy_input``, ``write_input``, ``spec(**fields)``), ``submit(draft, spec, wait_s)`` → JobRef ``{job_id,
  status, app, kind, label, pool, queue_position?, job_dir (logical), exit_code?, reason?, receipt?}``, ``status``
  (sets ``LOST`` when the runner vanished), ``list``, ``wait``, ``read``, ``receipt``, ``cancel``,
  ``publish_receipt`` (→ ``docs/engineering_tools/receipts/<name>.json`` after :func:`vkm_jobs.redact.
  public_text_problems` and the leakage scan), ``pools``; :func:`vkm_jobs.service.git_info`.
* Runner — ``python -m vkm_jobs.runner <job_dir>`` (:mod:`vkm_jobs.runner`), detached from the MCP server (survives a
  Claude Code restart), Windows Job Object ``KILL_ON_JOB_CLOSE`` over the application tree (:mod:`vkm_jobs.procs`),
  statuses ``QUEUED → RUNNING → SUCCEEDED | FAILED | CHECK_FAILED | TIMED_OUT | CANCELLED | LICENSE_UNAVAILABLE |
  LOST``.
* Pools — :mod:`vkm_jobs.pools`: OS file locks per slot (``ansys`` 1, ``matlab`` 1, ``dpf`` 2, ``py`` 2;
  ``VKM_POOL_CAPACITY_<POOL>``), FIFO queue markers; a dead runner never leaves a stale lock.
* Receipt — :mod:`vkm_jobs.receipt` (``vkm.sim_receipt/1``, logical paths only, ``result_status = MODEL_RESULT``,
  ``review_status = AUTO_UNREVIEWED``); :class:`vkm_jobs.redact.Redactor`.
* MCP — :mod:`vkm_jobs.mcp_tools`: :class:`~vkm_jobs.mcp_tools.Envelope` (``{"schema": "<server>.result/1", ok,
  server, server_version, tool, request_id, result, error}``) and :func:`~vkm_jobs.mcp_tools.register_job_tools`
  (``job_list``, ``job_status``, ``job_wait``, ``job_read``, ``job_receipt``, ``job_cancel``,
  ``job_publish_receipt``); errors — :mod:`vkm_jobs.errors`.
"""
__version__ = "0.1.0"

__all__ = ["__version__"]
