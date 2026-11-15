#!/usr/bin/env python3
"""Windows-native OpenCode worker controlled by Codex; no installers or model loops here."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import importlib.util

from vkm_worker_core import *
from process_tree import supervise, creation_kwargs
from request_gate import WirePolicy, BudgetThread, HookGate, PROFILE_NAMES
from tool_scope import ToolScope
from mcp_preparation import prepare_mcp

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MAX_ARTIFACT = 32 * 1024 * 1024


def implementation_hash() -> str:
    files = [*(PACKAGE_ROOT/'tools').glob('*.py'), *(PACKAGE_ROOT/'tools').glob('*.js'), *(PACKAGE_ROOT/'tools').glob('*.ps1'),
             *(PACKAGE_ROOT/'worker_skills').rglob('*.md')]
    return digest({p.relative_to(PACKAGE_ROOT).as_posix():sha256_file(p) for p in sorted(files)})


def require_windows() -> None:
    need(sys.platform == "win32" and os.name == "nt", "live runs require native Windows; no WSL fallback")


def command(argv, *, cwd=None, timeout=40, env=None) -> bytes:
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                           timeout=timeout, **creation_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerError("local command failed: " + type(exc).__name__) from None
    need(p.returncode == 0, "local command returned nonzero (output intentionally not exposed)")
    need(len(p.stdout) <= 64 * 1024 * 1024, "local command output limit exceeded")
    return p.stdout


def git(repo: Path, *args) -> bytes:
    # Disable hooks for coordinator-owned operations; do not use shell commands.
    return command(["git", "-c", "core.hooksPath=" + str(PACKAGE_ROOT / "empty-hooks"), "-C", str(repo), *args])


def source_state(repo: Path) -> str:
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    h = hashlib.sha256(git(repo, "rev-parse", "HEAD") + git(repo, "diff", "--binary", "HEAD", "--") + git(repo, "diff", "--cached", "--binary", "HEAD", "--"))
    for name in sorted(x for x in untracked if x):
        path = repo / os.fsdecode(name)
        h.update(name)
        if path.is_symlink():
            h.update(os.readlink(path).encode())
        elif path.is_file():
            h.update(sha256_file(path).encode())
    return h.hexdigest()


def ambient_snapshot(root: Path) -> str:
    """Stable content digest incl. filenames; no secret values are emitted."""
    if not root.exists():
        return digest({"absent": True})
    no_reparse(root)
    if root.is_file():
        return sha256_file(root)
    result = {}
    for path in sorted(root.rglob("*")):
        no_reparse(path)
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha256_file(path)
        need(len(result) <= 20000, "ambient config tree is unexpectedly large")
    return digest(result)


def ambient_paths(host: dict) -> list[Path]:
    """Enumerate known V2 configuration sources before starting any server."""
    home = Path(os.environ.get("USERPROFILE", str(Path.home()))).resolve()
    result = {home / ".config/opencode", home / ".agents/skills", home / ".claude/skills"}
    if os.name == "nt":
        need(bool(os.environ.get("PROGRAMDATA")), "PROGRAMDATA missing")
        result.add(Path(os.environ["PROGRAMDATA"]) / "opencode")
    else:
        result.add(Path("/etc/opencode"))
    runs = Path(host["run_root"]).resolve()
    for parent in [runs, *runs.parents]:
        result.update(parent / name for name in ("opencode.json", "opencode.jsonc", ".opencode"))
    return sorted(result, key=str)


def verify_native(host: dict) -> dict:
    executable = Path(host["opencode"]["path"])
    need(executable.is_absolute() and executable.is_file(), "native OpenCode executable missing")
    no_reparse(executable)
    if os.name == "nt":
        need(executable.suffix.lower() == ".exe", "use a real Windows .exe, not cmd/bat")
    need(sha256_file(executable) == host["opencode"]["sha256"], "OpenCode binary changed; requalify")
    q = read_json(verify_file_record(host["qualification_receipt"]))
    need(q.get("schema") == "vkm.opencode.qualification/2" and q.get("platform") == "win32", "native qualification missing")
    need(q.get('status') == 'APPROVED_BY_MAIN_CODEX', 'native qualification awaits main Codex review')
    need(q.get("opencode_sha256") == host["opencode"]["sha256"], "qualification binary mismatch")
    need(q.get("implementation_sha256") == implementation_hash(), "runner changed since native qualification")
    need(all(q.get("checks", {}).get(name) == "PASS" for name in RUNTIME_CHECKS), "native checks incomplete")
    for record in q.get("evidence", []):
        verify_file_record(record)
    need(bool(q.get("evidence")), "native qualification needs actual evidence files")
    expected = {str(p) for p in ambient_paths(host)}
    supplied = {r.get("path") for r in host.get("ambient_inputs", [])}
    need(supplied == expected, "ambient source inventory incomplete; Codex must qualify all sources")
    for record in host.get("ambient_inputs", []):
        need(set(record) == {"path", "digest"}, "invalid ambient input record")
        need(ambient_snapshot(Path(record["path"])) == record["digest"], "ambient OpenCode configuration changed")
    version = command([str(executable), "--version"], timeout=20).decode("utf-8").strip()
    need(host["opencode"]["version"] in re.findall(r"(?<![0-9.])2\.\d+\.\d+(?![0-9.])", version), "OpenCode version mismatch")
    help_text = command([str(executable), "run", "--help"], timeout=20).decode("utf-8")
    for flag in ("--standalone", "--archived", "--format", "--model", "--agent", "--file"):
        need(flag in help_text, "installed CLI lacks " + flag)
    return q


def child_environment(host: dict, run_dir: Path | None = None, config_path: Path | None = None) -> dict:
    safe = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "HOME", "LANG", "LC_ALL", "NUMBER_OF_PROCESSORS"}
    env = {k: v for k, v in os.environ.items() if k.upper() in safe}
    names = host.get("environment_names", [])
    strings(names, "additional environment names", maximum=30, maxlen=120)
    for name in names:
        need(name.startswith(("VKM_", "CONTEXT7_", "SSL_CERT_", "REQUESTS_CA_", "NODE_EXTRA_CA_")), "unapproved credential/environment forwarding")
        if name in os.environ:
            env[name] = os.environ[name]
    env["VKM_WORKER_AUTH"] = os.environ["OPENROUTER_API_KEY"]
    env["OPENROUTER_API_KEY"] = "VKM_RUNTIME_GATE_REQUIRED_INVALID"
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0")
    # --standalone alone inherits ancestor configs and executes their plugins.
    # Isolation must be explicit, before OpenCode starts any server.
    if run_dir is not None:
        roots = run_dir / "isolated"
        for name in ("home", "config", "data", "cache", "state", "temp"):
            (roots / name).mkdir(parents=True, exist_ok=True)
        env.update({"HOME": str(roots / "home"), "USERPROFILE": str(roots / "home"),
                    "APPDATA": str(roots / "config"), "LOCALAPPDATA": str(roots / "data"),
                    "XDG_CONFIG_HOME": str(roots / "config"), "XDG_DATA_HOME": str(roots / "data"),
                    "XDG_CACHE_HOME": str(roots / "cache"), "XDG_STATE_HOME": str(roots / "state"),
                    "TEMP": str(roots / "temp"), "TMP": str(roots / "temp"),
                    "OPENCODE_CONFIG_PROJECT_DISABLE": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1",
                    "OPENCODE_DISABLE_FREE_MODELS": "1"})
        need(config_path is not None and config_path.is_absolute(), "explicit trusted config required")
        env["OPENCODE_CONFIG"] = str(config_path)
    # Do not inherit OPENCODE_CONFIG/CONTENT, other provider credentials, GitHub
    # tokens, database DSNs or the entire parent agent environment.
    return env


def prompt(job: dict) -> str:
    return "\n".join([
        "# VKM worker task v2", f"Task: {job['task_id']}", f"Base: {job['base_sha']}",
        f"Mode: {job['mode']}", "Use skill @" + SCENARIOS[job["scenario"]],
        "Read AGENTS.md. Codex alone integrates and accepts results.",
        "Use tools. No recursive agents, commits, pushes, deployments, provider changes or canonical writes.",
        "Exact editable files: " + json.dumps(job["edit_paths"], ensure_ascii=False),
        "## Objective", job["objective"], "## Acceptance", *["- " + x for x in job["acceptance_criteria"]],
        "## Source locators", *job.get("source_locators", []),
        "## Prior accepted context (not scientific evidence)", job.get("context_note", "None"),
        "## Return", "Return a concise final report: actual tools, file:line findings, changed files, tests truly executed, UNKNOWNs, source/provenance locators, next step. Do not fabricate PASS.",
    ]) + "\n"


def plan(job: dict, host: dict, repo: Path) -> dict:
    validate_job(job); validate_host(host, job, evidence=False)
    repo = repo.resolve()
    need(repo.is_dir(), "repository missing")
    top = Path(os.fsdecode(git(repo, "rev-parse", "--show-toplevel")).strip()).resolve()
    need(top == repo, "repo must be the checkout root")
    base = git(repo, "rev-parse", "--verify", job["base_sha"] + "^{commit}").decode().strip()
    need(base == job["base_sha"], "base commit mismatch")
    roots = [repo, *[Path(p).resolve() for p in host.get("protected_roots", [])]]
    need(Path(host["run_root"]).is_absolute() and Path(host["state_root"]).is_absolute(), "state/run roots must be absolute")
    runs = Path(host["run_root"]).resolve(); state = Path(host["state_root"]).resolve()
    for root in (runs, state):
        need(root.is_absolute() and all(root != p and p not in root.parents for p in roots), "private state/runs cannot be inside source repositories")
        for parent in [root, *root.parents]:
            no_reparse(parent)
    run_dir = runs / job["task_id"]
    need(not run_dir.exists(), "task ID already used; never overwrite a run")
    tree = run_dir / "worktree"
    return {"schema": "vkm.opencode.plan/2", "task_id": job["task_id"], "repo": str(repo), "base_sha": base,
            "worktree": str(tree), "run_dir": str(run_dir), "profile": job["profile"],
            "model": "openrouter/" + host["profiles"][job["profile"]]["model_id"],
            "config": build_config(job, host, tree, run_dir, source_root=repo), "job_sha256": digest(job),
            "inference_performed": False, "native_acceptance": "REQUIRED_BEFORE_RUN"}


def checkout_controls(tree: Path) -> None:
    # Never delete a pre-existing configuration, including on an early exception.
    for rel in ("opencode.json", "opencode.jsonc", ".opencode", ".vkm-worker-task.md"):
        p = tree / rel
        need(not p.exists() and not p.is_symlink(), "source has OpenCode control files: reconcile in a dedicated integration, never overwrite")
    for path in tree.rglob("*"):
        no_reparse(path)


def checkout_base(repo: Path, tree: Path, base_sha: str) -> None:
    # Command-scoped only: tracked attributes retain their intended semantics.
    git(repo, '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
        'worktree', 'add', '--detach', str(tree), base_sha)


def write_owned(path: Path, data: bytes, owned: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        f.write(data)
    owned[path] = hashlib.sha256(data).hexdigest()


def remove_owned(owned: dict) -> list[str]:
    altered = []
    for path, expected in reversed(list(owned.items())):
        try:
            for ancestor in [path, *path.parents]:
                no_reparse(ancestor)
            unchanged = path.is_file() and sha256_file(path) == expected
        except (WorkerError, OSError):
            unchanged = False
        if not unchanged:
            altered.append(str(path)); continue
        path.unlink()  # The wrapper created this exact file and verifies its bytes.
    return altered


def audit_changes(tree: Path, job: dict, git_pointer_hash: str) -> dict:
    violations = []
    no_reparse(tree / ".git")
    need(sha256_file(tree / ".git") == git_pointer_hash, "worktree Git pointer changed")
    head = git(tree, "rev-parse", "HEAD").decode().strip()
    if head != job["base_sha"]:
        violations.append("WORKER_CHANGED_HEAD")
    tracked = {os.fsdecode(p) for p in git(tree, "diff", "--name-only", "-z", job["base_sha"], "--").split(b"\0") if p}
    untracked = {os.fsdecode(p) for p in git(tree, "ls-files", "--others", "-z").split(b"\0") if p}
    runtime_files = []
    files = []
    for name in sorted(tracked | untracked):
        p = tree / name
        inside(p, tree)
        # Untracked interpreter/tool caches are reported, not a source-code patch.
        parts = Path(name).parts
        is_cache = ("__pycache__" in parts or name.startswith((".pytest_cache/", ".serena/cache/")))
        if name not in tracked and is_cache:
            runtime_files.append(name); continue
        if name not in job["edit_paths"]:
            violations.append("OUT_OF_SCOPE:" + name)
        if p.is_file():
            need(p.stat().st_size <= MAX_ARTIFACT, "artifact too large; preserve worktree for Codex")
            files.append({"path": name, "sha256": sha256_file(p), "untracked": name in untracked, "bytes": p.stat().st_size})
        else:
            files.append({"path": name, "deleted": True})
    return {"files": files, "runtime_files": runtime_files, "violations": violations}


def take_lock(state: Path, key_fingerprint: str, task_id: str) -> Path:
    state.mkdir(parents=True, exist_ok=True)
    blocked = state / (key_fingerprint + ".pending.json")
    need(not blocked.exists(), "previous key run awaits Codex reconciliation; do not reset/rotate the key")
    lock = state / (key_fingerprint + ".lock")
    try:
        write_json(lock, {"task_id": task_id, "pid": os.getpid(), "created_at": utc()}, exclusive=True)
    except FileExistsError as exc:
        raise WorkerError("another worker owns the key lock (stale locks require reconciliation)") from exc
    return lock


def start_budget_gate(job, host, run_dir, key_before):
    """No legacy server-cap guard is waived by enabling shared accounting."""
    from shared_budget import SharedSession
    settings = host.get("shared_ledger")
    need(isinstance(settings, dict), "existing shared ledger is required for instrumental runs")
    total = finite_money(host['budget']['authorized_total_usd'])
    if total == 40:
        authority = read_json(verify_file_record(host['budget'].get('authorization_receipt')))
        need(authority.get('schema') == 'vkm.owner-budget-authorization/2', 'explicit40 owner authority required')
        need(finite_money(authority.get('authorized_total_usd')) == total and authority.get('key_fingerprint') == host['budget']['key_fingerprint'], '40 authority identity mismatch')
        need(Path(authority.get('ledger_path','')).resolve() == Path(settings['ledger_path']).resolve(), '40 authority ledger differs')
        need(authority.get('per_call_caps_changed') is False and authority.get('fallback_authorized') is False and authority.get('key_rotation_authorized_by_this_receipt') is False, '40 authority cannot change model caps/routes/key')
    record = settings.get("current_client", {})
    for name in ("client", "policy"):
        verify_file_record({"path": record.get(name + "_path"), "sha256": record.get(name + "_sha256")})
    spec = importlib.util.spec_from_file_location("_worker_policy_client", record["client_path"])
    client = importlib.util.module_from_spec(spec); spec.loader.exec_module(client)
    client.POLICY_PATH = Path(record["policy_path"])
    profile_name = PROFILE_NAMES[job["profile"]]
    selected = host["profiles"][job["profile"]]
    model, provider, _, cap = client.profile_spec(profile_name)
    need(selected["model_id"] == model and selected["provider_only"] == [provider], "host profile differs from current Delegate policy")
    policy = read_json(Path(record["policy_path"]))["profiles"][profile_name]
    need(selected["reasoning_effort"] == policy["default_effort"], "host reasoning differs from explicit policy default")
    metadata = read_json(verify_file_record(selected.get("endpoint_metadata")), limit=32 * 1024 * 1024)
    _, model_metadata, endpoint = client.select_endpoint(metadata, profile_name, fresh=True)
    need({"tools", "tool_choice"} <= set(endpoint["supported_parameters"]), "selected endpoint tools unsupported")
    image_policy=None
    image_record=host.get('image_admission',{}).get(job['profile'])
    if image_record is not None:
        need('image' in model_metadata.get('architecture',{}).get('input_modalities',[]),'selected model image capability unconfirmed')
        from image_admission import ImageAdmission
        image_policy=ImageAdmission(read_json(verify_file_record(image_record)),selected,
                                    workspace=run_dir/'worktree',read_paths=job.get('read_paths',[]),task_id=job['task_id'],metadata_guard=getattr(client,'guard_image_metadata',None))
    financial = read_json(verify_file_record(host.get("accounting_qualification")))
    need(financial.get("schema") == "vkm.opencode.accounting-qualification/2" and financial.get("status") == "APPROVED_BY_MAIN_CODEX", "per-request bounds/accounting not approved")
    need(financial.get("implementation_sha256") == implementation_hash(), "accounting qualification changed")
    cap = str(settings.get("session_caps", {}).get(job["profile"], cap))
    budget_record = {"ledger_path": settings["ledger_path"], "total_budget_usd": str(total),
                     "key_fingerprint": host["budget"]["key_fingerprint"], "key_remaining_usd": key_before["remaining"],
                     "diagnostic_reserve_usd": ".025"}
    record = {**record, "requested_reasoning": {"enabled": True, "effort": selected["reasoning_effort"], "exclude": True}}
    def factory():
        return SharedSession.begin(budget_record, record, profile_name, cap, job["task_id"],
                                   job["max_runtime_sec"], run_dir / "shared-accounting.json")
    tool_scope = ToolScope(run_dir / 'worktree',job,host.get('shell_commands',{}).get(job['scenario'],[]),
                           host.get('mcp_tool_guards',{}))
    # Reject malformed opt-in before any reservation/ledger supervisor begins.
    wire_policy = WirePolicy(selected, endpoint, image_policy=image_policy,
                             output_rate_policy=host.get('output_rate_policy', {}).get(job['profile']))
    return HookGate(BudgetThread(factory), wire_policy, run_dir, tool_scope=tool_scope)


def execute(job: dict, host: dict, repo: Path) -> dict:
    require_windows()
    need(job.get('profile') != 'kimi', 'Kimi API revoked by owner; use subscription workflow')
    p = plan(job, host, repo)
    validate_host(host, job, evidence=True)
    verify_native(host)
    key = os.environ.get("OPENROUTER_API_KEY", "")
    need(key, "OpenRouter credential missing; Codex must open the auth flow")
    state = Path(host["state_root"]); fp = host["budget"]["key_fingerprint"]
    lock = take_lock(state, fp, job["task_id"])
    pending = state / (fp + ".pending.json")
    run_dir = Path(p["run_dir"]); tree = Path(p["worktree"])
    repo = Path(p["repo"])
    receipt = {"schema": "vkm.opencode.receipt/2", "task_id": job["task_id"], "base_sha": job["base_sha"],
               "requested_model": p["model"], "started_at": utc(), "status": "PREPARING",
               "scientific_acceptance": "NOT_RUN", "codex_review": "PENDING", "native_tests_by_this_bundle": "NOT_RUN",
               "effective_reasoning": "UNCONFIRMED", "actual_provider_endpoint": "UNCONFIRMED"}
    owned = {}; launched = False; created = False; gate = None
    try:
        before = check_budget(get_key_info(key), host["budget"], key)
        receipt["key_before"] = before
        source_before = source_state(repo)
        run_dir.mkdir(parents=True, exist_ok=False); created = True
        if os.name != "nt": run_dir.chmod(0o700)
        write_json(run_dir / "job.json", job, exclusive=True)
        write_json(run_dir / "plan.json", p, exclusive=True)
        # An explicit immutable base, not implicit HEAD and not the dirty checkout.
        # Do not inherit host autocrlf conversion for frozen scientific/code
        # inputs. Explicit tracked attributes still apply; no Git config is saved.
        checkout_base(repo,tree,job['base_sha'])
        checkout_controls(tree)
        for rel in job["edit_paths"]:
            inside(tree / rel, tree)
            need(not (tree / rel).is_dir(), "edit_paths must name exact files, not directories")
        for rel in job.get("read_paths", []):
            inside(tree / rel, tree)
            need((tree / rel).is_file(), "explicit read file missing")
        write_json(run_dir / 'checkout-inputs.json', {
            'policy':'PER_COMMAND_AUTOCRLF_FALSE_EOL_LF_TRACKED_ATTRIBUTES_PRESERVED',
            'global_or_repository_config_changed':False,
            'base_sha':job['base_sha'],
            'read_files':[{ 'path':rel,'sha256':sha256_file(tree / rel)}
                          for rel in job.get('read_paths',[])]}, exclusive=True)
        git_hash = sha256_file(tree / ".git")
        receipt['mcp_preparation'] = prepare_mcp(job,host,tree,run_dir,write_owned,owned)
        gate = start_budget_gate(job, host, run_dir, before)
        guard_dir = run_dir / "guard"; guard_dir.mkdir()
        guard_source = PACKAGE_ROOT / "tools/budget_guard.js"
        (guard_dir / "index.js").write_bytes(guard_source.read_bytes())
        p["config"]["plugins"] = [str(guard_dir)]
        p["config"]["policies"].append({"action": "plugin.use", "resource": "vkm-budget-guard", "effect": "allow"})
        p["config"]["policies"].append({"action": "plugin.use", "resource": str(guard_dir), "effect": "allow"})
        receipt["guard_sha256"] = sha256_file(guard_dir / "index.js")
        write_owned(tree / ".opencode/opencode.jsonc", json_bytes(p["config"]), owned)
        write_json(run_dir / "trusted-config.json", p["config"], exclusive=True)
        skill = SCENARIOS[job["scenario"]]
        skill_source = PACKAGE_ROOT / "worker_skills" / skill / "SKILL.md"
        need(skill_source.is_file(), "worker scenario skill missing from package")
        write_owned(tree / ".opencode/skills" / skill / "SKILL.md", skill_source.read_bytes(), owned)
        # Project config discovery is disabled, including automatic skills.
        # Supply only this coordinator-pinned scenario through explicit paths.
        trusted_skills = run_dir / "skills" / skill
        trusted_skills.mkdir(parents=True)
        (trusted_skills / "SKILL.md").write_bytes(skill_source.read_bytes())
        p["config"]["skills"] = [str(run_dir / "skills")]
        validate_generated_config(p["config"])
        # Rewrite only the coordinator-owned external config before any launch.
        write_json(run_dir / "trusted-config.json", p["config"])
        task_file = tree / ".vkm-worker-task.md"
        write_owned(task_file, prompt(job).encode("utf-8"), owned)
        env = child_environment(host, run_dir, run_dir / "trusted-config.json")
        env["VKM_BUDGET_GATE"] = gate.url
        env["VKM_BUDGET_NONCE"] = gate.token
        events = Events([key])
        argv = [host["opencode"]["path"], "run", "--standalone", "--archived", "--format", "json",
                "--agent", "vkm-worker", "--model", p["model"], "--title", job["task_id"]]
        write_json(pending, {"task_id": job["task_id"], "run_dir": str(run_dir), "key_fingerprint": fp,
                             "started_at": utc(), "key_before": before, "state": "PENDING_RECONCILIATION"}, exclusive=True)
        launched = True
        receipt["status"] = "RUNNING"
        write_json(run_dir / "receipt.json", receipt)
        # Image tool events contain original base64 bytes. Permit only the
        # SHA-approved largest image plus the unchanged 1MiB event envelope;
        # total stdout/stderr remains bounded by the original 32MiB limit.
        output_line_limit = min(MAX_ARTIFACT, 1024 * 1024 + p['config'].get('media', {}).get('image', {}).get('max_base64_bytes', 0))
        need(output_line_limit <= MAX_ARTIFACT, 'image event exceeds existing total stdio bound')
        receipt['stdio_bounds'] = {'max_line_bytes': output_line_limit, 'max_total_bytes': MAX_ARTIFACT,
                                   'basis': 'EXACT_APPROVED_IMAGE_BASE64_PLUS_1MIB_ENVELOPE_OR_UNCHANGED_TEXT_1MIB'}
        with (run_dir / "events.meta.jsonl").open("x", encoding="utf-8") as out, (run_dir / "stderr.redacted.log").open("x", encoding="utf-8") as err:
            def event_line(line):
                if line.strip():
                    out.write(json.dumps(events.feed(line), ensure_ascii=False) + "\n"); out.flush()
            def error_line(line):
                err.write(sanitize(line, [key])); err.flush()
            proc = supervise(argv, cwd=tree, env=env, timeout=job["max_runtime_sec"], stdout_line=event_line, stderr_line=error_line,
                             max_line=output_line_limit, max_bytes=MAX_ARTIFACT,
                             stdin_bytes=prompt(job).encode("utf-8"))
        receipt["shared_accounting"] = gate.close(clean=proc.get("tree_cleanup_proven") is True)
        gate = None
        receipt["process"] = proc
        receipt["events"] = events.summary()
        (run_dir / "worker-report.txt").write_text("\n".join(events.text_parts), encoding="utf-8")
        tampered = remove_owned(owned); owned.clear()
        receipt["changed_control_files"] = tampered
        changes = audit_changes(tree, job, git_hash)
        receipt["changes"] = changes
        receipt["source_unchanged"] = source_state(repo) == source_before
        patch_data = git(tree, "diff", "--binary", job["base_sha"], "--")
        need(len(patch_data) <= MAX_ARTIFACT, "patch exceeds artifact limit")
        (run_dir / "changes.patch").write_bytes(patch_data)
        for f in changes["files"]:
            if f.get("untracked"):
                target = run_dir / "new-files" / f["path"]
                inside(target, run_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tree / f["path"], target)
        try:
            after = get_key_info(key)
            receipt["key_after_observed"] = {"usage": str(finite_money(after.get("usage"))), "checked_at": utc()}
            delta = finite_money(after.get("usage")) - finite_money(before["usage"])
            need(delta >= 0, "usage counter regressed")
            receipt["key_usage_delta_usd"] = str(delta)
            receipt["cost_basis"] = "KEY_USAGE_DELTA_NOT_EXCLUSIVE_TASK_COST; RECONCILE_GENERATIONS"
        except WorkerError:
            receipt["key_usage_delta_usd"] = None
            receipt["cost_basis"] = "UNKNOWN"
        blocked = []
        if proc["lifecycle"] != "EXITED" or proc["returncode"] != 0: blocked.append("PROCESS_NOT_CLEAN")
        if events.bad or events.errors or events.truncated_completion: blocked.append("EVENT_ERRORS_OR_INCOMPLETE")
        if not events.text_parts: blocked.append("NO_ASSISTANT_REPORT")
        if not events.finished_tools: blocked.append("NO_COMPLETED_TOOL_EVIDENCE")
        if not set(job.get("required_tools", [])) <= events.finished_tools: blocked.append("REQUIRED_TOOLS_NOT_OBSERVED")
        if tampered or changes["violations"]: blocked.append("SCOPE_VIOLATION")
        if not receipt["source_unchanged"]: blocked.append("SOURCE_CHANGED_DURING_RUN")
        receipt["blockers"] = blocked
        receipt["status"] = "CANDIDATE_READY_FOR_CODEX" if not blocked else "REVIEW_REQUIRED"
        return receipt
    except BaseException as exc:
        receipt["status"] = "FAILED_COST_UNRESOLVED" if launched else "BLOCKED_BEFORE_INFERENCE"
        receipt["error_type"] = type(exc).__name__
        raise
    finally:
        if gate is not None:
            receipt["shared_accounting"] = gate.close(clean=False)
        if owned:
            receipt["changed_control_files"] = remove_owned(owned)
        if created:
            receipt["completed_at"] = utc()
            receipt["accounting"] = "CODEX_RECONCILIATION_REQUIRED" if launched else "NO_INFERENCE_STARTED"
            write_json(run_dir / "receipt.json", receipt)
            write_json(run_dir / "handoff.json", {"task_id": job["task_id"], "base_sha": job["base_sha"], "worktree": str(tree),
                       "receipt": str(run_dir / "receipt.json"), "status": receipt["status"], "codex_review": "PENDING",
                       "independent_tests": "NOT_RUN", "scientific_acceptance": "NOT_RUN",
                       "source_locators": job.get("source_locators", []),
                       "next": "Codex verifies diff/tests, settles provider usage, then saves accepted concise memory in existing VKM stores."})
        if lock.exists(): lock.unlink()
        # Keep pending after every launched run; never silently forgive unknown charges.


def reconcile(host: dict, evidence: dict) -> dict:
    """Codex-supplied accounting attestation, not a substitute for provider evidence."""
    require_windows()
    fp = host["budget"]["key_fingerprint"]; state = Path(host["state_root"])
    need(not (state / (fp + ".lock")).exists(), "worker still owns key lock")
    pending_path = state / (fp + ".pending.json")
    pending = read_json(pending_path)
    need(evidence.get("schema") == "vkm.opencode.settlement/2", "bad settlement schema")
    need(evidence.get("task_id") == pending["task_id"] and evidence.get("key_fingerprint") == fp, "settlement identity mismatch")
    need(evidence.get("no_inflight_requests_confirmed") is True and evidence.get("all_generations_settled") is True, "unresolved remote requests")
    need(evidence.get("basis") in {"generation_records", "provider_activity"}, "CLI exit is not cost evidence")
    records = evidence.get("evidence", [])
    need(bool(records), "settlement evidence required")
    for record in records: verify_file_record(record)
    key = os.environ.get("OPENROUTER_API_KEY", "")
    need(hashlib.sha256(key.encode()).hexdigest() == fp, "wrong accounting key")
    observed = get_key_info(key)
    need(finite_money(observed.get("usage")) >= finite_money(pending["key_before"]["usage"]), "usage regression")
    settled = {**evidence, "observed_usage": str(finite_money(observed["usage"])), "recorded_at": utc(), "reviewer": "Codex"}
    run_dir = Path(pending["run_dir"])
    write_json(run_dir / "settlement.json", settled, exclusive=True)
    pending_path.rename(run_dir / "settled.pending.json")
    return {"task_id": pending["task_id"], "accounting": "SETTLED_BY_CODEX_EVIDENCE", "scientific_acceptance": "NOT_IMPLIED"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        p = sub.add_parser(name); p.add_argument("--host", type=Path, required=True); p.add_argument("--job", type=Path, required=True); p.add_argument("--repo", type=Path, required=True)
    p = sub.add_parser("reconcile"); p.add_argument("--host", type=Path, required=True); p.add_argument("--evidence", type=Path, required=True)
    sub.add_parser("implementation-hash")
    p = sub.add_parser("ambient-hash"); p.add_argument("--path", type=Path, required=True)
    p = sub.add_parser("ambient-inventory"); p.add_argument("--host", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "implementation-hash": result = {"implementation_sha256": implementation_hash()}
        elif args.command == "ambient-hash": result = {"path": str(args.path), "digest": ambient_snapshot(args.path)}
        elif args.command == "ambient-inventory": result = {"ambient_inputs": [{"path": str(p), "digest": ambient_snapshot(p)} for p in ambient_paths(read_json(args.host))]}
        elif args.command == "reconcile": result = reconcile(read_json(args.host), read_json(args.evidence))
        else:
            job = validate_job(read_json(args.job)); host = read_json(args.host)
            result = plan(job, host, args.repo) if args.command == "plan" else execute(job, host, args.repo)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if args.command != "run" or result.get("status") == "CANDIDATE_READY_FOR_CODEX" else 3
    except (WorkerError, OSError, ValueError, KeyError, TypeError) as exc:
        # Error messages never include subprocess outputs or credential values.
        print("VKM_WORKER_ERROR: " + (str(exc) if isinstance(exc, WorkerError) else type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
