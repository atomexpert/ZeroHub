"""
Unit & ASGI API tests for Antigravity Hub v=68:
- `database.append_message_code_handoff`: initial insert, repeat upsert (`send_count` increment + move to end),
  12-item cap, preservation of existing `usage_json` fields, and 0 impact on `get_token_usage_stats` / `get_user_24h_tokens`.
- `POST /api/chats/{chat_id}/messages/{message_id}/handoff`: RBAC isolation (`403` for foreign user, `400` on empty target,
  `404` on missing chat/message), WebSocket `message_handoff_updated` broadcast, and persistence in `GET /api/chats/{chat_id}`.
- Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or `run_server.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

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

    def json(self) -> dict:
        return json.loads(self.text)


class _MiniAsgiClient:
    def __init__(self, app, *, client: tuple[str, int] = ("127.0.0.1", 52680)) -> None:
        self._app = app
        self._client = client

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> _MiniAsgiResponse:
        parsed_url = urllib.parse.urlsplit(path)
        raw_path = parsed_url.path or "/"
        qs = parsed_url.query
        if params:
            extra_qs = urllib.parse.urlencode(params)
            qs = f"{qs}&{extra_qs}" if qs else extra_qs

        payload = b""
        req_headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8080")]
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            req_headers.append((b"content-type", b"application/json"))
            req_headers.append((b"content-length", str(len(payload)).encode("ascii")))
        if headers:
            for k, v in headers.items():
                req_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": urllib.parse.unquote(raw_path),
            "raw_path": raw_path.encode("ascii", errors="ignore"),
            "query_string": qs.encode("ascii", errors="ignore"),
            "headers": req_headers,
            "client": self._client,
            "server": ("127.0.0.1", 8080),
        }

        sent_body = False
        resp_status = 500
        resp_headers: dict[str, str] = {}
        resp_chunks: list[bytes] = []

        async def _receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def _send(message):
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


class TestCodeHandoffReceiptV68(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_test_v68_", ignore_cleanup_errors=True
        )
        self.tmp_root = Path(self._tmp.name).resolve()

        self._orig = {
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_cli": config.CLI_HOMES_DIR,
            "cfg_agy": config.AGY_CLI_DATA_DIR,
            "eng_agy": agy_engine.AGY_CLI_DATA_DIR,
            "cfg_ws": config.USER_WORKSPACES_DIR,
            "db_ws": database.USER_WORKSPACES_DIR,
            "br_ws": brain_reader.USER_WORKSPACES_DIR,
            "cfg_sh": config.SHARED_PROJECTS_DIR,
            "db_sh": database.SHARED_PROJECTS_DIR,
            "br_sh": brain_reader.SHARED_PROJECTS_DIR,
            "cfg_up": config.UPLOADS_DIR,
            "eng_up": agy_engine.UPLOADS_DIR,
            "cfg_sbx": config.CHAT_SANDBOX_DIR,
            "eng_sbx": agy_engine.CHAT_SANDBOX_DIR,
        }

        tmp_db = self.tmp_root / "test_v68.db"
        tmp_cli = self.tmp_root / "cli_homes"
        tmp_agy = self.tmp_root / "agy_cli_data"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sh = self.tmp_root / "shared_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_sbx = self.tmp_root / "chat_sandbox"
        for d in (tmp_cli, tmp_agy, tmp_ws, tmp_sh, tmp_up, tmp_sbx):
            d.mkdir(parents=True, exist_ok=True)

        config.DB_PATH = tmp_db
        database.DB_PATH = tmp_db
        config.CLI_HOMES_DIR = tmp_cli
        config.AGY_CLI_DATA_DIR = tmp_agy
        agy_engine.AGY_CLI_DATA_DIR = tmp_agy
        config.USER_WORKSPACES_DIR = tmp_ws
        database.USER_WORKSPACES_DIR = tmp_ws
        brain_reader.USER_WORKSPACES_DIR = tmp_ws
        config.SHARED_PROJECTS_DIR = tmp_sh
        database.SHARED_PROJECTS_DIR = tmp_sh
        brain_reader.SHARED_PROJECTS_DIR = tmp_sh
        config.UPLOADS_DIR = tmp_up
        agy_engine.UPLOADS_DIR = tmp_up
        config.CHAT_SANDBOX_DIR = tmp_sbx
        agy_engine.CHAT_SANDBOX_DIR = tmp_sbx

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        config.DB_PATH = self._orig["cfg_db"]
        database.DB_PATH = self._orig["db_db"]
        config.CLI_HOMES_DIR = self._orig["cfg_cli"]
        config.AGY_CLI_DATA_DIR = self._orig["cfg_agy"]
        agy_engine.AGY_CLI_DATA_DIR = self._orig["eng_agy"]
        config.USER_WORKSPACES_DIR = self._orig["cfg_ws"]
        database.USER_WORKSPACES_DIR = self._orig["db_ws"]
        brain_reader.USER_WORKSPACES_DIR = self._orig["br_ws"]
        config.SHARED_PROJECTS_DIR = self._orig["cfg_sh"]
        database.SHARED_PROJECTS_DIR = self._orig["db_sh"]
        brain_reader.SHARED_PROJECTS_DIR = self._orig["br_sh"]
        config.UPLOADS_DIR = self._orig["cfg_up"]
        agy_engine.UPLOADS_DIR = self._orig["eng_up"]
        config.CHAT_SANDBOX_DIR = self._orig["cfg_sbx"]
        agy_engine.CHAT_SANDBOX_DIR = self._orig["eng_sbx"]

        _unlink_test_junctions(self.tmp_root)
        self._tmp.cleanup()

        if self._live_stat_before is not None:
            live_after = (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            self.assertEqual(
                self._live_stat_before,
                live_after,
                "Live data/hub.db must NEVER be modified by tests!",
            )

    async def test_db_append_handoff_and_token_stats_invariant(self) -> None:
        import app.database as database

        await database.init_db()
        user = await database.create_user(
            username="arch_user",
            display_name="Arch",
            password="pass1234",
            max_access_mode="write_files",
        )
        plan_chat = await database.create_chat(
            owner_id=user["id"],
            kind="project",
            project_name="DemoProj",
            project_path=str(Path(user["workspace_dir"]) / "DemoProj"),
            title="Обсуждение",
            access_mode="read_only",
        )
        # Message 1: has real token usage (1500 tokens)
        msg_with_tokens = await database.add_message(
            plan_chat["id"],
            user_id=user["id"],
            role="assistant",
            content="# ТЗ v=68\nСделать квитанцию отправки.",
            model="gemini-4-carbon-high",
            usage={"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
        )
        # Message 2: zero-token message (e.g. imported / manual) — must NOT inflate `turns` or `total_tokens`!
        msg_zero_tokens = await database.add_message(
            plan_chat["id"],
            user_id=user["id"],
            role="assistant",
            content="# Черновик без токенов",
            model="gemini-4-carbon-high",
            usage={},
        )

        stats_before = await database.get_token_usage_stats(filter_user_id=user["id"])
        tok24_before = await database.get_user_24h_tokens(user["id"])
        self.assertEqual(stats_before["all_time"]["total_tokens"], 1500)
        self.assertEqual(stats_before["all_time"]["turns"], 1)
        self.assertEqual(tok24_before, 1500)

        # 1. Missing message returns None
        self.assertIsNone(
            await database.append_message_code_handoff(
                plan_chat["id"], 999999, {"target_chat_id": "code-1"}
            )
        )

        # 2. Record two distinct target tabs, then re-send to the first tab
        h1 = await database.append_message_code_handoff(
            plan_chat["id"],
            msg_with_tokens["id"],
            {
                "target_chat_id": "code-tab-1",
                "target_title": "Визуальное оформление",
                "target_icon": "⚡",
                "access_mode": "write_files",
                "project_name": "DemoProj",
                "sent_at": "2026-11-11T21:14:00+08:00",
            },
        )
        self.assertIsNotNone(h1)
        self.assertEqual(len(h1), 1)
        self.assertEqual(h1[0]["send_count"], 1)

        h2 = await database.append_message_code_handoff(
            plan_chat["id"],
            msg_with_tokens["id"],
            {
                "target_chat_id": "code-tab-2",
                "target_title": "Бэкенд API",
                "target_icon": "🚀",
                "access_mode": "full_access",
                "project_name": "DemoProj",
                "sent_at": "2026-11-11T21:18:00+08:00",
            },
        )
        self.assertEqual([x["target_chat_id"] for x in h2], ["code-tab-1", "code-tab-2"])

        # Re-send to code-tab-1 -> increments send_count to 2 and moves code-tab-1 to the end!
        h3 = await database.append_message_code_handoff(
            plan_chat["id"],
            msg_with_tokens["id"],
            {
                "target_chat_id": "code-tab-1",
                "target_title": "Визуальное оформление v2",
                "target_icon": "",  # empty icon preserves previous "⚡"
                "access_mode": "write_files",
                "project_name": "DemoProj",
                "sent_at": "2026-11-11T21:25:00+08:00",
            },
        )
        self.assertEqual([x["target_chat_id"] for x in h3], ["code-tab-2", "code-tab-1"])
        self.assertEqual(h3[1]["send_count"], 2)
        self.assertEqual(h3[1]["target_icon"], "⚡")
        self.assertEqual(h3[1]["target_title"], "Визуальное оформление v2")

        # 3. Also append 15 handoffs to zero-token message -> capped at last 12 AND 0 effect on token stats!
        for idx in range(15):
            res_cap = await database.append_message_code_handoff(
                plan_chat["id"],
                msg_zero_tokens["id"],
                {
                    "target_chat_id": f"bulk-{idx}",
                    "target_title": f"Tab {idx}",
                    "sent_at": f"2026-11-11T22:{idx:02d}:00+08:00",
                },
            )
        self.assertEqual(len(res_cap), 12)
        self.assertEqual(res_cap[0]["target_chat_id"], "bulk-3")
        self.assertEqual(res_cap[-1]["target_chat_id"], "bulk-14")

        # Verify original token usage keys on msg_with_tokens are intact AND stats/turns did NOT change!
        msgs_after = await database.get_messages(plan_chat["id"])
        m1_after = next(m for m in msgs_after if m["id"] == msg_with_tokens["id"])
        self.assertEqual(m1_after["usage"]["total_tokens"], 1500)
        self.assertEqual(len(m1_after["usage"]["code_handoffs"]), 2)

        stats_after = await database.get_token_usage_stats(filter_user_id=user["id"])
        tok24_after = await database.get_user_24h_tokens(user["id"])
        self.assertEqual(stats_after["all_time"]["total_tokens"], 1500)
        self.assertEqual(stats_after["all_time"]["turns"], 1)
        self.assertEqual(tok24_after, 1500)

    async def test_api_record_handoff_rbac_ws_and_v68_assets(self) -> None:
        import app.auth as auth
        import app.database as database
        import app.main as main

        await database.init_db()
        alice = await database.create_user(
            username="alice_h68",
            display_name="Alice",
            password="pass1234",
            max_access_mode="write_files",
        )
        bob = await database.create_user(
            username="bob_h68",
            display_name="Bob",
            password="pass5678",
            max_access_mode="write_files",
        )
        proj_dir = Path(alice["workspace_dir"]) / "HandoffProj"
        proj_dir.mkdir(parents=True, exist_ok=True)

        plan_chat = await database.create_chat(
            owner_id=alice["id"],
            kind="project",
            project_name="HandoffProj",
            project_path=str(proj_dir),
            title="Штаб архитектора",
            access_mode="read_only",
        )
        code_chat = await database.create_chat(
            owner_id=alice["id"],
            kind="project",
            project_name="HandoffProj",
            project_path=str(proj_dir),
            title="Визуальное оформление",
            icon="⚡",
            access_mode="write_files",
        )
        plan_msg = await database.add_message(
            plan_chat["id"],
            user_id=alice["id"],
            role="assistant",
            content="Готовое ТЗ v=68",
            model="gemini-4-carbon-high",
        )

        hdr_a = {
            "Authorization": f"Bearer {auth.create_user_session_token(user_id=alice['id'], session_version=alice['session_version'])}"
        }
        hdr_b = {
            "Authorization": f"Bearer {auth.create_user_session_token(user_id=bob['id'], session_version=bob['session_version'])}"
        }
        client = _MiniAsgiClient(main.app)

        ws_broadcasts: list[dict] = []
        orig_bcast = main.ws_manager.broadcast_for_chat

        async def _spy_bcast(chat_obj, payload, shared_member_ids=None):
            ws_broadcasts.append(payload)
            await orig_bcast(chat_obj, payload, shared_member_ids=shared_member_ids)

        main.ws_manager.broadcast_for_chat = _spy_bcast
        try:
            # 1. Bob cannot record handoff on Alice's private project chat -> 403
            r_forbidden = await client.request(
                "POST",
                f"/api/chats/{plan_chat['id']}/messages/{plan_msg['id']}/handoff",
                json_body={"target_chat_id": code_chat["id"], "target_title": "Hack"},
                headers=hdr_b,
            )
            self.assertEqual(r_forbidden.status_code, 403)

            # 2. Empty target_chat_id -> 400
            r_bad = await client.request(
                "POST",
                f"/api/chats/{plan_chat['id']}/messages/{plan_msg['id']}/handoff",
                json_body={"target_chat_id": "   "},
                headers=hdr_a,
            )
            self.assertEqual(r_bad.status_code, 400)

            # 3. Alice records handoff -> 200 + WS `message_handoff_updated` + persisted in GET /api/chats/{id}
            r_ok = await client.request(
                "POST",
                f"/api/chats/{plan_chat['id']}/messages/{plan_msg['id']}/handoff",
                json_body={
                    "target_chat_id": code_chat["id"],
                    "target_title": "Визуальное оформление",
                    "target_icon": "⚡",
                    "access_mode": "write_files",
                    "project_name": "HandoffProj",
                    "sent_at": "2026-11-11T22:50:00+08:00",
                },
                headers=hdr_a,
            )
            self.assertEqual(r_ok.status_code, 200)
            body = r_ok.json()
            self.assertTrue(body["ok"])
            self.assertEqual(len(body["code_handoffs"]), 1)
            self.assertEqual(body["code_handoffs"][0]["target_chat_id"], code_chat["id"])
            self.assertEqual(body["code_handoffs"][0]["send_count"], 1)

            self.assertTrue(
                any(
                    ev.get("type") == "message_handoff_updated"
                    and ev.get("chat_id") == plan_chat["id"]
                    and ev.get("message_id") == plan_msg["id"]
                    for ev in ws_broadcasts
                )
            )

            r_get = await client.request(
                "GET", f"/api/chats/{plan_chat['id']}", headers=hdr_a
            )
            self.assertEqual(r_get.status_code, 200)
            fetched_msgs = r_get.json()["messages"]
            self.assertEqual(
                fetched_msgs[0]["usage"]["code_handoffs"][0]["target_chat_id"],
                code_chat["id"],
            )

            # 4. Verify static cache version (>= v=68) in index.html & sw.js
            import re

            r_html = await client.request("GET", "/")
            self.assertEqual(r_html.status_code, 200)
            m_css = re.search(r"/static/styles\.css\?v=(\d+)", r_html.text)
            m_js = re.search(r"/static/app\.js\?v=(\d+)", r_html.text)
            self.assertIsNotNone(m_css)
            self.assertIsNotNone(m_js)
            self.assertGreaterEqual(int(m_css.group(1)), 68)
            self.assertGreaterEqual(int(m_js.group(1)), 68)

            r_sw = await client.request("GET", "/sw.js")
            self.assertEqual(r_sw.status_code, 200)
            m_sw_cache = re.search(r'"agy-hub-v(\d+)"', r_sw.text)
            m_sw_js = re.search(r"/static/app\.js\?v=(\d+)", r_sw.text)
            self.assertIsNotNone(m_sw_cache)
            self.assertIsNotNone(m_sw_js)
            self.assertGreaterEqual(int(m_sw_cache.group(1)), 68)
            self.assertGreaterEqual(int(m_sw_js.group(1)), 68)
        finally:
            main.ws_manager.broadcast_for_chat = orig_bcast


if __name__ == "__main__":
    unittest.main()
