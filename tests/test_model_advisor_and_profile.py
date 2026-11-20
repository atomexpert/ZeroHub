"""
Isolated unit, API, WebSocket & Node VM tests for Antigravity Hub v=84:
Stage 2 — «🧭 Советник выбора нейросети и подсказки моделей» (`advisor_prefs`, `<TASK_PROFILE>`,
0ms Two-Contour Task Analyzer, Quota-Aware Ranking, `#handoffAdvisorSlot` & `#composerModelHintBar`).

Runs 100% inside `tempfile.TemporaryDirectory()` — NEVER touches live `data/hub.db` or `run_server.py`.
Zero GPU / 0 MB VRAM guaranteed (`CUDA_VISIBLE_DEVICES=""`, `ORT_DISABLE_ALL_GPU="1"`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["ORT_DISABLE_ALL_GPU"] = "1"

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


class _CaptureWS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


class TestModelAdvisorAndProfile(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database
        import app.main as main_mod

        self._live_db = (ROOT_DIR / "data" / "hub.db").resolve()

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_hub_v84_advisor_", ignore_cleanup_errors=True
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

        tmp_db = (self.tmp_root / "test_v84.db").resolve()
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

        self.assertNotEqual(database.DB_PATH.resolve(), self._live_db)
        self.assertIn(self.tmp_root, database.DB_PATH.resolve().parents)

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

    async def test_advisor_prefs_db_endpoints_and_multiuser_isolation(self) -> None:
        import app.database as database
        import app.main as main_mod

        await database.init_db()
        # Idempotent migration re-run check
        await database.init_db()

        admin = await database.get_user_by_username("admin")
        assert admin is not None
        self.assertEqual(
            admin["advisor_prefs"],
            {
                "enabled": True,
                "strategy": "balanced",
                "auto_pick_new_tab": True,
                "composer_hints": True,
            },
        )

        guest = await database.create_user(
            username="alice",
            password="alice_secret_password",
            display_name="Alice",
            role="user",
            max_access_mode="write_files",
            allowed_models=["gemini-4-carbon-medium", "claude-sonnet-4-6-medium"],
        )
        self.assertEqual(guest["advisor_prefs"]["strategy"], "balanced")

        ws_admin = _CaptureWS()
        ws_guest = _CaptureWS()
        await main_mod.ws_manager.connect(ws_admin, admin)  # type: ignore[arg-type]
        await main_mod.ws_manager.connect(ws_guest, guest)  # type: ignore[arg-type]
        try:
            # Update Alice's prefs via endpoint handler
            req = main_mod.UpdateAdvisorPrefsRequest(
                enabled=True,
                strategy="max_quality",
                auto_pick_new_tab=False,
                composer_hints=False,
            )
            resp = await main_mod.api_update_advisor_prefs(req, current_user=guest)
            self.assertTrue(resp["ok"])
            self.assertEqual(
                resp["advisor_prefs"],
                {
                    "enabled": True,
                    "strategy": "max_quality",
                    "auto_pick_new_tab": False,
                    "composer_hints": False,
                },
            )

            # WS event broadcast ONLY to Alice, never leaked to Admin
            self.assertEqual(len(ws_guest.sent), 1)
            self.assertEqual(ws_guest.sent[0]["type"], "advisor_prefs_updated")
            self.assertEqual(ws_guest.sent[0]["advisor_prefs"]["strategy"], "max_quality")
            self.assertEqual(len(ws_admin.sent), 0)

            # Invalid strategy falls back safely to "balanced"
            bad_req = main_mod.UpdateAdvisorPrefsRequest(strategy="ultra_expensive")
            resp2 = await main_mod.api_update_advisor_prefs(bad_req, current_user=admin)
            self.assertEqual(resp2["advisor_prefs"]["strategy"], "balanced")

            # Reload from SQLite and verify strict isolation + /api/auth/me + /api/account/advisor-prefs
            fresh_guest = await database.get_user_by_id(guest["id"])
            fresh_admin = await database.get_user_by_id(admin["id"])
            assert fresh_guest is not None and fresh_admin is not None
            self.assertEqual(fresh_guest["advisor_prefs"]["strategy"], "max_quality")
            self.assertFalse(fresh_guest["advisor_prefs"]["auto_pick_new_tab"])
            self.assertEqual(fresh_admin["advisor_prefs"]["strategy"], "balanced")
            self.assertTrue(fresh_admin["advisor_prefs"]["auto_pick_new_tab"])

            me_guest = await main_mod.auth_me(current_user=fresh_guest)
            self.assertEqual(me_guest["user"]["advisor_prefs"]["strategy"], "max_quality")
            self.assertEqual(me_guest["advisor_prefs"]["strategy"], "max_quality")

            get_prefs = await main_mod.api_get_advisor_prefs(current_user=fresh_guest)
            self.assertEqual(get_prefs["advisor_prefs"]["strategy"], "max_quality")
        finally:
            main_mod.ws_manager.disconnect(ws_admin)  # type: ignore[arg-type]
            main_mod.ws_manager.disconnect(ws_guest)  # type: ignore[arg-type]

    async def test_task_profile_prompt_injection_strictly_scoped(self) -> None:
        import app.agy_engine as agy_engine

        # 1. Project + read_only (План) + first turn + NOT live_voice -> MUST inject <TASK_PROFILE>
        p_plan_first = agy_engine._augment_prompt_for_mode(
            kind="project",
            prompt="Составь подробное ТЗ на рефакторинг авторизации",
            attachments=[],
            is_first_turn=True,
            access_mode="read_only",
            live_voice=False,
        )
        self.assertIn("<TASK_PROFILE>", p_plan_first)
        self.assertIn("</TASK_PROFILE>", p_plan_first)

        # 2. Regular chat -> MUST NOT inject
        p_regular = agy_engine._augment_prompt_for_mode(
            kind="chat",
            prompt="Привет, как дела?",
            attachments=[],
            is_first_turn=True,
            access_mode="read_only",
            live_voice=False,
        )
        self.assertNotIn("<TASK_PROFILE>", p_regular)

        # 3. Project + write_files or full_access -> MUST NOT inject
        for exec_mode in ("write_files", "full_access"):
            p_exec = agy_engine._augment_prompt_for_mode(
                kind="project",
                prompt="Реализуй фичу",
                attachments=[],
                is_first_turn=True,
                access_mode=exec_mode,
                live_voice=False,
            )
            self.assertNotIn("<TASK_PROFILE>", p_exec)

        # 4. Project + read_only + subsequent turn (is_first_turn=False) -> MUST NOT inject
        p_second_turn = agy_engine._augment_prompt_for_mode(
            kind="project",
            prompt="Уточни пункт 3",
            attachments=[],
            is_first_turn=False,
            access_mode="read_only",
            live_voice=False,
        )
        self.assertNotIn("<TASK_PROFILE>", p_second_turn)

        # 5. Project + read_only + live_voice=True -> MUST NOT inject
        p_live_voice = agy_engine._augment_prompt_for_mode(
            kind="project",
            prompt="Обсудим голосом архитектуру",
            attachments=[],
            is_first_turn=True,
            access_mode="read_only",
            live_voice=True,
        )
        self.assertNotIn("<TASK_PROFILE>", p_live_voice)

    def test_advisor_engine_ranking_stripping_and_composer_skew_in_node(self) -> None:
        from app.config import subprocess_hidden_kwargs

        node_script = textwrap.dedent(
            r"""
            const assert = require("assert");
            const fs = require("fs");
            const path = require("path");
            const vm = require("vm");

            const rootDir = process.env.HUB_ROOT_DIR;
            const appCode = fs.readFileSync(path.join(rootDir, "app", "static", "app.js"), "utf8");

            const dummyEl = () => ({
              classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
              addEventListener() {},
              removeEventListener() {},
              querySelector: () => null,
              querySelectorAll: () => [],
              setAttribute() {},
              getAttribute: () => null,
              appendChild() {},
              style: {},
              dataset: {},
              options: [],
              value: "",
              textContent: "",
              innerHTML: "",
            });
            const storageMap = new Map();
            const storage = {
              getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
              setItem: (k, v) => storageMap.set(k, String(v)),
              removeItem: (k) => storageMap.delete(k),
              clear: () => storageMap.clear(),
            };
            const doc = {
              documentElement: { getAttribute: () => "dark", setAttribute() {}, style: { setProperty() {}, getPropertyValue: () => "" } },
              body: dummyEl(),
              getElementById: () => dummyEl(),
              querySelector: () => null,
              querySelectorAll: () => [],
              createElement: () => dummyEl(),
              addEventListener() {},
            };
            const win = {
              __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
              __AGY_SKIP_AUTO_INIT__: true,
              document: doc,
              localStorage: storage,
              sessionStorage: storage,
              location: { protocol: "http:", host: "127.0.0.1:8080", pathname: "/", search: "", hash: "" },
              navigator: { userAgent: "NodeTest" },
              innerWidth: 1280,
              innerHeight: 800,
              setTimeout,
              clearTimeout,
              setInterval: () => 1,
              clearInterval: () => {},
              requestAnimationFrame: (cb) => setTimeout(cb, 0),
              cancelAnimationFrame: clearTimeout,
              matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
              addEventListener() {},
              CSS: { supports: () => true, escape: (s) => String(s) },
              URL,
              URLSearchParams,
            };
            win.window = win;
            win.globalThis = win;

            const ctx = vm.createContext(
              Object.assign({}, win, {
                window: win,
                globalThis: win,
                document: doc,
                console,
                Math,
                Date,
                JSON,
                Promise,
                Set,
                Map,
                WeakMap,
                AbortController,
                RegExp,
                Array,
                Object,
                String,
                Number,
                Boolean,
                Error,
                TypeError,
                encodeURIComponent,
                decodeURIComponent,
                parseInt,
                parseFloat,
                isNaN,
                isFinite,
                Intl,
              })
            );
            vm.runInContext(appCode, ctx, { filename: "app.js" });
            const api = win.__AGY_ADVISOR_API__ || ctx.__AGY_ADVISOR_API__;
            assert(api, "Advisor test API must be exported");

            // 1. Complete & streaming <TASK_PROFILE> stripping
            const specWithTag = "## План доработки UI\n1. Поправить кнопку в `app/static/styles.css`.\n\n<TASK_PROFILE>{\"complexity\":\"light\",\"type\":\"ui\",\"files\":1,\"risk\":\"low\",\"needs_terminal\":false}</TASK_PROFILE>";
            assert.strictEqual(
              api.stripTaskProfileFromText(specWithTag),
              "## План доработки UI\n1. Поправить кнопку в `app/static/styles.css`."
            );
            assert.strictEqual(
              api.stripTaskProfileFromText("Готовый план.\n<TASK_PROFILE>{\"complexity\":\"he"),
              "Готовый план."
            );
            assert.strictEqual(
              api.stripTaskProfileFromText("Готовый план.\n<TASK_PRO"),
              "Готовый план."
            );
            assert.strictEqual(
              api.stripTaskProfileFromText("Текст с `<div>` и обычным тегом."),
              "Текст с `<div>` и обычным тегом."
            );

            // 2. Contour A (tag) & Contour B (0ms heuristic fallback)
            const extracted = api.extractTaskProfileFromText(specWithTag);
            assert.strictEqual(extracted.complexity, "light");
            assert.strictEqual(extracted.type, "ui");
            assert.strictEqual(extracted.files, 1);
            assert.strictEqual(extracted.needsTerminal, false);

            const t0 = Date.now();
            const heurHeavy = api.analyzeTaskForAdvisor(
              "## ТЗ: Миграция SQLite и WebSocket гонки\n" +
              "Файлы: `app/database.py`, `app/main.py`, `app/agy_engine.py`, `app/codex_engine.py`, `tests/test_ws.py`.\n" +
              "Проблема: deadlock транзакции и гонка потоков asyncio. После правок запустить `pytest tests/test_ws.py`.\n" +
              "```python\nasync def migrate():\n    pass\n```\n"
            );
            const elapsedMs = Date.now() - t0;
            assert(elapsedMs < 25, `Heuristic analyzer must be instant, took ${elapsedMs}ms`);
            assert.strictEqual(heurHeavy.source, "heuristic");
            assert(heurHeavy.files >= 4, `Expected >=4 files, got ${heurHeavy.files}`);
            assert.strictEqual(heurHeavy.needsTerminal, true);
            assert(["medium", "heavy", "critical"].includes(heurHeavy.complexity));

            // 3. Model Ranking: Balanced vs Max Quality, Quota protection & RBAC
            const fullModels = [
              { id: "gemini-3-flash-low", label: "Gemini 3 Flash (Low)" },
              { id: "gemini-3-flash-high", label: "Gemini 3 Flash (High)" },
              { id: "gemini-4-carbon-medium", label: "Gemini 4 Carbon (Medium)" },
              { id: "gemini-4-carbon-high", label: "Gemini 4 Carbon (High)" },
              { id: "claude-sonnet-4-6-medium", label: "Claude Sonnet 4.6 (Medium)" },
              { id: "claude-sonnet-4-6-thinking", label: "Claude Sonnet 4.6 (Thinking)" },
              { id: "claude-opus-4-6-thinking", label: "Claude Opus 4.6 (Thinking)" },
              { id: "codex:gpt-5.4-mini@low", label: "GPT-5.4 Mini (Low)" },
              { id: "codex:gpt-5.4@medium", label: "GPT-5.4 (Medium)" },
              { id: "codex:gpt-5.4@high", label: "GPT-5.4 (High)" },
              { id: "codex:gpt-5.4@xhigh", label: "GPT-5.4 (XHigh)" },
            ];
            const healthyQuotas = { gemini_5h_pct: 90, claude_5h_pct: 88 };

            const recLightBal = api.recommendModelsForTask(specWithTag, {
              models: fullModels,
              strategy: "balanced",
              quotas: healthyQuotas,
              chatgpt_5h_pct: 85,
            });
            assert.strictEqual(recLightBal.top3.length, 3);
            assert(
              !recLightBal.top3[0].isOpus && !recLightBal.top3[0].isXHigh,
              `Light UI in balanced should pick medium/fast model, got ${recLightBal.top3[0].id}`
            );

            const heavySpec = "План.\n<TASK_PROFILE>{\"complexity\":\"critical\",\"type\":\"backend\",\"files\":6,\"risk\":\"high\",\"needs_terminal\":true}</TASK_PROFILE>";
            const recHeavy = api.recommendModelsForTask(heavySpec, {
              models: fullModels,
              strategy: "balanced",
              quotas: healthyQuotas,
              chatgpt_5h_pct: 85,
            });
            assert(
              recHeavy.top3[0].power >= 87,
              `Critical backend task must pick strong senior/flagship model, got ${recHeavy.top3[0].id} (power=${recHeavy.top3[0].power})`
            );

            const recLowClaude = api.recommendModelsForTask(heavySpec, {
              models: fullModels,
              strategy: "max_quality",
              quotas: { gemini_5h_pct: 92, claude_5h_pct: 3 },
              chatgpt_5h_pct: 88,
            });
            assert.notStrictEqual(
              recLowClaude.top3[0].vendor,
              "claude",
              `3% Claude quota must not be Top-1 when ChatGPT/Gemini are ~90%, got ${recLowClaude.top3[0].id}`
            );

            const guestModels = [
              { id: "gemini-3-flash-high", label: "Gemini 3 Flash (High)" },
              { id: "gemini-4-carbon-medium", label: "Gemini 4 Carbon (Medium)" },
            ];
            const recGuest = api.recommendModelsForTask(heavySpec, {
              models: guestModels,
              strategy: "max_quality",
              quotas: healthyQuotas,
            });
            assert(recGuest.top3.every((m) => guestModels.some((gm) => gm.id === m.id)));

            // 4. Quiet Composer Skew Indicator (strictly 3 extreme cases)
            const skewQuota = api.evaluateComposerModelSkew({
              models: fullModels,
              currentModelId: "claude-opus-4-6-thinking",
              text: "Помоги разобрать архитектуру модуля кэширования",
              quotas: { gemini_5h_pct: 85, claude_5h_pct: 5 },
              chatgpt_5h_pct: 80,
            });
            assert(skewQuota && skewQuota.kind === "quota_low", "Expected quota_low skew");

            const skewEco = api.evaluateComposerModelSkew({
              models: fullModels,
              currentModelId: "codex:gpt-5.4@xhigh",
              text: "Как в Python быстро развернуть список в обратном порядке?",
              quotas: healthyQuotas,
              chatgpt_5h_pct: 90,
            });
            assert(skewEco && skewEco.kind === "eco_overkill", "Expected eco_overkill skew");

            const noSkewTech = api.evaluateComposerModelSkew({
              models: fullModels,
              currentModelId: "claude-opus-4-6-thinking",
              text: "Исправь баг авторизации в app/main.py",
              quotas: healthyQuotas,
              chatgpt_5h_pct: 90,
            });
            assert.strictEqual(noSkewTech, null, "Must stay silent when file path / bug keyword present");

            const bigTrace =
              "Разбери падение воркера в продакшене:\n```python\nTraceback (most recent call last):\n  File \"app/main.py\", line 142, in run\nValueError: broken pipe\n```\n" +
              "Подробное описание шагов воспроизведения и состояния очереди сообщений. ".repeat(16);
            assert(bigTrace.length >= 900);
            const skewUnder = api.evaluateComposerModelSkew({
              models: fullModels,
              currentModelId: "gemini-3-flash-low",
              text: bigTrace,
              quotas: healthyQuotas,
              chatgpt_5h_pct: 90,
            });
            assert(skewUnder && skewUnder.kind === "underpowered", "Expected underpowered skew");

            const skewNormal = api.evaluateComposerModelSkew({
              models: fullModels,
              currentModelId: "gemini-4-carbon-high",
              text: "Напиши функцию парсинга даты с обработкой пустых строк и юнит-тестами.",
              quotas: healthyQuotas,
              chatgpt_5h_pct: 90,
            });
            assert.strictEqual(skewNormal, null);
            """
        )

        env = os.environ.copy()
        env["HUB_ROOT_DIR"] = str(ROOT_DIR)
        proc = subprocess.run(
            ["node", "-e", node_script],
            cwd=str(ROOT_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            **subprocess_hidden_kwargs(),
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node Advisor unit test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )

    def test_dom_handoff_advisor_slot_invisibility_and_composer_bar_in_node(self) -> None:
        from app.config import subprocess_hidden_kwargs
        from tests.test_task_counters_and_queues import NODE_VM_HARNESS_JS

        dom_prefix = NODE_VM_HARNESS_JS.split("async function main() {")[0]
        dom_script = (
            dom_prefix
            + r"""
