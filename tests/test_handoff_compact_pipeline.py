"""
Isolated unit, WebSocket & Node VM tests for Antigravity Hub v=83:
Unified «🚀 В Код» Launch Window, Model Selector, OpenRouter Boosters & Atomic `compact_first` Pipeline.

Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or `run_server.py`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import HTTPException

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


class _DummyWS:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = list(payloads)
        self.sent: list[dict[str, Any]] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict[str, Any]:
        if self._payloads:
            return self._payloads.pop(0)
        import app.main as main_mod

        raise main_mod.WebSocketDisconnect()

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        pass


class TestHandoffCompactPipeline(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database
        import app.main as main_mod

        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_hub_v83_test_", ignore_cleanup_errors=True
        )
        self.tmp_root = Path(self._tmp.name).resolve()

        self._orig = {
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_cli": config.CLI_HOMES_DIR,
            "cfg_agy": config.AGY_CLI_DATA_DIR,
            "eng_agy": agy_engine.AGY_CLI_DATA_DIR,
            "cfg_proj": config.PROJECTS_ROOT,
            "db_proj": database.PROJECTS_ROOT,
            "br_proj": brain_reader.PROJECTS_ROOT,
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

        tmp_db = self.tmp_root / "test_v83.db"
        tmp_cli = self.tmp_root / "cli_homes"
        tmp_agy = self.tmp_root / "agy_cli_data"
        tmp_proj = self.tmp_root / "projects"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sh = self.tmp_root / "shared_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_sbx = self.tmp_root / "chat_sandbox"
        for d in (tmp_cli, tmp_agy, tmp_proj, tmp_ws, tmp_sh, tmp_up, tmp_sbx):
            d.mkdir(parents=True, exist_ok=True)

        config.DB_PATH = tmp_db
        database.DB_PATH = tmp_db
        config.CLI_HOMES_DIR = tmp_cli
        config.AGY_CLI_DATA_DIR = tmp_agy
        agy_engine.AGY_CLI_DATA_DIR = tmp_agy
        config.PROJECTS_ROOT = tmp_proj
        database.PROJECTS_ROOT = tmp_proj
        brain_reader.PROJECTS_ROOT = tmp_proj
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

        main_mod.CHAT_TURN_QUEUES.clear()
        main_mod.CHAT_QUEUE_PAUSED.clear()
        main_mod.COMPACTING_CHATS.clear()
        main_mod.ACTIVE_TURN_TASKS.clear()

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database
        import app.main as main_mod

        main_mod.CHAT_TURN_QUEUES.clear()
        main_mod.CHAT_QUEUE_PAUSED.clear()
        main_mod.COMPACTING_CHATS.clear()
        main_mod.ACTIVE_TURN_TASKS.clear()

        config.DB_PATH = self._orig["cfg_db"]
        database.DB_PATH = self._orig["db_db"]
        config.CLI_HOMES_DIR = self._orig["cfg_cli"]
        config.AGY_CLI_DATA_DIR = self._orig["cfg_agy"]
        agy_engine.AGY_CLI_DATA_DIR = self._orig["eng_agy"]
        config.PROJECTS_ROOT = self._orig["cfg_proj"]
        database.PROJECTS_ROOT = self._orig["db_proj"]
        brain_reader.PROJECTS_ROOT = self._orig["br_proj"]
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

    async def _create_admin_and_project_chat(
        self, *, access_mode: str = "write_files", model: str = "gemini-4-carbon-high"
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        import app.database as database

        await database.init_db()
        admin = await database.get_user_by_username("admin")
        assert admin is not None
        proj_dir = self.tmp_root / "projects" / "AlphaProj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        chat = await database.create_chat(
            owner_id=admin["id"],
            kind="project",
            project_name="AlphaProj",
            project_path=str(proj_dir),
            title="Разработка",
            model=model,
            access_mode=access_mode,
        )
        return admin, chat

    async def test_send_message_compact_first_on_idle_chat_runs_compact_then_message(self) -> None:
        import app.database as database
        import app.main as main_mod

        admin, chat = await self._create_admin_and_project_chat()
        cid = chat["id"]

        for i in range(4):
            await database.add_message(
                cid,
                user_id=admin["id"],
                role="user",
                content=f"Старый запрос #{i}: " + ("код " * 140),
            )
            await database.add_message(
                cid,
                user_id=admin["id"],
                role="assistant",
                content=f"Старый ответ #{i}: " + ("результат " * 160),
            )

        call_order: list[str] = []
        captured_turn_chat: dict[str, Any] = {}

        async def _fake_compact(
            chat_id: str, cur_user: dict[str, Any], *, from_queue: bool = False
        ) -> dict[str, Any]:
            self.assertEqual(chat_id, cid)
            self.assertTrue(from_queue)
            call_order.append("compact")
            return {"summary_text": "Сжатое резюме"}

        async def _fake_stream(**kwargs: Any):
            call_order.append("message")
            captured_turn_chat["model"] = kwargs.get("model")
            captured_turn_chat["access_mode"] = kwargs.get("access_mode")
            yield {
                "type": "done",
                "content": "Задача успешно выполнена после сжатия.",
                "thinking": "",
                "tool_calls": [],
                "usage": {"total_tokens": 120},
            }

        async def _noop_quotas(_uid: str) -> None:
            return None

        ws = _DummyWS(
            [
                {
                    "action": "send_message",
                    "chat_id": cid,
                    "prompt": "Выполни новое ТЗ",
                    "model": "claude-opus-4-6-thinking",
                    "access_mode": "full_access",
                    "compact_first": True,
                }
            ]
        )
        models_stub = [
            {"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"},
            {"id": "claude-opus-4-6-thinking", "label": "Claude Opus 4.6 (Thinking)"},
        ]
        with (
            patch.object(main_mod.auth, "authenticate_ws_user", return_value=admin),
            patch.object(main_mod, "_execute_chat_compaction", side_effect=_fake_compact),
            patch.object(main_mod, "stream_agy_turn", side_effect=_fake_stream),
            patch.object(main_mod, "_refresh_and_broadcast_quotas", side_effect=_noop_quotas),
            patch.object(main_mod, "fetch_available_models", return_value=models_stub),
            patch.object(
                main_mod,
                "check_docker_status",
                return_value={"installed": False, "sandbox_mcp_ready": False},
            ),
        ):
            await main_mod.websocket_endpoint(ws)  # type: ignore[arg-type]
            await asyncio.sleep(0)
            task = main_mod.ACTIVE_TURN_TASKS.get(cid)
            if task is not None:
                await asyncio.wait_for(task, timeout=5.0)

        self.assertEqual(call_order, ["compact", "message"])
        self.assertEqual(captured_turn_chat.get("model"), "claude-opus-4-6-thinking")
        self.assertEqual(captured_turn_chat.get("access_mode"), "full_access")
        self.assertFalse(main_mod.CHAT_TURN_QUEUES.get(cid))
        self.assertFalse(any(m.get("type") == "ws_error" for m in ws.sent))

        updated_chat = await database.get_chat(cid)
        assert updated_chat is not None
        self.assertEqual(updated_chat["model"], "claude-opus-4-6-thinking")
        self.assertEqual(updated_chat["access_mode"], "full_access")

    async def test_send_message_compact_first_on_busy_chat_enqueues_atomically_without_duplicates(
        self,
    ) -> None:
        import app.main as main_mod

        admin, chat = await self._create_admin_and_project_chat()
        cid = chat["id"]
        models_stub = [
            {"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"},
            {"id": "claude-opus-4-6-thinking", "label": "Claude Opus 4.6 (Thinking)"},
        ]

        # Case A: Chat is currently generating -> enqueues [compact, message] atomically
        ws1 = _DummyWS(
            [
                {
                    "action": "send_message",
                    "chat_id": cid,
                    "prompt": "Задача №1",
                    "model": "claude-opus-4-6-thinking",
                    "access_mode": "full_access",
                    "compact_first": True,
                }
            ]
        )
        with (
            patch.object(main_mod.auth, "authenticate_ws_user", return_value=admin),
            patch.object(main_mod, "is_chat_generating", side_effect=lambda x: x == cid),
            patch.object(main_mod, "fetch_available_models", return_value=models_stub),
            patch.object(
                main_mod,
                "check_docker_status",
                return_value={"installed": False, "sandbox_mcp_ready": False},
            ),
        ):
            await main_mod.websocket_endpoint(ws1)  # type: ignore[arg-type]

        q = main_mod.CHAT_TURN_QUEUES.get(cid) or []
        self.assertEqual([it["kind"] for it in q], ["compact", "message"])
        self.assertEqual(q[1]["model"], "claude-opus-4-6-thinking")
        self.assertEqual(q[1]["access_mode"], "full_access")

        # Case B: Last item in queue is already `compact` -> does NOT insert duplicate `compact`
        main_mod.CHAT_TURN_QUEUES[cid] = [q[0]]
        ws2 = _DummyWS(
            [
                {
                    "action": "send_message",
                    "chat_id": cid,
                    "prompt": "Задача №2 без дубля compact",
                    "compact_first": True,
                }
            ]
        )
        with (
            patch.object(main_mod.auth, "authenticate_ws_user", return_value=admin),
            patch.object(main_mod, "is_chat_generating", side_effect=lambda x: x == cid),
            patch.object(main_mod, "fetch_available_models", return_value=models_stub),
            patch.object(
                main_mod,
                "check_docker_status",
                return_value={"installed": False, "sandbox_mcp_ready": False},
            ),
        ):
            await main_mod.websocket_endpoint(ws2)  # type: ignore[arg-type]
        self.assertEqual(
            [it["kind"] for it in main_mod.CHAT_TURN_QUEUES[cid]], ["compact", "message"]
        )

        # Case C: Chat is currently in COMPACTING_CHATS with empty queue -> appends ONLY `message`
        main_mod.CHAT_TURN_QUEUES[cid] = []
        main_mod.COMPACTING_CHATS.add(cid)
        ws3 = _DummyWS(
            [
                {
                    "action": "send_message",
                    "chat_id": cid,
                    "prompt": "Задача №3 во время текущего сжатия",
                    "compact_first": True,
                }
            ]
        )
        with (
            patch.object(main_mod.auth, "authenticate_ws_user", return_value=admin),
            patch.object(main_mod, "is_chat_generating", return_value=False),
            patch.object(main_mod, "fetch_available_models", return_value=models_stub),
            patch.object(
                main_mod,
                "check_docker_status",
                return_value={"installed": False, "sandbox_mcp_ready": False},
            ),
        ):
            await main_mod.websocket_endpoint(ws3)  # type: ignore[arg-type]
        self.assertEqual([it["kind"] for it in main_mod.CHAT_TURN_QUEUES[cid]], ["message"])

    async def test_short_chat_compact_first_skips_400_without_ws_error_and_runs_message(
        self,
    ) -> None:
        import app.main as main_mod

        admin, chat = await self._create_admin_and_project_chat()
        cid = chat["id"]
        models_stub = [{"id": "gemini-4-carbon-high", "label": "Gemini 4 Carbon (High)"}]

        compact_called = 0
        turn_called = 0

        async def _compact_raises_400(*_a: Any, **_kw: Any) -> dict[str, Any]:
            nonlocal compact_called
            compact_called += 1
            raise HTTPException(status_code=400, detail="Диалог слишком короткий для сжатия")

        async def _fake_stream(**_kwargs: Any):
            nonlocal turn_called
            turn_called += 1
            yield {
                "type": "done",
                "content": "Выполнено без ошибки 400!",
                "thinking": "",
                "tool_calls": [],
                "usage": {"total_tokens": 50},
            }

        async def _noop_quotas(_uid: str) -> None:
            return None

        # Subcase 1: Idle empty chat (`can_compact == False`) -> skips compaction completely and runs turn
        ws_idle = _DummyWS(
            [
                {
                    "action": "send_message",
                    "chat_id": cid,
                    "prompt": "Старт на свежей вкладке",
                    "compact_first": True,
                }
            ]
        )
        with (
            patch.object(main_mod.auth, "authenticate_ws_user", return_value=admin),
            patch.object(main_mod, "_execute_chat_compaction", side_effect=_compact_raises_400),
            patch.object(main_mod, "stream_agy_turn", side_effect=_fake_stream),
            patch.object(main_mod, "_refresh_and_broadcast_quotas", side_effect=_noop_quotas),
            patch.object(main_mod, "fetch_available_models", return_value=models_stub),
            patch.object(
                main_mod,
                "check_docker_status",
                return_value={"installed": False, "sandbox_mcp_ready": False},
            ),
        ):
            await main_mod.websocket_endpoint(ws_idle)  # type: ignore[arg-type]
            await asyncio.sleep(0)
            task = main_mod.ACTIVE_TURN_TASKS.get(cid)
            if task is not None:
                await asyncio.wait_for(task, timeout=5.0)

        self.assertEqual(compact_called, 0)
        self.assertEqual(turn_called, 1)
        self.assertFalse(any(m.get("type") == "ws_error" for m in ws_idle.sent))

        # Subcase 2: Queued `[compact, message]` hitting HTTPException(400) -> NO ws_error, runs message!
        broadcasted_events: list[dict[str, Any]] = []

        async def _capture_broadcast_user(_uid: str, event: dict[str, Any], **_kw: Any) -> None:
            broadcasted_events.append(event)

        main_mod.CHAT_TURN_QUEUES[cid] = [
            {
                "id": "msg_after_400",
                "chat_id": cid,
                "kind": "message",
                "prompt": "Задача сразу после пропущенного compact",
                "attachments": [],
                "referenced_chat_ids": [],
                "referenced_chats": [],
                "model": "gemini-4-carbon-high",
                "access_mode": "write_files",
                "user_id": admin["id"],
                "author_name": "Admin",
                "created_at": "2026-11-13T00:00:00Z",
            }
        ]
        with (
            patch.object(main_mod, "_execute_chat_compaction", side_effect=_compact_raises_400),
            patch.object(main_mod, "stream_agy_turn", side_effect=_fake_stream),
            patch.object(main_mod, "_refresh_and_broadcast_quotas", side_effect=_noop_quotas),
            patch.object(main_mod, "fetch_available_models", return_value=models_stub),
            patch.object(
                main_mod,
                "check_docker_status",
                return_value={"installed": False, "sandbox_mcp_ready": False},
            ),
            patch.object(
                main_mod.ws_manager, "broadcast_user", side_effect=_capture_broadcast_user
            ),
        ):
            await main_mod._run_turn_task(
                chat_id=cid,
                prompt="Сжатие контекста",
                attachments=[],
                override_model=None,
                override_access_mode=None,
                trigger_user=admin,
                initial_kind="compact",
            )

        self.assertEqual(compact_called, 1)
        self.assertEqual(turn_called, 2)
        self.assertFalse(any(e.get("type") == "ws_error" for e in broadcasted_events))
        self.assertFalse(main_mod.CHAT_TURN_QUEUES.get(cid))

        # Subcase 3: Standalone `compact` WITHOUT a queued message still broadcasts ws_error on 400
        standalone_events: list[dict[str, Any]] = []
        with (
            patch.object(main_mod, "_execute_chat_compaction", side_effect=_compact_raises_400),
            patch.object(
                main_mod.ws_manager,
                "broadcast_user",
                side_effect=lambda _u, ev, **_k: standalone_events.append(ev),
            ),
        ):
            await main_mod._run_turn_task(
                chat_id=cid,
                prompt="Сжатие контекста",
                attachments=[],
                override_model=None,
                override_access_mode=None,
                trigger_user=admin,
                initial_kind="compact",
            )
        self.assertTrue(any(e.get("type") == "ws_error" for e in standalone_events))

    def test_pwa_cache_v83_and_unified_modal_dom_and_js(self) -> None:
        index_html = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        sw_js = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        self.assertRegex(index_html, r"styles\.css\?v=(?:8[3-9]|[9]\d|\d{3,})")
        self.assertRegex(index_html, r"app\.js\?v=(?:8[3-9]|[9]\d|\d{3,})")
        self.assertRegex(sw_js, r'"agy-hub-v(?:8[3-9]|[9]\d|\d{3,})"')
        self.assertRegex(sw_js, r"/static/styles\.css\?v=(?:8[3-9]|[9]\d|\d{3,})")
        self.assertRegex(sw_js, r"/static/app\.js\?v=(?:8[3-9]|[9]\d|\d{3,})")
        for required_id in (
            'id="handoffTargetsList"',
            'id="handoffTargetNewWrap"',
            'id="handoffNewTabConfigBox"',
            'id="handoffIconPickerBox"',
            'id="handoffAdvisorSlot"',
            'id="handoffModeFullBtn"',
            'id="handoffModeWriteBtn"',
            'id="handoffModelBtn"',
            'id="handoffModelMenu"',
            'id="handoffQuotaPills"',
            'id="handoffOrBoostersBox"',
            'id="handoffExportDocBtn"',
        ):
            self.assertIn(required_id, index_html)

    def test_unified_modal_js_runtime_in_node_vm(self) -> None:
        from tests.test_task_counters_and_queues import NODE_VM_HARNESS_JS

        dom_prefix = NODE_VM_HARNESS_JS.split("async function main() {")[0]
        test_script = (
            dom_prefix
            + r"""
