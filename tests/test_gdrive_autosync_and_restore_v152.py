"""Regression suite v152 (TZ3): Event-driven background auto-sync (`0% CPU` idle, quiet period, ACTIVE_RUNS guard,
4h periodic safety sweep), non-destructive Restore Catalog & Safe Restore Executor (Backup -> Clean PC -> Restore,
SQLite merge + `project_path` rebinding, encrypted `.env` vault decryption, path traversal & `.git/` & self-Hub shield),
WS `gdrive_sync_status` live UI updates, and Chromium geometry gate (`1440x900`, `390x844`, `320x568`).

Runs strictly in temporary directories and temporary SQLite databases with `0` real network calls and never touches
live `data/hub.db`, `users/`, `shared_projects/`, or running services.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = _TESTS_DIR.parent
for _p in (str(ROOT_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_frontend_contracts import STATIC, ShellParser, run_node
from test_gdrive_sync_core_v151 import FakeGoogleDriveServer, _asgi_request
from test_ui_workspace_minimal_v142 import run_workspace_chromium


class FakeGoogleDriveServerV152(FakeGoogleDriveServer):
    """Extends FakeGoogleDriveServer with full read/list/download (`?alt=media`, `size`, `modifiedTime`) support."""

    def __init__(self, *, email: str = "restore.owner@example.com") -> None:
        super().__init__(email=email)
        self.now_iso = "2026-11-20T09:00:00Z"

    def inject_file_under_path(self, logical_path: str, content: bytes, *, raw_name: Optional[str] = None) -> str:
        """Directly insert an arbitrary file (including crafted unsafe names) under the root mirror folder."""
        from app.gdrive_service import GOOGLE_DRIVE_FOLDER_MIME

        root_id = next(
            (
                fid
                for fid, rec in self.files.items()
                if rec.get("mimeType") == GOOGLE_DRIVE_FOLDER_MIME
                and not rec.get("trashed")
                and (rec.get("parents") == ["root"] or not rec.get("parents"))
            ),
            None,
        )
        assert root_id is not None, "Root folder must exist before injecting remote file"
        parts = [seg for seg in logical_path.split("/") if seg]
        parent_id = root_id
        for seg in parts[:-1]:
            found = next(
                (
                    fid
                    for fid, rec in self.files.items()
                    if rec.get("mimeType") == GOOGLE_DRIVE_FOLDER_MIME
                    and not rec.get("trashed")
                    and rec.get("name") == seg
                    and parent_id in (rec.get("parents") or [])
                ),
                None,
            )
            if not found:
                found = self._next_id("dir")
                self.files[found] = {
                    "id": found,
                    "name": seg,
                    "parents": [parent_id],
                    "mimeType": GOOGLE_DRIVE_FOLDER_MIME,
                    "trashed": False,
                    "content": b"",
                    "modifiedTime": self.now_iso,
                }
            parent_id = found
        fid = self._next_id("file")
        fname = raw_name if raw_name is not None else parts[-1]
        self.files[fid] = {
            "id": fid,
            "name": fname,
            "parents": [parent_id],
            "mimeType": "application/octet-stream",
            "trashed": False,
            "content": bytes(content),
            "size": str(len(content)),
            "modifiedTime": self.now_iso,
            "update_count": 0,
        }
        return fid

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> Any:
        from app.gdrive_service import GOOGLE_DRIVE_FOLDER_MIME, HttpResponse

        m = method.upper()
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # Auth header check before custom read handlers
        auth_hdr = headers.get("Authorization", "")
        if "/drive/v3/" in path and self.require_token and auth_hdr != f"Bearer {self.require_token}":
            self.calls.append({"method": m, "url": url, "headers": dict(headers), "body_len": len(body or b"")})
            return HttpResponse(401, {"content-type": "application/json"}, b'{"error":{"message":"Invalid Credentials"}}')

        # Media download: GET /drive/v3/files/{id}?alt=media
        if m == "GET" and path.startswith("/drive/v3/files/") and (qs.get("alt") or [""])[0] == "media":
            self.calls.append({"method": m, "url": url, "headers": dict(headers), "body_len": 0})
            fid = urllib.parse.unquote(path.split("/drive/v3/files/", 1)[1])
            rec = self.files.get(fid)
            if not rec or rec.get("trashed"):
                return HttpResponse(404, {"content-type": "application/json"}, b'{"error":{"message":"Not Found"}}')
            return HttpResponse(200, {"content-type": rec.get("mimeType") or "application/octet-stream"}, bytes(rec.get("content") or b""))

        # Enhanced list endpoint: supports `mimeType != folder`, `size`, `modifiedTime`, and listing without `name =`
        if m == "GET" and path == "/drive/v3/files":
            self.calls.append({"method": m, "url": url, "headers": dict(headers), "body_len": 0})
            q = (qs.get("q") or [""])[0]
            name_m = re.search(r"name = '((?:\\'|\\\\|[^'])*)'", q)
            parent_m = re.search(r"'([^']+)' in parents", q)
            want_name = name_m.group(1).replace("\\'", "'").replace("\\\\", "\\") if name_m else None
            want_parent = parent_m.group(1) if parent_m else None
            want_folder_only = f"mimeType = '{GOOGLE_DRIVE_FOLDER_MIME}'" in q
            want_non_folder = f"mimeType != '{GOOGLE_DRIVE_FOLDER_MIME}'" in q

            matched = []
            for rec in self.files.values():
                if rec.get("trashed"):
                    continue
                is_dir = rec.get("mimeType") == GOOGLE_DRIVE_FOLDER_MIME
                if want_folder_only and not is_dir:
                    continue
                if want_non_folder and is_dir:
                    continue
                if want_name is not None and rec.get("name") != want_name:
                    continue
                if want_parent is not None and want_parent not in (rec.get("parents") or []):
                    continue
                matched.append(
                    {
                        "id": rec["id"],
                        "name": rec["name"],
                        "mimeType": rec.get("mimeType") or "application/octet-stream",
                        "size": str(len(rec.get("content") or b"")),
                        "modifiedTime": rec.get("modifiedTime") or self.now_iso,
                        "trashed": bool(rec.get("trashed")),
                    }
                )
            return HttpResponse(
                200,
                {"content-type": "application/json"},
                json.dumps({"files": matched}).encode("utf-8"),
            )

        resp = super().__call__(method, url, headers, body, timeout)
        # Stamp modifiedTime on newly created/updated files
        for rec in self.files.values():
            rec.setdefault("modifiedTime", self.now_iso)
        return resp


def _make_isolated_env(base_dir: Path) -> dict[str, Path]:
    iso_root = base_dir.resolve() / "iso_hub"
    data_dir = iso_root / "data"
    users_dir = iso_root / "users"
    projects_root = users_dir / "admin" / "projects"
    shared_dir = iso_root / "shared_projects"
    shared_uploads = shared_dir / "_uploads"
    shared_ctx = shared_dir / "_context"
    cli_homes = data_dir / "cli_homes"
    uploads_dir = data_dir / "uploads"
    db_path = data_dir / "test_v152.db"
    for d in (
        iso_root,
        iso_root / "app",
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
    return {
        "BASE_DIR": iso_root,
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
    }


def _apply_env_patch(patch_map: dict[str, Path]) -> list[tuple[Any, str, Any]]:
    from app import brain_reader, config, database, main as main_mod

    saved: list[tuple[Any, str, Any]] = []
    for mod in (config, database, brain_reader, main_mod):
        for attr, val in patch_map.items():
            if hasattr(mod, attr):
                saved.append((mod, attr, getattr(mod, attr)))
                setattr(mod, attr, val)
    return saved


def _restore_env_patch(saved: list[tuple[Any, str, Any]]) -> None:
    for mod, attr, orig in reversed(saved):
        setattr(mod, attr, orig)


# ---------------------------------------------------------------------------
# 1. Path Validation Unit Tests
# ---------------------------------------------------------------------------


def test_restore_rel_path_security_validator() -> None:
    from app import gdrive_service

    # Safe source code paths
    assert gdrive_service._validate_restore_rel_path("src/main.py", allow_secrets=False) == "src/main.py"
    assert gdrive_service._validate_restore_rel_path(".env.example", allow_secrets=False) == ".env.example"

    # Unsafe traversal / absolute / Windows drive / `.git/` / secrets in code mode
    for bad in (
        "../escaped.txt",
        "src/../../escaped.txt",
        "/etc/passwd",
        "\\Windows\\System32\\cmd.exe",
        "C:/Windows/win.ini",
        "C:\\evil.txt",
        ".git/config",
        "sub/.git/HEAD",
        ".env",
        ".env.local",
        ".secrets/id_rsa",
        "data/hub.db",
        "hub.db-wal",
        "hub_secret.key",
        "CON.txt",
        "app\x00.py",
        ".agents/rules/hub_project.md",
    ):
        assert gdrive_service._validate_restore_rel_path(bad, allow_secrets=False) is None, f"Should block {bad}"

    # Self-hub additionally blocks top-level `users/` and `shared_projects/`
    assert gdrive_service._validate_restore_rel_path("users/admin/a.py", allow_secrets=False, is_self_hub=True) is None
    assert gdrive_service._validate_restore_rel_path("shared_projects/X/a.py", allow_secrets=False, is_self_hub=True) is None

    # Safe vault paths vs unsafe vault paths
    assert gdrive_service._validate_restore_rel_path(".env", allow_secrets=True) == ".env"
    assert gdrive_service._validate_restore_rel_path(".secrets/api.pem", allow_secrets=True) == ".secrets/api.pem"
    for bad_vault in (
        "../.env",
        "/root/.env",
        ".git/config",
        "bot.session",
        "data/.env",
        "src/main.py",
        "hub.db",
    ):
        assert gdrive_service._validate_restore_rel_path(bad_vault, allow_secrets=True) is None, f"Vault should block {bad_vault}"


# ---------------------------------------------------------------------------
# 2. Deterministic Auto-Sync Scheduler & End-to-End Backup -> Clean PC -> Restore
# ---------------------------------------------------------------------------


class GDriveAutoSyncAndRestoreV152Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app import agy_engine, auth, brain_reader, config, database, gdrive_service, main as main_mod

        self.agy_engine = agy_engine
        self.auth = auth
        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.gdrive_service = gdrive_service
        self.main_mod = main_mod

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v152_gdrive_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()
        self.pc1_map = _make_isolated_env(self.tmp_root / "pc1")
        (self.pc1_map["BASE_DIR"] / "app" / "hub_core.py").write_bytes(b"OLD_CLOUD_HUB_CORE = 1\n")

        await database.close_db_pool()
        self._saved_attrs = _apply_env_patch(self.pc1_map)
        self.fake_drive = FakeGoogleDriveServerV152(email="cloud.admin@example.com")
        gdrive_service.set_default_transport(self.fake_drive)
        gdrive_service.clear_gdrive_dirty_state(None)
        brain_reader.invalidate_projects_list_cache()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        self.agy_engine.ACTIVE_RUNS.clear()
        self.gdrive_service.clear_gdrive_dirty_state(None)
        self.gdrive_service.set_default_transport(None)
        await self.database.close_db_pool()
        _restore_env_patch(self._saved_attrs)
        self.brain_reader.invalidate_projects_list_cache()
        self._tmp.cleanup()

    async def _connect_admin_gdrive(
        self,
        *,
        is_enabled: bool = True,
        sync_self_hub: bool = False,
        include_env_vault: bool = False,
        vault_passphrase: str = "",
    ) -> None:
        await self.database.save_gdrive_config(
            "admin",
            oauth_client={
                "client_id": "123456-v152.apps.googleusercontent.com",
                "client_secret": "GOCSPX-v152-secret",
                "auth_uri": self.gdrive_service.GOOGLE_AUTH_URL,
                "token_uri": self.gdrive_service.GOOGLE_TOKEN_URL,
            },
            tokens={
                "access_token": "tok_v152_valid",
                "refresh_token": "rtok_v152_valid",
                "expires_at": int(datetime.now(timezone.utc).timestamp()) + 7200,
                "scope": self.gdrive_service.GOOGLE_DRIVE_SCOPE,
            },
            account_email=self.fake_drive.email,
            is_enabled=is_enabled,
            sync_self_hub=sync_self_hub,
            include_env_vault=include_env_vault,
            vault_passphrase=vault_passphrase if vault_passphrase else None,
        )
        self.fake_drive.require_token = "tok_v152_valid"

    async def test_autosync_tick_quiet_period_active_runs_guard_and_periodic_sweep(self) -> None:
        proj = self.pc1_map["PROJECTS_ROOT"] / "AutoSyncProj"
        proj.mkdir(parents=True)
        (proj / "worker.py").write_bytes(b"VAL = 1\n")
        self.brain_reader.invalidate_projects_list_cache()

        await self._connect_admin_gdrive(is_enabled=True, sync_self_hub=False)
        admin_user = await self.database.get_user_by_id("admin")
        assert admin_user is not None

        # Initial baseline sync so `last_sync_at` is set to now
        ws_events: list[dict[str, Any]] = []
        await self.gdrive_service.sync_user_to_gdrive(
            admin_user,
            transport=self.fake_drive,
            trigger="manual",
            ws_notifier=ws_events.append,
        )
        self.assertGreaterEqual(len(ws_events), 2)
        self.assertTrue(all(e["type"] == "gdrive_sync_status" for e in ws_events))
        self.assertTrue(all(e["state"] == "running" and e["sync_in_progress"] for e in ws_events[:-1]))
        self.assertEqual(ws_events[-1]["state"], "ok")
        self.assertFalse(ws_events[-1]["sync_in_progress"])
        ws_events.clear()

        # 1. Idle tick (no dirty change, < 4h since last_sync_at) -> 0 network calls!
        calls_before_idle = len(self.fake_drive.calls)
        idle_res = await self.gdrive_service.run_gdrive_auto_sync_tick(
            now_mono=1000.0,
            now_utc=datetime.now(timezone.utc) + timedelta(minutes=5),
            transport=self.fake_drive,
            ws_notifier=ws_events.append,
        )
        self.assertFalse(idle_res["ran"])
        self.assertEqual(idle_res["skipped_users"].get("admin"), "idle")
        self.assertEqual(len(self.fake_drive.calls), calls_before_idle)

        # 2. Local edit + notify_gdrive_change -> tick inside 45s Quiet Period MUST WAIT
        (proj / "worker.py").write_bytes(b"VAL = 2\n")
        self.gdrive_service.notify_gdrive_change("admin", reason="turn_done", now_mono=2000.0)
        calls_before_quiet = len(self.fake_drive.calls)
        quiet_wait_res = await self.gdrive_service.run_gdrive_auto_sync_tick(
            quiet_period_sec=45.0,
            now_mono=2025.0,
            transport=self.fake_drive,
            ws_notifier=ws_events.append,
        )
        self.assertFalse(quiet_wait_res["ran"])
        self.assertEqual(quiet_wait_res["skipped_users"].get("admin"), "quiet_period_pending")
        self.assertEqual(len(self.fake_drive.calls), calls_before_quiet)

        # 3. Quiet Period elapsed (50s >= 45s), BUT AI generation is active in `ACTIVE_RUNS` -> MUST BLOCK!
        self.agy_engine.ACTIVE_RUNS["chat_busy_1"] = object()  # type: ignore[assignment]
        try:
            busy_res = await self.gdrive_service.run_gdrive_auto_sync_tick(
                quiet_period_sec=45.0,
                now_mono=2050.0,
                transport=self.fake_drive,
                ws_notifier=ws_events.append,
            )
            self.assertFalse(busy_res["ran"])
            self.assertEqual(busy_res["skipped_reason"], "active_runs")
            self.assertEqual(len(self.fake_drive.calls), calls_before_quiet)
        finally:
            self.agy_engine.ACTIVE_RUNS.clear()

        # 4. Concurrent per-user lock held -> silently skips without error
        lock = self.gdrive_service.get_user_sync_lock("admin")
        async with lock:
            locked_res = await self.gdrive_service.run_gdrive_auto_sync_tick(
                quiet_period_sec=45.0,
                now_mono=2055.0,
                transport=self.fake_drive,
                ws_notifier=ws_events.append,
            )
            self.assertFalse(locked_res["ran"])
            self.assertEqual(locked_res["skipped_users"].get("admin"), "sync_in_progress")

        # 5. Now AI is idle & quiet period passed -> delta upload runs & emits WS `gdrive_sync_status`!
        ok_res = await self.gdrive_service.run_gdrive_auto_sync_tick(
            quiet_period_sec=45.0,
            now_mono=2060.0,
            transport=self.fake_drive,
            ws_notifier=ws_events.append,
        )
        self.assertTrue(ok_res["ran"])
        self.assertEqual(ok_res["synced_users"], ["admin"])
        self.assertEqual(ok_res["results"][0]["trigger"], "auto_quiet")
        self.assertEqual(
            self.fake_drive.active_files_by_path()["Проекты/AutoSyncProj/code/worker.py"]["content"],
            b"VAL = 2\n",
        )
        self.assertGreaterEqual(len(ws_events), 2)
        self.assertTrue(all(e["state"] == "running" and e["sync_in_progress"] for e in ws_events[:-1]))
        self.assertEqual(ws_events[-1]["state"], "ok")
        self.assertFalse(ws_events[-1]["sync_in_progress"])
        self.assertIsNone(self.gdrive_service.get_gdrive_dirty_state("admin"))
        ws_events.clear()

        # 6. External IDE edit (no `notify_gdrive_change`) -> 4-hour periodic safety sweep picks it up!
        (proj / "worker.py").write_bytes(b"VAL = 3_EXTERNAL_IDE\n")
        periodic_res = await self.gdrive_service.run_gdrive_auto_sync_tick(
            quiet_period_sec=45.0,
            safety_interval_sec=4 * 3600.0,
            now_mono=2100.0,
            now_utc=datetime.now(timezone.utc) + timedelta(hours=4, minutes=2),
            transport=self.fake_drive,
            ws_notifier=ws_events.append,
        )
        self.assertTrue(periodic_res["ran"])
        self.assertEqual(periodic_res["results"][0]["trigger"], "auto_periodic")
        self.assertEqual(
            self.fake_drive.active_files_by_path()["Проекты/AutoSyncProj/code/worker.py"]["content"],
            b"VAL = 3_EXTERNAL_IDE\n",
        )

        # 7. Verify REST mutation routes trigger `notify_gdrive_change`
        app = self.main_mod.app
        cname = self.auth.COOKIE_NAME
        admin_cookie = f"{cname}={self.auth.create_user_session_token('admin', 1)}"
        ch = await self.database.create_chat(title="Hook Chat", kind="chat", owner_id="admin")
        msg = await self.database.add_message(ch["id"], role="user", content="hello", user_id="admin")
        self.gdrive_service.clear_gdrive_dirty_state("admin")
        st_bm, _, _ = await _asgi_request(
            app,
            "PATCH",
            f"/api/chats/{ch['id']}/messages/{msg['id']}/bookmark",
            cookie_header=admin_cookie,
            json_body={"bookmarked": True},
        )
        self.assertEqual(st_bm, 200)
        dirty = self.gdrive_service.get_gdrive_dirty_state("admin")
        self.assertIsNotNone(dirty)
        self.assertIn("bookmark", dirty["reasons"])

    async def test_backup_clean_pc_restore_full_end_to_end_and_security_shields(self) -> None:
        app = self.main_mod.app
        cname = self.auth.COOKIE_NAME
        admin_cookie = f"{cname}={self.auth.create_user_session_token('admin', 1)}"
        vault_pass = "Ultra-Safe-Restore-Pass!2026"

        # ------------------------------------------------------------------
        # PHASE 1: Seed PC 1 (code, `.env`, `.secrets/`, chats, bookmark, folder, Passport, self-Hub) & Sync
        # ------------------------------------------------------------------
        proj_pc1 = self.pc1_map["PROJECTS_ROOT"] / "AlphaApp"
        (proj_pc1 / "src").mkdir(parents=True)
        (proj_pc1 / ".secrets").mkdir(parents=True)
        (proj_pc1 / "src" / "main.py").write_bytes(b"print('alpha v1')\n")
        (proj_pc1 / "README.md").write_bytes(b"# AlphaApp\nClean docs.\n")
        (proj_pc1 / ".env").write_bytes(b"API_TOKEN=secret_alpha_2026\nDB_URL=postgres://localhost/a\n")
        (proj_pc1 / ".secrets" / "service.pem").write_bytes(b"-----BEGIN KEY-----\nALPHA_PEM\n-----END KEY-----\n")
        self.brain_reader.invalidate_projects_list_cache()

        reg_chat = await self.database.create_chat(
            title="План запуска Alpha",
            kind="chat",
            owner_id="admin",
            folder="Стратегия",
        )
        await self.database.add_message(reg_chat["id"], role="user", content="Составь план запуска", user_id="admin")
        bm_msg = await self.database.add_message(
            reg_chat["id"], role="assistant", content="Ключевой вывод: три этапа релиза.", user_id="admin"
        )
        await self.database.set_message_bookmarked(reg_chat["id"], int(bm_msg["id"]), True)
        await self.database.set_message_custom_title(reg_chat["id"], int(bm_msg["id"]), "Ключевой вывод")

        proj_chat = await self.database.create_chat(
            title="Разработка AlphaApp",
            kind="project",
            project_name="AlphaApp",
            project_path=str(proj_pc1),
            owner_id="admin",
        )
        await self.database.add_message(proj_chat["id"], role="user", content="Настрой main.py", user_id="admin")

        scope_key = self.database.build_project_memory_scope_key("AlphaApp", owner_id="admin", is_shared=False)
        await self.database.save_project_memory(
            scope_key,
            owner_id="admin",
            project_name="AlphaApp",
            passport_overview="Основной сервис AlphaApp",
            passport_rules="- Всегда проверять тесты",
            passport_file_map="- src/main.py",
        )

        await self._connect_admin_gdrive(
            is_enabled=True,
            sync_self_hub=True,
            include_env_vault=True,
            vault_passphrase=vault_pass,
        )
        st_sync, d_sync, _ = await _asgi_request(app, "POST", "/api/gdrive/sync", cookie_header=admin_cookie)
        self.assertEqual(st_sync, 200, d_sync)
        self.assertTrue(d_sync["summary"]["vault_included"])

        # Plant malicious path-traversal and `.git/` files in remote cloud Drive under AlphaApp/code/
        self.fake_drive.inject_file_under_path(
            "Проекты/AlphaApp/code/evil_file",
            b"SHOULD_NEVER_BE_WRITTEN",
            raw_name="../escaped_outside.txt",
        )
        self.fake_drive.inject_file_under_path(
            "Проекты/AlphaApp/code/.git/config",
            b"[core]\nmalicious = true\n",
        )

        # ------------------------------------------------------------------
        # PHASE 2: Switch to Clean PC 2 (completely empty projects dir & fresh SQLite DB)
        # ------------------------------------------------------------------
        await self.database.close_db_pool()
        _restore_env_patch(self._saved_attrs)

        pc2_map = _make_isolated_env(self.tmp_root / "pc2_clean")
        (pc2_map["BASE_DIR"] / "app" / "hub_core.py").write_bytes(b"LIVE_PC2_HUB_DO_NOT_OVERWRITE\n")
        self._saved_attrs = _apply_env_patch(pc2_map)
        self.brain_reader.invalidate_projects_list_cache()
        await self.database.init_db()

        # Connect PC 2 to the same Google Drive account (notice root_folder_id is empty -> discovered by name!)
        await self._connect_admin_gdrive(is_enabled=False, sync_self_hub=True, include_env_vault=False)

        # RBAC check: non-admin user gets 403 on restore endpoints
        guest = await self.database.create_user(
            username="guest_v152",
            password="Password123!",
            display_name="Guest",
            max_access_mode="write_files",
        )
        guest_cookie = f"{cname}={self.auth.create_user_session_token(guest['id'], int(guest.get('session_version') or 1))}"
        st_g_cat, _, _ = await _asgi_request(app, "GET", "/api/gdrive/restore/catalog", cookie_header=guest_cookie)
        st_g_exec, _, _ = await _asgi_request(
            app,
            "POST",
            "/api/gdrive/restore/execute",
            cookie_header=guest_cookie,
            json_body={"restore_dialogs": True},
        )
        self.assertEqual(st_g_cat, 403)
        self.assertEqual(st_g_exec, 403)

        # 1. Inspect Restore Catalog on Clean PC 2
        st_cat, d_cat, _ = await _asgi_request(app, "GET", "/api/gdrive/restore/catalog", cookie_header=admin_cookie)
        self.assertEqual(st_cat, 200, d_cat)
        self.assertTrue(d_cat["has_backup"])
        self.assertTrue(d_cat["manifest_found"])
        self.assertTrue(d_cat["dialogs_backup"]["exists"])
        self.assertEqual(d_cat["dialogs_backup"]["chats_count"], 2)
        self.assertEqual(d_cat["dialogs_backup"]["messages_count"], 3)
        self.assertEqual(d_cat["dialogs_backup"]["folders_count"], 1)
        self.assertEqual(d_cat["dialogs_backup"]["passports_count"], 1)
        self.assertTrue(d_cat["secrets_vault"]["exists"])
        self.assertGreater(d_cat["secrets_vault"]["size_bytes"], 0)
        self.assertEqual(d_cat["secrets_vault"]["files_count"], 2)

        projs_by_name = {p["name"]: p for p in d_cat["projects"]}
        self.assertIn("AlphaApp", projs_by_name)
        self.assertIn("Antigravity_Hub", projs_by_name)
        self.assertFalse(projs_by_name["AlphaApp"]["exists_locally"])
        self.assertTrue(projs_by_name["Antigravity_Hub"]["exists_locally"])
        self.assertTrue(projs_by_name["Antigravity_Hub"]["is_self_hub"])

        # 2. Wrong vault passphrase -> 400 Bad Request, ZERO files created, ZERO DB chats restored!
        st_bad, d_bad, _ = await _asgi_request(
            app,
            "POST",
            "/api/gdrive/restore/execute",
            cookie_header=admin_cookie,
            json_body={
                "restore_dialogs": True,
                "restore_code": True,
                "selected_projects": [],
                "restore_env_vault": True,
                "vault_passphrase": "Wrong-Password-999",
                "overwrite_existing": False,
            },
        )
        self.assertEqual(st_bad, 400, d_bad)
        self.assertFalse((pc2_map["PROJECTS_ROOT"] / "AlphaApp").exists())
        self.assertEqual(
            len(await self.database.list_chats(kind="chat", owner_id="admin", include_context_pct=False)),
            0,
        )

        # 3. Full Restore on Clean PC 2 (with `overwrite_existing=True` to prove self-Hub overwrite shield!)
        st_exec, d_exec, _ = await _asgi_request(
            app,
            "POST",
            "/api/gdrive/restore/execute",
            cookie_header=admin_cookie,
            json_body={
                "restore_dialogs": True,
                "restore_code": True,
                "selected_projects": [],
                "restore_env_vault": True,
                "vault_passphrase": vault_pass,
                "overwrite_existing": True,
            },
        )
        self.assertEqual(st_exec, 200, d_exec)

        # Verify Dialogs, Bookmarks, Folders, Passports & Local `project_path` Rebinding!
        self.assertEqual(d_exec["dialogs"]["chats_added"], 2)
        self.assertEqual(d_exec["dialogs"]["messages_restored"], 3)
        self.assertEqual(d_exec["dialogs"]["passports_restored"], 1)
        self.assertGreaterEqual(d_exec["dialogs"]["rebound_project_paths"], 1)

        restored_reg = await self.database.get_chat(reg_chat["id"])
        assert restored_reg is not None
        self.assertEqual(restored_reg["folder"], "Стратегия")
        restored_msgs = await self.database.get_messages(reg_chat["id"], include_thinking=False)
        self.assertEqual(len(restored_msgs), 2)
        bm_restored = [m for m in restored_msgs if m.get("bookmarked")]
        self.assertEqual(len(bm_restored), 1)
        self.assertEqual(bm_restored[0]["custom_title"], "Ключевой вывод")

        restored_proj_chat = await self.database.get_chat(proj_chat["id"])
        assert restored_proj_chat is not None
        expected_pc2_proj_dir = (pc2_map["PROJECTS_ROOT"] / "AlphaApp").resolve()
        self.assertEqual(Path(restored_proj_chat["project_path"]).resolve(), expected_pc2_proj_dir)
        self.assertNotEqual(str(restored_proj_chat["project_path"]), str(proj_pc1))

        restored_mem = await self.database.get_project_memory(scope_key, project_name="AlphaApp", owner_id="admin")
        self.assertEqual(restored_mem["passport_overview"], "Основной сервис AlphaApp")
        self.assertIn("Всегда проверять тесты", restored_mem["passport_rules"])

        # Verify Clean Source Code & Decrypted `.env` / `.secrets/*`
        self.assertEqual((expected_pc2_proj_dir / "src" / "main.py").read_bytes(), b"print('alpha v1')\n")
        self.assertEqual((expected_pc2_proj_dir / "README.md").read_bytes(), b"# AlphaApp\nClean docs.\n")
        self.assertEqual(
            (expected_pc2_proj_dir / ".env").read_bytes(),
            b"API_TOKEN=secret_alpha_2026\nDB_URL=postgres://localhost/a\n",
        )
        self.assertEqual(
            (expected_pc2_proj_dir / ".secrets" / "service.pem").read_bytes(),
            b"-----BEGIN KEY-----\nALPHA_PEM\n-----END KEY-----\n",
        )
        self.assertEqual(d_exec["vault"]["files_restored"], 2)

        # Verify Path Traversal & `.git/` Attacks Blocked
        self.assertFalse((pc2_map["PROJECTS_ROOT"] / "escaped_outside.txt").exists())
        self.assertFalse((expected_pc2_proj_dir / ".git" / "config").exists())
        self.assertTrue(any("escaped_outside.txt" in p for p in d_exec["blocked_paths"]))
        self.assertTrue(any(".git/config" in p for p in d_exec["blocked_paths"]))

        # Verify Live `Antigravity_Hub` Self-Project Protection (NEVER overwritten even with overwrite_existing=True!)
        self.assertEqual(
            (pc2_map["BASE_DIR"] / "app" / "hub_core.py").read_bytes(),
            b"LIVE_PC2_HUB_DO_NOT_OVERWRITE\n",
        )
        self.assertTrue(d_exec["self_hub_overwrite_blocked"])
        self.assertTrue(any("Antigravity_Hub" in w for w in d_exec["warnings"]))

        # 4. Safe Non-Destructive Merge (`overwrite_existing=False`) preserves existing local code, `.env` & chats
        (expected_pc2_proj_dir / "src" / "main.py").write_bytes(b"print('local pc2 edit')\n")
        (expected_pc2_proj_dir / ".env").write_bytes(b"API_TOKEN=local_pc2_keep_me\n")
        await self.database.update_chat(reg_chat["id"], title="Локально переименован")

        st_merge, d_merge, _ = await _asgi_request(
            app,
            "POST",
            "/api/gdrive/restore/execute",
            cookie_header=admin_cookie,
            json_body={
                "restore_dialogs": True,
                "restore_code": True,
                "selected_projects": ["AlphaApp"],
                "restore_env_vault": True,
                "vault_passphrase": vault_pass,
                "overwrite_existing": False,
            },
        )
        self.assertEqual(st_merge, 200, d_merge)
        self.assertEqual(d_merge["dialogs"]["chats_added"], 0)
        self.assertEqual(d_merge["dialogs"]["chats_skipped"], 2)
        self.assertEqual(d_merge["code"]["files_restored"], 0)
        self.assertGreaterEqual(d_merge["code"]["files_skipped"], 2)
        self.assertEqual(d_merge["vault"]["files_restored"], 0)
        self.assertEqual(d_merge["vault"]["files_skipped"], 2)
        self.assertEqual((expected_pc2_proj_dir / "src" / "main.py").read_bytes(), b"print('local pc2 edit')\n")
        self.assertEqual((expected_pc2_proj_dir / ".env").read_bytes(), b"API_TOKEN=local_pc2_keep_me\n")
        local_reg_after = await self.database.get_chat(reg_chat["id"])
        assert local_reg_after is not None
        self.assertEqual(local_reg_after["title"], "Локально переименован")

        # 5. Concurrent lock check (409 Conflict when lock is held)
        lock = self.gdrive_service.get_user_sync_lock("admin")
        async with lock:
            st_409, _, _ = await _asgi_request(
                app,
                "POST",
                "/api/gdrive/restore/execute",
                cookie_header=admin_cookie,
                json_body={"restore_dialogs": True},
            )
            self.assertEqual(st_409, 409)


# ---------------------------------------------------------------------------
# 3. Frontend UI Controller (Node VM) & Live WS `gdrive_sync_status` Tests
# ---------------------------------------------------------------------------

NODE_RESTORE_AND_WS_UI_CHECKS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const noop = () => {};
const ids = JSON.parse(fs.readFileSync(0, "utf8"));

function createEmitter() {
  const map = new Map();
  return {
    addEventListener(type, fn, capture = false) {
      const key = `${type}::${Boolean(capture)}`;
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(fn);
    },
    removeEventListener(type, fn, capture = false) {
      const key = `${type}::${Boolean(capture)}`;
      const arr = map.get(key);
      if (!arr) return;
      const idx = arr.indexOf(fn);
      if (idx >= 0) arr.splice(idx, 1);
    },
    dispatch(type, event = {}) {
      const evt = {
        type,
        defaultPrevented: false,
        stopped: false,
        immediateStopped: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() { this.stopped = true; },
        stopImmediatePropagation() { this.immediateStopped = true; this.stopped = true; },
        ...event,
      };
      for (const phase of [true, false]) {
        const key = `${type}::${phase}`;
        const list = [...(map.get(key) || [])];
        for (const fn of list) {
          fn(evt);
          if (evt.immediateStopped) return evt;
        }
      }
      return evt;
    },
  };
}

let docRef = null;
function makeElement(id = "") {
  const initiallyHidden = [
    "agyConfirmModal", "settingsModal", "gdriveModal",
    "quickGdriveSyncBtn", "gdriveClientSavedHint", "toggleGdriveClientEditBtn",
    "gdriveConnectedBar", "gdriveVaultPassRow", "gdriveSkippedDetails",
    "gdriveRestoreCatalogBox", "gdriveRestoreVaultPassWrap", "gdriveRestoreResultBox",
  ].includes(id);
  const initiallyChecked = [
    "gdriveSyncSelfHubChk", "gdriveRestoreDialogsChk", "gdriveRestoreCodeChk",
  ].includes(id);
  const classes = new Set(initiallyHidden ? ["hidden"] : []);
  const attrs = new Map();
  const emitter = createEmitter();
  const el = {
    id,
    value: "",
    checked: initiallyChecked,
    disabled: false,
    textContent: "",
    innerHTML: "",
    title: "",
    placeholder: "",
    className: "",
    isConnected: true,
    options: [],
    dataset: {},
    children: [],
    style: { setProperty: noop, removeProperty: noop, getPropertyValue: () => "" },
    classList: {
      add: (...names) => names.forEach(n => classes.add(n)),
      remove: (...names) => names.forEach(n => classes.delete(n)),
      toggle: (name, force) => {
        const next = force === undefined ? !classes.has(name) : Boolean(force);
        if (next) classes.add(name); else classes.delete(name);
        return next;
      },
      contains: name => classes.has(name),
    },
    setAttribute: (k, v) => attrs.set(k, String(v)),
    getAttribute: k => attrs.get(k) ?? null,
    removeAttribute: k => attrs.delete(k),
    addEventListener: emitter.addEventListener,
    removeEventListener: emitter.removeEventListener,
    dispatchEvent: evt => emitter.dispatch(evt.type, { target: el, ...evt }),
    click() {
      return emitter.dispatch("click", { target: el });
    },
    focus() {
      if (docRef) docRef.activeElement = el;
    },
    appendChild(child) { el.children.push(child); return child; },
    remove: noop,
    replaceChildren: noop,
    closest: () => null,
    querySelector: sel => sel.startsWith("#") ? (nodes.get(sel.slice(1)) ?? null) : null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 20, top: 20, right: 120, bottom: 50, width: 100, height: 30 }),
  };
  return el;
}

const nodes = new Map(ids.map(id => [id, makeElement(id)]));
const docEmitter = createEmitter();
const winEmitter = createEmitter();
const document = {
  documentElement: makeElement("html"),
  body: makeElement("body"),
  activeElement: null,
  getElementById: id => nodes.get(id) ?? null,
  querySelector: sel => sel.startsWith("#") ? (nodes.get(sel.slice(1)) ?? null) : null,
  querySelectorAll: sel => {
    if (sel === ".modal-backdrop:not(.hidden)") {
      return ["settingsModal", "gdriveModal", "agyConfirmModal"]
        .map(id => nodes.get(id))
        .filter(el => el && !el.classList.contains("hidden"));
    }
    return [];
  },
  createElement: tag => makeElement(tag),
  addEventListener: docEmitter.addEventListener,
  removeEventListener: docEmitter.removeEventListener,
  dispatch: docEmitter.dispatch,
};
docRef = document;

const storage = () => {
  const map = new Map();
  return {
    getItem: k => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: k => map.delete(k),
  };
};

const calls = [];
const nativeConfirmCalls = [];

async function mockFetch(url, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({ method, url: String(url), body });

  const reply = (payload, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  });

  if (url === "/api/gdrive/status") {
    return reply({
      ok: true,
      has_oauth_client: true,
      connected: true,
      account_email: "owner@example.com",
      root_folder_name: "Antigravity_Hub",
      is_enabled: true,
      sync_self_hub: true,
      excluded_projects: [],
      include_env_vault: true,
      has_vault_passphrase: true,
      last_sync_at: "2026-11-20T08:00:00Z",
      last_sync_status: "ok",
      last_sync_error: "",
      last_sync_summary: { uploaded: 2, updated: 1, skipped_unchanged: 10, trashed: 0, uploaded_bytes: 4096 },
      sync_in_progress: false,
    });
  }
  if (url === "/api/gdrive/restore/catalog") {
    return reply({
      ok: true,
      has_backup: true,
      root_folder_name: "Antigravity_Hub",
      dialogs_backup: {
        exists: true,
        size_bytes: 42000,
        modified_at: "2026-11-20T08:00:00Z",
        chats_count: 15,
        messages_count: 124,
        folders_count: 3,
        passports_count: 4,
      },
      secrets_vault: {
        exists: true,
        size_bytes: 2048,
        modified_at: "2026-11-20T08:00:00Z",
        files_count: 5,
      },
      projects: [
        { name: "AlphaApp", code_files_count: 14, exists_locally: false, is_self_hub: false },
        { name: "Antigravity_Hub", code_files_count: 52, exists_locally: true, is_self_hub: true },
      ],
    });
  }
  if (url === "/api/gdrive/restore/execute") {
    return reply({
      ok: true,
      dialogs: { requested: body.restore_dialogs, chats_added: 15, chats_updated: 0, chats_skipped: 0, messages_restored: 124, passports_restored: 4, passports_updated: 0 },
      code: { requested: body.restore_code, files_restored: 14, files_overwritten: 0, files_skipped: 52 },
      vault: { requested: body.restore_env_vault, files_restored: body.restore_env_vault ? 5 : 0, files_skipped: 0 },
      self_hub_overwrite_blocked: Boolean(body.overwrite_existing),
      warnings: body.overwrite_existing
        ? ["Защита самопроекта Antigravity_Hub: перезапись существующих файлов принудительно отключена."]
        : [],
      blocked_paths: [],
    });
  }
  if (url.startsWith("/api/projects")) return reply({ projects: [] });
  if (url.startsWith("/api/chats")) return reply({ chats: [] });
  return reply({ ok: true });
}

const context = {
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
  document,
  fetch: mockFetch,
  confirm: msg => { nativeConfirmCalls.push(String(msg)); return true; },
  localStorage: storage(),
  sessionStorage: storage(),
  location: new URL("https://hub.invalid/"),
  URL,
  URLSearchParams,
  CSS: { supports: () => true, escape: String },
  innerWidth: 1440,
  innerHeight: 900,
  navigator: { userAgent: "Desktop" },
  matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop }),
  requestAnimationFrame: noop,
  cancelAnimationFrame: noop,
  setTimeout: noop,
  clearTimeout: noop,
  setInterval: noop,
  clearInterval: noop,
  addEventListener: winEmitter.addEventListener,
  removeEventListener: winEmitter.removeEventListener,
};
context.window = context;
const sandbox = vm.createContext(context);
for (const name of ["ui-shell.js", "app.js"]) {
  vm.runInContext(fs.readFileSync("app/static/" + name, "utf8"), sandbox, { filename: name, timeout: 6000 });
}

async function flushMicrotasks() {
  for (let i = 0; i < 6; i++) {
    await new Promise(r => setImmediate(r));
  }
}

(async () => {
  const api = context.__AGY_ADVISOR_API__;
  api.bindGdriveEvents();
  api.state.currentUser = { id: "admin", username: "admin", role: "admin", max_access_mode: "full_access" };
  await api.openGdriveModal();

  // 1. Live WebSocket `gdrive_sync_status` updates badges immediately (running -> ok)
  api.handleWsEvent({
    type: "gdrive_sync_status",
    state: "running",
    sync_in_progress: true,
    trigger: "auto_quiet",
  });
  assert.equal(nodes.get("gdriveConnBadge").textContent, "Синхронизация…");
  assert.equal(nodes.get("gdriveSettingsStatusPill").textContent, "Синхронизация…");
  assert.equal(nodes.get("runGdriveSyncBtn").disabled, true);
  assert.equal(nodes.get("quickGdriveSyncBtn").disabled, true);

  api.handleWsEvent({
    type: "gdrive_sync_status",
    state: "ok",
    sync_in_progress: false,
    trigger: "auto_quiet",
    summary: { uploaded: 3, updated: 4, skipped_unchanged: 22, trashed: 0, uploaded_bytes: 8192 },
    status: {
      has_oauth_client: true,
      connected: true,
      account_email: "owner@example.com",
      root_folder_name: "Antigravity_Hub",
      last_sync_at: "2026-11-20T09:10:00Z",
      last_sync_status: "ok",
      last_sync_error: "",
      last_sync_summary: { uploaded: 3, updated: 4, skipped_unchanged: 22, trashed: 0, uploaded_bytes: 8192 },
    },
  });
  assert.equal(nodes.get("gdriveConnBadge").textContent, "Подключён");
  assert.equal(nodes.get("gdriveSettingsStatusPill").textContent, "✓ owner@example.com");
  assert.equal(nodes.get("runGdriveSyncBtn").disabled, false);
  assert.match(nodes.get("gdrivePreviewSummary").innerHTML, /загружено <strong>3<\/strong>, обновлено <strong>4<\/strong>/);

  // 2. Load Restore Catalog -> populates metrics & checklist
  await api.loadGdriveRestoreCatalog();
  assert.ok(!nodes.get("gdriveRestoreCatalogBox").classList.contains("hidden"));
  assert.match(nodes.get("gdriveRestoreSummaryText").innerHTML, /15 чат\., 124 сообщ\., 4 пасп\./);
  assert.match(nodes.get("gdriveRestoreSummaryText").innerHTML, /Доступен ✓/);
  assert.match(nodes.get("gdriveRestoreProjectsChecklist").innerHTML, /AlphaApp \(14\)/);
  assert.match(nodes.get("gdriveRestoreProjectsChecklist").innerHTML, /новый/);
  assert.match(nodes.get("gdriveRestoreProjectsChecklist").innerHTML, /есть на ПК/);

  // 3. Vault checkbox toggles passphrase input & blocks empty password
  nodes.get("gdriveRestoreVaultChk").checked = true;
  api.syncGdriveRestoreVaultPassUI();
  assert.ok(!nodes.get("gdriveRestoreVaultPassWrap").classList.contains("hidden"));
  nodes.get("gdriveRestoreVaultPassInput").value = "";
  const badRes = await api.runGdriveRestore();
  assert.equal(badRes, null);
  assert.ok(nodes.get("gdriveRestoreVaultPassInput").classList.contains("is-invalid"));
  assert.equal(calls.filter(c => c.url === "/api/gdrive/restore/execute").length, 0);

  // 4. Restore with overwrite_existing=true uses AgyUI.confirm with danger styling (NEVER native confirm!)
  nodes.get("gdriveRestoreVaultPassInput").value = "SecretPass2026!";
  nodes.get("gdriveRestoreOverwriteChk").checked = true;
  const pCancel = api.runGdriveRestore();
  await flushMicrotasks();
  assert.equal(nativeConfirmCalls.length, 0);
  assert.ok(!nodes.get("agyConfirmModal").classList.contains("hidden"));
  assert.ok(nodes.get("agyConfirmOkBtn").classList.contains("btn-danger"), "Overwrite confirm must use danger style");
  nodes.get("agyConfirmCancelBtn").click();
  await pCancel;
  assert.equal(calls.filter(c => c.url === "/api/gdrive/restore/execute").length, 0);

  const pExec = api.runGdriveRestore();
  await flushMicrotasks();
  assert.ok(!nodes.get("agyConfirmModal").classList.contains("hidden"));
  nodes.get("agyConfirmOkBtn").click();
  await pExec;
  await flushMicrotasks();

  const execCall = calls.find(c => c.url === "/api/gdrive/restore/execute");
  assert.ok(execCall, "Restore execute API call must fire after confirm");
  assert.equal(execCall.body.restore_dialogs, true);
  assert.equal(execCall.body.restore_code, true);
  assert.equal(execCall.body.restore_env_vault, true);
  assert.equal(execCall.body.overwrite_existing, true);
  assert.equal(execCall.body.vault_passphrase, "SecretPass2026!");
  assert.equal(nodes.get("gdriveRestoreVaultPassInput").value, "", "Passphrase input must clear after restore");
  assert.ok(!nodes.get("gdriveRestoreResultBox").classList.contains("hidden"));
  assert.match(nodes.get("gdriveRestoreResultBox").innerHTML, /Восстановление завершено/);
  assert.match(nodes.get("gdriveRestoreResultBox").innerHTML, /Защита самопроекта Antigravity_Hub/);
  assert.ok(calls.some(c => c.url.startsWith("/api/projects")), "Restore must refresh projects list");
  assert.ok(calls.some(c => c.url.startsWith("/api/chats")), "Restore must refresh chats list");
  assert.equal(nativeConfirmCalls.length, 0, "Zero native window.confirm() calls allowed");
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""


def test_gdrive_restore_and_ws_ui_controller_in_node_vm() -> None:
    parser = ShellParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    parser.close()
    run_node("gdrive-restore-ws-vm", parser.ids, script=NODE_RESTORE_AND_WS_UI_CHECKS)


CHROMIUM_RESTORE_AND_WS_CHECKS = r"""
const viewports = [[1440, 900], [390, 844], [320, 568]];
function check(ok, message) {
  if (!ok) throw new Error(message);
}

