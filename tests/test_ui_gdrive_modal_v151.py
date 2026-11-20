"""Regression suite v151 (TZ2): Google Drive Cloud Mirror UI (#gdriveSettingsCard, #gdriveModal), vault validation, OAuth postMessage & responsive geometry."""
from __future__ import annotations

import re
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = _TESTS_DIR.parent
for _p in (str(ROOT_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_frontend_contracts import STATIC, ShellParser, run_node
from test_ui_workspace_minimal_v142 import run_workspace_chromium


NODE_GDRIVE_UI_CHECKS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const noop = () => {};
const ids = JSON.parse(fs.readFileSync(0, "utf8"));

function createEmitter() {
  const map = new Map();
  return {
    addEventListener(type, fn, capture = false) {
      const key = `${type}::${Boolean(capture)}`;
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(fn);
    },
    removeEventListener(type, fn, capture = false) {
      const key = `${type}::${Boolean(capture)}`;
      const arr = map.get(key);
      if (!arr) return;
      const idx = arr.indexOf(fn);
      if (idx >= 0) arr.splice(idx, 1);
    },
    dispatch(type, event = {}) {
      const evt = {
        type,
        defaultPrevented: false,
        stopped: false,
        immediateStopped: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() { this.stopped = true; },
        stopImmediatePropagation() { this.immediateStopped = true; this.stopped = true; },
        ...event,
      };
      for (const phase of [true, false]) {
        const key = `${type}::${phase}`;
        const list = [...(map.get(key) || [])];
        for (const fn of list) {
          fn(evt);
          if (evt.immediateStopped) return evt;
        }
      }
      return evt;
    },
  };
}

let docRef = null;

function makeElement(id = "") {
  const initiallyHidden = [
    "agyConfirmModal", "settingsModal", "gdriveModal",
    "quickGdriveSyncBtn", "gdriveClientSavedHint", "toggleGdriveClientEditBtn",
    "gdriveConnectedBar", "gdriveVaultPassRow", "gdriveSkippedDetails",
  ].includes(id);
  const classes = new Set(initiallyHidden ? ["hidden"] : []);
  const attrs = new Map();
  const emitter = createEmitter();
  const el = {
    id,
    value: "",
    checked: id === "gdriveSyncSelfHubChk",
    disabled: false,
    textContent: "",
    innerHTML: "",
    title: "",
    placeholder: "",
    className: "",
    isConnected: true,
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
    addEventListener: emitter.addEventListener,
    removeEventListener: emitter.removeEventListener,
    dispatchEvent: evt => emitter.dispatch(evt.type, { target: el, ...evt }),
    click() {
      return emitter.dispatch("click", { target: el });
    },
    focus() {
      if (docRef) docRef.activeElement = el;
    },
    appendChild(child) {
      el.children.push(child);
      return child;
    },
    remove: noop,
    replaceChildren: noop,
    closest: () => null,
    querySelector: sel => sel.startsWith("#") ? (nodes.get(sel.slice(1)) ?? null) : null,
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ left: 20, top: 20, right: 120, bottom: 50, width: 100, height: 30 }),
  };
  return el;
}

const nodes = new Map(ids.map(id => [id, makeElement(id)]));
const docEmitter = createEmitter();
const winEmitter = createEmitter();
const document = {
  documentElement: makeElement("html"),
  body: makeElement("body"),
  activeElement: null,
  getElementById: id => nodes.get(id) ?? null,
  querySelector: sel => sel.startsWith("#") ? (nodes.get(sel.slice(1)) ?? null) : null,
  querySelectorAll: sel => {
    if (sel === ".modal-backdrop:not(.hidden)") {
      return ["settingsModal", "gdriveModal", "agyConfirmModal"]
        .map(id => nodes.get(id))
        .filter(el => el && !el.classList.contains("hidden"));
    }
    return [];
  },
  createElement: tag => makeElement(tag),
  addEventListener: docEmitter.addEventListener,
  removeEventListener: docEmitter.removeEventListener,
  dispatch: docEmitter.dispatch,
};
docRef = document;

