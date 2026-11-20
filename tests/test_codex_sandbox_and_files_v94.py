from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


class _LoopbackOrRemoteAsgiClient:
    """Minimal ASGI test client allowing explicit `client_ip` injection for loopback Zero-Trust checks."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def request(
        self,
        method: str,
        path: str,
        *,
        client_ip: str = "127.0.0.1",
        headers: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> tuple[int, dict[str, str], bytes]:
        path_only, _, query_str = path.partition("?")
        raw_body = (
            json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            if json_body is not None
            else b""
        )
        hdrs: list[tuple[bytes, bytes]] = [
            (b"host", b"127.0.0.1:8765"),
        ]
        if json_body is not None:
            hdrs.append((b"content-type", b"application/json"))
        for k, v in (headers or {}).items():
            hdrs.append((k.lower().encode("latin-1"), v.encode("latin-1")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path_only,
            "raw_path": path_only.encode("utf-8"),
            "query_string": query_str.encode("utf-8"),
            "headers": hdrs,
            "client": (client_ip, 54321),
            "server": ("127.0.0.1", 8765),
        }

        sent_body = False
        resp_status = 500
        resp_headers: dict[str, str] = {}
        resp_chunks: list[bytes] = []

        async def receive() -> dict[str, Any]:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(event: dict[str, Any]) -> None:
            nonlocal resp_status
            if event["type"] == "http.response.start":
                resp_status = int(event["status"])
                for hk, hv in event.get("headers") or []:
                    resp_headers[hk.decode("latin-1").lower()] = hv.decode("latin-1")
            elif event["type"] == "http.response.body":
                resp_chunks.append(event.get("body") or b"")

        await self.app(scope, receive, send)
        return resp_status, resp_headers, b"".join(resp_chunks)


def _unlink_junction_or_symlink(p: Path) -> None:
    if not p.exists() and not p.is_symlink():
        if not (hasattr(os.path, "isjunction") and os.path.isjunction(p)):
            return
    try:
        if os.name == "nt" and hasattr(os.path, "isjunction") and os.path.isjunction(p):
            os.rmdir(p)
        else:
            p.unlink(missing_ok=True)
    except OSError:
        try:
            os.rmdir(p)
        except OSError:
            pass


MOCK_APPROVAL_CODEX_SCRIPT = textwrap.dedent(
    r"""
    import json
    import os
    import pathlib
    import sys

    def main() -> int:
        args = sys.argv[1:]
        if not args or args[0] != "app-server":
            return 1

        codex_home = pathlib.Path(os.environ.get("CODEX_HOME", "."))
        cfg_text = (codex_home / "config.toml").read_text(encoding="utf-8") if (codex_home / "config.toml").is_file() else ""
        rpc_log_path = os.environ.get("MOCK_V94_RPC_LOG", "").strip()

        def log_event(obj):
            if rpc_log_path:
                with open(rpc_log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

        log_event({"event": "startup_config_toml", "toml": cfg_text})

        pending_checks = []
        thread_id = "thr_v94_mock"

        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            msg = json.loads(line)
            rid = msg.get("id")
            method = msg.get("method")
            params = msg.get("params") or {}

            if method == "initialize":
                log_event({"event": "initialize", "params": params})
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"userAgent": "mock-v94"}}), flush=True)
            elif method == "thread/start":
                log_event({"event": "thread/start", "params": params})
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": thread_id}}}), flush=True)
            elif method == "turn/start":
                log_event({"event": "turn/start", "params": params})
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": "turn_v94", "status": "inProgress"}}}), flush=True)

                # Emit tracked in-workspace fileChange item before requesting approval
                in_ws_file = str(pathlib.Path(params.get("cwd") or ".") / "src" / "main.py")
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "method": "item/started",
                    "params": {
                        "item": {
                            "id": "fc_ok",
                            "type": "fileChange",
                            "status": "inProgress",
                            "changes": [{"path": in_ws_file, "kind": "update"}],
                        }
                    },
                }), flush=True)

                # Emit tracked out-of-workspace fileChange item
                print(json.dumps({
                    "jsonrpc": "2.0",
                    "method": "item/started",
                    "params": {
                        "item": {
                            "id": "fc_escape",
                            "type": "fileChange",
                            "status": "inProgress",
                            "changes": [{"path": "../outside_secret.txt", "kind": "update"}],
                        }
                    },
                }), flush=True)

                # Send batch of server-to-client approval & elicitation requests (ids 501..507)
                reqs = [
                    {
                        "jsonrpc": "2.0",
                        "id": 501,
                        "method": "mcpServer/elicitation/request",
                        "params": {
                            "serverName": "hub_sandbox",
                            "threadId": thread_id,
                            "turnId": "turn_v94",
                            "mode": "form",
                            "message": 'Allow the hub_sandbox MCP server to run tool "sandbox_exec"?',
                            "_meta": {"codex_approval_kind": "mcp_tool_call"},
                            "requestedSchema": {"type": "object", "properties": {}},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 502,
                        "method": "mcpServer/elicitation/request",
                        "params": {
                            "serverName": "evil_third_party_mcp",
                            "threadId": thread_id,
                            "turnId": "turn_v94",
                            "mode": "form",
                            "message": "Run untrusted external tool?",
                            "_meta": {"codex_approval_kind": "mcp_tool_call"},
                            "requestedSchema": {"type": "object", "properties": {}},
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 503,
                        "method": "item/tool/requestUserInput",
                        "params": {
                            "threadId": thread_id,
                            "turnId": "turn_v94",
                            "itemId": "mcp_files_1",
                            "isBlocking": True,
                            "questions": [
                                {
                                    "id": "mcp_tool_call_approval_files",
                                    "header": "Approve app tool call?",
                                    "question": 'Allow the hub_files MCP server to run tool "write_file"?',
                                    "options": [
                                        {"label": "Allow", "description": "Run once"},
                                        {"label": "Allow for this session", "description": "Session"},
                                        {"label": "Cancel", "description": "Cancel"},
                                    ],
                                },
                                {
                                    "id": "mcp_tool_call_approval_rogue",
                                    "header": "Approve app tool call?",
                                    "question": 'Allow the rogue_mcp MCP server to run tool "steal"?',
                                    "options": [
                                        {"label": "Allow", "description": "Run once"},
                                        {"label": "Cancel", "description": "Cancel"},
                                    ],
                                },
                            ],
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 504,
                        "method": "item/fileChange/requestApproval",
                        "params": {
                            "threadId": thread_id,
                            "turnId": "turn_v94",
                            "itemId": "fc_ok",
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 505,
                        "method": "item/fileChange/requestApproval",
                        "params": {
                            "threadId": thread_id,
                            "turnId": "turn_v94",
                            "itemId": "fc_escape",
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 506,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "threadId": thread_id,
                            "turnId": "turn_v94",
                            "itemId": "cmd_host_1",
                            "command": "whoami",
                        },
                    },
                    {
                        "jsonrpc": "2.0",
                        "id": 507,
                        "method": "item/permissions/requestApproval",
                        "params": {
                            "threadId": thread_id,
                            "turnId": "turn_v94",
                            "itemId": "perm_1",
                            "cwd": params.get("cwd") or ".",
                            "startedAtMs": 1700000000000,
                            "permissions": {"network": {"enabled": True}},
                        },
                    },
                ]
                pending_checks = [501, 502, 503, 504, 505, 506, 507]
                for rq in reqs:
                    print(json.dumps(rq), flush=True)

            elif rid in pending_checks:
                log_event({"event": "client_approval_reply", "id": rid, "msg": msg})
                pending_checks.remove(rid)
                if not pending_checks:
                    # Emit hub_files write_file mcpToolCall + hub_sandbox sandbox_exec mcpToolCall
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "id": "mcp_wf_1",
                                "type": "mcpToolCall",
                                "status": "completed",
                                "server": "hub_files",
                                "tool": "write_file",
                                "arguments": {"path": "src/generated.py", "content": "x = 1\n"},
                            }
                        },
                    }), flush=True)
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "id": "mcp_sbx_1",
                                "type": "mcpToolCall",
                                "status": "completed",
                                "server": "hub_sandbox",
                                "tool": "sandbox_exec",
                                "arguments": {"command": "pytest -q"},
                            }
                        },
                    }), flush=True)
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "item/agentMessage/delta",
                        "params": {"itemId": "am_1", "delta": "Все проверки пройдены в Docker и hub_files!"},
                    }), flush=True)
                    print(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn_v94", "status": "completed"}},
                    }), flush=True)
                    return 0
        return 0

    if __name__ == "__main__":
        raise SystemExit(main())
    """
).strip() + "\n"


class TestCodexSandboxAndFilesV94(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database

        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(prefix="agy_v94_test_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()

        self._orig = {
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_cli": config.CLI_HOMES_DIR,
            "cfg_ws": config.USER_WORKSPACES_DIR,
            "db_ws": database.USER_WORKSPACES_DIR,
            "br_ws": brain_reader.USER_WORKSPACES_DIR,
            "cfg_sh": config.SHARED_PROJECTS_DIR,
            "db_sh": database.SHARED_PROJECTS_DIR,
            "br_sh": brain_reader.SHARED_PROJECTS_DIR,
            "cfg_up": config.UPLOADS_DIR,
            "eng_up": agy_engine.UPLOADS_DIR,
            "cdx_up": codex_engine.UPLOADS_DIR,
            "cfg_sbx": config.CHAT_SANDBOX_DIR,
            "eng_sbx": agy_engine.CHAT_SANDBOX_DIR,
            "cdx_sbx": codex_engine.CHAT_SANDBOX_DIR,
            "env_codex_bin": os.environ.get("HUB_CODEX_BIN"),
            "env_rpc_log": os.environ.get("MOCK_V94_RPC_LOG"),
        }

        self.junctions_to_clean: list[Path] = []

        tmp_db = self.tmp_root / "test_hub.db"
        tmp_cli = self.tmp_root / "cli_homes"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sh = self.tmp_root / "shared_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_sbx = self.tmp_root / "chat_sandbox"
        for d in (tmp_cli, tmp_ws, tmp_sh, tmp_up, tmp_sbx):
            d.mkdir(parents=True, exist_ok=True)

        config.DB_PATH = tmp_db
        database.DB_PATH = tmp_db
        config.CLI_HOMES_DIR = tmp_cli
        config.USER_WORKSPACES_DIR = tmp_ws
        database.USER_WORKSPACES_DIR = tmp_ws
        brain_reader.USER_WORKSPACES_DIR = tmp_ws
        config.SHARED_PROJECTS_DIR = tmp_sh
        database.SHARED_PROJECTS_DIR = tmp_sh
        brain_reader.SHARED_PROJECTS_DIR = tmp_sh
        config.UPLOADS_DIR = tmp_up
        agy_engine.UPLOADS_DIR = tmp_up
        codex_engine.UPLOADS_DIR = tmp_up
        config.CHAT_SANDBOX_DIR = tmp_sbx
        agy_engine.CHAT_SANDBOX_DIR = tmp_sbx
        codex_engine.CHAT_SANDBOX_DIR = tmp_sbx

        await database.init_db()

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database

        agy_engine.ACTIVE_RUNS.clear()
        await asyncio.sleep(0.05)

        config.DB_PATH = self._orig["cfg_db"]
        database.DB_PATH = self._orig["db_db"]
        config.CLI_HOMES_DIR = self._orig["cfg_cli"]
        config.USER_WORKSPACES_DIR = self._orig["cfg_ws"]
        database.USER_WORKSPACES_DIR = self._orig["db_ws"]
        brain_reader.USER_WORKSPACES_DIR = self._orig["br_ws"]
        config.SHARED_PROJECTS_DIR = self._orig["cfg_sh"]
        database.SHARED_PROJECTS_DIR = self._orig["db_sh"]
        brain_reader.SHARED_PROJECTS_DIR = self._orig["br_sh"]
        config.UPLOADS_DIR = self._orig["cfg_up"]
        agy_engine.UPLOADS_DIR = self._orig["eng_up"]
        codex_engine.UPLOADS_DIR = self._orig["cdx_up"]
        config.CHAT_SANDBOX_DIR = self._orig["cfg_sbx"]
        agy_engine.CHAT_SANDBOX_DIR = self._orig["eng_sbx"]
        codex_engine.CHAT_SANDBOX_DIR = self._orig["cdx_sbx"]

        for env_k, orig_k in (
            ("HUB_CODEX_BIN", "env_codex_bin"),
            ("MOCK_V94_RPC_LOG", "env_rpc_log"),
        ):
            if self._orig[orig_k] is None:
                os.environ.pop(env_k, None)
            else:
                os.environ[env_k] = self._orig[orig_k]

        for jp in self.junctions_to_clean:
            _unlink_junction_or_symlink(jp)
        self._tmp.cleanup()

        if self._live_stat_before is not None:
            stat_now = (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            self.assertEqual(
                self._live_stat_before,
                stat_now,
                "CRITICAL: Live data/hub.db must NEVER be touched during tests!",
            )

    # ------------------------------------------------------------------
    # 1. Real `codex.exe --strict-config` + MCP `annotations` validation
    # ------------------------------------------------------------------
    async def test_01_real_codex_binary_strict_config_and_mcp_annotations(self) -> None:
        import app.codex_engine as codex_engine
        import app.config as config
        import app.openrouter_service as openrouter_service
        import app.sandbox_service as sandbox_service
        import app.workspace_mcp as workspace_mcp

        os.environ.pop("HUB_CODEX_BIN", None)
        real_tools_dir = ROOT_DIR / "data" / "tools" / "codex"
        with patch.object(config, "CODEX_TOOLS_DIR", real_tools_dir):
            real_bin = codex_engine.resolve_codex_bin()
        self.assertTrue(real_bin and Path(real_bin).is_file(), f"Expected real codex binary, got: {real_bin}")

        scenarios = [
            {
                "name": "plan_read_only",
                "sandbox_mode": "read-only",
                "shell_tool": False,
                "network_access": True,
                "files_mcp_url": "http://127.0.0.1:8765/mcp/files/tok_files_ro",
                "openrouter_mcp_url": "http://127.0.0.1:8765/mcp/openrouter/tok_or",
                "sandbox_mcp_url": "",
                "expected_approval": "on-request",
            },
            {
                "name": "code_write_files",
                "sandbox_mode": "workspace-write",
                "shell_tool": False,
                "network_access": True,
                "files_mcp_url": "http://127.0.0.1:8765/mcp/files/tok_files_rw",
                "openrouter_mcp_url": "",
                "sandbox_mcp_url": "",
                "expected_approval": "on-request",
            },
            {
                "name": "docker_sandbox",
                "sandbox_mode": "workspace-write",
                "shell_tool": False,
                "network_access": False,
                "files_mcp_url": "http://127.0.0.1:8765/mcp/files/tok_files_dk",
                "openrouter_mcp_url": "http://127.0.0.1:8765/mcp/openrouter/tok_or",
                "sandbox_mcp_url": "http://127.0.0.1:8765/mcp/sandbox/tok_sbx",
                "expected_approval": "on-request",
            },
            {
                "name": "host_full_admin",
                "sandbox_mode": "danger-full-access",
                "shell_tool": True,
                "network_access": True,
                "files_mcp_url": "",
                "openrouter_mcp_url": "http://127.0.0.1:8765/mcp/openrouter/tok_or",
                "sandbox_mcp_url": "",
                "expected_approval": "never",
            },
        ]

        for sc in scenarios:
            chome = self.tmp_root / f"codex_strict_{sc['name']}"
            cfg_path = codex_engine.write_codex_config_toml(
                chome,
                sandbox_mode=sc["sandbox_mode"],
                shell_tool=sc["shell_tool"],
                network_access=sc["network_access"],
                openrouter_mcp_url=sc["openrouter_mcp_url"],
                sandbox_mcp_url=sc["sandbox_mcp_url"],
                files_mcp_url=sc["files_mcp_url"],
            )
            toml_text = cfg_path.read_text(encoding="utf-8")
            first_line = toml_text.splitlines()[0]
            self.assertEqual(first_line, 'cli_auth_credentials_store = "file"')
            self.assertIn(f'approval_policy = "{sc["expected_approval"]}"', toml_text)

            env = os.environ.copy()
            env["CODEX_HOME"] = str(chome)
            env["CUDA_VISIBLE_DEVICES"] = ""
            argv = codex_engine._build_codex_exec_argv(
                real_bin, "app-server", "--strict-config", "--print-startup-config"
            )
            res = subprocess.run(
                argv,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                **config.subprocess_hidden_kwargs(),
            )
            self.assertEqual(
                res.returncode,
                0,
                f"--strict-config failed for {sc['name']}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}\nTOML:\n{toml_text}",
            )
            parsed_startup = json.loads(res.stdout.strip().splitlines()[-1])
            self.assertIn("appServerVersion", parsed_startup)

        # Verify MCP annotations (`destructiveHint: False` on all Hub MCP tools)
        sbx_tools = sandbox_service._mcp_tools_definitions()
        self.assertEqual({t["name"] for t in sbx_tools}, {"sandbox_exec", "sandbox_service_ctl", "sandbox_reset"})
        for t in sbx_tools:
            ann = t.get("annotations") or {}
            self.assertIs(ann.get("destructiveHint"), False, t)
            self.assertIs(ann.get("readOnlyHint"), False, t)
            self.assertIs(ann.get("openWorldHint"), False, t)

        or_tools = openrouter_service._build_mcp_tools_schema(
            {"default_model": "openai/gpt-4.1", "daily_limit_usd": 5.0, "custom_rules": ""}
        )
        self.assertEqual({t["name"] for t in or_tools}, {"openrouter_chat", "openrouter_multi_chat", "openrouter_models"})
        for t in or_tools:
            ann = t.get("annotations") or {}
            self.assertIs(ann.get("destructiveHint"), False, t)
            self.assertIs(ann.get("readOnlyHint"), True, t)
            expected_ow = t["name"] in ("openrouter_chat", "openrouter_multi_chat")
            self.assertIs(ann.get("openWorldHint"), expected_ow, t)

        files_tools = workspace_mcp._build_files_mcp_tools_schema(include_write_tools=True)
        self.assertEqual(
            {t["name"] for t in files_tools},
            {"read_file", "list_dir", "find_by_name", "grep_search", "write_file", "edit_file"},
        )
        for t in files_tools:
            ann = t.get("annotations") or {}
            self.assertIs(ann.get("destructiveHint"), False, t)
            self.assertIs(ann.get("openWorldHint"), False, t)
            expected_ro = t["name"] in ("read_file", "list_dir", "find_by_name", "grep_search")
            self.assertIs(ann.get("readOnlyHint"), expected_ro, t)

    # ------------------------------------------------------------------
    # 2. Zero-Trust JSON-RPC Auto-Approver & E2E `stream_codex_turn`
    # ------------------------------------------------------------------
    async def test_02_codex_jsonrpc_auto_approver_and_docker_turn_e2e(self) -> None:
        import app.codex_engine as codex_engine
        import app.database as database

        proj_dir = (self.tmp_root / "shared_projects" / "DockerCodexProj").resolve()
        (proj_dir / "src").mkdir(parents=True, exist_ok=True)
        (proj_dir / ".git").mkdir(parents=True, exist_ok=True)
        (proj_dir / ".git" / "config").write_text("[core]\n", encoding="utf-8")

        # Direct unit tests on `evaluate_codex_server_request` edge cases (read_only, legacy methods, url elicitation)
        r_url_elicit = codex_engine.evaluate_codex_server_request(
            {
                "id": 11,
                "method": "mcpServer/elicitation/request",
                "params": {"serverName": "hub_sandbox", "mode": "url", "url": "https://evil.example"},
            },
            trusted_mcp_servers=codex_engine.TRUSTED_HUB_MCP_SERVERS,
            shell_tool=False,
            effective_access_mode="full_access",
            kind="project",
            workspace_dir=proj_dir,
        )
        self.assertEqual(r_url_elicit["result"]["action"], "decline")

        r_ro_fc = codex_engine.evaluate_codex_server_request(
            {
                "id": 12,
                "method": "item/fileChange/requestApproval",
                "params": {"itemId": "fc_ro", "path": "src/app.py"},
            },
            trusted_mcp_servers=codex_engine.TRUSTED_HUB_MCP_SERVERS,
            shell_tool=False,
            effective_access_mode="read_only",
            kind="project",
            workspace_dir=proj_dir,
        )
        self.assertEqual(r_ro_fc["result"]["decision"], "decline")

        r_git_fc = codex_engine.evaluate_codex_server_request(
            {
                "id": 13,
                "method": "item/fileChange/requestApproval",
                "params": {"itemId": "fc_git", "path": ".git/config"},
            },
            trusted_mcp_servers=codex_engine.TRUSTED_HUB_MCP_SERVERS,
            shell_tool=False,
            effective_access_mode="write_files",
            kind="project",
            workspace_dir=proj_dir,
        )
        self.assertEqual(r_git_fc["result"]["decision"], "decline")

        r_legacy_exec = codex_engine.evaluate_codex_server_request(
            {"id": 14, "method": "execCommandApproval", "params": {"command": ["cmd.exe"]}},
            trusted_mcp_servers=codex_engine.TRUSTED_HUB_MCP_SERVERS,
            shell_tool=False,
            effective_access_mode="full_access",
            kind="project",
            workspace_dir=proj_dir,
        )
        self.assertIn("denied", r_legacy_exec["result"]["decision"])

        r_legacy_patch_ok = codex_engine.evaluate_codex_server_request(
            {
                "id": 15,
                "method": "applyPatchApproval",
                "params": {"callId": "p1", "conversationId": "t1", "fileChanges": {"src/ok.py": {}}},
            },
            trusted_mcp_servers=codex_engine.TRUSTED_HUB_MCP_SERVERS,
            shell_tool=False,
            effective_access_mode="write_files",
            kind="project",
            workspace_dir=proj_dir,
        )
        self.assertEqual(r_legacy_patch_ok["result"]["decision"], "approved")

        # Full E2E `stream_codex_turn` in Docker project for non-admin `docker_full` member
        member = await database.create_user(
            username="dev_docker",
            password="pw_dev_docker",
            display_name="Docker Dev",
            max_access_mode="docker_full",
        )
        await database.upsert_user_chatgpt_account(
            member["id"],
            raw_auth_json=json.dumps({"tokens": {"access_token": "tok_v94", "account_id": "acc_v94"}}),
            chatgpt_account_id="acc_v94",
            email="dev@example.com",
            label="Dev Plus",
            plan_type="plus",
        )

        mock_py = self.tmp_root / "mock_codex_v94.py"
        mock_py.write_text(MOCK_APPROVAL_CODEX_SCRIPT, encoding="utf-8")
        rpc_log = self.tmp_root / "rpc_v94.jsonl"
        os.environ["HUB_CODEX_BIN"] = str(mock_py)
        os.environ["MOCK_V94_RPC_LOG"] = str(rpc_log)

        events: list[dict[str, Any]] = []
        with patch(
            "app.codex_engine.check_docker_status",
            return_value={"sandbox_mcp_ready": True, "daemon_running": True, "image_ready": True},
        ):
            async for ev in codex_engine.stream_codex_turn(
                chat_id="chat_v94_docker",
                kind="project",
                project_name="DockerCodexProj",
                model="codex:gpt-5.3-codex@high",
                access_mode="full_access",
                prompt="Собери и протестируй проект",
                attachments=[],
                codex_thread_id="",
                codex_account_id="",
                history_messages=[],
                user=member,
                resolved_project_dir=proj_dir,
                execution_mode="docker",
            ):
                events.append(ev)

        done_ev = next((e for e in events if e.get("type") == "done"), None)
        self.assertIsNotNone(done_ev, f"Expected done event, got: {events}")
        assert done_ev is not None
        self.assertIn("Все проверки пройдены", done_ev["content"])

        tool_names = [tc["name"] for tc in done_ev["tool_calls"]]
        self.assertIn("mcp_hub_files_write_file", tool_names)
        self.assertIn("mcp_hub_sandbox_sandbox_exec", tool_names)
        wf_tc = next(tc for tc in done_ev["tool_calls"] if tc["name"] == "mcp_hub_files_write_file")
        self.assertEqual(wf_tc["parameters"]["Path"], "src/generated.py")

        logged = [json.loads(ln) for ln in rpc_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        startup_toml = next(x["toml"] for x in logged if x["event"] == "startup_config_toml")
        self.assertIn('approval_policy = "on-request"', startup_toml)
        self.assertIn("[mcp_servers.hub_files]", startup_toml)
        self.assertIn("[mcp_servers.hub_sandbox]", startup_toml)
        self.assertIn('default_tools_approval_mode = "approve"', startup_toml)

        ts_params = next(x["params"] for x in logged if x["event"] == "thread/start")
        self.assertEqual(ts_params["approvalPolicy"], "on-request")
        self.assertIn("hub_files", ts_params["developerInstructions"])
        self.assertIn("hub_sandbox", ts_params["developerInstructions"])

        turn_params = next(x["params"] for x in logged if x["event"] == "turn/start")
        self.assertEqual(turn_params["approvalPolicy"], "on-request")

        replies = {
            int(x["id"]): x["msg"]
            for x in logged
            if x["event"] == "client_approval_reply"
        }
        # 501: trusted hub_sandbox elicitation -> accept
        self.assertEqual(replies[501]["result"], {"action": "accept", "content": {}})
        # 502: untrusted evil_third_party_mcp elicitation -> decline
        self.assertEqual(replies[502]["result"], {"action": "decline"})
        # 503: requestUserInput -> hub_files allowed for session, rogue_mcp cancelled
        ans_503 = replies[503]["result"]["answers"]
        self.assertEqual(ans_503["mcp_tool_call_approval_files"]["answers"], ["Allow for this session"])
        self.assertEqual(ans_503["mcp_tool_call_approval_rogue"]["answers"], ["Cancel"])
        # 504: in-workspace fileChange -> accept
        self.assertEqual(replies[504]["result"], {"decision": "accept"})
        # 505: ../outside_secret.txt fileChange -> decline
        self.assertEqual(replies[505]["result"], {"decision": "decline"})
        # 506: host commandExecution when shell_tool=False -> decline
        self.assertEqual(replies[506]["result"], {"decision": "decline"})
        # 507: sandbox permissions escalation -> empty permissions
        self.assertEqual(replies[507]["result"], {"permissions": {}, "scope": "turn"})

    # ------------------------------------------------------------------
    # 3. `hub_files` (`/mcp/files/{mcp_token}`) Zero-Trust & 0-Subprocess Suite
    # ------------------------------------------------------------------
    async def test_03_hub_files_mcp_zero_trust_isolation_and_active_files(self) -> None:
        import app.agy_engine as agy_engine
        import app.config as config
        import app.database as database
        import app.main as main
        import app.workspace_mcp as workspace_mcp

        user_a = await database.create_user(
            username="alice_v94",
            password="pw_a",
            display_name="Alice",
            max_access_mode="write_files",
        )
        user_b = await database.create_user(
            username="bob_v94",
            password="pw_b",
            display_name="Bob",
            max_access_mode="write_files",
        )

        proj_dir = (self.tmp_root / "workspaces" / "alice_v94" / "DemoApp").resolve()
        (proj_dir / "src").mkdir(parents=True, exist_ok=True)
        (proj_dir / ".git").mkdir(parents=True, exist_ok=True)
        (proj_dir / ".git" / "config").write_text("[core]\nrepositoryformatversion = 0\n", encoding="utf-8")
        (proj_dir / "src" / "hello.py").write_bytes("def greet():\r\n    return 'Привет мир'\r\n".encode("utf-8"))
        # Place decoy secret files inside project to verify denylist even inside workspace
        (proj_dir / "auth.json").write_text('{"tokens":{"access_token":"secret"}}', encoding="utf-8")
        (proj_dir / "hub.db").write_text("sqlite_secret", encoding="utf-8")

        # Create external secret folder + NTFS junction / symlink inside proj_dir pointing outside
        outside_dir = (self.tmp_root / "outside_vault").resolve()
        outside_dir.mkdir(parents=True, exist_ok=True)
        (outside_dir / "vault.txt").write_text("TOP_SECRET_OUTSIDE", encoding="utf-8")
        junction_path = proj_dir / "linked_vault"
        if os.name == "nt":
            subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(junction_path), str(outside_dir)],
                check=True,
                capture_output=True,
                **config.subprocess_hidden_kwargs(),
            )
        else:
            os.symlink(outside_dir, junction_path, target_is_directory=True)
        self.junctions_to_clean.append(junction_path)

        chat_id = "chat_alice_files_94"
        other_chat_id = "chat_other_94"
        up_alice = config.UPLOADS_DIR / chat_id
        up_other = config.UPLOADS_DIR / other_chat_id
        up_alice.mkdir(parents=True, exist_ok=True)
        up_other.mkdir(parents=True, exist_ok=True)
        (up_alice / "spec.md").write_text("# ТЗ вложения\nСтрока 2\n", encoding="utf-8")
        (up_other / "foreign.md").write_text("Чужое вложение", encoding="utf-8")

        tok_alice = workspace_mcp.build_files_mcp_token(user_a["id"], 1, chat_id)
        tok_bob = workspace_mcp.build_files_mcp_token(user_b["id"], 1, chat_id)
        client = _LoopbackOrRemoteAsgiClient(main.app)

        # A) Non-loopback IP -> 403 Forbidden
        st_lan, _, _ = await client.request(
            "POST",
            f"/mcp/files/{tok_alice}",
            client_ip="192.168.1.77",
            json_body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        self.assertEqual(st_lan, 403)

        # B) Tampered HMAC signature -> 403 Forbidden
        st_bad, _, _ = await client.request(
            "POST",
            f"/mcp/files/{tok_alice[:-4]}ffff",
            json_body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        self.assertEqual(st_bad, 403)

        # C) Mismatched chat_id query param -> 403 Forbidden
        st_q, _, _ = await client.request(
            "POST",
            f"/mcp/files/{tok_alice}?chat_id=wrong_chat",
            json_body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        self.assertEqual(st_q, 403)

        # D) No active run in ACTIVE_RUNS -> tool call returns isError=True
        st_inactive, _, raw_inactive = await client.request(
            "POST",
            f"/mcp/files/{tok_alice}",
            json_body={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_dir", "arguments": {}},
            },
        )
        self.assertEqual(st_inactive, 200)
        self.assertTrue(json.loads(raw_inactive)["result"]["isError"])

        # Register active run in `read_only` mode first
        run = agy_engine.register_chat_turn(
            chat_id,
            user_id=user_a["id"],
            project_name="DemoApp",
            chat_title="Files Test",
            access_mode="read_only",
            model="codex:gpt-5.4@high",
            engine="codex",
        )
        run.kind = "project"
        run.project_dir = proj_dir
        run.workspace_dir = proj_dir

        # Bob's valid token for chat_id belonging to Alice -> rejected!
        _, _, raw_bob = await client.request(
            "POST",
            f"/mcp/files/{tok_bob}",
            json_body={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_dir", "arguments": {}},
            },
        )
        self.assertTrue(json.loads(raw_bob)["result"]["isError"])

        async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            st, _, body = await client.request(
                "POST",
                f"/mcp/files/{tok_alice}",
                json_body={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            self.assertEqual(st, 200)
            return json.loads(body)["result"]

        # Assert 0 OS child processes are spawned during all hub_files operations!
        with (
            patch("subprocess.Popen", side_effect=AssertionError("hub_files must spawn 0 subprocesses!")),
            patch("asyncio.create_subprocess_exec", side_effect=AssertionError("hub_files must spawn 0 subprocesses!")),
        ):
            # In read_only mode, tools/list returns ONLY the 4 read/search tools
            _, _, raw_list_ro = await client.request(
                "POST",
                f"/mcp/files/{tok_alice}",
                json_body={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
            )
            ro_names = [t["name"] for t in json.loads(raw_list_ro)["result"]["tools"]]
            self.assertEqual(ro_names, ["read_file", "list_dir", "find_by_name", "grep_search"])

            # Read project file + chat attachment
            r_read = await _call_tool("read_file", {"path": "src/hello.py", "start_line": 1, "end_line": 10})
            self.assertFalse(r_read["isError"], r_read)
            self.assertIn("Привет мир", r_read["content"][0]["text"])

            r_att = await _call_tool("read_file", {"path": str(up_alice / "spec.md")})
            self.assertFalse(r_att["isError"], r_att)
            self.assertIn("ТЗ вложения", r_att["content"][0]["text"])

            # Foreign chat attachment -> blocked!
            r_foreign_att = await _call_tool("read_file", {"path": str(up_other / "foreign.md")})
            self.assertTrue(r_foreign_att["isError"])

            # list_dir, find_by_name, grep_search (and ensure junction + secrets are hidden/skipped)
            r_ls = await _call_tool("list_dir", {"directory_path": "."})
            self.assertFalse(r_ls["isError"])
            ls_txt = r_ls["content"][0]["text"]
            self.assertIn("src/", ls_txt)
            self.assertNotIn("linked_vault", ls_txt)
            self.assertNotIn("auth.json", ls_txt)
            self.assertNotIn("hub.db", ls_txt)

            r_find = await _call_tool("find_by_name", {"pattern": "*.py"})
            self.assertFalse(r_find["isError"])
            self.assertIn("src/hello.py", r_find["content"][0]["text"])

            r_grep = await _call_tool("grep_search", {"query": "Привет\\s+мир", "is_regex": True})
            self.assertFalse(r_grep["isError"])
            self.assertIn("src/hello.py:2:", r_grep["content"][0]["text"])

            # Write attempt in read_only -> rejected!
            r_ro_write = await _call_tool("write_file", {"path": "src/blocked.py", "content": "nope"})
            self.assertTrue(r_ro_write["isError"])
            self.assertFalse((proj_dir / "src" / "blocked.py").exists())

            # Upgrade active turn to `write_files`
            run.access_mode = "write_files"
            _, _, raw_list_rw = await client.request(
                "POST",
                f"/mcp/files/{tok_alice}",
                json_body={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
            )
            rw_names = [t["name"] for t in json.loads(raw_list_rw)["result"]["tools"]]
            self.assertEqual(
                rw_names,
                ["read_file", "list_dir", "find_by_name", "grep_search", "write_file", "edit_file"],
            )

            # Valid write_file & edit_file (preserving CRLF on hello.py and recording active_files)
            r_wf = await _call_tool(
                "write_file",
                {"path": "/workspace/src/new_mod.py", "content": "VAL = 'Русский текст'\n"},
            )
            self.assertFalse(r_wf["isError"], r_wf)
            written_bytes = (proj_dir / "src" / "new_mod.py").read_bytes()
            self.assertFalse(written_bytes.startswith(b"\xef\xbb\xbf"))
            self.assertIn("Русский текст", written_bytes.decode("utf-8"))
            self.assertIn("src/new_mod.py", run.active_files)

            r_ef = await _call_tool(
                "edit_file",
                {
                    "path": "src/hello.py",
                    "target": "return 'Привет мир'",
                    "replacement": "return 'Обновлено v94'",
                },
            )
            self.assertFalse(r_ef["isError"], r_ef)
            edited_raw = (proj_dir / "src" / "hello.py").read_bytes()
            self.assertIn(b"\r\n", edited_raw)
            self.assertIn("Обновлено v94", edited_raw.decode("utf-8"))
            self.assertIn("src/hello.py", run.active_files)

            # Security blocks: `..` traversal, junction read/write, `.git` write, `auth.json`/`hub.db`/`CLI_HOMES`
            for bad_read in (
                "../outside_vault/vault.txt",
                str(outside_dir / "vault.txt"),
                "linked_vault/vault.txt",
                "auth.json",
                "hub.db",
                str(config.CLI_HOMES_DIR / "secret.json"),
            ):
                res_br = await _call_tool("read_file", {"path": bad_read})
                self.assertTrue(res_br["isError"], f"Expected read block for {bad_read}, got {res_br}")

            for bad_write in (
                "../escape.py",
                "linked_vault/escape.py",
                ".git/config",
                ".git/hooks/pre-commit",
                "auth.json",
                "hub.db",
            ):
                res_bw = await _call_tool("write_file", {"path": bad_write, "content": "pwn"})
                self.assertTrue(res_bw["isError"], f"Expected write block for {bad_write}, got {res_bw}")

        # Session revocation (password rotation bumps `session_version`) immediately invalidates HMAC token -> 403
        await database.update_user(user_a["id"], password="pw_a_rotated_v94")
        st_revoked, _, _ = await client.request(
            "POST",
            f"/mcp/files/{tok_alice}",
            json_body={"jsonrpc": "2.0", "id": 99, "method": "ping"},
        )
        self.assertEqual(st_revoked, 403)

    # ------------------------------------------------------------------
    # 4. `setup_sandbox_win.ps1` smoke test, UI pills, UTF-8 no-BOM & `?v=94`
    # ------------------------------------------------------------------
    async def test_04_setup_sandbox_ps1_smoke_frontend_pills_and_static_v94(self) -> None:
        import app.agy_engine as agy_engine
        import app.config as config

        # 1. Verify `tools/setup_sandbox_win.ps1` smoke-test line has no nested `""` bug
        ps1_path = ROOT_DIR / "tools" / "setup_sandbox_win.ps1"
        ps1_text = ps1_path.read_text(encoding="utf-8")
        m_smoke = re.search(r"^\s*\$smokeCmd\s*=\s*(.+)$", ps1_text, flags=re.MULTILINE)
        self.assertIsNotNone(m_smoke, "Could not find $smokeCmd assignment in setup_sandbox_win.ps1")
        smoke_rhs = m_smoke.group(1).strip()
        self.assertNotIn('""', smoke_rhs)

        # If Docker + `agy-hub-sandbox:latest` are live on this host, execute the exact PowerShell -> docker smoke command
        docker_st = await agy_engine.check_docker_status(force_refresh=True)
        docker_bin = agy_engine.resolve_docker_bin()
        if os.name == "nt" and docker_st.get("sandbox_mcp_ready") and docker_bin:
            ps_script = (
                f"$dockerExe = '{docker_bin}'\n"
                f"$ImageTag = '{agy_engine.DOCKER_SANDBOX_IMAGE}'\n"
                f"$smokeCmd = {smoke_rhs}\n"
                "& $dockerExe run --rm --network none --cap-drop ALL "
                "--security-opt no-new-privileges -e NVIDIA_VISIBLE_DEVICES=none "
                "$ImageTag bash -lc $smokeCmd\n"
                "exit $LASTEXITCODE\n"
            )
            smoke_proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=30,
                **config.subprocess_hidden_kwargs(),
            )
            self.assertEqual(
                smoke_proc.returncode,
                0,
                f"Live Docker smoke-test from setup_sandbox_win.ps1 failed:\n{smoke_proc.stdout}\n{smoke_proc.stderr}",
            )
            self.assertIn("GPU=none", smoke_proc.stdout)

        # 2. Verify `node --check` + `hub_files` Russian UI pill mapping & ExecutionFactSummary in `app.js`
        js_path = ROOT_DIR / "app" / "static" / "app.js"
        chk = subprocess.run(
            ["node", "--check", str(js_path)],
            capture_output=True,
            text=True,
            timeout=15,
            **config.subprocess_hidden_kwargs(),
        )
        self.assertEqual(chk.returncode, 0, chk.stderr)

        node_vm_check = textwrap.dedent(
            r"""
            const assert = require("assert");
            const fs = require("fs");
            const path = require("path");
            const vm = require("vm");

            const rootDir = process.env.HUB_ROOT_DIR;
            const appCode = fs.readFileSync(path.join(rootDir, "app", "static", "app.js"), "utf8");

            const dummyEl = () => ({
              classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
              addEventListener() {},
              removeEventListener() {},
              setAttribute() {},
              getAttribute: () => "",
              appendChild() {},
              querySelector: () => null,
              querySelectorAll: () => [],
              style: { setProperty() {}, removeProperty() {}, getPropertyValue: () => "" },
              dataset: {},
              innerHTML: "",
              textContent: "",
              value: "",
            });

            let exposed = null;
            const patched = appCode.replace(
              /\binit\(\);\s*\}\)\(\);\s*$/,
              "globalThis.__exposeV94 = { buildExecutionFactSummaryFromMsg, buildToolsHtml };\n})();\n"
            );

            const ctx = {
              __AGY_SKIP_AUTO_INIT__: true,
              window: {},
              addEventListener() {},
              removeEventListener() {},
              innerWidth: 1280,
              innerHeight: 800,
              document: {
                documentElement: dummyEl(),
                body: dummyEl(),
                getElementById: () => dummyEl(),
                querySelector: () => dummyEl(),
                querySelectorAll: () => [],
                createElement: () => dummyEl(),
                addEventListener() {},
              },
              localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
              sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
              navigator: { serviceWorker: { register: () => Promise.resolve() }, clipboard: {} },
              location: { protocol: "http:", host: "127.0.0.1:8765", pathname: "/", search: "" },
              matchMedia: () => ({ matches: false, addEventListener() {} }),
              fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }),
              WebSocket: function() { this.close = () => {}; },
              Notification: { permission: "default" },
              AudioContext: function() {},
              setTimeout: () => 1,
              clearTimeout() {},
              setInterval: () => 1,
              clearInterval() {},
              requestAnimationFrame: () => 1,
              URL: { createObjectURL: () => "", revokeObjectURL() {} },
              console,
            };
            ctx.window = ctx;
            ctx.globalThis = ctx;
            vm.createContext(ctx);
            vm.runInContext(patched, ctx);
            exposed = ctx.__exposeV94;
            assert(exposed && exposed.buildToolsHtml && exposed.buildExecutionFactSummaryFromMsg);

            const sampleTools = [
              { name: "mcp_hub_files_read_file", state: "DONE", parameters: { ServerName: "hub_files", ToolName: "read_file", Arguments: { path: "app/main.py" } } },
              { name: "mcp_hub_files_list_dir", state: "DONE", parameters: { ServerName: "hub_files", ToolName: "list_dir", Arguments: { directory_path: "app" } } },
              { name: "mcp_hub_files_find_by_name", state: "DONE", parameters: { ServerName: "hub_files", ToolName: "find_by_name", Arguments: { pattern: "*.py" } } },
              { name: "mcp_hub_files_grep_search", state: "DONE", parameters: { ServerName: "hub_files", ToolName: "grep_search", Arguments: { query: "stream_codex" } } },
              { name: "mcp_hub_files_write_file", state: "DONE", parameters: { ServerName: "hub_files", ToolName: "write_file", Arguments: { path: "src/new_file.py" } } },
              { name: "mcp_hub_files_edit_file", state: "DONE", parameters: { ServerName: "hub_files", ToolName: "edit_file", Arguments: { path: "src/edit_me.py" } } },
            ];
            const html = exposed.buildToolsHtml(sampleTools, true);
            for (const expectedLabel of ["Чтение", "Обзор папки", "Поиск", "Запись", "Правка", "main.py", "new_file.py", "edit_me.py"]) {
              assert(html.includes(expectedLabel), `Missing "${expectedLabel}" in rendered pills HTML: ${html}`);
            }

            const fact = exposed.buildExecutionFactSummaryFromMsg({ tool_calls: sampleTools }, {});
            assert(Array.isArray(fact.files), "Expected fact.files array");
            assert(fact.files.includes("src/new_file.py"), `Expected src/new_file.py in fact.files: ${JSON.stringify(fact.files)}`);
            assert(fact.files.includes("src/edit_me.py"), `Expected src/edit_me.py in fact.files: ${JSON.stringify(fact.files)}`);
            """
        )
        env_node = os.environ.copy()
        env_node["HUB_ROOT_DIR"] = str(ROOT_DIR)
        vm_res = subprocess.run(
            ["node", "-e", node_vm_check],
            env=env_node,
            capture_output=True,
            text=True,
            timeout=20,
            **config.subprocess_hidden_kwargs(),
        )
        self.assertEqual(vm_res.returncode, 0, f"Node VM check failed:\n{vm_res.stderr}")

        # 3. UTF-8 without BOM on all Python/JS/HTML/CSS files + PowerShell 5.1 AST parse on setup_sandbox_win.ps1
        no_bom_files = [
            ROOT_DIR / "app" / "workspace_mcp.py",
            ROOT_DIR / "app" / "codex_engine.py",
            ROOT_DIR / "app" / "sandbox_service.py",
            ROOT_DIR / "app" / "openrouter_service.py",
            ROOT_DIR / "app" / "agy_engine.py",
            ROOT_DIR / "app" / "main.py",
            ROOT_DIR / "app" / "static" / "app.js",
            ROOT_DIR / "app" / "static" / "index.html",
            ROOT_DIR / "app" / "static" / "sw.js",
            ROOT_DIR / "app" / "static" / "styles.css",
        ]
        for fp in no_bom_files:
            raw_b = fp.read_bytes()
            self.assertFalse(raw_b.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM forbidden in {fp}")
            raw_b.decode("utf-8")

        ps1_path.read_bytes().decode("utf-8-sig")
        if os.name == "nt":
            ast_chk = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        "$errs = $null; $toks = $null; "
                        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps1_path}', [ref]$toks, [ref]$errs); "
                        "if ($errs -and $errs.Count -gt 0) { Write-Error ($errs | Out-String); exit 1 }"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                **config.subprocess_hidden_kwargs(),
            )
            self.assertEqual(ast_chk.returncode, 0, f"PowerShell AST parse failed:\n{ast_chk.stderr}")

        idx_html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        sw_js = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        m_cache = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        self.assertIsNotNone(m_cache)
        ver_num = int(m_cache.group(1))
        self.assertGreaterEqual(ver_num, 94)
        self.assertIn(f"/static/styles.css?v={ver_num}", idx_html)
        self.assertIn(f"/static/app.js?v={ver_num}", idx_html)
        self.assertIn(f"/static/styles.css?v={ver_num}", sw_js)
        self.assertIn(f"/static/app.js?v={ver_num}", sw_js)


if __name__ == "__main__":
    unittest.main()