function measureViewport(width, height) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "border:0;position:absolute;width:" + width + "px;height:" + height + "px";
    frame.onload = async () => {
      try {
        const w = frame.contentWindow;
        const d = frame.contentDocument;
        const api = w.__AGY_ADVISOR_API__;
        const get = id => d.getElementById(id);
        const rect = el => (typeof el === "string" ? get(el) : el).getBoundingClientRect();
        const eps = 1.5;

        api.state.currentUser = { id: "admin", username: "admin", role: "admin", max_access_mode: "full_access" };
        api.state.gdrive.status = {
          has_oauth_client: true,
          connected: true,
          account_email: "admin.restore.mirror@example.com",
          root_folder_name: "Antigravity_Hub",
          is_enabled: true,
          sync_self_hub: true,
          excluded_projects: [],
          include_env_vault: true,
          has_vault_passphrase: true,
          last_sync_at: "2026-11-20T08:30:00Z",
          last_sync_status: "ok",
          last_sync_error: "",
          last_sync_summary: { uploaded: 5, updated: 1, skipped_unchanged: 40, trashed: 0, uploaded_bytes: 12000 },
          sync_in_progress: false,
        };
        api.state.gdrive.restoreCatalog = {
          has_backup: true,
          root_folder_name: "Antigravity_Hub",
          dialogs_backup: {
            exists: true,
            size_bytes: 51200,
            modified_at: "2026-11-20T08:30:00Z",
            chats_count: 19,
            messages_count: 230,
            folders_count: 4,
            passports_count: 5,
          },
          secrets_vault: {
            exists: true,
            size_bytes: 3100,
            modified_at: "2026-11-20T08:30:00Z",
            files_count: 6,
          },
          projects: [
            { name: "VeryLongCloudProjectName_RestoreGeometryCheck_2026", code_files_count: 34, exists_locally: false, is_self_hub: false },
            { name: "Antigravity_Hub", code_files_count: 85, exists_locally: true, is_self_hub: true },
          ],
        };
        api.state.gdrive.lastRestoreResult = {
          dialogs: { requested: true, chats_added: 19, chats_updated: 0, chats_skipped: 0, messages_restored: 230, passports_restored: 5, passports_updated: 0 },
          code: { requested: true, files_restored: 34, files_overwritten: 0, files_skipped: 85 },
          vault: { requested: true, files_restored: 6, files_skipped: 0 },
          warnings: ["Защита самопроекта Antigravity_Hub: перезапись существующих файлов принудительно отключена (overwrite_existing проигнорирован)."],
          blocked_paths: ["AlphaApp/../escaped.txt"],
        };

        d.addEventListener("keydown", e => {
          if (e.key === "Escape") api.handleWorkspaceEscape(e);
        });

        const modal = get("gdriveModal");
        const card = modal.querySelector(".gdrive-modal-card");
        card.style.animation = "none";
        modal.classList.remove("hidden");
        get("gdriveRestoreDetails").open = true;
        get("gdriveRestoreVaultChk").checked = true;
        get("gdriveRestoreOverwriteChk").checked = true;
        api.renderGdriveModal();

        // 1. Geometry: zero horizontal overflow with expanded Section 4 Restore Wizard
        const cardR = rect(card);
        check(cardR.width > 0 && cardR.left >= -eps && cardR.right <= width + eps, "#gdriveModal escaped width on " + width);
        check(d.documentElement.scrollWidth <= w.innerWidth, "Document horizontal overflow on " + width + "px");
        check(card.scrollWidth <= card.clientWidth + 1, "Modal card horizontal overflow on " + width + "px");
        check(!get("gdriveRestoreCatalogBox").classList.contains("hidden"), "Restore catalog box must be visible");
        check(!get("gdriveRestoreVaultPassWrap").classList.contains("hidden"), "Restore vault passphrase row must be visible");
        check(!get("gdriveRestoreResultBox").classList.contains("hidden"), "Restore result box must be visible");

        // 2. Live WebSocket `gdrive_sync_status` transitions in real DOM
        api.handleWsEvent({ type: "gdrive_sync_status", state: "running", sync_in_progress: true });
        check(get("gdriveConnBadge").textContent === "Синхронизация…", "WS running must update #gdriveConnBadge");
        check(get("gdriveSettingsStatusPill").textContent === "Синхронизация…", "WS running must update #gdriveSettingsStatusPill");
        check(get("runGdriveRestoreBtn").disabled === true, "Restore button must be disabled while syncing");

        api.handleWsEvent({
          type: "gdrive_sync_status",
          state: "ok",
          sync_in_progress: false,
          summary: { uploaded: 1, updated: 2, skipped_unchanged: 40, trashed: 0, uploaded_bytes: 2048 },
        });
        check(get("gdriveConnBadge").textContent === "Подключён", "WS ok must restore #gdriveConnBadge");
        check(get("runGdriveRestoreBtn").disabled === false, "Restore button must re-enable after sync");

        // 3. `askUserConfirm` danger modal when overwrite_existing is checked
        get("gdriveRestoreVaultPassInput").value = "ValidPass123!";
        const confirmModal = get("agyConfirmModal");
        confirmModal.querySelector(".agy-confirm-card").style.animation = "none";
        const restorePromise = api.runGdriveRestore();
        await new Promise(r => setTimeout(r, 10));
        check(!confirmModal.classList.contains("hidden"), "runGdriveRestore must open #agyConfirmModal");
        check(get("agyConfirmOkBtn").classList.contains("btn-danger"), "Overwrite confirmation must apply btn-danger");
        d.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
        const res = await restorePromise;
        check(res === null, "Escape must cancel restore confirmation cleanly");
        check(confirmModal.classList.contains("hidden"), "Escape must close #agyConfirmModal");
        check(!modal.classList.contains("hidden"), "Escape must keep #gdriveModal open");

        resolve({ viewport: width + "x" + height, cardWidth: Math.round(cardR.width) });
      } catch (err) {
        reject(new Error(width + "x" + height + ": " + err.message));
      } finally {
        frame.remove();
      }
    };
    frame.srcdoc = workspacePage;
    document.body.append(frame);
  });
}

(async () => {
  const result = [];
  for (const [width, height] of viewports) {
    result.push(await measureViewport(width, height));
  }
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


def test_gdrive_restore_section_geometry_confirm_and_ws_in_chromium(tmp_path: Path) -> None:
    reports = run_workspace_chromium(tmp_path, CHROMIUM_RESTORE_AND_WS_CHECKS)
    assert [row["viewport"] for row in reports] == ["1440x900", "390x844", "320x568"]