const storage = () => {
  const map = new Map();
  return {
    getItem: k => map.get(k) ?? null,
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: k => map.delete(k),
  };
};

const calls = [];
const openedWindows = [];
const nativeConfirmCalls = [];

let serverCfg = {
  user_id: "admin",
  has_oauth_client: false,
  oauth_client_id_mask: "",
  connected: false,
  account_email: "",
  root_folder_id: "",
  root_folder_name: "Antigravity_Hub",
  is_enabled: false,
  sync_self_hub: true,
  excluded_projects: [],
  include_env_vault: false,
  has_vault_passphrase: false,
  last_sync_at: "",
  last_sync_status: "",
  last_sync_error: "",
  last_sync_summary: {},
  sync_in_progress: false,
};
let nextSyncStatusCode = 200;
let syncDeferResolve = null;

async function mockFetch(url, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({ method, url: String(url), body });

  const reply = (payload, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  });

  if (url === "/api/system/info") {
    return reply({
      ok: true,
      user: context.__AGY_ADVISOR_API__.state.currentUser,
      voice: {},
    });
  }
  if (url === "/api/account/rules") {
    return reply({ ok: true, rules: "" });
  }
  if (url === "/api/system/storage") {
    return reply({ ok: true, db_bytes: 1024 });
  }
  if (url === "/api/gdrive/status" && method === "GET") {
    return reply({ ok: true, ...serverCfg });
  }
  if (url === "/api/gdrive/config" && method === "POST") {
    if (body && body.oauth_client_json) {
      serverCfg.has_oauth_client = true;
      serverCfg.oauth_client_id_mask = "123456…apps.googleusercontent.com";
    }
    if (typeof body?.is_enabled === "boolean") serverCfg.is_enabled = body.is_enabled;
    if (typeof body?.sync_self_hub === "boolean") serverCfg.sync_self_hub = body.sync_self_hub;
    if (Array.isArray(body?.excluded_projects)) serverCfg.excluded_projects = [...body.excluded_projects];
    if (typeof body?.include_env_vault === "boolean") serverCfg.include_env_vault = body.include_env_vault;
    if (body?.vault_passphrase) serverCfg.has_vault_passphrase = true;
    return reply({ ok: true, ...serverCfg });
  }
  if (url === "/api/gdrive/oauth/start" && method === "POST") {
    return reply({
      ok: true,
      auth_url: "https://accounts.google.com/o/oauth2/v2/auth?state=st_demo_151&scope=drive.file",
      state: "st_demo_151",
      redirect_uri: "http://127.0.0.1:8765/api/gdrive/oauth/callback",
    });
  }
  if (url === "/api/gdrive/oauth/exchange" && method === "POST") {
    serverCfg.connected = true;
    serverCfg.account_email = "manual.user@gmail.com";
    return reply({ ok: true, ...serverCfg });
  }
  if (url === "/api/gdrive/disconnect" && method === "POST") {
    serverCfg.connected = false;
    serverCfg.account_email = "";
    serverCfg.last_sync_status = "";
    serverCfg.last_sync_summary = {};
    return reply({ ok: true, ...serverCfg });
  }
  if (url === "/api/gdrive/preview" && method === "GET") {
    return reply({
      ok: true,
      regular_chats_count: 14,
      project_chats_count: 9,
      projects_count: 2,
      projects: [
        { name: "AlphaBot", is_self_hub: false, chats_count: 5, code_files_count: 18, code_bytes: 52000 },
        { name: "Antigravity_Hub", is_self_hub: true, chats_count: 4, code_files_count: 42, code_bytes: 280000 },
      ],
      code_files_count: 60,
      code_bytes: 332000,
      vault_candidates_count: 3,
      vault_candidates_bytes: 2048,
      include_env_vault: serverCfg.include_env_vault,
      has_vault_passphrase: serverCfg.has_vault_passphrase,
      sync_self_hub: serverCfg.sync_self_hub,
      excluded_projects: serverCfg.excluded_projects,
      skipped_large_files: [
        { project: "AlphaBot", path: "assets/<big>.bin", size_bytes: 2500000, reason: "size_limit" },
      ],
      skipped_large_files_total: 1,
      skipped_dump_dirs: [
        { project: "AlphaBot", path: "scraped_json", data_files_count: 120, reason: "anti_dump_guard" },
      ],
      skipped_dump_dirs_total: 1,
      skipped_objects: [
        { project: "AlphaBot", path: "scraped_json", data_files_count: 120, reason: "anti_dump_guard" },
        { project: "AlphaBot", path: "assets/<big>.bin", size_bytes: 2500000, reason: "size_limit" },
      ],
    });
  }
  if (url === "/api/gdrive/sync" && method === "POST") {
    if (syncDeferResolve) {
      await new Promise(r => { syncDeferResolve = r; });
    }
    if (nextSyncStatusCode === 409) {
      nextSyncStatusCode = 200;
      return reply({ detail: "Синхронизация с Google Диском уже выполняется." }, 409);
    }
    const summary = {
      uploaded: 7,
      updated: 2,
      skipped_unchanged: 51,
      trashed: 1,
      uploaded_bytes: 48120,
      vault_included: serverCfg.include_env_vault && serverCfg.has_vault_passphrase,
    };
    serverCfg.last_sync_at = "2026-11-20T08:15:00+08:00";
    serverCfg.last_sync_status = "ok";
    serverCfg.last_sync_error = "";
    serverCfg.last_sync_summary = summary;
    return reply({ ok: true, summary, status: { ...serverCfg } });
  }
  return reply({ ok: true });
}

