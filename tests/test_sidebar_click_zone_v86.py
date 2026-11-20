"""
Isolated static & Node VM tests for Antigravity Hub v=86:
«Единая зона клика по вкладкам в сайдбаре и перенос перехода к папке проекта в меню ⋯».

- Row icon `.nav-item-icon-btn` is a passive `<span aria-hidden>` (no icon-picker click hijack in sidebar rows).
- Activity subline `.activity-proj-tag.is-subline-context` is passive (no `is-clickable-tag`, no `[data-jump-*]`,
  no `stopPropagation` handlers) while preserving the visual structure.
- `#chatContextMenu` gains «Перейти в папку проекта» (`reveal_project`) / «Перейти в папку чата» (`reveal_folder`),
  wired to `revealProjectFolderInSidebar` / `revealCustomFolderInSidebar`, which expand a collapsed desktop sidebar,
  open the mobile drawer and clear an active sidebar search filter before revealing.
- `#activeChatIconBtn` in the chat header keeps the quick icon picker.
- CSS: `.nav-item-icon-btn` uses `cursor: inherit` without its own hover square; stale `.is-clickable-tag` hover rules removed.
- Cache version sync (`v>=86`). Never touches live `data/hub.db`; 0% GPU; no console windows.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["ORT_DISABLE_ALL_GPU"] = "1"

ROOT_DIR = Path(__file__).resolve().parent.parent
APP_JS_PATH = ROOT_DIR / "app" / "static" / "app.js"
CSS_PATH = ROOT_DIR / "app" / "static" / "styles.css"
INDEX_HTML_PATH = ROOT_DIR / "app" / "static" / "index.html"
SW_JS_PATH = ROOT_DIR / "app" / "static" / "sw.js"
LIVE_DB_PATH = ROOT_DIR / "data" / "hub.db"

CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


NODE_HARNESS_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");

let src = fs.readFileSync(process.env.AGY_APP_JS_PATH, "utf8");
const tailTarget = /init\(\);\s*\}\)\(\);\s*$/;
if (!tailTarget.test(src)) throw new Error("Could not locate IIFE tail init() in app.js");
src = src.replace(
  tailTarget,
  `globalThis.__AGY_TEST__ = {
    bindEvents,
    openChatContextMenuAtPoint,
    revealProjectFolderInSidebar,
    revealCustomFolderInSidebar,
    state,
    els,
  };
})();`
);

const storageMap = new Map();
const localStorageStub = {
  getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
};

function makeClassList() {
  const set = new Set();
  return {
    add: (...cls) => cls.forEach((c) => set.add(c)),
    remove: (...cls) => cls.forEach((c) => set.delete(c)),
    toggle: (c, force) => {
      if (force === undefined) {
        if (set.has(c)) { set.delete(c); return false; }
        set.add(c); return true;
      }
      if (force) set.add(c); else set.delete(c);
      return Boolean(force);
    },
    contains: (c) => set.has(c),
  };
}

function makeEl(id = "", tag = "DIV") {
  const listeners = new Map();
  const children = [];
  const el = {
    id, tagName: tag, innerHTML: "", textContent: "", value: "",
    style: { setProperty: () => {}, removeProperty: () => {} },
    dataset: {}, classList: makeClassList(),
    scrollTop: 0, scrollHeight: 600, clientHeight: 600, offsetWidth: 100,
    children, childNodes: children,
    addEventListener: (ev, fn) => {
      if (!listeners.has(ev)) listeners.set(ev, []);
      listeners.get(ev).push(fn);
    },
    removeEventListener: () => {},
    dispatch: (ev, extra = {}) => {
      const evt = Object.assign({ type: ev, target: el, preventDefault() {}, stopPropagation() {} }, extra);
      for (const fn of listeners.get(ev) || []) fn(evt);
      return evt;
    },
    scrollTo: () => {}, scrollIntoView: () => {},
    querySelector: () => null, querySelectorAll: () => [],
    appendChild: (c) => { children.push(c); return c; },
    replaceChildren: (...n) => { children.length = 0; children.push(...n); },
    insertBefore: (c) => c, remove: () => {}, closest: () => null,
    setAttribute: () => {}, getAttribute: () => null, removeAttribute: () => {},
    focus: () => {}, blur: () => {}, select: () => {}, setSelectionRange: () => {},
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 200, height: 300, bottom: 300, right: 200 }),
  };
  return el;
}

const byId = new Map();
const getElById = (id) => {
  if (!byId.has(id)) byId.set(id, makeEl(id, id === "searchInput" ? "INPUT" : "DIV"));
  return byId.get(id);
};

const docStub = {
  hidden: false,
  documentElement: makeEl("html", "HTML"),
  body: makeEl("body", "BODY"),
  activeElement: null,
  getElementById: getElById,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (t) => makeEl("", String(t || "DIV").toUpperCase()),
  createDocumentFragment: () => makeEl("", "FRAG"),
  addEventListener: () => {},
};

let mobile = false;
const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval, URL, URLSearchParams,
  localStorage: localStorageStub,
  performance: { now: () => Date.now() },
  ResizeObserver: class { observe() {} disconnect() {} },
  WebSocket: { OPEN: 1, CONNECTING: 0, CLOSING: 2, CLOSED: 3 },
  navigator: { userAgent: "node-test", clipboard: {} },
  location: { protocol: "http:", host: "127.0.0.1:8765", origin: "http://127.0.0.1:8765", pathname: "/" },
  CSS: { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&") },
  matchMedia: (q) => ({ matches: String(q).includes("860px") ? mobile : false, addEventListener: () => {} }),
  requestAnimationFrame: (fn) => { fn(); return 1; },
  cancelAnimationFrame: () => {},
  innerWidth: 1400, innerHeight: 900,
  document: docStub,
  addEventListener: () => {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const fetchCalls = [];
sandbox.fetch = async (url) => {
  fetchCalls.push(String(url));
  return {
    ok: true, status: 200,
    headers: { get: () => "application/json" },
    json: async () => ({ chats: [], folders: [] }),
    text: async () => "{}",
  };
};

vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });
const api = sandbox.__AGY_TEST__;
api.bindEvents();

(async () => {
  const menu = api.els.chatContextMenu;
  const projChat = { id: "c_proj", title: "Вёрстка", kind: "project", project_name: "HubStudio", icon: "" };
  const folderChat = { id: "c_fold", title: "Идея", kind: "chat", folder: "Идеи", icon: "🔥" };
  const rootChat = { id: "c_root", title: "Обычный", kind: "chat", folder: "", icon: "" };
  api.state.projects = [{ name: "HubStudio" }];

  api.openChatContextMenuAtPoint(projChat, 10, 10, []);
  const projMenuHtml = menu.innerHTML;
  api.openChatContextMenuAtPoint(folderChat, 10, 10, []);
  const folderMenuHtml = menu.innerHTML;
  api.openChatContextMenuAtPoint(rootChat, 10, 10, []);
  const rootMenuHtml = menu.innerHTML;

  // Desktop: collapsed sidebar + active search -> reveal_project via ⋯ menu click
  api.els.sidebar.classList.add("collapsed");
  api.state.searchQuery = "верст";
  api.els.searchInput.value = "верст";
  api.openChatContextMenuAtPoint(projChat, 10, 10, []);
  menu.dispatch("click", {
    target: { closest: (sel) => (sel === "[data-ctx-action]" ? { dataset: { ctxAction: "reveal_project" } } : null) },
  });
  await new Promise((r) => setTimeout(r, 30));
  const desktopRes = {
    collapsedAfter: api.els.sidebar.classList.contains("collapsed"),
    searchQueryAfter: api.state.searchQuery,
    searchInputAfter: api.els.searchInput.value,
    expanded: [...api.state.expandedProjects].some((p) => String(p).toLowerCase() === "hubstudio"),
    refetchedWithoutQuery: fetchCalls.some((u) => u.includes("/api/chats?query=") && u.endsWith("query=")),
  };

  // Mobile: reveal_folder opens drawer + uncollapses saved folder
  mobile = true;
  api.state.collapsedFolders["Идеи"] = true;
  api.openChatContextMenuAtPoint(folderChat, 10, 10, []);
  menu.dispatch("click", {
    target: { closest: (sel) => (sel === "[data-ctx-action]" ? { dataset: { ctxAction: "reveal_folder" } } : null) },
  });
  await new Promise((r) => setTimeout(r, 30));
  const mobileRes = {
    mobileOpen: api.els.sidebar.classList.contains("mobile-open"),
    backdropHidden: api.els.sidebarBackdrop.classList.contains("hidden"),
    folderCollapsed: Boolean(api.state.collapsedFolders["Идеи"]),
  };

  console.log(JSON.stringify({ projMenuHtml, folderMenuHtml, rootMenuHtml, desktopRes, mobileRes }));
})().catch((err) => { console.error(err); process.exit(1); });
"""


