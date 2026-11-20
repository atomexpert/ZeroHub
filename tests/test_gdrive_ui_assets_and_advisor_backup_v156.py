"""Regression suite v156: Project UI assets sync (up to 5 MiB), compact UI sounds (<= 512 KiB in asset dirs),
script media/output anti-dump guard, and `model_advisor_catalog` backup & restore in Google Drive mirror.

Runs strictly inside temporary directories and temporary SQLite fixtures with `FakeGoogleDriveServerV152`,
`0` real network requests, and `0` touches to live `data/hub.db`, `users/`, or `shared_projects/`.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = _TESTS_DIR.parent
for _p in (str(ROOT_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_gdrive_autosync_and_restore_v152 import (
    FakeGoogleDriveServerV152,
    _apply_env_patch,
    _make_isolated_env,
    _restore_env_patch,
)


# ---------------------------------------------------------------------------
# 1. Unit Tests: UI Assets (5 MiB), UI Sounds (512 KiB), Script Dumps & Media Anti-Dump
# ---------------------------------------------------------------------------


def test_scan_ui_assets_sounds_script_dirs_and_media_anti_dump(tmp_path: Path) -> None:
    from app import gdrive_service

    proj = tmp_path / "ZeroStudioApp"
    proj.mkdir()

    # Core source file
    (proj / "app").mkdir()
    (proj / "app" / "main.py").write_text("print('ui studio')\n", encoding="utf-8")

    # 1. UI graphic asset (2.4 MiB) & web font (1.4 MiB) -> MUST pass (<= 5 MiB)
    hero_path = proj / "app" / "static" / "hero-banner.png"
    hero_path.parent.mkdir(parents=True, exist_ok=True)
    hero_bytes = b"\x89PNG\r\n\x1a\n" + (b"H" * (int(2.4 * 1024 * 1024) - 8))
    hero_path.write_bytes(hero_bytes)

    font_path = proj / "public" / "fonts" / "inter.woff2"
    font_path.parent.mkdir(parents=True, exist_ok=True)
    font_bytes = b"wOF2" + (b"F" * (int(1.4 * 1024 * 1024) - 4))
    font_path.write_bytes(font_bytes)

    # Oversized UI image (6 MiB) & oversized JSON dump (1.5 MiB) -> MUST be skipped_large_files (size_limit)
    huge_png = proj / "app" / "static" / "huge-raw.png"
    huge_png.write_bytes(b"\x89PNG\r\n\x1a\n" + (b"X" * (6 * 1024 * 1024)))

    big_dump = proj / "dump.json"
    big_dump.write_bytes(b"[" + (b"0" * int(1.5 * 1024 * 1024)) + b"]")

    # 2. Compact UI sound (48 KiB) inside UI dir -> MUST pass;
    #    root voice recording (48 KiB) -> blocked binary;
    #    large UI sound (800 KiB) -> skipped_large_files
    ui_sound = proj / "app" / "static" / "sounds" / "notify.mp3"
    ui_sound.parent.mkdir(parents=True, exist_ok=True)
    ui_sound.write_bytes(b"ID3" + (b"\x11" * (48 * 1024 - 3)))

    root_voice = proj / "bot_voice.mp3"
    root_voice.write_bytes(b"ID3" + (b"\x22" * (48 * 1024 - 3)))

    big_music = proj / "static" / "sounds" / "music.mp3"
    big_music.parent.mkdir(parents=True, exist_ok=True)
    big_music.write_bytes(b"ID3" + (b"\x33" * (800 * 1024 - 3)))

    # 3. Script output dirs (`scraped/`, `recordings/`, `captures/`, `temp/`) -> MUST be blocked completely
    for blocked_dir in ("scraped", "recordings", "captures", "temp"):
        d = proj / blocked_dir
        d.mkdir()
        (d / "artifact.txt").write_text("runtime output\n", encoding="utf-8")
        (d / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n1234")

    # 4. UI icons dir (`public/icons/` with 110 `.png`/`.svg` files, 0 code) -> MUST sync completely
    icons_dir = proj / "public" / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)
    for i in range(60):
        (icons_dir / f"ico_{i:03d}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i % 256]) * 16)
    for i in range(50):
        (icons_dir / f"vec_{i:03d}.svg").write_text(f'<svg id="v{i}"></svg>', encoding="utf-8")

    # 5. Parser image dump (`avito_photos/` with 100 `.jpg` and 0 code) -> MUST be caught by Anti-Dump Guard
    dump_photos = proj / "avito_photos"
    dump_photos.mkdir()
    for i in range(100):
        (dump_photos / f"photo_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + bytes([i % 256]) * 12)

    # 6. Frontend component folder with 90 `.json` fixtures + 1 `.css` / `.vue` -> NOT treated as dump
    comp_dir = proj / "theme_tokens"
    comp_dir.mkdir()
    (comp_dir / "palette.css").write_text(":root { --accent: #10b981; }\n", encoding="utf-8")
    for i in range(90):
        (comp_dir / f"tok_{i:03d}.json").write_text('{"k":1}\n', encoding="utf-8")

    scan = gdrive_service.scan_project_for_gdrive(proj, project_name="ZeroStudioApp", is_self_hub=False)
    code_paths = {f.rel_path for f in scan.code_files}

    # Allowed files present
    assert "app/main.py" in code_paths
    assert "app/static/hero-banner.png" in code_paths
    assert "public/fonts/inter.woff2" in code_paths
    assert "app/static/sounds/notify.mp3" in code_paths
    assert "public/icons/ico_000.png" in code_paths
    assert "public/icons/ico_059.png" in code_paths
    assert "public/icons/vec_049.svg" in code_paths
    assert len([p for p in code_paths if p.startswith("public/icons/")]) == 110
    assert "theme_tokens/palette.css" in code_paths

    # Blocked files absent
    for forbidden in (
        "app/static/huge-raw.png",
        "dump.json",
        "bot_voice.mp3",
        "static/sounds/music.mp3",
        "scraped/artifact.txt",
        "scraped/shot.png",
        "recordings/artifact.txt",
        "captures/shot.png",
        "temp/artifact.txt",
        "avito_photos/photo_000.jpg",
    ):
        assert forbidden not in code_paths, f"Unexpectedly included in code_files: {forbidden}"

    # Verify diagnostics
    large_by_path = {item["path"]: item for item in scan.skipped_large_files}
    assert "app/static/huge-raw.png" in large_by_path and large_by_path["app/static/huge-raw.png"]["reason"] == "size_limit"
    assert "dump.json" in large_by_path and large_by_path["dump.json"]["reason"] == "size_limit"
    assert "static/sounds/music.mp3" in large_by_path and large_by_path["static/sounds/music.mp3"]["reason"] == "size_limit"
    assert scan.skipped_binary_count >= 1

    dump_by_path = {item["path"]: item for item in scan.skipped_dump_dirs}
    assert "avito_photos" in dump_by_path
    assert dump_by_path["avito_photos"]["reason"] == "anti_dump_guard"
    assert dump_by_path["avito_photos"]["data_files_count"] == 100
    assert "public/icons" not in dump_by_path
    assert "theme_tokens" not in dump_by_path

    # Restore path validator permits UI graphics, fonts, and sounds inside UI dirs, and rejects script output dirs
    assert gdrive_service._validate_restore_rel_path("app/static/hero-banner.png", allow_secrets=False) == "app/static/hero-banner.png"
    assert gdrive_service._validate_restore_rel_path("public/fonts/inter.woff2", allow_secrets=False) == "public/fonts/inter.woff2"
    assert gdrive_service._validate_restore_rel_path("app/static/sounds/notify.mp3", allow_secrets=False) == "app/static/sounds/notify.mp3"
    assert gdrive_service._validate_restore_rel_path("scraped/shot.png", allow_secrets=False) is None
    assert gdrive_service._validate_restore_rel_path("recordings/clip.mp3", allow_secrets=False) is None
    assert gdrive_service._validate_restore_rel_path("captures/frame.png", allow_secrets=False) is None
    assert gdrive_service._validate_restore_rel_path("temp/junk.txt", allow_secrets=False) is None


# ---------------------------------------------------------------------------
# 2. End-to-End Backup -> Clean PC Restore of UI Assets & `model_advisor_catalog`
# ---------------------------------------------------------------------------


class GDriveUiAssetsAndAdvisorRestoreV156Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app import brain_reader, config, database, gdrive_service

        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.gdrive_service = gdrive_service

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v156_gdrive_assets_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()
        self.pc1_map = _make_isolated_env(self.tmp_root / "pc1_source")
        self.fake_drive = FakeGoogleDriveServerV152(email="assets.owner@example.com")

        await database.close_db_pool()
        self._saved_attrs = _apply_env_patch(self.pc1_map)
        gdrive_service.set_default_transport(self.fake_drive)
        brain_reader.invalidate_projects_list_cache()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        self.gdrive_service.set_default_transport(None)
        await self.database.close_db_pool()
        _restore_env_patch(self._saved_attrs)
        self.brain_reader.invalidate_projects_list_cache()
        self._tmp.cleanup()

    async def _connect_admin_gdrive(self) -> dict[str, Any]:
        await self.database.save_gdrive_config(
            "admin",
            oauth_client={"client_id": "cid-156", "client_secret": "csec-156"},
            tokens={
                "access_token": "acc_tok_156",
                "refresh_token": "ref_tok_156",
                "expires_at": 4_102_444_800,
                "scope": self.gdrive_service.GOOGLE_DRIVE_SCOPE,
            },
            account_email="assets.owner@example.com",
            is_enabled=True,
            sync_self_hub=False,
            include_env_vault=False,
        )
        return await self.database.get_user_by_id("admin")

    async def test_ui_assets_and_advisor_catalog_roundtrip_backup_to_clean_pc(self) -> None:
        # ------------------------------------------------------------------
        # PHASE 1 (PC 1): Seed project with UI assets + sounds + blocked dumps + AI advisor catalog
        # ------------------------------------------------------------------
        proj_pc1 = self.pc1_map["PROJECTS_ROOT"] / "DesignStudio"
        (proj_pc1 / "app" / "static" / "sounds").mkdir(parents=True)
        (proj_pc1 / "public" / "fonts").mkdir(parents=True)
        (proj_pc1 / "public" / "icons").mkdir(parents=True)
        (proj_pc1 / "scraped_cards_img").mkdir(parents=True)
        (proj_pc1 / "recordings").mkdir(parents=True)

        hero_bytes = b"\x89PNG\r\n\x1a\n" + (b"HERO_PIXEL_DATA_" * 157_286)  # ~2.4 MiB
        font_bytes = b"wOF2" + (b"INTER_FONT_DATA_" * 91_750)  # ~1.4 MiB
        sound_bytes = b"ID3" + (b"DING_SFX" * 6_144)  # ~48 KiB
        assert 2_300_000 < len(hero_bytes) < 2_600_000
        assert 1_300_000 < len(font_bytes) < 1_600_000
        assert 40_000 < len(sound_bytes) < 60_000

        (proj_pc1 / "app" / "main.py").write_bytes(b"APP = 'DesignStudio'\n")
        (proj_pc1 / "app" / "static" / "hero-banner.png").write_bytes(hero_bytes)
        (proj_pc1 / "public" / "fonts" / "inter.woff2").write_bytes(font_bytes)
        (proj_pc1 / "app" / "static" / "sounds" / "notify.mp3").write_bytes(sound_bytes)
        (proj_pc1 / "app" / "static" / "huge-raw.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"Z" * (6 * 1024 * 1024))
        (proj_pc1 / "dump.json").write_bytes(b"{" + b"a" * (1_500_000) + b"}")
        (proj_pc1 / "bot_voice.mp3").write_bytes(sound_bytes)
        (proj_pc1 / "recordings" / "call.wav").write_bytes(sound_bytes)

        for i in range(110):
            if i % 2 == 0:
                (proj_pc1 / "public" / "icons" / f"i_{i:03d}.png").write_bytes(b"\x89PNG" + f"{i}".encode("ascii"))
            else:
                (proj_pc1 / "public" / "icons" / f"i_{i:03d}.svg").write_text(f"<svg>{i}</svg>", encoding="utf-8")

        for i in range(100):
            (proj_pc1 / "scraped_cards_img" / f"card_{i:03d}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"J" * 32)

        self.brain_reader.invalidate_projects_list_cache()

        # Seed `model_advisor_catalog` with an auto-calibrated AI passport
        passport_payload = {
            "role_ru": "Флагман новой серии для сложного кода",
            "efforts": {
                "high": {"capability": 94, "burn_class": 2, "burn_score": 1.7},
                "xhigh": {"capability": 96, "burn_class": 3, "burn_score": 2.9},
            },
            "affinities": {"coding": 1.0, "debug": 0.97, "ui": 0.9, "docs": 0.85},
        }
        await self.database.upsert_model_advisor_entry(
            series_key="gemini-5-quantum",
            vendor="gemini",
            display_name="Gemini 5 Quantum",
            source="auto_ai",
            passport=passport_payload,
            calibrated_at="2026-11-20T07:50:00+00:00",
        )

        admin_user = await self._connect_admin_gdrive()
        sync_res = await self.gdrive_service.sync_user_to_gdrive(admin_user, transport=self.fake_drive)
        self.assertGreater(sync_res["summary"]["uploaded"], 110)

        active_cloud = self.fake_drive.active_files_by_path()
        self.assertIn("Проекты/DesignStudio/code/app/static/hero-banner.png", active_cloud)
        self.assertIn("Проекты/DesignStudio/code/public/fonts/inter.woff2", active_cloud)
        self.assertIn("Проекты/DesignStudio/code/app/static/sounds/notify.mp3", active_cloud)
        self.assertIn("Проекты/DesignStudio/code/public/icons/i_109.svg", active_cloud)
        self.assertNotIn("Проекты/DesignStudio/code/app/static/huge-raw.png", active_cloud)
        self.assertNotIn("Проекты/DesignStudio/code/dump.json", active_cloud)
        self.assertNotIn("Проекты/DesignStudio/code/bot_voice.mp3", active_cloud)
        self.assertFalse(any("scraped_cards_img" in p for p in active_cloud))
        self.assertFalse(any("recordings" in p for p in active_cloud))

        # Verify `model_advisor_catalog` is packed inside `_restore_vault/hub_dialogs_and_memory.sqlite.gz`
        self.assertIn("_restore_vault/hub_dialogs_and_memory.sqlite.gz", active_cloud)
        raw_sqlite = gzip.decompress(active_cloud["_restore_vault/hub_dialogs_and_memory.sqlite.gz"]["content"])
        with tempfile.TemporaryDirectory(prefix="hub_v156_inspect_") as insp_dir:
            db_copy = Path(insp_dir) / "snap.sqlite"
            db_copy.write_bytes(raw_sqlite)
            conn = sqlite3.connect(str(db_copy))
            try:
                rows = conn.execute(
                    "SELECT series_key, source, display_name FROM model_advisor_catalog"
                ).fetchall()
                self.assertEqual(rows, [("gemini-5-quantum", "auto_ai", "Gemini 5 Quantum")])
            finally:
                conn.close()

        # ------------------------------------------------------------------
        # PHASE 2 (Clean PC 2): Empty projects dir & fresh DB -> execute_gdrive_restore
        # ------------------------------------------------------------------
        await self.database.close_db_pool()
        _restore_env_patch(self._saved_attrs)

        pc2_map = _make_isolated_env(self.tmp_root / "pc2_clean")
        self._saved_attrs = _apply_env_patch(pc2_map)
        self.brain_reader.invalidate_projects_list_cache()
        await self.database.init_db()
        admin_pc2 = await self._connect_admin_gdrive()

        # Ensure clean PC 2 starts with 0 advisor catalog entries and no DesignStudio folder
        self.assertEqual(await self.database.list_model_advisor_catalog(), [])
        self.assertFalse((pc2_map["PROJECTS_ROOT"] / "DesignStudio").exists())

        restore_res = await self.gdrive_service.execute_gdrive_restore(
            admin_pc2,
            restore_dialogs=True,
            restore_code=True,
            selected_projects=["DesignStudio"],
            restore_env_vault=False,
            overwrite_existing=False,
            transport=self.fake_drive,
        )
        self.assertGreater(restore_res["code"]["files_restored"], 110)
        self.assertEqual(restore_res["dialogs"]["advisor_models_restored"], 1)

        # Verify byte-for-byte restoration of UI hero image, font, UI sound, and all 110 icons
        proj_pc2 = pc2_map["PROJECTS_ROOT"] / "DesignStudio"
        self.assertEqual(
            hashlib.sha256((proj_pc2 / "app" / "static" / "hero-banner.png").read_bytes()).hexdigest(),
            hashlib.sha256(hero_bytes).hexdigest(),
        )
        self.assertEqual(
            hashlib.sha256((proj_pc2 / "public" / "fonts" / "inter.woff2").read_bytes()).hexdigest(),
            hashlib.sha256(font_bytes).hexdigest(),
        )
        self.assertEqual(
            (proj_pc2 / "app" / "static" / "sounds" / "notify.mp3").read_bytes(),
            sound_bytes,
        )
        self.assertEqual(len(list((proj_pc2 / "public" / "icons").iterdir())), 110)
        self.assertFalse((proj_pc2 / "scraped_cards_img").exists())
        self.assertFalse((proj_pc2 / "recordings").exists())

        # Verify `model_advisor_catalog` restored completely
        restored_entry = await self.database.get_model_advisor_entry("gemini-5-quantum")
        self.assertIsNotNone(restored_entry)
        self.assertEqual(restored_entry["source"], "auto_ai")
        self.assertEqual(restored_entry["display_name"], "Gemini 5 Quantum")
        self.assertEqual(restored_entry["passport"]["efforts"]["high"]["capability"], 94)

        # Verify non-destructive behavior when `overwrite_existing=False` vs updating when `overwrite_existing=True`
        await self.database.upsert_model_advisor_entry(
            series_key="gemini-5-quantum",
            vendor="gemini",
            display_name="Local Edited Name",
            source="auto_ai",
            passport=passport_payload,
        )
        res_skip = await self.gdrive_service.execute_gdrive_restore(
            admin_pc2,
            restore_dialogs=True,
            restore_code=False,
            overwrite_existing=False,
            transport=self.fake_drive,
        )
        self.assertEqual(res_skip["dialogs"]["advisor_models_restored"], 0)
        self.assertEqual(
            (await self.database.get_model_advisor_entry("gemini-5-quantum"))["display_name"],
            "Local Edited Name",
        )

        res_overwrite = await self.gdrive_service.execute_gdrive_restore(
            admin_pc2,
            restore_dialogs=True,
            restore_code=False,
            overwrite_existing=True,
            transport=self.fake_drive,
        )
        self.assertEqual(res_overwrite["dialogs"]["advisor_models_restored"], 1)
        self.assertEqual(
            (await self.database.get_model_advisor_entry("gemini-5-quantum"))["display_name"],
            "Gemini 5 Quantum",
        )
