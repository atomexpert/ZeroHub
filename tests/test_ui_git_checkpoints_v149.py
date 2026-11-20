"""Regression suite v149 (TZ2): Project Git checkpoints UI, diff viewer, shield controls, guardrails toggle & responsive geometry."""
from __future__ import annotations

import os
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

from test_frontend_contracts import STATIC, ShellParser, run_node
from test_project_git_checkpoints_and_guardrails_v148 import _asgi_request
from test_ui_workspace_minimal_v142 import run_workspace_chromium


class ProjectMemoryGuardrailsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app import auth, brain_reader, config, database
        import app.main as main_mod

        self.auth = auth
        self.brain_reader = brain_reader
        self.config = config
        self.database = database
        self.main_mod = main_mod

        self._tmp = tempfile.TemporaryDirectory(prefix="hub_v149_mem_", ignore_cleanup_errors=True)
        iso_root = Path(self._tmp.name).resolve() / "iso_hub"
        data_dir = iso_root / "data"
        users_dir = iso_root / "users"
        projects_root = users_dir / "admin" / "projects"
        shared_dir = iso_root / "shared_projects"
        shared_uploads = shared_dir / "_uploads"
        shared_ctx = shared_dir / "_context"
        cli_homes = data_dir / "cli_homes"
        uploads_dir = data_dir / "uploads"
        db_path = data_dir / "test_v149.db"
        for d in (
            iso_root,
            iso_root / "app",
            iso_root / ".venv",
            iso_root / ".git",
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

        await database.close_db_pool()
        patch_map = {
            "BASE_DIR": iso_root,
            "DATA_DIR": data_dir,
            "DB_PATH": db_path,
            "USERS_DIR": users_dir,
            "USER_WORKSPACES_DIR": users_dir,
            "PROJECTS_ROOT": projects_root,
            "SHARED_PROJECTS_DIR": shared_dir,
            "SHARED_UPLOADS_DIR": shared_uploads,
            "SHARED_CONTEXT_DIR": shared_ctx,
            "CLI_HOMES_DIR": cli_homes,
            "UPLOADS_DIR": uploads_dir,
        }
        self._saved_attrs: list[tuple[Any, str, Any]] = []
        for mod in (config, database, brain_reader, main_mod):
            for attr, val in patch_map.items():
                if hasattr(mod, attr):
                    self._saved_attrs.append((mod, attr, getattr(mod, attr)))
                    setattr(mod, attr, val)

        self.proj_dir = projects_root / "AlphaBot"
        self.proj_dir.mkdir(parents=True, exist_ok=True)
        (self.proj_dir / "bot.py").write_text("READY = True\n", encoding="utf-8")

        brain_reader.invalidate_projects_list_cache()
        await database.init_db()

    async def asyncTearDown(self) -> None:
        await self.database.close_db_pool()
        for mod, attr, orig in reversed(self._saved_attrs):
            setattr(mod, attr, orig)
        self.brain_reader.invalidate_projects_list_cache()
        self._tmp.cleanup()

    async def test_project_memory_guardrails_flag_persistence_via_asgi(self) -> None:
        cname = self.auth.COOKIE_NAME
        admin_cookie = f"{cname}={self.auth.create_user_session_token('admin', 1)}"
        app = self.main_mod.app

        code, d_get0 = await _asgi_request(app, "GET", "/api/projects/AlphaBot/memory", cookie_header=admin_cookie)
        self.assertEqual(code, 200, d_get0)
        self.assertIs(d_get0["memory"]["disable_default_guardrails"], False)

        code, d_put1 = await _asgi_request(
            app,
            "PUT",
            "/api/projects/AlphaBot/memory",
            cookie_header=admin_cookie,
            json_body={"disable_default_guardrails": True, "passport_overview": "Telegram bot"},
        )
        self.assertEqual(code, 200, d_put1)
        self.assertIs(d_put1["memory"]["disable_default_guardrails"], True)

        rules_md = self.proj_dir / ".agents" / "rules" / "hub_project.md"
        self.assertTrue(rules_md.is_file())
        self.assertIn("hub:disable-default-guardrails", rules_md.read_text(encoding="utf-8"))

        # Saving passport text without passing disable_default_guardrails preserves existing flag
        code, d_put_keep = await _asgi_request(
            app,
            "PUT",
            "/api/projects/AlphaBot/memory",
            cookie_header=admin_cookie,
            json_body={"passport_rules": "Zero console windows"},
        )
        self.assertEqual(code, 200, d_put_keep)
        self.assertIs(d_put_keep["memory"]["disable_default_guardrails"], True)

        code, d_get1 = await _asgi_request(app, "GET", "/api/projects/AlphaBot/memory", cookie_header=admin_cookie)
        self.assertEqual(code, 200)
        self.assertIs(d_get1["memory"]["disable_default_guardrails"], True)

        # Re-enabling default guardrails removes opt-out marker
        code, d_put2 = await _asgi_request(
            app,
            "PUT",
            "/api/projects/AlphaBot/memory",
            cookie_header=admin_cookie,
            json_body={"disable_default_guardrails": False},
        )
        self.assertEqual(code, 200, d_put2)
        self.assertIs(d_put2["memory"]["disable_default_guardrails"], False)
        self.assertNotIn("hub:disable-default-guardrails", rules_md.read_text(encoding="utf-8"))


NODE_GIT_UI_CHECKS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const noop = () => {};
const ids = JSON.parse(fs.readFileSync(0, "utf8"));

function makeElement(id = "") {
  const classes = new Set(
    [
      "openProjectGitBtn", "projectGitBadge", "mobileProjectGitBtn", "mobileProjectGitBadge",
      "projectGitModal", "projectGitEmptyInitBox", "installProjectShieldBtn",
      "projectGitDirtyBox", "projectGitWorkingDiffWrap", "projectMemoryModal",
    ].includes(id) ? ["hidden"] : []
  );
  const attrs = new Map();
  const listeners = new Map();
  return {
    id,
    value: "",
    checked: false,
    disabled: false,
    textContent: "",
    innerHTML: "",
    title: "",
    options: [],
    dataset: {},
    children: [],
    style: { setProperty: noop, removeProperty: noop, getPropertyValue: () => "" },
    classList: {
      add: (...names) => names.forEach(n => classes.add(n)),
      remove: (...names) => names.forEach(n => classes.delete(n)),
      toggle: (name, force) => {
        const next = force === undefined ? !classes.has(name) : Boolean(force);
        if (next) classes.add(name); else classes.delete(name);
        return next;
      },
      contains: name => classes.has(name),
    },
    setAttribute: (k, v) => attrs.set(k, String(v)),
    getAttribute: k => attrs.get(k) ?? null,
    removeAttribute: k => attrs.delete(k),
    addEventListener: (t, fn) => listeners.set(t, fn),
    removeEventListener: t => listeners.delete(t),
    dispatchEvent: evt => listeners.get(evt.type)?.(evt),
    appendChild: noop,
    replaceChildren: noop,
    focus: noop,
    querySelector: () => null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 20, top: 20, right: 120, bottom: 50, width: 100, height: 30 }),
  };
}