def _extract_function(src: str, name: str) -> str:
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"{name} not found"
    body = re.compile(r"\)\s*\{").search(src, m.end())
    assert body, f"{name} body not found"
    start = body.end() - 1
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[m.start() : i + 1]
    raise AssertionError(f"Unbalanced braces in {name}")


class TestSidebarClickZoneV86(unittest.TestCase):
    def test_static_markup_css_and_cache(self) -> None:
        app = APP_JS_PATH.read_text(encoding="utf-8")
        css = CSS_PATH.read_text(encoding="utf-8")
        sw = SW_JS_PATH.read_text(encoding="utf-8")
        html = INDEX_HTML_PATH.read_text(encoding="utf-8")

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw)
        self.assertIsNotNone(m_sw)
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 86)
        for needle in (f'"/static/styles.css?v={ver}"', f'"/static/app.js?v={ver}"'):
            self.assertIn(needle, sw)
        self.assertIn(f'href="/static/styles.css?v={ver}"', html)
        self.assertIn(f'src="/static/app.js?v={ver}"', html)

        for js_file in (APP_JS_PATH, SW_JS_PATH):
            proc = subprocess.run(
                ["node", "--check", str(js_file)],
                capture_output=True, text=True, encoding="utf-8", creationflags=CREATIONFLAGS,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

        chat_row = _extract_function(app, "createChatNavItem")
        draft_row = _extract_function(app, "createDraftActivityNavItem")
        for fn_src in (chat_row, draft_row):
            self.assertIn('<span class="nav-item-icon-btn" aria-hidden="true">', fn_src)
            self.assertNotIn('<button type="button" class="nav-item-icon-btn"', fn_src)
            self.assertNotIn("is-clickable-tag", fn_src)
            self.assertNotIn("data-jump-project", fn_src)
            self.assertNotIn("data-jump-folder", fn_src)
            self.assertNotIn("openIconPickerModal", fn_src)
            self.assertNotIn("revealProjectFolderInSidebar", fn_src)
            self.assertNotIn("revealCustomFolderInSidebar", fn_src)
            self.assertIn("activity-proj-tag is-subline-context", fn_src)
        self.assertIn("activity-folder-ico", chat_row)
        self.assertIn("activity-shared-ico", chat_row)
        self.assertIn("— Проект: ", chat_row)

        # Header icon button still opens the icon picker
        self.assertRegex(app, r"els\.activeChatIconBtn\.addEventListener\(\"click\"")

        # Context menu wiring
        menu_fn = _extract_function(app, "openChatContextMenuAtPoint")
        self.assertIn('action: "reveal_project"', menu_fn)
        self.assertIn('action: "reveal_folder"', menu_fn)
        self.assertLess(menu_fn.index("reveal_project"), menu_fn.index("project_memory"))
        self.assertIn('action === "reveal_project"', app)
        self.assertIn("revealProjectFolderInSidebar(chat.project_name)", app)
        self.assertIn('action === "reveal_folder"', app)
        self.assertIn("revealCustomFolderInSidebar(chat.folder)", app)

        # CSS: passive icon + no stale clickable-tag hover rules, base subline styles kept
        m_icon = re.search(r"\n\.nav-item-icon-btn\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m_icon)
        icon_body = m_icon.group(1)
        self.assertIn("cursor: inherit", icon_body)
        for prop in ("width: 24px", "height: 24px", "margin-left: -2px", "display: inline-flex", "flex-shrink: 0"):
            self.assertIn(prop, icon_body)
        self.assertNotRegex(css, r"\.nav-item-icon-btn:hover")
        self.assertNotIn("is-clickable-tag", css)
        self.assertRegex(css, r"\.activity-proj-tag\.is-subline-context\s*\{[^}]*font-size:\s*10\.8px")
        self.assertRegex(css, r"\.folder-group\.is-flash-highlight\s*>\s*\.folder-header-btn")

    def test_context_menu_reveal_actions_in_vm(self) -> None:
        db_before = (
            (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size) if LIVE_DB_PATH.exists() else None
        )
        env = dict(os.environ)
        env["AGY_APP_JS_PATH"] = str(APP_JS_PATH)
        proc = subprocess.run(
            ["node", "-e", NODE_HARNESS_SCRIPT],
            capture_output=True, text=True, encoding="utf-8", env=env, creationflags=CREATIONFLAGS,
        )
        self.assertEqual(proc.returncode, 0, f"Node harness failed:\n{proc.stderr}\n{proc.stdout}")
        res = json.loads(proc.stdout.strip().splitlines()[-1])

        self.assertIn('data-ctx-action="reveal_project"', res["projMenuHtml"])
        self.assertIn("Перейти в папку проекта", res["projMenuHtml"])
        self.assertIn('data-ctx-action="icon"', res["projMenuHtml"])
        self.assertNotIn("reveal_folder", res["projMenuHtml"])

        self.assertIn('data-ctx-action="reveal_folder"', res["folderMenuHtml"])
        self.assertIn("Перейти в папку чата", res["folderMenuHtml"])
        self.assertIn("Сменить значок…", res["folderMenuHtml"])

        self.assertNotIn("reveal_project", res["rootMenuHtml"])
        self.assertNotIn("reveal_folder", res["rootMenuHtml"])

        d = res["desktopRes"]
        self.assertFalse(d["collapsedAfter"], "Collapsed desktop sidebar must expand on reveal")
        self.assertEqual(d["searchQueryAfter"], "")
        self.assertEqual(d["searchInputAfter"], "")
        self.assertTrue(d["expanded"])
        self.assertTrue(d["refetchedWithoutQuery"], "Clearing the search must refetch the unfiltered list")

        mres = res["mobileRes"]
        self.assertTrue(mres["mobileOpen"])
        self.assertFalse(mres["backdropHidden"])
        self.assertFalse(mres["folderCollapsed"])

        db_after = (
            (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size) if LIVE_DB_PATH.exists() else None
        )
        self.assertEqual(db_before, db_after, "Live data/hub.db must remain untouched!")


if __name__ == "__main__":
    unittest.main()
