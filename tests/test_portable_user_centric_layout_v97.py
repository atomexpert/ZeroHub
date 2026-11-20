"""
Isolated automated verification suite for Release v97:
1. Portable `.env` auto-healing (`_resolve_portable_env_path`), user-centric hierarchy
   (`users/<slug>/{projects,chats,uploads,context}` + `shared_projects/{_uploads,_context}`),
   upload routing with legacy `data/uploads/<chat_id>` fallback, and `.md` context mirrors
   (`RULES.md`, `<Project>.md`, `CONTEXT_SUMMARY.md`).
2. Conflict-free self-hosting of `Antigravity_Hub` (`is_self_hub=True` for `admin`, hidden/403 for
   non-admin, real `.git` preserved on `BASE_DIR`, `.git` boundary on child projects, `workspace_mcp`
   top-level `users`/`shared_projects`/`data`/`scratch` exclusion, reserved project names, and
   `validate_project_extra_dirs` Zero-Trust checks).
3. Automatic path relocation (`relocate_hub_paths_if_base_moved`) when moving `Antigravity_Hub`
   across folders/drives (`hub_meta.last_base_dir`, `chats`, `shared_projects`, `users`,
   `messages.attachments_json`, `project_memories.extra_dirs_json`, `host_services/registry.json`).
4. Idempotent `v96 -> v97` startup migration + sibling project consolidation (`move_legacy_siblings`,
   recursion guard on `BASE_DIR`, DB chat relinking, and Windows `PermissionError` copy fallback).
5. Portable launcher `.venv` health checks (`start_hub.bat`, `start_hub_hidden.pyw`) & `v=97`
   frontend/UTF-8 no-BOM/`main.workspace` CSS hygiene.

Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or live folders.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import patch

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("ORT_DISABLE_ALL_GPU", "1")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


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
    def __init__(self, app: Any, *, client: tuple[str, int] = ("127.0.0.1", 53197)) -> None:
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


class TestReleaseV97PortableUserCentricLayout(unittest.IsolatedAsyncioTestCase):
    def _build_hub_patch_specs(self, hub_base: Path, hsm_inst: Any) -> list[tuple[Any, dict[str, Any]]]:
        data_dir = hub_base / "data"
        users_dir = hub_base / "users"
        projects_root = users_dir / "admin" / "projects"
        shared_dir = hub_base / "shared_projects"
        shared_uploads = shared_dir / "_uploads"
        shared_ctx = shared_dir / "_context"
        cli_homes = data_dir / "cli_homes"
        uploads_dir = data_dir / "uploads"
        db_path = data_dir / "hub.db"

        for d in (
            hub_base,
            hub_base / "app",
            hub_base / "tools",
            hub_base / "tests",
            hub_base / ".venv",
            hub_base / ".git",
            data_dir,
            users_dir,
            projects_root,
            shared_dir,
            shared_uploads,
            shared_ctx,
            cli_homes,
            uploads_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        (hub_base / "run_server.py").write_text("# mock hub entry\n", encoding="utf-8")
        (hub_base / "app" / "main.py").write_text("# mock hub core\nSECRET_MARKER = 'CORE_OK'\n", encoding="utf-8")
        (hub_base / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        return [
            (
                self.config,
                {
                    "BASE_DIR": hub_base,
                    "ENV_PATH": hub_base / ".env",
                    "DATA_DIR": data_dir,
                    "DB_PATH": db_path,
                    "USERS_DIR": users_dir,
                    "USER_WORKSPACES_DIR": users_dir,
                    "PROJECTS_ROOT": projects_root,
                    "SHARED_PROJECTS_DIR": shared_dir,
                    "SHARED_UPLOADS_DIR": shared_uploads,
                    "SHARED_CONTEXT_DIR": shared_ctx,
                    "CLI_HOMES_DIR": cli_homes,
                    "UPLOADS_DIR": uploads_dir,
                    "LEGACY_DATA_SHARED_PROJECTS_DIR": data_dir / "shared_projects",
                    "LEGACY_DATA_WORKSPACES_DIR": data_dir / "workspaces",
                    "LEGACY_DATA_UPLOADS_DIR": uploads_dir,
                    "AGY_CLI_DATA_DIR": self.agy_cli_dir,
                    "AGY_APP_DATA_DIR": self.agy_app_dir,
                },
            ),
            (
                self.database,
                {
                    "BASE_DIR": hub_base,
                    "DB_PATH": db_path,
                    "USERS_DIR": users_dir,
                    "USER_WORKSPACES_DIR": users_dir,
                    "PROJECTS_ROOT": projects_root,
                    "SHARED_PROJECTS_DIR": shared_dir,
                },
            ),
            (
                self.brain_reader,
                {
                    "BASE_DIR": hub_base,
                    "USERS_DIR": users_dir,
                    "USER_WORKSPACES_DIR": users_dir,
                    "PROJECTS_ROOT": projects_root,
                    "SHARED_PROJECTS_DIR": shared_dir,
                    "AGY_APP_DATA_DIR": self.agy_app_dir,
                },
            ),
            (
                self.workspace_mcp,
                {
                    "BASE_DIR": hub_base,
                    "DATA_DIR": data_dir,
                    "CLI_HOMES_DIR": cli_homes,
                },
            ),
            (
                self.agy_engine,
                {
                    "BASE_DIR": hub_base,
                    "PROJECTS_ROOT": projects_root,
                    "SHARED_PROJECTS_DIR": shared_dir,
                    "CLI_HOMES_DIR": cli_homes,
                    "AGY_CLI_DATA_DIR": self.agy_cli_dir,
                },
            ),
            (
                self.project_migrator,
                {
                    "BASE_DIR": hub_base,
                    "AGY_APP_DATA_DIR": self.agy_app_dir,
                },
            ),
            (
                self.main_mod,
                {
                    "BASE_DIR": hub_base,
                    "USERS_DIR": users_dir,
                    "USER_WORKSPACES_DIR": users_dir,
                    "PROJECTS_ROOT": projects_root,
                    "SHARED_PROJECTS_DIR": shared_dir,
                    "SHARED_UPLOADS_DIR": shared_uploads,
                    "UPLOADS_DIR": uploads_dir,
                },
            ),
            (self.host_service_manager, {"HOST_SERVICE_MANAGER": hsm_inst}),
            (self.database, {"HOST_SERVICE_MANAGER": hsm_inst}),
        ]

    def _apply_hub_patches(self, hub_base: Path, hsm_inst: Any) -> None:
        for p in reversed(getattr(self, "_patches", [])):
            p.stop()
        self._patches = []
        for mod, mapping in self._build_hub_patch_specs(hub_base, hsm_inst):
            for attr, val in mapping.items():
                if hasattr(mod, attr):
                    self._patches.append(patch.object(mod, attr, val))
        for p in self._patches:
            p.start()

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v97_test_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()
        self.agy_cli_dir = self.tmp_root / "agy_cli_shared"
        self.agy_app_dir = self.tmp_root / "agy_app_desktop"
        self.agy_cli_dir.mkdir(parents=True, exist_ok=True)
        self.agy_app_dir.mkdir(parents=True, exist_ok=True)

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

        self.parent_dir = self.tmp_root / "Internet" / "Antigravity"
        self.hub_dir = self.parent_dir / "Antigravity_Hub"
        self._patches: list[Any] = []
        self._isolated_hsm = host_service_manager.HostServiceManager()
        self._apply_hub_patches(self.hub_dir, self._isolated_hsm)

        await database.init_db()
        self.admin_user = await database.get_user_by_id("admin")
        assert self.admin_user is not None

        self.garik = await database.create_user(
            username="garik",
            display_name="Garik",
            password="pass1234",
            role="user",
            max_access_mode="docker_full",
        )
        self.leha = await database.create_user(
            username="leha",
            display_name="Leha",
            password="pass1234",
            role="user",
            max_access_mode="host_full",
        )
        self.admin_token = auth.create_user_session_token(
            "admin", int(self.admin_user["session_version"])
        )
        self.garik_token = auth.create_user_session_token(
            self.garik["id"], int(self.garik["session_version"])
        )
        self.leha_token = auth.create_user_session_token(
            self.leha["id"], int(self.leha["session_version"])
        )
        self.client = _MiniAsgiClient(main_mod.app)

    async def asyncTearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        _unlink_junctions(self.tmp_root)
        self._tmp.cleanup()

    async def _call_files_mcp(self, token: str, tool_name: str, arguments: dict[str, Any]) -> str:
        resp = await self.client.request(
            "POST",
            f"/mcp/files/{token}",
            json_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertNotIn("error", body, body)
        res = body["result"]
        if res.get("isError"):
            raise RuntimeError(res["content"][0]["text"])
        return str(res["content"][0]["text"])

    async def test_01_user_centric_hierarchy_uploads_fallback_and_context_mirrors(self) -> None:
        # 1. Portable `.env` auto-healing
        env_sim = self.tmp_root / "env_sim" / "Antigravity_Hub"
        (env_sim / "users" / "admin" / "projects").mkdir(parents=True, exist_ok=True)
        (env_sim / "data").mkdir(parents=True, exist_ok=True)
        env_file = env_sim / ".env"
        env_file.write_text(
            f"HUB_PIN=9999\nPROJECTS_ROOT={env_sim.parent}\nHUB_DATA_DIR={env_sim / 'data'}\nSHARED_PROJECTS_DIR=E:\\MovedOld\\Antigravity_Hub\\shared_projects\n",
            encoding="utf-8",
        )
        sim_environ = {
            "PROJECTS_ROOT": str(env_sim.parent),
            "HUB_DATA_DIR": str(env_sim / "data"),
            "SHARED_PROJECTS_DIR": r"E:\MovedOld\Antigravity_Hub\shared_projects",
        }
        p_proj = self.config._resolve_portable_env_path(
            "PROJECTS_ROOT",
            Path("users/admin/projects"),
            legacy_parent_migrate=True,
            base_dir=env_sim,
            env_path=env_file,
            environ=sim_environ,
        )
        p_data = self.config._resolve_portable_env_path(
            "HUB_DATA_DIR",
            Path("data"),
            base_dir=env_sim,
            env_path=env_file,
            environ=sim_environ,
        )
        p_shared = self.config._resolve_portable_env_path(
            "SHARED_PROJECTS_DIR",
            Path("shared_projects"),
            base_dir=env_sim,
            env_path=env_file,
            environ=sim_environ,
        )
        self.assertEqual(p_proj, (env_sim / "users" / "admin" / "projects").resolve())
        self.assertEqual(p_data, (env_sim / "data").resolve())
        self.assertEqual(p_shared, (env_sim / "shared_projects").resolve())
        healed_txt = env_file.read_text(encoding="utf-8")
        self.assertIn("HUB_PIN=9999", healed_txt)
        self.assertIn("PROJECTS_ROOT=./users/admin/projects", healed_txt)
        self.assertIn("HUB_DATA_DIR=./data", healed_txt)
        self.assertIn("SHARED_PROJECTS_DIR=./shared_projects", healed_txt)

        # 2. Canonical `users/{admin,garik,leha}/{projects,chats,uploads,context}` + `shared_projects/{_uploads,_context}`
        for slug in ("admin", "garik", "leha"):
            for sub in ("projects", "chats", "uploads", "context"):
                self.assertTrue((self.hub_dir / "users" / slug / sub).is_dir(), f"Missing {slug}/{sub}")
        self.assertTrue((self.hub_dir / "shared_projects" / "_uploads").is_dir())
        self.assertTrue((self.hub_dir / "shared_projects" / "_context").is_dir())

        # Free-chat sandbox resolution
        garik_sb = self.config.resolve_user_chat_sandbox_dir(self.garik, "chat-garik-1")
        self.assertEqual(garik_sb, (self.hub_dir / "users" / "garik" / "chats" / "chat-garik-1").resolve())

        # 3. Upload routing + legacy `data/uploads/<chat_id>` fallback + `/api/media` RBAC
        await self.database.create_shared_project(
            name="SharedCRM", execution_mode="docker", member_ids=[self.garik["id"]]
        )

        up_personal = self.config.resolve_chat_uploads_dir(
            {"id": "c_pers_1"}, owner_username="garik", is_shared_project=False
        )
        up_shared = self.config.resolve_chat_uploads_dir(
            {"id": "c_shared_1"}, owner_username="garik", is_shared_project=True
        )
        self.assertEqual(up_personal, (self.hub_dir / "users" / "garik" / "uploads" / "c_pers_1").resolve())
        self.assertEqual(up_shared, (self.hub_dir / "shared_projects" / "_uploads" / "c_shared_1").resolve())

        # Seed a legacy attachment in `data/uploads/<chat_id>` and verify fallback lookup + media RBAC
        chat_g = await self.database.create_chat(
            title="Garik Legacy Upload Chat",
            kind="chat",
            owner_id=self.garik["id"],
        )
        legacy_dir = self.hub_dir / "data" / "uploads" / chat_g["id"]
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_img = legacy_dir / "legacy_diagram.png"
        legacy_img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        found_legacy = self.config.find_existing_chat_upload_file(
            chat_g["id"], "legacy_diagram.png", chat=chat_g, owner_username="garik", is_shared_project=False
        )
        self.assertEqual(found_legacy, legacy_img.resolve())

        r_owner = await self.client.request(
            "GET",
            f"/api/media?path={urllib.parse.quote(str(legacy_img))}",
            headers={"Authorization": f"Bearer {self.garik_token}"},
        )
        self.assertEqual(r_owner.status_code, 200)
        r_stranger = await self.client.request(
            "GET",
            f"/api/media?path={urllib.parse.quote(str(legacy_img))}",
            headers={"Authorization": f"Bearer {self.leha_token}"},
        )
        self.assertEqual(r_stranger.status_code, 403)

        # 4. Readable `.md` context mirrors (`RULES.md`, `<Project>.md`, `CONTEXT_SUMMARY.md`)
        await self.database.update_user_rules(self.garik["id"], custom_rules="Пиши кратко и по делу.")
        rules_mirror = self.hub_dir / "users" / "garik" / "context" / "RULES.md"
        self.assertTrue(rules_mirror.is_file())
        self.assertIn("Пиши кратко и по делу.", rules_mirror.read_text(encoding="utf-8"))

        self.brain_reader.create_personal_project_folder(project_name="MyFastApi", user=self.admin_user)
        scope_my = self.database.build_project_memory_scope_key("MyFastApi", owner_id="admin", is_shared=False)
        await self.database.save_project_memory(
            scope_my,
            owner_id="admin",
            project_name="MyFastApi",
            passport_overview="FastAPI микросервис заказов",
            passport_rules="Python 3.12, ruff",
            passport_file_map="app/api.py",
        )
        for idx in range(30):
            await self.database.append_project_changelog_entry(
                scope_my,
                owner_id="admin",
                project_name="MyFastApi",
                entry={"summary": f"Шаг #{idx + 1}"},
            )
        mem_personal = await self.database.get_project_memory(scope_my, project_name="MyFastApi", owner_id="admin")
        self.assertEqual(len(mem_personal["changelog"]), 25)
        proj_md = self.hub_dir / "users" / "admin" / "context" / "MyFastApi.md"
        self.assertTrue(proj_md.is_file())
        self.assertIn("FastAPI микросервис заказов", proj_md.read_text(encoding="utf-8"))

        scope_shared = self.database.build_project_memory_scope_key("SharedCRM", owner_id="admin", is_shared=True)
        await self.database.save_project_memory(
            scope_shared,
            owner_id="_shared",
            project_name="SharedCRM",
            passport_overview="Общая CRM команды",
        )
        shared_md = self.hub_dir / "shared_projects" / "_context" / "SharedCRM.md"
        self.assertTrue(shared_md.is_file())
        self.assertIn("Общая CRM команды", shared_md.read_text(encoding="utf-8"))

        await self.database.save_chat_compaction(
            chat_g["id"],
            compacted_summary="Краткая сводка диалога Гарика",
            compacted_up_to_msg_id=5,
            compacted_count=5,
        )
        summary_md = self.hub_dir / "users" / "garik" / "chats" / chat_g["id"] / "CONTEXT_SUMMARY.md"
        self.assertTrue(summary_md.is_file())
        self.assertIn("Краткая сводка диалога Гарика", summary_md.read_text(encoding="utf-8"))

        r_projs_info = await self.client.request(
            "GET", "/api/projects", headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(r_projs_info.status_code, 200)
        self.assertEqual(r_projs_info.json()["workspace_label"], "users/admin/projects")

        r_garik_projs_info = await self.client.request(
            "GET", "/api/projects", headers={"Authorization": f"Bearer {self.garik_token}"}
        )
        self.assertEqual(r_garik_projs_info.status_code, 200)
        self.assertEqual(r_garik_projs_info.json()["workspace_label"], "users/garik/projects")

        r_sys = await self.client.request(
            "GET", "/api/system/info", headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(r_sys.status_code, 200)
        sys_json = r_sys.json()
        self.assertEqual(Path(sys_json["base_dir"]).resolve(), self.hub_dir.resolve())
        self.assertEqual(Path(sys_json["users_dir"]).resolve(), (self.hub_dir / "users").resolve())
        self.assertEqual(
            Path(sys_json["projects_root"]).resolve(),
            (self.hub_dir / "users" / "admin" / "projects").resolve(),
        )

    async def test_02_conflict_free_self_hosting_and_zero_trust_boundaries(self) -> None:
        # 1. Child project inside `users/admin/projects/AlphaBot` gets `.git` boundary; `BASE_DIR` keeps real `.git`
        alpha_info = self.brain_reader.create_personal_project_folder(
            project_name="AlphaBot", user=self.admin_user
        )
        alpha_dir = Path(alpha_info["path"]).resolve()
        (alpha_dir / "bot_main.py").write_text("ALPHA_TOKEN_MARKER = 'CHILD_BOT_OK'\n", encoding="utf-8")
        self.assertTrue((alpha_dir / ".git").is_dir())
        self.assertEqual(
            (self.hub_dir / ".git" / "HEAD").read_text(encoding="utf-8"),
            "ref: refs/heads/main\n",
        )

        # 2. Admin sees `Antigravity_Hub` (`is_self_hub=True`); non-admin does NOT see it and gets 403
        r_admin_projs = await self.client.request(
            "GET", "/api/projects", headers={"Authorization": f"Bearer {self.admin_token}"}
        )
        self.assertEqual(r_admin_projs.status_code, 200)
        admin_projs = {p["name"]: p for p in r_admin_projs.json()["projects"]}
        self.assertIn("Antigravity_Hub", admin_projs)
        self.assertTrue(admin_projs["Antigravity_Hub"]["is_self_hub"])
        self.assertEqual(admin_projs["Antigravity_Hub"]["execution_mode"], "host")
        self.assertEqual(Path(admin_projs["Antigravity_Hub"]["path"]).resolve(), self.hub_dir.resolve())

        r_garik_projs = await self.client.request(
            "GET", "/api/projects", headers={"Authorization": f"Bearer {self.garik_token}"}
        )
        self.assertEqual(r_garik_projs.status_code, 200)
        garik_names = {p["name"] for p in r_garik_projs.json()["projects"]}
        self.assertNotIn("Antigravity_Hub", garik_names)
        self.assertIsNone(
            self.brain_reader.resolve_user_project("Antigravity_Hub", self.garik, [])
        )
        r_garik_chat = await self.client.request(
            "POST",
            "/api/chats",
            json_body={"kind": "project", "project_name": "Antigravity_Hub", "title": "Intrude"},
            headers={"Authorization": f"Bearer {self.garik_token}"},
        )
        self.assertIn(r_garik_chat.status_code, (403, 404))

        # 3. Reserved project names blocked on both personal and shared project creation
        for reserved_name in ("Antigravity_Hub", "users", "shared_projects", "data", "app", "_uploads", "_context"):
            r_bad_p = await self.client.request(
                "POST",
                "/api/projects/create",
                json_body={"name": reserved_name},
                headers={"Authorization": f"Bearer {self.admin_token}"},
            )
            self.assertEqual(r_bad_p.status_code, 400, f"Expected 400 for {reserved_name}")
            r_bad_sp = await self.client.request(
                "POST",
                "/api/admin/shared-projects",
                json_body={"name": reserved_name, "execution_mode": "docker"},
                headers={"Authorization": f"Bearer {self.admin_token}"},
            )
            self.assertEqual(r_bad_sp.status_code, 400, f"Expected 400 for shared {reserved_name}")

        # 4. `workspace_mcp` inside `Antigravity_Hub` (`BASE_DIR`) skips `users/`, `shared_projects/`, `data/`, `scratch/`
        (self.hub_dir / "scratch").mkdir(parents=True, exist_ok=True)
        (self.hub_dir / "scratch" / "temp_note.py").write_text("ALPHA_TOKEN_MARKER = 'SCRATCH'\n", encoding="utf-8")
        sp_portal = await self.database.create_shared_project(name="TeamPortal", execution_mode="docker")
        sp_dir = Path(sp_portal["path"]).resolve()
        (sp_dir / "portal.py").write_text("ALPHA_TOKEN_MARKER = 'SHARED'\n", encoding="utf-8")

        hub_chat = await self.database.create_chat(
            title="Self Hub Dev",
            kind="project",
            project_name="Antigravity_Hub",
            project_path=str(self.hub_dir),
            owner_id="admin",
        )
        hub_mcp_tok = self.workspace_mcp.build_files_mcp_token(
            "admin", int(self.admin_user["session_version"]), hub_chat["id"]
        )
        self.agy_engine.ACTIVE_RUNS[hub_chat["id"]] = self.agy_engine.ActiveRun(
            chat_id=hub_chat["id"],
            process=None,
            user_id="admin",
            kind="project",
            project_name="Antigravity_Hub",
            project_dir=self.hub_dir,
            workspace_dir=self.hub_dir,
            access_mode="host_full",
            host_allowed=True,
        )
        try:
            ls_root = await self._call_files_mcp(hub_mcp_tok, "list_dir", {"path": ".", "recursive": True})
            self.assertIn("app/main.py", ls_root)
            self.assertIn("run_server.py", ls_root)
            self.assertNotIn("AlphaBot", ls_root)
            self.assertNotIn("TeamPortal", ls_root)
            self.assertNotIn("hub.db", ls_root)
            self.assertNotIn("temp_note.py", ls_root)

            found_py = await self._call_files_mcp(hub_mcp_tok, "find_by_name", {"pattern": "*.py"})
            self.assertIn("app/main.py", found_py)
            self.assertNotIn("bot_main.py", found_py)
            self.assertNotIn("portal.py", found_py)

            grep_core = await self._call_files_mcp(hub_mcp_tok, "grep_search", {"query": "SECRET_MARKER"})
            self.assertIn("app/main.py", grep_core)
            grep_child = await self._call_files_mcp(hub_mcp_tok, "grep_search", {"query": "ALPHA_TOKEN_MARKER"})
            self.assertNotIn("bot_main.py", grep_child)
            self.assertNotIn("portal.py", grep_child)

            with self.assertRaises(RuntimeError):
                await self._call_files_mcp(hub_mcp_tok, "read_file", {"path": "data/hub.db"})
        finally:
            self.agy_engine.ACTIVE_RUNS.pop(hub_chat["id"], None)

        # Meanwhile inside child project `AlphaBot`, `bot_main.py` IS fully visible and searchable
        alpha_chat = await self.database.create_chat(
            title="Alpha Chat",
            kind="project",
            project_name="AlphaBot",
            project_path=str(alpha_dir),
            owner_id="admin",
        )
        alpha_mcp_tok = self.workspace_mcp.build_files_mcp_token(
            "admin", int(self.admin_user["session_version"]), alpha_chat["id"]
        )
        self.agy_engine.ACTIVE_RUNS[alpha_chat["id"]] = self.agy_engine.ActiveRun(
            chat_id=alpha_chat["id"],
            process=None,
            user_id="admin",
            kind="project",
            project_name="AlphaBot",
            project_dir=alpha_dir,
            workspace_dir=alpha_dir,
            access_mode="host_full",
            host_allowed=True,
        )
        try:
            grep_in_alpha = await self._call_files_mcp(
                alpha_mcp_tok, "grep_search", {"query": "ALPHA_TOKEN_MARKER"}
            )
            self.assertIn("bot_main.py", grep_in_alpha)
        finally:
            self.agy_engine.ACTIVE_RUNS.pop(alpha_chat["id"], None)

        # 5. `validate_project_extra_dirs` allows `users/admin/projects/BetaLib` and blocks Hub core/secrets
        beta_info = self.brain_reader.create_personal_project_folder(
            project_name="BetaLib", user=self.admin_user
        )
        beta_dir = Path(beta_info["path"]).resolve()
        valid_extra = self.brain_reader.validate_project_extra_dirs(
            [str(beta_dir)], user=self.admin_user, exec_mode="host", project_dir=alpha_dir
        )
        self.assertEqual([p.resolve() for p in valid_extra], [beta_dir])
        for forbidden_dir in (
            self.hub_dir / "data",
            self.hub_dir / "data" / "cli_homes",
            self.hub_dir / "app",
            self.hub_dir / ".venv",
            self.hub_dir / ".git",
        ):
            with self.assertRaises(ValueError):
                self.brain_reader.validate_project_extra_dirs(
                    [str(forbidden_dir)], user=self.admin_user, exec_mode="host", project_dir=alpha_dir
                )

    async def test_03_auto_relocation_on_hub_folder_move(self) -> None:
        # Seed personal project, shared project, self-hub chat, attachment, extra_dirs, and host_services registry
        alpha_dir = Path(
            self.brain_reader.create_personal_project_folder(
                project_name="AlphaBot", user=self.admin_user
            )["path"]
        ).resolve()
        beta_dir = Path(
            self.brain_reader.create_personal_project_folder(
                project_name="BetaLib", user=self.admin_user
            )["path"]
        ).resolve()
        await self.database.create_shared_project(name="SharedCRM", execution_mode="docker")

        c_alpha = await self.database.create_chat(
            title="Alpha Reloc",
            kind="project",
            project_name="AlphaBot",
            project_path=str(alpha_dir),
            owner_id="admin",
        )
        c_self = await self.database.create_chat(
            title="Core Reloc",
            kind="project",
            project_name="Antigravity_Hub",
            project_path=str(self.hub_dir),
            owner_id="admin",
        )
        up_dir = self.config.resolve_chat_uploads_dir(c_alpha, owner_username="admin")
        up_dir.mkdir(parents=True, exist_ok=True)
        att_file = up_dir / "spec.md"
        att_file.write_text("# Spec OK\n", encoding="utf-8")
        await self.database.add_message(
            c_alpha["id"],
            role="user",
            content="Прикрепляю ТЗ",
            attachments=[{"name": "spec.md", "disk_path": str(att_file), "mime_type": "text/markdown"}],
        )
        scope_alpha = self.database.build_project_memory_scope_key("AlphaBot", owner_id="admin", is_shared=False)
        await self.database.save_project_memory(
            scope_alpha,
            owner_id="admin",
            project_name="AlphaBot",
            extra_dirs=[str(beta_dir)],
        )

        # Seed a HostServiceManager record referencing paths inside `self.hub_dir`
        svc_log = self.hub_dir / "data" / "host_services" / "logs" / "alpha_bot.log"
        svc_log.parent.mkdir(parents=True, exist_ok=True)
        svc_log.write_text("ready\n", encoding="utf-8")
        self._isolated_hsm._loaded_data_dir = (self.hub_dir / "data").resolve()
        self._isolated_hsm._services["alphabot::bot"] = {
            "project_name": "AlphaBot",
            "project_dir": str(alpha_dir),
            "name": "bot",
            "command": f'"{self.hub_dir / ".venv" / "Scripts" / "python.exe"}" bot.py',
            "workdir": str(alpha_dir),
            "port": 0,
            "pid": 0,
            "create_time_ft": 0,
            "log_path": str(svc_log),
            "status": "stopped",
        }
        self._isolated_hsm._save_locked()

        # Physically move `Antigravity_Hub` to a brand new location (`DriveE_Sim/Portable/Antigravity_Hub`)
        moved_hub = self.tmp_root / "DriveE_Sim" / "Portable" / "Antigravity_Hub"
        moved_hub.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.hub_dir), str(moved_hub))
        self.hub_dir = moved_hub

        relocated_hsm = self.host_service_manager.HostServiceManager()
        self._isolated_hsm = relocated_hsm
        self._apply_hub_patches(moved_hub, relocated_hsm)

        await self.database.init_db()

        # Verify DB & registry paths were atomically rewritten to `moved_hub`
        c_alpha_after = await self.database.get_chat(c_alpha["id"])
        c_self_after = await self.database.get_chat(c_self["id"])
        self.assertEqual(
            Path(c_alpha_after["project_path"]).resolve(),
            (moved_hub / "users" / "admin" / "projects" / "AlphaBot").resolve(),
        )
        self.assertEqual(Path(c_self_after["project_path"]).resolve(), moved_hub.resolve())

        sp_after = await self.database.get_shared_project_by_name("SharedCRM")
        self.assertEqual(
            Path(sp_after["path"]).resolve(),
            (moved_hub / "shared_projects" / "SharedCRM").resolve(),
        )
        garik_after = await self.database.get_user_by_id(self.garik["id"])
        self.assertEqual(
            Path(garik_after["workspace_dir"]).resolve(),
            (moved_hub / "users" / "garik" / "projects").resolve(),
        )
        mem_alpha_after = await self.database.get_project_memory(
            scope_alpha, project_name="AlphaBot", owner_id="admin"
        )
        self.assertEqual(
            [Path(p).resolve() for p in mem_alpha_after["extra_dirs"]],
            [(moved_hub / "users" / "admin" / "projects" / "BetaLib").resolve()],
        )
        msgs_after = await self.database.get_messages(c_alpha["id"])
        relocated_disk_path = Path(msgs_after[0]["attachments"][0]["disk_path"]).resolve()
        self.assertTrue(relocated_disk_path.is_file())
        self.assertTrue(str(relocated_disk_path).startswith(str(moved_hub.resolve())))

        with relocated_hsm._lock:
            relocated_hsm._ensure_loaded_locked()
            svc_rec = relocated_hsm._services["alphabot::bot"]
        self.assertEqual(
            Path(svc_rec["workdir"]).resolve(),
            (moved_hub / "users" / "admin" / "projects" / "AlphaBot").resolve(),
        )
        self.assertEqual(
            Path(svc_rec["project_dir"]).resolve(),
            (moved_hub / "users" / "admin" / "projects" / "AlphaBot").resolve(),
        )
        self.assertIn(str(moved_hub / ".venv"), svc_rec["command"])

    async def test_04_v96_to_v97_startup_migration_and_sibling_consolidation(self) -> None:
        # Part A: Seed legacy v96 layout (`data/shared_projects`, `data/workspaces/<uid>`, `data/uploads/<cid>`)
        leg_sp = self.hub_dir / "data" / "shared_projects" / "LegacyShared"
        leg_sp.mkdir(parents=True, exist_ok=True)
        (leg_sp / "shared_app.py").write_text("print('legacy_shared')\n", encoding="utf-8")
        sp_created = await self.database.create_shared_project(name="LegacyShared", execution_mode="docker")
        # Remove empty v97 folder and point DB row to legacy `data/shared_projects/LegacyShared`
        shutil.rmtree(Path(sp_created["path"]), ignore_errors=True)
        import aiosqlite

        async with aiosqlite.connect(self.database.DB_PATH) as db:
            await db.execute(
                "UPDATE shared_projects SET path = ? WHERE id = ?;",
                (str(leg_sp), sp_created["id"]),
            )
            await db.commit()
        c_leg_sp = await self.database.create_chat(
            title="Legacy Shared Chat",
            kind="project",
            project_name="LegacyShared",
            project_path=str(leg_sp),
            owner_id="admin",
        )

        leg_ws_proj = self.hub_dir / "data" / "workspaces" / self.garik["id"] / "GarikTool"
        leg_ws_proj.mkdir(parents=True, exist_ok=True)
        (leg_ws_proj / "tool.py").write_text("print('garik_tool')\n", encoding="utf-8")
        c_garik_p = await self.database.create_chat(
            title="Garik Tool Chat",
            kind="project",
            project_name="GarikTool",
            project_path=str(leg_ws_proj),
            owner_id=self.garik["id"],
        )

        leg_up = self.hub_dir / "data" / "uploads" / c_garik_p["id"]
        leg_up.mkdir(parents=True, exist_ok=True)
        leg_doc = leg_up / "notes.txt"
        leg_doc.write_text("garik upload payload\n", encoding="utf-8")
        await self.database.add_message(
            c_garik_p["id"],
            role="user",
            content="Файл",
            attachments=[{"name": "notes.txt", "disk_path": str(leg_doc), "mime_type": "text/plain"}],
        )

        orphan_up = self.hub_dir / "data" / "uploads" / "orphan_untracked_99"
        orphan_up.mkdir(parents=True, exist_ok=True)
        (orphan_up / "keep.txt").write_text("orphan stays\n", encoding="utf-8")

        # Reset v97 migration marker so `init_db()` runs `_migrate_v96_to_v97_layout`
        async with aiosqlite.connect(self.database.DB_PATH) as db:
            await db.execute("DELETE FROM hub_meta WHERE key = 'v97_user_centric_migrated';")
            await db.commit()

        await self.database.init_db()

        self.assertTrue((self.hub_dir / "shared_projects" / "LegacyShared" / "shared_app.py").is_file())
        self.assertTrue(
            (self.hub_dir / "users" / "garik" / "projects" / "GarikTool" / "tool.py").is_file()
        )
        self.assertTrue(
            (self.hub_dir / "users" / "garik" / "uploads" / c_garik_p["id"] / "notes.txt").is_file()
        )
        self.assertTrue((orphan_up / "keep.txt").is_file())

        c_sp_migrated = await self.database.get_chat(c_leg_sp["id"])
        c_gp_migrated = await self.database.get_chat(c_garik_p["id"])
        self.assertEqual(
            Path(c_sp_migrated["project_path"]).resolve(),
            (self.hub_dir / "shared_projects" / "LegacyShared").resolve(),
        )
        self.assertEqual(
            Path(c_gp_migrated["project_path"]).resolve(),
            (self.hub_dir / "users" / "garik" / "projects" / "GarikTool").resolve(),
        )

        # Part B: Sibling project consolidation (`ParentDir/MyTeleBot` -> `users/admin/projects/MyTeleBot`)
        sib_bot = self.parent_dir / "MyTeleBot"
        (sib_bot / ".git" / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (sib_bot / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (sib_bot / ".venv" / "Lib").mkdir(parents=True, exist_ok=True)
        (sib_bot / ".venv" / "Lib" / "stale.py").write_text("# stale venv\n", encoding="utf-8")
        (sib_bot / ".env").write_text("BOT_TOKEN=xyz\n", encoding="utf-8")
        (sib_bot / "bot.py").write_text("print('sibling_bot_v1')\n", encoding="utf-8")

        non_proj = self.parent_dir / "VacationPhotos"
        non_proj.mkdir(parents=True, exist_ok=True)
        (non_proj / "beach.jpg").write_bytes(b"jpeg")

        c_sib = await self.database.create_chat(
            title="TeleBot Old Path Chat",
            kind="project",
            project_name="MyTeleBot",
            project_path=str(sib_bot),
            owner_id="admin",
        )

        plan = self.project_migrator.discover_desktop_migration_plan(allow_temp_subdirs=True)
        by_name = {p["project_name"]: p for p in plan["projects"]}
        self.assertNotIn("Antigravity_Hub", by_name)
        self.assertNotIn("VacationPhotos", by_name)
        self.assertIn("MyTeleBot", by_name)
        self.assertTrue(by_name["MyTeleBot"]["is_legacy_sibling"])
        self.assertEqual(by_name["MyTeleBot"]["source_origin"], "legacy_sibling")
        self.assertEqual(by_name["MyTeleBot"]["action"], "copy_to_hub")

        exec_res = await self.project_migrator.execute_desktop_migration(
            selected_projects=["MyTeleBot"],
            move_legacy_siblings=True,
            import_chats=False,
            rebuild_venv=False,
            allow_temp_subdirs=True,
        )
        self.assertTrue(exec_res["ok"])
        self.assertEqual(exec_res["moved_legacy_siblings_count"], 1)
        dst_bot = self.hub_dir / "users" / "admin" / "projects" / "MyTeleBot"
        self.assertTrue((dst_bot / "bot.py").is_file())
        self.assertTrue((dst_bot / ".env").is_file())
        self.assertTrue((dst_bot / ".git" / "HEAD").is_file())
        self.assertFalse((dst_bot / ".venv").exists())
        self.assertFalse(sib_bot.exists())

        c_sib_after = await self.database.get_chat(c_sib["id"])
        self.assertEqual(Path(c_sib_after["project_path"]).resolve(), dst_bot.resolve())

        # Windows `PermissionError` fallback during sibling move -> clean copy + Russian warning
        locked_sib = self.parent_dir / "LockedWorker"
        locked_sib.mkdir(parents=True, exist_ok=True)
        (locked_sib / "worker.py").write_text("print('locked')\n", encoding="utf-8")
        with (
            patch.object(shutil, "move", side_effect=PermissionError("WinError 32")),
            patch.object(shutil, "rmtree", side_effect=PermissionError("WinError 32")),
        ):
            exec_locked = await self.project_migrator.execute_desktop_migration(
                selected_projects=["LockedWorker"],
                move_legacy_siblings=True,
                import_chats=False,
                rebuild_venv=False,
                allow_temp_subdirs=True,
            )
        self.assertTrue(exec_locked["ok"])
        self.assertTrue(
            (self.hub_dir / "users" / "admin" / "projects" / "LockedWorker" / "worker.py").is_file()
        )
        res_locked = next(r for r in exec_locked["results"] if r["project_name"] == "LockedWorker")
        self.assertIn("закройте процесс в старой папке", res_locked.get("warning", ""))

    async def test_05_v97_launcher_and_frontend_hygiene(self) -> None:
        checked_files = (
            "app/config.py",
            "app/brain_reader.py",
            "app/workspace_mcp.py",
            "app/agy_engine.py",
            "app/codex_engine.py",
            "app/claude_engine.py",
            "app/sandbox_service.py",
            "app/host_service_manager.py",
            "app/database.py",
            "app/project_migrator.py",
            "app/main.py",
            "tools/migrate_projects.py",
            "start_hub.bat",
            "start_hub_hidden.pyw",
            "pyproject.toml",
            ".gitignore",
            "app/static/index.html",
            "app/static/app.js",
            "app/static/styles.css",
            "app/static/sw.js",
        )
        for rel in checked_files:
            p = ROOT_DIR / rel
            raw = p.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM found in {rel}")
            raw.decode("utf-8")

        bat_txt = (ROOT_DIR / "start_hub.bat").read_text(encoding="utf-8")
        pyw_txt = (ROOT_DIR / "start_hub_hidden.pyw").read_text(encoding="utf-8")
        self.assertIn("import uvicorn, fastapi, aiosqlite", bat_txt)
        self.assertIn("CUDA_VISIBLE_DEVICES=", bat_txt)
        self.assertIn("_python_runs_cleanly", pyw_txt)

        sw_txt = (ROOT_DIR / "app/static/sw.js").read_text(encoding="utf-8")
        html_txt = (ROOT_DIR / "app/static/index.html").read_text(encoding="utf-8")
        js_txt = (ROOT_DIR / "app/static/app.js").read_text(encoding="utf-8")
        css_txt = (ROOT_DIR / "app/static/styles.css").read_text(encoding="utf-8")

        m_sw_cache = re.search(r"agy-hub-v(\d+)", sw_txt)
        self.assertIsNotNone(m_sw_cache)
        self.assertGreaterEqual(int(m_sw_cache.group(1)), 97)
        m_sw_css = re.search(r"/static/styles\.css\?v=(\d+)", sw_txt)
        m_sw_js = re.search(r"/static/app\.js\?v=(\d+)", sw_txt)
        m_html_css = re.search(r"/static/styles\.css\?v=(\d+)", html_txt)
        m_html_js = re.search(r"/static/app\.js\?v=(\d+)", html_txt)
        for m in (m_sw_css, m_sw_js, m_html_css, m_html_js):
            self.assertIsNotNone(m)
            self.assertGreaterEqual(int(m.group(1)), 97)
        self.assertIn('id="dmMoveSiblingsChk"', html_txt)
        self.assertIn('id="dmTargetRootPath"', html_txt)
        self.assertIn("📦 Собрать", html_txt)
        self.assertIn("⚙️ Ядро", js_txt)

        forbidden_props = (
            "transform",
            "filter",
            "backdrop-filter",
            "perspective",
            "contain",
            "container-type",
        )
        for m in re.finditer(r"main\.workspace\s*\{([^}]*)\}", css_txt, flags=re.DOTALL):
            block = m.group(1)
            for prop in forbidden_props:
                self.assertIsNone(
                    re.search(rf"(?<![\w-]){re.escape(prop)}\s*:", block),
                    f"Forbidden CSS property `{prop}` found inside `main.workspace`",
                )


if __name__ == "__main__":
    unittest.main()