const nodes = new Map(ids.map(id => [id, makeElement(id)]));
const document = {
  documentElement: makeElement("html"),
  body: makeElement("body"),
  getElementById: id => nodes.get(id) ?? null,
  querySelector: sel => sel.startsWith("#") ? (nodes.get(sel.slice(1)) ?? null) : null,
  querySelectorAll: () => [],
  createElement: tag => makeElement(tag),
  addEventListener: noop,
};
const storage = () => {
  const map = new Map();
  return {
    getItem: k => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: k => map.delete(k),
  };
};

const calls = [];
let mockState = {
  initialized: false,
  shield_installed: false,
  shield_kind: "project",
  branch: "main",
  head_sha: "",
  head_short: "",
  changed_files: [],
  checkpoints: [],
  stashes_count: 0,
  is_self_hub: false,
  disable_default_guardrails: false,
};

async function mockFetch(url, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({ method, url: String(url), body });

  const reply = payload => ({
    ok: true,
    status: 200,
    headers: { get: () => "application/json" },
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  });

  if (url.includes("/memory") && method === "PUT") {
    if (typeof body?.disable_default_guardrails === "boolean") {
      mockState.disable_default_guardrails = body.disable_default_guardrails;
    }
    return reply({
      ok: true,
      memory: {
        project_name: "DemoProj",
        has_passport: true,
        changelog_count: 1,
        disable_default_guardrails: mockState.disable_default_guardrails,
      },
    });
  }
  if (url.includes("/git/init") && method === "POST") {
    mockState.initialized = true;
    mockState.changed_files = [
      { path: ".gitignore", status: "??", staged: false },
      { path: "app.py", status: "M", staged: false },
      { path: "old.py", status: "D", staged: false },
    ];
    return reply({
      ok: true,
      initialized: true,
      status: {
        initialized: true,
        branch: "main",
        head_sha: "",
        head_short: "",
        is_clean: false,
        changed_count: 3,
        changed_files: mockState.changed_files,
        checkpoints: [],
        stashes_count: 0,
        shield_installed: false,
        shield_kind: "project",
      },
    });
  }
  if (url.includes("/git/install-shield") && method === "POST") {
    mockState.shield_installed = true;
    return reply({ ok: true, shield_installed: true, shield_kind: "project" });
  }
  if (url.includes("/git/checkpoint") && method === "POST") {
    const newSha = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2";
    const subj = (body && body.message) || "Автоматическая точка";
    mockState.head_sha = newSha;
    mockState.head_short = newSha.slice(0, 7);
    mockState.checkpoints.unshift({
      sha: newSha,
      short_sha: newSha.slice(0, 7),
      subject: subj,
      author: "Antigravity Hub",
      timestamp: 1795083200,
      iso_date: "2026-11-19T20:00:00+08:00",
      files_changed: mockState.changed_files.length,
    });
    mockState.changed_files = [];
    return reply({ ok: true, created: true, sha: newSha, short_sha: newSha.slice(0, 7), message: subj });
  }
  if (url.includes("/git/diff")) {
    return reply({
      ok: true,
      diff: "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n-OLD = '<script>'\n+NEW = 42\n",
      truncated: false,
    });
  }
  if (url.includes("/git/rollback") && method === "POST") {
    mockState.stashes_count += 1;
    mockState.changed_files = [];
    return reply({
      ok: true,
      mode: body.mode,
      target_short: (body.target_sha || "0000000").slice(0, 7),
      backup_stash_created: true,
    });
  }
  if (url.includes("/git/status")) {
    const isHub = url.toLowerCase().includes("antigravity_hub");
    return reply({
      ok: true,
      project_name: isHub ? "Antigravity_Hub" : "DemoProj",
      is_self_hub: isHub,
      initialized: isHub ? true : mockState.initialized,
      branch: mockState.branch,
      head_sha: isHub ? "6fc10db000000000000000000000000000000000" : mockState.head_sha,
      head_short: isHub ? "6fc10db" : mockState.head_short,
      is_clean: isHub ? false : mockState.changed_files.length === 0,
      changed_count: isHub ? 2 : mockState.changed_files.length,
      changed_files: isHub ? [{ path: "app/main.py", status: "M", staged: false }] : mockState.changed_files,
      checkpoints: isHub
        ? [
            { sha: "6fc10db000000000000000000000000000000000", short_sha: "6fc10db", subject: "HEAD commit", files_changed: 3, timestamp: 1795083200 },
            { sha: "2782a76000000000000000000000000000000000", short_sha: "2782a76", subject: "Previous commit", files_changed: 2, timestamp: 1795080000 },
          ]
        : mockState.checkpoints,
      stashes_count: mockState.stashes_count,
      shield_installed: isHub ? true : mockState.shield_installed,
      shield_kind: isHub ? "hub" : mockState.shield_kind,
    });
  }
  return reply({ ok: true });
}

