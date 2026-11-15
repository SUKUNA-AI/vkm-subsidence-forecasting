"""Validated contracts and OpenCode V2 configuration. Python 3.11+, stdlib only.

This is a trusted-workstation coordinator, NOT an OS sandbox. Native acceptance
of the installed OpenCode/MCP builds is separate from these offline checks.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import stat
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
import urllib.request

SCENARIOS = {
    "code_investigation": "vkm-code-investigation",
    "structural_change": "vkm-structural-change",
    "source_research": "vkm-source-research",
    "independent_review": "vkm-independent-review",
}
PROFILES = {"deepseek", "kimi", "hydrogen", "opus"}
SERVER_NAMES = {"graphify", "serena", "ast_grep", "context7", "vkm_corpus", "vkm_qgis", "vkm_cad"}
HASH = re.compile(r"[0-9a-f]{64}\Z")
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,79}\Z")
RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
CONTROL_NAMES = {"agents.md", "claude.md", "gemini.md", "opencode.json", "opencode.jsonc", "config.toml"}
RUNTIME_CHECKS = {"windows_native", "headless_flags", "config_schema", "effective_permissions", "ambient_config_review", "process_tree_cleanup", "auth_isolation", "provider_body", "event_schema"}

class WorkerError(Exception):
    """A fail-closed, user-readable validation or execution failure."""


def need(condition: bool, message: str) -> None:
    if not condition:
        raise WorkerError(message)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        need(k not in out, f"duplicate JSON key: {k}")
        out[k] = v
    return out


def parse_json(text: str):
    def bad_constant(_):
        raise WorkerError("non-finite JSON number")
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=bad_constant)
    except (ValueError, RecursionError) as exc:
        raise WorkerError("invalid JSON") from exc


def read_json(path: Path, limit: int = 1024 * 1024):
    need(path.is_file() and not path.is_symlink(), "missing or symlinked JSON file")
    need(path.stat().st_size <= limit, "JSON size limit exceeded")
    try:
        return parse_json(path.read_text(encoding="utf-8-sig"))
    except UnicodeError as exc:
        raise WorkerError("JSON must be UTF-8") from exc


def write_json(path: Path, value: object, *, exclusive: bool = False) -> None:
    """Write only runner-owned output; exclusive is mandatory for new control files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json_bytes(value)
    if exclusive:
        with path.open("xb") as f:
            f.write(data)
    else:
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    if os.name != "nt":
        path.chmod(0o600)


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def finite_money(value, name: str = "money") -> Decimal:
    need(not isinstance(value, bool) and isinstance(value, (int, float, str, Decimal)), f"{name}: invalid type")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise WorkerError(f"{name}: invalid number") from exc
    need(d.is_finite() and d >= 0, f"{name}: invalid amount")
    return d


def integer(value, low: int, high: int, name: str) -> int:
    need(type(value) is int and low <= value <= high, f"{name} outside [{low}, {high}]")
    return value


def strings(value, name: str, *, nonempty: bool = False, maximum: int = 100, maxlen: int = 2048) -> list[str]:
    need(isinstance(value, list) and len(value) <= maximum and (bool(value) or not nonempty), f"invalid {name}")
    need(all(isinstance(x, str) and 0 < len(x) <= maxlen and "\x00" not in x for x in value), f"invalid {name} item")
    need(len(value) == len(set(value)), f"duplicate {name} entries")
    return value


def relative_file(value: str, *, read_only: bool = False) -> str:
    """Exact cross-platform file path, never a glob or Windows device/ADS."""
    need(isinstance(value, str) and 0 < len(value) <= 220, "invalid edit path")
    need(not any(ord(c) < 32 or ord(c) == 127 for c in value), "control character in path")
    need(not any(c in value for c in '\\:*?[]{}<>|"'), "path must be literal, not wildcard/drive/ADS")
    parts = value.split("/")
    need(all(p not in {"", ".", ".."} and not p.startswith(".") and not p.endswith((".", " ")) for p in parts), "unsafe path component")
    for p in parts:
        need(p.split(".")[0].upper() not in RESERVED, "Windows device path denied")
        need(read_only or p.casefold() not in CONTROL_NAMES, "worker cannot edit agent/runtime instructions")
    return value


