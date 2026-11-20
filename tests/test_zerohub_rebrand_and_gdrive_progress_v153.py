"""Regression suite v153: ZeroHub rebranding (`01 · Zero Ring` emerald SVG/PNG icons & manifest),
Google Drive in-place root folder rename (`Antigravity_Hub` -> `ZeroHub` via PATCH with zero re-uploads),
6-thread parallel upload with thread-safe folder cache & retry/refresh, live progress + ETA tracking
(`/api/gdrive/status` & WS `gdrive_sync_status`), and Chromium geometry gate (`1440x900`, `390x844`, `320x568`).

Runs strictly in temporary directories and temporary SQLite databases with `0` real network calls and never touches
live `data/hub.db`, `users/`, `shared_projects/`, or running services.
"""
from __future__ import annotations

import asyncio
import json
import re
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = _TESTS_DIR.parent
for _p in (str(ROOT_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_frontend_contracts import STATIC, ShellParser, run_node
from test_gdrive_autosync_and_restore_v152 import (
    FakeGoogleDriveServerV152,
    _apply_env_patch,
    _make_isolated_env,
    _restore_env_patch,
)
from test_gdrive_sync_core_v151 import _asgi_request
from test_ui_workspace_minimal_v142 import run_workspace_chromium


# ---------------------------------------------------------------------------
# 1. ZeroHub Brand Icons (`icon.svg`, `icon-192.png`, `icon-512.png`), Manifest & Shell
# ---------------------------------------------------------------------------


def _parse_and_validate_png(path: Path, expected_size: int) -> bytes:
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name}: invalid PNG signature"
    pos = 8
    ihdr_info: Optional[tuple[int, int, int, int, int, int, int]] = None
    idat_parts: list[bytes] = []
    saw_iend = False
    while pos < len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        cdata = raw[pos + 8 : pos + 8 + length]
        crc_stored = struct.unpack(">I", raw[pos + 8 + length : pos + 12 + length])[0]
        crc_calc = zlib.crc32(ctype + cdata) & 0xFFFFFFFF
        assert crc_stored == crc_calc, f"{path.name}: bad CRC in {ctype!r}"
        pos += 12 + length
        if ctype == b"IHDR":
            ihdr_info = struct.unpack(">IIBBBBB", cdata)
        elif ctype == b"IDAT":
            idat_parts.append(cdata)
        elif ctype == b"IEND":
            saw_iend = True
            break
    assert ihdr_info is not None and saw_iend, f"{path.name}: missing IHDR or IEND"
    width, height, bit_depth, color_type, compression, filt, interlace = ihdr_info
    assert width == expected_size and height == expected_size, (
        f"{path.name}: expected {expected_size}x{expected_size}, got {width}x{height}"
    )
    assert bit_depth == 8 and color_type == 6, f"{path.name}: expected 8-bit RGBA, got depth={bit_depth} type={color_type}"
    assert compression == 0 and filt == 0 and interlace == 0
    decompressed = zlib.decompress(b"".join(idat_parts))
    expected_bytes = height * (1 + width * 4)
    assert len(decompressed) == expected_bytes, f"{path.name}: unexpected scanline byte length"
    return decompressed


def test_zerohub_icons_manifest_and_html_branding() -> None:
    # 1. icon.svg (`01 · Zero Ring` Emerald A)
    svg_text = (STATIC / "icon.svg").read_text(encoding="utf-8")
    svg_root = ET.fromstring(svg_text)
    assert svg_root.attrib.get("viewBox") == "0 0 512 512"
    for expected_token in ("#15191c", "#0a0c0e", "#263330", "#6ee7b7", "#10b981", "#047857", "#f3f4f6", "round"):
        assert expected_token in svg_text.lower(), f"icon.svg missing {expected_token}"

    # 2. PNG icons (`192x192` and `512x512`) with valid CRC, dimensions & center core `#f3f4f6`
    for fname, size in (("icon-192.png", 192), ("icon-512.png", 512)):
        scanlines = _parse_and_validate_png(STATIC / fname, size)
        stride = 1 + size * 4
        mid = size // 2
        offset = mid * stride + 1 + mid * 4
        r, g, b, a = scanlines[offset : offset + 4]
        assert a == 255 and r >= 235 and g >= 235 and b >= 240, (
            f"{fname}: expected bright #f3f4f6 center core at ({mid},{mid}), got rgba=({r},{g},{b},{a})"
        )

    # 3. manifest.json
    manifest = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "ZeroHub"
    assert manifest["short_name"] == "ZeroHub"
    assert manifest["background_color"] == "#0e1112"
    assert manifest["theme_color"] == "#0e1112"
    icon_srcs = {item["src"] for item in manifest.get("icons", [])}
    assert {"/static/icon-192.png", "/static/icon-512.png", "/static/icon.svg"} <= icon_srcs

    # 4. index.html branding, inline Zero Ring marks & v153 progress/folder DOM
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "<title>ZeroHub</title>" in html
    assert '<meta name="apple-mobile-web-app-title" content="ZeroHub" />' in html
    assert "<h1>ZeroHub</h1>" in html
    assert '<span class="brand-title">ZeroHub</span>' in html
    assert html.count('class="zero-ring-mark"') >= 3
    assert ">A</div>" not in html
    for elem_id in (
        "gdriveRootFolderInput",
        "gdriveSyncProgressBox",
        "gdriveSyncProgressText",
        "gdriveSyncEtaText",
        "gdriveSyncProgressBar",
        "gdriveSyncProgressFill",
        "gdriveSyncCurrentItem",
    ):
        assert f'id="{elem_id}"' in html

    # 5. Zero new !important in ui-system.css (strictly 1 in @layer utilities)
    css = (STATIC / "ui-system.css").read_text(encoding="utf-8")
    stripped = re.sub(r"/\*[\s\S]*?\*/", "", css)
    important_hits = re.findall(r"!\s*important\b", stripped, flags=re.IGNORECASE)
    assert len(important_hits) == 1, f"Expected strictly 1 !important in ui-system.css, got {len(important_hits)}"
    assert ".zero-ring-mark" in stripped and ".gdrive-progress-box" in stripped


# ---------------------------------------------------------------------------
# 2. Thread-Safe Fake Google Drive Server with Concurrency, Rename & Retry Hooks
# ---------------------------------------------------------------------------


class FakeGoogleDriveServerV153(FakeGoogleDriveServerV152):
    """Thread-safe Fake Drive server supporting folder rename PATCH, concurrency telemetry, and fault injection."""

    def __init__(self, *, email: str = "zerohub.admin@example.com") -> None:
        super().__init__(email=email)
        self._lock = threading.Lock()
        self.upload_delay_sec: float = 0.0
        self.active_uploads: int = 0
        self.max_concurrent_uploads: int = 0
        self.rename_calls: list[dict[str, Any]] = []
        self.injected_upload_failures: list[int] = []  # e.g. [429, 503] consumed once per retry test

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> Any:
        from app.gdrive_service import HttpResponse

        m = method.upper()
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path

        # Measure real concurrent in-flight uploads outside the state lock
        is_upload = (m == "POST" and path == "/upload/drive/v3/files") or (
            m == "PATCH" and path.startswith("/upload/drive/v3/files/")
        )
        if is_upload:
            with self._lock:
                if self.injected_upload_failures:
                    fail_status = self.injected_upload_failures.pop(0)
                    self.calls.append({"method": m, "url": url, "headers": dict(headers), "body_len": len(body or b"")})
                    return HttpResponse(
                        fail_status,
                        {"content-type": "application/json"},
                        json.dumps({"error": {"message": f"Transient HTTP {fail_status}"}}).encode("utf-8"),
                    )
                self.active_uploads += 1
                if self.active_uploads > self.max_concurrent_uploads:
                    self.max_concurrent_uploads = self.active_uploads
            try:
                if self.upload_delay_sec > 0:
                    time.sleep(self.upload_delay_sec)
                with self._lock:
                    return super().__call__(method, url, headers, body, timeout)
            finally:
                with self._lock:
                    self.active_uploads -= 1

        # Handle metadata PATCH including in-place folder rename (`{"name": "..."}`)
        if m == "PATCH" and path.startswith("/drive/v3/files/"):
            with self._lock:
                self.calls.append({"method": m, "url": url, "headers": dict(headers), "body_len": len(body or b"")})
                auth_hdr = headers.get("Authorization", "")
                if self.require_token and auth_hdr != f"Bearer {self.require_token}":
                    return HttpResponse(
                        401,
                        {"content-type": "application/json"},
                        b'{"error":{"message":"Invalid Credentials"}}',
                    )
                fid = urllib.parse.unquote(path.split("/drive/v3/files/", 1)[1])
                rec = self.files.get(fid)
                if not rec:
                    return HttpResponse(404, {"content-type": "application/json"}, b'{"error":{"message":"Not Found"}}')
                patch = json.loads((body or b"{}").decode("utf-8"))
                if "name" in patch:
                    old_name = rec.get("name")
                    rec["name"] = str(patch["name"])
                    self.rename_calls.append({"id": fid, "old_name": old_name, "new_name": rec["name"], "url": url})
                if "trashed" in patch:
                    rec["trashed"] = bool(patch["trashed"])
                return HttpResponse(
                    200,
                    {"content-type": "application/json"},
                    json.dumps(
                        {"id": rec["id"], "name": rec["name"], "trashed": bool(rec.get("trashed"))}
                    ).encode("utf-8"),
                )

        with self._lock:
            return super().__call__(method, url, headers, body, timeout)


# ---------------------------------------------------------------------------
# 3. Backend Unit & Integration Tests: Rename In-Place, 6x Parallel Upload,
#    Folder Cache Thread-Safety, Retries & Live Progress + ETA
# ---------------------------------------------------------------------------


class ZeroHubGDriveProgressAndRenameV153Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app import auth, brain_reader, config, database, gdrive_service, main as main_mod

        self.auth = auth
        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.gdrive_service = gdrive_service
        self.main_mod = main_mod

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v153_zerohub_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()
        self.env_map = _make_isolated_env(self.tmp_root / "hub_pc")
        (self.env_map["BASE_DIR"] / "app" / "stub.py").write_bytes(b"STUB = 1\n")

        await database.close_db_pool()
        self._saved_attrs = _apply_env_patch(self.env_map)
        self.fake_drive = FakeGoogleDriveServerV153(email="zerohub.admin@example.com")
        gdrive_service.set_default_transport(self.fake_drive)
        gdrive_service.clear_gdrive_dirty_state(None)
        brain_reader.invalidate_projects_list_cache()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        self.gdrive_service.clear_gdrive_dirty_state(None)
        self.gdrive_service.set_default_transport(None)
        await self.database.close_db_pool()
        _restore_env_patch(self._saved_attrs)
        self.brain_reader.invalidate_projects_list_cache()
        self._tmp.cleanup()

    async def _connect_admin(self, *, root_folder_name: str = "ZeroHub", sync_self_hub: bool = False) -> None:
        await self.database.save_gdrive_config(
            "admin",
            oauth_client={
                "client_id": "123456-v153.apps.googleusercontent.com",
                "client_secret": "GOCSPX-v153-secret",
                "auth_uri": self.gdrive_service.GOOGLE_AUTH_URL,
                "token_uri": self.gdrive_service.GOOGLE_TOKEN_URL,
            },
            tokens={
                "access_token": "tok_v153_live",
                "refresh_token": "rtok_v153_live",
                "expires_at": int(datetime.now(timezone.utc).timestamp()) + 7200,
                "scope": self.gdrive_service.GOOGLE_DRIVE_SCOPE,
            },
            account_email=self.fake_drive.email,
            root_folder_name=root_folder_name,
            is_enabled=True,
            sync_self_hub=sync_self_hub,
        )
        self.fake_drive.require_token = "tok_v153_live"

    async def test_db_migration_and_in_place_root_folder_rename_zero_reuploads(self) -> None:
        assert self.database.DEFAULT_GDRIVE_ROOT_FOLDER_NAME == "ZeroHub"

        # 1. Start with legacy folder name `Antigravity_Hub` and sync initial project + chat
        proj = self.env_map["PROJECTS_ROOT"] / "ZeroNote"
        (proj / "src").mkdir(parents=True)
        for i in range(6):
            (proj / "src" / f"mod_{i}.py").write_bytes(f"MOD = {i}\n".encode("utf-8"))
        self.brain_reader.invalidate_projects_list_cache()

        chat = await self.database.create_chat(title="ZeroHub Plan", kind="chat", owner_id="admin")
        await self.database.add_message(chat["id"], role="user", content="Check in-place rename")
        await self._connect_admin(root_folder_name="Antigravity_Hub", sync_self_hub=False)

        admin_user = await self.database.get_user_by_id("admin")
        assert admin_user is not None

        res1 = await self.gdrive_service.sync_user_to_gdrive(admin_user, trigger="manual")
        old_root_id = res1["summary"]["root_folder_id"]
        assert old_root_id
        assert self.fake_drive.files[old_root_id]["name"] == "Antigravity_Hub"
        items_before = await self.database.list_gdrive_sync_items("admin")
        code_ids_before = {
            lp: row["drive_file_id"]
            for lp, row in items_before.items()
            if lp.startswith("Проекты/ZeroNote/code/")
        }
        assert len(code_ids_before) == 6

        # 2. Simulate upgrading an existing Hub installation: remove migration marker & re-run init_db()
        async with self.database.connect_db() as db:
            await db.execute("DELETE FROM hub_meta WHERE key = 'gdrive_root_renamed_zerohub_v153';")
            await db.commit()
        await self.database.init_db()

        migrated_cfg = await self.database.get_gdrive_config("admin")
        assert migrated_cfg["root_folder_name"] == "ZeroHub"
        assert migrated_cfg["root_folder_id"] == old_root_id

        # Idempotency check: if user sets custom name via REST API, re-running init_db() does NOT override it
        admin_cookie = f"{self.auth.COOKIE_NAME}={self.auth.create_user_session_token('admin', int(admin_user.get('session_version') or 1))}"
        st_cfg, d_cfg, _ = await _asgi_request(
            self.main_mod.app,
            "POST",
            "/api/gdrive/config",
            cookie_header=admin_cookie,
            json_body={"root_folder_name": "CustomBackup"},
        )
        assert st_cfg == 200 and d_cfg["root_folder_name"] == "CustomBackup"
        await self.database.init_db()
        assert (await self.database.get_gdrive_config("admin"))["root_folder_name"] == "CustomBackup"

        # Set target back to `ZeroHub` via API and trigger sync -> in-place PATCH rename!
        st_cfg2, d_cfg2, _ = await _asgi_request(
            self.main_mod.app,
            "POST",
            "/api/gdrive/config",
            cookie_header=admin_cookie,
            json_body={"root_folder_name": "ZeroHub"},
        )
        assert st_cfg2 == 200 and d_cfg2["root_folder_name"] == "ZeroHub"

        uploads_before_rename_sync = self.fake_drive.count_upload_calls()
        res2 = await self.gdrive_service.sync_user_to_gdrive(admin_user, trigger="manual")

        # Root folder was renamed IN PLACE via PATCH without changing root_folder_id or clearing gdrive_sync_items!
        assert res2["summary"]["root_folder_id"] == old_root_id
        assert res2["summary"]["root_folder_name"] == "ZeroHub"
        assert self.fake_drive.files[old_root_id]["name"] == "ZeroHub"
        assert len(self.fake_drive.rename_calls) == 1
        assert self.fake_drive.rename_calls[0]["id"] == old_root_id
        assert self.fake_drive.rename_calls[0]["old_name"] == "Antigravity_Hub"
        assert self.fake_drive.rename_calls[0]["new_name"] == "ZeroHub"
        assert res2["summary"]["uploaded"] == 0
        # Zero user code/chat/passport/db files were re-uploaded (only sync_manifest.json updates root_folder_name)
        assert res2["summary"]["skipped_unchanged"] == len(items_before) - 1
        assert self.fake_drive.count_upload_calls() == uploads_before_rename_sync + 1  # manifest PATCH only

        items_after = await self.database.list_gdrive_sync_items("admin")
        code_ids_after = {
            lp: row["drive_file_id"]
            for lp, row in items_after.items()
            if lp.startswith("Проекты/ZeroNote/code/")
        }
        assert code_ids_after == code_ids_before

        # Run #3 with zero changes -> 0 uploads, 0 updates, 0 extra renames!
        res3 = await self.gdrive_service.sync_user_to_gdrive(admin_user, trigger="manual")
        assert res3["summary"]["uploaded"] == 0
        assert res3["summary"]["updated"] == 0
        assert len(self.fake_drive.rename_calls) == 1

    async def test_parallel_6_thread_upload_folder_cache_retries_and_live_progress(self) -> None:
        # Build 45 files across 5 packages to test 6-thread parallel upload, folder cache safety & progress/ETA
        proj = self.env_map["PROJECTS_ROOT"] / "ParallelPack"
        for pkg_idx in range(5):
            sub = proj / "src" / f"pkg_{pkg_idx}"
            sub.mkdir(parents=True, exist_ok=True)
            for f_idx in range(9):
                (sub / f"file_{f_idx}.py").write_bytes(f"PKG={pkg_idx}; FILE={f_idx}\n".encode("utf-8"))
        self.brain_reader.invalidate_projects_list_cache()

        await self._connect_admin(root_folder_name="ZeroHub", sync_self_hub=False)
        admin_user = await self.database.get_user_by_id("admin")
        assert admin_user is not None

        # Inject small per-upload delay so concurrent threads genuinely overlap (6 workers max)
        self.fake_drive.upload_delay_sec = 0.025
        ws_events: list[dict[str, Any]] = []
        observed_status_during_sync: list[dict[str, Any]] = []

        def _capture_ws(evt: dict[str, Any]) -> None:
            ws_events.append(dict(evt))
            if evt.get("state") == "running" and isinstance(evt.get("progress"), dict):
                if evt["progress"].get("phase") == "uploading" and len(observed_status_during_sync) < 3:
                    prog_now = self.gdrive_service.get_user_sync_progress("admin")
                    if prog_now is not None:
                        observed_status_during_sync.append(prog_now)

        # Verify /api/gdrive/status live polling while sync is running
        admin_cookie = f"{self.auth.COOKIE_NAME}={self.auth.create_user_session_token('admin', int(admin_user.get('session_version') or 1))}"
        api_polled_progress: list[dict[str, Any]] = []

        async def _poll_status_while_running() -> None:
            for _ in range(40):
                await asyncio.sleep(0.02)
                st_code, payload, _ = await _asgi_request(
                    self.main_mod.app,
                    "GET",
                    "/api/gdrive/status",
                    cookie_header=admin_cookie,
                )
                assert st_code == 200
                if payload.get("sync_in_progress") and isinstance(payload.get("sync_progress"), dict):
                    api_polled_progress.append(payload["sync_progress"])
                    if payload["sync_progress"].get("phase") == "uploading":
                        return

        sync_task = asyncio.create_task(
            self.gdrive_service.sync_user_to_gdrive(
                admin_user,
                trigger="manual",
                ws_notifier=_capture_ws,
                progress_throttle_sec=0.0,
            )
        )
        poll_task = asyncio.create_task(_poll_status_while_running())
        res = await sync_task
        await poll_task

        # 1. All 45 code files + passport + sqlite.gz + sync_manifest.json uploaded cleanly
        assert res["summary"]["uploaded"] == 48
        assert res["summary"]["updated"] == 0
        assert self.gdrive_service.SYNC_UPLOAD_CONCURRENCY == 6
        assert self.fake_drive.max_concurrent_uploads >= 3, (
            f"Expected real parallel uploads, saw max_concurrent={self.fake_drive.max_concurrent_uploads}"
        )
        assert self.fake_drive.max_concurrent_uploads <= 6

        # 2. Zero duplicate folders created on Drive despite parallel workers
        from app.gdrive_service import GOOGLE_DRIVE_FOLDER_MIME

        folder_keys: list[tuple[str, str]] = []
        for rec in self.fake_drive.files.values():
            if rec.get("mimeType") == GOOGLE_DRIVE_FOLDER_MIME and not rec.get("trashed"):
                parent = (rec.get("parents") or ["root"])[0]
                folder_keys.append((parent, str(rec.get("name") or "")))
        assert len(folder_keys) == len(set(folder_keys)), f"Duplicate folders detected: {folder_keys}"

        # 3. Intermediate progress events & /api/gdrive/status progress payload
        upload_progs = [
            e["progress"]
            for e in ws_events
            if e.get("state") == "running"
            and isinstance(e.get("progress"), dict)
            and e["progress"].get("phase") == "uploading"
        ]
        assert len(upload_progs) >= 10
        assert upload_progs[-1]["processed"] == 48
        assert upload_progs[-1]["total"] == 48
        assert upload_progs[-1]["percent"] == 100
        assert any(
            0 < p["percent"] < 100 and p["eta_sec"] is not None and bool(p["current_item"])
            for p in upload_progs
        ), "Expected intermediate uploading progress with percent, eta_sec and current_item"
        assert observed_status_during_sync, "Expected get_user_sync_progress() snapshot during active upload"
        assert api_polled_progress, "Expected GET /api/gdrive/status to expose sync_progress while running"
        assert ws_events[-1]["state"] == "ok"
        assert ws_events[-1]["sync_in_progress"] is False
        assert ws_events[-1]["progress"] is None

        # After completion, status reports idle & sync_progress=None
        st_after, d_after, _ = await _asgi_request(
            self.main_mod.app, "GET", "/api/gdrive/status", cookie_header=admin_cookie
        )
        assert st_after == 200
        assert d_after["sync_in_progress"] is False
        assert d_after["sync_progress"] is None

        # 4. Direct retry + thread-safe 401 refresh test on GoogleDriveClient
        refresh_calls = 0
        refresh_lock = threading.Lock()

        def _refresh_once() -> str:
            nonlocal refresh_calls
            with refresh_lock:
                refresh_calls += 1
            time.sleep(0.02)
            self.fake_drive.require_token = "tok_refreshed_v153"
            return "tok_refreshed_v153"

        self.fake_drive.upload_delay_sec = 0.0
        self.fake_drive.injected_upload_failures = [429, 503]
        retry_client = self.gdrive_service.GoogleDriveClient(
            "tok_v153_live",
            transport=self.fake_drive,
            refresh_token_fn=_refresh_once,
            retry_delays=(0.01, 0.01),
        )
        root_id = res["summary"]["root_folder_id"]
        fid_retried = retry_client.upload_new_file(
            name="retried_after_429_503.txt",
            parent_id=root_id,
            content=b"OK_AFTER_RETRY",
            mime_type="text/plain",
        )
        assert fid_retried in self.fake_drive.files
        assert self.fake_drive.injected_upload_failures == []

        # Force 401 across 6 concurrent threads -> refresh_token_fn must execute ONCE!
        self.fake_drive.require_token = "tok_expired_mismatch"
        results: list[str] = []
        errors: list[Exception] = []

        def _worker(idx: int) -> None:
            try:
                fid = retry_client.ensure_folder_path(root_id, ["ConcurrentRefreshTest", f"shared_sub"])
                results.append(fid)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        assert not errors, f"Concurrent 401 refresh errors: {errors}"
        assert len(results) == 6 and len(set(results)) == 1
        assert refresh_calls == 1, f"Expected single deduplicated token refresh, got {refresh_calls}"


# ---------------------------------------------------------------------------
# 4. Node VM UI Checks: ZeroHub Title, Progress Bar, ETA & Folder Input
# ---------------------------------------------------------------------------


NODE_V153_UI_CHECKS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const ids = JSON.parse(fs.readFileSync(0, "utf8"));
const noop = () => {};

function makeNode(id) {
  const classes = new Set();
  const listeners = new Map();
  const attrs = new Map();
  const node = {
    id,
    value: "",
    checked: false,
    disabled: false,
    textContent: "",
    innerHTML: "",
    title: "",
    dataset: {},
    style: { width: "", setProperty: noop, removeProperty: noop, getPropertyValue: () => "" },
    classList: {
      add: (...c) => c.forEach(x => classes.add(x)),
      remove: (...c) => c.forEach(x => classes.delete(x)),
      toggle: (c, force) => {
        if (force === undefined) {
          if (classes.has(c)) { classes.delete(c); return false; }
          classes.add(c); return true;
        }
        if (force) classes.add(c); else classes.delete(c);
        return Boolean(force);
      },
      contains: c => classes.has(c),
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const list = listeners.get(type);
      if (!list) return;
      const idx = list.indexOf(fn);
      if (idx >= 0) list.splice(idx, 1);
    },
    dispatchEvent(evt) {
      for (const fn of (listeners.get(evt.type) || []).slice()) fn(evt);
    },
    click() { this.dispatchEvent({ type: "click", target: this, preventDefault: noop, stopPropagation: noop }); },
    focus: noop,
    blur: noop,
    setAttribute(k, v) { attrs.set(k, String(v)); },
    getAttribute(k) { return attrs.has(k) ? attrs.get(k) : null; },
    removeAttribute(k) { attrs.delete(k); },
    appendChild: noop,
    replaceChildren: noop,
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
  };
  return node;
}

const nodes = new Map(ids.map(id => [id, makeNode(id)]));
for (const hiddenId of ["gdriveModal", "gdriveSyncProgressBox", "agyConfirmModal", "gdriveConnectedBar"]) {
  if (nodes.has(hiddenId)) nodes.get(hiddenId).classList.add("hidden");
}

const document = {
  title: "ZeroHub",
  documentElement: makeNode("html"),
  body: makeNode("body"),
  getElementById: id => nodes.get(id) ?? null,
  querySelector: s => s.startsWith("#") ? nodes.get(s.slice(1)) ?? null : null,
  querySelectorAll: () => [],
  createElement: () => makeNode("el"),
  addEventListener: noop,
  removeEventListener: noop,
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
      account_email: "admin@zerohub.dev",
      root_folder_name: "ZeroHub",
      is_enabled: true,
      sync_self_hub: true,
      excluded_projects: [],
      include_env_vault: false,
      has_vault_passphrase: false,
      last_sync_at: "",
      last_sync_status: "",
      last_sync_error: "",
      last_sync_summary: {},
      sync_in_progress: true,
      sync_progress: {
        phase: "uploading",
        processed: 3420,
        total: 4502,
        uploaded: 3418,
        updated: 2,
        skipped_unchanged: 0,
        percent: 76,
        current_item: "Проекты/ZeroNote/src/App.tsx",
        elapsed_sec: 184.2,
        eta_sec: 85,
      },
    });
  }
  if (url === "/api/gdrive/config") {
    return reply({
      ok: true,
      has_oauth_client: true,
      connected: true,
      account_email: "admin@zerohub.dev",
      root_folder_name: (body && body.root_folder_name) || "ZeroHub",
      is_enabled: Boolean(body && body.is_enabled),
      sync_self_hub: Boolean(body && body.sync_self_hub),
      excluded_projects: (body && body.excluded_projects) || [],
      include_env_vault: Boolean(body && body.include_env_vault),
      has_vault_passphrase: false,
      sync_in_progress: false,
      sync_progress: null,
    });
  }
  return reply({ ok: true });
}

