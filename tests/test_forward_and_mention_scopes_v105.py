"""
Automated isolated verification suite for Release v105:
1. Live `data/hub.db` byte-level immutability guard (`st_mtime_ns`, `st_size`).
2. `database.build_referenced_chat_digest` 3 CPU-only scopes (`full`, `last`, `summary`):
   - `full`: up to 12 000 chars, zero thinking/tool-call leakage, preserves `compacted_summary`,
     `🔖` bookmarks, early turns, and long final assistant answer without 650-char truncation.
   - `last`: up to 8 000 chars, returns ONLY the last completed assistant answer + preceding user prompt.
   - `summary`: up to 3 200 chars, 0% GPU extractive distiller stripping fenced code blocks and filler
     while preserving early, middle, bookmarked, and final architectural decisions.
3. Zero-Trust RBAC & contour isolation (`_sanitize_referenced_chat_specs`, `_resolve_allowed_referenced_chats`,
   `_sanitize_handoff_meta`, `_make_auto_title`, `_run_turn_task_inner`, `_augment_prompt_for_mode`):
   - Regular chats (`kind == "chat"`) can reference ONLY the same owner's regular chats; project tabs
     (`kind == "project"`) can reference ONLY accessible tabs of the same project; cross-contour mixing,
     foreign chats, and cross-project tabs are strictly blocked.
   - `handoff_meta.kind == "forwarded_messages"` clamps `messages_count` (`1..50`), clears unauthorized
     `source_chat_id`, and `_make_auto_title` / `extract_outline_title_source_text` unwrap `<FORWARDED_MESSAGES>`.
4. Frontend `app/static/app.js` & `app/static/sw.js` syntax (`node --check`) + Node VM unit tests:
   - `buildForwardedMessagesXmlBlock`, `buildForwardedQuotedMarkdown`, `parseForwardedEnvelope` (preserving
     internal `---` Markdown horizontal rules, both with `userNote` and neutral frame), Outline titles.
   - `parseHubMessageTimestamp` (SQLite UTC + ISO), `formatChatDateDividerLabel` (`Сегодня`, `Вчера`,
     same-year, past-year), `buildMessageTimeTagHtml`, idempotent `syncMessageDateDividers`, `COPY_STRIP_SELECTOR`.
   - `normalizeMentionScope`, `cycleMentionScope`, `getMentionScopeUiMeta`.
5. Static `v=105` cache sync (`index.html`, `sw.js`), `#pendingForwardPreview` & `#msgSelectBar` DOM placement,
   UTF-8 without BOM, and `main.workspace` CSS invariant.

Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or live server ports.
"""

from __future__ import annotations

import json
import os
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
_INITIAL_LIVE_DB_SIG: tuple[int, int] | None = None
if LIVE_HUB_DB.exists():
    _st0 = LIVE_HUB_DB.stat()
    _INITIAL_LIVE_DB_SIG = (_st0.st_mtime_ns, _st0.st_size)