def validate_job(job: dict) -> dict:
    required = {"schema", "task_id", "base_sha", "mode", "profile", "scenario", "objective", "acceptance_criteria", "mcp", "edit_paths", "max_steps", "max_runtime_sec"}
    optional = {"source_locators", "context_note", "required_tools", "read_paths"}
    need(isinstance(job, dict), "job must be an object")
    need(required <= set(job), "missing job fields: " + ", ".join(sorted(required - set(job))))
    need(set(job) <= required | optional, "unknown job fields: " + ", ".join(sorted(set(job) - required - optional)))
    need(job["schema"] == "vkm.opencode.job/2", "unsupported job schema")
    need(isinstance(job["task_id"], str) and bool(IDENT.fullmatch(job["task_id"])), "invalid task_id")
    relative_file(job["task_id"])
    need(isinstance(job["base_sha"], str) and bool(SHA.fullmatch(job["base_sha"])), "base_sha must be a full explicit commit hash")
    need(job["mode"] in {"review", "edit"}, "invalid mode")
    need(job["profile"] in PROFILES and job["scenario"] in SCENARIOS, "unknown profile/scenario")
    need(isinstance(job["objective"], str) and 8 <= len(job["objective"]) <= 24000 and "\x00" not in job["objective"], "invalid objective")
    strings(job["acceptance_criteria"], "acceptance criteria", nonempty=True, maximum=20)
    strings(job["mcp"], "MCP selection", maximum=8, maxlen=64)
    need(set(job["mcp"]) <= SERVER_NAMES, "unknown MCP selection")
    strings(job["edit_paths"], "edit paths", maximum=200, maxlen=220)
    for name in job["edit_paths"]:
        relative_file(name)
    need(len({x.casefold() for x in job["edit_paths"]}) == len(job["edit_paths"]), "case-colliding edit paths")
    need(bool(job["edit_paths"]) == (job["mode"] == "edit"), "review cannot edit; edit requires exact files")
    integer(job["max_steps"], 1, 64, "max_steps")
    integer(job["max_runtime_sec"], 30, 7200, "max_runtime_sec")
    strings(job.get("source_locators", []), "source locators", maximum=60)
    reads = strings(job.get("read_paths", []), "read paths", maximum=200)
    for path in reads: relative_file(path, read_only=True)
    need(len({x.casefold() for x in reads}) == len(reads), "case-colliding read paths")
    strings(job.get("required_tools", []), "required tools", maximum=20, maxlen=200)
    if "context_note" in job:
        need(isinstance(job["context_note"], str) and len(job["context_note"]) <= 12000, "invalid context note")
    return job


def verify_file_record(record: dict) -> Path:
    need(isinstance(record, dict) and set(record) == {"path", "sha256"}, "expected path+sha256 record")
    path = Path(record["path"])
    need(path.is_absolute() and path.is_file() and not path.is_symlink(), "evidence file is missing, relative or linked")
    need(isinstance(record["sha256"], str) and bool(HASH.fullmatch(record["sha256"])), "invalid evidence hash")
    need(sha256_file(path) == record["sha256"], "evidence hash mismatch")
    return path


def check_fresh(timestamp: str, hours: int = 24) -> None:
    try:
        when = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        need(when.tzinfo is not None, "timestamp must contain timezone")
        age = (datetime.now(timezone.utc) - when).total_seconds()
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkerError("invalid timestamp") from exc
    need(-120 <= age <= hours * 3600, "stale or future capability record; refresh without inference")


def tool_action(server: str, tool: str) -> str:
    # OpenCode V2 preserves '-' (Context7 resolve-library-id is NOT resolve_library_id).
    return re.sub(r"[^A-Za-z0-9_-]", "_", server) + "_" + re.sub(r"[^A-Za-z0-9_-]", "_", tool)