const storage = () => {
  const m = new Map();
  return { getItem: k => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) };
};

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
  addEventListener: noop,
  removeEventListener: noop,
};
context.window = context;
const sandbox = vm.createContext(context);
for (const name of ["ui-shell.js", "app.js"]) {
  vm.runInContext(fs.readFileSync("app/static/" + name, "utf8"), sandbox, { filename: name, timeout: 6000 });
}

async function flush() {
  for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r));
}

(async () => {
  const api = context.__AGY_ADVISOR_API__;
  const ui = context.AgyUI;
  assert.equal(typeof ui.ICO.zeroRing, "string");
  assert.match(ui.icon("zeroRing"), /viewBox="0 0 24 24"/);

  // 1. Dynamic document.title picks up ZeroHub from initial document.title
  api.syncBackgroundAlertsUI();
  assert.equal(document.title, "ZeroHub");
  api.handleWsEvent({ type: "assistant_start", chat_id: "c1" });
  api.state.unreadCompletedChats.set("c2", { status: "done", ts: Date.now(), title: "Done", runsCount: 2 });
  api.syncBackgroundAlertsUI();
  assert.equal(document.title, "(● 1 · ✓ 2) ZeroHub");
  api.handleWsEvent({ type: "turn_done", chat_id: "c1" });
  api.state.unreadCompletedChats.clear();
  api.syncBackgroundAlertsUI();
  assert.equal(document.title, "ZeroHub");

  // 2. Open modal while server reports active sync_progress (76%)
  api.bindGdriveEvents();
  api.state.currentUser = { id: "admin", username: "admin", role: "admin", max_access_mode: "full_access" };
  await api.openGdriveModal();

  assert.equal(nodes.get("gdriveRootFolderLabel").textContent, "ZeroHub");
  assert.equal(nodes.get("gdriveRootFolderInput").value, "ZeroHub");
  assert.ok(!nodes.get("gdriveSyncProgressBox").classList.contains("hidden"));
  assert.match(nodes.get("gdriveSyncProgressText").textContent, /Загрузка в облако:.*76%\)/);
  assert.equal(nodes.get("gdriveSyncEtaText").textContent, "~1 мин");
  assert.equal(nodes.get("gdriveSyncProgressFill").style.width, "76%");
  assert.equal(nodes.get("gdriveSyncCurrentItem").textContent, "Проекты/ZeroNote/src/App.tsx");
  assert.equal(nodes.get("runGdriveSyncBtn").textContent, "☁️ Синхронизация 76%…");
  assert.equal(nodes.get("gdriveSettingsStatusPill").textContent, "Синхронизация 76%…");

  // 3. Live WS progress update (92%) and completion -> hides progress bar
  api.handleWsEvent({
    type: "gdrive_sync_status",
    state: "running",
    sync_in_progress: true,
    progress: {
      phase: "uploading",
      processed: 4142,
      total: 4502,
      percent: 92,
      eta_sec: 18,
      current_item: "Проекты/ZeroNote/src/SyncWorker.ts",
    },
  });
  assert.equal(nodes.get("gdriveSyncProgressFill").style.width, "92%");
  assert.equal(nodes.get("gdriveSyncEtaText").textContent, "~18 сек");
  assert.equal(nodes.get("runGdriveSyncBtn").textContent, "☁️ Синхронизация 92%…");
  assert.equal(nodes.get("gdriveSettingsStatusPill").textContent, "Синхронизация 92%…");

  api.handleWsEvent({
    type: "gdrive_sync_status",
    state: "ok",
    sync_in_progress: false,
    progress: null,
    summary: { uploaded: 4502, updated: 0, skipped_unchanged: 0, trashed: 0, uploaded_bytes: 1200000 },
    status: {
      has_oauth_client: true,
      connected: true,
      account_email: "admin@zerohub.dev",
      root_folder_name: "ZeroHub",
      last_sync_at: "2026-11-20T12:00:00Z",
      last_sync_status: "ok",
      last_sync_summary: { uploaded: 4502, updated: 0, skipped_unchanged: 0, trashed: 0, uploaded_bytes: 1200000 },
    },
  });
  assert.ok(nodes.get("gdriveSyncProgressBox").classList.contains("hidden"));
  assert.equal(nodes.get("runGdriveSyncBtn").textContent, "☁️ Синхронизировать сейчас");
  assert.equal(nodes.get("gdriveSettingsStatusPill").textContent, "✓ admin@zerohub.dev");

  // 4. Editing #gdriveRootFolderInput persists root_folder_name in POST /api/gdrive/config
  nodes.get("gdriveRootFolderInput").value = "ZeroHub_Mirror";
  await api.saveGdriveSettings();
  const cfgPost = calls.filter(c => c.url === "/api/gdrive/config" && c.method === "POST").at(-1);
  assert.equal(cfgPost.body.root_folder_name, "ZeroHub_Mirror");
  assert.equal(nodes.get("gdriveRootFolderLabel").textContent, "ZeroHub_Mirror");

  // 5. Disconnect prompt mentions ZeroHub via AgyUI.confirm (0 native confirm calls)
  const discP = api.disconnectGdriveFromModal();
  await flush();
  assert.equal(nativeConfirmCalls.length, 0);
  assert.match(nodes.get("agyConfirmMessage").textContent, /ZeroHub/);
  nodes.get("agyConfirmCancelBtn").click();
  await discP;
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""


def test_zerohub_ui_and_progress_in_node_vm() -> None:
    parser = ShellParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    parser.close()
    run_node("zerohub-v153-vm", parser.ids, script=NODE_V153_UI_CHECKS)


# ---------------------------------------------------------------------------
# 5. Headless Chromium Geometry Gate (1440x900, 390x844, 320x568)
# ---------------------------------------------------------------------------


CHROMIUM_V153_CHECKS = r"""
const viewports = [[1440, 900], [390, 844], [320, 568]];
function check(ok, message) {
  if (!ok) throw new Error(message);
}

function measureViewport(width, height) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = `position:fixed;left:0;top:0;width:${width}px;height:${height}px;border:0`;
    frame.onload = async () => {
      try {
        const w = frame.contentWindow;
        const d = frame.contentDocument;
        const api = w.__AGY_ADVISOR_API__;
        const get = id => d.getElementById(id);
        const rect = el => (typeof el === "string" ? get(el) : el).getBoundingClientRect();
        const eps = 1.5;

        // 1. Verify ZeroHub brand logo marks & sidebar header height (48px)
        const brandTitle = d.querySelector(".sidebar-brand .brand-title");
        check(brandTitle && brandTitle.textContent.trim() === "ZeroHub", "Sidebar brand title must be ZeroHub");
        const brandSvg = d.querySelector(".brand-logo-mark svg.zero-ring-mark");
        check(Boolean(brandSvg), "Missing .zero-ring-mark SVG in .brand-logo-mark");
        const welcomeSvg = d.querySelector("#welcomeState .welcome-logo-box svg.zero-ring-mark");
        check(Boolean(welcomeSvg), "Missing .zero-ring-mark SVG in #welcomeState");
        if (width >= 1024) {
          const brandH = Math.round(rect(d.querySelector(".sidebar-brand")).height);
          check(brandH === 48, "Sidebar brand header height changed from 48px: " + brandH);
        }

        // 2. Open #gdriveModal with active sync progress & ultra-long unbroken path
        api.state.currentUser = { id: "u1", username: "admin", role: "admin", max_access_mode: "full_access" };
        api.state.projects = [{ name: "ZeroNote", is_shared: false, is_self_hub: false }];
        const ultraLongPath =
          "Проекты/VeryLongWorkspaceProjectNameWithoutAnyBreaks/packages/frontend/components/cloud_mirror/extremely_long_component_filename_that_must_ellipsis_cleanly_without_widening_modal_v153.tsx";
        api.state.gdrive.syncing = true;
        api.state.gdrive.progress = {
          phase: "uploading",
          processed: 3840,
          total: 4502,
          uploaded: 3840,
          updated: 0,
          skipped_unchanged: 0,
          percent: 85,
          current_item: ultraLongPath,
          elapsed_sec: 195.4,
          eta_sec: 42,
        };
        api.state.gdrive.status = {
          has_oauth_client: true,
          connected: true,
          account_email: "owner@zerohub.dev",
          root_folder_name: "ZeroHub",
          is_enabled: true,
          sync_self_hub: true,
          excluded_projects: [],
          include_env_vault: false,
          has_vault_passphrase: false,
          sync_in_progress: true,
          sync_progress: api.state.gdrive.progress,
        };
        get("gdriveRootFolderInput").value = "ZeroHub";
        get("gdriveModal").classList.remove("hidden");
        api.renderGdriveModal();

        const modal = get("gdriveModal");
        const card = modal.querySelector(".gdrive-modal-card");
        const progressBox = get("gdriveSyncProgressBox");
        const barFill = get("gdriveSyncProgressFill");
        const curItem = get("gdriveSyncCurrentItem");

        check(!progressBox.classList.contains("hidden"), "#gdriveSyncProgressBox must be visible when syncing");
        const cardR = rect(card);
        const boxR = rect(progressBox);
        const fillR = rect(barFill);
        const curR = rect(curItem);

        check(fillR.width > 10, "#gdriveSyncProgressFill must have positive width at 85%");
        check(boxR.right <= cardR.right + eps, "Progress box overflows card right edge");
        check(curR.right <= boxR.right + eps, "Current item path overflows progress box right edge");
        check(w.getComputedStyle(curItem).textOverflow === "ellipsis", "Current item must use text-overflow: ellipsis");
        check(d.documentElement.scrollWidth <= d.documentElement.clientWidth + 1, "Root horizontal overflow");
        check(modal.scrollWidth <= modal.clientWidth + 1, "#gdriveModal horizontal overflow");
        check(card.scrollWidth <= card.clientWidth + 1, ".gdrive-modal-card horizontal overflow");
        check(progressBox.scrollWidth <= progressBox.clientWidth + 1, "#gdriveSyncProgressBox horizontal overflow");

        resolve({
          viewport: width + "x" + height,
          cardWidth: Math.round(cardR.width),
          fillWidth: Math.round(fillR.width),
          curOverflowClipped: curItem.scrollWidth > curItem.clientWidth,
        });
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


def test_zerohub_branding_and_gdrive_progress_chromium_geometry(tmp_path: Path) -> None:
    measurements = run_workspace_chromium(tmp_path, CHROMIUM_V153_CHECKS)
    assert [row["viewport"] for row in measurements] == ["1440x900", "390x844", "320x568"]
    for row in measurements:
        assert row["fillWidth"] > 10
        assert row["curOverflowClipped"] is True
