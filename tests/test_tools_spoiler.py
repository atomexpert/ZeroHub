"""
Unit & integration tests for Antigravity Hub v=74:
- Compact collapsed-by-default `<details class="tools-spoiler">` for `msg.tool_calls`
  during both `ACTIVE` streaming and `DONE` states.
- Live spinner + human-readable step preview in `.tools-summary` without long workspace roots,
  plus `compactToolHintPath` compact tails & full path in `.tool-pill[title]`.
- Categorized summary breakdown when all steps finish, internal `.tools-spoiler-body` scroll,
  and preservation of manual `open` + `data-user-open="1"` + `scrollTop` / `wasNearBottom`
  across live `updateSingleAssistantDom(msg)` updates.
- `COPY_STRIP_SELECTOR` clipboard hygiene, mobile `#stopBtn` (`display: none` on `span` at `<=680px`),
  and cache version sync (`v>=74`) across `app/static/sw.js` and `app/static/index.html`.
- Strictly 0% backend / live DB touch (`data/hub.db` verified byte-identical).
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
APP_JS_PATH = ROOT_DIR / "app" / "static" / "app.js"
STYLES_CSS_PATH = ROOT_DIR / "app" / "static" / "styles.css"
INDEX_HTML_PATH = ROOT_DIR / "app" / "static" / "index.html"
SW_JS_PATH = ROOT_DIR / "app" / "static" / "sw.js"
LIVE_DB_PATH = ROOT_DIR / "data" / "hub.db"


NODE_HARNESS_SCRIPT = r"""
const fs = require("fs");
const vm = require("vm");

const appJsPath = process.env.AGY_APP_JS_PATH || process.argv[1];
let src = fs.readFileSync(appJsPath, "utf8");

