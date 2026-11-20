"""
Automated isolated verification suite for Release v98:
1. Live `data/hub.db` byte-level immutability guard (`st_mtime_ns`, `st_size`).
2. Backend SQLite schema & idempotent migration (`messages.custom_title`), explicit `SELECT`
   column coverage in `get_messages(..., include_thinking=False)` and
   `get_message_by_id(..., include_thinking=False)`, and `set_message_custom_title` normalization.
3. REST `PATCH /api/chats/{chat_id}/messages/{message_id}/title` + WebSocket `message_title_updated`
   broadcast + Zero-Trust RBAC (`403` foreign private chat, `200` shared project member, `404` missing).
4. Frontend smart turn-title distiller (`extractCleanUserTurnBody`, `stripConversationalLeadIn`,
   `extractSmartOutlineTurnTitle`, `buildChatOutlineModel`) in Node VM + `node --check` via
   `subprocess_hidden_kwargs()` with `0% GPU` env (`CUDA_VISIBLE_DEVICES=""`).
5. Static `v=98` cache sync (`index.html`, `sw.js`), `#outlineTurnPreviewCard` DOM placement,
   2-line `.outline-turn-title` clamp, UTF-8 without BOM, and `main.workspace` CSS invariant.

Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or live server ports.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
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
    def __init__(self, app: Any, *, client: tuple[str, int] = ("127.0.0.1", 53198)) -> None:
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


class TestReleaseV98OutlineSmartTitles(unittest.IsolatedAsyncioTestCase):
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

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v98_test_", ignore_cleanup_errors=True)
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

    async def test_01_idempotent_custom_title_migration_and_explicit_selects(self) -> None:
        """Verify legacy DB migration adds `messages.custom_title` and explicit SELECTs return it."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE chats (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'chat',
                    project_name TEXT,
                    project_path TEXT,
                    agy_conversation_id TEXT,
                    model TEXT NOT NULL DEFAULT '',
                    access_mode TEXT NOT NULL DEFAULT 'write_files',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    icon TEXT NOT NULL DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    user_id TEXT NOT NULL DEFAULT 'admin',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    thinking TEXT NOT NULL DEFAULT '',
                    tool_calls_json TEXT NOT NULL DEFAULT '[]',
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    model TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'done',
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    author_id TEXT NOT NULL DEFAULT '',
                    author_name TEXT NOT NULL DEFAULT '',
                    bookmarked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO chats (id, title, user_id) VALUES ('legacy-c1', 'Legacy Chat', 'admin');
                INSERT INTO messages (chat_id, role, content) VALUES ('legacy-c1', 'user', 'Старый вопрос?');
                """
            )
            conn.commit()
        finally:
            conn.close()

        # Run init_db twice to verify idempotency
        await self.database.init_db()
        await self.database.init_db()

        conn_chk = sqlite3.connect(self.db_path)
        try:
            cols = {r[1] for r in conn_chk.execute("PRAGMA table_info(messages)").fetchall()}
        finally:
            conn_chk.close()
        self.assertIn("custom_title", cols)

        # Verify explicit column lists in include_thinking=False queries
        msgs_light = await self.database.get_messages("legacy-c1", include_thinking=False)
        self.assertEqual(len(msgs_light), 1)
        self.assertIn("custom_title", msgs_light[0])
        self.assertEqual(msgs_light[0]["custom_title"], "")

        msg_id = int(msgs_light[0]["id"])
        single_light = await self.database.get_message_by_id(
            "legacy-c1", msg_id, include_thinking=False
        )
        self.assertIsNotNone(single_light)
        assert single_light is not None
        self.assertEqual(single_light["custom_title"], "")

        # Normalize whitespace + clamp to 140 chars
        raw_long = "   Мой   ручной\n\tзаголовок   " + ("А" * 160)
        updated = await self.database.set_message_custom_title("legacy-c1", msg_id, raw_long)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(len(updated["custom_title"]), 140)
        self.assertTrue(updated["custom_title"].startswith("Мой ручной заголовок "))
        self.assertNotIn("\n", updated["custom_title"])

        # Re-read via include_thinking=False
        reloaded = await self.database.get_message_by_id("legacy-c1", msg_id, include_thinking=False)
        assert reloaded is not None
        self.assertEqual(reloaded["custom_title"], updated["custom_title"])

        # Reset to automatic via empty string
        reset = await self.database.set_message_custom_title("legacy-c1", msg_id, "   ")
        assert reset is not None
        self.assertEqual(reset["custom_title"], "")

        # Wrong chat_id or nonexistent message_id -> None
        self.assertIsNone(await self.database.set_message_custom_title("other-chat", msg_id, "X"))
        self.assertIsNone(await self.database.set_message_custom_title("legacy-c1", 999999, "X"))

    async def test_02_patch_message_title_endpoint_rbac_and_ws_broadcast(self) -> None:
        """Verify PATCH /api/chats/{chat_id}/messages/{message_id}/title RBAC, persistence, and WS broadcast."""
        await self.database.init_db()

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
        from app import auth

        alice_token = auth.create_user_session_token(
            alice["id"], int(alice["session_version"])
        )
        bob_token = auth.create_user_session_token(
            bob["id"], int(bob["session_version"])
        )

        # 1. Alice's private chat
        priv_chat = await self.database.create_chat(
            title="Alice Private",
            kind="chat",
            owner_id=alice["id"],
        )
        u_msg = await self.database.add_message(
            priv_chat["id"],
            user_id=alice["id"],
            role="user",
            content="Капец. Что-то я не догоняю. Где конфиг?",
        )
        msg_id = int(u_msg["id"])

        client = _MiniAsgiClient(self.main_mod.app)
        alice_hdrs = {"Authorization": f"Bearer {alice_token}"}
        bob_hdrs = {"Authorization": f"Bearer {bob_token}"}

        with patch.object(
            self.main_mod.ws_manager, "broadcast_for_chat", new_callable=AsyncMock
        ) as mock_bcast:
            # Foreign user Bob gets 403 Zero-Trust
            r_forbidden = await client.request(
                "PATCH",
                f"/api/chats/{priv_chat['id']}/messages/{msg_id}/title",
                json_body={"custom_title": "Чужая правка"},
                headers=bob_hdrs,
            )
            self.assertEqual(r_forbidden.status_code, 403)
            mock_bcast.assert_not_awaited()

            # Owner Alice saves custom title (with whitespace & >140 truncation check)
            r_ok = await client.request(
                "PATCH",
                f"/api/chats/{priv_chat['id']}/messages/{msg_id}/title",
                json_body={"custom_title": "  Поиск   конфига  \n  проекта  " + ("x" * 150)},
                headers=alice_hdrs,
            )
            self.assertEqual(r_ok.status_code, 200, r_ok.text)
            body = r_ok.json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["message_id"], msg_id)
            self.assertEqual(len(body["custom_title"]), 140)
            self.assertTrue(body["custom_title"].startswith("Поиск конфига проекта "))

            mock_bcast.assert_awaited_once()
            bcast_args, bcast_kwargs = mock_bcast.await_args
            self.assertEqual(bcast_args[0]["id"], priv_chat["id"])
            self.assertEqual(
                bcast_args[1],
                {
                    "type": "message_title_updated",
                    "chat_id": priv_chat["id"],
                    "message_id": msg_id,
                    "custom_title": body["custom_title"],
                    "auto_title": "",
                },
            )
            self.assertIsNone(bcast_kwargs.get("shared_member_ids"))

            # Reset to ""
            mock_bcast.reset_mock()
            r_reset = await client.request(
                "PATCH",
                f"/api/chats/{priv_chat['id']}/messages/{msg_id}/title",
                json_body={"custom_title": ""},
                headers=alice_hdrs,
            )
            self.assertEqual(r_reset.status_code, 200)
            self.assertEqual(r_reset.json()["custom_title"], "")
            mock_bcast.assert_awaited_once()
            self.assertEqual(mock_bcast.await_args[0][1]["custom_title"], "")

            # Non-existent message -> 404; non-existent chat -> 404
            r_no_msg = await client.request(
                "PATCH",
                f"/api/chats/{priv_chat['id']}/messages/999999/title",
                json_body={"custom_title": "Test"},
                headers=alice_hdrs,
            )
            self.assertEqual(r_no_msg.status_code, 404)

            r_no_chat = await client.request(
                "PATCH",
                f"/api/chats/nonexistent-chat-id/messages/{msg_id}/title",
                json_body={"custom_title": "Test"},
                headers=alice_hdrs,
            )
            self.assertEqual(r_no_chat.status_code, 404)

        # 2. Shared project chat: member Bob IS allowed to rename turn
        sp = await self.database.create_shared_project(
            name="TeamPortal",
            description="Shared project",
            member_ids=[alice["id"], bob["id"]],
            created_by="admin",
        )
        shared_chat = await self.database.create_chat(
            title="Обсуждение TeamPortal",
            kind="project",
            project_name="TeamPortal",
            project_path=str(sp["path"]),
            owner_id=alice["id"],
        )
        sh_msg = await self.database.add_message(
            shared_chat["id"],
            user_id=alice["id"],
            role="user",
            content="Слушай, как устроен роутинг?",
        )

        with patch.object(
            self.main_mod.ws_manager, "broadcast_for_chat", new_callable=AsyncMock
        ) as mock_sh_bcast:
            r_member = await client.request(
                "PATCH",
                f"/api/chats/{shared_chat['id']}/messages/{sh_msg['id']}/title",
                json_body={"custom_title": "Архитектура роутинга TeamPortal"},
                headers=bob_hdrs,
            )
            self.assertEqual(r_member.status_code, 200, r_member.text)
            self.assertEqual(
                r_member.json()["custom_title"], "Архитектура роутинга TeamPortal"
            )
            mock_sh_bcast.assert_awaited_once()
            _, sh_kwargs = mock_sh_bcast.await_args
            shared_members = sh_kwargs.get("shared_member_ids") or set()
            self.assertIn(alice["id"], shared_members)
            self.assertIn(bob["id"], shared_members)

    def test_03_frontend_smart_distiller_and_node_syntax(self) -> None:
        """Run `node --check` and verify `extractSmartOutlineTurnTitle` & `buildChatOutlineModel` via Node VM."""
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
assert.strictEqual(typeof api.extractCleanUserTurnBody, "function");
assert.strictEqual(typeof api.stripConversationalLeadIn, "function");
assert.strictEqual(typeof api.extractSmartOutlineTurnTitle, "function");
assert.strictEqual(typeof api.buildChatOutlineModel, "function");

// Case 1: Emotional + filler intro before domain substance
const r1 = api.extractSmartOutlineTurnTitle({
  role: "user",
  content: "Капец. Что-то я не догоняю. В проекте фотогенератор появилась папка Дата в ней какая-то база.",
});
assert.strictEqual(r1.isCustom, false);
assert.ok(!/^капец/i.test(r1.title), `Unexpected filler in r1: ${r1.title}`);
assert.ok(!/не догоняю/i.test(r1.title), `Unexpected filler in r1: ${r1.title}`);
assert.ok(
  r1.title.includes("В проекте фотогенератор появилась папка Дата"),
  `Expected substantive topic in r1, got: ${r1.title}`
);

// Case 2: Conversational request lead-in -> clean question with ❓
const r2 = api.extractSmartOutlineTurnTitle({
  role: "user",
  content: "Слушай, а можно сделать, чтобы в содержании мои вопросы более информативно отражались?",
});
assert.strictEqual(
  r2.title,
  "❓ В содержании мои вопросы более информативно отражались?"
);

// Case 3: Agreement sentence followed by actual question -> picks question with ❓
const r3 = api.extractSmartOutlineTurnTitle({
  role: "user",
  content: "Да, меня устраивает. Главное теперь мне скажи: старую папку можно удалять или пока подождать?",
});
assert.strictEqual(
  r3.title,
  "❓ Старую папку можно удалять или пока подождать?"
);

// Case 4: Short confirmation enriched with Assistant H2 heading
const r4 = api.extractSmartOutlineTurnTitle(
  { role: "user", content: "Перезапустил, проверь" },
  [{ level: 2, rawText: "Проверка релиза v=98", text: "Проверка релиза v=98" }]
);
assert.strictEqual(r4.title, "Перезапустил, проверь → Проверка релиза v=98");

// Case 5: HQ -> Code handoff prompt («Реализуй утверждённый план из диалога…»)
const r5 = api.extractSmartOutlineTurnTitle({
  role: "user",
  content:
    "Реализуй утверждённый план из диалога «ШТАБ»:\n\n" +
    "Ниже готовое самодостаточное техническое задание.\n\n---\n\n" +
    "# ТЗ (Релиз v=98): Информативные заголовки ходов в «Содержании» чата\n\n## Этап 1. Бэкенд",
});
assert.ok(r5.title.startsWith("🚀 ТЗ:"), `Expected 🚀 ТЗ: prefix, got: ${r5.title}`);
assert.ok(
  r5.title.includes("Информативные заголовки ходов"),
  `Expected spec topic in r5, got: ${r5.title}`
);

// Case 6: Code -> HQ return report («↩ Отчёт в Штаб…» and «[Отчёт о выполнении…]»)
const r6a = api.extractSmartOutlineTurnTitle({
  role: "user",
  content: "↩ Отчёт в Штаб — Релиз v=98 реализован и проверен (4/4 тестов зелёные)",
});
assert.ok(r6a.title.startsWith("↩ Отчёт:"), `Expected ↩ Отчёт: prefix, got: ${r6a.title}`);
assert.ok(r6a.title.includes("Релиз v=98"), `Unexpected r6a: ${r6a.title}`);

const r6b = api.extractSmartOutlineTurnTitle({
  role: "user",
  content:
    "[Отчёт о выполнении из рабочей вкладки «Ядро» (Claude Opus 4.6)]\n\n" +
    "## Итоги релиза v=98: умные заголовки оглавления",
});
assert.ok(r6b.title.startsWith("↩ Отчёт:"), `Expected ↩ Отчёт: prefix, got: ${r6b.title}`);
assert.ok(r6b.title.includes("Итоги релиза v=98"), `Unexpected r6b: ${r6b.title}`);

// Case 7: Image screenshot attachment (image-only & image + text)
const r7Only = api.extractSmartOutlineTurnTitle({
  role: "user",
  content: "",
  attachments: [{ name: "screen_bug.png", mime_type: "image/png", url: "/api/media/c1/screen_bug.png" }],
});
assert.strictEqual(r7Only.title, "🖼 Скриншот: screen_bug.png");

const r7WithText = api.extractSmartOutlineTurnTitle({
  role: "user",
  content: "Слушай, почему съехала рамка на этом скриншоте?",
  attachments: [{ name: "screen_bug.png", mime_type: "image/png", url: "/api/media/c1/screen_bug.png" }],
});
assert.strictEqual(r7WithText.title, "🖼 Почему съехала рамка на этом скриншоте?");

// Case 8: Manual custom_title priority + buildChatOutlineModel integration + system wrapper stripping
const wrappedRaw =
  "<CONTEXT_SUMMARY>Старый контекст</CONTEXT_SUMMARY>\n" +
  "<PROJECT_PULSE project=\"Antigravity_Hub\">Пульс</PROJECT_PULSE>\n" +
  "<REFERENCED_CHAT id=\"c0\">Цитата</REFERENCED_CHAT>\n" +
  "<USER_REQUEST>\nКороче, проверь упоминание `<TASK_PROFILE>` внутри текста ТЗ без обрезания!\n</USER_REQUEST>\n" +
  '<TASK_PROFILE>{"complexity":"light","type":"ui","files":2,"risk":"low","needs_terminal":false}</TASK_PROFILE>';

const cleanPreview = api.extractCleanUserTurnBody(wrappedRaw);
assert.ok(!cleanPreview.includes("<CONTEXT_SUMMARY>"), "CONTEXT_SUMMARY must be stripped");
assert.ok(!cleanPreview.includes("<PROJECT_PULSE"), "PROJECT_PULSE must be stripped");
assert.ok(
  cleanPreview.includes("`<TASK_PROFILE>` внутри текста ТЗ без обрезания!"),
  `Backticked <TASK_PROFILE> mention must NOT truncate body: ${cleanPreview}`
);

const model = api.buildChatOutlineModel([
  {
    id: 101,
    role: "user",
    content: wrappedRaw,
    custom_title: "Проверка парсера TASK_PROFILE",
    created_at: "2026-11-14 15:10:00",
  },
  {
    id: 102,
    role: "assistant",
    content: "## Парсер защищён\nГотово.",
  },
  {
    id: 103,
    role: "user",
    content: "Перезапустил, проверь",
  },
  {
    id: 104,
    role: "assistant",
    content: "## Проверка релиза v=98\nВсе проверки зелёные.",
  },
]);

assert.strictEqual(model.eligible, true);
assert.strictEqual(model.turns.length, 2);
assert.strictEqual(model.turns[0].title, "Проверка парсера TASK_PROFILE");
assert.strictEqual(model.turns[0].isCustomTitle, true);
assert.ok(
  model.turns[0].autoTitle.includes("Проверь упоминание"),
  `Expected autoTitle preserved on custom turn, got: ${model.turns[0].autoTitle}`
);
assert.ok(model.turns[0].searchBlob.includes("проверка парсера task_profile"));
assert.ok(model.turns[0].searchBlob.includes("проверь упоминание"));
assert.strictEqual(model.turns[0].previewText, cleanPreview);
assert.strictEqual(model.turns[1].title, "Перезапустил, проверь → Проверка релиза v=98");
assert.strictEqual(model.turns[1].isCustomTitle, false);

// Meaning guard & domain-adjective safety
assert.strictEqual(api.stripConversationalLeadIn("Ок"), "Ок");
assert.strictEqual(
  api.stripConversationalLeadIn("В общем проекте пропала кнопка"),
  "В общем проекте пропала кнопка"
);
assert.strictEqual(
  api.stripConversationalLeadIn("Главное меню не открывается"),
  "Главное меню не открывается"
);

console.log("NODE_SMART_DISTILLER_OK");
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
                f"Node smart distiller tests failed:\nSTDOUT:\n{run_res.stdout}\nSTDERR:\n{run_res.stderr}",
            )
            self.assertIn("NODE_SMART_DISTILLER_OK", run_res.stdout)
        finally:
            harness_path.unlink(missing_ok=True)

    def test_04_static_v98_sync_encoding_and_workspace_css_invariants(self) -> None:
        """Verify v=98 cache sync, UTF-8 without BOM, preview DOM placement, and main.workspace CSS invariant."""
        checked_files = (
            "app/database.py",
            "app/main.py",
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/app.js",
            "app/static/sw.js",
            "tests/test_outline_smart_titles.py",
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
        self.assertIsNotNone(m_sw_cache)
        assert m_sw_cache is not None
        self.assertGreaterEqual(int(m_sw_cache.group(1)), 98)
        for m in (
            re.search(r"/static/styles\.css\?v=(\d+)", html_txt),
            re.search(r"/static/app\.js\?v=(\d+)", html_txt),
            re.search(r"/static/styles\.css\?v=(\d+)", sw_txt),
            re.search(r"/static/app\.js\?v=(\d+)", sw_txt),
        ):
            self.assertIsNotNone(m)
            assert m is not None
            self.assertGreaterEqual(int(m.group(1)), 98)

        # Ensure #outlineTurnPreviewCard exists and is placed AFTER #chatOutlinePopover closing tag
        self.assertIn('id="outlineTurnPreviewCard"', html_txt)
        pop_idx = html_txt.index('id="chatOutlinePopover"')
        preview_idx = html_txt.index('id="outlineTurnPreviewCard"')
        self.assertGreater(preview_idx, pop_idx)
        between_pop_and_preview = html_txt[pop_idx:preview_idx]
        self.assertIn("</aside>", between_pop_and_preview)

        # Verify 2-line clamp on .outline-turn-title
        m_title_css = re.search(r"\.outline-turn-title\s*\{([^}]*)\}", css_txt, flags=re.DOTALL)
        self.assertIsNotNone(m_title_css)
        assert m_title_css is not None
        title_block = m_title_css.group(1)
        self.assertIn("-webkit-line-clamp: 2", title_block)
        self.assertIn("white-space: normal", title_block)

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
