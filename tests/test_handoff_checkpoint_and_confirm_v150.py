"""Regression suite v150 (TZ3): Handoff Git auto-checkpoint & universal minimal AgyUI.confirm()."""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = _TESTS_DIR.parent
for _p in (str(ROOT_DIR), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_frontend_contracts import STATIC, ShellParser, run_node
from test_ui_workspace_minimal_v142 import run_workspace_chromium


NODE_CONFIRM_AND_HANDOFF_CHECKS = r"""
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
    "agyConfirmModal", "handoffNewTabModal", "handoffGitCheckpointBox",
    "handoffOrBoostersBox", "handoffIconPickerBox", "handoffAdvisorSlot",
    "handoffModelMenu", "handoffQuotaPills", "handoffAccountPickerRow",
    "handoffNewRecPill", "settingsModal", "projectGitModal",
    "openProjectGitBtn", "projectGitBadge", "mobileProjectGitBtn", "mobileProjectGitBadge",
  ].includes(id);
  const classes = new Set(initiallyHidden ? ["hidden"] : []);
  const attrs = new Map();
  const emitter = createEmitter();
  const el = {
    id,
    value: "",
    checked: false,
    disabled: false,
    textContent: "",
    innerHTML: "",
    title: "",
    placeholder: "",
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
    select: noop,
    appendChild: noop,
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
const document = {
  documentElement: makeElement("html"),
  body: makeElement("body"),
  activeElement: null,
  getElementById: id => nodes.get(id) ?? null,
  querySelector: sel => sel.startsWith("#") ? (nodes.get(sel.slice(1)) ?? null) : null,
  querySelectorAll: sel => {
    if (sel === ".modal-backdrop:not(.hidden)") {
      return ["settingsModal", "handoffNewTabModal", "projectGitModal", "agyConfirmModal"]
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
let gitStatusByProject = {
  cleanproj: {
    ok: true,
    project_name: "CleanProj",
    is_self_hub: false,
    initialized: true,
    branch: "main",
    head_sha: "1111111111111111111111111111111111111111",
    head_short: "1111111",
    is_clean: true,
    changed_count: 0,
    changed_files: [],
    checkpoints: [],
    stashes_count: 0,
    shield_installed: true,
    shield_kind: "project",
  },
  demoproj: {
    ok: true,
    project_name: "DemoProj",
    is_self_hub: false,
    initialized: true,
    branch: "main",
    head_sha: "2222222222222222222222222222222222222222",
    head_short: "2222222",
    is_clean: false,
    changed_count: 3,
    changed_files: [
      { path: "app.py", status: "M", staged: false },
      { path: "utils.py", status: "??", staged: false },
      { path: "old.py", status: "D", staged: false },
    ],
    checkpoints: [],
    stashes_count: 0,
    shield_installed: true,
    shield_kind: "project",
  },
  antigravity_hub: {
    ok: true,
    project_name: "Antigravity_Hub",
    is_self_hub: true,
    initialized: true,
    branch: "main",
    head_sha: "30d3bcd000000000000000000000000000000000",
    head_short: "30d3bcd",
    is_clean: false,
    changed_count: 2,
    changed_files: [{ path: "app/static/app.js", status: "M", staged: false }],
    checkpoints: [],
    stashes_count: 0,
    shield_installed: true,
    shield_kind: "hub",
  },
};
let failNextCheckpointWith = null;
let nativeConfirmCalls = [];

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

  if (url.includes("/git/status")) {
    const match = url.match(/\/api\/projects\/([^/]+)\/git\/status/i);
    const key = match ? decodeURIComponent(match[1]).toLowerCase() : "demoproj";
    return reply(gitStatusByProject[key] || { ok: true, initialized: false, changed_count: 0 });
  }
  if (url.includes("/git/checkpoint") && method === "POST") {
    if (failNextCheckpointWith) {
      return reply({ detail: failNextCheckpointWith }, 400);
    }
    const match = url.match(/\/api\/projects\/([^/]+)\/git\/checkpoint/i);
    const key = match ? decodeURIComponent(match[1]).toLowerCase() : "demoproj";
    if (gitStatusByProject[key]) {
      gitStatusByProject[key] = {
        ...gitStatusByProject[key],
        is_clean: true,
        changed_count: 0,
        changed_files: [],
        head_short: "beef999",
      };
    }
    return reply({ ok: true, created: true, sha: "beef999000000000000000000000000000000000", short_sha: "beef999" });
  }
  if (url === "/api/chats" && method === "POST") {
    return reply({
      ok: true,
      chat: {
        id: "chat_created_" + calls.length,
        kind: "project",
        project_name: body.project_name,
        title: body.title,
        model: body.model,
        access_mode: body.access_mode,
        icon: body.icon,
      },
    });
  }
  if (url.startsWith("/api/chats/") && url.endsWith("/messages")) {
    return reply({ ok: true, messages: [] });
  }
  if (url.startsWith("/api/chats?")) {
    return reply({ ok: true, chats: [] });
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

async function flushMicrotasks() {
  for (let i = 0; i < 6; i++) {
    await new Promise(r => setImmediate(r));
  }
}

(async () => {
  const api = context.__AGY_ADVISOR_API__;
  const ui = context.AgyUI;
  assert.equal(typeof ui.confirm, "function");

  // Wire app's Escape handler to document keydown (bubble phase) to verify Escape stacking
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") api.handleWorkspaceEscape(e);
  });

  // 1. AgyUI.confirm: OK button -> true, danger toggles classes
  const p1 = ui.confirm({
    title: "Удалить аккаунт",
    message: "Точно удалить?",
    confirmText: "Удалить",
    cancelText: "Отмена",
    danger: true,
  });
  assert.ok(!nodes.get("agyConfirmModal").classList.contains("hidden"));
  assert.equal(nodes.get("agyConfirmTitle").textContent, "Удалить аккаунт");
  assert.equal(nodes.get("agyConfirmMessage").textContent, "Точно удалить?");
  assert.ok(nodes.get("agyConfirmOkBtn").classList.contains("btn-danger"));
  assert.ok(nodes.get("agyConfirmOkBtn").classList.contains("ui-btn-danger"));
  assert.ok(!nodes.get("agyConfirmOkBtn").classList.contains("btn-primary"));
  nodes.get("agyConfirmOkBtn").click();
  assert.equal(await p1, true);
  assert.ok(nodes.get("agyConfirmModal").classList.contains("hidden"));

  // 2. AgyUI.confirm: Enter on focused OK button -> true, non-danger uses btn-primary
  const pEnter = ui.confirm({ title: "Запуск", message: "Продолжить?", danger: false });
  assert.ok(nodes.get("agyConfirmOkBtn").classList.contains("btn-primary"));
  assert.ok(!nodes.get("agyConfirmOkBtn").classList.contains("btn-danger"));
  nodes.get("agyConfirmOkBtn").focus();
  document.dispatch("keydown", { key: "Enter" });
  assert.equal(await pEnter, true);

  // 3. AgyUI.confirm: Cancel button -> false; Backdrop click -> false
  const pCancel = ui.confirm({ title: "Отмена", message: "Проверка отмены" });
  nodes.get("agyConfirmCancelBtn").click();
  assert.equal(await pCancel, false);

  const pBackdrop = ui.confirm({ title: "Фон", message: "Клик по фону" });
  nodes.get("agyConfirmModal").click();
  assert.equal(await pBackdrop, false);

  // 4. AgyUI.confirm on top of open parent modal (#settingsModal): Escape closes ONLY #agyConfirmModal
  nodes.get("settingsModal").classList.remove("hidden");
  const pEsc = ui.confirm({ title: "Перезапуск", message: "Перезапустить сервер?" });
  assert.ok(!nodes.get("agyConfirmModal").classList.contains("hidden"));
  document.dispatch("keydown", { key: "Escape" });
  assert.equal(await pEsc, false);
  assert.ok(nodes.get("agyConfirmModal").classList.contains("hidden"));
  assert.ok(!nodes.get("settingsModal").classList.contains("hidden"), "Parent #settingsModal must stay open after confirm Escape");
  nodes.get("settingsModal").classList.add("hidden");

  // 5. triggerServerRestart uses AgyUI.confirm (not native confirm) and keeps settings open on Cancel
  api.state.currentUser = { id: "u1", username: "admin", role: "admin", max_access_mode: "full_access" };
  nodes.get("settingsModal").classList.remove("hidden");
  const restartPromise = api.triggerServerRestart();
  await flushMicrotasks();
  assert.equal(nativeConfirmCalls.length, 0);
  assert.ok(!nodes.get("agyConfirmModal").classList.contains("hidden"));
  nodes.get("agyConfirmCancelBtn").click();
  await restartPromise;
  assert.ok(!nodes.get("settingsModal").classList.contains("hidden"));
  assert.ok(!calls.some(c => c.url === "/api/system/restart"));
  nodes.get("settingsModal").classList.add("hidden");

  // 6. Native confirm fallback flag for legacy VM compatibility
  context.__AGY_USE_NATIVE_CONFIRM__ = true;
  const nativeOk = await api.askUserConfirm({ title: "Legacy", message: "Fallback check" });
  assert.equal(nativeOk, true);
  assert.deepEqual(nativeConfirmCalls, ["Fallback check"]);
  context.__AGY_USE_NATIVE_CONFIRM__ = false;

  // 7. Handoff modal Git auto-checkpoint box visibility & defaults
  api.state.projects = [
    { name: "CleanProj", is_shared: false, is_self_hub: false },
    { name: "DemoProj", is_shared: false, is_self_hub: false },
    { name: "Antigravity_Hub", is_shared: false, is_self_hub: true },
  ];
  const sampleMsg = { id: "m1", role: "assistant", content: "# План\nСделай рефакторинг модуля" };

  // 7a. CleanProj (changed_count === 0) -> hidden
  api.openHandoffNewTabModal("CleanProj", sampleMsg, "ШТАБ", "c_hq");
  await flushMicrotasks();
  assert.ok(nodes.get("handoffGitCheckpointBox").classList.contains("hidden"));
  api.closeHandoffNewTabModal();

  // 7b. Antigravity_Hub (is_self_hub: true, changed_count === 2) -> visible, checked === false
  api.openHandoffNewTabModal("Antigravity_Hub", sampleMsg, "ШТАБ", "c_hq");
  await flushMicrotasks();
  assert.ok(!nodes.get("handoffGitCheckpointBox").classList.contains("hidden"));
  assert.equal(nodes.get("handoffAutoCheckpointChk").checked, false);
  assert.match(nodes.get("handoffGitCheckpointLabel").textContent, /2 ф\./);
  assert.match(nodes.get("handoffGitCheckpointSub").textContent, /pre-commit ~60 с/);
  api.closeHandoffNewTabModal();

  // 7c. DemoProj (regular project, changed_count === 3) -> visible, checked === true
  api.state.activeChat = { id: "c_proj_hq", kind: "project", project_name: "DemoProj", title: "ШТАБ" };
  api.openHandoffNewTabModal("DemoProj", sampleMsg, "ШТАБ", "c_proj_hq");
  await flushMicrotasks();
  assert.ok(!nodes.get("handoffGitCheckpointBox").classList.contains("hidden"));
  assert.equal(nodes.get("handoffAutoCheckpointChk").checked, true);
  assert.match(nodes.get("handoffGitCheckpointLabel").textContent, /3 ф\./);

  // 7d. Syntax validation failure during auto-checkpoint -> AgyUI.confirm prompt
  failNextCheckpointWith = "SyntaxError in app.py: invalid syntax";
  nodes.get("handoffTitleInput").value = "Фикс парсера";
  const chatsBeforeFail = calls.filter(c => c.url === "/api/chats" && c.method === "POST").length;
  const submitFailCancel = api.submitHandoffNewTabModal();
  await flushMicrotasks();
  assert.ok(!nodes.get("agyConfirmModal").classList.contains("hidden"), "AgyUI.confirm must open on checkpoint error");
  assert.match(nodes.get("agyConfirmMessage").textContent, /SyntaxError in app\.py/);
  // Cancel -> stay in handoff modal, do NOT launch chat
  nodes.get("agyConfirmCancelBtn").click();
  await submitFailCancel;
  assert.ok(!nodes.get("handoffNewTabModal").classList.contains("hidden"), "Handoff modal must stay open after cancelling");
  assert.equal(nodes.get("confirmHandoffNewTabBtn").disabled, false);
  assert.equal(calls.filter(c => c.url === "/api/chats" && c.method === "POST").length, chatsBeforeFail);

  // Retry and accept launching without checkpoint -> launches chat!
  const submitFailProceed = api.submitHandoffNewTabModal();
  await flushMicrotasks();
  assert.ok(!nodes.get("agyConfirmModal").classList.contains("hidden"));
  nodes.get("agyConfirmOkBtn").click();
  await submitFailProceed;
  assert.ok(nodes.get("handoffNewTabModal").classList.contains("hidden"));
  assert.equal(calls.filter(c => c.url === "/api/chats" && c.method === "POST").length, chatsBeforeFail + 1);

  // 7e. Happy path: auto-checkpoint succeeds BEFORE POST /api/chats and updates #projectGitBadge
  failNextCheckpointWith = null;
  calls.length = 0;
  api.openHandoffNewTabModal("DemoProj", sampleMsg, "ШТАБ", "c_proj_hq");
  await flushMicrotasks();
  assert.equal(nodes.get("handoffAutoCheckpointChk").checked, true);
  nodes.get("handoffTitleInput").value = "Реализация ТЗ3";
  await api.submitHandoffNewTabModal();
  const cpIdx = calls.findIndex(c => c.url.includes("/DemoProj/git/checkpoint") && c.method === "POST");
  const chatIdx = calls.findIndex(c => c.url === "/api/chats" && c.method === "POST");
  assert.ok(cpIdx >= 0, "Auto-checkpoint POST must be sent");
  assert.ok(chatIdx > cpIdx, "POST /api/chats must happen AFTER auto-checkpoint");
  assert.match(calls[cpIdx].body.message, /Авто-точка перед «В Код»: Реализация ТЗ3/);
  assert.ok(nodes.get("projectGitBadge").classList.contains("hidden"), "Badge hides once working tree becomes clean");
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""


def test_confirm_and_handoff_auto_checkpoint_in_node_vm() -> None:
    parser = ShellParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    parser.close()
    run_node("confirm-handoff-vm", parser.ids, script=NODE_CONFIRM_AND_HANDOFF_CHECKS)


CHROMIUM_V150_GEOMETRY_CHECKS = r"""
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

        api.state.currentUser = { id: "u1", username: "admin", role: "admin", max_access_mode: "full_access" };
        api.state.projects = [{ name: "DemoProj", is_shared: false, is_self_hub: false }];
        api.state.projectGit.statusByProject.set("demoproj", {
          ok: true,
          initialized: true,
          is_self_hub: false,
          branch: "main",
          head_short: "abcdef1",
          changed_count: 3,
          changed_files: [
            { path: "app/main.py", status: "M", staged: false },
            { path: "app/static/app.js", status: "M", staged: false },
            { path: "tests/test_new.py", status: "??", staged: false },
          ],
          checkpoints: [],
          stashes_count: 0,
          shield_installed: true,
          shield_kind: "project",
        });

        // 1. Open #handoffNewTabModal with visible #handoffGitCheckpointBox
        const handoffModal = get("handoffNewTabModal");
        const hCard = handoffModal.querySelector(".modal-card");
        hCard.style.animation = "none";
        handoffModal.classList.remove("hidden");
        api.state.sendToCode.modalProjName = "DemoProj";
        api.state.sendToCode.autoCheckpointUserTouched = false;
        api.syncHandoffGitCheckpointUI("DemoProj", api.state.projectGit.statusByProject.get("demoproj"), { initDefault: true });

        check(d.documentElement.scrollWidth <= w.innerWidth, "Handoff modal overflowed horizontally on " + width + "px");
        const hCardR = rect(hCard);
        const gitBox = get("handoffGitCheckpointBox");
        const gitBoxR = rect(gitBox);
        check(!gitBox.classList.contains("hidden"), "#handoffGitCheckpointBox must be visible");
        check(get("handoffAutoCheckpointChk").checked === true, "Checkbox must be checked by default for DemoProj");
        check(gitBoxR.width > 0 && gitBoxR.height > 0, "#handoffGitCheckpointBox has zero size");
        check(gitBoxR.left >= hCardR.left - eps && gitBoxR.right <= hCardR.right + eps, "#handoffGitCheckpointBox overflows handoff card on " + width + "px");

        if (mobile) {
          check(Math.abs(hCardR.bottom - height) <= eps, "Handoff modal must be a bottom sheet on " + width + "px");
        }

        // 2. Open #agyConfirmModal on top of already-open #handoffNewTabModal
        const confirmModal = get("agyConfirmModal");
        const cCard = confirmModal.querySelector(".agy-confirm-card");
        cCard.style.animation = "none";
        const confirmPromise = w.AgyUI.confirm({
          title: "Не удалось создать авто-точку",
          message: "Не удалось создать авто-точку сохранения (SyntaxError in app/main.py). Запустить задачу в работу без предварительной точки?",
          confirmText: "Запустить без точки",
          cancelText: "Отмена",
          danger: true,
        });

        const cCardR = rect(cCard);
        const cancelR = rect("agyConfirmCancelBtn");
        const okR = rect("agyConfirmOkBtn");

        check(!confirmModal.classList.contains("hidden"), "#agyConfirmModal must be visible");
        check(!handoffModal.classList.contains("hidden"), "Parent #handoffNewTabModal must stay open beneath confirm");
        check(d.documentElement.scrollWidth <= w.innerWidth, "Confirm modal overflowed horizontally on " + width + "px");

        const zConfirm = parseInt(w.getComputedStyle(confirmModal).zIndex || "0", 10);
        const zHandoff = parseInt(w.getComputedStyle(handoffModal).zIndex || "0", 10);
        check(zConfirm > zHandoff, "Confirm z-index (" + zConfirm + ") must exceed parent modal (" + zHandoff + ")");

        check(cCardR.left >= -eps && cCardR.right <= width + eps, "Confirm card clips horizontally on " + width + "px");
        check(cancelR.width > 0 && okR.width > 0, "Confirm buttons must be visible");
        check(cancelR.right <= okR.left + eps || cancelR.bottom <= okR.top + eps, "Confirm buttons overlap on " + width + "px");
        check(okR.right <= cCardR.right + eps && cancelR.left >= cCardR.left - eps, "Confirm buttons clip outside card on " + width + "px");

        if (mobile) {
          check(Math.abs(cCardR.width - width) <= eps, "Confirm bottom sheet width must fill viewport on " + width + "px");
          check(Math.abs(cCardR.bottom - height) <= eps, "Confirm bottom sheet must touch bottom on " + width + "px");
        } else {
          check(cCardR.width <= 401.5, "Desktop confirm card must be compact (<= 400px), got " + cCardR.width);
          const centerX = (cCardR.left + cCardR.right) / 2;
          const centerY = (cCardR.top + cCardR.bottom) / 2;
          check(Math.abs(centerX - width / 2) <= 2.5, "Desktop confirm card not centered horizontally");
          check(Math.abs(centerY - height / 2) <= 2.5, "Desktop confirm card not centered vertically");
        }

        // 3. Real Escape key closes ONLY #agyConfirmModal, keeping #handoffNewTabModal open
        const escEvt = new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
        d.dispatchEvent(escEvt);
        const res = await confirmPromise;
        check(res === false, "Escape must resolve AgyUI.confirm with false");
        check(confirmModal.classList.contains("hidden"), "#agyConfirmModal must close on Escape");
        check(!handoffModal.classList.contains("hidden"), "Parent #handoffNewTabModal must remain open after confirm Escape");

        frame.remove();
        resolve({ width, height, viewport: width + "x" + height, zConfirm, zHandoff, confirmWidth: Math.round(cCardR.width) });
      } catch (err) {
        frame.remove();
        reject(new Error(width + "x" + height + ": " + err.message));
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


def test_handoff_and_confirm_geometry_in_chromium(tmp_path: Path) -> None:
    report = run_workspace_chromium(tmp_path, CHROMIUM_V150_GEOMETRY_CHECKS)
    assert len(report) == 3
    assert [row["viewport"] for row in report] == ["1440x900", "390x844", "320x568"]
