"""
Automated isolated verification suite for Release v100:
1. Live `data/hub.db` byte-level immutability guard (`st_mtime_ns`, `st_size`).
2. Backend SQLite `append_message_hq_return`: preserves sibling `usage_json` keys (`input_tokens`,
   `code_handoffs`), idempotently enriches `target_msg_id` via `return_uid` / `increment_count=False`,
   and increments `send_count` on distinct re-sends.
3. REST `POST /api/chats/{chat_id}/messages/{message_id}/hq-return`: Zero-Trust RBAC (`403` foreign
   private source chat, `403` foreign private `target_chat_id`, `200` owner & shared project member,
   `400` empty target, `404` missing) + WebSocket `message_hq_return_updated` broadcast.
4. Turn pipeline (`_sanitize_handoff_meta`, `_sanitize_hq_return_source`, `_run_turn_task_inner`):
   persists `user_msg.usage["handoff_meta"]`, strips unauthorized `source_chat_id`, and atomically
   records `hq_returns` with `target_msg_id = user_msg["id"]` on the source execution message.
5. Frontend `parseForwardedEnvelope`, `mergeHqReturnLists`, `buildMessageHqReturnsHtml`, and
   `extractSmartOutlineTurnTitle` (`Комментарий пользователя` -> `↩ Отчёт: <комментарий>`) in Node VM
   + `node --check` via `subprocess_hidden_kwargs()` with `0% GPU` env (`CUDA_VISIBLE_DEVICES=""`).
6. Static `v=100` cache sync (`index.html`, `sw.js`), `#pendingHqReturnPreview` DOM placement before
   `#referencedChatsPreview`, UTF-8 without BOM, and `main.workspace` CSS invariant.

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
import urllib.parse
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
    def __init__(self, app: Any, *, client: tuple[str, int] = ("127.0.0.1", 53200)) -> None:
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


class TestReleaseV100HandoffEnvelopesAndHqReceipts(unittest.IsolatedAsyncioTestCase):
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
        from app import brain_reader, config, database, host_service_manager, main as main_mod

        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.main_mod = main_mod

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v100_test_", ignore_cleanup_errors=True)
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

    async def test_01_database_append_message_hq_return(self) -> None:
        """Verify append_message_hq_return preserves sibling usage keys and handles idempotent return_uid."""
        await self.database.init_db()

        chat = await self.database.create_chat(
            title="Вкладка Кода",
            kind="project",
            project_name="MyApp",
            owner_id="admin",
        )
        initial_usage = {
            "input_tokens": 1400,
            "output_tokens": 620,
            "total_tokens": 2020,
            "code_handoffs": [
                {
                    "target_chat_id": "code-sub-1",
                    "target_title": "Подзадача",
                    "send_count": 1,
                }
            ],
        }
        msg = await self.database.add_message(
            chat["id"],
            user_id="admin",
            role="assistant",
            content="## Отчёт\nВсе файлы обновлены.",
            usage=initial_usage,
        )
        msg_id = int(msg["id"])

        # 1. First return without target_msg_id (e.g. client fallback before server msg id arrives)
        res1 = await self.database.append_message_hq_return(
            chat["id"],
            msg_id,
            {
                "target_chat_id": "hq-chat-1",
                "target_title": "ШТАБ — MyApp",
                "target_icon": "🏠",
                "project_name": "MyApp",
                "return_uid": "ret_abc_001",
            },
        )
        self.assertIsNotNone(res1)
        assert res1 is not None
        self.assertEqual(len(res1), 1)
        self.assertEqual(res1[0]["target_chat_id"], "hq-chat-1")
        self.assertEqual(res1[0]["send_count"], 1)
        self.assertIsNone(res1[0]["target_msg_id"])
        self.assertEqual(res1[0]["return_uid"], "ret_abc_001")

        # 2. Idempotent enrichment with same return_uid sets target_msg_id WITHOUT incrementing send_count
        res_idem = await self.database.append_message_hq_return(
            chat["id"],
            msg_id,
            {
                "target_chat_id": "hq-chat-1",
                "target_title": "ШТАБ — MyApp",
                "target_icon": "🏠",
                "project_name": "MyApp",
                "target_msg_id": 777,
                "return_uid": "ret_abc_001",
            },
        )
        assert res_idem is not None
        self.assertEqual(len(res_idem), 1)
        self.assertEqual(res_idem[0]["send_count"], 1)
        self.assertEqual(res_idem[0]["target_msg_id"], 777)

        # 3. Enrichment with increment_count=False also keeps send_count == 1
        res_no_inc = await self.database.append_message_hq_return(
            chat["id"],
            msg_id,
            {
                "target_chat_id": "hq-chat-1",
                "target_title": "ШТАБ — MyApp (обновлён)",
                "increment_count": False,
            },
        )
        assert res_no_inc is not None
        self.assertEqual(res_no_inc[0]["send_count"], 1)
        self.assertEqual(res_no_inc[0]["target_msg_id"], 777)
        self.assertEqual(res_no_inc[0]["target_title"], "ШТАБ — MyApp (обновлён)")

        # 4. Second distinct return (new return_uid) increments send_count -> 2 and updates target_msg_id -> 888
        res2 = await self.database.append_message_hq_return(
            chat["id"],
            msg_id,
            {
                "target_chat_id": "hq-chat-1",
                "target_title": "ШТАБ — MyApp (обновлён)",
                "target_msg_id": 888,
                "return_uid": "ret_abc_002",
            },
        )
        assert res2 is not None
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2[0]["send_count"], 2)
        self.assertEqual(res2[0]["target_msg_id"], 888)
        self.assertEqual(res2[0]["return_uid"], "ret_abc_002")

        # 5. Sibling usage_json fields MUST remain 100% intact
        reloaded = await self.database.get_message_by_id(chat["id"], msg_id, include_thinking=False)
        assert reloaded is not None
        usage = reloaded["usage"]
        self.assertEqual(usage["input_tokens"], 1400)
        self.assertEqual(usage["output_tokens"], 620)
        self.assertEqual(usage["total_tokens"], 2020)
        self.assertEqual(len(usage["code_handoffs"]), 1)
        self.assertEqual(usage["code_handoffs"][0]["target_chat_id"], "code-sub-1")
        self.assertEqual(len(usage["hq_returns"]), 1)
        self.assertEqual(usage["hq_returns"][0]["send_count"], 2)

        # 6. Empty target_chat_id is ignored; wrong chat/message returns None
        res_empty = await self.database.append_message_hq_return(
            chat["id"], msg_id, {"target_chat_id": "   "}
        )
        assert res_empty is not None
        self.assertEqual(len(res_empty), 1)
        self.assertEqual(res_empty[0]["send_count"], 2)
        self.assertIsNone(
            await self.database.append_message_hq_return(
                "wrong-chat", msg_id, {"target_chat_id": "hq-chat-1"}
            )
        )
        self.assertIsNone(
            await self.database.append_message_hq_return(
                chat["id"], 999999, {"target_chat_id": "hq-chat-1"}
            )
        )

    async def test_02_rest_hq_return_rbac_and_ws_broadcast(self) -> None:
        """Verify POST /api/chats/{chat_id}/messages/{message_id}/hq-return Zero-Trust RBAC and WS event."""
        await self.database.init_db()
        from app import auth

        alice = await self.database.create_user(
            username="alice",
            password="AlicePassword123!",
            display_name="Alice",
            role="user",
            max_access_mode="full_access",
        )
        bob = await self.database.create_user(
            username="bob",
            password="BobPassword123!",
            display_name="Bob",
            role="user",
            max_access_mode="full_access",
        )
        alice_hdrs = {
            "Authorization": f"Bearer {auth.create_user_session_token(alice['id'], int(alice['session_version']))}"
        }
        bob_hdrs = {
            "Authorization": f"Bearer {auth.create_user_session_token(bob['id'], int(bob['session_version']))}"
        }

        alice_code = await self.database.create_chat(
            title="Alice Code", kind="chat", owner_id=alice["id"]
        )
        alice_hq = await self.database.create_chat(
            title="Alice HQ", kind="chat", icon="🏠", owner_id=alice["id"]
        )
        bob_priv_hq = await self.database.create_chat(
            title="Bob Secret HQ", kind="chat", owner_id=bob["id"]
        )
        a_msg = await self.database.add_message(
            alice_code["id"],
            user_id=alice["id"],
            role="assistant",
            content="Готово, все тесты зелёные.",
        )
        msg_id = int(a_msg["id"])

        client = _MiniAsgiClient(self.main_mod.app)

        with patch.object(
            self.main_mod.ws_manager, "broadcast_for_chat", new_callable=AsyncMock
        ) as mock_bcast:
            # 1. Foreign user Bob gets 403 on Alice's private chat
            r_bob_forbidden = await client.request(
                "POST",
                f"/api/chats/{alice_code['id']}/messages/{msg_id}/hq-return",
                json_body={"target_chat_id": bob_priv_hq["id"], "target_title": "Bob HQ"},
                headers=bob_hdrs,
            )
            self.assertEqual(r_bob_forbidden.status_code, 403)
            mock_bcast.assert_not_awaited()

            # 2. Alice cannot reference Bob's private chat as target_chat_id (Zero-Trust 403)
            r_foreign_target = await client.request(
                "POST",
                f"/api/chats/{alice_code['id']}/messages/{msg_id}/hq-return",
                json_body={"target_chat_id": bob_priv_hq["id"], "target_title": "Spoof"},
                headers=alice_hdrs,
            )
            self.assertEqual(r_foreign_target.status_code, 403)
            mock_bcast.assert_not_awaited()

            # 3. Empty target_chat_id -> 400
            r_bad = await client.request(
                "POST",
                f"/api/chats/{alice_code['id']}/messages/{msg_id}/hq-return",
                json_body={"target_chat_id": "   "},
                headers=alice_hdrs,
            )
            self.assertEqual(r_bad.status_code, 400)

            # 4. Missing chat / missing message -> 404
            r_no_msg = await client.request(
                "POST",
                f"/api/chats/{alice_code['id']}/messages/999999/hq-return",
                json_body={"target_chat_id": alice_hq["id"]},
                headers=alice_hdrs,
            )
            self.assertEqual(r_no_msg.status_code, 404)

            r_no_chat = await client.request(
                "POST",
                f"/api/chats/nonexistent-chat/messages/{msg_id}/hq-return",
                json_body={"target_chat_id": alice_hq["id"]},
                headers=alice_hdrs,
            )
            self.assertEqual(r_no_chat.status_code, 404)

            # 5. Owner Alice records HQ return -> 200 + WS broadcast `message_hq_return_updated`
            r_ok = await client.request(
                "POST",
                f"/api/chats/{alice_code['id']}/messages/{msg_id}/hq-return",
                json_body={
                    "target_chat_id": alice_hq["id"],
                    "target_title": "Alice HQ",
                    "target_icon": "🏠",
                    "target_msg_id": 42,
                    "return_uid": "ret_rest_1",
                },
                headers=alice_hdrs,
            )
            self.assertEqual(r_ok.status_code, 200, r_ok.text)
            body = r_ok.json()
            self.assertTrue(body["ok"])
            self.assertEqual(len(body["hq_returns"]), 1)
            self.assertEqual(body["hq_returns"][0]["target_chat_id"], alice_hq["id"])
            self.assertEqual(body["hq_returns"][0]["target_msg_id"], 42)
            self.assertEqual(body["hq_returns"][0]["send_count"], 1)

            mock_bcast.assert_awaited_once()
            b_args, b_kwargs = mock_bcast.await_args
            self.assertEqual(b_args[0]["id"], alice_code["id"])
            self.assertEqual(
                b_args[1],
                {
                    "type": "message_hq_return_updated",
                    "chat_id": alice_code["id"],
                    "message_id": msg_id,
                    "hq_returns": body["hq_returns"],
                },
            )
            self.assertIsNone(b_kwargs.get("shared_member_ids"))

        # 6. Shared project chat: member Bob IS allowed to record HQ return
        sp = await self.database.create_shared_project(
            name="SharedApp",
            description="Team workspace",
            member_ids=[alice["id"], bob["id"]],
            created_by="admin",
        )
        sh_code = await self.database.create_chat(
            title="Вкладка кода SharedApp",
            kind="project",
            project_name="SharedApp",
            project_path=str(sp["path"]),
            owner_id=alice["id"],
        )
        sh_hq = await self.database.create_chat(
            title="ШТАБ SharedApp",
            kind="project",
            project_name="SharedApp",
            project_path=str(sp["path"]),
            icon="🏠",
            owner_id=alice["id"],
        )
        sh_msg = await self.database.add_message(
            sh_code["id"],
            user_id=alice["id"],
            role="assistant",
            content="Реализовано по ТЗ.",
        )

        with patch.object(
            self.main_mod.ws_manager, "broadcast_for_chat", new_callable=AsyncMock
        ) as mock_sh_bcast:
            r_member = await client.request(
                "POST",
                f"/api/chats/{sh_code['id']}/messages/{sh_msg['id']}/hq-return",
                json_body={
                    "target_chat_id": sh_hq["id"],
                    "target_title": "ШТАБ SharedApp",
                    "return_uid": "ret_shared_1",
                },
                headers=bob_hdrs,
            )
            self.assertEqual(r_member.status_code, 200, r_member.text)
            mock_sh_bcast.assert_awaited_once()
            _, sh_kwargs = mock_sh_bcast.await_args
            shared_members = sh_kwargs.get("shared_member_ids") or set()
            self.assertIn(alice["id"], shared_members)
            self.assertIn(bob["id"], shared_members)

    async def test_03_turn_handoff_meta_and_atomic_hq_return_source(self) -> None:
        """Verify sanitizers and _run_turn_task_inner persistence of handoff_meta + atomic hq_returns.target_msg_id."""
        await self.database.init_db()

        # Unit-check sanitizers
        self.assertIsNone(self.main_mod._sanitize_handoff_meta(None))
        self.assertIsNone(self.main_mod._sanitize_handoff_meta({"kind": "unknown_kind"}))
        clean_spec = self.main_mod._sanitize_handoff_meta(
            {
                "kind": "code_spec",
                "source_chat_id": "hq-1",
                "source_title": "ШТАБ",
                "source_msg_id": "55",
                "or_review": 1,
                "or_council": False,
                "files_count": 999999,
            }
        )
        assert clean_spec is not None
        self.assertEqual(clean_spec["kind"], "code_spec")
        self.assertEqual(clean_spec["source_msg_id"], 55)
        self.assertTrue(clean_spec["or_review"])
        self.assertFalse(clean_spec["or_council"])
        self.assertNotIn("files_count", clean_spec)

        clean_rep = self.main_mod._sanitize_handoff_meta(
            {
                "kind": "hq_report",
                "source_chat_id": "code-1",
                "source_title": "Реализация",
                "source_model": "GPT-5.4",
                "files_count": 999999,
                "user_note": "  Всё проверил  ",
            }
        )
        assert clean_rep is not None
        self.assertEqual(clean_rep["kind"], "hq_report")
        self.assertEqual(clean_rep["model_label"], "GPT-5.4")
        self.assertEqual(clean_rep["files_count"], 99999)
        self.assertEqual(clean_rep["user_note"], "Всё проверил")

        self.assertIsNone(self.main_mod._sanitize_hq_return_source({"chat_id": "x", "message_id": 0}))
        clean_src = self.main_mod._sanitize_hq_return_source(
            {"chat_id": "code-1", "message_id": "12", "return_uid": "ret_xyz"}
        )
        self.assertEqual(
            clean_src, {"chat_id": "code-1", "message_id": 12, "return_uid": "ret_xyz"}
        )

        # Integration check of _run_turn_task_inner inside temp DB with mocked CLI stream
        alice = await self.database.create_user(
            username="alice2",
            password="AlicePassword123!",
            display_name="Alice",
            role="user",
            max_access_mode="full_access",
        )
        bob = await self.database.create_user(
            username="bob2",
            password="BobPassword123!",
            display_name="Bob",
            role="user",
            max_access_mode="full_access",
        )
        code_chat = await self.database.create_chat(
            title="Реализация UI", kind="chat", icon="💻", owner_id=alice["id"]
        )
        hq_chat = await self.database.create_chat(
            title="ШТАБ", kind="chat", icon="🏠", owner_id=alice["id"]
        )
        bob_secret = await self.database.create_chat(
            title="Bob Private", kind="chat", owner_id=bob["id"]
        )
        src_assistant = await self.database.add_message(
            code_chat["id"],
            user_id=alice["id"],
            role="assistant",
            content="## Отчёт\nВёрстка конвертов завершена.",
            usage={"input_tokens": 500, "output_tokens": 200},
        )
        src_msg_id = int(src_assistant["id"])

        async def _fake_agy_stream(**_kwargs: Any):
            yield {
                "type": "done",
                "content": "Отчёт принят в Штабе!",
                "thinking": "",
                "tool_calls": [],
                "usage": {"input_tokens": 300, "output_tokens": 80, "total_tokens": 380},
            }

        report_prompt = (
            "Комментарий пользователя:\nПроверь адаптив на 360px\n\n"
            "[Отчёт о выполнении из рабочей вкладки «Реализация UI» (GPT-5.4)]\n"
            "Статус хода: выполнено · Изменено файлов: 2 (`app.js`, `styles.css`)\n\n"
            "Пожалуйста, проверь результат относительно нашего плана и подскажи следующий шаг.\n\n"
            "---\n\n## Отчёт\nВёрстка конвертов завершена."
        )

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
            ) as mock_bcast,
        ):
            await self.main_mod._run_turn_task_inner(
                chat_id=hq_chat["id"],
                prompt=report_prompt,
                attachments=[],
                override_model="gemini-4-carbon-low",
                override_access_mode="read_only",
                trigger_user=alice,
                handoff_meta={
                    "kind": "hq_report",
                    "source_chat_id": code_chat["id"],
                    "source_title": "Реализация UI",
                    "source_icon": "💻",
                    "source_msg_id": src_msg_id,
                    "source_model": "GPT-5.4",
                    "status_label": "выполнено",
                    "files_count": 2,
                    "commands_count": 1,
                    "tool_steps_count": 4,
                    "user_note": "Проверь адаптив на 360px",
                },
                hq_return_source={
                    "chat_id": code_chat["id"],
                    "message_id": src_msg_id,
                    "return_uid": "ret_turn_001",
                },
            )

            # Verify HQ user message persisted full content + handoff_meta
            hq_msgs = await self.database.get_messages(hq_chat["id"], include_thinking=False)
            user_msgs = [m for m in hq_msgs if m["role"] == "user"]
            self.assertEqual(len(user_msgs), 1)
            hq_user_msg = user_msgs[0]
            self.assertEqual(hq_user_msg["content"], report_prompt)
            hm = hq_user_msg["usage"].get("handoff_meta")
            self.assertIsNotNone(hm)
            self.assertEqual(hm["kind"], "hq_report")
            self.assertEqual(hm["source_chat_id"], code_chat["id"])
            self.assertEqual(hm["source_msg_id"], src_msg_id)
            self.assertEqual(hm["files_count"], 2)
            self.assertEqual(hm["user_note"], "Проверь адаптив на 360px")

            # Verify source assistant message atomically received hq_returns with target_msg_id == hq_user_msg["id"]
            updated_src = await self.database.get_message_by_id(
                code_chat["id"], src_msg_id, include_thinking=False
            )
            assert updated_src is not None
            self.assertEqual(updated_src["usage"]["input_tokens"], 500)
            hq_rets = updated_src["usage"].get("hq_returns") or []
            self.assertEqual(len(hq_rets), 1)
            self.assertEqual(hq_rets[0]["target_chat_id"], hq_chat["id"])
            self.assertEqual(hq_rets[0]["target_msg_id"], int(hq_user_msg["id"]))
            self.assertEqual(hq_rets[0]["send_count"], 1)
            self.assertEqual(hq_rets[0]["return_uid"], "ret_turn_001")

            # Verify WS broadcast included message_hq_return_updated on code_chat
            hq_ret_events = [
                c.args[1]
                for c in mock_bcast.await_args_list
                if isinstance(c.args[1], dict)
                and c.args[1].get("type") == "message_hq_return_updated"
            ]
            self.assertEqual(len(hq_ret_events), 1)
            self.assertEqual(hq_ret_events[0]["chat_id"], code_chat["id"])
            self.assertEqual(hq_ret_events[0]["message_id"], src_msg_id)
            self.assertEqual(
                hq_ret_events[0]["hq_returns"][0]["target_msg_id"], int(hq_user_msg["id"])
            )

            # Verify Zero-Trust stripping if Alice passes Bob's private source_chat_id in handoff_meta
            await self.main_mod._run_turn_task_inner(
                chat_id=hq_chat["id"],
                prompt="Реализуй утверждённый план из диалога «Чужой»:\n\n---\n\n## План",
                attachments=[],
                override_model="gemini-4-carbon-low",
                override_access_mode="read_only",
                trigger_user=alice,
                handoff_meta={
                    "kind": "code_spec",
                    "source_chat_id": bob_secret["id"],
                    "source_title": "Чужой",
                    "source_msg_id": 99,
                },
            )
            hq_msgs_after = await self.database.get_messages(hq_chat["id"], include_thinking=False)
            second_user_msg = [m for m in hq_msgs_after if m["role"] == "user"][-1]
            hm2 = second_user_msg["usage"]["handoff_meta"]
            self.assertEqual(hm2["source_chat_id"], "")
            self.assertIsNone(hm2["source_msg_id"])
            self.assertEqual(hm2["source_title"], "Чужой")

    def test_04_frontend_envelope_parser_and_outline_in_node_vm(self) -> None:
        """Run `node --check` and verify `parseForwardedEnvelope`, `mergeHqReturnLists`, and Outline titles in Node VM."""
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
const dummyEl = () => ({
  classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
  addEventListener: noop,
  removeEventListener: noop,
  setAttribute: noop,
  getAttribute: () => "",
  querySelector: () => null,
  querySelectorAll: () => [],
  style: { setProperty: noop, removeProperty: noop },
  dataset: {},
  value: "",
  textContent: "",
  innerHTML: "",
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
    getElementById: () => dummyEl(),
    querySelector: () => dummyEl(),
    querySelectorAll: () => [],
    createElement: () => dummyEl(),
    createDocumentFragment: () => ({ appendChild: noop }),
    addEventListener: noop,
    documentElement: { setAttribute: noop, getAttribute: () => "", style: { setProperty: noop } },
    body: dummyEl(),
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
assert.strictEqual(typeof api.parseForwardedEnvelope, "function");
assert.strictEqual(typeof api.mergeHqReturnLists, "function");
assert.strictEqual(typeof api.buildMessageHqReturnsHtml, "function");

// 1. Ordinary user messages MUST return null (zero false positives)
assert.strictEqual(
  api.parseForwardedEnvelope({
    role: "user",
    content: "Привет! В диалоге «ШТАБ» мы обсуждали план и отчёт о выполнении — что думаешь?",
  }),
  null
);
assert.strictEqual(
  api.parseForwardedEnvelope({
    role: "assistant",
    content: "Реализуй утверждённый план из диалога «ШТАБ»:\n\n---\n\n# ТЗ",
  }),
  null
);

// 2. Retroactive Code Spec (without usage.handoff_meta) + OpenRouter boosters
const specText = api.buildCodeHandoffPrompt(
  "# ТЗ (Релиз v=100): Квитанции возврата и компактные конверты\n\nНужно свернуть длинные простыни ТЗ и отчётов в аккуратные карточки.",
  "ШТАБ — Antigravity Hub",
  { orReview: true, orCouncil: true }
);
const retroSpecEnv = api.parseForwardedEnvelope({
  role: "user",
  content: specText,
  usage: {},
});
assert.ok(retroSpecEnv, "Retroactive code_spec must be recognized");
assert.strictEqual(retroSpecEnv.kind, "code_spec");
assert.strictEqual(retroSpecEnv.sourceTitle, "ШТАБ — Antigravity Hub");
assert.strictEqual(retroSpecEnv.orReview, true);
assert.strictEqual(retroSpecEnv.orCouncil, true);
assert.ok(
  retroSpecEnv.headline.includes("ТЗ (Релиз v=100)"),
  `Unexpected spec headline: ${retroSpecEnv.headline}`
);
assert.ok(
  retroSpecEnv.bodyMarkdown.startsWith("# ТЗ (Релиз v=100)"),
  "Boilerplate instructions before --- must be stripped from bodyMarkdown"
);

// 2b. Desktop wrapper (<PROJECT_PULSE>...) around retroactive Code Spec
const wrappedDesktopSpec = api.parseForwardedEnvelope({
  role: "user",
  content:
    "<PROJECT_PULSE>\nProject: Antigravity_Hub\n</PROJECT_PULSE>\n\n" + specText,
});
assert.ok(wrappedDesktopSpec, "Desktop-wrapped code_spec must be recognized");
assert.strictEqual(wrappedDesktopSpec.kind, "code_spec");
assert.strictEqual(wrappedDesktopSpec.sourceTitle, "ШТАБ — Antigravity Hub");

// 3. New Code Spec WITH explicit usage.handoff_meta (preserves sourceChatId + sourceMsgId)
const metaSpecEnv = api.parseForwardedEnvelope({
  role: "user",
  content: specText,
  usage: {
    handoff_meta: {
      kind: "code_spec",
      source_chat_id: "hq-chat-42",
      source_title: "ШТАБ — Antigravity Hub",
      source_icon: "🏠",
      source_msg_id: 314,
      or_review: true,
      or_council: false,
    },
  },
});
assert.strictEqual(metaSpecEnv.kind, "code_spec");
assert.strictEqual(metaSpecEnv.sourceChatId, "hq-chat-42");
assert.strictEqual(metaSpecEnv.sourceMsgId, 314);
assert.strictEqual(metaSpecEnv.orReview, true);
assert.strictEqual(metaSpecEnv.orCouncil, false);

// 4. Retroactive HQ Report WITHOUT user comment
const rawReportText = api.buildReturnToHqPrompt(
  {
    id: 501,
    role: "assistant",
    model: "gpt-5.4-high",
    status: "done",
    content: "## Итоги реализации v=100\n\nВсе 6 файлов обновлены, тесты прошли успешно.",
    tool_calls: [
      { name: "edit_file", args: { Path: "D:/Hub/app/main.py" }, status: "done" },
      { name: "edit_file", args: { Path: "D:/Hub/app/static/app.js" }, status: "done" },
      { name: "run_command", args: { CommandLine: "pytest -q" }, status: "done" },
    ],
  },
  { id: "code-tab-9", title: "Реализация — Конверты v100" }
);
const retroReportEnv = api.parseForwardedEnvelope({
  role: "user",
  content: rawReportText,
  usage: {},
});
assert.ok(retroReportEnv, "Retroactive hq_report must be recognized");
assert.strictEqual(retroReportEnv.kind, "hq_report");
assert.strictEqual(retroReportEnv.sourceTitle, "Реализация — Конверты v100");
assert.strictEqual(retroReportEnv.userNote, "");
assert.strictEqual(retroReportEnv.filesCount, 2);
assert.strictEqual(retroReportEnv.commandsCount, 1);
assert.ok(retroReportEnv.headline.includes("Итоги реализации v=100"));
assert.ok(retroReportEnv.bodyMarkdown.startsWith("## Итоги реализации v=100"));

// 5. HQ Report WITH clean user comment («Комментарий пользователя:\n...») + handoff_meta
const commentedReportText =
  "Комментарий пользователя:\nПроверь, не сломалось ли оглавление на мобилке!\n\n" +
  rawReportText;
const commentedReportEnv = api.parseForwardedEnvelope({
  role: "user",
  content: commentedReportText,
  usage: {
    handoff_meta: {
      kind: "hq_report",
      source_chat_id: "code-tab-9",
      source_title: "Реализация — Конверты v100",
      source_icon: "💻",
      source_msg_id: 501,
      source_model: "GPT-5.4 (High)",
      status_label: "выполнено",
      files_count: 2,
      commands_count: 1,
      tool_steps_count: 3,
      user_note: "Проверь, не сломалось ли оглавление на мобилке!",
    },
  },
});
assert.strictEqual(commentedReportEnv.kind, "hq_report");
assert.strictEqual(
  commentedReportEnv.userNote,
  "Проверь, не сломалось ли оглавление на мобилке!"
);
assert.strictEqual(commentedReportEnv.sourceChatId, "code-tab-9");
assert.strictEqual(commentedReportEnv.sourceMsgId, 501);
assert.strictEqual(commentedReportEnv.toolStepsCount, 3);
assert.ok(!commentedReportEnv.bodyMarkdown.includes("Комментарий пользователя:"));

// 5b. Retroactive HQ Report WITH user comment and WITHOUT handoff_meta
const retroCommentedEnv = api.parseForwardedEnvelope({
  role: "user",
  content: commentedReportText,
  usage: {},
});
assert.strictEqual(retroCommentedEnv.kind, "hq_report");
assert.strictEqual(
  retroCommentedEnv.userNote,
  "Проверь, не сломалось ли оглавление на мобилке!"
);
assert.ok(retroCommentedEnv.bodyMarkdown.startsWith("## Итоги реализации v=100"));

// 6. Smart Outline integration: user comment becomes the primary outline title «↩ Отчёт: <комментарий>»
const outlineWithComment = api.extractSmartOutlineTurnTitle({
  role: "user",
  content: commentedReportText,
});
assert.strictEqual(
  outlineWithComment.title,
  "↩ Отчёт: Проверь, не сломалось ли оглавление на мобилке!"
);

const outlinePlainReport = api.extractSmartOutlineTurnTitle({
  role: "user",
  content: rawReportText,
});
assert.ok(
  outlinePlainReport.title.startsWith("↩ Отчёт: "),
  `Expected ↩ Отчёт: prefix, got: ${outlinePlainReport.title}`
);

// 7. mergeHqReturnLists + buildMessageHqReturnsHtml preserve target_msg_id & render jump attributes
const mergedRets = api.mergeHqReturnLists(
  [{ target_chat_id: "hq-1", target_title: "ШТАБ", send_count: 1, return_uid: "u1" }],
  [{ target_chat_id: "hq-1", target_title: "ШТАБ", send_count: 2, target_msg_id: 909, return_uid: "u2" }]
);
assert.strictEqual(mergedRets.length, 1);
assert.strictEqual(mergedRets[0].send_count, 2);
assert.strictEqual(mergedRets[0].target_msg_id, 909);

const chipsHtml = api.buildMessageHqReturnsHtml(mergedRets);
assert.ok(chipsHtml.includes('data-open-hq-return-chat="hq-1"'));
assert.ok(chipsHtml.includes('data-open-hq-return-msg="909"'));
assert.ok(chipsHtml.includes("×2"));

console.log("NODE_V100_ENVELOPES_OK");
"""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", encoding="utf-8", delete=False
        ) as tf:
            tf.write(node_harness)
            harness_path = Path(tf.name)

        try:
            run_res = subprocess.run(
                ["node", str(harness_path), str(ROOT_DIR / "app/static/app.js")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=25,
                env=env,
                **subprocess_hidden_kwargs(),
            )
            self.assertEqual(
                run_res.returncode,
                0,
                f"Node v100 envelope tests failed:\nSTDOUT:\n{run_res.stdout}\nSTDERR:\n{run_res.stderr}",
            )
            self.assertIn("NODE_V100_ENVELOPES_OK", run_res.stdout)
        finally:
            harness_path.unlink(missing_ok=True)

    def test_05_static_v100_cache_bom_and_css_invariants(self) -> None:
        """Verify v>=100 cache sync, #pendingHqReturnPreview DOM order, UTF-8 without BOM, and main.workspace CSS."""
        checked_files = (
            "app/database.py",
            "app/main.py",
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/app.js",
            "app/static/sw.js",
            "tests/test_handoff_envelopes_v100.py",
        )
        for rel in checked_files:
            p = ROOT_DIR / rel
            raw = p.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM found in {rel}")
            raw.decode("utf-8")

        html_txt = (ROOT_DIR / "app/static/index.html").read_text(encoding="utf-8")
        sw_txt = (ROOT_DIR / "app/static/sw.js").read_text(encoding="utf-8")
        css_txt = (ROOT_DIR / "app/static/styles.css").read_text(encoding="utf-8")

        m_sw_cache = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_txt)
        m_html_css = re.search(r"/static/styles\.css\?v=(\d+)", html_txt)
        m_html_js = re.search(r"/static/app\.js\?v=(\d+)", html_txt)
        m_sw_css = re.search(r"/static/styles\.css\?v=(\d+)", sw_txt)
        m_sw_js = re.search(r"/static/app\.js\?v=(\d+)", sw_txt)

        for m in (m_sw_cache, m_html_css, m_html_js, m_sw_css, m_sw_js):
            self.assertIsNotNone(m)
            assert m is not None
            self.assertGreaterEqual(int(m.group(1)), 100)

        assert m_sw_cache and m_html_css and m_html_js and m_sw_css and m_sw_js
        versions = {
            int(m_sw_cache.group(1)),
            int(m_html_css.group(1)),
            int(m_html_js.group(1)),
            int(m_sw_css.group(1)),
            int(m_sw_js.group(1)),
        }
        self.assertEqual(len(versions), 1, f"Version mismatch across index.html and sw.js: {versions}")

        # Ensure #pendingHqReturnPreview exists inside #composerBox before #referencedChatsPreview
        self.assertIn('id="pendingHqReturnPreview"', html_txt)
        composer_idx = html_txt.index('id="composerBox"')
        pending_hq_idx = html_txt.index('id="pendingHqReturnPreview"')
        ref_chats_idx = html_txt.index('id="referencedChatsPreview"')
        self.assertGreater(pending_hq_idx, composer_idx)
        self.assertLess(pending_hq_idx, ref_chats_idx)

        # Ensure key v100 CSS selectors exist
        for sel in (
            ".msg-return-to-hq-btn.is-staged-to-hq",
            ".msg-return-to-hq-btn.is-sent-to-hq",
            ".msg-hq-return-chip",
            ".pending-hq-return-preview",
            ".handoff-envelope-card",
            ".hec-user-note-bubble",
        ):
            self.assertIn(sel, css_txt, f"Missing CSS selector {sel}")

        # Verify `main.workspace` has zero forbidden containing-block / stacking-context properties
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
                    f"Forbidden CSS property '{prop}' found inside main.workspace!",
                )


if __name__ == "__main__":
    unittest.main()
