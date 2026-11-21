"""Audit v169 Stage 3: frontend z-index, Markdown URL safety, streaming perf, WS/logout/voice hygiene & PWA SW."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

from app.config import subprocess_hidden_kwargs
from test_frontend_contracts import STATIC, ShellParser, run_node
from test_ui_workspace_minimal_v142 import run_workspace_chromium

ROOT = Path(__file__).resolve().parents[1]


def _load_verify_hub():
    spec = importlib.util.spec_from_file_location("verify_hub_v169_s3", ROOT / "tools" / "verify_hub.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHROMIUM_CHECKS = r"""
const viewports = [[1440, 900], [768, 1024], [390, 844]];
const themes = ["dark", "light"];
function check(ok, message) {
  if (!ok) throw new Error(message);
}

function measureViewport(width, height, runBehaviorChecks) {
  return new Promise((resolve, reject) => {
    const frame = document.createElement("iframe");
    frame.style.cssText = "position:absolute;border:0;width:" + width + "px;height:" + height + "px";
    frame.onload = async () => {
      const w = frame.contentWindow;
      const d = frame.contentDocument;
      const get = id => d.getElementById(id);
      let profile = width + "x" + height;
      try {
        const api = w.__AGY_ADVISOR_API__;
        const checkedProfiles = [];
        const sidebar = get("sidebar");
        sidebar.style.transition = "none";
        const folders = get("foldersContainer");
        folders.classList.remove("hidden");
        folders.innerHTML =
          '<div class="folder-group"><div class="folder-header">' +
          '<button type="button" class="folder-header-btn"><span class="folder-chevron">▾</span>' +
          '<span class="folder-name">Work</span><span class="folder-count">1</span></button></div></div>';

        const tabs = [...get("settingsTabsBar").querySelectorAll("[data-settings-tab]")].map(b => b.dataset.settingsTab);
        check(JSON.stringify(tabs) === JSON.stringify(["appearance", "ai", "server", "account"]),
          "Unexpected settings tabs: " + JSON.stringify(tabs));

        const selToolbar = get("msgSelectionToolbar");
        const modals = [...d.querySelectorAll(".modal-backdrop")];
        check(modals.length >= 25, "Expected >= 25 modal backdrops, got " + modals.length);

        for (const theme of themes) {
          profile = width + "x" + height + "/" + theme;
          d.documentElement.dataset.theme = theme;
          d.documentElement.dataset.palette = "emerald";

          // 0 horizontal overflow on document root
          check(d.documentElement.scrollWidth <= w.innerWidth, "Document horizontal overflow");

          // Sidebar brand === 48px and folder-header-btn === 32px
          if (width <= 768) sidebar.classList.add("mobile-open");
          const brandRect = sidebar.querySelector(".sidebar-brand").getBoundingClientRect();
          check(Math.abs(brandRect.height - 48) <= 0.5, ".sidebar-brand height expected 48, got " + brandRect.height);
          const folderBtnRect = folders.querySelector(".folder-header-btn").getBoundingClientRect();
          check(Math.abs(folderBtnRect.height - 32) <= 0.5, ".folder-header-btn height expected 32, got " + folderBtnRect.height);
          if (width <= 768) sidebar.classList.remove("mobile-open");

          // z-index hierarchy: .modal-backdrop >= 800 > #msgSelectionToolbar / .ui-popover (650), #agyConfirmModal >= 1300
          selToolbar.classList.remove("hidden");
          const selZ = parseInt(w.getComputedStyle(selToolbar).zIndex, 10);
          check(selZ === 650, "#msgSelectionToolbar z-index expected 650, got " + selZ);

          for (const modal of modals) {
            modal.classList.remove("hidden");
            const z = parseInt(w.getComputedStyle(modal).zIndex, 10);
            check(Number.isFinite(z) && z >= 800 && z > selZ,
              modal.id + " z-index (" + z + ") must be >= 800 and > selection toolbar (" + selZ + ")");
            if (modal.id === "agyConfirmModal") {
              check(z >= 1300, "#agyConfirmModal z-index must be >= 1300, got " + z);
            }
            if (modal.id === "mediaLightboxModal") {
              check(z === 1200, "#mediaLightboxModal z-index must stay 1200, got " + z);
            }
            modal.classList.add("hidden");
          }

          // Opening any modal hides #msgSelectionToolbar
          selToolbar.classList.remove("hidden");
          check(!selToolbar.classList.contains("hidden"), "Selection toolbar should be visible before modal open");
          get("settingsModal").classList.remove("hidden");
          await Promise.resolve();
          check(selToolbar.classList.contains("hidden"), "#msgSelectionToolbar must hide when a modal opens");
          get("settingsModal").classList.add("hidden");

          // askUserConfirm hides #msgSelectionToolbar synchronously and opens #agyConfirmModal on top
          selToolbar.classList.remove("hidden");
          const confirmPromise = api.askUserConfirm({ title: "Test", message: "Proceed?" });
          check(selToolbar.classList.contains("hidden"), "askUserConfirm must hide #msgSelectionToolbar");
          check(!get("agyConfirmModal").classList.contains("hidden"), "#agyConfirmModal did not open");
          get("agyConfirmCancelBtn").click();
          check((await confirmPromise) === false, "askUserConfirm cancel should resolve false");

          checkedProfiles.push(profile);
        }

        if (runBehaviorChecks) {
          profile = width + "x" + height + "/behavior";
          api.setupMarkdown();
          // 1) Markdown URL safety: block javascript:, vbscript:, data:text/html while preserving safe schemes & file://
          const mdLinks = [
            "[js1](javascript:alert(1))",
            "[js2](JaVaScRiPt:alert(2))",
            "[vb](vbscript:msgbox(1))",
            "[datahtml](data:text/html,<script>alert(1)<" + "/script>)",
            "[https](https://example.com/docs)",
            "[mail](mailto:dev@example.com)",
            "[media](/api/media?path=demo.txt)",
            "[upload](/api/uploads/chat1/report.txt)",
            "[local](file:///D:/Projects/Demo/main.py#L10-L20)",
            "[apk](file:///D:/Projects/Demo/ZeroNote.apk)",
          ].join("\n\n");
          const wrap = d.createElement("div");
          wrap.innerHTML = api.renderMarkdownSafe(mdLinks);
          const unsafeSpans = [...wrap.querySelectorAll("span.unsafe-link-text")].map(el => el.textContent);
          check(unsafeSpans.length === 4, "Expected 4 blocked unsafe links, got " + unsafeSpans.length + ": " + wrap.innerHTML);
          const allAnchors = [...wrap.querySelectorAll("a")];
          for (const a of allAnchors) {
            const href = (a.getAttribute("href") || "").trim().toLowerCase();
            check(!href.startsWith("javascript:") && !href.startsWith("vbscript:") && !href.startsWith("data:"),
              "Unsafe href leaked into anchor: " + href);
          }
          check(wrap.querySelector('a[href="https://example.com/docs"]'), "https link missing");
          check(wrap.querySelector('a[href="mailto:dev@example.com"]'), "mailto link missing");
          check(wrap.querySelector('a[href="/api/media?path=demo.txt"]'), "/api/media link missing");
          check(wrap.querySelector('a[href="/api/uploads/chat1/report.txt"]'), "/api/uploads link missing");
          const fileAnchor = wrap.querySelector("a.hub-file-link");
          check(fileAnchor && fileAnchor.getAttribute("href").includes("/api/media?") && fileAnchor.getAttribute("href").endsWith("#L10-L20"),
            "file:// source link broken");
          const apkAnchor = wrap.querySelector("a.hub-media-link");
          check(apkAnchor && apkAnchor.getAttribute("download") === "ZeroNote.apk", "file:// deliverable link broken");
          check(!api.isSafeExternalHref("java\tscript:alert(1)"), "Control-char javascript: must be blocked");
          check(!api.isSafeExternalHref("//evil.example/phish"), "Protocol-relative URL must be blocked");
          check(api.isSafeExternalHref("./relative.md") && api.isSafeExternalHref("../parent.md") && api.isSafeExternalHref("#anchor"),
            "Relative/hash URLs should be allowed");

          // 2) Streaming unclosed fenced code skips hljsCache, closed blocks & done frame highlight normally, .copy-code-btn reads pre code
          const fence = String.fromCharCode(96).repeat(3);
          api.clearHljsCache();
          api.state.activeChatId = "chat-stream";
          const streamMsg = {
            id: "a-stream",
            chat_id: "chat-stream",
            role: "assistant",
            status: "streaming",
            thinking: "AAAA",
            has_thinking: true,
            tool_calls: [{ step_index: 0, name: "run_command", state: "DONE", output: "1111" }],
            content: fence + "js\nconst closedBlock = 1;\n" + fence + "\n\n" + fence + "python\ndef partial_fn():\n    return 1",
          };
          api.state.messages = [streamMsg];
          const msgList = get("messagesList");
          msgList.replaceChildren(api.buildMessageDom(streamMsg));
          check(api.getHljsCacheSize() === 1, "Only closed fenced block should populate hljsCache during streaming, got " + api.getHljsCacheSize());
          const copyBtns = [...msgList.querySelectorAll(".copy-code-btn")];
          check(copyBtns.length === 2, "Expected 2 copy-code-btn elements");
          check(copyBtns.every(b => !b.hasAttribute("data-code")), ".copy-code-btn must not duplicate code into data-code");
          check(api.extractCodeTextFromCopyButton(copyBtns[0]) === "const closedBlock = 1;", "Copy button 0 extraction failed");
          check(api.extractCodeTextFromCopyButton(copyBtns[1]) === "def partial_fn():\n    return 1", "Copy button 1 extraction failed");
          const legacyBtn = d.createElement("button");
          legacyBtn.dataset.code = encodeURIComponent("legacy_fallback()");
          check(api.extractCodeTextFromCopyButton(legacyBtn) === "legacy_fallback()", "Copy button legacy data-code fallback failed");

          const codes = [...msgList.querySelectorAll(".code-block-wrap pre code")];
          check(codes[0].classList.contains("hljs") && codes[0].querySelector("span"), "Closed block must be highlighted");
          check(!codes[1].classList.contains("hljs") && !codes[1].querySelector("span"), "Unclosed streaming block must skip hljs");

          // Additional streaming chunk still inside unclosed fence -> hljsCache size MUST stay 1
          streamMsg.content += "\n    # streaming more";
          api.updateSingleAssistantDom(streamMsg);
          check(api.getHljsCacheSize() === 1, "Unclosed streaming updates must not thrash hljsCache, got " + api.getHljsCacheSize());

          // Close fence and finalize -> hljsCache caches the second block and highlights it
          streamMsg.content += "\n" + fence;
          streamMsg.status = "done";
          api.updateSingleAssistantDom(streamMsg);
          check(api.getHljsCacheSize() === 2, "Finalized block must be highlighted and cached in hljsCache, got " + api.getHljsCacheSize());
          const finalCodes = [...msgList.querySelectorAll(".code-block-wrap pre code")];
          check(finalCodes[1].classList.contains("hljs") && finalCodes[1].querySelector("span"), "Finalized block must have hljs spans");

          // 3) Same-length replacements in body (before last 64 chars), thinking, and tool output MUST trigger slot re-render
          const tailPad = " " + "z".repeat(120);
          streamMsg.content = "AAAA" + tailPad;
          api.updateSingleAssistantDom(streamMsg);
          check(msgList.querySelector(".markdown-body").textContent.includes("AAAA"), "Initial body missing");
          const bodySig1 = msgList.querySelector(".assistant-body-slot").dataset.sig;
          streamMsg.content = "BBBB" + tailPad;
          api.updateSingleAssistantDom(streamMsg);
          const bodySig2 = msgList.querySelector(".assistant-body-slot").dataset.sig;
          check(bodySig1 !== bodySig2, "Body slot sig did not change on same-length prefix edit");
          check(msgList.querySelector(".markdown-body").textContent.includes("BBBB"), "Body DOM did not update on same-length edit");

          const thinkSig1 = api.describeAssistantSlot(streamMsg, "thinking", { thinkingOpen: true }).sig;
          streamMsg.thinking = "ZZZZ";
          const thinkSig2 = api.describeAssistantSlot(streamMsg, "thinking", { thinkingOpen: true }).sig;
          check(thinkSig1 !== thinkSig2, "Thinking slot sig did not change on same-length edit");

          const toolSig1 = api.computeAssistantToolsSig(streamMsg.tool_calls);
          streamMsg.tool_calls = [{ step_index: 0, name: "run_command", state: "DONE", output: "9999" }];
          const toolSig2 = api.computeAssistantToolsSig(streamMsg.tool_calls);
          check(toolSig1 !== toolSig2, "Tools slot sig did not change on same-length output edit");
        }

        resolve(checkedProfiles);
      } catch (error) {
        reject(new Error(profile + ": " + error.message));
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
  for (let i = 0; i < viewports.length; i++) {
    const [w, h] = viewports[i];
    const profiles = await measureViewport(w, h, i === 0);
    result.push(...profiles);
  }
  document.getElementById("report").textContent = JSON.stringify({ result });
})().catch(error => {
  document.getElementById("report").textContent = JSON.stringify({ error: error.message });
});
"""


def test_chromium_z_index_geometry_markdown_and_streaming_v169(tmp_path):
    report = run_workspace_chromium(
        tmp_path,
        CHROMIUM_CHECKS,
        vendor_scripts=("vendor/marked.min.js", "vendor/highlight.min.js"),
    )
    assert report == [
        "1440x900/dark", "1440x900/light",
        "768x1024/dark", "768x1024/light",
        "390x844/dark", "390x844/light",
    ]


NODE_WS_VOICE_AND_LOGOUT_CHECKS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const noop = () => {};
const ids = JSON.parse(fs.readFileSync(0, "utf8"));

function makeEl(id) {
  const classes = new Set(["hidden"]);
  const listeners = new Map();
  return {
    id, value: "", textContent: "", innerHTML: "", options: [], dataset: {}, children: [],
    classList: {
      add: n => classes.add(n),
      remove: n => classes.delete(n),
      toggle: (n, en) => (en === undefined ? (classes.has(n) ? classes.delete(n) : classes.add(n)) : (en ? classes.add(n) : classes.delete(n))),
      contains: n => classes.has(n),
    },
    style: { setProperty: noop, removeProperty: noop },
    addEventListener: (t, fn) => {
      if (!listeners.has(t)) listeners.set(t, []);
      listeners.get(t).push(fn);
    },
    removeEventListener: noop,
    setAttribute: noop, removeAttribute: noop, getAttribute: () => null,
    querySelector: () => null, querySelectorAll: () => [],
    focus: noop,
    _listeners: listeners,
  };
}

const nodes = new Map(ids.map(id => [id, makeEl(id)]));
const storageMap = new Map();
const storage = {
  getItem: k => storageMap.has(k) ? storageMap.get(k) : null,
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: k => storageMap.delete(k),
};

const scheduledTimeouts = [];
let nextTimerId = 1;
const clearedTimeouts = new Set();
function fakeSetTimeout(fn, ms) {
  const id = nextTimerId++;
  scheduledTimeouts.push({ id, fn, ms, cleared: false });
  return id;
}
function fakeClearTimeout(id) {
  if (!id) return;
  clearedTimeouts.add(id);
  const item = scheduledTimeouts.find(t => t.id === id);
  if (item) item.cleared = true;
}

const createdSockets = [];
class FakeWebSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.binaryType = "";
    this.closed = false;
    this.sent = [];
    createdSockets.push(this);
  }
  send(data) { this.sent.push(data); }
  close() { this.closed = true; this.readyState = FakeWebSocket.CLOSED; }
}

const doc = {
  documentElement: makeEl("html"),
  body: makeEl("body"),
  hidden: false,
  getElementById: id => nodes.get(id) ?? null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeEl("dyn"),
  addEventListener: noop,
};

const ctx = {
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
  document: doc,
  localStorage: storage,
  sessionStorage: storage,
  location: new URL("https://hub.invalid/"),
  URL, URLSearchParams,
  WebSocket: FakeWebSocket,
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  setInterval: () => nextTimerId++,
  clearInterval: noop,
  requestAnimationFrame: noop,
  cancelAnimationFrame: noop,
  addEventListener: noop,
  removeEventListener: noop,
  innerWidth: 1280, innerHeight: 800,
  navigator: { userAgent: "Desktop" },
  matchMedia: () => ({ matches: false }),
  CSS: { escape: String },
  console,
};
ctx.window = ctx;
ctx.globalThis = ctx;
const sandbox = vm.createContext(ctx);
vm.runInContext(fs.readFileSync("app/static/app.js", "utf8"), sandbox, { filename: "app.js", timeout: 5000 });
const api = ctx.__AGY_ADVISOR_API__;

// 1) formatCompactTokens single live implementation
assert.equal(api.formatCompactTokens(0), "0");
assert.equal(api.formatCompactTokens(1500), "1.5k");
assert.equal(api.formatCompactTokens(1250000), "1.25M");

// 2) Main WebSocket: does NOT schedule reconnect on close when state.token is empty (post-logout)
api.state.token = "valid-token";
api.connectWebSocket();
assert.equal(createdSockets.length, 1);
const ws1 = createdSockets[0];
scheduledTimeouts.length = 0;
ws1.onclose({ code: 1006 });
assert.equal(scheduledTimeouts.length, 1, "Should schedule reconnect while token is present");
assert.equal(scheduledTimeouts[0].ms, 1800);

api.state.token = "valid-token";
api.connectWebSocket();
const ws2 = createdSockets[createdSockets.length - 1];
scheduledTimeouts.length = 0;
api.state.token = "";
ws2.onclose({ code: 1000 });
assert.equal(scheduledTimeouts.length, 0, "Must NOT schedule WS reconnect when state.token is empty");
assert.equal(api.state.wsReconnectTimer, null);

// 3) stopLiveVoiceCall closes and clears playbackCtx
let playbackClosed = false;
api.state.liveCall.active = true;
api.state.liveCall.sessionId = "lv_close_test";
api.state.liveCall.playbackCtx = { close() { playbackClosed = true; } };
api.state.liveCall.playbackGain = {};
api.state.liveCall.playbackAnalyser = {};
api.stopLiveVoiceCall({ silent: true });
assert.equal(playbackClosed, true, "stopLiveVoiceCall must close playbackCtx");
assert.equal(api.state.liveCall.playbackCtx, null, "stopLiveVoiceCall must clear playbackCtx");
assert.equal(api.state.liveCall.playbackGain, null);
assert.equal(api.state.liveCall.playbackAnalyser, null);

// 4) openLiveCallVoiceWebSocket exponential backoff 500 -> 1000 -> 2000 -> 5000, reset on onopen, stop on !state.token
api.state.token = "voice-token";
api.state.liveCall.active = true;
api.state.liveCall.sessionId = "lv_backoff";
api.state.liveCall.wsReconnectAttempts = 0;
scheduledTimeouts.length = 0;
const delays = [];
api.openLiveCallVoiceWebSocket("lv_backoff");
for (let i = 0; i < 5; i++) {
  const voiceWs = createdSockets[createdSockets.length - 1];
  const before = scheduledTimeouts.length;
  voiceWs.onclose();
  assert.equal(scheduledTimeouts.length, before + 1, "Expected backoff timer on close #" + i);
  const timer = scheduledTimeouts[scheduledTimeouts.length - 1];
  delays.push(timer.ms);
  timer.fn();
}
assert.deepEqual(delays, [500, 1000, 2000, 5000, 5000]);

// Reset backoff on onopen
const openWs = createdSockets[createdSockets.length - 1];
openWs.readyState = FakeWebSocket.OPEN;
openWs.onopen();
assert.equal(api.state.liveCall.wsReconnectAttempts, 0);
const beforeResetClose = scheduledTimeouts.length;
openWs.onclose();
const resetTimer = scheduledTimeouts[scheduledTimeouts.length - 1];
assert.equal(scheduledTimeouts.length, beforeResetClose + 1);
assert.equal(resetTimer.ms, 500, "Backoff must reset to 500ms after onopen");

// Stop reconnect when token cleared
resetTimer.fn();
const noTokWs = createdSockets[createdSockets.length - 1];
const beforeNoTok = scheduledTimeouts.length;
api.state.token = "";
noTokWs.onclose();
assert.equal(scheduledTimeouts.length, beforeNoTok, "Voice WS must not reconnect when !state.token");
api.stopLiveVoiceCall({ silent: true });
"""


def test_node_ws_backoff_voice_cleanup_and_dead_code_v169():
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")

    # Exactly one formatCompactTokens declaration
    decl_hits = re.findall(r"\bfunction\s+formatCompactTokens\s*\(", app_js)
    assert len(decl_hits) == 1, f"Expected 1 formatCompactTokens declaration, found {len(decl_hits)}"

    # No duplicate keys in exportedAdvisorApi
    api_match = re.search(r"const\s+exportedAdvisorApi\s*=\s*\{([\s\S]*?)\};", app_js)
    assert api_match is not None
    keys = [tok for tok in re.split(r"[\s,]+", api_match.group(1)) if tok]
    duplicates = [k for k in set(keys) if keys.count(k) > 1]
    assert not duplicates, f"Duplicate keys in exportedAdvisorApi: {duplicates}"

    # No degenerate toggle condition and no data-code attribute template in renderer.code
    assert "!admin && inQuota === false ? false : false" not in app_js
    assert 'data-code="${encoded}"' not in app_js

    # CSS contracts: styles.css untouched vs HEAD, ui-system.css strictly 1 !important
    ui_css = (STATIC / "ui-system.css").read_text(encoding="utf-8")
    stripped_css = re.sub(r"/\*[\s\S]*?\*/", "", ui_css)
    important_hits = re.findall(r"!\s*important\b", stripped_css, flags=re.IGNORECASE)
    assert len(important_hits) == 1, f"Expected strictly 1 !important in ui-system.css, found {len(important_hits)}"

    diff_styles = subprocess.run(
        ["git", "diff", "HEAD", "--", "app/static/styles.css"],
        cwd=ROOT, capture_output=True, text=True, **subprocess_hidden_kwargs(),
    )
    assert diff_styles.returncode == 0 and diff_styles.stdout.strip() == "", "app/static/styles.css is frozen and must not change"

    parser = ShellParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    parser.close()
    run_node("ws-voice-logout-v169", parser.ids, script=NODE_WS_VOICE_AND_LOGOUT_CHECKS)


SW_CONTRACT_CHECKS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const swSource = fs.readFileSync("app/static/sw.js", "utf8");
const swCacheMatch = swSource.match(/CACHE_NAME\s*=\s*"(agy-hub-v(\d+))"/);
assert.ok(swCacheMatch, "CACHE_NAME missing in sw.js");
const CURRENT_CACHE = swCacheMatch[1];
const CURRENT_VER = swCacheMatch[2];
const PREV_CACHE = `agy-hub-v${Number(CURRENT_VER) - 1}`;

class FakeResponse {
  constructor(body = "", init = {}) {
    this.body = body;
    this.status = init.status ?? 200;
    this.statusText = init.statusText ?? "";
    this.ok = this.status >= 200 && this.status < 300;
  }
  clone() {
    return new FakeResponse(this.body, { status: this.status, statusText: this.statusText });
  }
}

function loadSw({ fetchImpl, initialCaches = {}, putThrows = false }) {
  const handlers = {};
  const cacheStores = new Map();
  for (const [name, entries] of Object.entries(initialCaches)) {
    cacheStores.set(name, new Map(Object.entries(entries)));
  }
  const deletedCaches = [];
  let skipWaitingCount = 0;
  let claimCount = 0;
  const openCache = (name) => {
    if (!cacheStores.has(name)) cacheStores.set(name, new Map());
    const store = cacheStores.get(name);
    return {
      async put(req, resp) {
        if (putThrows) throw new Error("QuotaExceededError");
        const key = typeof req === "string" ? req : req.url;
        store.set(key, resp);
      },
      async match(req) {
        const key = typeof req === "string" ? req : req.url;
        return store.get(key);
      },
    };
  };
  const sandbox = {
    URL, Promise, Response: FakeResponse,
    fetch: fetchImpl,
    caches: {
      async open(name) { return openCache(name); },
      async keys() { return [...cacheStores.keys()]; },
      async delete(name) { deletedCaches.push(name); return cacheStores.delete(name); },
      async match(req) {
        const key = typeof req === "string" ? req : req.url;
        for (const store of cacheStores.values()) {
          if (store.has(key)) return store.get(key);
        }
        return undefined;
      },
    },
    self: {
      location: { origin: "https://hub.test" },
      addEventListener: (type, fn) => { handlers[type] = fn; },
      skipWaiting() { skipWaitingCount += 1; },
      clients: { claim() { claimCount += 1; }, matchAll: async () => [], openWindow: async () => {} },
      registration: { showNotification: async () => {} },
    },
  };
  vm.runInNewContext(swSource, sandbox, { filename: "sw.js" });
  return { handlers, cacheStores, deletedCaches, getSkipWaiting: () => skipWaitingCount, getClaim: () => claimCount };
}

(async () => {
  // 1) install rejects when any SHELL_ASSETS returns !resp.ok (e.g. 503 during restart) and never skips waiting
  {
    const env = loadSw({
      fetchImpl: async (url) => url.includes("app.js")
        ? new FakeResponse("down", { status: 503 })
        : new FakeResponse("ok", { status: 200 }),
    });
    let installPromise = null;
    env.handlers.install({ waitUntil: p => { installPromise = p; } });
    await assert.rejects(installPromise, /Precache failed \(503\)/);
    assert.equal(env.getSkipWaiting(), 0, "Broken install must not call skipWaiting()");
  }

  // 2) install succeeds when all SHELL_ASSETS return 200
  {
    const env = loadSw({
      fetchImpl: async () => new FakeResponse("ok", { status: 200 }),
    });
    let installPromise = null;
    env.handlers.install({ waitUntil: p => { installPromise = p; } });
    await installPromise;
    assert.equal(env.getSkipWaiting(), 1);
    assert.ok(env.cacheStores.get(CURRENT_CACHE)?.has(`/static/app.js?v=${CURRENT_VER}`));
  }

  // 3) activate removes only old agy-hub-* caches, preserving current cache and third-party caches
  {
    const env = loadSw({
      fetchImpl: async () => new FakeResponse("ok"),
      initialCaches: {
        [PREV_CACHE]: {},
        [CURRENT_CACHE]: {},
        "unrelated-app-v1": {},
      },
    });
    let activatePromise = null;
    env.handlers.activate({ waitUntil: p => { activatePromise = p; } });
    await activatePromise;
    assert.deepEqual(env.deletedCaches, [PREV_CACHE]);
    assert.ok(env.cacheStores.has(CURRENT_CACHE));
    assert.ok(env.cacheStores.has("unrelated-app-v1"));
    assert.equal(env.getClaim(), 1);
  }

  // 4) navigate caches under canonical "/" key (not ?open_chat=...) and catches QuotaExceededError in waitUntil
  {
    const env = loadSw({
      fetchImpl: async () => new FakeResponse("SHELL_HTML", { status: 200 }),
    });
    const waitPromises = [];
    let respPromise = null;
    env.handlers.fetch({
      request: { method: "GET", url: "https://hub.test/?open_chat=chat_42", mode: "navigate" },
      respondWith: p => { respPromise = p; },
      waitUntil: p => { waitPromises.push(p); },
    });
    const resp = await respPromise;
    assert.equal(resp.status, 200);
    await Promise.all(waitPromises);
    const store = env.cacheStores.get(CURRENT_CACHE);
    assert.ok(store.has("/"), "Navigate response must be cached under canonical '/' key");
    assert.ok(!store.has("https://hub.test/?open_chat=chat_42"), "Navigate query URL must not bloat cache");
  }

  // 5) QuotaExceededError in cache.put is swallowed cleanly inside waitUntil
  {
    const env = loadSw({
      fetchImpl: async () => new FakeResponse("SHELL_HTML", { status: 200 }),
      putThrows: true,
    });
    const waitPromises = [];
    let respPromise = null;
    env.handlers.fetch({
      request: { method: "GET", url: "https://hub.test/?open_chat=chat_99", mode: "navigate" },
      respondWith: p => { respPromise = p; },
      waitUntil: p => { waitPromises.push(p); },
    });
    const resp = await respPromise;
    assert.equal(resp.status, 200);
    await Promise.all(waitPromises);
  }

  // 6) Offline cache miss returns valid 503 Response (never undefined) in BOTH Cache-First and Network-First branches
  {
    const env = loadSw({
      fetchImpl: async () => { throw new Error("offline"); },
    });
    let navResp = null;
    env.handlers.fetch({
      request: { method: "GET", url: "https://hub.test/?open_chat=miss", mode: "navigate" },
      respondWith: p => { navResp = p; },
    });
    const navRes = await navResp;
    assert.equal(navRes.status, 503);
    assert.equal(navRes.statusText, "Offline");

    let staticResp = null;
    env.handlers.fetch({
      request: { method: "GET", url: `https://hub.test/static/app.js?v=${CURRENT_VER}`, mode: "no-cors" },
      respondWith: p => { staticResp = p; },
    });
    const staticRes = await staticResp;
    assert.equal(staticRes.status, 503);
    assert.equal(staticRes.statusText, "Offline");
  }

  // 7) push & notificationclick handlers remain registered
  {
    const env = loadSw({ fetchImpl: async () => new FakeResponse("ok") });
    assert.equal(typeof env.handlers.push, "function");
    assert.equal(typeof env.handlers.notificationclick, "function");
  }
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""


def test_sw_v169_contracts_and_verify_hub_html_guard():
    sw_text = (STATIC / "sw.js").read_text(encoding="utf-8")
    html_text = (STATIC / "index.html").read_text(encoding="utf-8")

    match = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_text)
    assert match and int(match.group(1)) >= 169
    ver = match.group(1)
    for asset in ("styles.css", "ui-system.css", "ui-shell.js", "app.js"):
        assert f"/static/{asset}?v={ver}" in sw_text
        assert f"/static/{asset}?v={ver}" in html_text

    # verify_hub STATIC_ASSET_RE must cover index.html
    vh = _load_verify_hub()
    assert vh.STATIC_ASSET_RE.match("app/static/index.html")
    errors = vh.staged_guard_errors(
        ["app/static/index.html"],
        ["app/static/index.html"],
        'const CACHE_NAME = "agy-hub-v168";',
        'const CACHE_NAME = "agy-hub-v168";',
    )
    assert len(errors) == 1 and "Cache-First PWA guard" in errors[0]

    run_node("sw-v169-contracts", {}, script=SW_CONTRACT_CHECKS)
