"""
Automated isolated verification suite for Release v=101:
1. Test 1 (JS Distiller v2 via Node.js VM):
   - Exact screenshot prompt («Не работает режим терминал, на клиентском ПК. Через докер, можешь проверить в чём дело? И предложить решение проблемы»)
     produces a composite title containing BOTH problem subject (`терминал`, `клиент`, `докер`) AND compressed question (`как решить проблему?`),
     instead of the broken fragment `❓ Через докер, можешь проверить в чём дело?`.
   - `buildChatOutlineModel` enforces strict priority `custom_title > auto_title > JS`, preserves `autoTitle` for `↺` reset,
     sets `isAiTitle`, and indexes all variants in `searchBlob`.
2. Test 2 (DB migrations, `include_thinking=False`, and `custom_title` immutability):
   - Idempotent migration of legacy DB (`messages.auto_title`, `user_openrouter_configs.smart_titles_enabled`,
     `user_openrouter_configs.smart_titles_model`, `openrouter_logs.purpose`, `openrouter_logs.items_count`,
     `idx_or_logs_user_purpose` created strictly AFTER column migrations).
   - `get_messages(..., include_thinking=False)` & `get_message_by_id(..., include_thinking=False)` return `auto_title`.
   - `set_message_auto_title(..., respect_custom=True)` and `set_messages_auto_titles_bulk` NEVER overwrite non-empty `custom_title`.
3. Test 3 (Candidate selection, Zero-Trust RBAC, filtering, head+tail truncation, and secret sanitization):
   - `list_user_outline_title_candidates` excludes foreign private chats (RBAC), manual `custom_title`, short replies (`"Ок"`),
     `🚀 ТЗ` / `↩ Отчёт` envelopes, and existing `auto_title` (unless `include_existing_ai=True`).
   - Long `> 480`-char prompts use `head + " … " + tail` and `sanitize_outbound_openrouter_text` redacts secrets.
4. Test 4 (Batch regeneration by 25 items, WS broadcasts, live hook, and `purpose` spend tracking):
   - 30 eligible user turns + 3 manual `custom_title` turns via `POST /api/openrouter/regenerate-titles` make strictly **2**
     mocked `_openrouter_http_sync` calls (`25 + 5`), leave all 3 `custom_title` turns untouched, write 30 `auto_title` values,
     reduce `eligible_count` to `0`, and compute exact `spend_stats["smart_titles"]` (`titles_generated == 30`, `$` sum by `purpose`).
5. Test 5 (`node --check`, `v=101` cache sync, UTF-8 without BOM, and `main.workspace` CSS invariant).

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
    def __init__(self, app: Any, *, client: tuple[str, int] = ("127.0.0.1", 53201)) -> None:
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


class TestReleaseV101SmartTitlesOpenRouter(unittest.IsolatedAsyncioTestCase):
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
        from app import (
            brain_reader,
            config,
            database,
            host_service_manager,
            main as main_mod,
            openrouter_service,
        )

        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.main_mod = main_mod
        self.or_service = openrouter_service

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v101_test_", ignore_cleanup_errors=True)
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
        for mod in (config, database, brain_reader, main_mod, openrouter_service):
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
        self.main_mod._SMART_TITLE_REGEN_ACTIVE_USERS.clear()
        for p in reversed(getattr(self, "_patches", [])):
            p.stop()
        self._tmp.cleanup()
        self._assert_live_db_untouched()

    def test_01_js_distiller_v2_screenshot_prompt_and_priority_chain(self) -> None:
        """Test 1: Node VM check of screenshot prompt synthesis & `custom_title > auto_title > JS` priority."""
        from app.config import subprocess_hidden_kwargs

        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["ORT_DISABLE_ALL_GPU"] = "1"

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

// 1. Exact prompt from user's screenshot
const screenshotPrompt =
  "Не работает режим терминал, на клиентском ПК. Через докер, можешь проверить в чём дело? И предложить решение проблемы";
const rScreen = api.extractSmartOutlineTurnTitle({
  role: "user",
  content: screenshotPrompt,
});
assert.notStrictEqual(
  rScreen.title,
  "❓ Через докер, можешь проверить в чём дело?",
  "Must NOT pick the broken meta-question fragment"
);
assert.ok(rScreen.title.startsWith("❓ "), `Expected ❓ prefix, got: ${rScreen.title}`);
assert.ok(/терминал/i.test(rScreen.title), `Expected 'терминал' in: ${rScreen.title}`);
assert.ok(/клиент/i.test(rScreen.title), `Expected 'клиент' in: ${rScreen.title}`);
assert.ok(/докер/i.test(rScreen.title), `Expected 'докер' in: ${rScreen.title}`);
assert.ok(/как решить проблему\?$/i.test(rScreen.title), `Expected compressed question tail in: ${rScreen.title}`);
assert.strictEqual(
  rScreen.title,
  "❓ Не работает режим терминал на клиентском ПК через докер — как решить проблему?"
);

// 2. Standalone detached prepositional modifier + generic 'В чём дело?'
const rDetached = api.extractSmartOutlineTurnTitle({
  role: "user",
  content: "Падает сборка контейнера проекта. Через докер. В чём дело?",
});
assert.ok(/сборка контейнера проекта через докер/i.test(rDetached.title), `Got: ${rDetached.title}`);
assert.ok(/в чём причина\?$/i.test(rDetached.title), `Got: ${rDetached.title}`);

// 3. Priority chain in buildChatOutlineModel: custom_title > auto_title > JS
const outlineModel = api.buildChatOutlineModel([
  {
    id: 1,
    role: "user",
    content: screenshotPrompt,
    custom_title: "Ручной заголовок терминала",
    auto_title: "❓ Не работает терминал у клиента через Docker — как решить?",
  },
  { id: 2, role: "assistant", content: "## Диагностика Docker\nПроверим сокет." },
  {
    id: 3,
    role: "user",
    content: screenshotPrompt,
    custom_title: "",
    auto_title: "❓ Не работает терминал у клиента через Docker — как решить?",
  },
  { id: 4, role: "assistant", content: "## Решение\nГотово." },
  {
    id: 5,
    role: "user",
    content: screenshotPrompt,
    custom_title: "",
    auto_title: "",
  },
  { id: 6, role: "assistant", content: "## Финал\nОк." },
]);

assert.strictEqual(outlineModel.turns.length, 3);

// Turn 1: custom_title wins, autoTitle keeps AI title for ↺ reset
const t1 = outlineModel.turns[0];
assert.strictEqual(t1.title, "Ручной заголовок терминала");
assert.strictEqual(t1.isCustomTitle, true);
assert.strictEqual(t1.isAiTitle, false);
assert.strictEqual(t1.autoTitle, "❓ Не работает терминал у клиента через Docker — как решить?");
assert.ok(t1.searchBlob.includes("ручной заголовок терминала"));
assert.ok(t1.searchBlob.includes("через docker"));
assert.ok(t1.searchBlob.includes("клиентском пк"));

// Turn 2: auto_title wins over JS
const t2 = outlineModel.turns[1];
assert.strictEqual(t2.title, "❓ Не работает терминал у клиента через Docker — как решить?");
assert.strictEqual(t2.isCustomTitle, false);
assert.strictEqual(t2.isAiTitle, true);
assert.strictEqual(t2.autoTitle, "❓ Не работает терминал у клиента через Docker — как решить?");

// Turn 3: JS Distiller v2 fallback when both custom_title & auto_title are empty
const t3 = outlineModel.turns[2];
assert.strictEqual(
  t3.title,
  "❓ Не работает режим терминал на клиентском ПК через докер — как решить проблему?"
);
assert.strictEqual(t3.isCustomTitle, false);
assert.strictEqual(t3.isAiTitle, false);

// Russian declension helper check
assert.strictEqual(api.formatOutlineTitlesCountRu(1), "заголовок");
assert.strictEqual(api.formatOutlineTitlesCountRu(2), "заголовка");
assert.strictEqual(api.formatOutlineTitlesCountRu(5), "заголовков");
assert.strictEqual(api.formatOutlineTitlesCountRu(11), "заголовков");
assert.strictEqual(api.formatOutlineTitlesCountRu(34), "заголовка");

console.log("NODE_V101_DISTILLER_OK");
"""
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tf:
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
                f"Node v101 distiller test failed:\nSTDOUT:\n{run_res.stdout}\nSTDERR:\n{run_res.stderr}",
            )
            self.assertIn("NODE_V101_DISTILLER_OK", run_res.stdout)
        finally:
            harness_path.unlink(missing_ok=True)

    async def test_02_migrations_include_thinking_false_and_custom_title_guard(self) -> None:
        """Test 2: Legacy DB migration, `include_thinking=False` `auto_title` SELECTs, and `custom_title` guard."""
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
                    custom_title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE user_openrouter_configs (
                    user_id TEXT PRIMARY KEY,
                    api_key_encrypted TEXT NOT NULL DEFAULT '',
                    key_mask TEXT NOT NULL DEFAULT '',
                    key_nonce TEXT NOT NULL DEFAULT '',
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    default_model TEXT NOT NULL DEFAULT '',
                    custom_rules TEXT NOT NULL DEFAULT '',
                    max_price_per_m REAL NOT NULL DEFAULT 0,
                    daily_spend_limit_usd REAL NOT NULL DEFAULT 0,
                    cached_status_json TEXT NOT NULL DEFAULT '{}',
                    cached_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE openrouter_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL DEFAULT '',
                    chat_title TEXT NOT NULL DEFAULT '',
                    project_name TEXT NOT NULL DEFAULT '',
                    requested_model TEXT NOT NULL DEFAULT '',
                    resolved_model TEXT NOT NULL DEFAULT '',
                    provider_name TEXT NOT NULL DEFAULT '',
                    generation_id TEXT NOT NULL DEFAULT '',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ok',
                    finish_reason TEXT NOT NULL DEFAULT '',
                    prompt_preview TEXT NOT NULL DEFAULT '',
                    response_preview TEXT NOT NULL DEFAULT '',
                    error_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO chats (id, title, user_id) VALUES ('c-leg', 'Legacy v100 Chat', 'admin');
                INSERT INTO messages (chat_id, role, content, custom_title)
                VALUES ('c-leg', 'user', 'Первый вопрос без ручного имени?', '');
                INSERT INTO messages (chat_id, role, content, custom_title)
                VALUES ('c-leg', 'user', 'Второй вопрос с ручным именем?', 'Моё ручное имя ✎');
                """
            )
            conn.commit()
        finally:
            conn.close()

        # Run init_db twice to verify idempotency and index-after-column migration order
        await self.database.init_db()
        await self.database.init_db()

        conn_chk = sqlite3.connect(self.db_path)
        try:
            msg_cols = {r[1] for r in conn_chk.execute("PRAGMA table_info(messages)").fetchall()}
            cfg_cols = {
                r[1] for r in conn_chk.execute("PRAGMA table_info(user_openrouter_configs)").fetchall()
            }
            log_cols = {
                r[1] for r in conn_chk.execute("PRAGMA table_info(openrouter_logs)").fetchall()
            }
            indexes = {
                r[1] for r in conn_chk.execute("PRAGMA index_list(openrouter_logs)").fetchall()
            }
        finally:
            conn_chk.close()

        self.assertIn("auto_title", msg_cols)
        self.assertIn("smart_titles_enabled", cfg_cols)
        self.assertIn("smart_titles_model", cfg_cols)
        self.assertIn("purpose", log_cols)
        self.assertIn("items_count", log_cols)
        self.assertIn("idx_or_logs_user_purpose", indexes)

        msgs_light = await self.database.get_messages("c-leg", include_thinking=False)
        self.assertEqual(len(msgs_light), 2)
        m1_id = int(msgs_light[0]["id"])
        m2_id = int(msgs_light[1]["id"])
        self.assertEqual(msgs_light[0]["auto_title"], "")
        self.assertEqual(msgs_light[1]["custom_title"], "Моё ручное имя ✎")

        # 1. set_message_auto_title writes auto_title on m1 and returns it via include_thinking=False
        saved_m1 = await self.database.set_message_auto_title(
            "c-leg", m1_id, "  ❓   Первый\nвопрос   без ручного имени?  ", respect_custom=True
        )
        self.assertIsNotNone(saved_m1)
        assert saved_m1 is not None
        self.assertEqual(saved_m1["auto_title"], "❓ Первый вопрос без ручного имени?")

        reloaded_m1 = await self.database.get_message_by_id("c-leg", m1_id, include_thinking=False)
        assert reloaded_m1 is not None
        self.assertEqual(reloaded_m1["auto_title"], "❓ Первый вопрос без ручного имени?")
        reloaded_list = await self.database.get_messages("c-leg", include_thinking=False)
        self.assertEqual(reloaded_list[0]["auto_title"], "❓ Первый вопрос без ручного имени?")

        # 2. set_message_auto_title(..., respect_custom=True) MUST NOT touch m2 (has custom_title)
        blocked_m2 = await self.database.set_message_auto_title(
            "c-leg", m2_id, "❓ Попытка перезаписать ручной ход", respect_custom=True
        )
        self.assertIsNone(blocked_m2)
        reloaded_m2 = await self.database.get_message_by_id("c-leg", m2_id, include_thinking=False)
        assert reloaded_m2 is not None
        self.assertEqual(reloaded_m2["custom_title"], "Моё ручное имя ✎")
        self.assertEqual(reloaded_m2["auto_title"], "")

        # 3. set_messages_auto_titles_bulk updates m1 and skips m2 atomically
        bulk_applied = await self.database.set_messages_auto_titles_bulk(
            [
                ("c-leg", m1_id, "❓ Обновлённый ИИ-заголовок первого хода"),
                ("c-leg", m2_id, "❓ Недопустимая перезапись ручного хода"),
            ]
        )
        self.assertEqual(len(bulk_applied), 1)
        self.assertEqual(bulk_applied[0]["message_id"], m1_id)

        after_bulk = await self.database.get_messages("c-leg", include_thinking=False)
        self.assertEqual(after_bulk[0]["auto_title"], "❓ Обновлённый ИИ-заголовок первого хода")
        self.assertEqual(after_bulk[1]["custom_title"], "Моё ручное имя ✎")
        self.assertEqual(after_bulk[1]["auto_title"], "")

    async def test_03_candidates_rbac_filtering_truncation_and_secret_sanitization(self) -> None:
        """Test 3: RBAC isolation, filtering of custom/short/handoff/existing AI turns, head+tail & secrets."""
        await self.database.init_db()
        alice = await self.database.create_user(
            username="alice_v101",
            password="PassAlice123!",
            display_name="Alice",
            role="user",
            max_access_mode="full_access",
        )
        bob = await self.database.create_user(
            username="bob_v101",
            password="PassBob12345!",
            display_name="Bob",
            role="user",
            max_access_mode="full_access",
        )

        alice_chat = await self.database.create_chat(title="Alice Private", owner_id=alice["id"])
        bob_chat = await self.database.create_chat(title="Bob Secret", owner_id=bob["id"])

        # Shared project where Alice is a member
        sp = await self.database.create_shared_project(
            name="SharedV101",
            description="Shared",
            member_ids=[alice["id"]],
            created_by="admin",
        )
        shared_chat = await self.database.create_chat(
            title="Shared Discussion",
            kind="project",
            project_name="SharedV101",
            project_path=str(sp["path"]),
            owner_id="admin",
        )

        # Foreign message in Bob's private chat (must NEVER appear in Alice's candidates)
        await self.database.add_message(
            bob_chat["id"],
            user_id=bob["id"],
            role="user",
            content="Почему падает секретный сервис Боба при старте контейнера?",
        )

        # 1. Eligible turn in Alice's private chat (with secret token + system wrappers)
        m_elig_priv = await self.database.add_message(
            alice_chat["id"],
            user_id=alice["id"],
            role="user",
            content=(
                "<CONTEXT_SUMMARY>Скрытый контекст</CONTEXT_SUMMARY>\n"
                "<USER_REQUEST>\n"
                "Не работает авторизация ключа \x73k-or-v1-abcdef1234567890abcdef1234567890 в докере, "
                "можешь проверить в чём причина?\n"
                "</USER_REQUEST>\n"
                '<TASK_PROFILE>{"complexity":"light"}</TASK_PROFILE>'
            ),
        )

        # 2. Eligible long > 480-char turn in Shared project chat
        long_middle = "очень подробное описание логов запуска контейнера в локальной сети " * 14
        long_prompt = (
            "При запуске сборки фронтенда в контейнере возникает конфликт портов: "
            + long_middle
            + "И финальный вопрос в самом конце: как поменять порт без пересборки образа?"
        )
        self.assertGreater(len(long_prompt), 600)
        m_elig_long = await self.database.add_message(
            shared_chat["id"],
            user_id=alice["id"],
            role="user",
            content=long_prompt,
        )

        # 3. Manual custom_title turn (must increment manual_custom_count and be excluded)
        m_manual = await self.database.add_message(
            alice_chat["id"],
            user_id=alice["id"],
            role="user",
            content="Почему не открывается вкладка настроек после обновления кэша?",
        )
        await self.database.set_message_custom_title(
            alice_chat["id"], int(m_manual["id"]), "Настройки после обновления кэша"
        )

        # 4. Short trivial replies (< 18 chars / confirmation words -> excluded)
        await self.database.add_message(alice_chat["id"], user_id=alice["id"], role="user", content="Ок")
        await self.database.add_message(
            alice_chat["id"], user_id=alice["id"], role="user", content="Спасибо!"
        )

        # 5. Special envelopes: 🚀 ТЗ handoff and ↩ Отчёт return (excluded)
        await self.database.add_message(
            alice_chat["id"],
            user_id=alice["id"],
            role="user",
            content=(
                "Реализуй утверждённый план из диалога «ШТАБ»:\n\n"
                "# ТЗ (Релиз v=101): Умные заголовки оглавления"
            ),
        )
        await self.database.add_message(
            alice_chat["id"],
            user_id=alice["id"],
            role="user",
            content=(
                "[Отчёт о выполнении из рабочей вкладки «Ядро» (Claude Opus 4.6)]\n\n"
                "## Все 5 тестов успешно пройдены"
            ),
        )

        # 6. Existing AI auto_title turn (excluded by default, included when include_existing_ai=True)
        m_existing_ai = await self.database.add_message(
            alice_chat["id"],
            user_id=alice["id"],
            role="user",
            content="Как правильно настроить ротацию логов в фоновом сервисе Windows?",
        )
        await self.database.set_message_auto_title(
            alice_chat["id"],
            int(m_existing_ai["id"]),
            "❓ Настройка ротации логов фонового сервиса Windows",
        )

        cands_default = await self.database.list_user_outline_title_candidates(
            alice["id"], include_existing_ai=False
        )
        self.assertEqual(cands_default["eligible_count"], 2)
        self.assertEqual(cands_default["existing_ai_count"], 1)
        self.assertEqual(cands_default["manual_custom_count"], 1)
        cand_ids = {int(it["id"]) for it in cands_default["items"]}
        self.assertEqual(cand_ids, {int(m_elig_priv["id"]), int(m_elig_long["id"])})

        by_id = {int(it["id"]): it for it in cands_default["items"]}
        snip_priv = by_id[int(m_elig_priv["id"])]["snippet"]
        self.assertNotIn("<CONTEXT_SUMMARY>", snip_priv)
        self.assertNotIn("<TASK_PROFILE>", snip_priv)

        # Verify outbound secret sanitization
        sanitized_priv, redacted_cnt = self.or_service.sanitize_outbound_openrouter_text(snip_priv)
        self.assertGreaterEqual(redacted_cnt, 1)
        self.assertNotIn("sk-or-v1-abcdef1234567890", sanitized_priv)

        # Verify head + " … " + tail compression on > 480-char prompt
        snip_long = by_id[int(m_elig_long["id"])]["snippet"]
        self.assertLessEqual(len(snip_long), 485)
        self.assertIn(" … ", snip_long)
        self.assertTrue(snip_long.startswith("При запуске сборки фронтенда"))
        self.assertIn("как поменять порт без пересборки образа?", snip_long)

        # Rebuild mode includes existing AI titles while STILL protecting manual custom_title!
        cands_rebuild = await self.database.list_user_outline_title_candidates(
            alice["id"], include_existing_ai=True
        )
        self.assertEqual(cands_rebuild["eligible_count"], 3)
        rebuild_ids = {int(it["id"]) for it in cands_rebuild["items"]}
        self.assertIn(int(m_existing_ai["id"]), rebuild_ids)
        self.assertNotIn(int(m_manual["id"]), rebuild_ids)

    async def test_04_batch_25_regeneration_two_http_calls_and_purpose_spend_stats(self) -> None:
        """Test 4: 30 eligible + 3 manual custom_title -> strictly 2 HTTP calls (25 + 5) & exact purpose spend."""
        from app import auth

        await self.database.init_db()
        alice = await self.database.create_user(
            username="alice_batch",
            password="PassBatch123!",
            display_name="Alice Batch",
            role="user",
            max_access_mode="full_access",
        )
        alice_tok = auth.create_user_session_token(
            alice["id"], int(alice["session_version"])
        )
        alice_hdrs = {"Authorization": f"Bearer {alice_tok}"}

        await self.database.upsert_user_openrouter_config(
            alice["id"],
            new_api_key="\x73k-or-v1-11112222333344445555666677778888",
            is_enabled=True,
            smart_titles_enabled=True,
            smart_titles_model="openai/gpt-4.1-mini",
            cached_status={
                "label": "Alice Key",
                "is_free_tier": False,
                "credits_remaining": 12.5,
                "total_credits": 15.0,
                "usage": 2.5,
                "usage_daily": 0.0,
            },
        )

        chat = await self.database.create_chat(title="Batch Regen Chat", owner_id=alice["id"])

        eligible_ids: list[int] = []
        for idx in range(1, 31):
            m = await self.database.add_message(
                chat["id"],
                user_id=alice["id"],
                role="user",
                content=(
                    f"Проблема #{idx}: не запускается воркер обработки очереди на клиенте. "
                    f"Через докер, можешь проверить в чём дело и как починить модуль {idx}?"
                ),
            )
            eligible_ids.append(int(m["id"]))

        manual_ids: list[int] = []
        for k in range(1, 4):
            mm = await self.database.add_message(
                chat["id"],
                user_id=alice["id"],
                role="user",
                content=f"Ручной вопрос #{k}: почему не срабатывает хук авторизации в модуле {k}?",
            )
            mid = int(mm["id"])
            await self.database.set_message_custom_title(chat["id"], mid, f"Ручной заголовок #{k}")
            manual_ids.append(mid)

        client = _MiniAsgiClient(self.main_mod.app)

        # Verify GET /api/openrouter/state reports 30 eligible & 3 manual without leaking raw items
        r_state_before = await client.request(
            "GET", "/api/openrouter/state?refresh=0", headers=alice_hdrs
        )
        self.assertEqual(r_state_before.status_code, 200, r_state_before.text)
        st_before_json = r_state_before.json()
        self.assertEqual(
            st_before_json["smart_titles_eligibility"],
            {
                "eligible_count": 30,
                "existing_ai_count": 0,
                "manual_custom_count": 3,
            },
        )
        self.assertNotIn("items", st_before_json["smart_titles_eligibility"])

        http_calls: list[dict[str, Any]] = []

        def _fake_openrouter_http_sync(
            method: str,
            path: str,
            *,
            api_key: str | None = None,
            payload: dict[str, Any] | None = None,
            timeout: float = 20.0,
        ) -> tuple[int, dict[str, Any]]:
            http_calls.append({"method": method, "path": path, "payload": payload})
            assert method == "POST" and path == "/chat/completions", f"Unexpected call: {method} {path}"
            assert payload is not None
            user_msgs = [m for m in payload.get("messages", []) if m.get("role") == "user"]
            batch_in = json.loads(user_msgs[0]["content"])
            call_num = len(http_calls)
            chunk_cost = 0.00125 if call_num == 1 else 0.00035
            generated_list = [
                {
                    "id": int(entry["id"]),
                    "title": f"❓ Сбой воркера очереди в Docker — как починить модуль (ID {entry['id']})?",
                }
                for entry in batch_in
            ]
            return (
                200,
                {
                    "id": f"gen-batch-{call_num}",
                    "model": "openai/gpt-4.1-mini-2025-04-14",
                    "provider": "OpenAI",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"titles": generated_list}, ensure_ascii=False),
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 900 if call_num == 1 else 240,
                        "completion_tokens": 450 if call_num == 1 else 100,
                        "cost": chunk_cost,
                    },
                },
            )

        ws_user_events: list[dict[str, Any]] = []

        async def _capture_ws_user(uid: str, event: dict[str, Any]) -> None:
            if uid == alice["id"]:
                ws_user_events.append(event)

        with (
            patch.object(self.or_service, "_openrouter_http_sync", side_effect=_fake_openrouter_http_sync),
            patch.object(self.main_mod.ws_manager, "broadcast_user", side_effect=_capture_ws_user),
            patch.object(self.main_mod.ws_manager, "broadcast_to_user", side_effect=_capture_ws_user),
        ):
            r_regen = await client.request(
                "POST",
                "/api/openrouter/regenerate-titles",
                json_body={"rebuild_existing_ai": False},
                headers=alice_hdrs,
            )

        self.assertEqual(r_regen.status_code, 200, r_regen.text)
        regen_json = r_regen.json()

        # Strictly 2 HTTP requests: chunk 1 = 25 items, chunk 2 = 5 items
        self.assertEqual(len(http_calls), 2)
        chunk1_items = json.loads(http_calls[0]["payload"]["messages"][-1]["content"])
        chunk2_items = json.loads(http_calls[1]["payload"]["messages"][-1]["content"])
        self.assertEqual(len(chunk1_items), 25)
        self.assertEqual(len(chunk2_items), 5)
        # Ensure custom_rules / cross-vendor guardrails were NOT injected into utility calls
        self.assertNotIn("reasoning", http_calls[0]["payload"])

        self.assertEqual(regen_json["regenerated_count"], 30)
        self.assertEqual(regen_json["chunks_count"], 2)
        self.assertAlmostEqual(regen_json["cost_usd"], 0.0016, places=6)
        self.assertEqual(
            regen_json["smart_titles_eligibility"],
            {
                "eligible_count": 0,
                "existing_ai_count": 30,
                "manual_custom_count": 3,
            },
        )

        # Verify spend_stats["smart_titles"] & by_purpose["outline_title_batch"]
        sp_stats = regen_json["spend_stats"]
        self.assertEqual(sp_stats["smart_titles"]["titles_generated"], 30)
        self.assertEqual(sp_stats["smart_titles"]["calls_count"], 2)
        self.assertAlmostEqual(sp_stats["smart_titles"]["total_usd"], 0.0016, places=6)
        self.assertAlmostEqual(sp_stats["smart_titles"]["today_usd"], 0.0016, places=6)
        self.assertAlmostEqual(
            sp_stats["by_purpose"]["outline_title_batch"]["cost_usd"], 0.0016, places=6
        )
        self.assertEqual(sp_stats["by_purpose"]["outline_title_batch"]["items_count"], 30)

        # Verify all 30 eligible messages received auto_title and all 3 manual custom_title stayed untouched
        all_msgs = await self.database.get_messages(chat["id"], include_thinking=False)
        msgs_by_id = {int(m["id"]): m for m in all_msgs}
        for eid in eligible_ids:
            self.assertEqual(msgs_by_id[eid]["custom_title"], "")
            self.assertIn(f"(ID {eid})", msgs_by_id[eid]["auto_title"])
        for idx_k, mid in enumerate(manual_ids, start=1):
            self.assertEqual(msgs_by_id[mid]["custom_title"], f"Ручной заголовок #{idx_k}")
            self.assertEqual(msgs_by_id[mid]["auto_title"], "")

        # Verify WS progress & batch_updated events were emitted
        event_types = [e.get("type") for e in ws_user_events]
        self.assertIn("outline_titles_regen_progress", event_types)
        self.assertIn("outline_titles_batch_updated", event_types)

        # Also verify live single-turn hook (`_maybe_generate_live_outline_title_task`) + reasoning suppression retry
        live_msg = await self.database.add_message(
            chat["id"],
            user_id=alice["id"],
            role="user",
            content="Не работает режим терминал, на клиентском ПК. Через докер, можешь проверить в чём дело? И предложить решение проблемы",
        )
        await self.database.upsert_user_openrouter_config(
            alice["id"],
            smart_titles_model="google/gemini-2.5-flash-lite",
        )
        single_calls: list[dict[str, Any]] = []

        def _fake_single_with_reasoning_retry(
            method: str,
            path: str,
            *,
            api_key: str | None = None,
            payload: dict[str, Any] | None = None,
            timeout: float = 20.0,
        ) -> tuple[int, dict[str, Any]]:
            assert payload is not None
            single_calls.append(dict(payload))
            if "reasoning" in payload:
                return 400, {"error": {"message": "Provider rejected reasoning param"}}
            return (
                200,
                {
                    "id": "gen-single-1",
                    "model": "google/gemini-2.5-flash-lite",
                    "provider": "Google",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "«❓ Не работает терминал у клиента через Docker — как решить проблему?»",
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 22, "cost": 0.000021},
                },
            )

        with (
            patch.object(
                self.or_service, "_openrouter_http_sync", side_effect=_fake_single_with_reasoning_retry
            ),
            patch.object(
                self.main_mod.ws_manager, "broadcast_for_chat", new_callable=AsyncMock
            ) as mock_chat_bcast,
        ):
            await self.main_mod._maybe_generate_live_outline_title_task(
                user_id=alice["id"],
                chat_id=chat["id"],
                message_id=int(live_msg["id"]),
                raw_content=str(live_msg["content"]),
                chat_title=str(chat.get("title") or ""),
                project_name=str(chat.get("project_name") or ""),
            )

        self.assertEqual(len(single_calls), 2)
        self.assertEqual(single_calls[0].get("reasoning"), {"effort": "low", "exclude": True})
        self.assertNotIn("reasoning", single_calls[1])
        mock_chat_bcast.assert_awaited_once()
        ws_payload = mock_chat_bcast.await_args[0][1]
        self.assertEqual(
            ws_payload,
            {
                "type": "message_title_updated",
                "chat_id": chat["id"],
                "message_id": int(live_msg["id"]),
                "custom_title": "",
                "auto_title": "❓ Не работает терминал у клиента через Docker — как решить проблему?",
            },
        )
        final_stats = await self.database.get_openrouter_spend_stats(alice["id"])
        self.assertEqual(final_stats["smart_titles"]["titles_generated"], 31)
        self.assertEqual(final_stats["by_purpose"]["outline_title"]["items_count"], 1)

    def test_05_js_syntax_v101_cache_sync_utf8_no_bom_and_workspace_css(self) -> None:
        """Test 5: `node --check`, `v=101` sync in `index.html` & `sw.js`, UTF-8 no BOM, and `main.workspace` CSS."""
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

        checked_files = (
            "app/database.py",
            "app/openrouter_service.py",
            "app/main.py",
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/app.js",
            "app/static/sw.js",
            "tests/test_outline_smart_titles.py",
            "tests/test_smart_titles_openrouter_v101.py",
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
        self.assertGreaterEqual(int(m_sw_cache.group(1)), 101)
        for m in (
            re.search(r'/static/styles\.css\?v=(\d+)"', html_txt),
            re.search(r'/static/app\.js\?v=(\d+)"', html_txt),
            re.search(r'/static/styles\.css\?v=(\d+)"', sw_txt),
            re.search(r'/static/app\.js\?v=(\d+)"', sw_txt),
        ):
            self.assertIsNotNone(m)
            assert m is not None
            self.assertGreaterEqual(int(m.group(1)), 101)

        for dom_id in (
            "orSmartTitlesBox",
            "orSmartTitlesToggle",
            "orSmartTitlesSpendPill",
            "orSmartTitlesControls",
            "orSmartTitlesModelInput",
            "orSmartTitlesPresetsBar",
            "orRegenTitlesBtn",
            "orRegenEligibleHint",
            "orRebuildExistingAiTitlesBtn",
        ):
            self.assertIn(f'id="{dom_id}"', html_txt)

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