async function main() {
  Object.defineProperty(MiniElement.prototype, "options", {
    get() { return this.querySelectorAll("option"); },
    configurable: true,
  });
  const rootDir = process.env.HUB_ROOT_DIR;
  const htmlText = fs.readFileSync(path.join(rootDir, "app/static/index.html"), "utf8");
  const appCode = fs.readFileSync(path.join(rootDir, "app/static/app.js"), "utf8");

  const doc = new MiniDocument();
  const bodyMatch = htmlText.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  assert(bodyMatch, "body found in index.html");
  doc.body.innerHTML = bodyMatch[1].replace(/<script[\s\S]*?<\/script>/gi, "");

  const localStorage = new MemoryStorage();
  const sessionStorage = new MemoryStorage();
  localStorage.setItem("agy_hub_token", "tok_v84");

  const nowIso = "2026-11-13T10:00:00Z";
  const models = [
    { id: "gemini-3-flash-low", label: "Gemini 3 Flash (Low)" },
    { id: "gemini-4-carbon-medium", label: "Gemini 4 Carbon (Medium)" },
    { id: "gemini-4-carbon-high", label: "Gemini 4 Carbon (High)" },
    { id: "claude-sonnet-4-6-medium", label: "Claude Sonnet 4.6 (Medium)" },
    { id: "claude-opus-4-6-thinking", label: "Claude Opus 4.6 (Thinking)" },
    { id: "codex:gpt-5.4@high", label: "GPT-5.4 (High)" },
  ];
  const planMsgText =
    "## ТЗ: Добавление эндпоинта advisor-prefs и pytest\n\n" +
    "1. Обновить `app/database.py` и `app/main.py`.\n" +
    "2. Запустить `pytest tests/test_model_advisor_and_profile.py`.\n\n" +
    "<TASK_PROFILE>{\"complexity\":\"medium\",\"type\":\"backend\",\"files\":2,\"risk\":\"medium\",\"needs_terminal\":true}</TASK_PROFILE>";

  const chatsDb = new Map([
    ["c_plan", {
      id: "c_plan", title: "План советника", kind: "project", project_name: "AlphaProj",
      access_mode: "read_only", model: "claude-opus-4-6-thinking", is_generating: false, updated_at: nowIso,
    }],
  ]);

  let sentWsPayloads = [];
  let savedAdvisorPrefsPuts = [];
  let lastCreatedChat = null;

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
      ok: true, status: 200, headers: { get: () => "application/json" },
      json: async () => obj, text: async () => JSON.stringify(obj),
    });
    const curUser = {
      id: "u_admin", username: "admin", display_name: "Admin", role: "admin",
      is_admin: true, max_access_mode: "host_full",
      advisor_prefs: { enabled: true, strategy: "balanced", auto_pick_new_tab: true, composer_hints: true },
    };
    if (p === "/api/auth/check" || p === "/api/auth/status") {
      return jsonResp({ authenticated: true, auth_enabled: true, user: curUser });
    }
    if (p === "/api/system/info") {
      return jsonResp({
        user: curUser, models,
        docker: { installed: true, sandbox_mcp_ready: false },
        cli_auth: { ok: true, models_count: models.length },
        quotas: { gemini_5h_pct: 92, claude_5h_pct: 80 },
        chatgpt_summary: { has_account: true, is_enabled: true, quotas: { primary_5h: { remaining_pct: 88 } } },
        account_profiles: [],
      });
    }
    if (p === "/api/account/advisor-prefs" && method === "PUT") {
      const body = JSON.parse(opts.body || "{}");
      savedAdvisorPrefsPuts.push(body);
      return jsonResp({ ok: true, advisor_prefs: body });
    }
    if (p === "/api/projects") {
      return jsonResp({ projects: [{ name: "AlphaProj", path: "/ws/AlphaProj", is_shared: false }] });
    }
    if (p === "/api/chats" && method === "GET") {
      return jsonResp({ chats: Array.from(chatsDb.values()) });
    }
    if (p === "/api/chats" && method === "POST") {
      lastCreatedChat = Object.assign({ id: "c_exec_1", is_generating: false, updated_at: nowIso }, JSON.parse(opts.body || "{}"));
      chatsDb.set(lastCreatedChat.id, lastCreatedChat);
      return jsonResp({ chat: lastCreatedChat });
    }
    if (p.startsWith("/api/chats/") && method === "GET") {
      const cid = decodeURIComponent(p.split("/")[3]);
      return jsonResp({
        chat: chatsDb.get(cid),
        messages: cid === "c_plan" ? [{ id: 701, role: "assistant", status: "done", content: planMsgText, created_at: nowIso }] : [],
        context_stats: { fill_pct: 12, used_tokens: 4200 },
        queued_messages: [],
        queue_paused: false,
      });
    }
    return jsonResp({ ok: true, conversations: [], items: [], rules: "" });
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const win = {
    document: doc, localStorage, sessionStorage,
    location: { protocol: "http:", host: "127.0.0.1:8080", pathname: "/", search: "", hash: "" },
    navigator: { clipboard: { writeText: async () => {} }, userAgent: "Mozilla/5.0" },
    innerWidth: 1280, innerHeight: 860, fetch: mockFetch, WebSocket: MockWebSocket,
    requestAnimationFrame: (cb) => setTimeout(cb, 0), cancelAnimationFrame: clearTimeout,
    setTimeout, clearTimeout, setInterval: () => 1, clearInterval: () => {},
    matchMedia: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => true, scrollTo: () => {},
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
    IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
    MutationObserver: class { observe() {} disconnect() {} },
    Notification: { permission: "default", requestPermission: async () => "default" },
    history: { replaceState: () => {}, pushState: () => {} },
    confirm: () => true, alert: () => {},
    performance: { now: () => Date.now() },
    Event: class MiniEvent { constructor(type, opts = {}) { this.type = type; this.bubbles = Boolean(opts && opts.bubbles); } },
    CustomEvent: class MiniCustomEvent { constructor(type, opts = {}) { this.type = type; this.bubbles = Boolean(opts && opts.bubbles); this.detail = opts && opts.detail; } },
    Blob: class {}, FormData: class {}, FileReader: class {},
    AbortController, WeakMap,
    CSS: { supports: () => true, escape: (s) => String(s) }, URL, URLSearchParams,
  };
  win.window = win;
  win.globalThis = win;
  doc.defaultView = win;

  const ctx = vm.createContext(Object.assign({}, win, {
    window: win, globalThis: win, document: doc, WebSocket: MockWebSocket,
    Event: win.Event, CustomEvent: win.CustomEvent,
    console, Math, Date, JSON, Promise, Set, Map, WeakMap, AbortController,
    RegExp, Array, Object, String, Number, Boolean, Error, TypeError,
    encodeURIComponent, decodeURIComponent, parseInt, parseFloat, isNaN, isFinite, Intl,
  }));
  vm.runInContext(appCode, ctx, { filename: "app.js" });
  await sleep(90);

  // Expand AlphaProj and open c_plan
  const projGroup = doc.getElementById("projectsTreeList").querySelector(".proj-tree-group");
  assert(projGroup, "AlphaProj group rendered");
  if (!projGroup.classList.contains("expanded")) {
    projGroup.querySelector(".proj-tree-header").click();
    await sleep(30);
  }
  doc.querySelector('.nav-item[data-chat-id="c_plan"]').click();
  await sleep(60);

  // 1. <TASK_PROFILE> is 100% invisible in rendered chat DOM and in Export PLAN.md modal!
  const renderedHtml = doc.getElementById("messagesList").innerHTML;
  assert(!renderedHtml.includes("TASK_PROFILE"), "<TASK_PROFILE> must NEVER appear in rendered chat DOM");

  const expBtn = doc.getElementById("messagesList").querySelector("[data-export-msg]");
  if (expBtn) {
    expBtn.click();
    await sleep(30);
    assert(!doc.getElementById("exportContentArea").value.includes("TASK_PROFILE"), "Export modal must strip <TASK_PROFILE>");
    doc.getElementById("exportModal").classList.add("hidden");
  }

  // 2. Open «🚀 В Код» modal -> #handoffAdvisorSlot renders Top-3 & auto-picks Top-1 with 🧭 Авто!
  doc.getElementById("messagesList").querySelector("[data-send-to-code]").click();
  await sleep(40);
  const advSlot = doc.getElementById("handoffAdvisorSlot");
  assert(!advSlot.classList.contains("hidden"), "#handoffAdvisorSlot must be visible");
  const advCards = advSlot.querySelectorAll("[data-advisor-pick-model]");
  assert.strictEqual(advCards.length, 3, "Must render Top-3 recommended model cards");
  assert.strictEqual(doc.getElementById("handoffModelSourceTag").textContent, "🧭 Авто");
  assert(!doc.getElementById("handoffTitleInput").value.includes("TASK_PROFILE"), "Title must strip <TASK_PROFILE>");

  // 3. Switch mode to write_files -> needs_terminal tip appears & clicking it switches back to full_access!
  doc.getElementById("handoffModeWriteBtn").click();
  const termTip = advSlot.querySelector("[data-advisor-switch-terminal]");
  assert(termTip, "Terminal tip must appear when needs_terminal=true and mode=write_files");
  termTip.click();
  assert(doc.getElementById("handoffModeFullBtn").classList.contains("active"), "Tip click must switch mode to full_access");
  assert(!advSlot.querySelector("[data-advisor-switch-terminal]"), "Terminal tip hides once full_access is active");

  // 4. Click strategy «Макс. качество» -> persists via PUT /api/account/advisor-prefs!
  const maxQualBtn = advSlot.querySelector('[data-advisor-strategy="max_quality"]');
  maxQualBtn.click();
  await sleep(25);
  assert(savedAdvisorPrefsPuts.some((p) => p.strategy === "max_quality"), "Strategy switch must persist via API");

  // 5. Click #2 card -> sets userPickedModel ("выбрано") & launching strips <TASK_PROFILE> from WS prompt!
  const secondCard = advSlot.querySelectorAll("[data-advisor-pick-model]")[1];
  const pickedId = secondCard.getAttribute("data-advisor-pick-model");
  secondCard.click();
  assert.strictEqual(doc.getElementById("handoffModelSourceTag").textContent, "выбрано");

  sentWsPayloads = [];
  doc.getElementById("confirmHandoffNewTabBtn").click();
  await sleep(80);
  assert(lastCreatedChat && lastCreatedChat.model === pickedId, "Created tab must use clicked advisor card model");
  const wsSend = sentWsPayloads.find((p) => p.action === "send_message");
  assert(wsSend, "WS send_message must be dispatched after handoff");
  assert(!wsSend.prompt.includes("TASK_PROFILE"), "Handoff prompt must strip <TASK_PROFILE>");

  // 6. Quiet Composer Skew Bar: Opus + short casual question -> 1-click switch preserves prompt text!
  const modelSel = doc.getElementById("modelSelect");
  modelSel.value = "claude-opus-4-6-thinking";
  modelSel.dispatchEvent({ type: "change", bubbles: true });
  const promptIn = doc.getElementById("promptInput");
  const casualQuestion = "Подскажи коротко разницу между let и const в JavaScript?";
  promptIn.value = casualQuestion;
  promptIn.dispatchEvent({ type: "input", bubbles: true });
  await sleep(360);

  const hintBar = doc.getElementById("composerModelHintBar");
  assert(!hintBar.classList.contains("hidden"), "Composer skew hint bar should appear on Opus + short casual question");
  const switchBtn = hintBar.querySelector("[data-composer-hint-pick]");
  const suggestedModel = switchBtn.getAttribute("data-composer-hint-pick");
  switchBtn.click();
  assert.strictEqual(modelSel.value, suggestedModel, "1-click composer hint must switch active model");
  assert.strictEqual(promptIn.value, casualQuestion, "Prompt text must remain 100% intact after switching model");
  assert(hintBar.classList.contains("hidden"), "Composer hint bar hides after model switch");

  // 7. Session dismiss (✕) suppresses repeat eco_overkill pill for the rest of session!
  modelSel.value = "claude-opus-4-6-thinking";
  modelSel.dispatchEvent({ type: "change", bubbles: true });
  await sleep(360);
  assert(!hintBar.classList.contains("hidden"), "Composer hint bar re-appears when switching back to Opus");
  const dismissBtn = hintBar.querySelector("[data-composer-hint-dismiss]");
  assert(dismissBtn, "Dismiss button (✕) must be rendered");
  dismissBtn.click();
  assert(hintBar.classList.contains("hidden"), "Dismiss click hides composer hint bar immediately");
  promptIn.value = casualQuestion + " Ещё пара слов?";
  promptIn.dispatchEvent({ type: "input", bubbles: true });
  await sleep(360);
  assert(hintBar.classList.contains("hidden"), "Dismissed skew reason stays suppressed in session");
}
main().catch((err) => { console.error(err); process.exit(1); });
"""
        )

        env = os.environ.copy()
        env["HUB_ROOT_DIR"] = str(ROOT_DIR)
        proc = subprocess.run(
            ["node", "-e", dom_script],
            cwd=str(ROOT_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            **subprocess_hidden_kwargs(),
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node DOM Advisor integration test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
