"""No-model Codex app-server MCP client smoke; receipts must stay in ignored work.

Uses an ephemeral technical thread, never starts a turn or creates a user chat.
The bearer token is read only into the child's environment, never into receipts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time


class AppServer:
    def __init__(self, executable: str, cwd: Path, output: Path, token_file: Path, scratch_root: Path | None):
        self.output = output
        self.secret = token_file.read_text(encoding="utf-8").strip()
        if not self.secret:
            raise ValueError("Empty MCP token file")
        env = dict(os.environ, VKM_MCP_TOKEN=self.secret, OMP_NUM_THREADS="2",
                   OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2", PYTHONUTF8="1")
        command = [executable, "app-server", "--stdio"]
        if scratch_root:
            for server in ("vkm-cad", "vkm-drawio", "vkm-qgis"):
                command.extend(["-c", f"mcp_servers.{server}.env.VKM_WORK={json.dumps(str(scratch_root))}"])
        self.process = subprocess.Popen(command, cwd=cwd,
                                        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.incoming: queue.Queue = queue.Queue()
        self.events = []
        self.stderr = []
        self.counter = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._errors, daemon=True).start()

    def clean(self, value):
        return json.loads(json.dumps(value, ensure_ascii=False).replace(self.secret, "[REDACTED]"))

    def _read(self):
        for line in self.process.stdout:
            try:
                self.incoming.put(json.loads(line))
            except json.JSONDecodeError:
                self.events.append({"unparsed_stdout": self.clean(line)})
        self.incoming.put({"transport_closed": True})

    def _errors(self):
        for line in self.process.stderr:
            self.stderr.append(line.replace(self.secret, "[REDACTED]"))

    def request(self, method: str, params: dict, timeout=180):
        self.counter += 1
        msg = {"id": self.counter, "method": method, "params": params}
        self.events.append({"time_utc": datetime.now(timezone.utc).isoformat(), "request": self.clean(msg)})
        self.process.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = self.incoming.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise TimeoutError(f"Timed out waiting for {method}") from exc
            self.events.append({"time_utc": datetime.now(timezone.utc).isoformat(), "received": self.clean(response)})
            if response.get("id") == self.counter:
                self.save()
                if "error" in response:
                    raise RuntimeError(f"{method}: {response['error']}")
                return response["result"]
            if response.get("transport_closed"):
                raise RuntimeError("app-server stdout closed")
            if "id" in response and "method" in response:
                # Technical smoke never grants interactive approvals.
                reply = {"id": response["id"], "error": {"code": -32601,
                         "message": "Interactive server request unsupported by smoke client"}}
                self.process.stdin.write(json.dumps(reply) + "\n")
                self.process.stdin.flush()
        raise TimeoutError(method)

    def save(self):
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "protocol.json").write_text(json.dumps(self.events, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.output / "stderr.log").write_text("".join(self.stderr), encoding="utf-8")

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.save()


def unpack(result):
    """Unwrap MCP CallToolResult through the app-server result envelope."""
    value = result.get("result", result)
    if value.get("isError"):
        raise RuntimeError(f"MCP tool error: {value}")
    if value.get("structuredContent") is not None:
        value = value["structuredContent"]
        if isinstance(value, dict) and value.get("ok") is False:
            raise RuntimeError(f"MCP operation failed: {value}")
        return value
    texts = [c["text"] for c in value.get("content", []) if c.get("type") == "text"]
    if len(texts) == 1:
        try:
            parsed = json.loads(texts[0])
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                raise RuntimeError(f"MCP operation failed: {parsed}")
            return parsed
        except json.JSONDecodeError:
            return {"text": texts[0]}
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--token-file", type=Path, default=Path.home() / ".vkm/secrets/mcp_token")
    parser.add_argument("--plan", type=Path, help="Optional ordered calls; args may reference prior result paths via $ref")
    parser.add_argument("--scratch-root", type=Path, help="Temporary process-only VKM_WORK override for stdio servers")
    parser.add_argument("--require-skill", default="openrouter-delegate")
    args = parser.parse_args()
    if not args.codex:
        parser.error("codex executable not found")
    args.cwd = args.cwd.resolve()
    args.output = args.output.resolve()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Use a new empty attempt output directory")
    scratch_root = args.scratch_root.resolve() if args.scratch_root else None
    started = time.monotonic()
    config = args.cwd / ".codex/config.toml"
    config_before = hashlib.sha256(config.read_bytes()).hexdigest() if config.exists() else None
    client = AppServer(args.codex, args.cwd, args.output, args.token_file, scratch_root)
    summary = {"scope": "new app-server technical client, ephemeral thread, no turn/model/UI chat",
               "cwd": str(args.cwd), "executable": args.codex, "model_turns_requested": 0,
               "scratch_root": str(scratch_root) if scratch_root else None,
               "started_at_utc": datetime.now(timezone.utc).isoformat(),
               "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "plan_sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest() if args.plan else None,
               "project_config_before_sha256": config_before, "calls": {}}
    def record(name, result):
        summary["calls"][name] = client.clean(result)
        (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        record("initialize", client.request("initialize", {"clientInfo": {"name": "vkm_env_smoke", "version": "1.0.0"},
               "capabilities": {"experimentalApi": True}}))
        thread = client.request("thread/start", {"cwd": str(args.cwd), "ephemeral": True})
        # Keep only technical identity, avoiding inherited user/model configuration in receipts.
        thread_id = thread["thread"]["id"]
        record("thread", {"id": thread_id, "ephemeral": True})
        inventory = client.request("mcpServerStatus/list", {"threadId": thread_id, "limit": 100}, timeout=240)
        record("mcp_inventory", inventory)
        skills = client.request("skills/list", {"cwds": [str(args.cwd)], "forceReload": True})
        record("skills", skills)
        if args.plan:
            results = {}
            def resolve(value):
                if isinstance(value, dict) and set(value) == {"$ref"}:
                    parts = value["$ref"].split(".")
                    obj = results[parts.pop(0)]
                    for part in parts:
                        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
                    return obj
                if isinstance(value, dict):
                    return {key: resolve(val) for key, val in value.items()}
                if isinstance(value, list):
                    return [resolve(val) for val in value]
                return value
            failed_calls = []
            for call in json.loads(args.plan.read_text(encoding="utf-8-sig")):
                params = {"threadId": thread_id, "server": call["server"], "tool": call["tool"],
                          "arguments": resolve(call.get("arguments", {}))}
                try:
                    results[call["id"]] = unpack(client.request("mcpServer/tool/call", params, timeout=call.get("timeout", 180)))
                    record(call["id"], results[call["id"]])
                except Exception as exc:
                    failed_calls.append(call["id"])
                    record(call["id"], {"error": client.clean(str(exc))})
                    if not call.get("continue_on_error", False):
                        raise
            if failed_calls:
                raise RuntimeError(f"Failed MCP calls: {failed_calls}")
        names = {server["name"] for server in inventory["data"]}
        expected = {"vkm-cad", "vkm-corpus", "vkm-drawio", "vkm-qgis"}
        if not expected.issubset(names):
            raise RuntimeError(f"Missing MCP servers: {sorted(expected - names)}")
        empty = [server["name"] for server in inventory["data"]
                 if server["name"] in expected and not server.get("tools")]
        if empty:
            raise RuntimeError(f"MCP servers without tools: {empty}")
        repo_skills = [skill for group in skills.get("data", []) for skill in group.get("skills", [])
                       if skill.get("name") == args.require_skill and skill.get("enabled") is not False
                       and Path(skill.get("path", "")).is_relative_to(args.cwd)]
        if not repo_skills:
            raise RuntimeError(f"Required repo skill absent: {args.require_skill}")
        summary["status"] = "PASS"
    except Exception as exc:
        summary.update(status="FAIL", error=client.clean(str(exc)))
    finally:
        client.close()
        summary["project_config_sha256"] = hashlib.sha256(config.read_bytes()).hexdigest() if config.exists() else None
        summary["app_server_exit_code"] = client.process.returncode
        summary["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if summary["project_config_sha256"] != config_before:
            summary.update(status="FAIL", error="Project config changed during the smoke; inspect concurrent writer")
        (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": summary["status"], "output": str(args.output), "error": summary.get("error")}, ensure_ascii=False))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
