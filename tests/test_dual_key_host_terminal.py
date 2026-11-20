"""
Isolated automated test suite for Variant A ("Dual-Key" Host Terminal Access):
- Direct Windows Host terminal opens for a non-admin user ONLY when BOTH switches are enabled:
  1) `user.max_access_mode == "host_full"`
  2) `project.execution_mode == "host"` (or user's personal project in `workspaces/<username>/`)
- In `docker` shared projects, even a `host_full` user stays isolated inside `hub_sandbox`.
- A `docker_full` user NEVER gets host shell even inside a `host` shared project.
- Plain chats (`kind == "chat"`) NEVER get host shell.
- RBAC isolation (`/api/admin/*`, `/api/system/restart`, `/api/desktop/*`) remains strictly 403 for `host_full`.
- Subprocess 0% GPU invariant (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`) and hidden flags remain intact.
- Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _unlink_test_junctions(root: Path) -> None:
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
    def __init__(self, app: Any, *, client: tuple[str, int] = ("127.0.0.1", 52100)) -> None:
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
                chunk = message.get("body") or b""
                if chunk:
                    resp_chunks.append(chunk)

        await self._app(scope, _receive, _send)
        return _MiniAsgiResponse(resp_status, resp_headers, b"".join(resp_chunks))


class _FakeReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            await asyncio.sleep(0)
            return self._lines.pop(0)
        return b""

    async def read(self) -> bytes:
        out = b"".join(self._lines)
        self._lines.clear()
        return out


class _FakeProc:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.pid = 61001
        self.returncode: int | None = None
        encoded = [(json.dumps(e, ensure_ascii=False) + "\n").encode("utf-8") for e in events]
        self.stdout = _FakeReader(encoded)
        self.stderr = _FakeReader([])

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class TestDualKeyHostTerminal(unittest.IsolatedAsyncioTestCase):
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

        self._tmp = tempfile.TemporaryDirectory(prefix="agy_test_dual_key_")
        self.tmp_root = Path(self._tmp.name).resolve()

        self._saved_paths = {
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_homes": config.CLI_HOMES_DIR,
            "cfg_agy_data": config.AGY_CLI_DATA_DIR,
            "eng_agy_data": agy_engine.AGY_CLI_DATA_DIR,
            "cfg_ws": config.USER_WORKSPACES_DIR,
            "db_ws": database.USER_WORKSPACES_DIR,
            "br_ws": brain_reader.USER_WORKSPACES_DIR,
            "cfg_sp": config.SHARED_PROJECTS_DIR,
            "db_sp": database.SHARED_PROJECTS_DIR,
            "br_sp": brain_reader.SHARED_PROJECTS_DIR,
            "cfg_proj": config.PROJECTS_ROOT,
            "br_proj": brain_reader.PROJECTS_ROOT,
            "cfg_up": config.UPLOADS_DIR,
            "eng_up": agy_engine.UPLOADS_DIR,
            "cdx_up": codex_engine.UPLOADS_DIR,
            "cfg_csbx": config.CHAT_SANDBOX_DIR,
            "eng_csbx": agy_engine.CHAT_SANDBOX_DIR,
            "cdx_csbx": codex_engine.CHAT_SANDBOX_DIR,
        }

        tmp_db = self.tmp_root / "hub_test.db"
        tmp_homes = self.tmp_root / "cli_homes"
        tmp_agy_data = self.tmp_root / "agy_cli_data"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sp = self.tmp_root / "shared_projects"
        tmp_proj = self.tmp_root / "admin_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_csbx = self.tmp_root / "chat_sandbox"
        for d in (tmp_homes, tmp_agy_data, tmp_ws, tmp_sp, tmp_proj, tmp_up, tmp_csbx):
            d.mkdir(parents=True, exist_ok=True)

        config.DB_PATH = database.DB_PATH = tmp_db
        config.CLI_HOMES_DIR = tmp_homes
        config.AGY_CLI_DATA_DIR = agy_engine.AGY_CLI_DATA_DIR = tmp_agy_data
        config.USER_WORKSPACES_DIR = database.USER_WORKSPACES_DIR = brain_reader.USER_WORKSPACES_DIR = tmp_ws
        config.SHARED_PROJECTS_DIR = database.SHARED_PROJECTS_DIR = brain_reader.SHARED_PROJECTS_DIR = tmp_sp
        config.PROJECTS_ROOT = brain_reader.PROJECTS_ROOT = tmp_proj
        config.UPLOADS_DIR = agy_engine.UPLOADS_DIR = codex_engine.UPLOADS_DIR = tmp_up
        config.CHAT_SANDBOX_DIR = agy_engine.CHAT_SANDBOX_DIR = codex_engine.CHAT_SANDBOX_DIR = tmp_csbx

        await database.init_db()

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database

        agy_engine.ACTIVE_RUNS.clear()
        s = self._saved_paths
        config.DB_PATH = s["cfg_db"]
        database.DB_PATH = s["db_db"]
        config.CLI_HOMES_DIR = s["cfg_homes"]
        config.AGY_CLI_DATA_DIR = s["cfg_agy_data"]
        agy_engine.AGY_CLI_DATA_DIR = s["eng_agy_data"]
        config.USER_WORKSPACES_DIR = s["cfg_ws"]
        database.USER_WORKSPACES_DIR = s["db_ws"]
        brain_reader.USER_WORKSPACES_DIR = s["br_ws"]
        config.SHARED_PROJECTS_DIR = s["cfg_sp"]
        database.SHARED_PROJECTS_DIR = s["db_sp"]
        brain_reader.SHARED_PROJECTS_DIR = s["br_sp"]
        config.PROJECTS_ROOT = s["cfg_proj"]
        brain_reader.PROJECTS_ROOT = s["br_proj"]
        config.UPLOADS_DIR = s["cfg_up"]
        agy_engine.UPLOADS_DIR = s["eng_up"]
        codex_engine.UPLOADS_DIR = s["cdx_up"]
        config.CHAT_SANDBOX_DIR = s["cfg_csbx"]
        agy_engine.CHAT_SANDBOX_DIR = s["eng_csbx"]
        codex_engine.CHAT_SANDBOX_DIR = s["cdx_csbx"]

        _unlink_test_junctions(self.tmp_root)
        self._tmp.cleanup()

        if self._live_stat_before is not None:
            live_after = (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            self.assertEqual(self._live_stat_before, live_after, "Live data/hub.db must NEVER be modified!")

    async def test_1_db_and_effective_contour_matrix(self) -> None:
        import app.brain_reader as brain_reader
        import app.database as database
        import app.sandbox_service as sandbox_service

        admin = await database.get_user_by_id("admin")
        assert admin is not None

        # create_user preserves host_full and NEVER escalates role to admin
        u_host = await database.create_user(
            username="trusted_friend",
            display_name="Trusted Friend",
            password="pass1234",
            role="admin",  # attempted escalation must stay 'user'
            max_access_mode="host_full",
        )
        self.assertEqual(u_host["role"], "user")
        self.assertEqual(u_host["max_access_mode"], "host_full")

        u_docker = await database.create_user(
            username="docker_guest",
            password="pass1234",
            max_access_mode="docker_full",
        )
        u_files = await database.create_user(
            username="files_guest",
            password="pass1234",
            max_access_mode="write_files",
        )

        # update_user upgrades & downgrades host_full cleanly
        up_files_to_host = await database.update_user(u_files["id"], max_access_mode="host_full")
        assert up_files_to_host is not None
        self.assertEqual(up_files_to_host["max_access_mode"], "host_full")
        u_files = await database.update_user(u_files["id"], max_access_mode="write_files") or u_files
        self.assertEqual(u_files["max_access_mode"], "write_files")

        # Sandbox eligibility
        self.assertTrue(sandbox_service.is_user_sandbox_eligible(admin))
        self.assertTrue(sandbox_service.is_user_sandbox_eligible(u_host))
        self.assertTrue(sandbox_service.is_user_sandbox_eligible(u_docker))
        self.assertFalse(sandbox_service.is_user_sandbox_eligible(u_files))

        # Shared projects with host / docker / sandbox_files
        all_members = [u_host["id"], u_docker["id"], u_files["id"]]
        sp_host = await database.create_shared_project(
            name="gpu_lab",
            execution_mode="host",
            member_ids=all_members,
        )
        self.assertEqual(sp_host["execution_mode"], "host")

        sp_docker = await database.create_shared_project(
            name="docker_collab",
            execution_mode="docker",
            member_ids=all_members,
        )
        sp_switched = await database.update_shared_project(sp_docker["id"], execution_mode="host")
        assert sp_switched is not None and sp_switched["execution_mode"] == "host"
        sp_docker = await database.update_shared_project(sp_docker["id"], execution_mode="docker") or sp_docker
        self.assertEqual(sp_docker["execution_mode"], "docker")

        sp_files = await database.create_shared_project(
            name="code_only_sp",
            execution_mode="sandbox_files",
            member_ids=all_members,
        )

        # Personal projects for each user
        brain_reader.create_personal_project_folder(project_name="adm_personal", user=admin)
        brain_reader.create_personal_project_folder(project_name="friend_personal", user=u_host)
        brain_reader.create_personal_project_folder(project_name="docker_personal", user=u_docker)
        brain_reader.create_personal_project_folder(project_name="files_personal", user=u_files)

        all_sps = await database.list_shared_projects(is_admin=True)

        # Verify resolve_user_project across full Dual-Key matrix
        self.assertEqual(brain_reader.resolve_user_project("adm_personal", admin, all_sps)["execution_mode"], "host")
        self.assertEqual(brain_reader.resolve_user_project("gpu_lab", admin, all_sps)["execution_mode"], "host")
        self.assertEqual(brain_reader.resolve_user_project("docker_collab", admin, all_sps)["execution_mode"], "docker")
        self.assertEqual(brain_reader.resolve_user_project("code_only_sp", admin, all_sps)["execution_mode"], "sandbox_files")

        self.assertEqual(brain_reader.resolve_user_project("friend_personal", u_host, all_sps)["execution_mode"], "host")
        self.assertEqual(brain_reader.resolve_user_project("gpu_lab", u_host, all_sps)["execution_mode"], "host")
        self.assertEqual(brain_reader.resolve_user_project("docker_collab", u_host, all_sps)["execution_mode"], "docker")
        self.assertEqual(brain_reader.resolve_user_project("code_only_sp", u_host, all_sps)["execution_mode"], "sandbox_files")

        # docker_full user NEVER gets 'host' even inside gpu_lab (execution_mode='host')!
        self.assertEqual(brain_reader.resolve_user_project("docker_personal", u_docker, all_sps)["execution_mode"], "docker")
        self.assertEqual(brain_reader.resolve_user_project("gpu_lab", u_docker, all_sps)["execution_mode"], "docker")
        self.assertEqual(brain_reader.resolve_user_project("docker_collab", u_docker, all_sps)["execution_mode"], "docker")
        self.assertEqual(brain_reader.resolve_user_project("code_only_sp", u_docker, all_sps)["execution_mode"], "sandbox_files")

        # write_files user always stays in 'sandbox_files'
        self.assertEqual(brain_reader.resolve_user_project("files_personal", u_files, all_sps)["execution_mode"], "sandbox_files")
        self.assertEqual(brain_reader.resolve_user_project("gpu_lab", u_files, all_sps)["execution_mode"], "sandbox_files")

    async def test_2_engines_dual_key_matrix_and_zero_gpu(self) -> None:
        import app.agy_engine as agy_engine
        import app.codex_engine as codex_engine
        import app.config as config
        import app.database as database

        u_host = await database.create_user(
            username="host_dev",
            password="pw1234",
            max_access_mode="host_full",
        )
        u_docker = await database.create_user(
            username="docker_dev",
            password="pw1234",
            max_access_mode="docker_full",
        )
        sp_host = await database.create_shared_project(
            name="shared_host_gpu",
            execution_mode="host",
            member_ids=[u_host["id"], u_docker["id"]],
        )
        sp_docker = await database.create_shared_project(
            name="shared_docker_iso",
            execution_mode="docker",
            member_ids=[u_host["id"], u_docker["id"]],
        )
        cli_home = config.ensure_user_cli_home(u_host)
        proj_host_path = Path(sp_host["path"]).resolve()
        proj_docker_path = Path(sp_docker["path"]).resolve()

        # 2a. user=host_full + shared(host) + full_access -> host shell ON, hub_sandbox OFF
        agy_args_host = agy_engine._build_permission_args(
            "project",
            "full_access",
            is_admin=False,
            allow_hub_openrouter=False,
            allow_hub_sandbox=True,  # even if passed True, host contour must disable hub_sandbox!
            cli_home_dir=cli_home,
            project_dir=proj_host_path,
            force_no_host_shell=False,
        )
        self.assertIn("--dangerously-skip-permissions", agy_args_host)
        host_settings = json.loads(agy_args_host[agy_args_host.index("--settings") + 1])
        host_deny = host_settings["permissions"].get("deny") or []
        host_allow = host_settings["permissions"].get("allow") or []
        self.assertNotIn("command(*)", host_deny)
        self.assertIn("mcp__hub_sandbox__*", host_deny)
        self.assertIn("sandbox_exec", host_deny)
        self.assertNotIn("sandbox_exec", host_allow)

        cdx_host = codex_engine.resolve_codex_security_matrix(
            kind="project",
            access_mode="full_access",
            is_admin=False,
            force_no_host_shell=False,
            allow_hub_sandbox=True,
            workspace_dir=proj_host_path,
        )
        self.assertEqual(cdx_host["sandbox_mode"], "danger-full-access")
        self.assertTrue(cdx_host["shell_tool"])
        self.assertFalse(cdx_host["allow_hub_sandbox"])

        # 2b. user=host_full + shared(docker) + full_access -> stays inside Docker hub_sandbox!
        agy_args_dkr = agy_engine._build_permission_args(
            "project",
            "full_access",
            is_admin=False,
            allow_hub_sandbox=True,
            cli_home_dir=cli_home,
            project_dir=proj_docker_path,
            force_no_host_shell=True,
        )
        self.assertNotIn("--dangerously-skip-permissions", agy_args_dkr)
        dkr_settings = json.loads(agy_args_dkr[agy_args_dkr.index("--settings") + 1])
        self.assertIn("command(*)", dkr_settings["permissions"]["deny"])
        self.assertIn("sandbox_exec", dkr_settings["permissions"]["allow"])

        cdx_dkr = codex_engine.resolve_codex_security_matrix(
            kind="project",
            access_mode="full_access",
            is_admin=False,
            force_no_host_shell=True,
            allow_hub_sandbox=True,
            workspace_dir=proj_docker_path,
        )
        self.assertEqual(cdx_dkr["sandbox_mode"], "workspace-write")
        self.assertFalse(cdx_dkr["shell_tool"])
        self.assertTrue(cdx_dkr["allow_hub_sandbox"])

        # 2c. user=host_full + kind="chat" -> NEVER gets host shell
        agy_chat = agy_engine._build_permission_args(
            "chat",
            "full_access",
            is_admin=False,
            cli_home_dir=cli_home,
            force_no_host_shell=False,
        )
        self.assertNotIn("--dangerously-skip-permissions", agy_chat)
        chat_settings = json.loads(agy_chat[agy_chat.index("--settings") + 1])
        self.assertIn("command(*)", chat_settings["permissions"]["deny"])
        self.assertIn("write_file(*)", chat_settings["permissions"]["deny"])

        cdx_chat = codex_engine.resolve_codex_security_matrix(
            kind="chat",
            access_mode="full_access",
            is_admin=False,
            force_no_host_shell=False,
            allow_hub_sandbox=True,
        )
        self.assertEqual(cdx_chat["sandbox_mode"], "read-only")
        self.assertFalse(cdx_chat["shell_tool"])
        self.assertFalse(cdx_chat["allow_hub_sandbox"])

        # 2d. E2E stream_agy_turn checks:
        #     (1) host_full + shared(host) -> --dangerously-skip-permissions, 0% GPU env, sandbox_allowed=False
        #     (2) host_full + shared(docker) -> command(*) denied, sandbox_allowed=True
        #     (3) docker_full + shared(host) -> command(*) denied, NEVER --dangerously-skip-permissions!
        fake_agy_exe = self.tmp_root / "fake_agy.exe"
        fake_agy_exe.write_bytes(b"MZ")
        recorded_runs: list[dict[str, Any]] = []

        async def _fake_subproc(*cmd: Any, **kwargs: Any) -> _FakeProc:
            cid_active = list(agy_engine.ACTIVE_RUNS.keys())[0]
            rs = agy_engine.ACTIVE_RUNS[cid_active]
            recorded_runs.append(
                {
                    "cmd": list(cmd),
                    "env": dict(kwargs.get("env") or {}),
                    "sandbox_allowed": rs.sandbox_allowed,
                    "access_mode": rs.access_mode,
                }
            )
            return _FakeProc(
                [
                    {"event": "init", "conversation_id": "conv-dk"},
                    {"event": "message", "delta": "ok"},
                    {"event": "result", "status": "success", "usage": {"total_tokens": 10}},
                ]
            )

        with (
            patch("app.agy_engine.resolve_agy_bin", return_value=str(fake_agy_exe)),
            patch(
                "app.agy_engine.check_docker_status",
                return_value={"sandbox_mcp_ready": True, "daemon_running": True, "image_ready": True},
            ),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_subproc),
        ):
            # Case (1): host_full + shared(host)
            async for _ in agy_engine.stream_agy_turn(
                chat_id="c_host_ok",
                kind="project",
                project_name="shared_host_gpu",
                model="gemini-4-carbon-high",
                access_mode="full_access",
                prompt="ping",
                attachments=[],
                agy_conversation_id="",
                history_messages=[],
                user=u_host,
                resolved_project_dir=proj_host_path,
                execution_mode="host",
            ):
                pass
            agy_engine.ACTIVE_RUNS.clear()

            # Case (2): host_full + shared(docker)
            async for _ in agy_engine.stream_agy_turn(
                chat_id="c_host_in_docker",
                kind="project",
                project_name="shared_docker_iso",
                model="gemini-4-carbon-high",
                access_mode="full_access",
                prompt="ping",
                attachments=[],
                agy_conversation_id="",
                history_messages=[],
                user=u_host,
                resolved_project_dir=proj_docker_path,
                execution_mode="docker",
            ):
                pass
            agy_engine.ACTIVE_RUNS.clear()

            # Case (3): docker_full + shared(host) -> MUST stay in Docker, NO host shell!
            async for _ in agy_engine.stream_agy_turn(
                chat_id="c_docker_in_host_proj",
                kind="project",
                project_name="shared_host_gpu",
                model="gemini-4-carbon-high",
                access_mode="full_access",
                prompt="ping",
                attachments=[],
                agy_conversation_id="",
                history_messages=[],
                user=u_docker,
                resolved_project_dir=proj_host_path,
                execution_mode="host",
            ):
                pass
            agy_engine.ACTIVE_RUNS.clear()

        r1, r2, r3 = recorded_runs
        self.assertIn("--dangerously-skip-permissions", r1["cmd"])
        self.assertFalse(r1["sandbox_allowed"])
        self.assertEqual(r1["env"].get("CUDA_VISIBLE_DEVICES"), "")
        self.assertEqual(r1["env"].get("ORT_DISABLE_ALL_GPU"), "1")

        self.assertNotIn("--dangerously-skip-permissions", r2["cmd"])
        self.assertTrue(r2["sandbox_allowed"])

        self.assertNotIn("--dangerously-skip-permissions", r3["cmd"])
        self.assertTrue(r3["sandbox_allowed"])

    async def test_3_rbac_and_sandbox_reset_endpoints(self) -> None:
        import app.auth as auth
        import app.database as database
        import app.main as main

        admin = await database.get_user_by_id("admin")
        assert admin is not None
        u_host = await database.create_user(
            username="trusted_rbac",
            password="pw1234",
            max_access_mode="host_full",
        )
        u_docker = await database.create_user(
            username="docker_rbac",
            password="pw1234",
            max_access_mode="docker_full",
        )
        await database.create_shared_project(
            name="sp_host_mode",
            execution_mode="host",
            member_ids=[u_host["id"], u_docker["id"]],
        )
        await database.create_shared_project(
            name="sp_docker_mode",
            execution_mode="docker",
            member_ids=[u_host["id"], u_docker["id"]],
        )

        tok_host = auth.create_user_session_token(u_host["id"], u_host["session_version"])
        tok_docker = auth.create_user_session_token(u_docker["id"], u_docker["session_version"])
        tok_admin = auth.create_user_session_token(admin["id"], admin["session_version"])
        hdr_host = {"Authorization": f"Bearer {tok_host}"}
        hdr_docker = {"Authorization": f"Bearer {tok_docker}"}
        hdr_admin = {"Authorization": f"Bearer {tok_admin}"}

        client = _MiniAsgiClient(main.app)

        # RBAC: host_full user MUST receive 403 on all admin/system/desktop endpoints
        for method, ep in (
            ("GET", "/api/admin/overview"),
            ("GET", "/api/admin/shared-projects"),
            ("POST", "/api/system/restart"),
            ("GET", "/api/desktop/conversations"),
        ):
            resp = await client.request(method, ep, headers=hdr_host)
            self.assertEqual(resp.status_code, 403, f"Expected 403 for host_full on {ep}, got {resp.status_code}")

        # Admin can create/update host_full user & host shared project via REST API
        r_upd = await client.request(
            "PATCH",
            f"/api/admin/users/{u_docker['id']}",
            json_body={"max_access_mode": "host_full"},
            headers=hdr_admin,
        )
        self.assertEqual(r_upd.status_code, 200)
        self.assertEqual(r_upd.json()["user"]["max_access_mode"], "host_full")
        # Restore u_docker back to docker_full
        await client.request(
            "PATCH",
            f"/api/admin/users/{u_docker['id']}",
            json_body={"max_access_mode": "docker_full"},
            headers=hdr_admin,
        )

        # /sandbox/status & /sandbox/reset Dual-Key checks
        st_host_on_host = await client.request("GET", "/api/projects/sp_host_mode/sandbox/status", headers=hdr_host)
        self.assertEqual(st_host_on_host.status_code, 200)
        self.assertFalse(st_host_on_host.json()["docker_contour"])

        rst_host_on_host = await client.request("POST", "/api/projects/sp_host_mode/sandbox/reset", headers=hdr_host)
        self.assertEqual(rst_host_on_host.status_code, 400)

        rst_adm_on_host = await client.request("POST", "/api/projects/sp_host_mode/sandbox/reset", headers=hdr_admin)
        self.assertEqual(rst_adm_on_host.status_code, 400)

        st_host_on_dkr = await client.request("GET", "/api/projects/sp_docker_mode/sandbox/status", headers=hdr_host)
        self.assertEqual(st_host_on_dkr.status_code, 200)
        self.assertTrue(st_host_on_dkr.json()["docker_contour"])

        st_dkr_on_host = await client.request("GET", "/api/projects/sp_host_mode/sandbox/status", headers=hdr_docker)
        self.assertEqual(st_dkr_on_host.status_code, 200)
        self.assertTrue(st_dkr_on_host.json()["docker_contour"])

    def test_4_frontend_admin_and_contour_ui(self) -> None:
        html_text = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT_DIR / "app" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('<option value="host">💻 Терминал ПК (Windows Host + GPU)</option>', html_text)
        self.assertIn(
            '<option value="host_full">🚀 Полный доступ к ПК (Windows-терминал + Docker)</option>',
            html_text,
        )
        self.assertIn('id="ueHostFullHint"', html_text)
        self.assertIn("CUDA_VISIBLE_DEVICES", html_text)

        for js_anchor in (
            'maxMode === "host_full"',
            'u.max_access_mode === "host_full"',
            'host_full: "🚀 Полный доступ к ПК (Windows-терминал)"',
            'projObj.execution_mode === "host"',
            "Терминал ПК",
            '<option value="host" ${sp.execution_mode === "host" ? "selected" : ""}>💻 Терминал ПК (Windows Host + GPU)</option>',
        ):
            self.assertIn(js_anchor, app_js)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for js_rel in ("app.js", "sw.js"):
            proc = subprocess.run(
                ["node", "--check", str(ROOT_DIR / "app" / "static" / js_rel)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=creationflags,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
