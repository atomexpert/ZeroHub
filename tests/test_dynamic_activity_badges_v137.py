# -*- coding: utf-8 -*-
"""v137: Dynamic cross-device & cross-tab read sync, filter-scoped Activity badges (`Все / Проекты / Чаты`),
filter-tab mini-badges, zero sticky read chats in `⚡ Активность`, `pruneStaleUnreadAndRunningState()`,
and HTTP poll race protection."""
from __future__ import annotations

import json
import os
import py_compile
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_audit_fixes_v128 as isolated
import test_smart_hq_return_v115 as hq_iso

ROOT_DIR = Path(__file__).resolve().parents[1]


class DynamicActivityBadgesV137Tests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(isolated.AuditFixesV128Tests.setUpClass.__func__)
    tearDownClass = classmethod(isolated.AuditFixesV128Tests.tearDownClass.__func__)
    asyncSetUp = isolated.AuditFixesV128Tests.asyncSetUp
    asyncTearDown = isolated.AuditFixesV128Tests.asyncTearDown
    _assert_live_db_untouched = isolated.AuditFixesV128Tests._assert_live_db_untouched
    _make_agy_account = isolated.AuditFixesV128Tests._make_agy_account

    def setUp(self) -> None:
        from app import main as main_mod

        main_mod._USER_CHAT_READ_TS.clear()
        main_mod._USER_READ_ALL_TS.clear()
        main_mod._CHAT_LAST_TERMINAL_TS_MS.clear()

    async def test_01_server_ws_and_http_cross_device_read_sync_user_message_archive_and_delete(self):
        m = self.main_mod
        admin_user = await self.database.get_user_by_id("admin")
        assert admin_user is not None
        admin_token = self.main_mod.auth.create_user_session_token(admin_user["id"])

        dev_user = await self.database.create_user(
            username="dev_v137",
            password="dev_password_137",
            display_name="Dev 137",
            max_access_mode="write_files",
        )
        dev_token = self.main_mod.auth.create_user_session_token(dev_user["id"])

        proj_chat = await self.database.create_chat(
            owner_id="admin",
            kind="project",
            project_name="AlphaProj",
            project_path=str(self.hub_base / "users" / "admin" / "projects" / "AlphaProj"),
            title="Ветка Ядро",
            model="gemini-4-carbon-high",
            access_mode="write_files",
        )
        reg_chat = await self.database.create_chat(
            owner_id="admin",
            kind="chat",
            title="Обычный диалог",
            model="gemini-4-carbon-high",
            access_mode="read_only",
        )
        proj_cid = proj_chat["id"]
        reg_cid = reg_chat["id"]

        # Connect fake WebSocket clients directly to ws_manager to verify multi-device broadcast & user isolation
        pc_events: list[dict] = []
        phone_events: list[dict] = []
        other_user_events: list[dict] = []

        ws_pc = MagicMock()
        ws_pc.send_json = AsyncMock(side_effect=lambda payload: pc_events.append(payload))
        ws_phone = MagicMock()
        ws_phone.send_json = AsyncMock(side_effect=lambda payload: phone_events.append(payload))
        ws_other = MagicMock()
        ws_other.send_json = AsyncMock(side_effect=lambda payload: other_user_events.append(payload))

        m.ws_manager.active[ws_pc] = admin_user
        m.ws_manager.active[ws_phone] = admin_user
        m.ws_manager.active[ws_other] = dev_user

        async def _drive_ws_session(incoming_frames: list[dict]) -> list[dict]:
            sent: list[dict] = []
            it = iter(incoming_frames)

            async def _recv_json():
                try:
                    return next(it)
                except StopIteration:
                    raise WebSocketDisconnect()

            fake_ws = MagicMock()
            fake_ws.query_params = {"token": admin_token}
            fake_ws.cookies = {}
            fake_ws.headers = {}
            fake_ws.accept = AsyncMock()
            fake_ws.close = AsyncMock()
            fake_ws.send_json = AsyncMock(side_effect=lambda payload: sent.append(payload))
            fake_ws.receive_json = AsyncMock(side_effect=_recv_json)
            await m.websocket_endpoint(fake_ws)
            return sent

        try:
            # 1) Incoming WS `{"type": "mark_chat_read", "chat_id": ...}` via `websocket_endpoint`
            ws_sent_1 = await _drive_ws_session([{"type": "mark_chat_read", "chat_id": proj_cid}])
            self.assertEqual(len(ws_sent_1), 1)
            ws_reply = ws_sent_1[0]
            self.assertEqual(ws_reply["type"], "chat_read_synced")
            self.assertEqual(ws_reply["chat_id"], proj_cid)
            self.assertFalse(ws_reply["read_all"])
            read_ts_1 = int(ws_reply["read_ts_ms"])
            self.assertGreater(read_ts_1, 0)

            # Smartphone client of `admin` received `chat_read_synced`, while `dev_v137` received nothing!
            self.assertTrue(
                any(
                    e.get("type") == "chat_read_synced"
                    and e.get("chat_id") == proj_cid
                    and e.get("read_all") is False
                    for e in phone_events
                ),
                phone_events,
            )
            self.assertEqual(other_user_events, [])

            # Verify `/api/chats/mark-read` route is registered BEFORE `/api/chats/{chat_id}`
            route_paths = [getattr(r, "path", "") for r in m.app.routes]
            self.assertIn("/api/chats/mark-read", route_paths)
            self.assertLess(
                route_paths.index("/api/chats/mark-read"),
                route_paths.index("/api/chats/{chat_id}"),
            )

            # 2) `GET /api/chats` and `GET /api/chats/{id}` expose `last_read_ts_ms` for wake-from-sleep phones
            resp_list = await m.api_list_chats(
                kind=None,
                project_name=None,
                query=None,
                recent=False,
                limit=None,
                include_archived=True,
                current_user=admin_user,
            )
            chats_by_id = {c["id"]: c for c in resp_list["chats"]}
            self.assertEqual(chats_by_id[proj_cid].get("last_read_ts_ms"), read_ts_1)
            self.assertIsNone(chats_by_id[reg_cid].get("last_read_ts_ms"))

            resp_single = await m.api_get_chat(
                proj_cid, include_thinking=False, current_user=admin_user
            )
            self.assertEqual(resp_single["chat"].get("last_read_ts_ms"), read_ts_1)

            # Different user (`dev_v137`) does NOT see admin's read marks
            resp_dev = await m.api_list_chats(
                kind=None,
                project_name=None,
                query=None,
                recent=False,
                limit=None,
                include_archived=True,
                current_user=dev_user,
            )
            self.assertEqual(resp_dev["chats"], [])

            # 3) HTTP fallback `POST /api/chats/mark-read` (single & `read_all`)
            phone_events.clear()
            resp_mark = await m.api_mark_chats_read(
                m.MarkChatsReadRequest(chat_id=reg_cid),
                current_user=admin_user,
            )
            self.assertTrue(resp_mark["ok"])
            self.assertTrue(
                any(
                    e.get("type") == "chat_read_synced" and e.get("chat_id") == reg_cid
                    for e in phone_events
                )
            )

            # 4) WS `{"type": "mark_all_chats_read"}` broadcasts `read_all: True`
            phone_events.clear()
            ws_sent_all = await _drive_ws_session([{"type": "mark_all_chats_read"}])
            self.assertEqual(ws_sent_all[0]["type"], "chat_read_synced")
            self.assertTrue(ws_sent_all[0]["read_all"])
            self.assertTrue(
                any(
                    e.get("type") == "chat_read_synced" and e.get("read_all") is True
                    for e in phone_events
                )
            )

            # 5) Archiving a chat via `PATCH /api/chats/{id}` broadcasts `chat_read_synced`
            phone_events.clear()
            resp_arch = await m.api_update_chat(
                proj_cid,
                m.UpdateChatRequest(is_archived=True),
                current_user=admin_user,
            )
            self.assertTrue(resp_arch["chat"]["is_archived"])
            self.assertTrue(
                any(
                    e.get("type") == "chat_read_synced" and e.get("chat_id") == proj_cid
                    for e in phone_events
                )
            )

            # 6) Sending a new `user_message` in `_run_turn_task_inner_body` automatically broadcasts `chat_read_synced`
            account = await self._make_agy_account("read137@example.test", "Read137", make_active=True)
            binary = self.tmp_root / "fake_agy_v137.exe"
            binary.write_text("fake", encoding="utf-8")

            async def _spawn_ok(*_a, **_kw):
                import asyncio

                proc = MagicMock(returncode=0)
                proc.stdout = asyncio.StreamReader()
                proc.stderr = asyncio.StreamReader()
                for ev in (
                    {"event": "init", "conversation_id": "conv-137"},
                    {
                        "event": "step_update",
                        "step_update": {
                            "step_index": 1,
                            "step_type": "agent_response",
                            "state": "DONE",
                            "text_delta": "Принято.",
                        },
                    },
                    {"event": "result", "result": {"status": "SUCCESS"}},
                ):
                    proc.stdout.feed_data((json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8"))
                proc.stdout.feed_eof()
                proc.stderr.feed_eof()
                proc.wait = AsyncMock(return_value=0)
                proc.aclose_lease = AsyncMock()
                return proc

            phone_events.clear()
            with (
                patch.object(self.agy_engine, "resolve_agy_bin", return_value=str(binary)),
                patch.object(
                    self.agy_engine.proxy_router,
                    "ensure_proxy_for_context",
                    AsyncMock(return_value="http://127.0.0.1:10808"),
                ),
                patch.object(self.agy_engine, "create_agy_subprocess_exec", side_effect=_spawn_ok),
                patch.object(
                    m,
                    "fetch_available_models",
                    AsyncMock(return_value=[{"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"}]),
                ),
                patch.object(m, "_spawn_telemetry_task", lambda coro: coro.close()),
            ):
                await self.database.update_chat(reg_cid, preferred_agy_account_id=account["id"])
                await m._run_turn_task_inner_body(
                    chat_id=reg_cid,
                    prompt="Новый вопрос с ПК",
                    attachments=[],
                    override_model="gemini-4-carbon-high",
                    override_access_mode="read_only",
                    trigger_user=admin_user,
                )

            event_types = [e.get("type") for e in phone_events]
            self.assertIn("user_message", event_types)
            self.assertIn("chat_read_synced", event_types)
            self.assertLess(
                event_types.index("user_message"),
                event_types.index("turn_done"),
            )

            # 7) Deleting a chat purges read-ts memory & notifies connected clients
            phone_events.clear()
            resp_del = await m.api_delete_chat(reg_cid, current_user=admin_user)
            self.assertTrue(resp_del["ok"])
            self.assertNotIn(reg_cid, m._USER_CHAT_READ_TS.get("admin", {}))
            self.assertTrue(
                any(
                    e.get("type") == "chat_read_synced" and e.get("chat_id") == reg_cid
                    for e in phone_events
                )
            )
        finally:
            m.ws_manager.active.pop(ws_pc, None)
            m.ws_manager.active.pop(ws_phone, None)
            m.ws_manager.active.pop(ws_other, None)

        self._assert_live_db_untouched()

    def test_02_ui_filter_scoped_activity_badges_read_clearing_archive_delete_storage_and_poll_races(self):
        static_dir = ROOT_DIR / "app" / "static"
        index_html = (static_dir / "index.html").read_text(encoding="utf-8")
        sw_js = (static_dir / "sw.js").read_text(encoding="utf-8")
        css_text = (static_dir / "styles.css").read_text(encoding="utf-8")

        # 1) Verify static cache version >= 137 & exact sync across index.html and sw.js
        m_sw_cache = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        m_sw_css = re.search(r"/static/styles\.css\?v=(\d+)", sw_js)
        m_sw_js = re.search(r"/static/app\.js\?v=(\d+)", sw_js)
        m_html_css = re.search(r"/static/styles\.css\?v=(\d+)", index_html)
        m_html_js = re.search(r"/static/app\.js\?v=(\d+)", index_html)
        for m_ver in (m_sw_cache, m_sw_css, m_sw_js, m_html_css, m_html_js):
            self.assertIsNotNone(m_ver)
            assert m_ver is not None
            self.assertGreaterEqual(int(m_ver.group(1)), 137)
        versions = {
            int(m_sw_cache.group(1)),  # type: ignore[union-attr]
            int(m_sw_css.group(1)),  # type: ignore[union-attr]
            int(m_sw_js.group(1)),  # type: ignore[union-attr]
            int(m_html_css.group(1)),  # type: ignore[union-attr]
            int(m_html_js.group(1)),  # type: ignore[union-attr]
        }
        self.assertEqual(len(versions), 1, f"Version mismatch: {versions}")

        # 2) Syntax & UTF-8 without BOM & main.workspace CSS invariant
        for rel_py in ("app/main.py", "tests/test_dynamic_activity_badges_v137.py"):
            py_compile.compile(str(ROOT_DIR / rel_py), doraise=True)
        for rel_js in ("app/static/app.js", "app/static/sw.js"):
            chk = subprocess.run(
                ["node", "--check", str(ROOT_DIR / rel_js)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                **hq_iso._subprocess_hidden_kwargs(),
            )
            self.assertEqual(chk.returncode, 0, chk.stderr)
        for rel in (
            "app/main.py",
            "app/static/app.js",
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/sw.js",
            "tests/test_dynamic_activity_badges_v137.py",
        ):
            raw = (ROOT_DIR / rel).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{rel} has UTF-8 BOM")
            raw.decode("utf-8")

        for m_ws in re.finditer(r"(?<![-\w])main\.workspace\s*\{([^}]*)\}", css_text):
            blk = m_ws.group(1)
            for forbidden in ("transform", "filter", "backdrop-filter", "perspective", "contain", "container-type"):
                self.assertIsNone(re.search(rf"(?<![-\w]){re.escape(forbidden)}\s*:", blk))

        # 3) Comprehensive Node VM E2E verification of all v137 UI behaviors
        vm_js = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const rootDir = process.argv[2];
const htmlText = fs.readFileSync(rootDir + "/app/static/index.html", "utf8");
const appCode = fs.readFileSync(rootDir + "/app/static/app.js", "utf8");

class FakeClassList {
  constructor(owner) { this.owner = owner; this.set = new Set(); }
  _sync() { this.owner._className = Array.from(this.set).join(" "); }
  add(...xs) { for (const x of xs) if (x) this.set.add(x); this._sync(); }
  remove(...xs) { for (const x of xs) this.set.delete(x); this._sync(); }
  toggle(x, force) {
    const next = force === undefined ? !this.set.has(x) : Boolean(force);
    if (next) this.set.add(x); else this.set.delete(x);
    this._sync();
    return next;
  }
  contains(x) { return this.set.has(x); }
}

class FakeEl {
  constructor(tag = "div", id = "") {
    this.tagName = String(tag).toUpperCase();
    this.id = id;
    this._className = "";
    this.classList = new FakeClassList(this);
    this.dataset = {};
    this.attributes = {};
    this.style = { setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; } };
    this.children = [];
    this.parentElement = null;
    this.listeners = {};
    this._innerHTML = "";
    this._textContent = "";
    this.value = "";
    this.title = "";
    this.disabled = false;
    this.options = [];
  }
  get className() { return this._className; }
  get childNodes() { return this.children; }
  get firstChild() { return this.children[0] || null; }
  set className(v) {
    this._className = String(v || "");
    this.classList.set = new Set(this._className.split(/\s+/).filter(Boolean));
  }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(v) {
    this._innerHTML = String(v || "");
    this._textContent = this._innerHTML.replace(/<[^>]*>/g, "");
    this.children = [];
  }
  get textContent() {
    return this.children.length > 0
      ? this.children.map((c) => c.textContent).join("")
      : this._textContent;
  }
  set textContent(v) {
    this._textContent = String(v ?? "");
    this._innerHTML = this._textContent;
    this.children = [];
  }
  appendChild(ch) {
    if (ch) {
      if (ch.parentElement) ch.remove();
      ch.parentElement = this;
      this.children.push(ch);
    }
    return ch;
  }
  insertBefore(ch) { return this.appendChild(ch); }
  remove() {
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter((x) => x !== this);
      this.parentElement = null;
    }
  }
  replaceChildren(...nodes) {
    this.children = [];
    for (const n of nodes) this.appendChild(n);
  }
  insertAdjacentHTML(_pos, html) { this._innerHTML += String(html || ""); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(ev, fn) {
    if (!this.listeners[ev]) this.listeners[ev] = [];
    this.listeners[ev].push(fn);
  }
  removeEventListener() {}
  focus() {}
  blur() {}
  scrollTo() {}
  getBoundingClientRect() { return { top: 0, bottom: 600, left: 0, right: 260, width: 260, height: 600 }; }
  closest(sel) {
    if (sel === "[data-tree-filter]" && this.dataset.treeFilter) return this;
    return this.parentElement ? this.parentElement.closest(sel) : null;
  }
  contains(other) {
    let cur = other;
    while (cur) { if (cur === this) return true; cur = cur.parentElement; }
    return false;
  }
  _matches(sel) {
    if (sel === "[data-tree-filter]") return Boolean(this.dataset && this.dataset.treeFilter);
    const mAttr = sel.match(/^\[data-([a-z-]+)="([^"]+)"\]$/);
    if (mAttr) {
      const camel = mAttr[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      return Boolean(this.dataset && String(this.dataset[camel]) === mAttr[2]);
    }
    const mNav = sel.match(/^\.nav-item\[data-chat-id="([^"]+)"\]$/);
    if (mNav) return this.classList.contains("nav-item") && String(this.dataset.chatId) === mNav[1];
    if (sel.startsWith(".")) return this.classList.contains(sel.slice(1));
    return false;
  }
  querySelector(sel) {
    for (const ch of this.children) {
      if (ch._matches(sel)) return ch;
      const found = ch.querySelector(sel);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (node) => {
      for (const ch of node.children) {
        if (ch._matches(sel)) out.push(ch);
        walk(ch);
      }
    };
    walk(this);
    return out;
  }
}

const byId = new Map();
for (const m of htmlText.matchAll(/\bid="([^"]+)"/g)) {
  if (!byId.has(m[1])) byId.set(m[1], new FakeEl("div", m[1]));
}
// Wire filter buttons inside #sidebarFilterBar just like index.html
const filterBar = byId.get("sidebarFilterBar");
const btnAll = byId.get("treeFilterBtnAll"); btnAll.dataset.treeFilter = "all";
const bdgAll = byId.get("treeFilterBadgesAll"); bdgAll.dataset.filterBadges = "all"; bdgAll.classList.add("hidden");
btnAll.appendChild(bdgAll);
const btnProj = byId.get("treeFilterBtnProjects"); btnProj.dataset.treeFilter = "projects";
const bdgProj = byId.get("treeFilterBadgesProjects"); bdgProj.dataset.filterBadges = "projects"; bdgProj.classList.add("hidden");
btnProj.appendChild(bdgProj);
const btnChats = byId.get("treeFilterBtnChats"); btnChats.dataset.treeFilter = "chats";
const bdgChats = byId.get("treeFilterBadgesChats"); bdgChats.dataset.filterBadges = "chats"; bdgChats.classList.add("hidden");
btnChats.appendChild(bdgChats);
filterBar.appendChild(btnAll);
filterBar.appendChild(btnProj);
filterBar.appendChild(btnChats);

const storageMap = new Map();
const fakeStorage = {
  getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
};

const wsSent = [];
class FakeWS {
  constructor() { this.readyState = 1; }
  send(raw) { wsSent.push(JSON.parse(raw)); }
  close() { this.readyState = 3; }
}
FakeWS.OPEN = 1;

const chatsDb = new Map([
  ["c_open_reg", { id: "c_open_reg", kind: "chat", title: "Открытый чат", is_generating: false, is_archived: false, updated_at: "2026-11-19T01:50:00Z" }],
  ["c_bg_reg", { id: "c_bg_reg", kind: "chat", title: "Фоновый чат", is_generating: false, is_archived: false, updated_at: "2026-11-19T01:51:00Z" }],
  ["c_bg_proj", { id: "c_bg_proj", kind: "project", project_name: "Antigravity_Hub", title: "Ядро v137", is_generating: false, is_archived: false, updated_at: "2026-11-19T01:52:00Z" }],
  ["c_proj_2", { id: "c_proj_2", kind: "project", project_name: "Antigravity_Hub", title: "ШТАБ", is_generating: false, is_archived: false, updated_at: "2026-11-19T01:53:00Z" }],
]);

const fetchCalls = [];
const mockFetch = async (urlStr, opts = {}) => {
  fetchCalls.push({ url: String(urlStr), method: (opts.method || "GET").toUpperCase(), body: opts.body ? JSON.parse(opts.body) : null });
  const u = new URL(String(urlStr), "http://127.0.0.1:8765");
  if (u.pathname.startsWith("/api/chats/")) {
    const cid = decodeURIComponent(u.pathname.split("/")[3]);
    const c = Object.assign({}, chatsDb.get(cid) || { id: cid, kind: "chat", title: "Диалог" });
    if ((opts.method || "GET").toUpperCase() === "PATCH" && opts.body) {
      Object.assign(c, JSON.parse(opts.body));
      chatsDb.set(cid, c);
      return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({ chat: c }), text: async () => JSON.stringify({ chat: c }) };
    }
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ chat: c, messages: [{ id: 1, role: "assistant", status: "done", content: "Готово" }], queued_messages: [], queue_paused: false }),
      text: async () => "{}",
    };
  }
  return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => ({ ok: true, projects: [] }), text: async () => "{}" };
};

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval: () => 1,
  clearInterval: () => {},
  requestAnimationFrame: (fn) => { fn(); return 1; },
  cancelAnimationFrame: () => {},
  Node: { ELEMENT_NODE: 1 },
  Intl, URL, URLSearchParams, encodeURIComponent, decodeURIComponent,
  Math, Date, JSON, Map, Set, WeakMap, Promise, RegExp, Array, Object, String, Number, Boolean, Error,
  Infinity, NaN, parseInt, parseFloat, isNaN, isFinite,
  performance: { now: () => Date.now() },
  CSS: { escape: (s) => String(s) },
  FormData: class FormData {},
  Blob: class Blob {},
  AbortController: class AbortController { abort() {} },
  WebSocket: FakeWS,
  fetch: mockFetch,
  localStorage: fakeStorage,
  sessionStorage: fakeStorage,
  navigator: { userAgent: "NodeTest", clipboard: { writeText: async () => {} } },
  location: { protocol: "http:", host: "127.0.0.1:8765", hostname: "127.0.0.1", port: "8765", pathname: "/", search: "", hash: "", origin: "http://127.0.0.1:8765", href: "http://127.0.0.1:8765/" },
  getSelection: () => null,
  matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
  document: {
    title: "Antigravity Hub",
    hidden: false,
    documentElement: new FakeEl("html"),
    body: new FakeEl("body"),
    getElementById: (id) => {
      if (!byId.has(id)) byId.set(id, new FakeEl("div", id));
      return byId.get(id);
    },
    createElement: (tag) => new FakeEl(tag),
    createDocumentFragment: () => new FakeEl("fragment"),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.addEventListener = () => {};
sandbox.window.removeEventListener = () => {};

vm.createContext(sandbox);
vm.runInContext(appCode, sandbox, { filename: "app.js" });
const api = sandbox.__AGY_ADVISOR_API__;

(async () => {
  // Initialize state: user `admin`, open regular chat `c_open_reg`, active filter = `chats` (exact screenshot setup!)
  api.state.token = "tok_admin";
  api.state.currentUser = { id: "admin", username: "admin", role: "admin", max_access_mode: "full_access" };
  api.state.ws = new FakeWS();
  api.state.projects = [{ name: "Antigravity_Hub", is_self_hub: true, execution_mode: "host", hub_chat_count: 2 }];
  api.state.expandedProjects.add("Antigravity_Hub");
  api.state.chats = Array.from(chatsDb.values()).map((c) => Object.assign({}, c));
  api.state.chatsFullListLoadedOnce = true;
  api.state.activeChatId = "c_open_reg";
  api.state.activeChat = Object.assign({}, chatsDb.get("c_open_reg"));
  api.state.treeFilter = "chats";
  api.renderUnifiedSidebar();

  const actSection = byId.get("sidebarActivitySection");
  const actBadges = byId.get("activitySectionBadges");
  const actList = byId.get("sidebarActivityList");
  const clearBtn = byId.get("clearUnreadDoneBtn");
  const toggleRadar = byId.get("sidebarToggleRadar");
  const projTreeList = byId.get("projectsTreeList");

  assert.ok(actSection.classList.contains("hidden"), "Initially 0 active/unread items -> Activity section hidden");
  assert.equal(sandbox.document.title, "Antigravity Hub");

  // =========================================================================
  // 1) Screenshot Bug Fix: Project chat finishes while filter = `chats`
  //    -> NO phantom `✓ 1` in `Активность` header or `✓ Прочитано` on `Чаты`,
  //       while `Проекты` filter tab lights up with `✓1`!
  // =========================================================================
  api.handleWsEvent({ type: "assistant_start", chat_id: "c_bg_proj" });
  // While running in `projects`, `Проекты` filter tab shows pulsing `.tfb-run-dot`, while `Чаты` Activity stays hidden!
  assert.ok(!bdgProj.classList.contains("hidden"), "Projects filter badge visible while project chat runs");
  assert.ok(bdgProj.innerHTML.includes("tfb-run-dot"), "Projects filter badge renders pulsing .tfb-run-dot");
  assert.ok(actSection.classList.contains("hidden"), "Activity section on `chats` filter stays hidden for project run");
  assert.ok(actBadges.classList.contains("hidden"), "Activity header badges on `chats` filter stay hidden");

  api.handleWsEvent({ type: "turn_done", chat_id: "c_bg_proj", chat: Object.assign({}, chatsDb.get("c_bg_proj"), { is_generating: false }) });

  assert.equal(api.state.unreadCompletedChats.size, 1, "c_bg_proj recorded as unread");
  assert.ok(actSection.classList.contains("hidden"), "On `chats` filter, Activity section stays hidden");
  assert.ok(actBadges.classList.contains("hidden"), "On `chats` filter, NO phantom `✓ 1` in #activitySectionBadges!");
  assert.equal(actBadges.innerHTML, "", "#activitySectionBadges innerHTML is empty on `chats` filter");
  assert.ok(clearBtn.classList.contains("hidden"), "On `chats` filter, #clearUnreadDoneBtn stays hidden");

  // Meanwhile, `Проекты` & `Все` filter buttons show `✓1`, and global radar + title show `✓ 1`!
  assert.ok(btnProj.classList.contains("has-filter-unread"), "Projects filter button has .has-filter-unread");
  assert.ok(!bdgProj.classList.contains("hidden") && bdgProj.innerHTML.includes("✓1"), "Projects filter button displays ✓1 mini-badge");
  assert.ok(!bdgAll.classList.contains("hidden") && bdgAll.innerHTML.includes("✓1"), "All filter button displays ✓1 mini-badge");
  assert.ok(bdgChats.classList.contains("hidden"), "Chats filter button has NO unread mini-badge");
  assert.ok(!toggleRadar.classList.contains("hidden") && toggleRadar.innerHTML.includes("✓1"), "Global burger radar shows ✓1");
  assert.equal(sandbox.document.title, "(✓ 1) Antigravity Hub");

  // =========================================================================
  // 2) Switch to `projects` filter -> `✓ 1` appears in Activity & Project header;
  //    Open `c_bg_proj` -> immediately clears `unreadCompletedChats`, hides empty
  //    `Активность` (no sticky idle chat tail!), updates project badge, radar,
  //    title, filter badges, AND sends `mark_chat_read` over WebSocket!
  // =========================================================================
  api.state.treeFilter = "projects";
  api.renderUnifiedSidebar();
  assert.ok(!actSection.classList.contains("hidden"), "On `projects` filter, Activity section shows c_bg_proj");
  assert.ok(actBadges.innerHTML.includes("✓ 1"), "On `projects` filter, #activitySectionBadges shows `✓ 1`");
  assert.ok(!clearBtn.classList.contains("hidden"), "On `projects` filter, #clearUnreadDoneBtn is visible");
  assert.ok(projTreeList.children[0].children[0].innerHTML.includes("proj-count-done"), "Project header shows ✓ 1 badge");

  wsSent.length = 0;
  await api.openHubChat("c_bg_proj");

  assert.equal(api.state.unreadCompletedChats.size, 0, "Opening c_bg_proj cleared unreadCompletedChats");
  assert.ok(actSection.classList.contains("hidden"), "Activity section immediately hid after reading (no sticky read_completed tail!)");
  assert.equal(actList.children.length, 0, "Activity list has 0 lingering read items");
  assert.ok(actBadges.classList.contains("hidden"), "#activitySectionBadges hid after reading");
  assert.ok(clearBtn.classList.contains("hidden"), "#clearUnreadDoneBtn hid after reading");
  assert.ok(bdgProj.classList.contains("hidden"), "Projects filter mini-badge disappeared after reading");
  assert.ok(bdgAll.classList.contains("hidden"), "All filter mini-badge disappeared after reading");
  assert.ok(!projTreeList.children[0].children[0].innerHTML.includes("proj-count-done"), "Project header ✓ 1 disappeared after reading");
  assert.ok(toggleRadar.classList.contains("hidden"), "#sidebarToggleRadar hid after reading");
  assert.equal(sandbox.document.title, "Antigravity Hub", "document.title restored after reading");
  assert.ok(
    wsSent.some((m) => m.type === "mark_chat_read" && m.chat_id === "c_bg_proj"),
    "Opening unread chat sent WS mark_chat_read: " + JSON.stringify(wsSent)
  );

  // =========================================================================
  // 3) Scoped `✓ Прочитано`: on filter `chats`, clears ONLY `chats` unread items,
  //    preserving `projects` unread items!
  // =========================================================================
  await api.openHubChat("c_open_reg");
  api.handleWsEvent({ type: "assistant_start", chat_id: "c_proj_2" });
  api.handleWsEvent({ type: "turn_done", chat_id: "c_proj_2" });
  api.handleWsEvent({ type: "assistant_start", chat_id: "c_bg_reg" });
  api.handleWsEvent({ type: "turn_done", chat_id: "c_bg_reg" });
  assert.equal(api.state.unreadCompletedChats.size, 2);

  api.state.treeFilter = "chats";
  api.renderUnifiedSidebar();
  assert.ok(actBadges.innerHTML.includes("✓ 1"), "Scoped Activity badge on `chats` shows ✓ 1 (not ✓ 2)");

  api.clearAllUnreadCompleted({ scope: "chats" });
  assert.ok(!api.state.unreadCompletedChats.has("c_bg_reg"), "Regular unread chat cleared");
  assert.ok(api.state.unreadCompletedChats.has("c_proj_2"), "Project unread chat preserved when clearing on `chats` filter");
  assert.ok(actSection.classList.contains("hidden"), "Activity section on `chats` hid immediately");
  assert.ok(bdgChats.classList.contains("hidden"), "Chats filter badge hid");
  assert.ok(!bdgProj.classList.contains("hidden") && bdgProj.innerHTML.includes("✓1"), "Projects filter badge still shows ✓1");

  // =========================================================================
  // 4) Incoming WS `chat_read_synced` (from PC) clears Smartphone badge without echo loop
  // =========================================================================
  wsSent.length = 0;
  api.handleWsEvent({
    type: "chat_read_synced",
    chat_id: "c_proj_2",
    read_all: false,
    read_ts_ms: Date.now() + 50,
  });
  assert.equal(api.state.unreadCompletedChats.size, 0, "WS chat_read_synced cleared c_proj_2");
  assert.equal(wsSent.length, 0, "WS chat_read_synced must NOT echo mark_chat_read back to server");
  assert.ok(bdgProj.classList.contains("hidden"));
  assert.equal(sandbox.document.title, "Antigravity Hub");

  // =========================================================================
  // 5) Archive, Delete, WS `user_message`, and Cross-Tab `storage` event auto-clear
  // =========================================================================
  api.state.treeFilter = "all";
  // 5a) Archive unread chat
  api.handleWsEvent({ type: "assistant_start", chat_id: "c_proj_2" });
  api.handleWsEvent({ type: "turn_done", chat_id: "c_proj_2" });
  assert.ok(api.state.unreadCompletedChats.has("c_proj_2"));
  await api.setChatArchivedState("c_proj_2", true, { silentToast: true });
  assert.ok(!api.state.unreadCompletedChats.has("c_proj_2"), "Archiving unread chat cleared its unread badge");
  assert.ok(toggleRadar.classList.contains("hidden"));
  await api.setChatArchivedState("c_proj_2", false, { silentToast: true });

  // 5b) WS `user_message` clears unread chat
  api.handleWsEvent({ type: "assistant_start", chat_id: "c_bg_reg" });
  api.handleWsEvent({ type: "turn_done", chat_id: "c_bg_reg" });
  assert.ok(api.state.unreadCompletedChats.has("c_bg_reg"));
  api.handleWsEvent({
    type: "user_message",
    chat_id: "c_bg_reg",
    message: { id: 99, role: "user", content: "Ответ с другого устройства" },
  });
  assert.ok(!api.state.unreadCompletedChats.has("c_bg_reg"), "user_message cleared unread badge");

  // 5c) Cross-tab `storage` event clears unread chat
  api.handleWsEvent({ type: "assistant_start", chat_id: "c_bg_reg" });
  api.handleWsEvent({ type: "turn_done", chat_id: "c_bg_reg" });
  assert.ok(api.state.unreadCompletedChats.has("c_bg_reg"));
  api.handleCrossTabUnreadStorageEvent({
    key: "agy_hub_unread_done_admin",
    newValue: "[]",
  });
  assert.equal(api.state.unreadCompletedChats.size, 0, "Cross-tab storage event cleared unread badge");
  assert.ok(actSection.classList.contains("hidden"));

  // 5d) Deleting unread chat clears all badges immediately
  api.handleWsEvent({ type: "assistant_start", chat_id: "c_bg_reg" });
  api.handleWsEvent({ type: "turn_done", chat_id: "c_bg_reg" });
  assert.ok(api.state.unreadCompletedChats.has("c_bg_reg"));
  api.purgeDeletedChatState("c_bg_reg");
  assert.equal(api.state.unreadCompletedChats.size, 0, "Deleting unread chat cleared unread badge");
  assert.equal(sandbox.document.title, "Antigravity Hub");

  // =========================================================================
  // 6) `reconcileGeneratingFromChats` poll race guard (<15s recent terminal WS + `last_read_ts_ms`)
  //    & `pruneStaleUnreadAndRunningState` server `last_read_ts_ms >= unreadEntry.ts`
  // =========================================================================
  // 6a) Recent terminal WS (<15s) prevents HTTP poll from resurrecting unread badge after user read it
  api.state.watchedRunningIds.add("c_proj_2");
  api.state.recentlyFinishedAt.set("c_proj_2", Date.now() - 2000);
  api.reconcileGeneratingFromChats([
    Object.assign({}, chatsDb.get("c_open_reg")),
    Object.assign({}, chatsDb.get("c_bg_proj")),
    Object.assign({}, chatsDb.get("c_proj_2"), { is_generating: false }),
  ], { isFullList: true });
  assert.ok(!api.state.unreadCompletedChats.has("c_proj_2"), "Recent terminal WS (<15s) blocked poll resurrection");

  // 6b) Sleeping phone wakes up (`watchedRunningIds` has c_proj_2, no recent WS), but `last_read_ts_ms >= updated_at` from PC!
  api.state.recentlyFinishedAt.delete("c_proj_2");
  api.state.watchedRunningIds.add("c_proj_2");
  const updMs = Date.parse("2026-11-19T01:53:00Z");
  api.reconcileGeneratingFromChats([
    Object.assign({}, chatsDb.get("c_open_reg")),
    Object.assign({}, chatsDb.get("c_bg_proj")),
    Object.assign({}, chatsDb.get("c_proj_2"), { is_generating: false, last_read_ts_ms: updMs + 5000 }),
  ], { isFullList: true });
  assert.ok(!api.state.unreadCompletedChats.has("c_proj_2"), "Server last_read_ts_ms prevented phantom unread on wake-up");

  // 6c) Existing unread entry on phone gets pruned when `/api/chats` delivers `last_read_ts_ms >= unreadEntry.ts`
  api.markChatUnreadCompleted("c_proj_2", "done", chatsDb.get("c_proj_2"));
  const entryTs = api.state.unreadCompletedChats.get("c_proj_2").ts;
  api.state.chats = [
    Object.assign({}, chatsDb.get("c_open_reg")),
    Object.assign({}, chatsDb.get("c_bg_proj")),
    Object.assign({}, chatsDb.get("c_proj_2"), { last_read_ts_ms: entryTs + 10 }),
  ];
  api.pruneStaleUnreadAndRunningState();
  assert.ok(!api.state.unreadCompletedChats.has("c_proj_2"), "pruneStaleUnreadAndRunningState pruned entry read on server");

  console.log("V137_DYNAMIC_BADGES_OK");
  process.exit(0);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
        vm_script = self.tmp_root / "v137_vm.js"
        vm_script.write_text(vm_js, encoding="utf-8")
        vm_res = subprocess.run(
            ["node", str(vm_script), str(ROOT_DIR)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
            **hq_iso._subprocess_hidden_kwargs(),
        )
        self.assertEqual(
            vm_res.returncode,
            0,
            f"STDOUT:\n{vm_res.stdout}\nSTDERR:\n{vm_res.stderr}",
        )
        self.assertIn("V137_DYNAMIC_BADGES_OK", vm_res.stdout)
        self._assert_live_db_untouched()


if __name__ == "__main__":
    unittest.main()
