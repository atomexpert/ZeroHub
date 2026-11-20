"""
Automated isolated verification suite for Release v115 (Zero-Bloat HQ Return):
1. Live `data/hub.db` byte-level immutability guard (`st_mtime_ns`, `st_size`).
2. `database.build_referenced_chat_digest` task-segment isolation & anti-duplication:
   - Clean 1-turn `code_spec` cycle (`code_spec` -> `report_id`) returns `None` (0 extra tokens) in `summary` & `full`.
   - Permanent worker tab (`Ядро`) with old closed tasks and `compacted_summary` + new 1-turn `code_spec`
     slices away old history & `compacted_summary` and returns `None`.
   - Multi-turn refinement (`old task` -> 4000-char `code_spec` -> draft reply -> user fix -> `report_id`):
     omits old tasks, collapses 4000-char spec into 1-line `[Исходное ТЗ из Штаба: ...]`, excludes `report_id`,
     and preserves intermediate draft reply + user fix («исправь мобильный отступ»).
   - Bookmark `🔖` (`bookmarked=1`) on initial `code_spec` preserves the bookmarked message instead of returning `None`.
   - Manual branch without `code_spec` (`1 question` -> `1 report_id`) returns concise `[Исходная задача]`
     without duplicating `report_id` and without repeating the same question in `[Эволюция обсуждения]`.
   - Regular `@ИмяВкладки` (`focus_latest_task_segment=False, exclude_message_id=None`) remains 100% compatible.
3. End-to-end `main._run_turn_task_inner` integration with `hq_return_source` & `referenced_chats`.
4. Frontend `v=115` contracts (`node --check`, `py_compile`, Node VM tests for `resolveSmartHqReturnRefScope`,
   `buildReturnToHqPrompt` command counters, `renderContextMeter` `hqDraftEst` + scope token weights,
   `.delete-or-archive-footer` mobile CSS, `v=115` cache sync, UTF-8 without BOM, `main.workspace` CSS invariant).

Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or live server ports.
"""

from __future__ import annotations

import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("ORT_DISABLE_ALL_GPU", "1")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

LIVE_HUB_DB = ROOT_DIR / "data" / "hub.db"


def _stat_snapshot(p: Path) -> tuple[bool, int, int]:
    if not p.exists():
        return (False, 0, 0)
    st = p.stat()
    return (True, st.st_mtime_ns, st.st_size)


def _subprocess_hidden_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


