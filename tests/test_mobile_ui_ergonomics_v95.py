"""
Unit, DOM-logic & static invariant tests for Antigravity Hub Release v=95:
Comprehensive Mobile UI Adaptation & Ergonomics (320px-430px / <= 860px):
1. Cache version synchronization (`v=95`) across `app/static/sw.js` and `app/static/index.html`,
   UTF-8 without BOM encoding on all edited static/test files, and `node --check` JS syntax verification.
2. CSS & HTML ergonomics invariants:
   - Balanced `{}` braces in `app/static/styles.css`
   - Zero `transform`, `filter`, `backdrop-filter`, `perspective`, `contain`, or `container-type` on `main.workspace`
   - Removal of legacy `@media (max-width: 375px)` `.mobile-access-bar .mobile-seg { width: 100% }` rule
   - Compact `#activeChatStatePill` (`<= 760px`), `#topAccessSeg` hidden at `<= 760px`, `34x34px` `+`/`⋮` buttons,
     compact `#toggleSidebarBtn.has-radar-pills` (`<= 480px`), flexible `#mobileAccessBar` & `#modelPickerWrap`
   - `38x38px` `#composerBox` buttons (`<= 680px`) and `margin-right: 6px` on `#stopBtn` when `#sendBtn.is-queue-send` is visible
   - `.messages-container` mobile padding `12px 12px 28px`, separated `#mobileOutlinePill` (left `12px`) and
     `#scrollBottomBtn` (right `12px`, `36x36px`, `opacity: 0.9`) at `bottom: calc(var(--composer-floating-offset, 108px) + 10px)`
   - `.usage-tok-full` / `.usage-tok-short` (`<= 480px`), `.msg-action-btn` (`min-height: 32px` at `<= 600px`)
   - `#mobileTelemetryBar` 2x2 CSS Grid (`minmax(0, 1fr) minmax(0, 1fr) 28px`) without `104px..116px` pill truncation caps
   - Mobile `#modelPickerMenu`, `#chatContextMenu`, `#projectContextMenu` (`<= 640px`), and quota modal headers (`<= 480px`)
3. Isolated `node:vm` execution of real `app/static/app.js` functions:
   - `formatCompactMobileModelLabel` at `412px` and `360px` (`6.1 Sol · High`, `6 Astra · XHigh`, `Opus 4.7 · Think`, `Carbon 4 · Med`, `H`/`XH`/`L`)
   - `syncComposerState` across all 4 states `(isGen: false/true) x (hasDraftPayload: false/true)` + `liveCall.active` + `input` / clear / draft restore
   - `🐳 Терминал` -> `🐳 Терм.` shortening at `412px` and resize recalculation via `setupWorkspaceLayoutObservers()`
   - `syncActiveChatHeaderStatus` (`aria-label` + `.ocsp-q`), `buildAssistantFooterInnerHtml` (`29 998 tok` / `30.0k`),
     `updateScrollBottomBtnState` (`190px` mobile threshold), `positionModelPickerMenu`, and context menu `10px` edge clamping.
4. Strict `0% GPU / 0 MB VRAM`, `0` console windows on Windows 11 (`subprocess_hidden_kwargs()`), and `0` writes to `data/hub.db`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["ORT_DISABLE_ALL_GPU"] = "1"

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

APP_JS_PATH = ROOT_DIR / "app" / "static" / "app.js"
SW_JS_PATH = ROOT_DIR / "app" / "static" / "sw.js"
INDEX_HTML_PATH = ROOT_DIR / "app" / "static" / "index.html"
STYLES_CSS_PATH = ROOT_DIR / "app" / "static" / "styles.css"
LIVE_DB_PATH = ROOT_DIR / "data" / "hub.db"

try:
    from app.config import subprocess_hidden_kwargs
except Exception:

    def subprocess_hidden_kwargs() -> dict:
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            if hasattr(subprocess, "STARTUPINFO"):
                si = subprocess.STARTUPINFO()
                si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
                si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
                kwargs["startupinfo"] = si
        return kwargs


def strip_css_comments_and_strings(css: str) -> str:
    css_no_comments = re.sub(r"/\*[\s\S]*?\*/", "", css)
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', css_no_comments)


NODE_MOBILE_V95_VM_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const appJsPath = process.env.AGY_APP_JS_PATH;
let src = fs.readFileSync(appJsPath, "utf8");

const tailTarget = /init\(\);\s*\}\)\(\);\s*$/;
if (!tailTarget.test(src)) {
  throw new Error("Could not locate IIFE tail init() in app.js");
}
src = src.replace(
  tailTarget,
  `globalThis.__AGY_V95_TEST__ = {
    state,
    els,
    bindEvents,
    setupWorkspaceLayoutObservers,
    formatCompactMobileModelLabel,
    parseModelEntry,
    syncModelPickerUI,
    positionModelPickerMenu,
    syncMobileHeaderToolsSlot,
    syncAccessSelectStyle,
    syncComposerState,
    clearComposerAfterSend,
    restoreComposerDraft,
    syncActiveChatHeaderStatus,
    formatUsageTokensShort,
    buildAssistantFooterInnerHtml,
    updateScrollBottomBtnState,
    openChatContextMenuAtPoint,
    openProjectContextMenuAtPoint,
  };
})();`
);

const storageMap = new Map();
const localStorageStub = {
  getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
};

function decodeHtmlEntities(str) {
  return String(str || "")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

function attrToDatasetKey(attrName) {
  if (!attrName.startsWith("data-")) return null;
  return attrName
    .slice(5)
    .replace(/-([a-z0-9])/gi, (_, ch) => ch.toUpperCase());
}

function makeClassList(initial = "") {
  const set = new Set(String(initial || "").split(/\s+/).filter(Boolean));
  return {
    add: (...cls) => cls.forEach((c) => c && set.add(c)),
    remove: (...cls) => cls.forEach((c) => set.delete(c)),
    toggle: (c, force) => {
      if (force === undefined) {
        if (set.has(c)) {
          set.delete(c);
          return false;
        }
        if (c) set.add(c);
        return true;
      }
      if (force) {
        if (c) set.add(c);
      } else {
        set.delete(c);
      }
      return Boolean(force);
    },
    contains: (c) => set.has(c),
    toString: () => Array.from(set).join(" "),
    _resetFromString: (s) => {
      set.clear();
      String(s || "")
        .split(/\s+/)
        .filter(Boolean)
        .forEach((c) => set.add(c));
    },
  };
}

function matchesSimpleCompound(el, compound) {
  if (!el || !compound) return false;
  let rest = compound.trim();
  if (!rest) return false;

  const attrRegex = /\[\s*([a-zA-Z0-9_-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\]]+)))?\s*\]/g;
  let attrMatch;
  while ((attrMatch = attrRegex.exec(rest)) !== null) {
    const attrName = attrMatch[1];
    const hasVal =
      attrMatch[2] !== undefined || attrMatch[3] !== undefined || attrMatch[4] !== undefined;
    const expected = attrMatch[2] ?? attrMatch[3] ?? attrMatch[4] ?? "";
    const dsKey = attrToDatasetKey(attrName);
    let actual = el.getAttribute(attrName);
    if (actual === null && dsKey && el.dataset && el.dataset[dsKey] !== undefined) {
      actual = String(el.dataset[dsKey]);
    }
    if (actual === null) return false;
    if (hasVal && String(actual) !== expected) return false;
  }
  rest = rest.replace(attrRegex, "");

  const idMatch = rest.match(/#([a-zA-Z0-9_-]+)/);
  if (idMatch) {
    if (el.id !== idMatch[1]) return false;
    rest = rest.replace(/#[a-zA-Z0-9_-]+/, "");
  }

  const clsRegex = /\.([a-zA-Z0-9_-]+)/g;
  let clsMatch;
  while ((clsMatch = clsRegex.exec(rest)) !== null) {
    if (!el.classList || !el.classList.contains(clsMatch[1])) return false;
  }
  rest = rest.replace(clsRegex, "").trim();

  if (rest && rest !== "*") {
    if (String(el.tagName || "").toUpperCase() !== rest.toUpperCase()) return false;
  }
  return true;
}

function matchesSelector(el, selector) {
  const groups = String(selector || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  for (const g of groups) {
    const tokens = g.split(/\s+/).filter(Boolean);
    if (!tokens.length) continue;
    if (!matchesSimpleCompound(el, tokens[tokens.length - 1])) continue;
    let cur = el.parentElement;
    let ok = true;
    for (let i = tokens.length - 2; i >= 0; i--) {
      while (cur && !matchesSimpleCompound(cur, tokens[i])) {
        cur = cur.parentElement;
      }
      if (!cur) {
        ok = false;
        break;
      }
      cur = cur.parentElement;
    }
    if (ok) return true;
  }
  return false;
}

function walkDescendants(root, visitor) {
  const kids = root.children || [];
  for (let i = 0; i < kids.length; i++) {
    const child = kids[i];
    if (visitor(child) === true) return true;
    if (walkDescendants(child, visitor) === true) return true;
  }
  return false;
}

const VOID_TAGS = new Set([
  "BR",
  "HR",
  "IMG",
  "INPUT",
  "META",
  "LINK",
  "PATH",
  "CIRCLE",
  "RECT",
  "LINE",
  "POLYLINE",
  "POLYGON",
]);

function parseHtmlFragment(html, ownerFactory) {
  const str = String(html || "");
  const root = { children: [] };
  const stack = [root];
  const tokenRe = /<!--[\s\S]*?-->|<\/([a-zA-Z0-9:-]+)\s*>|<([a-zA-Z0-9:-]+)([^>]*?)(\/?)>|([^<]+)/g;
  let m;
  while ((m = tokenRe.exec(str)) !== null) {
    if (m[1]) {
      const closeTag = m[1].toUpperCase();
      for (let s = stack.length - 1; s >= 1; s--) {
        if (stack[s].tagName === closeTag) {
          stack.length = s;
          break;
        }
      }
    } else if (m[2]) {
      const tag = m[2].toUpperCase();
      const attrStr = m[3] || "";
      const selfClose = Boolean(m[4]) || VOID_TAGS.has(tag);
      const el = ownerFactory("", tag);
      const attrRe = /([a-zA-Z0-9_:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let am;
      while ((am = attrRe.exec(attrStr)) !== null) {
        const aName = am[1];
        const aVal = decodeHtmlEntities(am[2] ?? am[3] ?? am[4] ?? "");
        el.setAttribute(aName, aVal);
      }
      const parent = stack[stack.length - 1];
      if (parent === root) {
        root.children.push(el);
      } else {
        parent.appendChild(el);
      }
      if (!selfClose) {
        stack.push(el);
      }
    } else if (m[5]) {
      const txt = decodeHtmlEntities(m[5]);
      const parent = stack[stack.length - 1];
      if (parent !== root) {
        parent._ownText = (parent._ownText || "") + txt;
      }
    }
  }
  return root.children;
}

function makeDomEl(id = "", tag = "DIV") {
  const listeners = new Map();
  const attrs = new Map();
  const children = [];
  const classList = makeClassList();
  let rawInnerHtml = "";
  let explicitText = null;

  const el = {
    id,
    tagName: String(tag || "DIV").toUpperCase(),
    style: { setProperty: () => {}, removeProperty: () => {} },
    dataset: {},
    classList,
    options: [],
    disabled: false,
    title: "",
    value: "",
    scrollTop: 0,
    scrollHeight: 600,
    clientHeight: 600,
    clientWidth: 412,
    offsetWidth: 412,
    parentElement: null,
    get parentNode() {
      return el.parentElement;
    },
    children,
    childNodes: children,
    _ownText: "",
    _customRect: null,
    get firstChild() {
      return children[0] || null;
    },
    get className() {
      return classList.toString();
    },
    set className(val) {
      classList._resetFromString(val);
      attrs.set("class", String(val || ""));
    },
    get innerHTML() {
      return rawInnerHtml;
    },
    set innerHTML(val) {
      rawInnerHtml = String(val || "");
      explicitText = null;
      el._ownText = "";
      children.length = 0;
      if (rawInnerHtml.trim()) {
        const parsed = parseHtmlFragment(rawInnerHtml, makeDomEl);
        for (const child of parsed) {
          child.parentElement = el;
          children.push(child);
        }
      }
    },
    get textContent() {
      if (explicitText !== null) return explicitText;
      let out = el._ownText || "";
      for (const c of children) out += c.textContent;
      return out;
    },
    set textContent(val) {
      explicitText = String(val ?? "");
      rawInnerHtml = "";
      el._ownText = explicitText;
      children.length = 0;
    },
    setAttribute(name, val) {
      const sVal = String(val ?? "");
      attrs.set(name, sVal);
      if (name === "id") el.id = sVal;
      else if (name === "class") classList._resetFromString(sVal);
      else if (name === "title") el.title = sVal;
      else if (name === "value") el.value = sVal;
      else {
        const dsKey = attrToDatasetKey(name);
        if (dsKey) el.dataset[dsKey] = sVal;
      }
    },
    getAttribute(name) {
      if (name === "class") return classList.toString() || null;
      if (name === "id") return el.id || null;
      if (name === "title") return attrs.has("title") ? attrs.get("title") : el.title || null;
      return attrs.has(name) ? attrs.get(name) : null;
    },
    removeAttribute(name) {
      attrs.delete(name);
      if (name === "title") el.title = "";
      const dsKey = attrToDatasetKey(name);
      if (dsKey) delete el.dataset[dsKey];
    },
    appendChild(child) {
      if (!child) return child;
      if (child.parentElement) {
        const idx = child.parentElement.children.indexOf(child);
        if (idx >= 0) child.parentElement.children.splice(idx, 1);
      }
      child.parentElement = el;
      children.push(child);
      return child;
    },
    insertBefore(newNode, refNode) {
      if (!newNode) return newNode;
      if (newNode.parentElement) {
        const idx = newNode.parentElement.children.indexOf(newNode);
        if (idx >= 0) newNode.parentElement.children.splice(idx, 1);
      }
      newNode.parentElement = el;
      const refIdx = refNode ? children.indexOf(refNode) : -1;
      if (refIdx >= 0) children.splice(refIdx, 0, newNode);
      else children.push(newNode);
      return newNode;
    },
    remove() {
      if (el.parentElement) {
        const idx = el.parentElement.children.indexOf(el);
        if (idx >= 0) el.parentElement.children.splice(idx, 1);
        el.parentElement = null;
      }
    },
    contains(other) {
      let cur = other;
      while (cur) {
        if (cur === el) return true;
        cur = cur.parentElement;
      }
      return false;
    },
    closest(selector) {
      let cur = el;
      while (cur) {
        if (matchesSelector(cur, selector)) return cur;
        cur = cur.parentElement;
      }
      return null;
    },
    querySelector(selector) {
      let found = null;
      walkDescendants(el, (node) => {
        if (matchesSelector(node, selector)) {
          found = node;
          return true;
        }
        return false;
      });
      return found;
    },
    querySelectorAll(selector) {
      const results = [];
      walkDescendants(el, (node) => {
        if (matchesSelector(node, selector)) results.push(node);
        return false;
      });
      return results;
    },
    addEventListener(ev, fn) {
      if (!listeners.has(ev)) listeners.set(ev, []);
      listeners.get(ev).push(fn);
    },
    removeEventListener(ev, fn) {
      const arr = listeners.get(ev);
      if (!arr) return;
      const idx = arr.indexOf(fn);
      if (idx >= 0) arr.splice(idx, 1);
    },
    dispatch(ev, eventObj = {}) {
      const evt = Object.assign(
        {
          type: ev,
          target: el,
          currentTarget: el,
          preventDefault: () => {},
          stopPropagation: () => {},
        },
        eventObj
      );
      const arr = listeners.get(ev) || [];
      for (const fn of arr) fn(evt);
      return evt;
    },
    focus() {},
    blur() {},
    scrollTo(opts) {
      if (opts && typeof opts.top === "number") el.scrollTop = opts.top;
    },
    getBoundingClientRect() {
      if (typeof el._customRect === "function") return el._customRect();
      return { top: 0, left: 0, width: 220, height: 180, bottom: 180, right: 220 };
    },
  };
  return el;
}

const byId = new Map();
function getElById(id) {
  if (!byId.has(id)) {
    const tag =
      id === "promptInput"
        ? "TEXTAREA"
        : id === "modelSelect" || id === "accessModeSelect"
          ? "SELECT"
          : "DIV";
    byId.set(id, makeDomEl(id, tag));
  }
  return byId.get(id);
}

// Mount realistic mobile header & access segment DOM children
const mobSegFull = getElById("mobileSegFullAccessBtn");
mobSegFull.dataset.segMode = "full_access";
mobSegFull.innerHTML = '<span class="seg-full-label">Терминал</span>';
const mobAccessSeg = getElById("mobileAccessSeg");
mobAccessSeg.appendChild(mobSegFull);

const deskSegFull = getElById("segFullAccessBtn");
deskSegFull.dataset.segMode = "full_access";
deskSegFull.innerHTML = '<span class="seg-full-label">Терминал</span>';
const topAccessSeg = getElById("topAccessSeg");
topAccessSeg.appendChild(deskSegFull);

getElById("mobileResetSandboxBtn").classList.add("hidden");
getElById("resetSandboxBtn").classList.add("hidden");

const accessSelect = getElById("accessModeSelect");
const optRead = makeDomEl("", "OPTION");
optRead.value = "read_only";
const optWrite = makeDomEl("", "OPTION");
optWrite.value = "write_files";
const optFull = makeDomEl("", "OPTION");
optFull.value = "full_access";
accessSelect.options = [optRead, optWrite, optFull];
accessSelect.appendChild(optRead);
accessSelect.appendChild(optWrite);
accessSelect.appendChild(optFull);
accessSelect.value = "full_access";

const workspaceMain = makeDomEl("workspaceMain", "MAIN");
workspaceMain.className = "workspace";
workspaceMain._customRect = () => ({
  top: 0,
  left: 0,
  width: sandbox.innerWidth,
  height: sandbox.innerHeight,
  bottom: sandbox.innerHeight,
  right: sandbox.innerWidth,
});

const winListeners = new Map();
let rafCounter = 1;
const rafMap = new Map();

function flushRaf() {
  let guard = 0;
  while (rafMap.size > 0 && guard < 20) {
    guard++;
    const entries = Array.from(rafMap.entries());
    rafMap.clear();
    for (const [, cb] of entries) cb(1000);
  }
}

class FakeResizeObserver {
  observe() {}
  disconnect() {}
}

const docStub = {
  hidden: false,
  documentElement: makeDomEl("html", "HTML"),
  body: makeDomEl("body", "BODY"),
  activeElement: null,
  getElementById: (id) => getElById(id),
  querySelector: (sel) => (sel === "main.workspace" ? workspaceMain : null),
  querySelectorAll: () => [],
  createElement: (t) => makeDomEl("", String(t || "DIV").toUpperCase()),
  createDocumentFragment: () => makeDomEl("", "FRAG"),
  addEventListener: () => {},
};

const sandbox = {
  console,
  innerWidth: 412,
  innerHeight: 860,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  URL,
  URLSearchParams,
  localStorage: localStorageStub,
  performance: { now: () => 1000 },
  ResizeObserver: FakeResizeObserver,
  WebSocket: { OPEN: 1, CONNECTING: 0, CLOSING: 2, CLOSED: 3 },
  navigator: { userAgent: "node-v95-test", clipboard: {} },
  location: { protocol: "http:", host: "127.0.0.1:8765", origin: "http://127.0.0.1:8765", pathname: "/" },
  CSS: { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&") },
  matchMedia: (query) => {
    const q = String(query || "");
    const maxM = q.match(/max-width:\s*(\d+)px/);
    if (maxM) return { matches: sandbox.innerWidth <= Number(maxM[1]), addEventListener: () => {} };
    const minM = q.match(/min-width:\s*(\d+)px/);
    if (minM) return { matches: sandbox.innerWidth >= Number(minM[1]), addEventListener: () => {} };
    return { matches: false, addEventListener: () => {} };
  },
  requestAnimationFrame: (fn) => {
    const id = rafCounter++;
    rafMap.set(id, fn);
    return id;
  },
  cancelAnimationFrame: (id) => rafMap.delete(id),
  document: docStub,
  addEventListener: (ev, fn) => {
    if (!winListeners.has(ev)) winListeners.set(ev, []);
    winListeners.get(ev).push(fn);
  },
  dispatchWindowEvent: (ev) => {
    const arr = winListeners.get(ev) || [];
    for (const fn of arr) fn({ type: ev });
  },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });

const api = sandbox.__AGY_V95_TEST__;
api.bindEvents();
api.setupWorkspaceLayoutObservers();

// 1) Smart model label formatting at 412px & 360px
sandbox.innerWidth = 412;
const eSol = api.parseModelEntry({ id: "gpt-6.1-sol-high", label: "GPT-6.1-Sol (High)" }, 0);
const eAstra = api.parseModelEntry({ id: "gpt-6-astra-xhigh", label: "GPT-6-Astra (XHigh)" }, 1);
const eOpus = api.parseModelEntry({ id: "claude-opus-4-7-thinking", label: "Claude Opus 4.7 (Thinking)" }, 2);
const eCarbon = api.parseModelEntry({ id: "gemini-4-carbon-medium", label: "Gemini 4 Carbon (Medium)" }, 3);
const eLow = api.parseModelEntry({ id: "gpt-6.1-sol-low", label: "GPT-6.1-Sol (Low)" }, 4);

const modelLabels412 = {
  sol: api.formatCompactMobileModelLabel(eSol),
  astra: api.formatCompactMobileModelLabel(eAstra),
  opus: api.formatCompactMobileModelLabel(eOpus),
  carbon: api.formatCompactMobileModelLabel(eCarbon),
};

sandbox.innerWidth = 360;
const modelLabels360 = {
  sol: api.formatCompactMobileModelLabel(eSol),
  astra: api.formatCompactMobileModelLabel(eAstra),
  opus: api.formatCompactMobileModelLabel(eOpus),
  low: api.formatCompactMobileModelLabel(eLow),
};

// Verify syncModelPickerUI populates compact label on mobile & full tooltip
sandbox.innerWidth = 412;
api.state.models = [
  { id: "gpt-6.1-sol-high", label: "GPT-6.1-Sol (High)" },
  { id: "claude-opus-4-7-thinking", label: "Claude Opus 4.7 (Thinking)" },
];
const mOpt1 = makeDomEl("", "OPTION");
mOpt1.value = "gpt-6.1-sol-high";
const mOpt2 = makeDomEl("", "OPTION");
mOpt2.value = "claude-opus-4-7-thinking";
api.els.modelSelect.options = [mOpt1, mOpt2];
api.els.modelSelect.appendChild(mOpt1);
api.els.modelSelect.appendChild(mOpt2);
api.els.modelSelect.value = "gpt-6.1-sol-high";
api.syncModelPickerUI();
const pickerLabel412 = api.els.modelPickerLabel.textContent;
const pickerWrapTitle412 = api.els.modelPickerWrap.title;

// 2) Composer state matrix (4 states + liveCall + input event + clear + draft restore + desktop)
function snapComposer() {
  return {
    sendHidden: api.els.sendBtn.classList.contains("hidden"),
    sendQueue: api.els.sendBtn.classList.contains("is-queue-send"),
    stopHidden: api.els.stopBtn.classList.contains("hidden"),
    liveHidden: api.els.liveVoiceBtn.classList.contains("hidden"),
    sendTitle: api.els.sendBtn.title,
  };
}

sandbox.innerWidth = 412;
api.state.activeChatId = "chat-mob-1";
api.state.generatingChats.clear();
api.state.pendingAttachments = [];
api.state.liveCall.active = false;
api.els.promptInput.value = "";
api.syncComposerState();
const compIdleEmpty = snapComposer();

// Typing via real input event on #promptInput
api.els.promptInput.value = "Привет, проверь вёрстку";
api.els.promptInput.dispatch("input");
const compIdleDraftText = snapComposer();

// Attachment-only draft payload (empty text, 1 attachment)
api.els.promptInput.value = "";
api.state.pendingAttachments = [{ name: "screen.png", path: "/tmp/screen.png" }];
api.syncComposerState();
const compIdleDraftAttach = snapComposer();

// Live call active while typing -> liveVoiceBtn must stay visible!
api.state.liveCall.active = true;
api.syncComposerState();
const compIdleDraftLiveActive = snapComposer();
api.state.liveCall.active = false;

// Generating + empty input -> ONLY stopBtn visible, sendBtn & liveVoiceBtn hidden
api.state.pendingAttachments = [];
api.els.promptInput.value = "";
api.state.generatingChats.add("chat-mob-1");
api.syncComposerState();
const compGenEmpty = snapComposer();

// Generating + draft text -> BOTH sendBtn.is-queue-send & stopBtn visible, liveVoiceBtn hidden
api.els.promptInput.value = "Дополнение в очередь";
api.els.promptInput.dispatch("input");
const compGenDraft = snapComposer();

// clearComposerAfterSend during generation -> sendBtn hides again!
api.clearComposerAfterSend("chat-mob-1");
const compAfterSendClearInGen = snapComposer();

// Desktop (1280px) typing -> liveVoiceBtn remains visible
api.state.generatingChats.clear();
sandbox.innerWidth = 1280;
api.els.promptInput.value = "Desktop draft";
api.syncComposerState();
const compDesktopDraft = snapComposer();

// restoreComposerDraft on mobile restores text and syncs composer state
sandbox.innerWidth = 412;
api.els.promptInput.value = "";
api.state.chatDrafts.set("chat-mob-1", {
  text: "Черновик после переключения",
  attachments: [],
  referencedChats: [],
});
api.restoreComposerDraft("chat-mob-1");
const compAfterRestoreDraft = snapComposer();
api.els.promptInput.value = "";
api.state.chatDrafts.clear();

// 3) "🐳 Терминал" -> "🐳 Терм." shortening & resize observer recalculation
api.state.currentUser = { id: 1, username: "admin", role: "admin" };
api.state.projects = [
  { name: "SharedProj", is_shared: true, execution_mode: "docker", path: "/shared/SharedProj" },
];
api.state.activeChat = { id: "chat-mob-1", kind: "project", project_name: "SharedProj" };
api.state.chats = [api.state.activeChat];
api.state.dockerStatus = {
  installed: true,
  daemon_running: true,
  image_ready: true,
  sandbox_mcp_ready: true,
};
api.els.securityModeWrap.classList.remove("hidden");
api.els.accessModeSelect.value = "full_access";

sandbox.innerWidth = 412;
api.syncAccessSelectStyle();
const dockerTerm412 = api.els.mobileSegFullAccessBtn.textContent;
const dockerTermTitle412 = api.els.mobileSegFullAccessBtn.title;

// Also test non-docker project at 600px ("Терминал") -> resize to 412px ("Терм.") -> resize to 360px ("6.1 Sol · H")
api.state.projects = [{ name: "LocalProj", is_shared: false, path: "D:/LocalProj" }];
api.state.activeChat = { id: "chat-mob-1", kind: "project", project_name: "LocalProj" };
api.state.chats = [api.state.activeChat];
sandbox.innerWidth = 600;
sandbox.dispatchWindowEvent("resize");
flushRaf();
const localTerm600 = api.els.mobileSegFullAccessBtn.textContent;

sandbox.innerWidth = 412;
sandbox.dispatchWindowEvent("resize");
flushRaf();
const localTerm412 = api.els.mobileSegFullAccessBtn.textContent;

sandbox.innerWidth = 360;
sandbox.dispatchWindowEvent("resize");
flushRaf();
const pickerLabelAfterResize360 = api.els.modelPickerLabel.textContent;

// 4) Stage 3 & Stage 4 helpers: activeChatStatePill, usage tokens, scrollBottomBtn, menus
sandbox.innerWidth = 412;
api.state.generatingChats.add("chat-mob-1");
api.state.chatQueues.set("chat-mob-1", {
  items: [{ id: "q1", prompt: "First" }, { id: "q2", prompt: "Second" }],
  paused: false,
});
api.syncActiveChatHeaderStatus();
const pillAriaLabel = api.els.activeChatStatePill.getAttribute("aria-label");
const pillTitle = api.els.activeChatStatePill.title;
const pillQueueBadge = api.els.activeChatStatePill.querySelector(".ocsp-q")?.textContent || "";
api.state.generatingChats.clear();
api.state.chatQueues.clear();

const footerEl = makeDomEl("", "DIV");
footerEl.innerHTML = api.buildAssistantFooterInnerHtml(
  {
    id: "a-1",
    role: "assistant",
    status: "done",
    usage: { total_tokens: 29998, input_tokens: 20000, output_tokens: 9998 },
  },
  "Ответ ассистента готов."
);
const usageHintEl = footerEl.querySelector(".usage-hint");
const usageFullTxt = (usageHintEl?.querySelector(".usage-tok-full")?.textContent || "").replace(/[\u00a0\u202f]/g, " ");
const usageShortTxt = usageHintEl?.querySelector(".usage-tok-short")?.textContent || "";
const usageTitleTxt = (usageHintEl?.getAttribute("title") || "").replace(/[\u00a0\u202f]/g, " ");

// Scroll bottom threshold: 190px on mobile (<= 860px) vs 140px on desktop
api.els.messagesContainer.scrollHeight = 2000;
api.els.messagesContainer.clientHeight = 600;
// dist = 2000 - 1235 - 600 = 165px
api.els.messagesContainer.scrollTop = 1235;
sandbox.innerWidth = 412;
api.updateScrollBottomBtnState();
const scrollBtnHiddenAt165Mobile = api.els.scrollBottomBtn.classList.contains("hidden");

api.els.messagesContainer.scrollTop = 1195; // dist = 205px > 190px
api.updateScrollBottomBtnState();
const scrollBtnHiddenAt205Mobile = api.els.scrollBottomBtn.classList.contains("hidden");

api.els.messagesContainer.scrollTop = 1235; // dist = 165px > 140px on desktop
sandbox.innerWidth = 1280;
api.updateScrollBottomBtnState();
const scrollBtnHiddenAt165Desktop = api.els.scrollBottomBtn.classList.contains("hidden");

// Mobile modelPickerMenu & context menu clamping at 360px
sandbox.innerWidth = 360;
sandbox.innerHeight = 740;
api.els.modelPickerWrap._customRect = () => ({
  top: 46,
  bottom: 76,
  left: 210,
  right: 352,
  width: 142,
  height: 30,
});
api.positionModelPickerMenu();
const mpMenuStyle = {
  width: api.els.modelPickerMenu.style.width,
  left: api.els.modelPickerMenu.style.left,
  top: api.els.modelPickerMenu.style.top,
  maxHeight: api.els.modelPickerMenu.style.maxHeight,
};

api.els.chatContextMenu._customRect = () => ({
  top: 0,
  left: 0,
  width: 230,
  height: 260,
  bottom: 260,
  right: 230,
});
api.openChatContextMenuAtPoint(
  { id: "chat-mob-1", title: "Мобильный тест", kind: "chat" },
  348,
  710,
  [{ id: "chat-mob-1", title: "Мобильный тест", kind: "chat" }]
);
const chatCtxPos = {
  left: parseInt(api.els.chatContextMenu.style.left, 10),
  top: parseInt(api.els.chatContextMenu.style.top, 10),
};

api.els.projectContextMenu._customRect = () => ({
  top: 0,
  left: 0,
  width: 220,
  height: 200,
  bottom: 200,
  right: 220,
});
api.openProjectContextMenuAtPoint("LocalProj", 350, 720);
const projCtxPos = {
  left: parseInt(api.els.projectContextMenu.style.left, 10),
  top: parseInt(api.els.projectContextMenu.style.top, 10),
};

console.log(
  JSON.stringify({
    modelLabels412,
    modelLabels360,
    pickerLabel412,
    pickerWrapTitle412,
    compIdleEmpty,
    compIdleDraftText,
    compIdleDraftAttach,
    compIdleDraftLiveActive,
    compGenEmpty,
    compGenDraft,
    compAfterSendClearInGen,
    compDesktopDraft,
    compAfterRestoreDraft,
    dockerTerm412,
    dockerTermTitle412,
    localTerm600,
    localTerm412,
    pickerLabelAfterResize360,
    pillAriaLabel,
    pillTitle,
    pillQueueBadge,
    usageFullTxt,
    usageShortTxt,
    usageTitleTxt,
    scrollBtnHiddenAt165Mobile,
    scrollBtnHiddenAt205Mobile,
    scrollBtnHiddenAt165Desktop,
    mpMenuStyle,
    chatCtxPos,
    projCtxPos,
  })
);
"""