def no_reparse(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    need(not stat.S_ISLNK(info.st_mode), "symlinks are not accepted in worker paths")
    need(not getattr(info, "st_file_attributes", 0) & 0x400, "Windows reparse point denied")


def inside(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise WorkerError("path escaped workspace") from exc
    cur = path
    while cur != root and cur != cur.parent:
        no_reparse(cur)
        cur = cur.parent
    no_reparse(root)


def _substitute(value, workspace: Path, run_dir: Path):
    if isinstance(value, str):
        return value.replace("{workspace}", str(workspace)).replace("{run_dir}", str(run_dir))
    if isinstance(value, list):
        return [_substitute(v, workspace, run_dir) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, workspace, run_dir) for k, v in value.items()}
    return value


def check_config_secrets(config: dict) -> None:
    """Reject inline credentials in trusted MCP descriptors before saving a plan.

    Host manifests contain references, not secrets. This is a guard against
    accidental leakage, not a universal secret classifier.
    """
    env_ref = re.compile(r"\{env:[A-Z][A-Z0-9_]*\}\Z")
    for field in ("headers", "environment", "env"):
        values = config.get(field, {})
        need(isinstance(values, dict), "MCP " + field + " must be a mapping")
        for name, value in values.items():
            need(isinstance(name, str) and isinstance(value, str), "MCP values must be strings")
            sensitive = bool(re.search(r"(?i)token|password|secret|api.?key|authorization|cookie|credential", name))
            if sensitive:
                candidate = value.removeprefix("Bearer ").removeprefix("bearer ")
                need(bool(env_ref.fullmatch(candidate)), "MCP credentials must use environment references")
    raw = json.dumps(config, ensure_ascii=False)
    need(not re.search(r"sk-or-[A-Za-z0-9_-]+", raw), "inline OpenRouter key in MCP config")
    if config.get("type") == "remote":
        need(not urlparse(config.get("url", "")).query, "MCP URL query is forbidden; use credential headers")
    for item in config.get("command", []):
        need(not re.search(r"(?i)(?:--?|/)(?:api.?key|token|password|secret)(?:=|:|$)", item), "credential arguments are forbidden")


def validate_host(host: dict, job: dict, *, evidence: bool = True) -> None:
    need(isinstance(host, dict) and host.get("schema") == "vkm.opencode.host/2", "invalid host manifest")
    need(host.get("platform") == "windows-native", "native Windows required")
    if 'image_admission' in host:
        image_records=host['image_admission']
        need(isinstance(image_records,dict) and set(image_records)<={'hydrogen','opus'},'invalid image admission profile mapping')
        for image_record in image_records.values():
            need(isinstance(image_record,dict) and set(image_record)=={'path','sha256'},'invalid image admission file record')
            if evidence: verify_file_record(image_record)
    if 'output_rate_policy' in host:
        rates = host['output_rate_policy']
        need(isinstance(rates, dict) and set(rates) <= {'hydrogen'}, 'unsupported output rate policy profile')
        for record in rates.values():
            need(isinstance(record, dict) and set(record) == {'path', 'sha256'}, 'invalid output rate proof record')
            if evidence: verify_file_record(record)
    op = host.get("opencode", {})
    need(isinstance(op.get("path"), str) and bool(HASH.fullmatch(op.get("sha256", ""))), "OpenCode executable hash missing")
    version = op.get("version", "")
    need(isinstance(version, str) and bool(re.fullmatch(r"2\.\d+\.\d+", version)), "pin an exact stable OpenCode V2 version")
    need(tuple(map(int, version.split("."))) >= (2, 2, 6), "OpenCode >=2.2.6 is required for current policy actions")
    profile = host.get("profiles", {}).get(job["profile"])
    need(isinstance(profile, dict), "profile not configured by Codex")
    model = profile.get("model_id", "")
    need(isinstance(model, str) and bool(re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.:/-]+", model)) and not model.startswith("openrouter/"), "model_id must be the exact OpenRouter vendor/model ID")
    strings(profile.get("provider_only"), "provider pins", nonempty=True, maximum=4, maxlen=120)
    need(all(re.fullmatch(r"[a-z0-9_./-]+", x) for x in profile["provider_only"]), "invalid endpoint pin")
    need(profile.get("reasoning_effort") in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}, "unsupported reasoning policy")
    integer(profile.get("max_output_tokens"), 256, 262144, "max output tokens")
    if evidence:
        check_fresh(profile.get("checked_at", ""))
        verify_file_record(profile.get("capability_receipt"))
    for name in job["mcp"]:
        server = host.get("mcp", {}).get(name)
        need(isinstance(server, dict), f"MCP not configured: {name}")
        config = server.get("config", {})
        need(isinstance(config, dict), "MCP config must be an object")
        check_config_secrets(config)
        need(config.get("type") in {"local", "remote"}, "invalid MCP transport")
        if config["type"] == "local":
            command = strings(config.get("command"), "MCP command", nonempty=True)
            need(Path(command[0]).is_absolute() or PureWindowsPath(command[0]).is_absolute(), "MCP must use pinned absolute executable")
            need(not any("git+" in x or x in {"-y", "--latest"} or "@latest" in x for x in command), "no install-on-launch or floating MCP dependency")
        else:
            u = urlparse(config.get("url", ""))
            need(u.scheme in {"http", "https"} and u.hostname and not u.username and not u.password and not u.fragment, "invalid MCP URL")
            need(u.scheme == "https" or server.get("private_network_authorized") is True, "plain HTTP requires explicit private-network qualification")
        need(name != "graphify" or config["type"] == "local", "Graphify in this bundle is LOCAL; no hosted substitution")
        names = strings(server.get("allowed_tools"), "MCP tool names", nonempty=True, maximum=100, maxlen=150)
        actions = [tool_action(name, x) for x in names]
        need(len(actions) == len(set(actions)), "tool-name normalization collision")
        need(not any("*" in x or "?" in x for x in names), "MCP wildcard grant denied")
        need(server.get("access") in {"read", "scoped-write"}, "MCP access classification missing")
        need(job["mode"] == "edit" or server["access"] == "read", "review task requested a writing MCP")
        if server["access"] == "scoped-write":
            need(server.get("scope_enforced") is True, "MCP mutations need a verified path-scope adapter")
        if evidence:
            probe = read_json(verify_file_record(server.get("tools_receipt")))
            tool_names = {x["name"] for x in probe.get("tools", []) if isinstance(x, dict) and isinstance(x.get("name"), str)}
            need(set(names) <= tool_names, f"MCP permissions do not match tools/list: {name}")
    granted = {tool_action(n, t) for n in job["mcp"] for t in host["mcp"][n]["allowed_tools"]}
    need(set(job.get("required_tools", [])) <= granted | {"read", "grep", "glob", "edit", "write", "patch", "shell", "skill"}, "required tool is not granted")
    strings(host.get("approved_plugins", []), "approved plugins", maximum=80, maxlen=200)
    need(not any("*" in v or "?" in v for v in host.get("approved_plugins", [])), "plugin wildcard denied")
    b = host.get("budget", {})
    need(finite_money(b.get("authorized_total_usd"), "batch budget") > 0, "no authorized budget")
    finite_money(b.get("baseline_usage_usd"), "baseline usage")
    need(isinstance(b.get("authorization_id"), str) and b["authorization_id"], "budget authorization ID missing")
    need(isinstance(b.get("key_fingerprint"), str) and bool(HASH.fullmatch(b["key_fingerprint"])), "budget must be bound to the existing key")
    if evidence:
        verify_file_record(b.get("authorization_receipt"))


def build_config(job: dict, host: dict, tree: Path, run_dir: Path, *, source_root: Path | None = None) -> dict:
    validate_host(host, job, evidence=False)
    p = host["profiles"][job["profile"]]
    model = "openrouter/" + p["model_id"]
    skill = SCENARIOS[job["scenario"]]
    rules = [{"action": "*", "resource": "*", "effect": "deny"}]
    grants = []
    def allow(action: str, resource: str = "*"):
        rules.append({"action": action, "resource": resource, "effect": "allow"})
        grants.append((action, resource))
    # Native V2 read:* can read siblings even with external_directory denied.
    # Exact Location-relative files were independently qualified. Broad glob /
    # grep stay denied until their own argument/path confinement is qualified.
    for rel in sorted(set(job.get("read_paths", [])) | set(job["edit_paths"])):
        relative_file(rel, read_only=True)
        allow("read", rel)
    allow("skill", skill)
    # Exact commands only. Tests execute repository code with host authority.
    for command in host.get("shell_commands", {}).get(job["scenario"], []):
        need(isinstance(command, str) and not any(c in command for c in "\n\r*?;&|`<>"), "shell grant must be an exact, audited command")
        need(job["mode"] == "edit" or command in {"git status --short", "git diff --stat", "git diff --check"}, "review shell command not read-only")
        allow("shell", command)
    for rel in job["edit_paths"]:
        allow("edit", rel)  # V2 INTERNAL path checks are Location-relative.
        allow("edit", (tree / rel).as_posix())
        allow("write", rel)
        allow("write", (tree / rel).as_posix())
    servers = {}
    for name in job["mcp"]:
        data = host["mcp"][name]
        cfg = _substitute(data["config"], tree, run_dir)
        cfg["codemode"] = False
        cfg["disabled"] = False
        servers[name] = cfg
        for tool in data["allowed_tools"]:
            allow(tool_action(name, tool))
    # Mirror the positive tool list as a hard policy; saved approvals/agent
    # permissions cannot enable additional actions.
    policies = [
        {"action": "provider.use", "resource": "*", "effect": "deny"},
        {"action": "provider.use", "resource": "openrouter", "effect": "allow"},
        {"action": "mcp.use", "resource": "*", "effect": "deny"},
        {"action": "plugin.use", "resource": "*", "effect": "deny"},
        {"action": "skill.use", "resource": "*", "effect": "deny"},
        {"action": "skill.use", "resource": skill, "effect": "allow"},
        {"action": "tool.use", "resource": "*", "effect": "deny"},
    ]
    for plugin in host.get("approved_plugins", []):
        policies.append({"action": "plugin.use", "resource": plugin, "effect": "allow"})
    for name in servers:
        policies.append({"action": "mcp.use", "resource": name, "effect": "allow"})
    for action, resource in grants:
        policies.append({"action": "tool.use", "resource": action + ":" + resource, "effect": "allow"})
    for resource in ("external_directory:*", "read:*.env", "read:*.env.*", "read:*/.git/*", "read:.git", "read:*/.ssh/*", "read:*credentials*", "edit:AGENTS.md", "edit:*/AGENTS.md", "edit:.opencode/*", "edit:*/.opencode/*"):
        policies.append({"action": "tool.use", "resource": resource, "effect": "deny"})
    # No hosted Graphify, Code Mode, recursive worker or blanket 'auto' grant.
    agents = {
        "vkm-worker": {"mode": "primary", "model": model, "description": "Bounded VKM worker controlled by Codex", "steps": job["max_steps"], "permissions": rules},
        **{name: {"model": model} for name in ("title", "summary", "compaction")},
    }
    model_config={"limit":{"output":p["max_output_tokens"]}}
    if job['profile'] in host.get('image_admission',{}):
        model_config['modalities']={'input':['text','image'],'output':['text']}
    config = {
        "$schema": "https://opencode.ai/config.json", "model": model,
        "default_agent": "vkm-worker", "warming": False, "snapshots": False,
        "formatter": False, "tool_output": {"max_lines": 400, "max_bytes": 24000},
        "providers": {"openrouter": {"settings": {"apiKey": "VKM_RUNTIME_GATE_REQUIRED_INVALID"},
            "models": {p["model_id"]: model_config},
            "headers": {"X-OpenRouter-Metadata": "enabled"},
            "body": {"provider": {"only": p["provider_only"], "allow_fallbacks": False, "require_parameters": True},
                     "reasoning": {"enabled": True, "effort": p["reasoning_effort"], "exclude": True},
                     "usage": {"include": True}}}},
        "agents": agents, "permissions": rules, "policies": policies,
        "mcp": {"servers": servers}, "websearch": False,
        "compaction": {"auto": False},
        "plugins": host.get("plugin_directories", []),
    }
    if job['profile'] in host.get('image_admission', {}):
        from image_admission import ImageAdmission
        policy = read_json(verify_file_record(host['image_admission'][job['profile']]))
        ImageAdmission(policy, p, task_id=job['task_id'])
        sizes = []
        for source in policy['sources']:
            name = relative_file(source['path'], read_only=True)
            need(name in job.get('read_paths', []), 'native image limit source outside read paths')
            # Plan precedes worktree creation; caller may supply the verified
            # input checkout. Runtime independently verifies the new checkout.
            input_root = source_root if source_root is not None else tree
            target = input_root / name
            inside(target, input_root)
            verify_file_record({'path': str(target), 'sha256': source['sha256']})
            sizes.append(4 * ((target.stat().st_size + 2) // 3))
        need(max(sizes) + 1024 * 1024 <= 32 * 1024 * 1024, 'approved image event exceeds existing total stdio bound')
        config['media'] = {'image': {
            'auto_resize': False,
            'max_width': max(x['width'] for x in policy['sources']),
            'max_height': max(x['height'] for x in policy['sources']),
            'max_base64_bytes': max(sizes),
        }}
    return config


def validate_generated_config(config: dict) -> None:
    """Reject unknown native statements instead of relying on ignored warnings."""
    if 'media' in config:
        media = config['media']
        need(isinstance(media, dict) and set(media) == {'image'}, 'unqualified native media config')
        image = media['image']
        need(isinstance(image, dict) and set(image) == {'auto_resize', 'max_width', 'max_height', 'max_base64_bytes'}, 'invalid native image limits')
        need(image['auto_resize'] is False, 'native image auto transform forbidden')
        need(all(type(image[k]) is int and image[k] > 0 for k in ['max_width', 'max_height', 'max_base64_bytes']), 'invalid native image bound')
    policy_actions = {"provider.use", "mcp.use", "plugin.use", "skill.use", "tool.use"}
    permission_actions = {"*", "read", "edit", "write", "shell", "skill"}
    for field, known in (("policies", policy_actions), ("permissions", permission_actions)):
        rows = config.get(field)
        need(isinstance(rows, list), "generated permission/policy list missing")
        for row in rows:
            need(isinstance(row, dict) and set(row) == {"action", "resource", "effect"}, "invalid generated statement")
            action = row["action"]
            need(action in known or field == "permissions" and bool(re.fullmatch(r"[A-Za-z0-9_-]+_[A-Za-z0-9_-]+", action)), "unknown generated action")
            need(row["effect"] in {"allow", "deny"} and isinstance(row["resource"], str), "invalid generated effect/resource")
            need(not (field == "permissions" and row["effect"] == "allow" and action in {"read", "edit", "write"} and any(c in row["resource"] for c in "*?[]{}")), "file permission must be exact")


def check_budget(data: dict, budget: dict, key: str) -> dict:
    """Verify an existing, finite, non-resetting server key cap for the WHOLE batch.

Not a per-request/turn cap and not proof of exclusive cost attribution.
    """
    need(hashlib.sha256(key.encode()).hexdigest() == budget["key_fingerprint"], "key differs from authorized batch; no rotation")
    need(isinstance(data, dict), "bad key response")
    need(data.get("limit") is not None and data.get("limit_remaining") is not None, "OpenRouter key is unlimited/unconfirmed")
    need(data.get("limit_reset") is None, "auto-resetting key cannot bound a one-off batch")
    need(data.get("include_byok_in_limit") is True, "key cap must include possible BYOK spend")
    limit = finite_money(data["limit"], "key limit")
    remaining = finite_money(data["limit_remaining"], "remaining key limit")
    usage = finite_money(data.get("usage"), "key usage")
    base = finite_money(budget["baseline_usage_usd"])
    total = finite_money(budget["authorized_total_usd"])
    need(limit > 0 and remaining > 0, "no remaining key budget")
    need(usage >= base, "usage counter regressed")
    spent = usage - base
    need(spent <= total and remaining <= total - spent, "server key cap exceeds remaining authorized batch budget")
    need(remaining <= limit, "inconsistent key limit metadata")
    return {"usage": str(usage), "remaining": str(remaining), "observed_batch_spend": str(spent), "basis": "OPENROUTER_KEY_METADATA", "checked_at": utc()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WorkerError("redirect rejected for credentialed OpenRouter request")


def get_key_info(key: str) -> dict:
    need(isinstance(key, str) and key and "\n" not in key and "\r" not in key, "missing/invalid key")
    req = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=20) as response:
            raw = response.read(262145)
        need(len(raw) <= 262144, "key metadata too large")
        result = parse_json(raw.decode("utf-8"))
        need(isinstance(result, dict) and isinstance(result.get("data"), dict), "invalid key metadata shape")
        return result["data"]
    except WorkerError:
        raise
    except Exception as exc:
        raise WorkerError("OpenRouter key verification failed: " + type(exc).__name__) from None


def sanitize(text: str, secrets: list[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"sk-or-[A-Za-z0-9_-]+", "[REDACTED_OPENROUTER_KEY]", text)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1[REDACTED]", text)
    return text


class Events:
    """Keep control metadata and assistant TEXT, not reasoning/tool payloads."""
    def __init__(self, secrets=()):
        self.secrets = list(secrets)
        self.count = self.bad = self.errors = 0
        self.sessions = set()
        self.tools = set()
        self.finished_tools = set()
        self.types = {}
        self.text_parts = []
        self.truncated_completion = False

    def feed(self, line: str) -> dict:
        try:
            e = parse_json(line)
        except WorkerError:
            self.bad += 1
            return {"type": "invalid_json", "sha256": hashlib.sha256(line.encode()).hexdigest()}
        if not isinstance(e, dict) or not isinstance(e.get("type"), str):
            self.bad += 1
            return {"type": "invalid_event_shape"}
        typ = e["type"][:120]
        self.count += 1
        self.types[typ] = self.types.get(typ, 0) + 1
        sid = e.get("sessionID", e.get("session_id"))
        if isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
            self.sessions.add(sid)
        part = e.get("part") if isinstance(e.get("part"), dict) else {}
        if typ in {"error", "session.error"} or e.get("error"):
            self.errors += 1
        if part.get("reason") in {"length", "max_tokens"} or e.get("finish_reason") in {"length", "max_tokens"}:
            self.truncated_completion = True
        tool = part.get("tool", e.get("tool"))
        status = part.get("state", {}).get("status") if isinstance(part.get("state"), dict) else e.get("status")
        meta = {"type": typ, "session_id": sid if sid in self.sessions else None}
        if isinstance(tool, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", tool):
            self.tools.add(tool)
            meta.update(tool=tool, status=status if status in {"pending", "running", "completed", "error"} else None)
            if status == "completed":
                self.finished_tools.add(tool)
            if status == "error":
                self.errors += 1
        # Skip reasoning even if a future runtime embeds a text field in it.
        if typ == "text" and part.get("type", "text") == "text":
            text = part.get("text", e.get("text"))
            if isinstance(text, str):
                self.text_parts.append(sanitize(text, self.secrets))
                if sum(len(t) for t in self.text_parts) > 131072:
                    self.text_parts = ["[earlier assistant text omitted]\n", "".join(self.text_parts)[-65536:]]
        return meta

    def summary(self) -> dict:
        return {"event_count": self.count, "invalid_lines": self.bad, "errors": self.errors,
                "types": self.types, "sessions": sorted(self.sessions), "tools_observed": sorted(self.tools),
                "completed_tools": sorted(self.finished_tools), "completion_truncated": self.truncated_completion,
                "cost_usd": None, "cost_basis": "NOT_INFERRED_FROM_TRANSCRIPT"}