const context = {
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
  document,
  fetch: mockFetch,
  localStorage: storage(),
  sessionStorage: storage(),
  location: new URL("https://hub.invalid/"),
  URL,
  URLSearchParams,
  CSS: { supports: () => true, escape: String },
  innerWidth: 1440,
  innerHeight: 900,
  navigator: { userAgent: "Desktop" },
  matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop }),
  requestAnimationFrame: noop,
  cancelAnimationFrame: noop,
  setTimeout: noop,
  clearTimeout: noop,
  setInterval: noop,
  clearInterval: noop,
  addEventListener: noop,
  removeEventListener: noop,
};
context.window = context;
const sandbox = vm.createContext(context);
for (const name of ["ui-shell.js", "app.js"]) {
  vm.runInContext(fs.readFileSync("app/static/" + name, "utf8"), sandbox, { filename: name, timeout: 6000 });
}

(async () => {
  const api = context.__AGY_ADVISOR_API__;
  assert.equal(typeof context.AgyUI.ICO.gitBranch, "string");
  assert.equal(typeof context.AgyUI.ICO.history, "string");
  assert.equal(typeof context.AgyUI.ICO.shieldCheck, "string");

  // 1. Regular chat hides both desktop and mobile Git buttons
  api.state.projects = [
    { name: "DemoProj", is_shared: false, is_self_hub: false },
    { name: "Antigravity_Hub", is_shared: false, is_self_hub: true },
  ];
  api.state.chats = [
    { id: "c_reg", kind: "chat", title: "Regular" },
    { id: "c_proj", kind: "project", project_name: "DemoProj", title: "Dev branch" },
  ];
  api.state.activeChat = api.state.chats[0];
  api.syncProjectGitHeaderIndicator();
  assert.ok(nodes.get("openProjectGitBtn").classList.contains("hidden"));
  assert.ok(nodes.get("mobileProjectGitBtn").classList.contains("hidden"));

  // 2. Project chat shows uninitialized modal -> init -> 3 changed files -> badges visible
  api.state.activeChat = api.state.chats[1];
  await api.openProjectGitModal("DemoProj");
  assert.ok(!nodes.get("projectGitModal").classList.contains("hidden"));
  assert.ok(!nodes.get("projectGitEmptyInitBox").classList.contains("hidden"));
  assert.ok(nodes.get("projectGitMainContent").classList.contains("hidden"));

  await api.initProjectGitFromModal();
  assert.ok(nodes.get("projectGitEmptyInitBox").classList.contains("hidden"));
  assert.ok(!nodes.get("projectGitMainContent").classList.contains("hidden"));
  assert.ok(!nodes.get("openProjectGitBtn").classList.contains("hidden"));
  assert.ok(!nodes.get("mobileProjectGitBtn").classList.contains("hidden"));
  assert.ok(!nodes.get("projectGitBadge").classList.contains("hidden"));
  assert.equal(nodes.get("projectGitBadge").textContent, "3");
  assert.equal(nodes.get("mobileProjectGitBadge").textContent, "3");
  assert.match(nodes.get("projectGitChangedList").innerHTML, /is-add.*A/);
  assert.match(nodes.get("projectGitChangedList").innerHTML, /is-mod.*M/);
  assert.match(nodes.get("projectGitChangedList").innerHTML, /is-del.*D/);

  // 3. Install pre-commit shield
  assert.ok(!nodes.get("installProjectShieldBtn").classList.contains("hidden"));
  await api.installProjectGitShieldFromModal();
  assert.ok(nodes.get("installProjectShieldBtn").classList.contains("hidden"));
  assert.match(nodes.get("projectGitShieldStatusText").textContent, /Автозащита коммитов включена/);

  // 4. Load working file diff with + / - highlighting and HTML escaping
  await api.toggleProjectGitDiff({ path: "app.py" });
  const changedHtml = nodes.get("projectGitChangedList").innerHTML;
  assert.match(changedHtml, /git-diff-line is-del.*&lt;script&gt;/);
  assert.match(changedHtml, /git-diff-line is-add.*\+NEW = 42/);

  // 5. Create checkpoint -> badge hides -> HEAD marked "Текущая" -> add 2nd checkpoint -> restore enabled
  mockState.checkpoints.push({
    sha: "1111111111111111111111111111111111111111",
    short_sha: "1111111",
    subject: "Базовый каркас",
    files_changed: 1,
    timestamp: 1795070000,
  });
  nodes.get("gitCheckpointMsgInput").value = "Перед рефакторингом UI";
  await api.createProjectGitCheckpointFromModal();
  assert.equal(nodes.get("gitCheckpointMsgInput").value, "");
  assert.ok(nodes.get("projectGitBadge").classList.contains("hidden"));
  assert.ok(nodes.get("mobileProjectGitBadge").classList.contains("hidden"));
  const cpHtml = nodes.get("projectGitCheckpointsList").innerHTML;
  assert.match(cpHtml, /Перед рефакторингом UI/);
  assert.match(cpHtml, /proj-git-head-pill">Текущая</);
  assert.match(cpHtml, /data-git-restore-sha="1111111111111111111111111111111111111111"/);

  await api.toggleProjectGitDiff({ commit: "1111111111111111111111111111111111111111" });
  assert.match(nodes.get("projectGitCheckpointsList").innerHTML, /proj-git-diff-pre/);

  await api.rollbackProjectGitFromModal("restore_checkpoint", "1111111111111111111111111111111111111111", { skipConfirm: true });
  assert.ok(calls.some(c => c.url.endsWith("/git/rollback") && c.body.mode === "restore_checkpoint"));

  // 6. WebSocket turn completion refreshes project Git status automatically
  mockState.changed_files = [{ path: "worker.py", status: "M", staged: false }];
  const statusCallsBeforeWs = calls.filter(c => c.url.includes("/DemoProj/git/status")).length;
  api.handleWsEvent({ type: "turn_done", chat_id: "c_proj", chat: api.state.chats[1] });
  await new Promise(r => setImmediate(r));
  const statusCallsAfterWs = calls.filter(c => c.url.includes("/DemoProj/git/status")).length;
  assert.equal(statusCallsAfterWs, statusCallsBeforeWs + 1);
  assert.equal(nodes.get("projectGitBadge").textContent, "1");

  // 7. Self-Hub (`Antigravity_Hub`) blocks rollback & hides discard button
  const rollbackCallsBeforeHub = calls.filter(c => c.url.includes("/git/rollback")).length;
  await api.openProjectGitModal("Antigravity_Hub");
  assert.ok(nodes.get("discardGitUncommittedBtn").classList.contains("hidden"));
  assert.equal(nodes.get("discardGitUncommittedBtn").disabled, true);
  const hubCpHtml = nodes.get("projectGitCheckpointsList").innerHTML;
  assert.match(hubCpHtml, /disabled title="Откат ядра Antigravity Hub выполняется при остановленном сервере">Вернуться</);
  const hubRes = await api.rollbackProjectGitFromModal("restore_checkpoint", "2782a76000000000000000000000000000000000", { skipConfirm: true });
  assert.equal(hubRes, null);
  assert.equal(calls.filter(c => c.url.includes("/git/rollback")).length, rollbackCallsBeforeHub);

  // 8. Guardrails toggle in Project Memory modal sends PUT /memory
  api.state.projectMemory.activeProject = "DemoProj";
  await api.saveProjectGuardrailsToggleFromModal(true);
  assert.equal(nodes.get("projDisableGuardrailsChk").checked, true);
  assert.equal(nodes.get("projGuardrailsStatusBadge").textContent, "Отключён");
  await api.saveProjectGuardrailsToggleFromModal(false);
  assert.equal(nodes.get("projDisableGuardrailsChk").checked, false);
  assert.equal(nodes.get("projGuardrailsStatusBadge").textContent, "✓ Активен");

  // 9. Parallel status requests deduplicate into a single in-flight fetch
  const beforePar = calls.filter(c => c.url.includes("/DemoProj/git/status")).length;
  const [p1, p2] = await Promise.all([
    api.refreshProjectGitStatus("DemoProj", { force: true }),
    api.refreshProjectGitStatus("DemoProj", { force: false }),
  ]);
  assert.ok(p1 && p2);
  assert.equal(calls.filter(c => c.url.includes("/DemoProj/git/status")).length, beforePar + 1);

  // 10. Read-only guest sees history and diffs, while mutation buttons stay hidden
  mockState.shield_installed = false;
  mockState.changed_files = [{ path: "app.py", status: "M", staged: false }];
  api.state.currentUser = { id: "g1", username: "guest", role: "user", max_access_mode: "read_only" };
  await api.openProjectGitModal("DemoProj");
  assert.ok(nodes.get("installProjectShieldBtn").classList.contains("hidden"));
  assert.ok(nodes.get("projectGitCreateRow").classList.contains("hidden"));
  assert.ok(nodes.get("discardGitUncommittedBtn").classList.contains("hidden"));
  assert.doesNotMatch(nodes.get("projectGitCheckpointsList").innerHTML, /data-git-restore-sha=/);
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""


def test_git_checkpoints_ui_logic_in_node_vm() -> None:
    parser = ShellParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    parser.close()
    run_node("git-ui-vm", parser.ids, script=NODE_GIT_UI_CHECKS)


CHROMIUM_GIT_GEOMETRY_CHECKS = r"""
const viewports = [[1440, 900], [390, 844], [320, 568]];
function check(ok, message) {
  if (!ok) throw new Error(message);
}

function measureViewport(width, height) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "border:0;position:absolute;width:" + width + "px;height:" + height + "px";
    frame.onload = () => {
      try {
        const w = frame.contentWindow;
        const d = frame.contentDocument;
        const api = w.__AGY_ADVISOR_API__;
        const get = id => d.getElementById(id);
        const rect = el => (typeof el === "string" ? get(el) : el).getBoundingClientRect();
        const mobile = width <= 768;
        const eps = 1.2;
        const close = (a, b) => Math.abs(a - b) <= eps;

        // 1. Active project header with Memory + Git badge + neighboring controls
        api.state.projects = [{ name: "WorkspaceAlpha", is_shared: false }];
        api.state.activeChat = { id: "cp1", kind: "project", project_name: "WorkspaceAlpha", title: "Long branch title ".repeat(6) };
        api.state.projectGit.statusByProject.set("workspacealpha", {
          initialized: true,
          branch: "main",
          head_sha: "abcdef1234567890abcdef1234567890abcdef12",
          head_short: "abcdef1",
          is_clean: false,
          changed_count: 3,
          changed_files: [
            { path: "src/components/very_long_module_filename_for_mobile_layout.py", status: "M", staged: false },
            { path: "tests/test_feature.py", status: "??", staged: false },
            { path: "legacy/old_helper.js", status: "D", staged: false },
          ],
          checkpoints: [
            { sha: "abcdef1234567890abcdef1234567890abcdef12", short_sha: "abcdef1", subject: "Точка сохранения: обновлено 3 файла", files_changed: 3, timestamp: 1795083200 },
            { sha: "9876543210fedcba9876543210fedcba98765432", short_sha: "9876543", subject: "Начальная точка проекта с длинным описанием изменений", files_changed: 5, timestamp: 1795070000 },
          ],
          stashes_count: 1,
          shield_installed: false,
          shield_kind: "project",
        });

        api.applyChatHeaderState(api.state.activeChat);
        get("chatMetaOutlineBtn").textContent = "42";
        get("chatMetaOutlineBtn").classList.remove("hidden");
        get("chatHeaderActions").classList.remove("hidden");
        get("composerSection").classList.remove("hidden");
        if (mobile) {
          get("mobileResetSandboxBtn").classList.remove("hidden");
          get("mobileHostServicesBtn").classList.remove("hidden");
          api.syncMobileHeaderToolsSlot();
        } else {
          get("resetSandboxBtn").classList.remove("hidden");
          get("hostServicesBtn").classList.remove("hidden");
          get("newProjectBranchTopBtn").classList.remove("hidden");
        }
        api.syncProjectGitHeaderIndicator("WorkspaceAlpha", true);

        check(d.documentElement.scrollWidth <= w.innerWidth, "Header caused horizontal document overflow");

        if (!mobile) {
          const memR = rect("openProjectMemoryBtn");
          const gitR = rect("openProjectGitBtn");
          const badgeR = rect("projectGitBadge");
          const titleR = rect("activeChatTitle");
          check(memR.width > 0 && gitR.width > 0 && badgeR.width > 0, "Desktop Git button/badge not visible");
          check(memR.right <= gitR.left + eps, "Memory button overlaps Git button");
          check(gitR.right <= titleR.left + 24, "Git button collides with title");
        } else {
          const barR = rect("mobileAccessBar");
          const segR = rect("mobileAccessSeg");
          const hostR = rect("mobileHostServicesBtn");
          const gitMobR = rect("mobileProjectGitBtn");
          const modR = rect("modelPickerBtn");
          const outR = rect("chatMetaOutlineBtn");
          check(gitMobR.width > 0 && rect("mobileProjectGitBadge").width > 0, "Mobile Git button/badge not visible");
          check(
            segR.right <= rect("mobileResetSandboxBtn").left + eps &&
            rect("mobileResetSandboxBtn").right <= hostR.left + eps &&
            hostR.right <= gitMobR.left + eps &&
            gitMobR.right <= modR.left + eps &&
            modR.right <= outR.left + eps &&
            outR.right <= barR.right + eps,
            "Mobile access bar controls overlap or overflow on " + width + "px"
          );
        }

        // 2. Open #projectGitModal with a 200-char wide diff line
        const longPlus = "+" + "X".repeat(205);
        const longMinus = "-" + "Y".repeat(205);
        api.state.projectGit.activeProject = "WorkspaceAlpha";
        api.state.projectGit.expandedCommitShas.add("abcdef1234567890abcdef1234567890abcdef12");
        api.state.projectGit.diffCache.set(
          "commit::workspacealpha::abcdef1234567890abcdef1234567890abcdef12",
          {
            diff: [
              "diff --git a/app.py b/app.py",
              "--- a/app.py",
              "+++ b/app.py",
              "@@ -1,2 +1,2 @@",
              longMinus,
              longPlus,
            ].join("\n"),
            truncated: false,
          }
        );
        const modal = get("projectGitModal");
        const card = modal.querySelector(".project-git-modal-card");
        card.style.animation = "none";
        modal.classList.remove("hidden");
        api.renderProjectGitModal();

        const cardR = rect(card);
        check(
          cardR.width > 0 && cardR.height > 0 &&
          cardR.left >= -eps && cardR.right <= width + eps &&
          cardR.top >= -eps && cardR.bottom <= height + eps,
          "projectGitModal escaped viewport: " + JSON.stringify(cardR)
        );
        check(d.documentElement.scrollWidth <= w.innerWidth, "projectGitModal caused document overflow");
        check(card.scrollWidth <= card.clientWidth + 1, "projectGitModal card has horizontal overflow");

        if (mobile) {
          check(close(cardR.bottom, height) && close(cardR.left, 0) && close(cardR.width, width),
            "projectGitModal is not a full-width bottom sheet on mobile");
        } else {
          check(close((cardR.left + cardR.right) / 2, width / 2) && close((cardR.top + cardR.bottom) / 2, height / 2),
            "projectGitModal is not centered on desktop");
        }

        const pre = modal.querySelector(".proj-git-diff-pre");
        check(pre && w.getComputedStyle(pre).overflowX === "auto", "Diff <pre> must have overflow-x: auto");
        check(pre.scrollWidth > pre.clientWidth, "200-char diff must scroll locally inside <pre>");
        pre.scrollLeft = 48;
        check(pre.scrollLeft > 0 && card.scrollLeft === 0, "Diff <pre> did not scroll independently");

        resolve({ width, height, cardWidth: Math.round(cardR.width) });
      } catch (err) {
        reject(new Error(width + "x" + height + ": " + err.message));
      } finally {
        frame.remove();
      }
    };
    frame.srcdoc = workspacePage;
    document.body.append(frame);
  });
}

(async () => {
  const result = [];
  for (const [width, height] of viewports) {
    result.push(await measureViewport(width, height));
  }
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


def test_git_header_and_modal_geometry_in_chromium(tmp_path: Path) -> None:
    reports = run_workspace_chromium(tmp_path, CHROMIUM_GIT_GEOMETRY_CHECKS)
    assert [(r["width"], r["height"]) for r in reports] == [
        (1440, 900),
        (390, 844),
        (320, 568),
    ]