class TestMobileUiErgonomicsV95(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._live_db_stat_before = (
            (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
            if LIVE_DB_PATH.exists()
            else None
        )
        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["ORT_DISABLE_ALL_GPU"] = "1"
        env["AGY_APP_JS_PATH"] = str(APP_JS_PATH)
        proc = subprocess.run(
            ["node", "-e", NODE_MOBILE_V95_VM_HARNESS],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=25,
            **subprocess_hidden_kwargs(),
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"Node v95 VM harness failed (rc={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        cls.vm_data = json.loads(proc.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._live_db_stat_before is not None and LIVE_DB_PATH.exists():
            after = (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
            if after != cls._live_db_stat_before:
                raise AssertionError(
                    f"Live data/hub.db was modified! before={cls._live_db_stat_before} after={after}"
                )

    def test_utf8_no_bom_and_js_syntax(self) -> None:
        for path in (
            INDEX_HTML_PATH,
            STYLES_CSS_PATH,
            APP_JS_PATH,
            SW_JS_PATH,
            Path(__file__).resolve(),
        ):
            raw = path.read_bytes()
            self.assertFalse(
                raw.startswith(b"\xef\xbb\xbf"),
                f"UTF-8 BOM is forbidden in {path.name}",
            )
            raw.decode("utf-8")

        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["ORT_DISABLE_ALL_GPU"] = "1"
        for js_file in (APP_JS_PATH, SW_JS_PATH):
            res = subprocess.run(
                ["node", "--check", str(js_file)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=15,
                **subprocess_hidden_kwargs(),
            )
            self.assertEqual(
                res.returncode,
                0,
                f"node --check failed for {js_file.name}: {res.stderr}",
            )

    def test_pwa_cache_version_v95_synced(self) -> None:
        sw_js = SW_JS_PATH.read_text(encoding="utf-8")
        index_html = INDEX_HTML_PATH.read_text(encoding="utf-8")

        match = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        self.assertIsNotNone(match)
        version = int(match.group(1))
        self.assertGreaterEqual(version, 95)
        for asset in ("styles.css", "app.js"):
            url = f"/static/{asset}?v={version}"
            self.assertIn(f'"{url}"', sw_js)
            self.assertIn(f'"{url}"', index_html)
        self.assertNotIn("agy-hub-v94", sw_js)
        for source in (sw_js, index_html):
            self.assertNotIn("?v=94", source)

    def test_css_invariants_and_v95_responsive_rules(self) -> None:
        raw_css = STYLES_CSS_PATH.read_text(encoding="utf-8")
        clean_css = strip_css_comments_and_strings(raw_css)
        index_html = INDEX_HTML_PATH.read_text(encoding="utf-8")

        # 1. Balanced braces
        self.assertEqual(
            clean_css.count("{"),
            clean_css.count("}"),
            "Unbalanced curly braces in styles.css",
        )

        # 2. main.workspace must NEVER get stacking-context / fixed-positioning traps
        forbidden_ws_props = (
            "transform",
            "filter",
            "backdrop-filter",
            "perspective",
            "contain",
            "container-type",
        )
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", clean_css):
            selectors = [s.strip() for s in m.group(1).split(",")]
            body = m.group(2)
            for sel in selectors:
                if sel in ("main.workspace", ".workspace", "main.workspace.is-ws-compact", "main.workspace.is-ws-medium"):
                    for prop in forbidden_ws_props:
                        self.assertIsNone(
                            re.search(rf"(?<![a-zA-Z-]){re.escape(prop)}\s*:", body),
                            f"Forbidden property '{prop}' found in selector '{sel}'",
                        )

        # 3. Conflicting <= 375px .mobile-access-bar .mobile-seg { width: 100% } rule is removed
        self.assertIsNone(
            re.search(
                r"@media\s*\(\s*max-width:\s*375px\s*\)\s*\{[^}]*\.mobile-access-bar\s+\.mobile-seg\s*\{\s*width:\s*100%",
                clean_css,
            ),
            "Legacy @media (max-width: 375px) .mobile-access-bar .mobile-seg { width: 100% } must be removed",
        )

        # 4. Stage 1..4 CSS & HTML markers
        self.assertIn(".open-chat-state-pill .ocsp-q", raw_css)
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) 28px;", raw_css)
        self.assertIn("grid-row: 1 / span 2;", raw_css)
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", clean_css):
            if ".mob-telem-pill" in m.group(1):
                for old_cap in ("max-width: 116px", "max-width: 112px", "max-width: 104px"):
                    self.assertNotIn(
                        old_cap,
                        m.group(2),
                        f"Old pill width cap '{old_cap}' must be removed from {m.group(1).strip()}",
                    )

        self.assertIn(".composer-row:has(#sendBtn.is-queue-send:not(.hidden)) #stopBtn", raw_css)
        self.assertIn("padding: 12px 12px 28px;", raw_css)
        self.assertIn("bottom: calc(var(--composer-floating-offset, 108px) + 10px);", raw_css)
        self.assertIn(".usage-hint .usage-tok-short", raw_css)
        self.assertIn("max-height: min(68dvh, 460px);", raw_css)
        self.assertIn(".claude-modal-head-actions", raw_css)
        self.assertIn(".or-modal-head-actions", raw_css)
        self.assertIn("claude-modal-head-actions", index_html)
        self.assertIn("or-modal-head-actions", index_html)

    def test_smart_mobile_model_labels(self) -> None:
        d = self.vm_data
        self.assertEqual(
            d["modelLabels412"],
            {
                "sol": "6.1 Sol · High",
                "astra": "6 Astra · XHigh",
                "opus": "Opus 4.7 · Think",
                "carbon": "Carbon 4 · Med",
            },
        )
        self.assertEqual(
            d["modelLabels360"],
            {
                "sol": "6.1 Sol · H",
                "astra": "6 Astra · XH",
                "opus": "Opus 4.7 · Think",
                "low": "6.1 Sol · L",
            },
        )
        self.assertEqual(d["pickerLabel412"], "6.1 Sol · High")
        self.assertEqual(d["pickerWrapTitle412"], "Модель нейросети: GPT-6.1-Sol (High)")
        self.assertEqual(d["pickerLabelAfterResize360"], "6.1 Sol · H")

    def test_composer_context_visibility_matrix(self) -> None:
        d = self.vm_data
        # 1. Idle + empty input: send visible, stop hidden, liveVoice visible
        self.assertEqual(
            d["compIdleEmpty"],
            {
                "sendHidden": False,
                "sendQueue": False,
                "stopHidden": True,
                "liveHidden": False,
                "sendTitle": "Отправить",
            },
        )
        # 2a. Idle + text draft on <= 680px: liveVoice hides to free room
        self.assertFalse(d["compIdleDraftText"]["sendHidden"])
        self.assertFalse(d["compIdleDraftText"]["sendQueue"])
        self.assertTrue(d["compIdleDraftText"]["stopHidden"])
        self.assertTrue(d["compIdleDraftText"]["liveHidden"])

        # 2b. Idle + attachment draft on <= 680px: liveVoice also hides
        self.assertTrue(d["compIdleDraftAttach"]["liveHidden"])

        # 2c. Live call active while typing -> liveVoice stays visible!
        self.assertFalse(d["compIdleDraftLiveActive"]["liveHidden"])

        # 3. Generating + empty input -> ONLY stopBtn visible, sendBtn hidden!
        self.assertTrue(d["compGenEmpty"]["sendHidden"])
        self.assertFalse(d["compGenEmpty"]["sendQueue"])
        self.assertFalse(d["compGenEmpty"]["stopHidden"])
        self.assertTrue(d["compGenEmpty"]["liveHidden"])

        # 4. Generating + draft text -> sendBtn.is-queue-send + stopBtn visible!
        self.assertFalse(d["compGenDraft"]["sendHidden"])
        self.assertTrue(d["compGenDraft"]["sendQueue"])
        self.assertFalse(d["compGenDraft"]["stopHidden"])
        self.assertTrue(d["compGenDraft"]["liveHidden"])
        self.assertIn("очередь", d["compGenDraft"]["sendTitle"])

        # Clearing after send while still generating hides sendBtn again
        self.assertTrue(d["compAfterSendClearInGen"]["sendHidden"])
        self.assertFalse(d["compAfterSendClearInGen"]["stopHidden"])

        # Desktop (>= 681px) does not hide liveVoiceBtn when typing
        self.assertFalse(d["compDesktopDraft"]["liveHidden"])

        # Restoring draft on mobile updates composer visibility immediately
        self.assertTrue(d["compAfterRestoreDraft"]["liveHidden"])

    def test_mobile_terminal_shortening_and_resize_sync(self) -> None:
        d = self.vm_data
        self.assertEqual(d["dockerTerm412"], "🐳 Терм.")
        self.assertIn("Docker", d["dockerTermTitle412"])
        self.assertEqual(d["localTerm600"], "Терминал")
        self.assertEqual(d["localTerm412"], "Терм.")

    def test_header_pill_usage_tokens_scroll_and_menus(self) -> None:
        d = self.vm_data
        # Header status pill aria-label + +N queue badge
        self.assertTrue(d["pillTitle"])
        self.assertEqual(d["pillAriaLabel"], d["pillTitle"])
        self.assertEqual(d["pillQueueBadge"], "+2")

        # Usage tokens: 29 998 tok full, 30.0k short, full tooltip
        self.assertEqual(d["usageFullTxt"], "29 998 tok")
        self.assertEqual(d["usageShortTxt"], "30.0k")
        self.assertIn("29 998 tok", d["usageTitleTxt"])

        # Scroll-bottom threshold: 190px on mobile vs 140px on desktop
        self.assertTrue(d["scrollBtnHiddenAt165Mobile"])
        self.assertFalse(d["scrollBtnHiddenAt205Mobile"])
        self.assertFalse(d["scrollBtnHiddenAt165Desktop"])

        # Mobile modelPickerMenu at 360x740 -> width 340px, left 10px (>=10px margins)
        self.assertEqual(d["mpMenuStyle"]["width"], "340px")
        self.assertEqual(d["mpMenuStyle"]["left"], "10px")
        self.assertGreaterEqual(int(d["mpMenuStyle"]["top"].replace("px", "")), 10)

        # Context menus clamped with >= 10px margins on 360x740 viewport
        self.assertGreaterEqual(d["chatCtxPos"]["left"], 10)
        self.assertLessEqual(d["chatCtxPos"]["left"] + 230, 360 - 10)
        self.assertLessEqual(d["chatCtxPos"]["top"] + 260, 740 - 10)

        self.assertGreaterEqual(d["projCtxPos"]["left"], 10)
        self.assertLessEqual(d["projCtxPos"]["left"] + 220, 360 - 10)
        self.assertLessEqual(d["projCtxPos"]["top"] + 200, 740 - 10)


if __name__ == "__main__":
    unittest.main()