class SmartHqReturnV115Tests(unittest.IsolatedAsyncioTestCase):
    """Isolated verification of Release v115 Zero-Bloat HQ Return."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._live_db_before = _stat_snapshot(LIVE_HUB_DB)

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        from app import database

        database.close_db_pool_sync()
        if os.environ.get("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY") != "1":
            assert _stat_snapshot(LIVE_HUB_DB) == cls._live_db_before, (
                f"Live data/hub.db was modified! before={cls._live_db_before}, "
                f"after={_stat_snapshot(LIVE_HUB_DB)}"
            )

    def _assert_live_db_untouched(self) -> None:
        if os.environ.get("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY") == "1":
            return
        self.assertEqual(
            _stat_snapshot(LIVE_HUB_DB),
            self._live_db_before,
            "Live data/hub.db must remain byte-for-byte untouched!",
        )

    async def asyncSetUp(self) -> None:
        from app import agy_engine, brain_reader, config, database, host_service_manager, main as main_mod

        self.agy_engine = agy_engine
        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.main_mod = main_mod

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v115_test_", ignore_cleanup_errors=True)
        self.tmp_root = Path(self._tmp.name).resolve()
        self.hub_base = self.tmp_root / "Antigravity_Hub"
        data_dir = self.hub_base / "data"
        users_dir = self.hub_base / "users"
        projects_root = users_dir / "admin" / "projects"
        shared_dir = self.hub_base / "shared_projects"
        shared_uploads = shared_dir / "_uploads"
        shared_ctx = shared_dir / "_context"
        cli_homes = data_dir / "cli_homes"
        uploads_dir = data_dir / "uploads"
        self.db_path = data_dir / "hub_isolated_v115.db"

        for d in (
            self.hub_base,
            self.hub_base / "app",
            self.hub_base / ".venv",
            self.hub_base / ".git",
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
        (self.hub_base / "run_server.py").write_text("# test\n", encoding="utf-8")
        (self.hub_base / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (projects_root / "AlphaProj").mkdir(parents=True, exist_ok=True)

        patch_map = {
            "BASE_DIR": self.hub_base,
            "DATA_DIR": data_dir,
            "DB_PATH": self.db_path,
            "USERS_DIR": users_dir,
            "USER_WORKSPACES_DIR": users_dir,
            "PROJECTS_ROOT": projects_root,
            "SHARED_PROJECTS_DIR": shared_dir,
            "SHARED_UPLOADS_DIR": shared_uploads,
            "SHARED_CONTEXT_DIR": shared_ctx,
            "CLI_HOMES_DIR": cli_homes,
            "UPLOADS_DIR": uploads_dir,
        }
        self._patches = []
        for mod in (config, database, brain_reader, main_mod):
            for attr, val in patch_map.items():
                if hasattr(mod, attr):
                    p = patch.object(mod, attr, val)
                    p.start()
                    self._patches.append(p)

        hsm_inst = host_service_manager.HostServiceManager()
        for mod in (host_service_manager, database, main_mod):
            if hasattr(mod, "HOST_SERVICE_MANAGER"):
                p = patch.object(mod, "HOST_SERVICE_MANAGER", hsm_inst)
                p.start()
                self._patches.append(p)

        if hasattr(main_mod, "_PROJECTS_LIST_CACHE"):
            main_mod._PROJECTS_LIST_CACHE.clear()
        await database.close_db_pool()
        database.close_db_pool_sync()

    async def asyncTearDown(self) -> None:
        try:
            if hasattr(self.main_mod, "_PROJECTS_LIST_CACHE"):
                self.main_mod._PROJECTS_LIST_CACHE.clear()
            await self.database.close_db_pool()
            self.database.close_db_pool_sync()
        finally:
            for p in reversed(getattr(self, "_patches", [])):
                p.stop()
            self._tmp.cleanup()
        self._assert_live_db_untouched()

    async def test_01_clean_single_turn_code_spec_returns_none_and_zero_bloat_in_hq(self) -> None:
        """Clean 1-turn `code_spec` -> 1 report returns `None` in both `summary` and `full` (0 bloat in HQ)."""
        await self.database.init_db()

        hq_chat = await self.database.create_chat(
            title="ШТАБ",
            kind="project",
            project_name="AlphaProj",
            access_mode="read_only",
            owner_id="admin",
        )
        worker_chat = await self.database.create_chat(
            title="Ветка v115",
            kind="project",
            project_name="AlphaProj",
            access_mode="full_access",
            owner_id="admin",
        )

        spec_body = (
            "Реализуй утверждённый план из диалога «ШТАБ»:\n\n"
            "# 🚀 ТЗ (v=115): Умный возврат в Штаб\n"
            + ("Подробное описание требований к бэкенду и фронтенду. " * 60)
        )
        await self.database.add_message(
            worker_chat["id"],
            user_id="admin",
            role="user",
            content=spec_body,
            usage={"handoff_meta": {"kind": "code_spec", "source_chat_id": hq_chat["id"]}},
        )
        rep_msg = await self.database.add_message(
            worker_chat["id"],
            user_id="admin",
            role="assistant",
            content="## Отчёт о выполнении\nВсё реализовано и протестировано (REPORT_BODY_MARKER_01).",
        )
        report_id = int(rep_msg["id"])

        for sc in ("summary", "full", "last"):
            res = await self.database.build_referenced_chat_digest(
                worker_chat["id"],
                scope=sc,
                exclude_message_id=report_id,
                focus_latest_task_segment=True,
            )
            self.assertIsNone(
                res,
                f"Expected None for clean 1-turn code_spec in scope={sc!r}, got {res!r}",
            )

        # Also verify text-only recognition when `usage.handoff_meta` is absent
        worker_text_only = await self.database.create_chat(
            title="Ветка без мета",
            kind="project",
            project_name="AlphaProj",
            access_mode="write_files",
            owner_id="admin",
        )
        await self.database.add_message(
            worker_text_only["id"],
            user_id="admin",
            role="user",
            content="<PROJECT_PULSE>\nШум\n</PROJECT_PULSE>\n\nРеализуй утверждённый план из диалога «ШТАБ»:\n\n# ТЗ: Быстрый патч",
        )
        rep2 = await self.database.add_message(
            worker_text_only["id"],
            user_id="admin",
            role="assistant",
            content="Готово!",
        )
        self.assertIsNone(
            await self.database.build_referenced_chat_digest(
                worker_text_only["id"],
                scope="summary",
                exclude_message_id=int(rep2["id"]),
                focus_latest_task_segment=True,
            )
        )

        # End-to-end `_run_turn_task_inner`: even if client sent `referenced_chats`,
        # backend suppresses empty digest so 0 `<REFERENCED_CHAT>` blocks or empty chips leak into HQ!
        admin_user = await self.database.get_user_by_id("admin")
        assert admin_user is not None
        captured_ref_blocks: list[list[str]] = []

        async def _fake_stream(**kwargs: Any):
            captured_ref_blocks.append(list(kwargs.get("referenced_chat_blocks") or []))
            yield {
                "type": "done",
                "content": "Отчёт принят Штабом.",
                "thinking": "",
                "tool_calls": [],
                "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130},
            }

        with (
            patch.object(
                self.main_mod,
                "fetch_available_models",
                new=AsyncMock(return_value=[{"id": "gemini-4-carbon-high", "label": "Gemini"}]),
            ),
            patch.object(self.main_mod, "get_cli_auth_status", return_value={"ok": True}),
            patch.object(
                self.main_mod.openrouter_service,
                "is_user_openrouter_bridge_active",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                self.main_mod.sandbox_service, "is_user_sandbox_eligible", return_value=False
            ),
            patch.object(
                self.main_mod.host_service_manager, "is_user_host_eligible", return_value=False
            ),
            patch.object(
                self.main_mod, "_refresh_and_broadcast_quotas", new=AsyncMock(return_value=None)
            ),
            patch.object(self.main_mod, "stream_agy_turn", side_effect=_fake_stream),
            patch.object(
                self.main_mod.ws_manager, "broadcast_for_chat", new_callable=AsyncMock
            ),
        ):
            await self.main_mod._run_turn_task_inner(
                chat_id=hq_chat["id"],
                prompt="[Отчёт о выполнении]\nВсё готово!",
                attachments=[],
                override_model="gemini-4-carbon-high",
                override_access_mode="read_only",
                trigger_user=admin_user,
                referenced_chats=[{"id": worker_chat["id"], "scope": "summary"}],
                handoff_meta={
                    "kind": "hq_report",
                    "source_chat_id": worker_chat["id"],
                    "source_msg_id": report_id,
                    "source_title": worker_chat["title"],
                },
                hq_return_source={
                    "chat_id": worker_chat["id"],
                    "message_id": report_id,
                    "return_uid": "hqr_test_zero_bloat",
                },
            )

        self.assertEqual(captured_ref_blocks, [[]], "1-turn HQ return must inject 0 REFERENCED_CHAT blocks")
        hq_msgs = await self.database.get_messages(hq_chat["id"])
        hq_user_msgs = [m for m in hq_msgs if m["role"] == "user"]
        self.assertEqual(len(hq_user_msgs), 1)
        self.assertNotIn("referenced_chats", hq_user_msgs[0].get("usage") or {})

    async def test_02_permanent_tab_with_old_history_and_compacted_summary_returns_none(self) -> None:
        """Permanent tab (`Ядро`) with old closed tasks + `compacted_summary` + new 1-turn `code_spec` -> `None`."""
        await self.database.init_db()

        core_chat = await self.database.create_chat(
            title="Ядро",
            kind="project",
            project_name="AlphaProj",
            access_mode="full_access",
            owner_id="admin",
        )
        cid = core_chat["id"]

        old_u1 = await self.database.add_message(
            cid,
            user_id="admin",
            role="user",
            content="Реализуй утверждённый план из диалога «ШТАБ»:\n# Старое закрытое ТЗ v99 (OLD_SPEC_V99_MARKER)",
            usage={"handoff_meta": {"kind": "code_spec"}},
        )
        old_a1 = await self.database.add_message(
            cid,
            user_id="admin",
            role="assistant",
            content="Старый отчёт v99 выполнен (OLD_REPORT_V99_MARKER).",
        )
        await self.database.update_chat(
            cid,
            compacted_summary="СВОДКА_СТАРЫХ_ЗАДАЧ_V90_V99_LEAK_GUARD",
            compacted_up_to_msg_id=int(old_a1["id"]),
            compacted_count=2,
        )

        # Uncompacted closed task v110 still sitting in active history
        await self.database.add_message(
            cid,
            user_id="admin",
            role="user",
            content="Реализуй утверждённый план:\n# Закрытое ТЗ v110 (OLD_SPEC_V110_MARKER)",
            usage={"handoff_meta": {"kind": "code_spec"}},
        )
        await self.database.add_message(
            cid,
            user_id="admin",
            role="assistant",
            content="Отчёт v110 закрыт (OLD_REPORT_V110_MARKER).",
        )

        # Fresh 1-turn task v115
        await self.database.add_message(
            cid,
            user_id="admin",
            role="user",
            content="Реализуй утверждённый план из диалога «ШТАБ»:\n# Новое ТЗ v115 (FRESH_SPEC_V115)",
            usage={"handoff_meta": {"kind": "code_spec"}},
        )
        fresh_rep = await self.database.add_message(
            cid,
            user_id="admin",
            role="assistant",
            content="Финальный отчёт v115 готов (FRESH_REPORT_V115).",
        )
        _ = old_u1

        for sc in ("summary", "full"):
            res = await self.database.build_referenced_chat_digest(
                cid,
                scope=sc,
                exclude_message_id=int(fresh_rep["id"]),
                focus_latest_task_segment=True,
            )
            self.assertIsNone(
                res,
                f"Permanent tab with clean 1-turn new code_spec must return None in scope={sc!r}",
            )

    async def test_03_multiturn_refinement_delta_isolates_segment_and_collapses_spec(self) -> None:
        """Multi-turn refinement keeps delta & anchor, drops old tasks, 4000-char spec body, and `report_id`."""
        await self.database.init_db()

        chat = await self.database.create_chat(
            title="Ядро",
            kind="project",
            project_name="AlphaProj",
            access_mode="full_access",
            owner_id="admin",
        )
        cid = chat["id"]

        # Old task + compacted summary
        m_old_u = await self.database.add_message(
            cid,
            user_id="admin",
            role="user",
            content="Старая задача по настройке прокси (OLD_TASK_PROXY_NOISE_MARKER).",
        )
        m_old_a = await self.database.add_message(
            cid,
            user_id="admin",
            role="assistant",
            content="Старая задача прокси решена (OLD_TASK_PROXY_DONE_MARKER).",
        )
        await self.database.update_chat(
            cid,
            compacted_summary="СТАРАЯ_КОМПАКЦИЯ_ДО_ТЕКУЩЕГО_ТЗ_NOISE",
            compacted_up_to_msg_id=int(m_old_u["id"]),
            compacted_count=1,
        )
        _ = m_old_a

        # Current 4000+ char code_spec
        huge_spec_filler = (
            "Подробный пункт спецификации LONG_SPEC_BODY_DUPLICATE_TOKEN_BLOAT с деталями реализации. " * 65
        )
        spec_text = (
            "Реализуй утверждённый план из диалога «ШТАБ»:\n\n"
            "Ниже готовое самодостаточное ТЗ для отправки через «🚀 В Код».\n\n"
            "---\n\n"
            "# 🚀 ТЗ (v=115): Адаптивная модалка архивации\n\n"
            f"{huge_spec_filler}"
        )
        self.assertGreater(len(spec_text), 4000)

        await self.database.add_message(
            cid,
            user_id="admin",
            role="user",
            content=spec_text,
            usage={"handoff_meta": {"kind": "code_spec"}},
        )
        await self.database.add_message(
            cid,
            user_id="admin",
            role="assistant",
            content="## Черновая реализация\n- Собрал базовую вёрстку модалки (DRAFT_REPLY_MARKER_STEP1).",
        )
        await self.database.add_message(
            cid,
            user_id="admin",
            role="user",
            content="Исправь мобильный отступ на узких экранах 360px (MOBILE_PADDING_FIX_REQUEST).",
        )
        final_rep = await self.database.add_message(
            cid,
            user_id="admin",
            role="assistant",
            content="## Финальный отчёт\nМобильный отступ исправлен, все тесты зелёные (FINAL_REPORT_SELF_DUP_MARKER).",
        )
        report_id = int(final_rep["id"])

        # Add a subsequent message AFTER report_id to verify retrospective isolation too
        await self.database.add_message(
            cid,
            user_id="admin",
            role="user",
            content="А теперь посмотри совсем другую будущую тему (FUTURE_TURN_AFTER_REPORT_MARKER).",
        )

        for sc in ("summary", "full"):
            res = await self.database.build_referenced_chat_digest(
                cid,
                scope=sc,
                exclude_message_id=report_id,
                focus_latest_task_segment=True,
            )
            self.assertIsNotNone(res, f"Expected non-empty delta digest for scope={sc!r}")
            assert res is not None
            block = res["block"]
            # Must NOT contain old tasks, old compacted summary, 4000-char spec body, final report, or future turn
            self.assertNotIn("OLD_TASK_PROXY_NOISE_MARKER", block)
            self.assertNotIn("OLD_TASK_PROXY_DONE_MARKER", block)
            self.assertNotIn("СТАРАЯ_КОМПАКЦИЯ_ДО_ТЕКУЩЕГО_ТЗ_NOISE", block)
            self.assertNotIn("LONG_SPEC_BODY_DUPLICATE_TOKEN_BLOAT", block)
            self.assertNotIn("FINAL_REPORT_SELF_DUP_MARKER", block)
            self.assertNotIn("FUTURE_TURN_AFTER_REPORT_MARKER", block)
            # MUST contain short 1-line anchor + intermediate draft reply + user mobile padding fix
            self.assertIn("[Исходное ТЗ из Штаба:", block)
            self.assertIn("Адаптивная модалка архивации", block)
            self.assertIn("DRAFT_REPLY_MARKER_STEP1", block)
            self.assertIn("исправь мобильный отступ", block.lower())

    async def test_04_bookmarked_spec_manual_branch_and_regular_mention_compat(self) -> None:
        """Verify `bookmarked=1` on spec, manual 1-turn branch without duplicate evolution, and `@` compat."""
        await self.database.init_db()

        # A) Bookmarked 1-turn `code_spec` must NOT return None
        bm_chat = await self.database.create_chat(
            title="Ветка с закладкой",
            kind="project",
            project_name="AlphaProj",
            access_mode="write_files",
            owner_id="admin",
        )
        bm_spec = await self.database.add_message(
            bm_chat["id"],
            user_id="admin",
            role="user",
            content=(
                "Реализуй утверждённый план из диалога «ШТАБ»:\n"
                "# Важное ТЗ с закладкой (BOOKMARKED_SPEC_KEEP_MARKER)\n"
                "Сохранить критичный инвариант токенов."
            ),
            usage={"handoff_meta": {"kind": "code_spec"}},
        )
        await self.database.set_message_bookmarked(bm_chat["id"], int(bm_spec["id"]), True)
        bm_rep = await self.database.add_message(
            bm_chat["id"],
            user_id="admin",
            role="assistant",
            content="Отчёт по закладке (BM_REPORT_EXCLUDED_MARKER).",
        )

        for sc in ("summary", "full"):
            bm_res = await self.database.build_referenced_chat_digest(
                bm_chat["id"],
                scope=sc,
                exclude_message_id=int(bm_rep["id"]),
                focus_latest_task_segment=True,
            )
            self.assertIsNotNone(bm_res)
            assert bm_res is not None
            self.assertIn("BOOKMARKED_SPEC_KEEP_MARKER", bm_res["block"])
            self.assertIn("🔖", bm_res["block"])
            self.assertNotIn("BM_REPORT_EXCLUDED_MARKER", bm_res["block"])

        # B) Manual branch without `code_spec` (1 user question -> 1 assistant report_id)
        manual_chat = await self.database.create_chat(
            title="Ручной разбор бага",
            kind="project",
            project_name="AlphaProj",
            access_mode="write_files",
            owner_id="admin",
        )
        await self.database.add_message(
            manual_chat["id"],
            user_id="admin",
            role="user",
            content="Проверь, почему дублируется вызов рендера сайдбара при открытии вкладки (MANUAL_Q_MARKER).",
        )
        man_rep = await self.database.add_message(
            manual_chat["id"],
            user_id="admin",
            role="assistant",
            content="Причина найдена и устранена в openHubChat (MANUAL_REPORT_BODY_MARKER).",
        )

        man_sum = await self.database.build_referenced_chat_digest(
            manual_chat["id"],
            scope="summary",
            exclude_message_id=int(man_rep["id"]),
            focus_latest_task_segment=True,
        )
        self.assertIsNotNone(man_sum)
        assert man_sum is not None
        self.assertIn("[Исходная задача]:", man_sum["block"])
        self.assertIn("MANUAL_Q_MARKER", man_sum["block"])
        self.assertNotIn("MANUAL_REPORT_BODY_MARKER", man_sum["block"])
        self.assertNotIn(
            "[Эволюция обсуждения]:",
            man_sum["block"],
            "Single remaining user question must not repeat twice in [Эволюция обсуждения]",
        )

        # C) Backward compatibility for ordinary `@ИмяВкладки` (`focus_latest_task_segment=False, exclude_message_id=None`)
        for sc, expected_marker in (
            ("full", "MANUAL_REPORT_BODY_MARKER"),
            ("last", "MANUAL_REPORT_BODY_MARKER"),
            ("summary", "[Эволюция обсуждения]:"),
        ):
            reg_res = await self.database.build_referenced_chat_digest(
                manual_chat["id"],
                scope=sc,
            )
            self.assertIsNotNone(reg_res)
            assert reg_res is not None
            self.assertIn(expected_marker, reg_res["block"])
            self.assertIn("MANUAL_Q_MARKER", reg_res["block"])

    def test_05_frontend_v115_contracts_syntax_css_and_cache_sync(self) -> None:
        """Verify JS/Py syntax, Node VM unit tests for v115 helpers, `.delete-or-archive-footer`, and `v=115` sync."""
        for rel_py in (
            "app/database.py",
            "app/main.py",
            "tests/test_smart_hq_return_v115.py",
        ):
            py_compile.compile(str(ROOT_DIR / rel_py), doraise=True)

        env = os.environ.copy()
        for rel_js in ("app/static/app.js", "app/static/sw.js"):
            proc = subprocess.run(
                ["node", "--check", str(ROOT_DIR / rel_js)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                env=env,
                **_subprocess_hidden_kwargs(),
            )
            self.assertEqual(proc.returncode, 0, f"node --check failed for {rel_js}:\n{proc.stderr}")

        touched_files = [
            "app/database.py",
            "app/main.py",
            "app/static/app.js",
            "app/static/styles.css",
            "app/static/index.html",
            "app/static/sw.js",
            "tests/test_smart_hq_return_v115.py",
        ]
        for rel in touched_files:
            raw = (ROOT_DIR / rel).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{rel} must be UTF-8 without BOM")
            raw.decode("utf-8")

        app_js_text = (ROOT_DIR / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("hqDraftEst", app_js_text)
        self.assertIn("показаны последние", app_js_text)
        self.assertIn("resolveSmartHqReturnRefScope", app_js_text)

        css_text = (ROOT_DIR / "app" / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertRegex(
            css_text,
            r"\.delete-or-archive-actions\s*,\s*\.delete-or-archive-footer\s*\{",
        )
        self.assertRegex(
            css_text,
            r"\.delete-or-archive-actions\s*>\s*button\s*,\s*\.delete-or-archive-footer\s*>\s*button\s*\{",
        )

        # `main.workspace` CSS invariant
        forbidden_props = (
            "transform",
            "filter",
            "backdrop-filter",
            "perspective",
            "contain",
            "container-type",
        )
        for m in re.finditer(r"(?<![-\w])main\.workspace\s*\{([^}]*)\}", css_text):
            block = m.group(1)
            for prop in forbidden_props:
                self.assertIsNone(
                    re.search(rf"(?<![-\w]){re.escape(prop)}\s*:", block),
                    f"Forbidden property `{prop}` found inside `main.workspace` rule!",
                )

        # Static version >= 115 and synchronized across index.html and sw.js
        sw_text = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        html_text = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        m_sw_cache = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_text)
        m_sw_css = re.search(r"/static/styles\.css\?v=(\d+)", sw_text)
        m_sw_js = re.search(r"/static/app\.js\?v=(\d+)", sw_text)
        m_html_css = re.search(r"/static/styles\.css\?v=(\d+)", html_text)
        m_html_js = re.search(r"/static/app\.js\?v=(\d+)", html_text)
        for m in (m_sw_cache, m_sw_css, m_sw_js, m_html_css, m_html_js):
            self.assertIsNotNone(m)
            assert m is not None
            self.assertGreaterEqual(int(m.group(1)), 115)
        assert m_sw_cache and m_sw_css and m_sw_js and m_html_css and m_html_js
        versions = {
            int(m_sw_cache.group(1)),
            int(m_sw_css.group(1)),
            int(m_sw_js.group(1)),
            int(m_html_css.group(1)),
            int(m_html_js.group(1)),
        }
        self.assertEqual(len(versions), 1, f"Static version mismatch: {versions}")

        node_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const appJsPath = process.argv[2];
const code = fs.readFileSync(appJsPath, "utf8");

const noop = () => {};
const makeClassList = () => {
  const s = new Set();
  return {
    add: (...xs) => xs.forEach((x) => s.add(x)),
    remove: (...xs) => xs.forEach((x) => s.delete(x)),
    toggle: (x, force) => {
      if (force === undefined) {
        if (s.has(x)) { s.delete(x); return false; }
        s.add(x); return true;
      }
      if (force) s.add(x); else s.delete(x);
      return Boolean(force);
    },
    contains: (x) => s.has(x),
  };
};
const nodesById = new Map();
const makeNode = (tag = "div") => ({
  tagName: String(tag).toUpperCase(),
  style: { setProperty: noop, removeProperty: noop },
  dataset: {},
  classList: makeClassList(),
  children: [],
  textContent: "",
  innerHTML: "",
  value: "",
  title: "",
  disabled: false,
  addEventListener: noop,
  removeEventListener: noop,
  setAttribute: noop,
  getAttribute: () => null,
  removeAttribute: noop,
  focus: noop,
  blur: noop,
  closest: () => null,
  contains: () => false,
  appendChild(ch) { this.children.push(ch); return ch; },
  querySelector: () => null,
  querySelectorAll: () => [],
});

const storageMap = new Map();
const fakeStorage = {
  getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
};

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: clearTimeout,
  Node: { ELEMENT_NODE: 1 },
  Intl,
  URL,
  URLSearchParams,
  encodeURIComponent,
  decodeURIComponent,
  Math,
  Date,
  JSON,
  Map,
  Set,
  Promise,
  RegExp,
  Array,
  Object,
  String,
  Number,
  Boolean,
  Error,
  Infinity,
  NaN,
  parseInt,
  parseFloat,
  isNaN,
  isFinite,
  performance: { now: () => Date.now() },
  CSS: { escape: (s) => String(s) },
  localStorage: fakeStorage,
  sessionStorage: fakeStorage,
  navigator: { userAgent: "NodeTest", clipboard: { writeText: async () => {} } },
  location: {
    protocol: "http:",
    host: "127.0.0.1:8765",
    hostname: "127.0.0.1",
    port: "8765",
    pathname: "/",
    search: "",
    hash: "",
    origin: "http://127.0.0.1:8765",
    href: "http://127.0.0.1:8765/",
  },
  getSelection: () => null,
  matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop }),
  document: {
    getElementById: (id) => {
      if (!nodesById.has(id)) nodesById.set(id, makeNode("div"));
      return nodesById.get(id);
    },
    querySelector: () => makeNode(),
    querySelectorAll: () => [],
    createElement: (tag) => makeNode(tag),
    createDocumentFragment: () => ({ appendChild: noop }),
    addEventListener: noop,
    documentElement: { setAttribute: noop, getAttribute: () => "", style: { setProperty: noop } },
    body: makeNode("body"),
    hidden: false,
  },
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "app.js" });

const api = sandbox.__AGY_ADVISOR_API__;
assert.ok(api, "__AGY_ADVISOR_API__ must be exported");

// 1. resolveSmartHqReturnRefScope contracts
const execTab = {
  id: "w_core",
  title: "Ядро",
  kind: "project",
  project_name: "AlphaProj",
  access_mode: "full_access",
  compacted_summary: "",
};

// 1a. Clean 1-turn code_spec (via handoff_meta) -> null
const clean1TurnMeta = [
  { id: 10, role: "user", content: "ТЗ из Штаба", usage: { handoff_meta: { kind: "code_spec" } } },
  { id: 11, role: "assistant", content: "Финальный отчёт готов" },
];
assert.strictEqual(
  api.resolveSmartHqReturnRefScope(execTab, clean1TurnMeta[1], clean1TurnMeta),
  null
);

// 1b. Clean 1-turn code_spec (via text prefix after <PROJECT_PULSE>) -> null
const clean1TurnText = [
  {
    id: 20,
    role: "user",
    content: "<PROJECT_PULSE>\nШум\n</PROJECT_PULSE>\n\nРеализуй утверждённый план из диалога «ШТАБ»:\n# ТЗ v115",
  },
  { id: 21, role: "assistant", content: "Готово!" },
];
assert.strictEqual(
  api.resolveSmartHqReturnRefScope(execTab, clean1TurnText[1], clean1TurnText),
  null
);

// 1c. Permanent tab (`Ядро`) with old closed turns + compacted_summary + new 1-turn code_spec -> null
const permCoreTab = { ...execTab, compacted_summary: "Старая сводка v90..v110" };
const permMsgs = [
  { id: 1, role: "user", content: "Старая задача 1" },
  { id: 2, role: "assistant", content: "Старый ответ 1" },
  { id: 30, role: "user", content: "ТЗ v115", usage: { handoff_meta: { kind: "code_spec" } } },
  { id: 31, role: "assistant", content: "Отчёт v115" },
  { id: 32, role: "user", content: "Позднейший вопрос после отчёта" },
];
assert.strictEqual(
  api.resolveSmartHqReturnRefScope(permCoreTab, permMsgs[3], permMsgs),
  null,
  "Permanent tab with 1-turn latest code_spec (and ignored post-report turns) must return null"
);

// 1d. Multi-turn code_spec with intermediate refinement -> "summary"
const multiTurnSpec = [
  { id: 40, role: "user", content: "ТЗ v115", usage: { handoff_meta: { kind: "code_spec" } } },
  { id: 41, role: "assistant", content: "Черновой вариант" },
  { id: 42, role: "user", content: "Исправь отступ на мобильном" },
  { id: 43, role: "assistant", content: "Финальный отчёт" },
];
assert.strictEqual(
  api.resolveSmartHqReturnRefScope(execTab, multiTurnSpec[3], multiTurnSpec),
  "summary"
);

// 1e. Bookmarked 1-turn code_spec -> "summary"
const bmSpecMsgs = [
  {
    id: 50,
    role: "user",
    content: "ТЗ с важной закладкой",
    bookmarked: true,
    usage: { handoff_meta: { kind: "code_spec" } },
  },
  { id: 51, role: "assistant", content: "Отчёт" },
];
assert.strictEqual(
  api.resolveSmartHqReturnRefScope(execTab, bmSpecMsgs[1], bmSpecMsgs),
  "summary"
);

// 1f. Manual branch without code_spec (1 question -> 1 answer) -> "summary"
const manualMsgs = [
  { id: 60, role: "user", content: "Разбери баг с кэшем" },
  { id: 61, role: "assistant", content: "Баг исправлен" },
];
assert.strictEqual(
  api.resolveSmartHqReturnRefScope(execTab, manualMsgs[1], manualMsgs),
  "summary"
);

// 2. buildReturnToHqPrompt command counter transparency (> 8 commands vs <= 8 commands)
const makeToolCmds = (n) =>
  Array.from({ length: n }, (_, i) => ({
    name: "run_command",
    state: "DONE",
    parameters: { CommandLine: `pytest step_${i + 1}` },
  }));

const prompt3 = api.buildReturnToHqPrompt(
  { id: 70, role: "assistant", content: "Отчёт", model: "gemini-4-carbon-high", tool_calls: makeToolCmds(3) },
  execTab
);
assert.ok(prompt3.includes("- Команды и тесты (3):"), `Unexpected 3-cmd line: ${prompt3}`);

const prompt11 = api.buildReturnToHqPrompt(
  { id: 71, role: "assistant", content: "Отчёт", model: "gemini-4-carbon-high", tool_calls: makeToolCmds(11) },
  execTab
);
assert.ok(
  prompt11.includes("- Команды и тесты (показаны последние 8 из 11):"),
  `Expected 'показаны последние 8 из 11' in:\n${prompt11}`
);

// 3. renderContextMeter honest `+черновик` with pendingHqReturn.reportBodyText + _fromHqReturn summary weight
api.els.contextMeterBar = nodesById.get("contextMeterBar") || makeNode("div");
api.els.contextMeterFill = makeNode("div");
api.els.contextMeterInfoBtn = makeNode("button");
api.els.contextMeterValue = makeNode("span");
api.els.contextDraftTag = makeNode("span");
api.els.promptInput = makeNode("textarea");
api.els.promptInput.value = "";

api.state.activeChatId = "hq_1";
api.state.activeChat = {
  id: "hq_1",
  title: "ШТАБ",
  kind: "project",
  project_name: "AlphaProj",
  access_mode: "read_only",
  model: "gemini-4-carbon-high",
};
api.state.viewingDesktopConv = null;
api.state.messages = [{ id: 1, role: "assistant", content: "План утверждён." }];
api.state.contextStats = { used_tokens: 10000, max_tokens: 200000, source: "telemetry" };
api.state.pendingReferencedChats = [];
api.state.pendingForward = null;
api.state.pendingReply = null;
api.state.pendingHqReturn = null; api.state.pendingHqReturns = [];

api.renderContextMeter();
assert.strictEqual(api.els.contextDraftTag.classList.contains("hidden"), true);

const sampleReportBody = "Отчёт из ветки воркера. ".repeat(80);
const expectedHqTok = api.estimateDraftTokens(sampleReportBody);
assert.ok(expectedHqTok >= 300);

api.state.pendingHqReturn = {
  sourceChatId: "w_core",
  sourceMsgId: 71,
  reportBodyText: sampleReportBody,
};
api.mentionStatsCache.set("w_core", {summary: {tokens: 450}});
api.state.pendingReferencedChats = [
  { id: "w_core", title: "Ядро", scope: "summary", _fromHqReturn: true },
];
api.renderContextMeter();
assert.strictEqual(api.els.contextDraftTag.classList.contains("hidden"), false);
assert.ok(api.els.contextDraftTag.textContent.includes("черновик"));
assert.ok(
  api.els.contextMeterInfoBtn.title.includes((10000 + expectedHqTok + 450).toLocaleString("ru-RU")),
  `Expected exact totalUsed=${10000 + expectedHqTok + 450} in title: ${api.els.contextMeterInfoBtn.title}`
);

// Verify token meter uses the cached server estimates for each scope
api.mentionStatsCache.set("c1", {summary: {tokens: 650}});
api.mentionStatsCache.set("c2", {last: {tokens: 1400}});
api.mentionStatsCache.set("c3", {full: {tokens: 2400}});
api.state.pendingHqReturn = null; api.state.pendingHqReturns = [];
api.state.pendingReferencedChats = [
  { id: "c1", scope: "summary" },
  { id: "c2", scope: "last" },
  { id: "c3", scope: "full" },
];
api.renderContextMeter();
assert.ok(
  api.els.contextMeterInfoBtn.title.includes((10000 + 650 + 1400 + 2400).toLocaleString("ru-RU")),
  `Expected exact refDigestEst=4450 in title: ${api.els.contextMeterInfoBtn.title}`
);

console.log("NODE_VM_V115_OK");
"""
        harness_file = self.tmp_root / "v115_node_harness.js"
        harness_file.write_text(node_harness, encoding="utf-8")
        try:
            vm_proc = subprocess.run(
                ["node", str(harness_file), str(ROOT_DIR / "app" / "static" / "app.js")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=25,
                env=env,
                **_subprocess_hidden_kwargs(),
            )
            self.assertEqual(
                vm_proc.returncode,
                0,
                f"Node VM v115 test failed:\n{vm_proc.stderr}\n{vm_proc.stdout}",
            )
            self.assertIn("NODE_VM_V115_OK", vm_proc.stdout)
        finally:
            harness_file.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