// The VM owns these browser timers. Its quota poll re-arms after ~4 minutes;
// release them after all assertions rather than keeping the Node process alive.
const vmTimeouts = new Set();
function vmSetTimeout(callback, delay, ...args) {
  const timer = setTimeout(() => {
    vmTimeouts.delete(timer);
    callback(...args);
  }, delay);
  vmTimeouts.add(timer);
  return timer;
}
function vmClearTimeout(timer) {
  vmTimeouts.delete(timer);
  clearTimeout(timer);
}
async function main() {
  const rootDir = process.env.HUB_ROOT_DIR;
  const htmlText = fs.readFileSync(path.join(rootDir, "app/static/index.html"), "utf8");
  const appCode = fs.readFileSync(path.join(rootDir, "app/static/app.js"), "utf8");

  const doc = new MiniDocument();
  const bodyMatch = htmlText.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  assert(bodyMatch, "body found in index.html");
  doc.body.innerHTML = bodyMatch[1].replace(/<script[\s\S]*?<\/script>/gi, "");

  const localStorage = new MemoryStorage();
  const sessionStorage = new MemoryStorage();
  localStorage.setItem("agy_hub_token", "tok_dev");

  const nowIso = "2026-11-13T10:00:00Z";
  const models = [
    { id: "gemini-4-carbon-high", label: "Gemini 4 Carbon (High)" },
    { id: "claude-opus-4-6-thinking", label: "Claude Opus 4.6 (Thinking)" },
    { id: "codex:gpt-5.4-high", label: "GPT-5.4 (High) · ChatGPT" },
  ];
  const chatsDb = new Map([
    ["c_plan", {
      id: "c_plan", title: "Обсуждение", kind: "project", project_name: "AlphaProj",
      access_mode: "read_only", model: "gemini-4-carbon-high", is_generating: false, updated_at: nowIso,
    }],
    ["c_heavy", {
      id: "c_heavy", title: "Тяжёлая вкладка", kind: "project", project_name: "AlphaProj",
      access_mode: "full_access", model: "claude-opus-4-6-thinking", is_generating: false, updated_at: nowIso,
      context_pct: 72, context_stats: { pct: 72, used_tokens: 92000, can_compact: true },
    }],
    ["c_empty", {
      id: "c_empty", title: "Свежая вкладка", kind: "project", project_name: "AlphaProj",
      access_mode: "write_files", model: "codex:gpt-5.4-high", is_generating: false, updated_at: nowIso,
      context_pct: 2, context_stats: { pct: 2, used_tokens: 800, can_compact: false },
    }],
  ]);

  let sentWsPayloads = [];
  let lastCreatedChatBody = null;
  let orSummary = { has_key: true, is_enabled: true, credits_remaining: 14.5 };

  class MockWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      setTimeout(() => { if (typeof this.onopen === "function") this.onopen({}); }, 0);
    }
    send(raw) { sentWsPayloads.push(JSON.parse(raw)); }
    close() { this.readyState = 3; }
  }
  MockWebSocket.OPEN = 1;

  async function mockFetch(urlStr, opts = {}) {
    const u = new URL(urlStr, "http://127.0.0.1:8080");
    const p = u.pathname;
    const method = (opts.method || "GET").toUpperCase();
    const jsonResp = (obj) => ({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => obj,
      text: async () => JSON.stringify(obj),
    });
    const curUser = { id: "u_dev", username: "dev", display_name: "Dev", role: "admin", is_admin: true, max_access_mode: "host_full" };

    if (p === "/api/auth/check" || p === "/api/auth/status") {
      return jsonResp({ authenticated: true, auth_enabled: true, user: curUser });
    }
    if (p === "/api/models") {
      return jsonResp({ models, default: "gemini-4-carbon-high" });
    }
    if (p === "/api/system/info") {
      return jsonResp({ user: curUser, models, docker: { installed: true, sandbox_mcp_ready: false }, cli_auth: { ok: true, models_count: models.length }, openrouter_summary: orSummary, chatgpt_summary: { has_account: true, is_enabled: true, quotas: { primary_5h: { remaining_pct: 84 } } }, account_profiles: [] });
    }
    if (p === "/api/quotas" || p === "/api/system/quotas") {
      return jsonResp({ quotas: { gemini_5h_pct: 91, claude_5h_pct: 48 }, token_stats: {}, tier_summary: {} });
    }
    if (p === "/api/projects") return jsonResp({ projects: [{ name: "AlphaProj", path: "/ws/AlphaProj", is_shared: false }] });
    if (p === "/api/chats" && method === "GET") {
      const kind = u.searchParams.get("kind");
      const proj = u.searchParams.get("project_name");
      let list = Array.from(chatsDb.values());
      if (kind) list = list.filter((c) => c.kind === kind);
      if (proj) list = list.filter((c) => c.project_name === proj);
      return jsonResp({ chats: list });
    }
    if (p === "/api/chats" && method === "POST") {
      lastCreatedChatBody = JSON.parse(opts.body || "{}");
      const created = Object.assign({ id: "c_new_1", is_generating: false, updated_at: nowIso }, lastCreatedChatBody);
      chatsDb.set(created.id, created);
      return jsonResp({ chat: created });
    }
    if (p.startsWith("/api/chats/") && method === "GET") {
      const cid = decodeURIComponent(p.split("/")[3]);
      const chat = chatsDb.get(cid);
      const msgs = cid === "c_plan"
        ? [{ id: 501, role: "assistant", status: "done", content: "## План релиза\nСделать единое окно запуска.", created_at: nowIso }]
        : [];
      return jsonResp({ chat, messages: msgs, context_stats: chat && chat.context_stats || { fill_pct: 5, used_tokens: 1200 }, queued_messages: [], queue_paused: false });
    }
    return jsonResp({ ok: true, conversations: [], items: [], rules: "" });
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const win = {
    document: doc, localStorage, sessionStorage, location: { protocol: "http:", host: "127.0.0.1:8080", pathname: "/", search: "", hash: "" },
    navigator: { clipboard: { writeText: async () => {} }, userAgent: "Mozilla/5.0" },
    innerWidth: 1280, innerHeight: 860, fetch: mockFetch, WebSocket: MockWebSocket,
    requestAnimationFrame: (cb) => setTimeout(cb, 0), cancelAnimationFrame: clearTimeout,
    setTimeout: vmSetTimeout, clearTimeout: vmClearTimeout, setInterval: () => 1, clearInterval: () => {},
    matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => true, scrollTo: () => {},
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    ResizeObserver: class ResizeObserver { observe() {} unobserve() {} disconnect() {} },
    IntersectionObserver: class IntersectionObserver { observe() {} unobserve() {} disconnect() {} },
    MutationObserver: class MutationObserver { observe() {} disconnect() {} },
    Notification: { permission: "default", requestPermission: async () => "default" },
    history: { replaceState: () => {}, pushState: () => {} },
    confirm: () => true, alert: () => {},
    performance: { now: () => Date.now() },
    Blob: class Blob {}, FormData: class FormData {}, FileReader: class FileReader {},
    AbortController, WeakMap,
    CSS: { supports: () => true, escape: (s) => String(s) }, URL, URLSearchParams,
  };
  win.window = win;
  win.globalThis = win;
  doc.defaultView = win;
  const ctx = vm.createContext(Object.assign({}, win, { window: win, globalThis: win, document: doc, console, Math, Date, JSON, Promise, Set, Map, WeakMap, AbortController, RegExp, Array, Object, String, Number, Boolean, Error, TypeError, encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN, isFinite, Intl }));
  vm.runInContext(appCode, ctx, { filename: "app.js" });
  await sleep(90);

  // Expand AlphaProj group in #projectsTreeList, open c_plan, and click [data-send-to-code] -> 1-click unified modal!
  const projGroup = doc.getElementById("projectsTreeList").querySelector(".proj-tree-group");
  assert(projGroup, "AlphaProj group rendered in #projectsTreeList");
  if (!projGroup.classList.contains("expanded")) {
    projGroup.querySelector(".proj-tree-header").click();
    await sleep(30);
  }
  const planNav = doc.querySelector('.nav-item[data-chat-id="c_plan"]');
  assert(planNav, "c_plan nav item rendered");
  planNav.click();
  await sleep(60);

  const stcBtn = doc.getElementById("messagesList").querySelector("[data-send-to-code]");
  assert(stcBtn, "[data-send-to-code] button rendered");
  stcBtn.click();
  await sleep(50);

  const modal = doc.getElementById("handoffNewTabModal");
  assert(!modal.classList.contains("hidden"), "1-click opened #handoffNewTabModal");
  assert(doc.getElementById("sendToCodeMenu").classList.contains("hidden"), "Legacy #sendToCodeMenu stayed hidden");
  assert(!doc.getElementById("handoffAdvisorSlot").classList.contains("hidden"), "#handoffAdvisorSlot active in Stage 2 (v84)");

  // Quota pills show Gemini, Claude, ChatGPT 5h
  const qText = doc.getElementById("handoffQuotaPills").textContent;
  assert(qText.includes("Gemini: 91%") && qText.includes("Claude: 48%") && qText.includes("ChatGPT: 84%"), `Expected 3 quota pills, got: ${qText}`);

  // Select c_empty (<10% & <12k) -> compact chip is disabled, model auto-switches to codex:gpt-5.4-high
  const emptyCard = modal.querySelector('[data-handoff-target="c_empty"]');
  const emptyCmpBtn = modal.querySelector('[data-handoff-compact-chat="c_empty"]');
  assert(emptyCmpBtn.disabled, "Compact chip disabled on nearly empty tab");
  emptyCard.click();
  assert(doc.getElementById("handoffModelName").textContent.includes("GPT-5.4"), "Auto-picked c_empty model");
  assert.strictEqual(doc.getElementById("handoffModelSourceTag").textContent, "модель вкладки");

  // Select c_heavy (72% >= 65%) -> auto-checks compact chip, confirm button updates, 1-click unchecks & remembers
  const heavyCard = modal.querySelector('[data-handoff-target="c_heavy"]');
  heavyCard.click();
  let heavyCmpBtn = modal.querySelector('[data-handoff-compact-chat="c_heavy"]');
  assert.strictEqual(heavyCmpBtn.getAttribute("aria-checked"), "true", ">=65% auto-enabled compact chip");
  assert(doc.getElementById("confirmHandoffNewTabBtn").textContent.includes("Сжать и запустить"), "Confirm button reflects compact+run");
  assert(doc.getElementById("handoffModelName").textContent.includes("Opus 4.6"), "Auto-picked c_heavy model");

  // Uncheck in 1 click, switch away and back -> must NOT force back on!
  heavyCmpBtn.click();
  heavyCmpBtn = modal.querySelector('[data-handoff-compact-chat="c_heavy"]');
  assert.strictEqual(heavyCmpBtn.getAttribute("aria-checked"), "false", "1-click unchecked compact chip");
  emptyCard.click();
  heavyCard.click();
  heavyCmpBtn = modal.querySelector('[data-handoff-compact-chat="c_heavy"]');
  assert.strictEqual(heavyCmpBtn.getAttribute("aria-checked"), "false", "compactTouchedByUser respected on re-select");

  // Re-enable compact chip, check OpenRouter boosters, manually pick Codex model, and launch into c_heavy!
  heavyCmpBtn.click();
  const orBox = doc.getElementById("handoffOrBoostersBox");
  assert(!orBox.classList.contains("hidden"), "OpenRouter boosters visible when OR enabled");
  const revChk = doc.getElementById("handoffOrReviewChk");
  const councilChk = doc.getElementById("handoffOrCouncilChk");
  assert(!revChk.checked && !councilChk.checked, "OR boosters start unchecked");
  revChk.checked = true;
  revChk.dispatchEvent({ type: "change" });
  councilChk.checked = true;
  councilChk.dispatchEvent({ type: "change" });

  doc.getElementById("handoffModelBtn").click();
  const codexOpt = doc.getElementById("handoffModelMenu").querySelector('[data-handoff-model-id="codex:gpt-5.4-high"]');
  assert(codexOpt, "Codex model option rendered in #handoffModelMenu");
  codexOpt.click();
  assert.strictEqual(doc.getElementById("handoffModelSourceTag").textContent, "выбрано");

  sentWsPayloads = [];
  doc.getElementById("confirmHandoffNewTabBtn").click();
  await sleep(30);
  const sendMsg = sentWsPayloads.find((p) => p.action === "send_message" && p.chat_id === "c_heavy");
  assert(sendMsg, "WS send_message sent to c_heavy");
  assert.strictEqual(sendMsg.compact_first, true, "compact_first: true sent in WS payload");
  assert.strictEqual(sendMsg.model, "codex:gpt-5.4-high");
  assert(sendMsg.prompt.includes("openrouter_chat") && sendMsg.prompt.includes("openrouter_multi_chat"), "OR MCP booster instructions appended");

  // Re-open modal -> OR checkboxes reset to false; launch into __new__ -> saves per-user localStorage model pref!
  doc.getElementById("messagesList").querySelector("[data-send-to-code]").click();
  await sleep(30);
  assert(!doc.getElementById("handoffOrReviewChk").checked, "OR review reset on reopen");
  doc.getElementById("handoffModelBtn").click();
  doc.getElementById("handoffModelMenu").querySelector('[data-handoff-model-id="claude-opus-4-6-thinking"]').click();
  doc.getElementById("confirmHandoffNewTabBtn").click();
  await sleep(50);
  assert.strictEqual(localStorage.getItem("agy_hub_handoff_model_u_dev"), "claude-opus-4-6-thinking");
  assert(lastCreatedChatBody && lastCreatedChatBody.model === "claude-opus-4-6-thinking", "New tab created with chosen model");
}
main().finally(() => {
  for (const timer of vmTimeouts) clearTimeout(timer);
  vmTimeouts.clear();
}).catch((err) => { console.error(err); process.exit(1); });
"""
        )

        env = os.environ.copy()
        env["HUB_ROOT_DIR"] = str(ROOT_DIR)
        proc = subprocess.run(
            ["node", "-e", test_script],
            cwd=str(ROOT_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node VM unified modal test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
