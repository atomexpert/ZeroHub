"""
Isolated automated verification suite for Release v96:
1. Trusted Host per-invocation `--settings` (`enableTerminalSandbox`, `toolPermission`, `allowNonWorkspaceAccess`)
   & `hub_host` Dual-Key RBAC matrix without touching shared `settings.json`.
2. Zero-Trust `extra_dirs` validation, `GET`/`PUT /api/projects/{name}/settings`, and 3-engine propagation
   (`agy`, `codex`, `claude`, `workspace_mcp`).
3. Real `HostServiceManager` + `/mcp/host/{token}` E2E lifecycle on an ephemeral localhost port
   (`0% GPU` default, hidden window, anti-duplicate `name`/`command` guard, external port conflict check,
   `registry.json` `(pid, create_time_ft)` reload, `logs`, `restart`, `stop` with guaranteed cleanup).
4. Non-destructive `project_migrator` E2E (`discover_desktop_migration_plan`, `execute_desktop_migration`,
   `import_folder_into_project`): system path filtering, `shutil.copy2` source preservation, `.venv`/`node_modules`
   skip, `.env` & real `.git` copy, skeleton `README.md` replacement, conflict protection & merge-missing,
   `transcript.jsonl` auto-import without duplicates, and `migrated_from_paths` matching.
5. Frontend `v=96` hygiene: UTF-8 no-BOM, `node --check`, `main.workspace` CSS invariant, cache bump `v=96`.

Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or ports `8765`/`8766`.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _encode_varint(val: int) -> bytes:
    out = bytearray()
    while val > 0x7F:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def _pb_len_field(field_no: int, raw: bytes) -> bytes:
    tag = (field_no << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(raw)) + raw


def _pb_varint_field(field_no: int, val: int) -> bytes:
    tag = (field_no << 3) | 0
    return _encode_varint(tag) + _encode_varint(val)


def _build_fake_summary_b64(title: str, workspace_uris: list[str], step_count: int = 6) -> str:
    import base64

    blob = bytearray()
    blob.extend(_pb_len_field(1, title.encode("utf-8")))
    blob.extend(_pb_varint_field(2, step_count))
    for uri in workspace_uris:
        ws_msg = _pb_len_field(1, uri.encode("utf-8"))
        blob.extend(_pb_len_field(9, ws_msg))
    return base64.b64encode(bytes(blob)).decode("ascii")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _unlink_junctions(root: Path) -> None:
    if not root.exists():
        return
    for p in sorted(root.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            if hasattr(os.path, "isjunction") and os.path.isjunction(p):
                os.rmdir(p)
            elif p.is_symlink() and p.is_dir():
                os.rmdir(p)
        except Exception:
            pass


class _MiniAsgiResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> dict[str, Any]:
        return json.loads(self.text)


class _MiniAsgiClient:
    def __init__(self, app: Any, *, client: tuple[str, int] = ("127.0.0.1", 53100)) -> None:
        self._app = app
        self._client = client

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _MiniAsgiResponse:
        parsed_url = urllib.parse.urlsplit(path)
        raw_path = parsed_url.path or "/"
        qs = parsed_url.query
        payload = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
        hdr_list: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8765")]
        if json_body is not None:
            hdr_list.append((b"content-type", b"application/json"))
            hdr_list.append((b"content-length", str(len(payload)).encode("ascii")))
        for k, v in (headers or {}).items():
            hdr_list.append((k.lower().encode("latin-1"), str(v).encode("latin-1")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": urllib.parse.unquote(raw_path),
            "raw_path": raw_path.encode("ascii", errors="ignore"),
            "query_string": qs.encode("ascii"),
            "headers": hdr_list,
            "client": self._client,
            "server": ("127.0.0.1", 8765),
        }

        sent_body = False
        resp_status = 500
        resp_headers: dict[str, str] = {}
        resp_chunks: list[bytes] = []

        async def _receive() -> dict[str, Any]:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def _send(message: dict[str, Any]) -> None:
            nonlocal resp_status, resp_headers
            mtype = message.get("type")
            if mtype == "http.response.start":
                resp_status = int(message.get("status", 200))
                resp_headers = {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in (message.get("headers") or [])
                }
            elif mtype == "http.response.body":
                chunk = message.get("body", b"")
                if chunk:
                    resp_chunks.append(chunk)

        await self._app(scope, _receive, _send)
        return _MiniAsgiResponse(resp_status, resp_headers, b"".join(resp_chunks))


class TestReleaseV96TrustedHostServicesAndMigrator(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v96_test_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()
        self.data_dir = self.tmp_root / "data"
        self.projects_root = self.tmp_root / "projects"
        self.shared_projects_dir = self.data_dir / "shared_projects"
        self.workspaces_dir = self.data_dir / "workspaces"
        self.cli_homes_dir = self.data_dir / "cli_homes"
        self.uploads_dir = self.data_dir / "uploads"
        self.host_services_dir = self.data_dir / "host_services"
        self.agy_cli_dir = self.tmp_root / "agy_cli_shared"
        self.agy_app_dir = self.tmp_root / "agy_app_desktop"
        self.db_path = self.data_dir / "hub.db"

        for d in (
            self.data_dir,
            self.projects_root,
            self.shared_projects_dir,
            self.workspaces_dir,
            self.cli_homes_dir,
            self.uploads_dir,
            self.host_services_dir,
            self.agy_cli_dir,
            self.agy_app_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        # Seed a dummy shared settings.json to verify we NEVER mutate it
        self.shared_settings_file = self.agy_cli_dir / "settings.json"
        self.shared_settings_file.write_text('{"unchanged": true}\n', encoding="utf-8")
        self.shared_settings_before = self.shared_settings_file.read_bytes()

        import app.agy_engine as agy_engine
        import app.auth as auth
        import app.brain_reader as brain_reader
        import app.claude_engine as claude_engine
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database
        import app.host_service_manager as host_service_manager
        import app.main as main_mod
        import app.project_migrator as project_migrator
        import app.workspace_mcp as workspace_mcp

        self.agy_engine = agy_engine
        self.auth = auth
        self.brain_reader = brain_reader
        self.claude_engine = claude_engine
        self.codex_engine = codex_engine
        self.config = config
        self.database = database
        self.host_service_manager = host_service_manager
        self.main_mod = main_mod
        self.project_migrator = project_migrator
        self.workspace_mcp = workspace_mcp

        self._isolated_hsm = host_service_manager.HostServiceManager()

        self._patches = []
        for mod, mapping in (
            (
                config,
                {
                    "DATA_DIR": self.data_dir,
                    "DB_PATH": self.db_path,
                    "PROJECTS_ROOT": self.projects_root,
                    "SHARED_PROJECTS_DIR": self.shared_projects_dir,
                    "USER_WORKSPACES_DIR": self.workspaces_dir,
                    "CLI_HOMES_DIR": self.cli_homes_dir,
                    "UPLOADS_DIR": self.uploads_dir,
                    "AGY_CLI_DATA_DIR": self.agy_cli_dir,
                    "AGY_APP_DATA_DIR": self.agy_app_dir,
                },
            ),
            (database, {"DB_PATH": self.db_path}),
            (
                brain_reader,
                {
                    "PROJECTS_ROOT": self.projects_root,
                    "AGY_APP_DATA_DIR": self.agy_app_dir,
                    "SHARED_PROJECTS_DIR": self.shared_projects_dir,
                    "USER_WORKSPACES_DIR": self.workspaces_dir,
                },
            ),
            (
                agy_engine,
                {
                    "PROJECTS_ROOT": self.projects_root,
                    "CLI_HOMES_DIR": self.cli_homes_dir,
                    "AGY_CLI_DATA_DIR": self.agy_cli_dir,
                },
            ),
            (host_service_manager, {"HOST_SERVICE_MANAGER": self._isolated_hsm}),
            (main_mod, {"PROJECTS_ROOT": self.projects_root}),
        ):
            for attr, val in mapping.items():
                if hasattr(mod, attr):
                    self._patches.append(patch.object(mod, attr, val))
        for p in self._patches:
            p.start()

        await database.init_db()
        self.admin_user = await database.get_user_by_id("admin")
        assert self.admin_user is not None

        self.host_friend = await database.create_user(
            username="trusted_dev",
            display_name="Trusted Dev",
            password="pass1234",
            role="user",
            max_access_mode="host_full",
        )
        self.docker_friend = await database.create_user(
            username="docker_guest",
            display_name="Docker Guest",
            password="pass1234",
            role="user",
            max_access_mode="docker_full",
        )
        self.admin_token = auth.create_user_session_token(
            "admin", int(self.admin_user["session_version"])
        )
        self.host_friend_token = auth.create_user_session_token(
            self.host_friend["id"],
            int(self.host_friend["session_version"]),
        )
        self.docker_friend_token = auth.create_user_session_token(
            self.docker_friend["id"],
            int(self.docker_friend["session_version"]),
        )

    async def asyncTearDown(self) -> None:
        # Ensure zero background test processes survive teardown
        try:
            for svc in list(self._isolated_hsm._services.values()):
                pid = int(svc.get("pid") or 0)
                cft = int(svc.get("create_time_ft") or 0)
                if pid > 0:
                    self.host_service_manager._kill_verified_process_tree(pid, cft)
        except Exception:
            pass
        for p in reversed(self._patches):
            p.stop()
        _unlink_junctions(self.tmp_root)
        self._tmp.cleanup()

    def _parse_settings_json(self, args: list[str]) -> dict[str, Any]:
        self.assertIn("--settings", args)
        idx = args.index("--settings")
        return json.loads(args[idx + 1])

    async def test_01_trusted_host_sandbox_matrix_and_shared_settings_untouched(self) -> None:
        ext_dir = self.tmp_root / "ext_w"
        ext_dir.mkdir(parents=True, exist_ok=True)

        # 1. Admin + project + host + full_access -> Trusted Host unlocked
        s_admin_host = self._parse_settings_json(
            self.agy_engine._build_permission_args(
                "project",
                "full_access",
                is_admin=True,
                allow_hub_host=True,
                force_no_host_shell=False,
                extra_dirs=[ext_dir],
            )
        )
        self.assertIs(s_admin_host["enableTerminalSandbox"], False)
        self.assertEqual(s_admin_host["toolPermission"], "always-proceed")
        self.assertIs(s_admin_host["allowNonWorkspaceAccess"], True)
        for pat in self.agy_engine.HUB_HOST_ALLOW_PATTERNS:
            self.assertIn(pat, s_admin_host["permissions"]["allow"])
        for pat in self.agy_engine.SECRET_PROFILE_DENY_PATTERNS:
            self.assertIn(pat, s_admin_host["permissions"]["deny"])
        for pat in self.agy_engine.HEADLESS_ASYNC_DENY_PATTERNS:
            self.assertIn(pat, s_admin_host["permissions"]["deny"])
        ext_posix = ext_dir.resolve().as_posix()
        self.assertTrue(
            any(ext_posix in rule for rule in s_admin_host["permissions"]["allow"]),
            f"Expected extra_dir {ext_posix} in allow rules",
        )

        # 2. Admin in regular chat (`kind == "chat"`) -> Sandbox stays ON
        s_chat = self._parse_settings_json(
            self.agy_engine._build_permission_args(
                "chat",
                "full_access",
                is_admin=True,
                allow_hub_host=False,
                force_no_host_shell=True,
            )
        )
        self.assertIs(s_chat["enableTerminalSandbox"], True)
        self.assertIs(s_chat["allowNonWorkspaceAccess"], False)
        for pat in self.agy_engine.HUB_HOST_DENY_PATTERNS:
            self.assertIn(pat, s_chat["permissions"]["deny"])

        # 3. Admin in `write_files` / `read_only` -> Sandbox stays ON
        for mode in ("read_only", "write_files"):
            s_mode = self._parse_settings_json(
                self.agy_engine._build_permission_args(
                    "project",
                    mode,
                    is_admin=True,
                    allow_hub_host=False,
                    force_no_host_shell=False,
                )
            )
            self.assertIs(s_mode["enableTerminalSandbox"], True)
            self.assertIs(s_mode["allowNonWorkspaceAccess"], False)
            for pat in self.agy_engine.HUB_HOST_DENY_PATTERNS:
                self.assertIn(pat, s_mode["permissions"]["deny"])

        # 4. `host_full` user in `docker` project -> Sandbox ON, RunCommand & hub_host DENIED
        s_docker = self._parse_settings_json(
            self.agy_engine._build_permission_args(
                "project",
                "full_access",
                is_admin=False,
                allow_hub_sandbox=True,
                allow_hub_host=False,
                force_no_host_shell=True,
            )
        )
        self.assertIs(s_docker["enableTerminalSandbox"], True)
        self.assertIs(s_docker["allowNonWorkspaceAccess"], False)
        self.assertIn("run_command", s_docker["permissions"]["deny"])
        for pat in self.agy_engine.HUB_HOST_DENY_PATTERNS:
            self.assertIn(pat, s_docker["permissions"]["deny"])

        # 5. `docker_full` guest in `host` project -> `force_no_host_shell=True` -> Sandbox ON
        s_guest = self._parse_settings_json(
            self.agy_engine._build_permission_args(
                "project",
                "full_access",
                is_admin=False,
                allow_hub_host=False,
                force_no_host_shell=True,
            )
        )
        self.assertIs(s_guest["enableTerminalSandbox"], True)
        self.assertIs(s_guest["allowNonWorkspaceAccess"], False)
        self.assertIn("run_command", s_guest["permissions"]["deny"])

        # Shared settings.json on disk must remain byte-for-byte untouched
        self.assertEqual(self.shared_settings_file.read_bytes(), self.shared_settings_before)

    async def test_02_extra_dirs_zero_trust_and_multi_engine_propagation(self) -> None:
        proj_dir = self.projects_root / "AlphaProj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "main.py").write_text("print('alpha')\n", encoding="utf-8")

        ext_dataset = self.tmp_root / "external_datasets" / "quotes_2026"
        ext_dataset.mkdir(parents=True, exist_ok=True)
        (ext_dataset / "prices.csv").write_text("ts,price\n1,100\n", encoding="utf-8")

        # Valid external dir allowed for admin in host mode
        valid = self.brain_reader.validate_project_extra_dirs(
            [str(ext_dataset)],
            user=self.admin_user,
            exec_mode="host",
            project_dir=proj_dir,
            strict=True,
        )
        self.assertEqual(valid, [ext_dataset.resolve()])

        # Blocked paths: drive root, Windows dir, Hub DATA_DIR, CLI_HOMES_DIR
        for forbidden in (
            "C:\\",
            os.environ.get("SystemRoot", "C:\\Windows"),
            str(self.data_dir),
            str(self.cli_homes_dir),
            "\\\\server\\share\\folder",
        ):
            with self.assertRaises(ValueError):
                self.brain_reader.validate_project_extra_dirs(
                    [forbidden],
                    user=self.admin_user,
                    exec_mode="host",
                    project_dir=proj_dir,
                    strict=True,
                )

        # Blocked for non-admin and for docker contour
        with self.assertRaises(ValueError):
            self.brain_reader.validate_project_extra_dirs(
                [str(ext_dataset)],
                user=self.host_friend,
                exec_mode="host",
                project_dir=proj_dir,
                strict=True,
            )
        with self.assertRaises(ValueError):
            self.brain_reader.validate_project_extra_dirs(
                [str(ext_dataset)],
                user=self.admin_user,
                exec_mode="docker",
                project_dir=proj_dir,
                strict=True,
            )

        # REST API GET/PUT /api/projects/{name}/settings
        client = _MiniAsgiClient(self.main_mod.app)
        r_forbid = await client.request(
            "PUT",
            "/api/projects/AlphaProj/settings",
            json_body={"extra_dirs": [str(ext_dataset)]},
            headers={"Authorization": f"Bearer {self.host_friend_token}"},
        )
        self.assertEqual(r_forbid.status_code, 403)

        r_put = await client.request(
            "PUT",
            "/api/projects/AlphaProj/settings",
            json_body={"extra_dirs": [str(ext_dataset)]},
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        self.assertEqual(r_put.status_code, 200, r_put.text)
        self.assertEqual(r_put.json()["extra_dirs"], [str(ext_dataset.resolve())])

        r_get = await client.request(
            "GET",
            "/api/projects/AlphaProj/settings",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        self.assertEqual(r_get.status_code, 200)
        self.assertEqual(r_get.json()["extra_dirs"], [str(ext_dataset.resolve())])

        # Propagation to `workspace_mcp` (`hub_files`) & `codex_engine` & `claude_engine`
        run = self.agy_engine.register_chat_turn(
            "chat_ext_v96",
            user_id="admin",
            project_name="AlphaProj",
            chat_title="Ext",
            access_mode="write_files",
            model="codex:gpt-5.4@high",
            engine="codex",
        )
        try:
            run.kind = "project"
            run.project_dir = proj_dir
            run.workspace_dir = proj_dir
            run.extra_dirs = [ext_dataset]
            run_ctx = self.workspace_mcp.ResolvedFilesRun(
                user=self.admin_user,
                chat_id="chat_ext_v96",
                run_state=run,
                workspace_dir=proj_dir,
                project_dir=proj_dir,
                uploads_dir=None,
                can_write=True,
            )
            resolved_csv = self.workspace_mcp.resolve_safe_workspace_path(
                str(ext_dataset / "prices.csv"), run_ctx
            )
            self.assertEqual(resolved_csv, (ext_dataset / "prices.csv").resolve())
        finally:
            self.agy_engine.ACTIVE_RUNS.pop("chat_ext_v96", None)

        self.assertTrue(
            self.workspace_mcp.is_safe_workspace_write_target(
                str(ext_dataset / "out.json"), proj_dir, extra_dirs=[ext_dataset]
            )
        )
        # `.git/config` inside extra_dir must STILL be blocked from writes!
        (ext_dataset / ".git").mkdir(exist_ok=True)
        self.assertFalse(
            self.workspace_mcp.is_safe_workspace_write_target(
                str(ext_dataset / ".git" / "config"), proj_dir, extra_dirs=[ext_dataset]
            )
        )

        codex_sec_wf = self.codex_engine.resolve_codex_security_matrix(
            kind="project",
            access_mode="write_files",
            is_admin=True,
            force_no_host_shell=False,
            workspace_dir=proj_dir,
            allow_hub_openrouter=False,
            extra_dirs=[ext_dataset],
        )
        self.assertIn(
            str(ext_dataset.resolve()),
            codex_sec_wf["turn_sandbox_policy"]["writableRoots"],
        )
        self.assertFalse(codex_sec_wf["allow_hub_host"])

        codex_sec_full = self.codex_engine.resolve_codex_security_matrix(
            kind="project",
            access_mode="full_access",
            is_admin=True,
            force_no_host_shell=False,
            allow_hub_host=True,
            workspace_dir=proj_dir,
            extra_dirs=[ext_dataset],
        )
        self.assertTrue(codex_sec_full["allow_hub_host"])
        self.assertIn(str(ext_dataset.resolve()), codex_sec_full["extra_readable_roots"])

        claude_sec = self.claude_engine.resolve_claude_security_matrix(
            kind="project",
            access_mode="write_files",
            is_admin=True,
            force_no_host_shell=False,
        )
        self.assertIn("mcp__hub_host__*", claude_sec["disallowed_tools"])

    async def test_03_host_service_manager_and_mcp_e2e_lifecycle(self) -> None:
        svc_proj = self.projects_root / "LiveSvcProj"
        svc_proj.mkdir(parents=True, exist_ok=True)
        helper_script = svc_proj / "mini_http_svc.py"
        helper_script.write_text(
            "import http.server, json, os, sys\n"
            "port = int(sys.argv[1])\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        body = json.dumps({\n"
            "            'pid': os.getpid(),\n"
            "            'cuda': os.environ.get('CUDA_VISIBLE_DEVICES'),\n"
            "            'ort': os.environ.get('ORT_DISABLE_ALL_GPU'),\n"
            "        }).encode('utf-8')\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Length', str(len(body)))\n"
            "        self.end_headers()\n"
            "        self.wfile.write(body)\n"
            "    def log_message(self, fmt, *args):\n"
            "        print('REQ_LOG', fmt % args, flush=True)\n"
            "print(f'BOOTING_ON_{port}', flush=True)\n"
            "http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()\n",
            encoding="utf-8",
        )

        # CLI sync creates host-services/SKILL.md and registers hub_host in mcp_config.json
        self.host_service_manager.sync_user_host_cli_files(self.admin_user)
        admin_cli_home = self.config.ensure_user_cli_home("admin")
        skill_file = admin_cli_home / ".gemini" / "config" / "skills" / "host-services" / "SKILL.md"
        mcp_cfg_file = admin_cli_home / ".gemini" / "config" / "mcp_config.json"
        self.assertTrue(skill_file.is_file())
        mcp_cfg = json.loads(mcp_cfg_file.read_text(encoding="utf-8"))
        self.assertIn("hub_host", mcp_cfg.get("mcpServers", {}))

        mcp_token = self.host_service_manager.build_host_mcp_token(
            "admin", int(self.admin_user["session_version"])
        )
        docker_mcp_token = self.host_service_manager.build_host_mcp_token(
            self.docker_friend["id"], int(self.docker_friend["session_version"])
        )
        client = _MiniAsgiClient(self.main_mod.app)
        ext_client = _MiniAsgiClient(self.main_mod.app, client=("192.168.1.77", 54000))

        # Non-loopback IP and `docker_full` user must be strictly 403
        r_non_local = await ext_client.request(
            "POST",
            f"/mcp/host/{mcp_token}",
            json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertEqual(r_non_local.status_code, 403)

        r_docker_forbid = await client.request(
            "POST",
            f"/mcp/host/{docker_mcp_token}",
            json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertEqual(r_docker_forbid.status_code, 403)

        free_port = _find_free_port()
        cmd_str = f'"{sys.executable}" -u "{helper_script}" {free_port}'

        async def _mcp_ctl(arguments: dict[str, Any]) -> dict[str, Any]:
            import re as _re

            resp = await client.request(
                "POST",
                f"/mcp/host/{mcp_token}",
                json_body={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {"name": "host_service_ctl", "arguments": arguments},
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            rpc = resp.json()["result"]
            txt = str(rpc["content"][0]["text"])
            m_pid = _re.search(r"\bpid=(\d+)", txt)
            m_st = _re.search(r"\bstatus=([^\s·\]]+)", txt)
            status_val = m_st.group(1) if m_st else ""
            is_err = bool(rpc.get("isError"))
            return {
                "ok": not is_err,
                "isError": is_err,
                "pid": int(m_pid.group(1)) if m_pid else 0,
                "status": status_val,
                "alive": status_val == "running",
                "already_running": ("уже запущен" in txt.lower() or "already" in txt.lower()),
                "text": txt,
                "log_tail": txt,
                "error": txt if is_err else "",
            }

        # 0. Calling `hub_host` WITHOUT an active Trusted Host turn must fail closed (`isError=True`)
        no_turn_res = await _mcp_ctl({"action": "status", "name": "test-web"})
        self.assertFalse(no_turn_res["ok"])
        self.assertIn("активного хода", no_turn_res["error"])

        svc_run = self.agy_engine.register_chat_turn(
            "chat_svc_v96",
            user_id="admin",
            project_name="LiveSvcProj",
            chat_title="Host Svc Turn",
            access_mode="full_access",
            model="gemini-3.8-flash-high",
            engine="agy",
        )
        svc_run.kind = "project"
        svc_run.host_allowed = True
        svc_run.project_dir = svc_proj
        svc_run.workspace_dir = svc_proj

        try:
            # 1. Start detached service via MCP `hub_host`
            start_res = await _mcp_ctl(
                {
                    "action": "start",
                    "project": "LiveSvcProj",
                    "name": "test-web",
                    "command": cmd_str,
                    "port": free_port,
                }
            )
            self.assertTrue(start_res["ok"], start_res)
            first_pid = int(start_res["pid"])
            self.assertGreater(first_pid, 0)
            self.assertTrue(start_res["alive"])

            # Verify REST GET /api/projects/{project_name}/host-services reports running service
            r_rest_list = await client.request(
                "GET",
                "/api/projects/LiveSvcProj/host-services",
                headers={"Authorization": f"Bearer {self.admin_token}"},
            )
            self.assertEqual(r_rest_list.status_code, 200, r_rest_list.text)
            self.assertEqual(r_rest_list.json()["summary"]["running_count"], 1)

            # Verify HTTP 200 + 0% GPU env on the live detached service
            def _fetch_live() -> dict[str, Any]:
                with urllib.request.urlopen(f"http://127.0.0.1:{free_port}/check", timeout=3.0) as r:
                    return json.loads(r.read().decode("utf-8"))

            live_payload = await asyncio.to_thread(_fetch_live)
            self.assertEqual(live_payload["cuda"], "")
            self.assertEqual(live_payload["ort"], "1")

            # 2. Anti-duplicate by same `name` -> returns existing running PID without spawning a second copy
            dup_same = await _mcp_ctl(
                {
                    "action": "start",
                    "project": "LiveSvcProj",
                    "name": "test-web",
                    "command": cmd_str,
                    "port": free_port,
                }
            )
            self.assertTrue(dup_same["ok"])
            self.assertEqual(int(dup_same["pid"]), first_pid)

            # 3. Anti-duplicate by normalized `command` under a different `name` (protects Telegram bots from 409 Conflict!)
            dup_cmd = await _mcp_ctl(
                {
                    "action": "start",
                    "project": "LiveSvcProj",
                    "name": "second-bot-copy",
                    "command": f"  {cmd_str}  ",
                }
            )
            self.assertFalse(dup_cmd["ok"])
            self.assertIn("test-web", dup_cmd.get("error", ""))

            # 4. External occupied port conflict check
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy_sock:
                busy_sock.bind(("127.0.0.1", 0))
                busy_sock.listen(1)
                busy_port = int(busy_sock.getsockname()[1])
                port_conflict = await _mcp_ctl(
                    {
                        "action": "start",
                        "project": "LiveSvcProj",
                        "name": "port-clash",
                        "command": f'"{sys.executable}" -c "import time; time.sleep(10)"',
                        "port": busy_port,
                    }
                )
                self.assertFalse(port_conflict["ok"])
                self.assertIn(str(busy_port), port_conflict.get("error", ""))

            # 5. Persistence across Hub restart: brand-new HostServiceManager reloads `registry.json` with `(pid, create_time_ft)`
            reloaded_mgr = self.host_service_manager.HostServiceManager()
            reloaded_status = reloaded_mgr.list_project_services("LiveSvcProj")
            self.assertEqual(len(reloaded_status), 1)
            self.assertTrue(reloaded_status[0]["alive"])
            self.assertEqual(int(reloaded_status[0]["pid"]), first_pid)

            # 6. Read logs via MCP
            logs_res = await _mcp_ctl(
                {"action": "logs", "project": "LiveSvcProj", "name": "test-web", "tail_lines": 50}
            )
            self.assertTrue(logs_res["ok"])
            self.assertIn(f"BOOTING_ON_{free_port}", logs_res["log_tail"])

            # 7. Restart spawns a fresh PID and keeps serving HTTP 200
            restart_res = await _mcp_ctl(
                {"action": "restart", "project": "LiveSvcProj", "name": "test-web"}
            )
            self.assertTrue(restart_res["ok"], restart_res)
            second_pid = int(restart_res["pid"])
            self.assertNotEqual(second_pid, first_pid)
            live_after_restart = await asyncio.to_thread(_fetch_live)
            self.assertEqual(live_after_restart["cuda"], "")

            # 8. Stop kills process tree cleanly and frees the TCP port
            stop_res = await _mcp_ctl(
                {"action": "stop", "project": "LiveSvcProj", "name": "test-web"}
            )
            self.assertTrue(stop_res["ok"], stop_res)
            self.assertFalse(stop_res.get("alive", False))
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as verify_free:
                verify_free.bind(("127.0.0.1", free_port))
        finally:
            self.agy_engine.ACTIVE_RUNS.pop("chat_svc_v96", None)

    async def test_04_project_migrator_non_destructive_and_chat_relink_e2e(self) -> None:
        # 1. External project folder with `.env`, real `.git`, `app.py`, AND heavy `.venv`/`node_modules`
        ext_src = self.tmp_root / "old_disk" / "MyTradingBot"
        ext_src.mkdir(parents=True, exist_ok=True)
        (ext_src / "bot.py").write_text("print('trading_v2')\n", encoding="utf-8")
        (ext_src / ".env").write_text("TG_TOKEN=secret_123\n", encoding="utf-8")
        (ext_src / "README.md").write_text("# Real Bot Docs\nFull documentation.\n", encoding="utf-8")
        (ext_src / ".venv" / "Lib").mkdir(parents=True)
        (ext_src / ".venv" / "Lib" / "heavy.py").write_text("x = 1\n", encoding="utf-8")
        (ext_src / "node_modules" / "pkg").mkdir(parents=True)
        (ext_src / "node_modules" / "pkg" / "index.js").write_text("module.exports={};\n", encoding="utf-8")
        (ext_src / "__pycache__").mkdir()
        (ext_src / "__pycache__" / "bot.cpython-312.pyc").write_bytes(b"\x00\x01")
        # Real `.git` with commit ref
        (ext_src / ".git" / "refs" / "heads").mkdir(parents=True)
        (ext_src / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (ext_src / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="utf-8")

        # Destination in `PROJECTS_ROOT` currently has ONLY an empty Hub skeleton!
        skel_dst = self.projects_root / "MyTradingBot"
        skel_dst.mkdir(parents=True, exist_ok=True)
        (skel_dst / "README.md").write_text("# MyTradingBot\n", encoding="utf-8")
        self.project_migrator.ensure_isolated_project_git_boundary(skel_dst)
        self.assertTrue(self.brain_reader.inspect_project_dir_health(skel_dst)["is_empty_skeleton"])

        # 2. Conflict project where BOTH external and Hub folders have real files
        conflict_src = self.tmp_root / "old_disk" / "ConflictApp"
        conflict_src.mkdir(parents=True, exist_ok=True)
        (conflict_src / "shared.py").write_text("OLD_VERSION = 1\n", encoding="utf-8")
        (conflict_src / "only_in_old.py").write_text("EXTRA = True\n", encoding="utf-8")

        conflict_dst = self.projects_root / "ConflictApp"
        conflict_dst.mkdir(parents=True, exist_ok=True)
        (conflict_dst / "shared.py").write_text("HUB_NEW_VERSION = 2\n", encoding="utf-8")

        # 3. Build fake Desktop `conversation_summaries.db` + `transcript.jsonl`
        sum_db = self.agy_app_dir / "conversation_summaries.db"
        conn = sqlite3.connect(str(sum_db))
        conn.execute(
            """
            CREATE TABLE conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                preview TEXT,
                step_count INTEGER,
                last_modified_time TEXT,
                workspace_uris TEXT,
                status TEXT,
                project_id TEXT,
                nesting_depth INTEGER DEFAULT 0,
                killed INTEGER DEFAULT 0,
                last_user_input_time TEXT
            )
            """
        )
        conv_bot_id = "11111111-2222-3333-4444-555555555555"
        conv_sys_id = "99999999-8888-7777-6666-555555555555"
        conn.execute(
            """
            INSERT INTO conversation_summaries
            (conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, project_id, nesting_depth, killed, last_user_input_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                conv_bot_id,
                "Разработка MyTradingBot",
                "Напиши торгового бота",
                6,
                "2026-11-10T10:01:00Z",
                json.dumps([ext_src.as_uri()]),
                "DONE",
                "",
                "2026-11-10T10:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO conversation_summaries
            (conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, project_id, nesting_depth, killed, last_user_input_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                "22222222-3333-4444-5555-666666666666",
                "ConflictApp Архитектура",
                "Обсуждение архитектуры",
                4,
                "2026-11-09T12:00:00Z",
                json.dumps([conflict_src.as_uri()]),
                "DONE",
                "",
                "2026-11-09T11:59:00Z",
            ),
        )
        # System / root folders that MUST be filtered out
        conn.execute(
            """
            INSERT INTO conversation_summaries
            (conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, project_id, nesting_depth, killed, last_user_input_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                conv_sys_id,
                "Системный чат",
                "Системная проверка",
                3,
                "2026-11-08T09:00:00Z",
                json.dumps([Path("C:/Windows").as_uri(), Path.home().as_uri()]),
                "DONE",
                "",
                "2026-11-08T08:59:00Z",
            ),
        )
        conn.commit()
        conn.close()

        # Create real `transcript.jsonl` for `conv_bot_id`
        log_dir = self.agy_app_dir / "brain" / conv_bot_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "transcript.jsonl").write_text(
            json.dumps(
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "content": "Напиши торгового бота",
                    "created_at": "2026-11-10T10:00:00Z",
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "Готово, бот создан в bot.py!",
                    "created_at": "2026-11-10T10:01:00Z",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        # Discover plan
        plan = self.project_migrator.discover_desktop_migration_plan(
            allow_temp_subdirs=True
        )
        by_name = {p["project_name"]: p for p in plan["projects"]}
        self.assertIn("MyTradingBot", by_name)
        self.assertEqual(by_name["MyTradingBot"]["action"], "copy_to_hub")
        self.assertIn("ConflictApp", by_name)
        self.assertEqual(by_name["ConflictApp"]["action"], "conflict_both_non_empty")
        self.assertGreaterEqual(plan["filtered_system_paths_count"], 1)

        # Execute migration WITHOUT conflict merge first
        exec_res = await self.project_migrator.execute_desktop_migration(
            import_chats=True,
            merge_missing_on_conflict=False,
            rebuild_venv=False,
            allow_temp_subdirs=True,
        )
        self.assertTrue(exec_res["ok"])
        self.assertEqual(exec_res["migrated_projects_count"], 1)
        self.assertEqual(exec_res["total_imported_chats"], 1)

        # Source folder MUST be 100% untouched!
        self.assertTrue((ext_src / "bot.py").is_file())
        self.assertTrue((ext_src / ".venv" / "Lib" / "heavy.py").is_file())

        # Destination folder received `.env`, `bot.py`, real `README.md`, and real `.git`, while skipping `.venv`/`node_modules`/`__pycache__`
        self.assertEqual((skel_dst / "bot.py").read_text(encoding="utf-8"), "print('trading_v2')\n")
        self.assertEqual((skel_dst / ".env").read_text(encoding="utf-8"), "TG_TOKEN=secret_123\n")
        self.assertIn("Real Bot Docs", (skel_dst / "README.md").read_text(encoding="utf-8"))
        self.assertTrue((skel_dst / ".git" / "refs" / "heads" / "main").is_file())
        self.assertFalse((skel_dst / ".venv").exists())
        self.assertFalse((skel_dst / "node_modules").exists())
        self.assertFalse((skel_dst / "__pycache__").exists())

        # ConflictApp in Hub was NOT overwritten!
        self.assertEqual(
            (conflict_dst / "shared.py").read_text(encoding="utf-8"), "HUB_NEW_VERSION = 2\n"
        )
        self.assertFalse((conflict_dst / "only_in_old.py").exists())

        # Re-running migration with `merge_missing_on_conflict=True` copies ONLY `only_in_old.py`
        # without overwriting `shared.py` AND creates ZERO duplicate chats!
        exec_res_2 = await self.project_migrator.execute_desktop_migration(
            selected_projects=["MyTradingBot", "ConflictApp"],
            import_chats=True,
            merge_missing_on_conflict=True,
            allow_temp_subdirs=True,
        )
        self.assertEqual(exec_res_2["total_imported_chats"], 0)
        self.assertEqual(
            (conflict_dst / "shared.py").read_text(encoding="utf-8"), "HUB_NEW_VERSION = 2\n"
        )
        self.assertTrue((conflict_dst / "only_in_old.py").is_file())

        # `list_desktop_conversations(project_name="MyTradingBot")` matches via `migrated_from_paths`
        matched_convs = self.brain_reader.list_desktop_conversations(project_name="MyTradingBot")
        self.assertEqual(len(matched_convs), 1)
        self.assertEqual(matched_convs[0]["conversation_id"], conv_bot_id)

        # Targeted `POST /api/projects/{name}/import-folder` for an unindexed external folder
        unindexed_src = self.tmp_root / "manual_folder" / "MicrotikScripts"
        unindexed_src.mkdir(parents=True, exist_ok=True)
        (unindexed_src / "router.rsc").write_text("/ip address print\n", encoding="utf-8")
        empty_hub_proj = self.projects_root / "microtik"
        empty_hub_proj.mkdir(parents=True, exist_ok=True)
        (empty_hub_proj / "README.md").write_text("# microtik\n", encoding="utf-8")

        imp_res = await self.project_migrator.import_folder_into_project(
            project_name="microtik",
            dest_path=empty_hub_proj,
            source_path=unindexed_src,
            allow_temp_subdirs=True,
        )
        self.assertTrue(imp_res["ok"])
        self.assertTrue((empty_hub_proj / "router.rsc").is_file())
        self.assertTrue((unindexed_src / "router.rsc").is_file())

    async def test_05_frontend_v96_hygiene_and_css_invariant(self) -> None:
        checked_files = [
            "app/database.py",
            "app/brain_reader.py",
            "app/host_service_manager.py",
            "app/project_migrator.py",
            "app/agy_engine.py",
            "app/codex_engine.py",
            "app/claude_engine.py",
            "app/workspace_mcp.py",
            "app/main.py",
            "tools/migrate_projects.py",
            "app/static/sw.js",
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/app.js",
        ]
        for rel in checked_files:
            p = ROOT_DIR / rel
            raw = p.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM found in {rel}")
            raw.decode("utf-8")

        sw_txt = (ROOT_DIR / "app/static/sw.js").read_text(encoding="utf-8")
        html_txt = (ROOT_DIR / "app/static/index.html").read_text(encoding="utf-8")
        css_txt = (ROOT_DIR / "app/static/styles.css").read_text(encoding="utf-8")

        import re

        m_sw_cache = re.search(r"agy-hub-v(\d+)", sw_txt)
        self.assertIsNotNone(m_sw_cache)
        self.assertGreaterEqual(int(m_sw_cache.group(1)), 96)
        m_sw_css = re.search(r"/static/styles\.css\?v=(\d+)", sw_txt)
        m_sw_js = re.search(r"/static/app\.js\?v=(\d+)", sw_txt)
        m_html_css = re.search(r"/static/styles\.css\?v=(\d+)", html_txt)
        m_html_js = re.search(r"/static/app\.js\?v=(\d+)", html_txt)
        for m in (m_sw_css, m_sw_js, m_html_css, m_html_js):
            self.assertIsNotNone(m)
            self.assertGreaterEqual(int(m.group(1)), 96)
        self.assertIn('id="hostServicesModal"', html_txt)
        self.assertIn('id="desktopMigrationModal"', html_txt)
        self.assertIn('id="emptySkeletonBanner"', html_txt)

        # Verify `main.workspace` CSS invariant (no stacking-context / containing-block properties)

        forbidden_props = (
            "transform",
            "filter",
            "backdrop-filter",
            "perspective",
            "contain",
            "container-type",
        )
        for m in re.finditer(r"main\.workspace\s*\{([^}]*)\}", css_txt, re.S):
            body = m.group(1)
            for prop in forbidden_props:
                self.assertIsNone(
                    re.search(rf"(?<![\w-]){re.escape(prop)}\s*:", body),
                    f"Forbidden CSS property '{prop}' found inside main.workspace!",
                )

        # Verify JS syntax via `node --check`
        for js_rel in ("app/static/app.js", "app/static/sw.js"):
            proc = subprocess.run(
                ["node", "--check", str(ROOT_DIR / js_rel)],
                cwd=str(ROOT_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
                **self.config.subprocess_hidden_kwargs(),
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"node --check failed for {js_rel}: {(proc.stdout or b'').decode('utf-8', 'replace')}",
            )


if __name__ == "__main__":
    unittest.main()