const tailTarget = /init\(\);\s*\}\)\(\);\s*$/;
if (!tailTarget.test(src)) {
  throw new Error("Could not locate IIFE tail init() in app.js");
}
src = src.replace(
  tailTarget,
  `globalThis.__AGY_TEST__ = {
    compactToolHintPath,
    extractToolPathBasename,
    buildToolsHtml,
    buildThinkingHtml,
    buildAssistantInnerHtml,
    updateSingleAssistantDom,
    COPY_STRIP_SELECTOR,
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

function makeDummyEl(tag = "DIV") {
  return {
    tagName: tag,
    innerHTML: "",
    textContent: "",
    value: "",
    style: {},
    dataset: {},
    classList: makeClassList(),
    addEventListener: () => {},
    removeEventListener: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild: () => {},
    setAttribute: () => {},
    getAttribute: () => null,
  };
}

const docStub = {
  documentElement: makeDummyEl("HTML"),
  body: makeDummyEl("BODY"),
  getElementById: () => makeDummyEl("DIV"),
  querySelector: () => makeDummyEl("DIV"),
  querySelectorAll: () => [],
  createElement: (t) => makeDummyEl(String(t || "DIV").toUpperCase()),
  addEventListener: () => {},
};

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  URL,
  URLSearchParams,
  localStorage: localStorageStub,
  navigator: { userAgent: "node-test", clipboard: {} },
  location: { protocol: "http:", host: "127.0.0.1:8765", origin: "http://127.0.0.1:8765", pathname: "/" },
  CSS: { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&") },
  matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  requestAnimationFrame: (fn) => fn(),
  cancelAnimationFrame: () => {},
  document: docStub,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });

const api = sandbox.__AGY_TEST__;

// 1. Path compaction tests
const winCompact = api.compactToolHintPath(
  "D:\\Internet\\Antigravity\\Antigravity_Hub\\app\\static\\styles.css"
);
const sbxCompact = api.compactToolHintPath("/workspace/app/static/styles.css");
const cmdUntouched = api.compactToolHintPath("pytest tests/test_tools_spoiler.py -q");

// 2. Active tool_calls (step 5 reading styles.css)
const activeCalls = [
  { name: "read_file", parameters: { Path: "D:\\Internet\\Antigravity\\Antigravity_Hub\\app\\static\\index.html" }, state: "DONE" },
  { name: "grep_search", parameters: { Query: "buildToolsHtml" }, state: "DONE" },
  { name: "edit_file", parameters: { Path: "D:\\Internet\\Antigravity\\Antigravity_Hub\\app\\static\\app.js" }, state: "DONE" },
  { name: "call_mcp_tool", parameters: { ServerName: "hub_sandbox", ToolName: "sandbox_exec", Arguments: { command: "node --check app/static/app.js" } }, state: "DONE" },
  { name: "read_file", parameters: { Path: "D:\\Internet\\Antigravity\\Antigravity_Hub\\app\\static\\styles.css" }, state: "ACTIVE" },
];
const activeHtml = api.buildToolsHtml(activeCalls);

// Active Docker step 12
const dockerActiveCalls = Array.from({ length: 11 }, (_, i) => ({
  name: "read_file",
  parameters: { Path: `/workspace/app/file_${i}.py` },
  state: "DONE",
})).concat([
  {
    name: "call_mcp_tool",
    parameters: { ServerName: "hub_sandbox", ToolName: "sandbox_exec", Arguments: { command: "pytest -q" } },
    state: "ACTIVE",
  },
]);
const dockerActiveHtml = api.buildToolsHtml(dockerActiveCalls);

// 3. Completed tool_calls (11 reads, 2 edits, 1 Docker = 14 total)
const doneCalls = [
  ...Array.from({ length: 11 }, (_, i) => ({
    name: i === 10 ? "list_dir" : "read_file",
    parameters: { Path: `D:\\Internet\\Antigravity\\Antigravity_Hub\\app\\mod_${i}.py` },
    state: "DONE",
  })),
  { name: "edit_file", parameters: { Path: "D:\\Internet\\Antigravity\\Antigravity_Hub\\app\\static\\app.js" }, state: "DONE" },
  { name: "write_file", parameters: { Path: "/workspace/tests/test_tools_spoiler.py" }, state: "DONE" },
  { name: "sandbox_exec", parameters: { command: "pytest" }, state: "DONE" },
];
const doneHtml = api.buildToolsHtml(doneCalls);

// 4. buildAssistantInnerHtml openState compatibility
api.state.activeChatId = "chat-1";
api.state.activeChat = { id: "chat-1", kind: "project" };
const sampleMsg = {
  id: 42,
  chat_id: "chat-1",
  role: "assistant",
  status: "streaming",
  content: "Working on fix...",
  thinking: "Checking layout",
  has_thinking: true,
  _thinkingActive: false,
  tool_calls: doneCalls,
};
const innerDefault = api.buildAssistantInnerHtml(sampleMsg);
const innerLegacyThinkOpen = api.buildAssistantInnerHtml(sampleMsg, true);
const innerToolsOpen = api.buildAssistantInnerHtml(sampleMsg, {
  thinkingOpen: false,
  toolsOpen: true,
});

// 5. Simulate updateSingleAssistantDom preserving open + scrollTop / wasNearBottom
api.state.messages = [sampleMsg];

function buildSimulatedRow(isUserOpen, scrollTopVal, scrollHeightVal, clientHeightVal) {
  let currentCardHtml = api.buildAssistantInnerHtml(sampleMsg, {
    thinkingOpen: false,
    toolsOpen: isUserOpen,
  });
  const nextBodyObj = {
    scrollTop: 0,
    scrollHeight: 680,
    clientHeight: clientHeightVal,
  };
  const prevBodyObj = {
    scrollTop: scrollTopVal,
    scrollHeight: scrollHeightVal,
    clientHeight: clientHeightVal,
  };
  const existingToolsEl = {
    open: isUserOpen,
    dataset: { userOpen: isUserOpen ? "1" : "" },
    querySelector: (sel) => (sel === ".tools-spoiler-body" ? prevBodyObj : null),
  };
  const cardEl = {
    get innerHTML() {
      return currentCardHtml;
    },
    set innerHTML(v) {
      currentCardHtml = v;
    },
    querySelector: (sel) =>
      sel === "details.tools-spoiler .tools-spoiler-body" ? nextBodyObj : null,
  };
  const rowEl = {
    dataset: { msgId: "42" },
    classList: makeClassList(),
    insertBefore: () => {},
    querySelector: (sel) => {
      if (sel === "details.thinking-box") return null;
      if (sel === "details.tools-spoiler") return existingToolsEl;
      if (sel === ".assistant-card") return cardEl;
      return null;
    },
  };
  return { rowEl, cardEl, nextBodyObj };
}

// Case A: Closed by user -> stays closed after live update
const simClosed = buildSimulatedRow(false, 0, 240, 240);
api.els.messagesList.querySelector = () => simClosed.rowEl;
api.updateSingleAssistantDom(sampleMsg);

// Case B: Opened by user & scrolled up (scrollTop=75, scrollHeight=520, clientHeight=240) -> preserves scrollTop=75
const simScrolledUp = buildSimulatedRow(true, 75, 520, 240);
api.els.messagesList.querySelector = () => simScrolledUp.rowEl;
api.updateSingleAssistantDom(sampleMsg);

// Case C: Opened by user & near bottom (scrollTop=270, scrollHeight=500, clientHeight=240 -> gap=10 <= 24) -> follows nextBody.scrollHeight (680)
const simNearBottom = buildSimulatedRow(true, 270, 500, 240);
api.els.messagesList.querySelector = () => simNearBottom.rowEl;
api.updateSingleAssistantDom(sampleMsg);

process.stdout.write(
  JSON.stringify({
    winCompact,
    sbxCompact,
    cmdUntouched,
    activeHtml,
    dockerActiveHtml,
    doneHtml,
    innerDefault,
    innerLegacyThinkOpen,
    innerToolsOpen,
    simClosedHtml: simClosed.cardEl.innerHTML,
    simScrolledUpHtml: simScrolledUp.cardEl.innerHTML,
    simScrolledUpScrollTop: simScrolledUp.nextBodyObj.scrollTop,
    simNearBottomScrollTop: simNearBottom.nextBodyObj.scrollTop,
    copyStripSelector: api.COPY_STRIP_SELECTOR,
  })
);
"""


class TestToolsSpoilerV74(unittest.TestCase):
    def setUp(self) -> None:
        self._live_before = (
            (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
            if LIVE_DB_PATH.exists()
            else None
        )

    def tearDown(self) -> None:
        if self._live_before is not None:
            live_after = (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
            self.assertEqual(
                self._live_before,
                live_after,
                "Live data/hub.db must NEVER be touched!",
            )

    def test_js_syntax_check(self) -> None:
        for js_file in (APP_JS_PATH, SW_JS_PATH):
            proc = subprocess.run(
                ["node", "--check", str(js_file)],
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"node --check failed for {js_file.name}: {proc.stderr}",
            )

    def test_tools_spoiler_rendering_and_streaming_state(self) -> None:
        import os

        env = dict(os.environ, AGY_APP_JS_PATH=str(APP_JS_PATH))
        proc = subprocess.run(
            ["node", "-e", NODE_HARNESS_SCRIPT],
            cwd=str(ROOT_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
        )
        self.assertEqual(proc.returncode, 0, f"Node harness failed: {proc.stderr}")
        data = json.loads(proc.stdout)

        # Path compaction helper
        self.assertEqual(data["winCompact"], "app/static/styles.css")
        self.assertEqual(data["sbxCompact"], "app/static/styles.css")
        self.assertEqual(data["cmdUntouched"], "pytest tests/test_tools_spoiler.py -q")

        # 1. Active tool_calls: closed by default, live spinner + compact active preview
        active_html = data["activeHtml"]
        m_active_tag = re.search(r"<details\b[^>]*class=\"tools-spoiler[^\"]*\"[^>]*>", active_html)
        self.assertIsNotNone(m_active_tag)
        self.assertIn("is-running", m_active_tag.group(0))
        self.assertNotRegex(m_active_tag.group(0), r"\bopen\b")
        self.assertIn('class="tools-live-spinner"', active_html)
        self.assertIn("Выполняется шаг 5 · Чтение styles.css", active_html)
        self.assertIn('<span class="tools-count-badge is-active">5</span>', active_html)
        # Full original path is ONLY in title="...", never leaked into summary or pill inner text
        self.assertIn(
            'title="D:\\Internet\\Antigravity\\Antigravity_Hub\\app\\static\\styles.css"',
            active_html,
        )
        pill_texts = re.findall(r'<span class="tool-pill-text">(.*?)</span>', active_html)
        self.assertEqual(len(pill_texts), 5)
        for pt in pill_texts:
            self.assertNotIn("D:\\Internet\\Antigravity\\Antigravity_Hub", pt)
        self.assertTrue(any("app/static/styles.css" in pt for pt in pill_texts))

        # Active Docker step 12
        docker_html = data["dockerActiveHtml"]
        self.assertIn("Выполняется шаг 12 · 🐳 Docker · терминал", docker_html)
        self.assertIn('<span class="tools-count-badge is-active">12</span>', docker_html)

        # 2. Done tool_calls: closed by default, total count + categorized summary
        done_html = data["doneHtml"]
        m_done_tag = re.search(r"<details\b[^>]*class=\"tools-spoiler[^\"]*\"[^>]*>", done_html)
        self.assertIsNotNone(m_done_tag)
        self.assertIn("is-done", m_done_tag.group(0))
        self.assertNotRegex(m_done_tag.group(0), r"\bopen\b")
        self.assertNotIn('class="tools-live-spinner"', done_html)
        self.assertIn("Технические шаги", done_html)
        self.assertIn("чтение · 11, правка · 2, Docker · 1", done_html)
        self.assertIn('<span class="tools-count-badge">14</span>', done_html)
        self.assertIn('<div class="tools-spoiler-body">', done_html)

        # 3. buildAssistantInnerHtml openState backward compatibility + toolsOpen preservation
        self.assertNotRegex(
            re.search(r"<details\b[^>]*tools-spoiler[^>]*>", data["innerDefault"]).group(0),
            r"\bopen\b",
        )
        # Legacy boolean `true` opens thinking-box ONLY, keeping tools-spoiler closed
        self.assertRegex(
            re.search(r"<details\b[^>]*thinking-box[^>]*>", data["innerLegacyThinkOpen"]).group(0),
            r"\bopen\b",
        )
        self.assertNotRegex(
            re.search(r"<details\b[^>]*tools-spoiler[^>]*>", data["innerLegacyThinkOpen"]).group(0),
            r"\bopen\b",
        )
        # Object `{ thinkingOpen: false, toolsOpen: true }` preserves open + data-user-open="1"
        tools_open_tag = re.search(
            r"<details\b[^>]*tools-spoiler[^>]*>", data["innerToolsOpen"]
        ).group(0)
        self.assertRegex(tools_open_tag, r"\bopen\b")
        self.assertIn('data-user-open="1"', tools_open_tag)

        # 4. updateSingleAssistantDom streaming updates preserve open + scrollTop / auto-follow
        self.assertNotRegex(
            re.search(r"<details\b[^>]*tools-spoiler[^>]*>", data["simClosedHtml"]).group(0),
            r"\bopen\b",
        )
        sim_open_tag = re.search(
            r"<details\b[^>]*tools-spoiler[^>]*>", data["simScrolledUpHtml"]
        ).group(0)
        self.assertRegex(sim_open_tag, r"\bopen\b")
        self.assertIn('data-user-open="1"', sim_open_tag)
        self.assertEqual(data["simScrolledUpScrollTop"], 75)
        self.assertEqual(data["simNearBottomScrollTop"], 680)

        # 5. Clipboard serializer strips tools-spoiler elements
        strip_sel = data["copyStripSelector"]
        for required_sel in ("details.tools-spoiler", ".tools-summary", ".tools-spoiler-body"):
            self.assertIn(required_sel, strip_sel)

    def test_css_mobile_stop_btn_spoiler_scroll_and_cache_version(self) -> None:
        css = STYLES_CSS_PATH.read_text(encoding="utf-8")
        index_html = INDEX_HTML_PATH.read_text(encoding="utf-8")
        sw_js = SW_JS_PATH.read_text(encoding="utf-8")

        # .tools-spoiler-body desktop max-height & overscroll-behavior
        m_body = re.search(r"(?m)^\.tools-spoiler-body\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m_body)
        self.assertIn("max-height: 240px", m_body.group(1))
        self.assertIn("overflow-y: auto", m_body.group(1))
        self.assertIn("overscroll-behavior: contain", m_body.group(1))

        # Touch-selection isolation
        self.assertIn("details.tools-spoiler:not([open])", css)
        self.assertIn(".tools-summary", css)

        # Mobile @media (max-width: 680px): .tools-spoiler-body max-height: 200px & .stop-btn span { display: none; }
        media_680_blocks = [
            m.group(1)
            for m in re.finditer(
                r"@media\s*\(\s*max-width:\s*680px\s*\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}",
                css,
            )
        ]
        combined_680 = "\n".join(media_680_blocks)
        self.assertRegex(combined_680, r"\.tools-spoiler-body\s*\{[^}]*max-height:\s*200px")
        self.assertRegex(combined_680, r"\.stop-btn\s*\{[^}]*width:\s*3[4-8]px")
        self.assertRegex(combined_680, r"\.stop-btn\s*\{[^}]*padding:\s*0")
        self.assertRegex(combined_680, r"\.stop-btn\s*\{[^}]*justify-content:\s*center")
        self.assertRegex(combined_680, r"\.stop-btn\s+span\s*\{\s*display:\s*none\s*;?\s*\}")

        # Toggle listener in app.js handles tools-spoiler
        app_js = APP_JS_PATH.read_text(encoding="utf-8")
        self.assertIn('details.classList.contains("tools-spoiler")', app_js)

        # Cache version >= 74 & synchronized across sw.js and index.html
        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        self.assertIsNotNone(m_sw)
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 74)
        self.assertIn(f"/static/styles.css?v={ver}", sw_js)
        self.assertIn(f"/static/app.js?v={ver}", sw_js)
        self.assertIn(f"/static/styles.css?v={ver}", index_html)
        self.assertIn(f"/static/app.js?v={ver}", index_html)


if __name__ == "__main__":
    unittest.main()