const context = {
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
  document,
  fetch: mockFetch,
  confirm: msg => {
    nativeConfirmCalls.push(String(msg));
    return true;
  },
  open: (url, name, features) => {
    openedWindows.push({ url: String(url), name: String(name), features: String(features || "") });
    return { closed: false };
  },
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
  addEventListener: winEmitter.addEventListener,
  removeEventListener: winEmitter.removeEventListener,
};
context.window = context;
const sandbox = vm.createContext(context);
for (const name of ["ui-shell.js", "app.js"]) {
  vm.runInContext(fs.readFileSync("app/static/" + name, "utf8"), sandbox, { filename: name, timeout: 6000 });
}

async function flushMicrotasks() {
  for (let i = 0; i < 6; i++) {
    await new Promise(r => setImmediate(r));
  }
}

(async () => {
  const api = context.__AGY_ADVISOR_API__;
  const ui = context.AgyUI;

  // 0. New Lucide SVG icons registered without external CDN
  assert.equal(typeof ui.ICO.cloud, "string");
  assert.equal(typeof ui.ICO.lock, "string");
  assert.match(ui.icon("cloud"), /stroke-width="1\.75"/);
  assert.match(ui.icon("lock"), /stroke-width="1\.75"/);

  api.bindGdriveEvents();
  api.state.projects = [
    { name: "AlphaBot", is_shared: false, is_self_hub: false },
    { name: "BetaSite", is_shared: false, is_self_hub: false },
    { name: "Antigravity_Hub", is_shared: false, is_self_hub: true },
  ];

  // 1. Non-admin user: card hidden, zero /api/gdrive/* requests sent even on openSettingsModal
  api.state.currentUser = { id: "g1", username: "guest", role: "user", max_access_mode: "write_files" };
  api.applyRoleUI();
  assert.ok(nodes.get("gdriveSettingsCard").classList.contains("hidden"));
  await api.openSettingsModal();
  await flushMicrotasks();
  assert.ok(nodes.get("gdriveSettingsCard").classList.contains("hidden"));
  assert.equal(calls.filter(c => c.url.startsWith("/api/gdrive/")).length, 0, "Non-admin must never call /api/gdrive/*");
  nodes.get("settingsModal").classList.add("hidden");

  // 2. Admin user: card visible, openGdriveModal loads status
  api.state.currentUser = { id: "u_admin", username: "admin", role: "admin", max_access_mode: "full_access" };
  api.applyRoleUI();
  assert.ok(!nodes.get("gdriveSettingsCard").classList.contains("hidden"));
  await api.openGdriveModal();
  assert.ok(!nodes.get("gdriveModal").classList.contains("hidden"));
  assert.equal(nodes.get("gdriveConnBadge").textContent, "Не подключён");
  assert.ok(nodes.get("quickGdriveSyncBtn").classList.contains("hidden"));
  assert.ok(!nodes.get("gdriveClientEditWrap").classList.contains("hidden"));
  assert.ok(nodes.get("gdriveClientSavedHint").classList.contains("hidden"));

  // 3. Save OAuth client JSON -> collapses textarea & shows saved hint
  nodes.get("gdriveOauthJsonInput").value = JSON.stringify({
    installed: { client_id: "123456.apps.googleusercontent.com", client_secret: "GOCSPX-demo" },
  });
  await api.saveGdriveOauthClient();
  assert.ok(calls.some(c => c.url === "/api/gdrive/config" && c.method === "POST" && c.body.oauth_client_json));
  assert.ok(nodes.get("gdriveClientEditWrap").classList.contains("hidden"));
  assert.ok(!nodes.get("gdriveClientSavedHint").classList.contains("hidden"));
  assert.match(nodes.get("gdriveClientSavedHint").textContent, /Ключ OAuth сохранён ✓/);
  assert.ok(!nodes.get("toggleGdriveClientEditBtn").classList.contains("hidden"));
  assert.equal(nodes.get("gdriveOauthJsonInput").value, "");

  // 4. Start OAuth flow -> POST /api/gdrive/oauth/start & window.open
  await api.startGdriveOauthFlow();
  assert.equal(openedWindows.length, 1);
  assert.match(openedWindows[0].url, /accounts\.google\.com/);
  assert.doesNotMatch(openedWindows[0].features, /noopener/);

  // 5. OAuth postMessage: foreign origin ignored; same-origin refreshes status & connects UI
  const statusCallsBeforeMsg = calls.filter(c => c.url === "/api/gdrive/status").length;
  winEmitter.dispatch("message", {
    origin: "https://evil.invalid",
    data: { type: "agy-gdrive-oauth-done", ok: true, account_email: "evil@evil.invalid" },
  });
  await flushMicrotasks();
  assert.equal(calls.filter(c => c.url === "/api/gdrive/status").length, statusCallsBeforeMsg);

  serverCfg.connected = true;
  serverCfg.account_email = "owner.mirror@gmail.com";
  winEmitter.dispatch("message", {
    origin: "https://hub.invalid",
    data: { type: "agy-gdrive-oauth-done", ok: true, account_email: "owner.mirror@gmail.com" },
  });
  await flushMicrotasks();
  assert.equal(calls.filter(c => c.url === "/api/gdrive/status").length, statusCallsBeforeMsg + 1);
  assert.ok(!nodes.get("gdriveConnectedBar").classList.contains("hidden"));
  assert.ok(nodes.get("gdriveConnectActionsBox").classList.contains("hidden"));
  assert.equal(nodes.get("gdriveAccountEmail").textContent, "owner.mirror@gmail.com");
  assert.equal(nodes.get("gdriveSettingsStatusPill").textContent, "✓ owner.mirror@gmail.com");
  assert.ok(!nodes.get("quickGdriveSyncBtn").classList.contains("hidden"));

  // 6. Manual OAuth code/URL exchange
  nodes.get("gdriveManualCallbackInput").value = "http://127.0.0.1:8765/api/gdrive/oauth/callback?code=4/manual123&state=st_demo_151";
  await api.exchangeGdriveOauthCode();
  const exCall = calls.find(c => c.url === "/api/gdrive/oauth/exchange" && c.method === "POST");
  assert.ok(exCall && exCall.body.callback_url.includes("code=4/manual123"));
  assert.equal(nodes.get("gdriveAccountEmail").textContent, "manual.user@gmail.com");
  assert.equal(nodes.get("gdriveManualCallbackInput").value, "");

  // 7. Encrypted .env vault passphrase validation
  nodes.get("gdriveIncludeEnvVaultChk").checked = true;
  await api.handleGdriveVaultToggleChange();
  assert.ok(!nodes.get("gdriveVaultPassRow").classList.contains("hidden"));
  assert.equal(nodes.get("gdriveVaultPassStatusBadge").textContent, "Требуется пароль");

  const configCallsBeforeMissingPass = calls.filter(c => c.url === "/api/gdrive/config").length;
  nodes.get("gdriveVaultPassInput").value = "";
  const missingPassRes = await api.saveGdriveSettings();
  assert.equal(missingPassRes, null);
  assert.equal(calls.filter(c => c.url === "/api/gdrive/config").length, configCallsBeforeMissingPass);
  assert.ok(nodes.get("gdriveVaultPassInput").classList.contains("is-invalid"));

  // Provide passphrase & exclude BetaSite -> saves cleanly & clears password input
  api.state.gdrive.excludedDraft.add("betasite");
  nodes.get("gdriveAutoSyncChk").checked = true;
  nodes.get("gdriveVaultPassInput").value = "SuperSecretVault2026!";
  await api.saveGdriveSettings();
  const saveCfgCall = calls.filter(c => c.url === "/api/gdrive/config").at(-1);
  assert.equal(saveCfgCall.body.is_enabled, true);
  assert.equal(saveCfgCall.body.include_env_vault, true);
  assert.equal(saveCfgCall.body.vault_passphrase, "SuperSecretVault2026!");
  assert.deepEqual(saveCfgCall.body.excluded_projects, ["BetaSite"]);
  assert.equal(nodes.get("gdriveVaultPassInput").value, "");
  assert.equal(nodes.get("gdriveVaultPassStatusBadge").textContent, "Пароль задан ✓");

  // 8. Unchecking .env vault when already enabled -> asks via AgyUI.confirm (not native confirm)
  nodes.get("gdriveIncludeEnvVaultChk").checked = false;
  const disableCancelPromise = api.handleGdriveVaultToggleChange();
  await flushMicrotasks();
  assert.equal(nativeConfirmCalls.length, 0);
  assert.ok(!nodes.get("agyConfirmModal").classList.contains("hidden"));
  assert.match(nodes.get("agyConfirmMessage").textContent, /secrets\.vault\.enc.*Корзину/);
  nodes.get("agyConfirmCancelBtn").click();
  await disableCancelPromise;
  assert.equal(nodes.get("gdriveIncludeEnvVaultChk").checked, true, "Cancel must keep vault enabled");

  nodes.get("gdriveIncludeEnvVaultChk").checked = false;
  const disableOkPromise = api.handleGdriveVaultToggleChange();
  await flushMicrotasks();
  nodes.get("agyConfirmOkBtn").click();
  await disableOkPromise;
  assert.equal(nodes.get("gdriveIncludeEnvVaultChk").checked, false);
  await api.saveGdriveSettings();
  assert.equal(serverCfg.include_env_vault, false);

  // 9. Dry-run preview renders metrics and escaped skipped dump/large entries
  await api.previewGdriveSync();
  assert.ok(calls.some(c => c.url === "/api/gdrive/preview" && c.method === "GET"));
  const sumHtml = nodes.get("gdrivePreviewSummary").innerHTML;
  assert.match(sumHtml, /14/);
  assert.match(sumHtml, /60 ф\./);
  assert.match(sumHtml, /3 ф\./);
  assert.ok(!nodes.get("gdriveSkippedDetails").classList.contains("hidden"));
  assert.equal(nodes.get("gdriveSkippedCount").textContent, "2");
  const skipHtml = nodes.get("gdriveSkippedList").innerHTML;
  assert.match(skipHtml, /ДАМП · 120 ф\./);
  assert.match(skipHtml, /AlphaBot\/scraped_json/);
  assert.match(skipHtml, /AlphaBot\/assets\/&lt;big&gt;\.bin/);

  // 10. Sync double-click guard, 409 Conflict handling & final summary
  syncDeferResolve = () => {};
  const pSync1 = api.runGdriveSync();
  assert.equal(nodes.get("runGdriveSyncBtn").disabled, true);
  assert.match(nodes.get("runGdriveSyncBtn").textContent, /Синхронизация…/);
  const syncCallsBeforeDup = calls.filter(c => c.url === "/api/gdrive/sync").length;
  const pSyncDup = await api.runGdriveSync();
  assert.equal(pSyncDup, null);
  assert.equal(calls.filter(c => c.url === "/api/gdrive/sync").length, syncCallsBeforeDup);
  const release = syncDeferResolve;
  syncDeferResolve = null;
  release();
  await pSync1;
  assert.equal(nodes.get("runGdriveSyncBtn").disabled, false);
  assert.match(nodes.get("gdrivePreviewSummary").innerHTML, /загружено <strong>7<\/strong>, обновлено <strong>2<\/strong>/);
  assert.match(nodes.get("gdrivePreviewSummary").innerHTML, /без изменений \(SHA-256\) <strong>51<\/strong>, в Корзину <strong>1<\/strong>/);

  nextSyncStatusCode = 409;
  const conflictRes = await api.runGdriveSync();
  assert.equal(conflictRes, null);
  assert.equal(api.state.gdrive.syncing, false);

  // 11. Disconnect via askUserConfirm (cancel then confirm)
  const discCancel = api.disconnectGdriveFromModal();
  await flushMicrotasks();
  assert.equal(nativeConfirmCalls.length, 0);
  assert.ok(!nodes.get("agyConfirmModal").classList.contains("hidden"));
  nodes.get("agyConfirmCancelBtn").click();
  await discCancel;
  assert.equal(calls.filter(c => c.url === "/api/gdrive/disconnect").length, 0);

  const discConfirm = api.disconnectGdriveFromModal();
  await flushMicrotasks();
  nodes.get("agyConfirmOkBtn").click();
  await discConfirm;
  assert.equal(calls.filter(c => c.url === "/api/gdrive/disconnect").length, 1);
  assert.equal(nodes.get("gdriveConnBadge").textContent, "Ключ задан");
  assert.ok(nodes.get("gdriveConnectedBar").classList.contains("hidden"));
  assert.ok(!nodes.get("gdriveConnectActionsBox").classList.contains("hidden"));
  assert.ok(nodes.get("quickGdriveSyncBtn").classList.contains("hidden"));
  assert.equal(nativeConfirmCalls.length, 0, "Zero native window.confirm() calls allowed");
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""


def test_gdrive_ui_controller_in_node_vm() -> None:
    parser = ShellParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    parser.close()
    run_node("gdrive-ui-vm", parser.ids, script=NODE_GDRIVE_UI_CHECKS)


def test_zero_new_important_in_ui_system_css() -> None:
    css = (STATIC / "ui-system.css").read_text(encoding="utf-8")
    stripped = re.sub(r"/\*[\s\S]*?\*/", "", css)
    important_hits = re.findall(r"!\s*important\b", stripped, flags=re.IGNORECASE)
    assert len(important_hits) == 1, f"Expected strictly 1 !important in @layer utilities, got {len(important_hits)}"
    assert "#gdriveModal" in stripped and "#gdriveSettingsCard" in stripped


CHROMIUM_GDRIVE_GEOMETRY_CHECKS = r"""
const viewports = [[1440, 900], [390, 844], [320, 568]];
function check(ok, message) {
  if (!ok) throw new Error(message);
}

function measureViewport(width, height) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "border:0;position:absolute;width:" + width + "px;height:" + height + "px";
    frame.onload = async () => {
      try {
        const w = frame.contentWindow;
        const d = frame.contentDocument;
        const api = w.__AGY_ADVISOR_API__;
        const get = id => d.getElementById(id);
        const rect = el => (typeof el === "string" ? get(el) : el).getBoundingClientRect();
        const mobile = width <= 768;
        const eps = 1.5;
        const close = (a, b) => Math.abs(a - b) <= eps;

        api.state.currentUser = { id: "u1", username: "admin", role: "admin", max_access_mode: "full_access" };
        api.state.projects = [
          { name: "VeryLongProjectNameForMobileLayoutCheck_Alpha", is_shared: false, is_self_hub: false },
          { name: "TradingBot_Production", is_shared: false, is_self_hub: false },
          { name: "Antigravity_Hub", is_shared: false, is_self_hub: true },
        ];
        api.state.gdrive.status = {
          has_oauth_client: true,
          oauth_client_id_mask: "99887766…apps.googleusercontent.com",
          connected: true,
          account_email: "very.long.admin.cloud.mirror.account@example-domain.com",
          root_folder_name: "Antigravity_Hub",
          is_enabled: true,
          sync_self_hub: true,
          excluded_projects: ["TradingBot_Production"],
          include_env_vault: true,
          has_vault_passphrase: true,
          last_sync_at: "2026-11-20T08:15:00+08:00",
          last_sync_status: "ok",
          last_sync_error: "",
          last_sync_summary: {
            uploaded: 12,
            updated: 3,
            skipped_unchanged: 140,
            trashed: 1,
            uploaded_bytes: 94200,
            vault_included: true,
          },
          sync_in_progress: false,
        };
        api.state.gdrive.preview = {
          regular_chats_count: 18,
          project_chats_count: 25,
          projects_count: 2,
          code_files_count: 148,
          code_bytes: 842500,
          vault_candidates_count: 4,
          include_env_vault: true,
          has_vault_passphrase: true,
          sync_self_hub: true,
          skipped_dump_dirs: [
            {
              project: "VeryLongProjectNameForMobileLayoutCheck_Alpha",
              path: "raw_exports/2026_11_full_json_scrape_dump_folder",
              data_files_count: 245,
              reason: "anti_dump_guard",
            },
          ],
          skipped_dump_dirs_total: 1,
          skipped_large_files: [
            {
              project: "Antigravity_Hub",
              path: "static/models/extremely_long_untracked_weights_bundle_file_name.bin",
              size_bytes: 4200000,
              reason: "size_limit",
            },
          ],
          skipped_large_files_total: 1,
        };

        // Wire workspace Escape handler onto document for real layered Escape test
        d.addEventListener("keydown", e => {
          if (e.key === "Escape") api.handleWorkspaceEscape(e);
        });

        // 1. Check #gdriveSettingsCard inside #settingsModal
        api.applyRoleUI();
        api.renderGdriveSettingsCard();
        const settingsModal = get("settingsModal");
        const settingsCard = settingsModal.querySelector(".modal-card");
        settingsCard.style.animation = "none";
        settingsModal.classList.remove("hidden");
        const gdCard = get("gdriveSettingsCard");
        const gdCardR = rect(gdCard);
        const setCardR = rect(settingsCard);
        check(!gdCard.classList.contains("hidden"), "#gdriveSettingsCard must be visible for admin");
        check(!get("quickGdriveSyncBtn").classList.contains("hidden"), "#quickGdriveSyncBtn must be visible when connected");
        check(gdCardR.left >= setCardR.left - eps && gdCardR.right <= setCardR.right + eps, "#gdriveSettingsCard overflows settings modal");
        check(d.documentElement.scrollWidth <= w.innerWidth, "#settingsModal with gdrive card overflowed viewport on " + width + "px");
        settingsModal.classList.add("hidden");

        // 2. Open #gdriveModal with populated vault, checklist & skipped dump/large list
        const modal = get("gdriveModal");
        const card = modal.querySelector(".gdrive-modal-card");
        card.style.animation = "none";
        get("gdriveIncludeEnvVaultChk").checked = true;
        get("gdriveExcludedProjectsDetails").open = true;
        modal.classList.remove("hidden");
        api.renderGdriveModal();
        get("gdriveSkippedDetails").open = true;

        const cardR = rect(card);
        check(
          cardR.width > 0 && cardR.height > 0 &&
          cardR.left >= -eps && cardR.right <= width + eps &&
          cardR.top >= -eps && cardR.bottom <= height + eps,
          "#gdriveModal escaped viewport: " + JSON.stringify(cardR)
        );
        check(d.documentElement.scrollWidth <= w.innerWidth, "#gdriveModal caused document horizontal overflow on " + width + "px");
        check(card.scrollWidth <= card.clientWidth + 1, "#gdriveModal card has horizontal overflow on " + width + "px");

        if (mobile) {
          check(close(cardR.bottom, height) && close(cardR.left, 0) && close(cardR.width, width),
            "#gdriveModal is not a full-width bottom sheet on " + width + "px");
        } else {
          check(close((cardR.left + cardR.right) / 2, width / 2) && close((cardR.top + cardR.bottom) / 2, height / 2),
            "#gdriveModal is not centered on desktop");
        }

        // Verify close button and refresh button inside header bounds
        const closeBtn = modal.querySelector('[data-close-modal="gdriveModal"]');
        const closeR = rect(closeBtn);
        check(closeR.left >= cardR.left - eps && closeR.right <= cardR.right + eps, "Close button clipped out of #gdriveModal header");

        // 3. Open #agyConfirmModal on top of #gdriveModal -> Escape closes ONLY #agyConfirmModal first, then #gdriveModal
        const confirmModal = get("agyConfirmModal");
        confirmModal.querySelector(".agy-confirm-card").style.animation = "none";
        const confirmPromise = w.AgyUI.confirm({
          title: "Отключить Google Диск?",
          message: "Отвязать аккаунт от Antigravity Hub?",
          confirmText: "Отключить",
          cancelText: "Отмена",
          danger: true,
        });
        const zConfirm = parseInt(w.getComputedStyle(confirmModal).zIndex || "0", 10);
        const zGdrive = parseInt(w.getComputedStyle(modal).zIndex || "0", 10);
        check(zConfirm > zGdrive, "Confirm z-index (" + zConfirm + ") must exceed #gdriveModal (" + zGdrive + ")");

        d.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
        const confirmRes = await confirmPromise;
        check(confirmRes === false, "First Escape must resolve AgyUI.confirm with false");
        check(confirmModal.classList.contains("hidden"), "First Escape must close #agyConfirmModal");
        check(!modal.classList.contains("hidden"), "First Escape must NOT close parent #gdriveModal");

        d.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
        check(modal.classList.contains("hidden"), "Second Escape must close #gdriveModal");

        resolve({ width, height, viewport: width + "x" + height, cardWidth: Math.round(cardR.width), zConfirm, zGdrive });
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


def test_gdrive_modal_geometry_and_escape_stacking_in_chromium(tmp_path: Path) -> None:
    reports = run_workspace_chromium(tmp_path, CHROMIUM_GDRIVE_GEOMETRY_CHECKS)
    assert [row["viewport"] for row in reports] == ["1440x900", "390x844", "320x568"]