class ReleaseV105ForwardAndMentionScopesTests(unittest.IsolatedAsyncioTestCase):
    """Isolated verification of Release v105 forwarding, @mention scopes, timestamps, and RBAC."""

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        if _INITIAL_LIVE_DB_SIG is not None:
            assert LIVE_HUB_DB.exists(), "Live data/hub.db disappeared!"
            st_now = LIVE_HUB_DB.stat()
            assert (st_now.st_mtime_ns, st_now.st_size) == _INITIAL_LIVE_DB_SIG, (
                f"Live data/hub.db was modified! before={_INITIAL_LIVE_DB_SIG}, "
                f"after={(st_now.st_mtime_ns, st_now.st_size)}"
            )

    def _assert_live_db_untouched(self) -> None:
        if _INITIAL_LIVE_DB_SIG is not None:
            self.assertTrue(LIVE_HUB_DB.exists())
            st = LIVE_HUB_DB.stat()
            self.assertEqual(
                (st.st_mtime_ns, st.st_size),
                _INITIAL_LIVE_DB_SIG,
                "Live data/hub.db must remain untouched!",
            )

    async def asyncSetUp(self) -> None:
        from app import agy_engine, brain_reader, config, database, host_service_manager, main as main_mod

        self.agy_engine = agy_engine
        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.main_mod = main_mod

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v105_test_", ignore_cleanup_errors=True)
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
        self.db_path = data_dir / "hub.db"

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

    async def asyncTearDown(self) -> None:
        for p in reversed(getattr(self, "_patches", [])):
            p.stop()
        self._tmp.cleanup()
        self._assert_live_db_untouched()

    async def test_01_build_referenced_chat_digest_full_and_last_scopes(self) -> None:
        """Verify `full` (<=12k, no 650-char cut on last answer, 0 tool/thinking leak) and `last` (<=8k) scopes."""
        await self.database.init_db()

        chat = await self.database.create_chat(
            title="ШТАБ Архитектуры",
            kind="project",
            project_name="DemoProj",
            access_mode="read_only",
            owner_id="admin",
        )
        cid = chat["id"]

        await self.database.update_chat(
            cid,
            compacted_summary="РАННЯЯ_СВОДКА_КОМПАКЦИИ: Выбрана модульная схема БД SQLite WAL.",
            compacted_up_to_msg_id=1,
            compacted_count=4,
        )

        # Populate 6 user/assistant turns (12 messages total)
        bookmarked_msg_id: int | None = None
        for turn_idx in range(1, 6):
            u_prefix = (
                "<PROJECT_PULSE>\nШум пульса\n</PROJECT_PULSE>\n\n" if turn_idx == 1 else ""
            )
            await self.database.add_message(
                cid,
                user_id="admin",
                role="user",
                content=f"{u_prefix}ЗАПРОС_ХОДА_{turn_idx:02d}: Согласуем подсистему #{turn_idx}.",
            )
            asst_msg = await self.database.add_message(
                cid,
                user_id="admin",
                role="assistant",
                content=(
                    f"## Решение хода {turn_idx:02d} (TURN_{turn_idx:02d}_MARKER)\n"
                    f"- Принята конфигурация контура #{turn_idx} с изоляцией прав."
                ),
                thinking=f"SECRET_THINKING_LEAK_{turn_idx}",
                tool_calls=[
                    {
                        "name": "read_file",
                        "state": "done",
                        "output": f"SECRET_TOOL_OUTPUT_LEAK_{turn_idx}",
                    }
                ],
            )
            if turn_idx == 3:
                bookmarked_msg_id = int(asst_msg["id"])

        assert bookmarked_msg_id is not None
        await self.database.set_message_bookmarked(cid, bookmarked_msg_id, True)

        # Turn 6: final user question + long (> 2200 chars) final assistant specification
        await self.database.add_message(
            cid,
            user_id="admin",
            role="user",
            content="ЗАПРОС_ФИНАЛЬНОГО_ПЛАНА_06: Сформируй подробное ТЗ для релиза v105.",
        )
        long_spec_sections = "\n\n".join(
            f"### Секция {i}\nПодробное архитектурное требование номер {i}: "
            + ("детализация контракта API и проверки безопасности UI " * 4)
            for i in range(1, 9)
        )
        final_answer_text = (
            "# Итоговое ТЗ v105 (FINAL_SPEC_START_MARKER)\n\n"
            f"{long_spec_sections}\n\n"
            "ФИНАЛЬНЫЙ_ХВОСТ_БЕЗ_ОБРЕЗКИ_FINAL_SPEC_END_MARKER"
        )
        self.assertGreater(len(final_answer_text), 2100)
        await self.database.add_message(
            cid,
            user_id="admin",
            role="assistant",
            content=final_answer_text,
            thinking="SECRET_THINKING_FINAL",
            tool_calls=[{"name": "grep_search", "state": "done", "output": "SECRET_TOOL_FINAL"}],
        )

        # 1) Scope = "full"
        full_res = await self.database.build_referenced_chat_digest(cid, scope="full")
        assert full_res is not None
        self.assertEqual(full_res["scope"], "full")
        full_digest = full_res["block"]
        self.assertLessEqual(len(full_digest), self.database.MAX_REFERENCED_CHAT_FULL_CHARS)
        self.assertNotIn("SECRET_THINKING", full_digest)
        self.assertNotIn("SECRET_TOOL", full_digest)
        self.assertNotIn("<PROJECT_PULSE>", full_digest)
        self.assertIn("РАННЯЯ_СВОДКА_КОМПАКЦИИ", full_digest)
        self.assertIn("🔖", full_digest)
        self.assertIn("TURN_01_MARKER", full_digest)
        self.assertIn("TURN_03_MARKER", full_digest)
        self.assertIn("FINAL_SPEC_START_MARKER", full_digest)
        self.assertIn(
            "ФИНАЛЬНЫЙ_ХВОСТ_БЕЗ_ОБРЕЗКИ_FINAL_SPEC_END_MARKER",
            full_digest,
            "Last assistant message in scope='full' must NOT be truncated at 650 chars!",
        )

        # 2) Scope = "last"
        last_res = await self.database.build_referenced_chat_digest(cid, scope="last")
        assert last_res is not None
        self.assertEqual(last_res["scope"], "last")
        last_digest = last_res["block"]
        self.assertLessEqual(len(last_digest), self.database.MAX_REFERENCED_CHAT_LAST_CHARS)
        self.assertNotIn("SECRET_THINKING", last_digest)
        self.assertNotIn("SECRET_TOOL", last_digest)
        self.assertIn('scope="last"', last_digest)
        self.assertIn("[На запрос пользователя]:", last_digest)
        self.assertIn("[Последний ответ ассистента]:", last_digest)
        self.assertIn("ЗАПРОС_ФИНАЛЬНОГО_ПЛАНА_06", last_digest)
        self.assertIn("FINAL_SPEC_START_MARKER", last_digest)
        self.assertIn("ФИНАЛЬНЫЙ_ХВОСТ_БЕЗ_ОБРЕЗКИ_FINAL_SPEC_END_MARKER", last_digest)
        self.assertNotIn("TURN_01_MARKER", last_digest)
        self.assertNotIn("TURN_02_MARKER", last_digest)
        self.assertNotIn("TURN_04_MARKER", last_digest)

    async def test_02_build_referenced_chat_digest_summary_scope_cpu_only(self) -> None:
        """Verify `summary` scope strips code blocks & filler, fits <=3200 chars, and keeps start/mid/end decisions."""
        await self.database.init_db()

        chat = await self.database.create_chat(
            title="Аналитика API",
            kind="chat",
            owner_id="admin",
        )
        cid = chat["id"]

        turns = [
            (
                "Привет! Подскажи, пожалуйста, какую базу данных выбрать для локального хаба?",
                "# Выбор СУБД (EARLY_ARCH_DECISION)\n- Утвердили SQLite в режиме WAL с таймаутом 15 секунд.\n\n```python\ndef secret_code_block_1():\n    return 'CODE_FENCE_CONTENT_ALPHA'\n```",
            ),
            (
                "Спасибо! А как лучше организовать очередь фоновых задач?",
                "Здравствуйте! Конечно, давайте разберёмся.\n- Очередь строим в памяти процесса с защитой от дубликатов.",
            ),
            (
                "А что по изоляции прав пользователей между чатами?",
                "## Модель безопасности (MID_RBAC_DECISION)\n1. Обычные чаты видны строго владельцу.\n2. Проектные вкладки проверяются через can_user_access_project.\n\n```sql\nSELECT * FROM secret_sql_fence WHERE leak = 'CODE_FENCE_CONTENT_BETA';\n```",
            ),
            (
                "Ок, понятно, идём дальше по верстке карточек.",
                "- Для карточек используем ленивый рендер спойлеров `<details>`.",
            ),
            (
                "Как защитить буфер обмена от попадания меток времени?",
                "### Чистый буфер обмена (LATE_CLIPBOARD_DECISION)\n- Добавляем `.msg-time-tag` и `.msg-date-divider` в `COPY_STRIP_SELECTOR`.",
            ),
            (
                "Отлично, сформулируй финальный чеклист перед выкладкой v105.",
                "## Финальный чеклист релиза (FINAL_RELEASE_DECISION)\n- Обновить кэш Service Worker до `v=105` и прогнать изолированный `pytest`.\n\n```bash\npytest SECRET_BASH_FENCE_GAMMA\n```",
            ),
        ]

        bm_id: int | None = None
        for idx, (u_text, a_text) in enumerate(turns, start=1):
            await self.database.add_message(cid, user_id="admin", role="user", content=u_text)
            am = await self.database.add_message(
                cid,
                user_id="admin",
                role="assistant",
                content=a_text,
                thinking=f"THINK_SUM_{idx}",
            )
            if idx == 3:
                bm_id = int(am["id"])

        assert bm_id is not None
        await self.database.set_message_bookmarked(cid, bm_id, True)

        summary_res = await self.database.build_referenced_chat_digest(cid, scope="summary")
        assert summary_res is not None
        self.assertEqual(summary_res["scope"], "summary")
        summary_digest = summary_res["block"]
        self.assertLessEqual(len(summary_digest), self.database.MAX_REFERENCED_CHAT_SUMMARY_CHARS)
        self.assertIn('scope="summary"', summary_digest)
        self.assertIn("[Исходная задача]:", summary_digest)
        self.assertIn("[Эволюция обсуждения]:", summary_digest)
        # Code fences MUST be stripped
        self.assertNotIn("```", summary_digest)
        self.assertNotIn("CODE_FENCE_CONTENT_ALPHA", summary_digest)
        self.assertNotIn("CODE_FENCE_CONTENT_BETA", summary_digest)
        self.assertNotIn("SECRET_BASH_FENCE_GAMMA", summary_digest)
        self.assertNotIn("THINK_SUM_", summary_digest)
        # Key architectural points & snake_case names across start, bookmarked middle, and last 2 turns MUST survive
        self.assertIn("EARLY_ARCH_DECISION", summary_digest)
        self.assertIn("MID_RBAC_DECISION", summary_digest)
        self.assertIn("can_user_access_project", summary_digest)
        self.assertIn("LATE_CLIPBOARD_DECISION", summary_digest)
        self.assertIn("COPY_STRIP_SELECTOR", summary_digest)
        self.assertIn("FINAL_RELEASE_DECISION", summary_digest)
        self.assertIn("🔖", summary_digest)

    async def test_03_rbac_contour_isolation_and_forwarded_sanitizers(self) -> None:
        """Verify Zero-Trust RBAC, strict `project` <-> `chat` contour isolation, sanitizers, and auto-titles."""
        await self.database.init_db()

        alice = await self.database.create_user(
            username="alice_v105",
            password="AlicePassword123!",
            display_name="Alice",
            role="user",
            max_access_mode="full_access",
        )
        bob = await self.database.create_user(
            username="bob_v105",
            password="BobPassword123!",
            display_name="Bob",
            role="user",
            max_access_mode="full_access",
        )

        # Regular chats
        alice_chat_1 = await self.database.create_chat(
            title="Alice Заметки 1", kind="chat", owner_id=alice["id"]
        )
        alice_chat_2 = await self.database.create_chat(
            title="Alice Идеи 2", kind="chat", owner_id=alice["id"]
        )
        bob_private_chat = await self.database.create_chat(
            title="Bob Секрет", kind="chat", owner_id=bob["id"]
        )

        # Project chats
        alice_proj_dir = (self.hub_base / "users" / alice["username"] / "projects" / "AlphaProj").resolve()
        alice_proj_dir.mkdir(parents=True, exist_ok=True)
        other_proj_dir = (self.hub_base / "users" / alice["username"] / "projects" / "BetaProj").resolve()
        other_proj_dir.mkdir(parents=True, exist_ok=True)

        alice_alpha_hq = await self.database.create_chat(
            title="ШТАБ Alpha",
            kind="project",
            project_name="AlphaProj",
            project_path=str(alice_proj_dir),
            owner_id=alice["id"],
        )
        alice_alpha_code = await self.database.create_chat(
            title="Код Alpha",
            kind="project",
            project_name="AlphaProj",
            project_path=str(alice_proj_dir),
            owner_id=alice["id"],
        )
        alice_beta_hq = await self.database.create_chat(
            title="ШТАБ Beta",
            kind="project",
            project_name="BetaProj",
            project_path=str(other_proj_dir),
            owner_id=alice["id"],
        )

        # 1) _sanitize_referenced_chat_specs: backward compat + dedup + scope normalization + cap 3
        specs = self.main_mod._sanitize_referenced_chat_specs(
            [
                {"id": "c1", "scope": "LAST"},
                {"id": "c1", "scope": "summary"},  # duplicate id ignored
                {"id": "c2", "scope": "invalid_mode"},  # falls back to "full"
                {"id": "desktop:123", "scope": "full"},  # desktop:* rejected
            ],
            ["c2", "c3", "c4", "c5"],
        )
        self.assertEqual(
            specs,
            [
                {"id": "c1", "scope": "last"},
                {"id": "c2", "scope": "full"},
                {"id": "c3", "scope": "full"},
            ],
        )

        # 2) Regular chat RBAC & contour isolation:
        #    - allows alice_chat_2
        #    - blocks self (alice_chat_1)
        #    - blocks bob_private_chat (foreign user)
        #    - blocks alice_alpha_hq (project contour cannot be mixed into regular chat via @)
        allowed_for_regular = await self.main_mod._resolve_allowed_referenced_chats(
            alice_chat_1,
            alice,
            [
                {"id": alice_chat_1["id"], "scope": "full"},
                {"id": bob_private_chat["id"], "scope": "full"},
                {"id": alice_alpha_hq["id"], "scope": "summary"},
                {"id": alice_chat_2["id"], "scope": "last"},
            ],
        )
        self.assertEqual(
            [{"id": x["id"], "scope": x["scope"]} for x in allowed_for_regular],
            [{"id": alice_chat_2["id"], "scope": "last"}],
        )

        # 3) Project chat RBAC & contour isolation:
        #    - allows sibling alice_alpha_code (same project)
        #    - blocks alice_beta_hq (other project)
        #    - blocks alice_chat_2 (regular chat cannot be mixed into project via @)
        allowed_for_proj = await self.main_mod._resolve_allowed_referenced_chats(
            alice_alpha_hq,
            alice,
            [
                {"id": alice_beta_hq["id"], "scope": "full"},
                {"id": alice_chat_2["id"], "scope": "full"},
                {"id": alice_alpha_code["id"], "scope": "summary"},
            ],
        )
        self.assertEqual(
            [{"id": x["id"], "scope": x["scope"]} for x in allowed_for_proj],
            [{"id": alice_alpha_code["id"], "scope": "summary"}],
        )

        # 4) _sanitize_handoff_meta for `forwarded_messages`
        fwd_meta = self.main_mod._sanitize_handoff_meta(
            {
                "kind": "forwarded_messages",
                "source_chat_id": alice_chat_2["id"],
                "source_title": "Alice Идеи 2",
                "source_icon": "💬",
                "messages_count": 999,
                "user_note": "  Сравни эти подходы  ",
            }
        )
        assert fwd_meta is not None
        self.assertEqual(fwd_meta["kind"], "forwarded_messages")
        self.assertEqual(fwd_meta["messages_count"], 50)
        self.assertEqual(fwd_meta["user_note"], "Сравни эти подходы")

        # 5) _make_auto_title & extract_outline_title_source_text on <FORWARDED_MESSAGES>
        prompt_with_note = (
            "Проанализируй риски миграции схемы\n\n"
            '<FORWARDED_MESSAGES source="ШТАБ Alpha" count="2">\n'
            "[Сообщение 1 · Пользователь]:\nСтарый вопрос\n\n---\n\n"
            "[Сообщение 2 · Ассистент (GPT-5.4)]:\nСтарый ответ\n"
            "</FORWARDED_MESSAGES>"
        )
        self.assertEqual(
            self.main_mod._make_auto_title(prompt_with_note),
            "Проанализируй риски миграции схемы",
        )
        self.assertIn(
            "Проанализируй риски миграции схемы",
            self.database.extract_outline_title_source_text(prompt_with_note),
        )

        prompt_neutral_frame = (
            "Пользователь переслал сообщения из диалога «ШТАБ Alpha» для контекста.\n\n"
            '<FORWARDED_MESSAGES source="ШТАБ Alpha" count="1">\n'
            "[Сообщение 1 · Ассистент (Claude Opus 4.6)]:\n"
            "## Архитектура единого шлюза авторизации\nПодробности схемы.\n"
            "</FORWARDED_MESSAGES>"
        )
        auto_from_neutral = self.main_mod._make_auto_title(prompt_neutral_frame)
        self.assertNotIn("FORWARDED_MESSAGES", auto_from_neutral)
        self.assertNotIn("Пользователь переслал", auto_from_neutral)
        self.assertIn("Архитектура единого шлюза авторизации", auto_from_neutral)

        outline_src_neutral = self.database.extract_outline_title_source_text(prompt_neutral_frame)
        self.assertNotIn("FORWARDED_MESSAGES", outline_src_neutral)
        self.assertNotIn("[Сообщение 1", outline_src_neutral)
        self.assertIn("Архитектура единого шлюза авторизации", outline_src_neutral)

        # 6) End-to-end `_run_turn_task_inner` check:
        #    - strips unauthorized `bob_private_chat["id"]` from `handoff_meta.source_chat_id`
        #    - injects regular-chat `<REFERENCED_CHATS>` digest with `scope="last"` and adapted header
        await self.database.add_message(
            alice_chat_2["id"],
            user_id=alice["id"],
            role="user",
            content="Какой порт у локального сервиса?",
        )
        await self.database.add_message(
            alice_chat_2["id"],
            user_id=alice["id"],
            role="assistant",
            content="Локальный сервис слушает порт 8765 (REGULAR_REF_FACT_8765).",
        )

        captured_augmented_prompts: list[str] = []

        async def _fake_agy_stream(**kwargs: Any):
            aug_text = self.agy_engine._augment_prompt_for_mode(
                kind=str(kwargs.get("kind") or "chat"),
                prompt=str(kwargs.get("prompt") or ""),
                attachments=list(kwargs.get("attachments") or []),
                is_first_turn=True,
                access_mode=str(kwargs.get("access_mode") or "read_only"),
                referenced_chat_blocks=list(kwargs.get("referenced_chat_blocks") or []),
            )
            captured_augmented_prompts.append(aug_text)
            yield {
                "type": "done",
                "content": "Принято к сведению!",
                "thinking": "",
                "tool_calls": [],
                "usage": {"input_tokens": 120, "output_tokens": 40, "total_tokens": 160},
            }

        with (
            patch.object(
                self.main_mod,
                "fetch_available_models",
                new=AsyncMock(return_value=[{"id": "gemini-4-carbon-low", "label": "Gemini"}]),
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
            patch.object(self.main_mod, "stream_agy_turn", side_effect=_fake_agy_stream),
            patch.object(
                self.main_mod.ws_manager, "broadcast_for_chat", new_callable=AsyncMock
            ),
        ):
            await self.main_mod._run_turn_task_inner(
                chat_id=alice_chat_1["id"],
                prompt=prompt_with_note,
                attachments=[],
                override_model="gemini-4-carbon-low",
                override_access_mode="read_only",
                trigger_user=alice,
                referenced_chats=[
                    {"id": alice_chat_2["id"], "scope": "last"},
                    {"id": bob_private_chat["id"], "scope": "full"},
                ],
                handoff_meta={
                    "kind": "forwarded_messages",
                    "source_chat_id": bob_private_chat["id"],  # unauthorized foreign chat!
                    "source_title": "Чужой чат",
                    "messages_count": 2,
                    "user_note": "Проанализируй риски миграции схемы",
                },
            )

        self.assertEqual(len(captured_augmented_prompts), 1)
        aug = captured_augmented_prompts[0]
        self.assertIn("<REFERENCED_CHAT ", aug)
        self.assertIn("REGULAR_REF_FACT_8765", aug)
        self.assertIn("связанных диалогов", aug)
        self.assertNotIn("соседних вкладок этого же проекта", aug)

        msgs_in_c1 = await self.database.get_messages(alice_chat_1["id"])
        user_turn_msgs = [m for m in msgs_in_c1 if m["role"] == "user"]
        self.assertEqual(len(user_turn_msgs), 1)
        u_usage = user_turn_msgs[0].get("usage") or {}
        # Unauthorized source_chat_id must be cleared to ""
        self.assertEqual(u_usage["handoff_meta"]["kind"], "forwarded_messages")
        self.assertEqual(u_usage["handoff_meta"]["source_chat_id"], "")
        # Only alice_chat_2 with scope="last" may be recorded in referenced_chats
        self.assertEqual(len(u_usage["referenced_chats"]), 1)
        self.assertEqual(u_usage["referenced_chats"][0]["id"], alice_chat_2["id"])
        self.assertEqual(u_usage["referenced_chats"][0]["scope"], "last")

    def test_04_frontend_node_vm_forwarding_scopes_and_timestamps(self) -> None:
        """Run `node --check` and verify v105 forwarding, @scopes, timestamps, and clipboard guards in Node VM."""
        from app.config import subprocess_hidden_kwargs

        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["ORT_DISABLE_ALL_GPU"] = "1"

        for rel_js in ("app/static/app.js", "app/static/sw.js"):
            proc = subprocess.run(
                ["node", "--check", str(ROOT_DIR / rel_js)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                env=env,
                **subprocess_hidden_kwargs(),
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"node --check failed for {rel_js}:\n{proc.stderr}\n{proc.stdout}",
            )

        node_harness = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const appJsPath = process.argv[2];
const code = fs.readFileSync(appJsPath, "utf8");

const noop = () => {};
const makeNode = (tag = "div") => {
  const node = {
    tagName: String(tag || "div").toUpperCase(),
    className: "",
    id: "",
    dataset: {},
    attributes: {},
    children: [],
    parentNode: null,
    style: { setProperty: noop, removeProperty: noop },
    textContent: "",
    innerHTML: "",
    classList: {
      add(...cls) {
        const set = new Set(String(node.className || "").split(/\s+/).filter(Boolean));
        cls.forEach((c) => set.add(c));
        node.className = Array.from(set).join(" ");
      },
      remove(...cls) {
        const set = new Set(String(node.className || "").split(/\s+/).filter(Boolean));
        cls.forEach((c) => set.delete(c));
        node.className = Array.from(set).join(" ");
      },
      toggle(c, force) {
        const has = this.contains(c);
        const next = force === undefined ? !has : Boolean(force);
        if (next) this.add(c);
        else this.remove(c);
        return next;
      },
      contains(c) {
        return String(node.className || "").split(/\s+/).filter(Boolean).includes(c);
      },
    },
    addEventListener: noop,
    removeEventListener: noop,
    setAttribute(k, v) {
      node.attributes[k] = String(v);
      if (k.startsWith("data-")) {
        const camel = k
          .slice(5)
          .replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
        node.dataset[camel] = String(v);
      }
    },
    getAttribute(k) {
      return Object.prototype.hasOwnProperty.call(node.attributes, k) ? node.attributes[k] : "";
    },
    appendChild(child) {
      if (child.parentNode) child.parentNode.removeChild(child);
      child.parentNode = node;
      node.children.push(child);
      return child;
    },
    insertBefore(child, ref) {
      if (child.parentNode) child.parentNode.removeChild(child);
      child.parentNode = node;
      const idx = ref ? node.children.indexOf(ref) : -1;
      if (idx >= 0) node.children.splice(idx, 0, child);
      else node.children.push(child);
      return child;
    },
    removeChild(child) {
      const i = node.children.indexOf(child);
      if (i >= 0) {
        node.children.splice(i, 1);
        child.parentNode = null;
      }
      return child;
    },
    remove() {
      if (node.parentNode) node.parentNode.removeChild(node);
    },
    get previousElementSibling() {
      if (!node.parentNode) return null;
      const idx = node.parentNode.children.indexOf(node);
      return idx > 0 ? node.parentNode.children[idx - 1] : null;
    },
    get firstChild() {
      return node.children[0] || null;
    },
    querySelector: () => null,
    querySelectorAll(sel) {
      if (sel === ":scope > .msg-date-divider") {
        return node.children.filter((ch) => ch.classList.contains("msg-date-divider"));
      }
      if (sel === ":scope > .msg-row" || sel === ":scope > .msg-row[data-msg-id]") {
        return node.children.filter(
          (ch) =>
            ch.classList.contains("msg-row") &&
            (sel === ":scope > .msg-row" || Boolean(ch.dataset && ch.dataset.msgId))
        );
      }
      return [];
    },
  };
  return node;
};

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
  matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop }),
  document: {
    getElementById: () => makeNode(),
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

// 1. <FORWARDED_MESSAGES> XML builder + parser with internal `---` Markdown horizontal rules!
const xmlBlock = api.buildForwardedMessagesXmlBlock({
  sourceTitle: "ШТАБ — Antigravity Hub",
  items: [
    {
      id: 101,
      role: "user",
      authorLabel: "Admin",
      text: "Сравним два варианта кэширования?",
    },
    {
      id: 102,
      role: "assistant",
      model: "Claude Opus 4.6",
      text: "## Вариант А\nБыстрый кэш в памяти.\n\n---\n\n## Вариант Б\nSQLite таблица с индексом.",
    },
  ],
});
assert.ok(xmlBlock.startsWith('<FORWARDED_MESSAGES source="ШТАБ — Antigravity Hub" count="2">'));
assert.ok(xmlBlock.endsWith("</FORWARDED_MESSAGES>"));

// 1a. With userNote on top + retroactive parsing (no usage.handoff_meta)
const fullPromptWithNote = `Объедини плюсы обоих вариантов в гибрид\n\n${xmlBlock}`;
const parsedWithNote = api.parseForwardedEnvelope({
  role: "user",
  content: fullPromptWithNote,
  usage: {},
});
assert.ok(parsedWithNote, "parseForwardedEnvelope must recognize <FORWARDED_MESSAGES>");
assert.strictEqual(parsedWithNote.kind, "forwarded_messages");
assert.strictEqual(parsedWithNote.sourceTitle, "ШТАБ — Antigravity Hub");
assert.strictEqual(parsedWithNote.messagesCount, 2);
assert.strictEqual(parsedWithNote.userNote, "Объедини плюсы обоих вариантов в гибрид");
assert.ok(
  parsedWithNote.bodyMarkdown.includes("## Вариант А") &&
    parsedWithNote.bodyMarkdown.includes("## Вариант Б"),
  "Internal `---` horizontal rule inside assistant Markdown must NOT truncate bodyMarkdown!"
);

// 1b. With neutral frame (empty userNote) + <PROJECT_PULSE> desktop wrapper
const neutralPrompt =
  `<PROJECT_PULSE>\nPulse\n</PROJECT_PULSE>\n\n` +
  `Пользователь переслал сообщения из диалога «ШТАБ — Antigravity Hub» для контекста.\n\n${xmlBlock}`;
const parsedNeutral = api.parseForwardedEnvelope({
  role: "user",
  content: neutralPrompt,
  usage: {
    handoff_meta: {
      kind: "forwarded_messages",
      source_chat_id: "hq-chat-42",
      source_title: "ШТАБ — Antigravity Hub",
      source_icon: "🏠",
      messages_count: 2,
      user_note: "",
    },
  },
});
assert.ok(parsedNeutral);
assert.strictEqual(parsedNeutral.kind, "forwarded_messages");
assert.strictEqual(parsedNeutral.userNote, "");
assert.strictEqual(parsedNeutral.sourceChatId, "hq-chat-42");

// 1c. Quoted Markdown expansion ("В текст")
const quotedMd = api.buildForwardedQuotedMarkdown({
  sourceTitle: "ШТАБ",
  items: [{ role: "assistant", model: "GPT-5.4", text: "Первая строка\nВторая строка" }],
});
assert.ok(quotedMd.includes("> **Из «ШТАБ» · Ассистент (GPT-5.4):**"));
assert.ok(quotedMd.includes("> Первая строка\n> Вторая строка"));

// 1d. Smart Outline Titles on <FORWARDED_MESSAGES>
const outlineWithNote = api.extractSmartOutlineTurnTitle(
  { role: "user", content: fullPromptWithNote },
  []
);
assert.ok(
  outlineWithNote.title.startsWith("📎 ") && outlineWithNote.title.includes("Объедини плюсы"),
  `Unexpected outline title with note: ${JSON.stringify(outlineWithNote)}`
);
const outlineNeutral = api.extractSmartOutlineTurnTitle(
  { role: "user", content: neutralPrompt },
  []
);
assert.ok(
  outlineNeutral.title.startsWith("📎 ") && !outlineNeutral.title.includes("FORWARDED_MESSAGES"),
  `Unexpected neutral outline title: ${JSON.stringify(outlineNeutral)}`
);

// 2. Mention Scope Helpers
assert.strictEqual(api.normalizeMentionScope("LAST"), "last");
assert.strictEqual(api.normalizeMentionScope("Summary"), "summary");
assert.strictEqual(api.normalizeMentionScope("bogus"), "full");
assert.strictEqual(api.cycleMentionScope("full", 1), "last");
assert.strictEqual(api.cycleMentionScope("last", 1), "summary");
assert.strictEqual(api.cycleMentionScope("summary", 1), "full");
assert.strictEqual(api.cycleMentionScope("full", -1), "summary");
assert.strictEqual(api.getMentionScopeUiMeta("last").sentSuffix, " · последнее");
assert.strictEqual(api.getMentionScopeUiMeta("summary").sentSuffix, " · выжимка");
assert.strictEqual(api.getMentionScopeUiMeta("full").sentSuffix, "");

// 3. Message Timestamps, Day Dividers & Clean Copy Selector
const sqliteUtc = api.parseHubMessageTimestamp("2026-11-15 07:05:00");
assert.ok(sqliteUtc instanceof Date && !isNaN(sqliteUtc.getTime()), "SQLite UTC timestamp must parse");
assert.strictEqual(sqliteUtc.toISOString(), "2026-11-15T07:05:00.000Z");

const isoTs = api.parseHubMessageTimestamp("2026-11-15T07:05:00Z");
assert.strictEqual(isoTs.getTime(), sqliteUtc.getTime());

const refNow = new Date(2026, 10, 15, 15, 30, 0); // 15 Nov 2026 local
assert.strictEqual(api.formatChatDateDividerLabel(new Date(2026, 10, 15, 9, 0, 0), refNow), "Сегодня");
assert.strictEqual(api.formatChatDateDividerLabel(new Date(2026, 10, 14, 21, 0, 0), refNow), "Вчера");
assert.strictEqual(api.formatChatDateDividerLabel(new Date(2026, 2, 10, 12, 0, 0), refNow), "10 марта");
assert.strictEqual(api.formatChatDateDividerLabel(new Date(2025, 2, 10, 12, 0, 0), refNow), "10 марта 2025");

assert.strictEqual(api.formatMessageTimestampLabel(new Date(2026, 10, 15, 19, 19, 0), refNow), "Сегодня в 19:19");
assert.strictEqual(api.formatMessageTimestampLabel(new Date(2026, 10, 14, 19, 19, 0), refNow), "Вчера в 19:19");
assert.strictEqual(api.formatMessageTimestampLabel(new Date(2026, 2, 10, 9, 5, 0), refNow), "10 марта в 09:05");
assert.strictEqual(api.formatMessageTimestampLabel(new Date(2025, 2, 10, 9, 5, 0), refNow), "10 марта 2025 в 09:05");

const timeHtml = api.buildMessageTimeTagHtml({ created_at: new Date(2026, 10, 15, 19, 19, 0).toISOString() }, refNow);
assert.ok(timeHtml.includes('class="msg-time-tag"'), `Missing .msg-time-tag: ${timeHtml}`);
assert.ok(/^\<time\b.*>Сегодня в 19:19<\/time>$/.test(timeHtml), `Unexpected time tag HTML: ${timeHtml}`);
const yestHtml = api.buildMessageTimeTagHtml({ created_at: new Date(2026, 10, 14, 19, 19, 0).toISOString() }, refNow);
assert.ok(/^\<time\b.*>Вчера в 19:19<\/time>$/.test(yestHtml), `Unexpected yesterday time tag HTML: ${yestHtml}`);
const olderHtml = api.buildMessageTimeTagHtml({ created_at: new Date(2026, 2, 10, 9, 5, 0).toISOString() }, refNow);
assert.ok(/^\<time\b.*>10 марта в 09:05<\/time>$/.test(olderHtml), `Unexpected older time tag HTML: ${olderHtml}`);

// Idempotent syncMessageDateDividers test
const listEl = makeNode("div");
const r1 = makeNode("div");
r1.className = "msg-row user";
r1.dataset.msgId = "1";
r1.dataset.dateKey = "2026-11-14";
const r2 = makeNode("div");
r2.className = "msg-row assistant";
r2.dataset.msgId = "2";
r2.dataset.dateKey = "2026-11-14";
const r3 = makeNode("div");
r3.className = "msg-row user";
r3.dataset.msgId = "3";
r3.dataset.dateKey = "2026-11-15";
listEl.appendChild(r1);
listEl.appendChild(r2);
listEl.appendChild(r3);

api.syncMessageDateDividers(listEl);
const divsPass1 = listEl.querySelectorAll(":scope > .msg-date-divider");
assert.strictEqual(divsPass1.length, 2, "Must insert exactly 2 dividers for 2 distinct dateKeys");
api.syncMessageDateDividers(listEl);
const divsPass2 = listEl.querySelectorAll(":scope > .msg-date-divider");
assert.strictEqual(divsPass2.length, 2, "syncMessageDateDividers must be strictly idempotent");

// COPY_STRIP_SELECTOR must exclude date dividers, time tags, and multi-select checkboxes
for (const requiredSel of [".msg-date-divider", ".msg-time-tag", ".msg-select-check"]) {
  assert.ok(
    api.COPY_STRIP_SELECTOR.includes(requiredSel),
    `COPY_STRIP_SELECTOR must include ${requiredSel}`
  );
}

console.log("NODE_VM_V105_OK");
"""
        harness_file = self.tmp_root / "v105_node_harness.js"
        harness_file.write_text(node_harness, encoding="utf-8")
        try:
            vm_proc = subprocess.run(
                ["node", str(harness_file), str(ROOT_DIR / "app" / "static" / "app.js")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=25,
                env=env,
                **subprocess_hidden_kwargs(),
            )
            self.assertEqual(
                vm_proc.returncode,
                0,
                f"Node VM v105 test failed:\n{vm_proc.stderr}\n{vm_proc.stdout}",
            )
            self.assertIn("NODE_VM_V105_OK", vm_proc.stdout)
        finally:
            harness_file.unlink(missing_ok=True)

    def test_05_static_cache_v105_utf8_no_bom_and_workspace_css_invariant(self) -> None:
        """Verify `v=105` cache version, composer DOM order, UTF-8 without BOM, and `main.workspace` CSS safety."""
        touched_files = [
            "app/database.py",
            "app/agy_engine.py",
            "app/main.py",
            "app/static/app.js",
            "app/static/styles.css",
            "app/static/index.html",
            "app/static/sw.js",
            "tests/test_forward_and_mention_scopes_v105.py",
        ]
        for rel in touched_files:
            raw = (ROOT_DIR / rel).read_bytes()
            self.assertFalse(
                raw.startswith(b"\xef\xbb\xbf"),
                f"{rel} must be UTF-8 WITHOUT BOM!",
            )
            raw.decode("utf-8")

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
            self.assertGreaterEqual(int(m.group(1)), 105)
        assert m_sw_cache and m_sw_css and m_sw_js and m_html_css and m_html_js
        versions = {
            int(m_sw_cache.group(1)),
            int(m_sw_css.group(1)),
            int(m_sw_js.group(1)),
            int(m_html_css.group(1)),
            int(m_html_js.group(1)),
        }
        self.assertEqual(len(versions), 1, f"Cache version mismatch across index.html and sw.js: {versions}")

        self.assertIn('id="msgSelectBar"', html_text)
        self.assertIn('id="forwardTargetBackdrop"', html_text)
        self.assertIn('id="forwardTargetPopover"', html_text)

        # `#pendingForwardPreview` must sit strictly between `#pendingHqReturnPreview` and `#referencedChatsPreview`
        idx_hq = html_text.index('id="pendingHqReturnPreview"')
        idx_fwd = html_text.index('id="pendingForwardPreview"')
        idx_ref = html_text.index('id="referencedChatsPreview"')
        self.assertLess(idx_hq, idx_fwd)
        self.assertLess(idx_fwd, idx_ref)

        # `main.workspace` CSS invariant: never create a containing block that traps position:fixed modals
        css_text = (ROOT_DIR / "app" / "static" / "styles.css").read_text(encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
