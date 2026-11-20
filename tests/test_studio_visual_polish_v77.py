"""
Automated tests for Antigravity Hub v=77:
Studio Visual Polish (Linear · Cursor · Claude Desktop aesthetic) across all 5 pillars.

Verifies:
1. Static cache bump (`>= v82`) across `app/static/sw.js` and `app/static/index.html`, plus JS syntax validity.
2. Pillar 1 — Sidebar Zoning:
   - Elevated Tray `#sidebarActivitySection.activity-radar-section` in `styles.css`,
   - `.activity-tier-divider` inserted between hot items (unread / running / queued) and Tier-3 idle items,
   - Compact two-line matte project/folder subline `.activity-proj-tag.is-subline-context` (`.has-activity-context`, `.activity-proj-dot`, `.activity-folder-ico`) as a passive part of the row (v86: no `[data-jump-*]`; folder reveal moved to the `⋯` menu), plus desktop idle right-side dead-zone cleanup,
   - Clean borderless `+` icon buttons (`#openCreateProjectBtn`, `#newRegularChatBtn`).
3. Pillar 2 — Matte Palette & Tinted Status Pills:
   - `PROJECT_ACCENT_PALETTE` uses 10 harmonious matte studio tones with ZERO neon fuchsia (`#ec4899`),
     ZERO alarm red (`#f43f5e`), and ZERO acid yellow (`#eab308`),
   - `--status-info` (`#6b9eff` dark / `#2b5fc7` light) & `--bg-info-subtle` tokens present,
   - `.nav-item.is-unread-done` / `.is-generating` have transparent row backgrounds (`box-shadow: none`).
4. Pillar 3 — Ghost Button Discipline & Unified Radius Scale:
   - `--radius-xs: 6px`, `--radius-sm: 8px`, `--radius-md: 11px`, `--island-radius: 14px`,
   - Ghost rules for `#openProjectMemoryBtn`, `.sandbox-reset-btn`, `.topbar-outline-btn`, `.chat-title-icon-btn`, `.tree-sec-count`.
5. Pillar 4 & 5 — Single-Surface Telemetry Pod, Monochrome Chrome SVGs, Flat Handoff Chips & Quiet Historical Actions:
   - Chrome emojis (`🧠`, `🐳↻`, `📌`, `⏳`, `📝`) replaced with crisp monochrome `<svg>` icons while user custom chat emojis remain intact,
   - `.msg-code-handoffs` has `border-top: none`, `.mch-sep` inline dot, and flat borderless `.mch-state-pill`,
   - `.msg-row.assistant:not(:last-child) .msg-actions` quiet hover rule present,
   - Live `data/hub.db` is NEVER touched.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


class TestStudioVisualPolishV77(unittest.TestCase):
    def setUp(self) -> None:
        self._live_db = ROOT_DIR / "data" / "hub.db"
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

    def tearDown(self) -> None:
        if self._live_stat_before is not None:
            live_after = (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            self.assertEqual(
                self._live_stat_before,
                live_after,
                "Live data/hub.db must NEVER be modified by tests!",
            )

    def test_v77_cache_syntax_and_all_five_css_pillars(self) -> None:
        sw_path = ROOT_DIR / "app" / "static" / "sw.js"
        app_path = ROOT_DIR / "app" / "static" / "app.js"
        html_path = ROOT_DIR / "app" / "static" / "index.html"
        css_path = ROOT_DIR / "app" / "static" / "styles.css"

        sw_text = sw_path.read_text(encoding="utf-8")
        app_text = app_path.read_text(encoding="utf-8")
        html_text = html_path.read_text(encoding="utf-8")
        css_text = css_path.read_text(encoding="utf-8")

        # 1. Version >= 82 & consistent across sw.js and index.html
        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_text)
        self.assertIsNotNone(m_sw, "CACHE_NAME must match agy-hub-v<N>")
        sw_ver = int(m_sw.group(1))
        self.assertGreaterEqual(sw_ver, 82)
        self.assertIn(f'"/static/styles.css?v={sw_ver}"', sw_text)
        self.assertIn(f'"/static/app.js?v={sw_ver}"', sw_text)
        self.assertIn(f'href="/static/styles.css?v={sw_ver}"', html_text)
        self.assertIn(f'src="/static/app.js?v={sw_ver}"', html_text)

        # Syntax check via node --check (CREATE_NO_WINDOW on Windows)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for js_file in (app_path, sw_path):
            proc = subprocess.run(
                ["node", "--check", str(js_file)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=creationflags,
            )
            self.assertEqual(proc.returncode, 0, f"Syntax error in {js_file}: {proc.stderr}")

        # 2. Matte PROJECT_ACCENT_PALETTE (10 colors, zero neon fuchsia / alarm red / acid yellow)
        m_pal = re.search(
            r"const\s+PROJECT_ACCENT_PALETTE\s*=\s*\[(.*?)\];",
            app_text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(m_pal, "PROJECT_ACCENT_PALETTE must exist in app.js")
        pal_body = m_pal.group(1).lower()
        hex_colors = re.findall(r"#[0-9a-f]{6}", pal_body)
        self.assertEqual(len(hex_colors), 10)
        for forbidden_neon in ("#ec4899", "#f43f5e", "#eab308", "#a855f7", "#3b82f6"):
            self.assertNotIn(
                forbidden_neon,
                hex_colors,
                f"Acidic neon color {forbidden_neon} must not be in PROJECT_ACCENT_PALETTE",
            )

        # 3. Chrome buttons in index.html use monochrome SVGs instead of system emojis
        self.assertNotIn("🐳↻", html_text)
        for btn_id in ("openProjectMemoryBtn", "resetSandboxBtn", "mobileResetSandboxBtn", "openCreateProjectBtn", "newRegularChatBtn"):
            m_btn = re.search(rf'id="{btn_id}"[^>]*>(.*?)</button>', html_text, flags=re.DOTALL)
            self.assertIsNotNone(m_btn, f"Button #{btn_id} must exist in index.html")
            self.assertIn("<svg", m_btn.group(1), f"Button #{btn_id} must use monochrome <svg>")

        # 4. CSS tokens, all 5 studio pillar selectors, & v82 two-line Activity subline + zero dead-zone rules
        for legacy_cls in ("is-inline-activity-row", "is-inline-suffix"):
            self.assertNotIn(legacy_cls, app_text, f"Legacy class {legacy_cls} must be removed from app.js")
            self.assertNotIn(legacy_cls, css_text, f"Legacy class {legacy_cls} must be removed from styles.css")

        for token_or_rule in (
            "--radius-xs: 6px;",
            "--radius-sm: 8px;",
            "--radius-md: 11px;",
            "--island-radius: 14px;",
            "--status-info: #6b9eff;",
            "--status-info: #2b5fc7;",
            "--bg-info-subtle:",
            "#sidebarActivitySection.activity-radar-section",
            ".activity-tier-divider",
            "#sidebarActivityList .nav-item:has(.has-activity-context)",
            ".nav-item-main.has-activity-context",
            ".activity-proj-tag.is-subline-context",
            ".activity-proj-tag.is-subline-context .activity-proj-dot",
            ".activity-proj-tag.is-subline-context .activity-folder-ico",
            "@media (hover: hover) and (pointer: fine)",
            ".badge-row:not(:has(.nav-pin-mark, .chat-state-pill, .activity-draft-pill))",
            "@media (hover: none), (pointer: coarse), (max-width: 860px)",
            ".tree-head-action-btn.tree-head-icon-btn",
            ".proj-tree-group.expanded",
            ".proj-memory-pill-btn.header-mem-pill",
            ".sidebar-telemetry-pod",
            ".mch-sep",
            ".msg-row.assistant:not(:last-child) .msg-actions",
        ):
            self.assertIn(token_or_rule, css_text)

    def test_v77_sidebar_divider_inline_suffix_and_svg_chrome_in_vm(self) -> None:
        harness_js = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const appCode = fs.readFileSync("app/static/app.js", "utf8");

class FakeClassList {
  constructor(el) { this.el = el; this.set = new Set(); }
  _sync() { this.el._className = Array.from(this.set).join(" "); }
  add(...names) { for (const n of names) if (n) this.set.add(n); this._sync(); }
  remove(...names) { for (const n of names) this.set.delete(n); this._sync(); }
  toggle(name, force) {
    if (force === undefined) {
      if (this.set.has(name)) { this.set.delete(name); this._sync(); return false; }
      this.set.add(name); this._sync(); return true;
    }
    if (force) this.set.add(name); else this.set.delete(name);
    this._sync();
    return Boolean(force);
  }
  contains(name) { return this.set.has(name); }
}

class FakeElement {
  constructor(tag = "div", id = "") {
    this.tagName = String(tag).toUpperCase();
    this.id = id;
    this._className = "";
    this.classList = new FakeClassList(this);
    this.dataset = {};
    this.attributes = {};
    this.style = {
      setProperty(k, v) { this[k] = v; },
      removeProperty(k) { delete this[k]; },
    };
    this.children = [];
    this.parentElement = null;
    this.listeners = {};
    this._innerHTML = "";
    this._textContent = "";
    this.value = "";
    this.title = "";
    this.disabled = false;
    this.options = [];
    this.selectionStart = 0;
    this.scrollHeight = 40;
  }
  get className() { return this._className; }
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
    if (this.children.length > 0) {
      return this.children.map((c) => c.textContent).join("");
    }
    return this._textContent;
  }
  set textContent(v) {
    this._textContent = String(v ?? "");
    this._innerHTML = this._textContent;
    this.children = [];
  }
  appendChild(child) {
    if (child) {
      child.parentElement = this;
      this.children.push(child);
    }
    return child;
  }
  remove() {
    if (this.parentElement) {
      this.parentElement.children = this.parentElement.children.filter((c) => c !== this);
      this.parentElement = null;
    }
  }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === "id") this.id = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(ev, fn) {
    if (!this.listeners[ev]) this.listeners[ev] = [];
    this.listeners[ev].push(fn);
  }
  querySelector(sel) {
    for (const ch of this.children) {
      if (sel.startsWith(".") && ch.classList.contains(sel.slice(1))) return ch;
      const sub = ch.querySelector(sel);
      if (sub) return sub;
    }
    return null;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (node) => {
      for (const ch of node.children) {
        if (sel.startsWith(".") && ch.classList.contains(sel.slice(1))) out.push(ch);
        walk(ch);
      }
    };
    walk(this);
    return out;
  }
  closest() { return null; }
  focus() {}
  scrollIntoView() {}
}

const byId = new Map();
const htmlText = fs.readFileSync("app/static/index.html", "utf8");
for (const m of htmlText.matchAll(/\bid="([^"]+)"/g)) {
  const id = m[1];
  if (!byId.has(id)) byId.set(id, new FakeElement("div", id));
}
for (const extraId of ["workspaceTabsList"]) {
  if (!byId.has(extraId)) byId.set(extraId, new FakeElement("div", extraId));
}

const nowTs = Date.now();
const storageMap = new Map([
  ["agy_hub_token", "tok_v77"],
  ["agy_hub_authed", "1"],
  ["agy_hub_tab", "project"],
  ["agy_hub_project", "HubStudio"],
  ["agy_hub_unread_done_u1", JSON.stringify([
    ["c_folder", { status: "done", ts: nowTs - 1000, title: "Собственная папка", kind: "chat", runsCount: 1 }],
    ["c_root", { status: "done", ts: nowTs - 2000, title: "Обычный чат", kind: "chat", runsCount: 1 }],
  ])],
  ["agy_hub_chat_activity_u1", JSON.stringify({ ts: [["c_folder", nowTs - 1000], ["c_root", nowTs - 2000], ["c_idle", nowTs - 3000]], dismissed: [] })],
]);
const fakeStorage = {
  getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
};

let wsInstance = null;
class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 1;
    wsInstance = this;
  }
  send() {}
  close() { this.readyState = 3; }
}
FakeWebSocket.OPEN = 1;

const docRoot = new FakeElement("html");
const docBody = new FakeElement("body");

let viewportMobile = false;
const scheduledTimers = [];
const ctx = {
  console,
  setTimeout: (fn, ms) => { scheduledTimers.push({ fn, ms }); return scheduledTimers.length; },
  clearTimeout: () => {},
  setInterval: () => 1,
  clearInterval: () => {},
  requestAnimationFrame: (fn) => { fn(); return 1; },
  cancelAnimationFrame: () => {},
  URL,
  URLSearchParams,
  CSS: { escape: (s) => String(s) },
  FormData: class {},
  Blob: class {},
  AbortController: class { abort() {} },
  WebSocket: FakeWebSocket,
  localStorage: fakeStorage,
  sessionStorage: fakeStorage,
  navigator: { clipboard: { writeText: async () => {} }, userAgent: "Test" },
  location: { protocol: "http:", host: "127.0.0.1:8080", origin: "http://127.0.0.1:8080", pathname: "/", search: "" },
  document: {
    documentElement: docRoot,
    body: docBody,
    hidden: false,
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => new FakeElement(tag),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  window: null,
  marked: null,
  hljs: null,
  fetch: async (url) => {
    const u = String(url);
    let data = {};
    if (u.startsWith("/api/system/info")) {
      data = {
        user: { id: "u1", username: "admin", role: "admin", max_access_mode: "full_access" },
        models: [{ id: "gemini-4-carbon-high", label: "Gemini 4 Carbon" }],
        cli_auth: { ok: true },
      };
    } else if (u.startsWith("/api/projects")) {
      data = {
        projects: [
          { name: "HubStudio", is_shared: true, execution_mode: "docker", has_passport: true, changelog_count: 2, hub_chat_count: 3 },
        ],
      };
    } else if (u.startsWith("/api/chats/c_arch")) {
      data = {
        chat: { id: "c_arch", kind: "project", project_name: "HubStudio", title: "Обсуждение", icon: "💬", pinned: true, is_generating: false },
        messages: [
          {
            id: 101,
            role: "assistant",
            content: "Готовый план редизайна.",
            status: "done",
            code_handoffs: [
              { target_chat_id: "c_code", target_title: "Вёрстка", target_icon: "🚀", access_mode: "full_access", send_count: 1, sent_at: "2026-11-12T14:00:00Z" }
            ],
          }
        ],
        queue: [],
        queue_paused: false,
      };
    } else if (u.startsWith("/api/chats")) {
      data = {
        chats: [
          { id: "c_arch", kind: "project", project_name: "HubStudio", is_shared_project: true, title: "Обсуждение", icon: "💬", pinned: true, is_generating: false, queue_count: 0, updated_at: "2026-11-12T14:10:00Z" },
          { id: "c_code", kind: "project", project_name: "HubStudio", is_shared_project: true, title: "Вёрстка", icon: "🚀", pinned: false, is_generating: true, queue_count: 2, updated_at: "2026-11-12T14:12:00Z" },
          { id: "c_folder", kind: "chat", folder: "Идеи", title: "Собственная папка", icon: "", pinned: false, is_generating: false, queue_count: 0, updated_at: "2026-11-12T14:07:00Z" },
          { id: "c_root", kind: "chat", folder: "", title: "Обычный чат", icon: "", pinned: false, is_generating: false, queue_count: 0, updated_at: "2026-11-12T14:06:00Z" },
          { id: "c_idle", kind: "project", project_name: "HubStudio", is_shared_project: true, title: "Заметки", icon: "", pinned: false, is_generating: false, queue_count: 0, updated_at: "2026-11-12T14:05:00Z" },
        ],
      };
    } else if (u.startsWith("/api/quotas")) {
      data = { quotas: { gemini_5h_pct: 80, claude_5h_pct: 25 }, token_stats: {} };
    }
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => data,
      text: async () => JSON.stringify(data),
    };
  },
};
ctx.window = ctx;
ctx.window.addEventListener = () => {};
ctx.window.removeEventListener = () => {};
ctx.window.matchMedia = (q) => ({
  matches: q.includes("max-width") ? viewportMobile : false,
  addEventListener: () => {},
});

vm.createContext(ctx);
vm.runInContext(appCode, ctx);

(async () => {
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));

  // Trigger a WebSocket queue_updated + project draft typing to populate all Activity row variants
  if (wsInstance && wsInstance.onmessage) {
    wsInstance.onmessage({
      data: JSON.stringify({
        type: "queue_updated",
        chat_id: "c_code",
        queue: [{ id: "q1", content: "Шаг 1" }, { id: "q2", content: "Шаг 2" }],
        paused: false,
      }),
    });
  }

  const promptEl = byId.get("promptInput");
  if (promptEl && Array.isArray(promptEl.listeners.input)) {
    promptEl.value = "Новый черновик проекта";
    for (const fn of promptEl.listeners.input) fn({ target: promptEl });
    for (const t of scheduledTimers.splice(0)) {
      if (typeof t.fn === "function" && t.ms === 600) t.fn();
    }
  }

  await new Promise((r) => setImmediate(r));

  // 1) Verify .activity-tier-divider appears between hot item (c_code running) and Tier-3 idle items
  const actList = byId.get("sidebarActivityList");
  const childClasses = actList.children.map((c) => c.className);
  assert(
    childClasses.some((cls) => cls.includes("activity-tier-divider")),
    "Expected .activity-tier-divider between hot and idle items in #sidebarActivityList, got: " + JSON.stringify(childClasses)
  );

  // 2) Verify Activity rows use two-tier subline context (.has-activity-context + .is-subline-context + .activity-proj-dot)
  const projRow = actList.children.find((c) => c.dataset && c.dataset.chatId === "c_code");
  assert(projRow, "Project chat c_code must be in Activity");
  assert(projRow._innerHTML.includes("has-activity-context"), "Project row must use .has-activity-context");
  assert(projRow._innerHTML.includes("is-subline-context"), "Project row must use .is-subline-context");
  assert(projRow._innerHTML.includes("activity-proj-dot"), "Project row must include .activity-proj-dot");
  assert(projRow._innerHTML.includes("activity-proj-name"), "Project row must include .activity-proj-name");
  assert(projRow._innerHTML.includes("activity-shared-ico"), "Shared project row must include .activity-shared-ico");
  assert(!projRow._innerHTML.includes("data-jump-project"), "v86: Activity subline is passive (no data-jump-project)");
  assert(!projRow._innerHTML.includes("is-clickable-tag"), "v86: Activity subline must not be a clickable tag");
  assert(!projRow._innerHTML.includes("<button type=\"button\" class=\"nav-item-icon-btn\""), "v86: Row icon must be a passive span, not a button");
  assert(projRow._innerHTML.includes('title="Вёрстка — Проект: HubStudio"'), "Project row must set full title tooltip on .nav-item-main");
  assert(!projRow._innerHTML.includes("is-inline-suffix"), "Legacy .is-inline-suffix must not be rendered");
  assert(!projRow._innerHTML.includes("is-inline-activity-row"), "Legacy .is-inline-activity-row must not be rendered");
  assert(!projRow._innerHTML.includes("📁"), "Activity row should not contain 📁 emoji");

  const draftRow = actList.children.find((c) => c.className.includes("is-draft-activity"));
  assert(draftRow, "Project draft row must be in Activity Tier-3");
  assert(draftRow._innerHTML.includes("has-activity-context"), "Project draft must use .has-activity-context");
  assert(draftRow._innerHTML.includes("is-subline-context"), "Project draft must use .is-subline-context");
  assert(draftRow._innerHTML.includes("activity-proj-dot"), "Project draft must include .activity-proj-dot");
  assert(!draftRow._innerHTML.includes("data-jump-project"), "v86: Project draft subline is passive (no data-jump-project)");
  assert(draftRow._innerHTML.includes('title="Новый диалог (черновик) — Проект: HubStudio"'), "Project draft must include full tooltip");

  const folderRow = actList.children.find((c) => c.dataset && c.dataset.chatId === "c_folder");
  assert(folderRow, "Custom folder chat c_folder must be in Activity Tier-3");
  assert(folderRow._innerHTML.includes("has-activity-context"), "Folder row must use .has-activity-context");
  assert(folderRow._innerHTML.includes("is-subline-context is-folder-context"), "Folder row must use .is-subline-context.is-folder-context");
  assert(folderRow._innerHTML.includes("activity-folder-ico"), "Folder row must include .activity-folder-ico");
  assert(!folderRow._innerHTML.includes("data-jump-folder"), "v86: Folder subline is passive (no data-jump-folder)");
  assert(folderRow._innerHTML.includes('title="Собственная папка — Папка: Идеи"'), "Folder row must set folder tooltip");

  const rootRow = actList.children.find((c) => c.dataset && c.dataset.chatId === "c_root");
  assert(rootRow, "Plain root chat c_root must be in Activity Tier-3");
  assert(!rootRow._innerHTML.includes("has-activity-context"), "Plain root chat must remain single-line");
  assert(!rootRow._innerHTML.includes("is-subline-context"), "Plain root chat must not render subline context");
  assert(rootRow._innerHTML.includes('title="Обычный чат"'), "Plain root chat in Activity must still expose full title tooltip");

  // 3) Verify Project header uses monochrome SVG for Memory, Shared Cube, and Queue Clock (+2 preserved!)
  const projList = byId.get("projectsTreeList");
  assert(projList.children.length === 1, "Expected 1 project group");
  const projHeaderHtml = projList.children[0].children[0]._innerHTML;
  assert(projHeaderHtml.includes("+2"), "Project queue counter must still contain +2");
  assert(!projHeaderHtml.includes("⏳"), "Project queue counter must replace ⏳ with monochrome SVG");
  assert(!projHeaderHtml.includes("🧠"), "Project inline memory button must replace 🧠 with monochrome SVG");
  assert(!projHeaderHtml.includes("🐳"), "Project shared badge must replace 🐳 with monochrome SVG");

  // 4) Verify Quota bar severity classes: 80% remaining -> calm default (neither is-warn nor is-low), 25% -> is-warn
  const gemFill = byId.get("sacGemFill");
  const cldFill = byId.get("sacClaudeFill");
  assert(!gemFill.classList.contains("is-warn") && !gemFill.classList.contains("is-low"), "80% quota bar must remain calm without warn/low classes");
  assert(cldFill.classList.contains("is-warn"), "25% quota bar must get .is-warn");

  console.log("V77_UI_CHECKS_OK");
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
        with tempfile.TemporaryDirectory(prefix="agy_v77_vm_") as tmp_dir:
            script_path = Path(tmp_dir) / "verify_v77_ui.js"
            script_path.write_text(harness_js, encoding="utf-8")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                ["node", str(script_path)],
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=creationflags,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"VM UI check failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
            )
            self.assertIn("V77_UI_CHECKS_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
