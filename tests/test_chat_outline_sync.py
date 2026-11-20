"""
Isolated unit, DOM-geometry & static invariant tests for Antigravity Hub v=89:
Contextual scroll synchronization & Studio visual polish for `#chatOutlinePopover` ("Содержание"):
1. 99 turns + pinned `📌` (`state.outline.pinned = true`): opening a 99-turn chat (both cached
   and async empty->loaded) and switching between long chats positions `#chatOutlineList.scrollTop`
   at the active 99th turn (`maxScroll`), never stuck at `01` or `20-24`.
2. Synchronous ascent & descent (`99 -> 50 -> 01 -> 99`): scrolling `#messagesContainer` moves
   `#chatOutlineList.scrollTop` monotonically, keeps active rows on the `36%` comfort line, hits
   `0` at `atTopEdge` and `maxScroll` at `atBottomEdge`, and respects `anchorY` on long answers
   in `follow_answer_start` (`distFromBottom > 16`).
3. Sub-heading (`↳`) scroll-spy inside a long Markdown reply: scrolling across 6 `## Раздел 1..6`
   headings switches `.outline-heading-btn.is-active` sequentially and smoothly advances
   `#chatOutlineList.scrollTop`.
4. Anti-Conflict Guard + `#outlineSyncActiveBtn`: manual `wheel`/hover/search inside
   `#chatOutlineList` protects user scroll against background chat updates and displays
   `#outlineSyncActiveBtn` (`К текущему · 99`) when the active row leaves the viewport; user
   scrolling `#messagesContainer` or clicking `#outlineSyncActiveBtn` immediately re-syncs and hides it.
5. Zero `innerHTML` thrashing & static invariants: `.outline-turn-group` and `.outline-heading-btn`
   nodes preserve strict JS reference identity (`===`) during live scroll, JS syntax passes
   `node --check`, `?v=N` and `agy-hub-vN` match (`v>=89`), CSS braces are balanced, and
   `main.workspace` sets no `transform`, `filter`, `perspective`, or `container-type`.
Strictly 0% GPU / 0 MB VRAM, 0 console popups on Windows 11, and 0 touches to `data/hub.db`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
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


NODE_OUTLINE_VM_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

const appJsPath = process.env.AGY_APP_JS_PATH;
const scenario = process.env.AGY_OUTLINE_SCENARIO || "all";
let src = fs.readFileSync(appJsPath, "utf8");

const tailTarget = /init\(\);\s*\}\)\(\);\s*$/;
if (!tailTarget.test(src)) {
  throw new Error("Could not locate IIFE tail init() in app.js");
}
src = src.replace(
  tailTarget,
  `globalThis.__AGY_OUTLINE_TEST__ = {
    bindEvents,
    state,
    els,
    scrollGuard,
    computeVisibleOutlineContext,
    computeVisibleOutlineTurnNum,
    syncOutlineActiveContextDom,
    syncOutlineActiveTurnDom,
    syncOutlineScrollToContext,
    scheduleOutlineScrollSync,
    updateOutlineSyncChipVisibility,
    isOutlineAutoSyncPaused,
    buildOutlineTurnHeadingsHtml,
    buildOutlineTurnHeadingsInnerHtml,
    renderChatOutlinePopoverContent,
    openChatOutline,
    closeChatOutline,
    toggleChatOutline,
    toggleOutlinePin,
    refreshChatOutline,
    handleMessagesContainerScrollForOutline,
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
        if (set.has(c)) { set.delete(c); return false; }
        if (c) set.add(c);
        return true;
      }
      if (force) { if (c) set.add(c); } else { set.delete(c); }
      return Boolean(force);
    },
    contains: (c) => set.has(c),
    toString: () => Array.from(set).join(" "),
    _resetFromString: (s) => {
      set.clear();
      String(s || "").split(/\s+/).filter(Boolean).forEach((c) => set.add(c));
    },
  };
}

function splitTopLevelCommas(selector) {
  const parts = [];
  let buf = "";
  let bracketDepth = 0;
  let parenDepth = 0;
  let quote = "";
  for (let i = 0; i < selector.length; i++) {
    const ch = selector[i];
    if (quote) {
      buf += ch;
      if (ch === "\\" && i + 1 < selector.length) {
        buf += selector[++i];
      } else if (ch === quote) {
        quote = "";
      }
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      buf += ch;
    } else if (ch === "[") {
      bracketDepth++;
      buf += ch;
    } else if (ch === "]") {
      bracketDepth = Math.max(0, bracketDepth - 1);
      buf += ch;
    } else if (ch === "(") {
      parenDepth++;
      buf += ch;
    } else if (ch === ")") {
      parenDepth = Math.max(0, parenDepth - 1);
      buf += ch;
    } else if (ch === "," && bracketDepth === 0 && parenDepth === 0) {
      if (buf.trim()) parts.push(buf.trim());
      buf = "";
    } else {
      buf += ch;
    }
  }
  if (buf.trim()) parts.push(buf.trim());
  return parts;
}

function splitDescendantTokens(selector) {
  const tokens = [];
  let buf = "";
  let bracketDepth = 0;
  let parenDepth = 0;
  let quote = "";
  for (let i = 0; i < selector.length; i++) {
    const ch = selector[i];
    if (quote) {
      buf += ch;
      if (ch === "\\" && i + 1 < selector.length) {
        buf += selector[++i];
      } else if (ch === quote) {
        quote = "";
      }
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      buf += ch;
    } else if (ch === "[") {
      bracketDepth++;
      buf += ch;
    } else if (ch === "]") {
      bracketDepth = Math.max(0, bracketDepth - 1);
      buf += ch;
    } else if (ch === "(") {
      parenDepth++;
      buf += ch;
    } else if (ch === ")") {
      parenDepth = Math.max(0, parenDepth - 1);
      buf += ch;
    } else if (/\s/.test(ch) && bracketDepth === 0 && parenDepth === 0) {
      if (buf) {
        tokens.push(buf);
        buf = "";
      }
    } else {
      buf += ch;
    }
  }
  if (buf) tokens.push(buf);
  return tokens;
}

function matchesSimpleCompound(el, compound) {
  if (!el || !compound) return false;
  let rest = compound;

  // Handle :not(...)
  const notRegex = /:not\(([^)]+)\)/g;
  let notMatch;
  const negations = [];
  while ((notMatch = notRegex.exec(rest)) !== null) {
    negations.push(notMatch[1].trim());
  }
  rest = rest.replace(notRegex, "");
  for (const neg of negations) {
    if (matchesSimpleCompound(el, neg)) return false;
  }

  // Handle [attr] or [attr="val"]
  const attrRegex = /\[\s*([a-zA-Z0-9_-]+)(?:\s*=\s*(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|([^\]]+)))?\s*\]/g;
  let attrMatch;
  while ((attrMatch = attrRegex.exec(rest)) !== null) {
    const attrName = attrMatch[1];
    const hasVal = attrMatch[2] !== undefined || attrMatch[3] !== undefined || attrMatch[4] !== undefined;
    const rawExpected = attrMatch[2] ?? attrMatch[3] ?? attrMatch[4] ?? "";
    const expected = rawExpected.replace(/\\(.)/g, "$1");
    const dsKey = attrToDatasetKey(attrName);
    let actual = el.getAttribute(attrName);
    if (actual === null && dsKey && el.dataset && el.dataset[dsKey] !== undefined) {
      actual = String(el.dataset[dsKey]);
    }
    if (actual === null) return false;
    if (hasVal && String(actual) !== expected) return false;
  }
  rest = rest.replace(attrRegex, "");

  // Handle #id
  const idMatch = rest.match(/#([a-zA-Z0-9_-]+)/);
  if (idMatch) {
    if (el.id !== idMatch[1]) return false;
    rest = rest.replace(/#[a-zA-Z0-9_-]+/, "");
  }

  // Handle .class
  const clsRegex = /\.([a-zA-Z0-9_-]+)/g;
  let clsMatch;
  while ((clsMatch = clsRegex.exec(rest)) !== null) {
    if (!el.classList || !el.classList.contains(clsMatch[1])) return false;
  }
  rest = rest.replace(clsRegex, "").trim();

  // Handle tagName
  if (rest && rest !== "*") {
    if (String(el.tagName || "").toUpperCase() !== rest.toUpperCase()) return false;
  }
  return true;
}

function matchesSelectorChain(el, chain, stopRoot = null) {
  if (!chain.length) return false;
  if (!matchesSimpleCompound(el, chain[chain.length - 1])) return false;
  let cur = el.parentElement;
  for (let idx = chain.length - 2; idx >= 0; idx--) {
    while (cur && cur !== stopRoot) {
      if (matchesSimpleCompound(cur, chain[idx])) break;
      cur = cur.parentElement;
    }
    if (!cur || cur === stopRoot) return false;
    cur = cur.parentElement;
  }
  return true;
}

function matchesSelector(el, selector, scopeRoot = null) {
  const groups = splitTopLevelCommas(String(selector || ""));
  for (const g of groups) {
    const chain = splitDescendantTokens(g);
    if (matchesSelectorChain(el, chain, scopeRoot ? scopeRoot.parentElement : null)) {
      return true;
    }
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

const VOID_TAGS = new Set(["BR", "HR", "IMG", "INPUT", "META", "LINK", "PATH", "CIRCLE", "RECT", "LINE", "POLYLINE", "POLYGON"]);

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

let outlineLayoutDirty = true;

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
    scrollTop: 0,
    _scrollHeight: 600,
    clientHeight: 600,
    clientWidth: 800,
    offsetWidth: 300,
    parentElement: null,
    children,
    childNodes: children,
    _ownText: "",
    _geom: null,
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
      outlineLayoutDirty = true;
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
      outlineLayoutDirty = true;
    },
    get scrollHeight() {
      if (el.id === "chatOutlineList") {
        ensureOutlineListLayout();
      }
      return el._scrollHeight;
    },
    set scrollHeight(v) {
      el._scrollHeight = Number(v) || 0;
    },
    value: "",
    setAttribute(name, val) {
      const sVal = String(val ?? "");
      attrs.set(name, sVal);
      if (name === "id") el.id = sVal;
      else if (name === "class") classList._resetFromString(sVal);
      else if (name === "title") el.title = sVal;
      else {
        const dsKey = attrToDatasetKey(name);
        if (dsKey) el.dataset[dsKey] = sVal;
      }
    },
    getAttribute(name) {
      if (name === "class") return classList.toString() || null;
      if (name === "id") return el.id || null;
      return attrs.has(name) ? attrs.get(name) : null;
    },
    removeAttribute(name) {
      attrs.delete(name);
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
      outlineLayoutDirty = true;
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
      outlineLayoutDirty = true;
      return newNode;
    },
    replaceChildren(...nodes) {
      children.length = 0;
      for (const n of nodes) el.appendChild(n);
      outlineLayoutDirty = true;
    },
    remove() {
      if (el.parentElement) {
        const idx = el.parentElement.children.indexOf(el);
        if (idx >= 0) el.parentElement.children.splice(idx, 1);
        el.parentElement = null;
        outlineLayoutDirty = true;
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
        if (matchesSelector(node, selector, el)) {
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
        if (matchesSelector(node, selector, el)) {
          results.push(node);
        }
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
    click() {
      el.dispatch("click", { target: el });
    },
    focus() {
      activeEl = el;
    },
    blur() {
      if (activeEl === el) activeEl = null;
    },
    select() {},
    setSelectionRange() {},
    scrollTo(opts) {
      if (opts && typeof opts.top === "number") {
        el.scrollTop = opts.top;
      }
    },
    getBoundingClientRect() {
      if (typeof el._customRect === "function") {
        return el._customRect();
      }
      const listEl = byId.get("chatOutlineList");
      if (listEl && (el === listEl || listEl.contains(el))) {
        ensureOutlineListLayout();
        if (el === listEl) {
          return {
            top: 80,
            left: 1120,
            width: 300,
            height: listEl.clientHeight,
            bottom: 80 + listEl.clientHeight,
            right: 1420,
          };
        }
        if (el._geom) {
          const top = 80 + el._geom.offsetTop - listEl.scrollTop;
          return {
            top,
            left: 1128,
            width: 284,
            height: el._geom.height,
            bottom: top + el._geom.height,
            right: 1412,
          };
        }
      }
      return { top: 0, left: 0, width: 800, height: 600, bottom: 600, right: 800 };
    },
  };
  return el;
}

const byId = new Map();
function getElById(id) {
  if (!byId.has(id)) {
    byId.set(id, makeDomEl(id, id === "promptInput" ? "TEXTAREA" : id === "chatOutlineSearchInput" ? "INPUT" : "DIV"));
  }
  return byId.get(id);
}

// Wire realistic parent-child hierarchy for #chatOutlinePopover
const popoverEl = getElById("chatOutlinePopover");
const listElInit = getElById("chatOutlineList");
const syncBtnInit = getElById("outlineSyncActiveBtn");
const syncLabelInit = getElById("outlineSyncActiveLabel");
syncBtnInit.classList.add("hidden");
syncBtnInit.appendChild(syncLabelInit);
popoverEl.appendChild(getElById("chatOutlineSearchInput"));
popoverEl.appendChild(getElById("chatOutlineSearchClearBtn"));
popoverEl.appendChild(listElInit);
popoverEl.appendChild(syncBtnInit);

listElInit.clientHeight = 420;
listElInit._scrollHeight = 420;

function ensureOutlineListLayout() {
  const listEl = getElById("chatOutlineList");
  let y = 2; // top padding
  const kids = listEl.children;
  for (let i = 0; i < kids.length; i++) {
    const child = kids[i];
    if (child.classList.contains("outline-turn-group")) {
      const groupStart = y;
      const row = child.querySelector(".outline-turn-row");
      if (row) {
        row._geom = { offsetTop: y, height: 30 };
        y += 30;
      }
      const hList = child.querySelector(".outline-headings-list");
      if (hList && hList.children.length > 0) {
        const hStart = y;
        y += 1;
        for (let k = 0; k < hList.children.length; k++) {
          const hItem = hList.children[k];
          const itemH = hItem.classList.contains("outline-more-headings-btn") ? 20 : 22;
          hItem._geom = { offsetTop: y, height: itemH };
          y += itemH + (k < hList.children.length - 1 ? 1 : 0);
        }
        y += 3;
        hList._geom = { offsetTop: hStart, height: y - hStart };
      }
      child._geom = { offsetTop: groupStart, height: Math.max(30, y - groupStart) };
      y += 1.5;
    } else {
      child._geom = { offsetTop: y, height: 24 };
      y += 25.5;
    }
  }
  const padBottom = listEl.classList.contains("has-sync-chip") ? 42 : 14;
  y += padBottom;
  listEl._scrollHeight = Math.max(listEl.clientHeight, Math.ceil(y));
  outlineLayoutDirty = false;
}

let activeEl = null;
const docListeners = new Map();
const winListeners = new Map();

const docStub = {
  hidden: false,
  documentElement: makeDomEl("html", "HTML"),
  body: makeDomEl("body", "BODY"),
  get activeElement() {
    return activeEl;
  },
  getElementById: (id) => getElById(id),
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (t) => makeDomEl("", String(t || "DIV").toUpperCase()),
  createDocumentFragment: () => makeDomEl("", "FRAG"),
  addEventListener: (ev, fn) => {
    if (!docListeners.has(ev)) docListeners.set(ev, []);
    docListeners.get(ev).push(fn);
  },
  dispatch: (ev, eventObj = {}) => {
    const evt = Object.assign(
      { type: ev, target: docStub.body, preventDefault: () => {}, stopPropagation: () => {} },
      eventObj
    );
    const arr = docListeners.get(ev) || [];
    for (const fn of arr) fn(evt);
    return evt;
  },
};

let fakeNow = 5000;
let rafCounter = 1;
const rafMap = new Map();

function flushRaf() {
  let guard = 0;
  while (rafMap.size > 0 && guard < 30) {
    guard++;
    const entries = Array.from(rafMap.entries());
    rafMap.clear();
    for (const [, cb] of entries) {
      cb(fakeNow);
    }
  }
}

class FakeResizeObserver {
  observe() {}
  disconnect() {}
}

const sandbox = {
  console,
  innerWidth: 1440,
  innerHeight: 900,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  URL,
  URLSearchParams,
  localStorage: localStorageStub,
  performance: { now: () => fakeNow },
  ResizeObserver: FakeResizeObserver,
  WebSocket: { OPEN: 1, CONNECTING: 0, CLOSING: 2, CLOSED: 3 },
  navigator: { userAgent: "node-outline-test", clipboard: {} },
  location: { protocol: "http:", host: "127.0.0.1:8765", origin: "http://127.0.0.1:8765", pathname: "/" },
  CSS: { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&") },
  matchMedia: (query) => {
    const q = String(query || "");
    const maxM = q.match(/max-width:\s*(\d+)px/);
    if (maxM) {
      return { matches: sandbox.innerWidth <= Number(maxM[1]), addEventListener: () => {} };
    }
    const minM = q.match(/min-width:\s*(\d+)px/);
    if (minM) {
      return { matches: sandbox.innerWidth >= Number(minM[1]), addEventListener: () => {} };
    }
    return { matches: q.includes("hover: hover"), addEventListener: () => {} };
  },
  requestAnimationFrame: (fn) => {
    const id = rafCounter++;
    rafMap.set(id, fn);
    return id;
  },
  cancelAnimationFrame: (id) => {
    rafMap.delete(id);
  },
  document: docStub,
  addEventListener: (ev, fn) => {
    if (!winListeners.has(ev)) winListeners.set(ev, []);
    winListeners.get(ev).push(fn);
  },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });

const api = sandbox.__AGY_OUTLINE_TEST__;
const mc = api.els.messagesContainer;
const ml = api.els.messagesList;
const outlineList = api.els.chatOutlineList;
const syncBtn = api.els.outlineSyncActiveBtn;
const syncLabel = api.els.outlineSyncActiveLabel;

// Configure #messagesContainer viewport: top=60, height=720 -> bottom=780
// Reading line anchorY = 60 + min(136, max(48, 720 * 0.28)) = 60 + 136 = 196
mc.clientHeight = 720;
mc._customRect = () => ({
  top: 60,
  left: 280,
  width: 820,
  height: mc.clientHeight,
  bottom: 60 + mc.clientHeight,
  right: 1100,
});
mc.appendChild(ml);

api.bindEvents();

function mountChatFixture(chatId, turnSpecs) {
  const messages = [];
  ml.children.length = 0;
  let y = 16;

  for (let i = 0; i < turnSpecs.length; i++) {
    const spec = turnSpecs[i];
    const tNum = i + 1;
    const uId = `${chatId}-u-${tNum}`;
    const aId = `${chatId}-a-${tNum}`;
    const userH = spec.userHeight || 72;
    const assistH = spec.assistantHeight || 190;
    const headings = Array.isArray(spec.headings) ? spec.headings : [];

    messages.push({
      id: uId,
      role: "user",
      content: spec.userText || `Вопрос пользователя #${tNum}: проверка синхронизации`,
    });

    let mdContent = spec.assistantIntro || `Ответ ассистента на ход #${tNum}.`;
    if (headings.length > 0) {
      mdContent += "\n\n" + headings.map((h) => `${"#".repeat(h.level || 2)} ${h.text}\nТекст секции.`).join("\n\n");
    }
    messages.push({
      id: aId,
      role: "assistant",
      content: mdContent,
    });

    const uRow = makeDomEl("", "DIV");
    uRow.className = "msg-row user";
    uRow.setAttribute("data-msg-id", uId);
    const uTop = y;
    uRow._offsetTop = uTop;
    uRow._height = userH;
    uRow._customRect = () => {
      const top = 60 + uTop - mc.scrollTop;
      return { top, left: 300, width: 760, height: userH, bottom: top + userH, right: 1060 };
    };
    ml.appendChild(uRow);
    y += userH + 12;

    const aRow = makeDomEl("", "DIV");
    aRow.className = "msg-row assistant";
    aRow.setAttribute("data-msg-id", aId);
    const aTop = y;
    aRow._offsetTop = aTop;
    aRow._height = assistH;
    aRow._customRect = () => {
      const top = 60 + aTop - mc.scrollTop;
      return { top, left: 300, width: 760, height: assistH, bottom: top + assistH, right: 1060 };
    };

    const mdBody = makeDomEl("", "DIV");
    mdBody.className = "markdown-body";
    aRow.appendChild(mdBody);

    for (let hIdx = 0; hIdx < headings.length; hIdx++) {
      const hSpec = headings[hIdx];
      const tag = `H${hSpec.level || 2}`;
      const hEl = makeDomEl("", tag);
      hEl.textContent = hSpec.text;
      const relY = hSpec.offsetY !== undefined ? hSpec.offsetY : 90 + hIdx * 280;
      hEl._customRect = () => {
        const top = 60 + aTop + relY - mc.scrollTop;
        return { top, left: 316, width: 720, height: 28, bottom: top + 28, right: 1036 };
      };
      mdBody.appendChild(hEl);
    }

    ml.appendChild(aRow);
    y += assistH + 20;
  }

  mc.scrollHeight = Math.max(mc.clientHeight, y + 24);
  return { messages, totalHeight: mc.scrollHeight };
}

function makeSimpleTurns(count, overrides = {}) {
  const arr = [];
  for (let i = 1; i <= count; i++) {
    arr.push(overrides[i] || {});
  }
  return arr;
}

const out = {};

// ---------------------------------------------------------------------------
// Case 1: 99 turns + pinned 📌 (direct open, async empty->99 load, chat switch)
// ---------------------------------------------------------------------------
{
  api.state.outline.pinned = true;
  sandbox.innerWidth = 1440;

  // 1A: Direct cached open of 99-turn chat positioned at bottom (turn 99)
  const chatA = mountChatFixture("chat99A", makeSimpleTurns(99));
  api.state.activeChatId = "chat99A";
  api.state.activeChat = { id: "chat99A", kind: "chat" };
  api.state.messages = chatA.messages;
  mc.scrollTop = mc.scrollHeight - mc.clientHeight;
  outlineList.scrollTop = 580; // stale scroll around turns 20-24

  api.refreshChatOutline();
  flushRaf();

  const maxScrollA = outlineList.scrollHeight - outlineList.clientHeight;
  out.case1DirectActiveTurn = api.state.outline.activeTurnNum;
  out.case1DirectScrollTop = outlineList.scrollTop;
  out.case1DirectMaxScroll = maxScrollA;
  out.case1DirectIsOpen = api.state.outline.isOpen;

  // 1B: Uncached async load simulation (openHubChat sets activeChatId with empty messages first,
  // then renderMessages populates 99 messages and calls refreshChatOutline with same chatKey)
  outlineList.scrollTop = 610; // simulate stale offset ~turn 20-24
  api.state.activeChatId = "chat99Async";
  api.state.activeChat = { id: "chat99Async", kind: "chat" };
  api.state.messages = [];
  ml.children.length = 0;
  api.refreshChatOutline();
  flushRaf();

  const chatAsync = mountChatFixture("chat99Async", makeSimpleTurns(99));
  api.state.messages = chatAsync.messages;
  outlineList.scrollTop = 610; // ensure pre-sync is still stale
  mc.scrollTop = mc.scrollHeight - mc.clientHeight;
  api.refreshChatOutline();
  flushRaf();

  const maxScrollAsync = outlineList.scrollHeight - outlineList.clientHeight;
  out.case1AsyncActiveTurn = api.state.outline.activeTurnNum;
  out.case1AsyncScrollTop = outlineList.scrollTop;
  out.case1AsyncMaxScroll = maxScrollAsync;

  // 1C: Switch to second long chat (85 turns) where user is reading turn 55, then switch back to 99
  const chatB = mountChatFixture("chat85B", makeSimpleTurns(85));
  api.state.activeChatId = "chat85B";
  api.state.activeChat = { id: "chat85B", kind: "chat" };
  api.state.messages = chatB.messages;
  // Place turn 55 user row right above anchorY (anchorY=196 -> offset = rowTop - (196 - 60) + 10)
  const turn55Row = ml.querySelector('.msg-row[data-msg-id="chat85B-u-55"]');
  mc.scrollTop = turn55Row._offsetTop - 100;
  api.refreshChatOutline();
  flushRaf();

  out.case1SwitchBActiveTurn = api.state.outline.activeTurnNum;
  out.case1SwitchBScrollTop = outlineList.scrollTop;
  const row55 = outlineList.querySelector('.outline-turn-group[data-outline-group-turn="55"] .outline-turn-row');
  const listRectB = outlineList.getBoundingClientRect();
  const row55Rect = row55.getBoundingClientRect();
  out.case1SwitchBRowCenterRatio = ((row55Rect.top + row55Rect.height * 0.5) - listRectB.top) / listRectB.height;
}

// ---------------------------------------------------------------------------
// Case 2: Synchronous ascent & descent (99 -> 80 -> 50 -> 25 -> 1 -> 99)
//         + follow_answer_start long final answer check
// ---------------------------------------------------------------------------
{
  api.state.outline.pinned = true;
  const chat2 = mountChatFixture("chatSync99", makeSimpleTurns(99));
  api.state.activeChatId = "chatSync99";
  api.state.activeChat = { id: "chatSync99", kind: "chat" };
  api.state.messages = chat2.messages;
  mc.scrollTop = mc.scrollHeight - mc.clientHeight;
  api.refreshChatOutline();
  flushRaf();

  const maxScroll2 = outlineList.scrollHeight - outlineList.clientHeight;
  const stepsUp = [99, 80, 50, 25, 10, 1];
  const recordedScrolls = [];
  const recordedTurns = [];
  const comfortRatios = {};

  for (const t of stepsUp) {
    fakeNow += 50;
    if (t === 99) {
      mc.scrollTop = mc.scrollHeight - mc.clientHeight;
    } else if (t === 1) {
      mc.scrollTop = 0;
    } else {
      const uRow = ml.querySelector(`.msg-row[data-msg-id="chatSync99-u-${t}"]`);
      mc.scrollTop = uRow._offsetTop - 90;
    }
    mc.dispatch("scroll");
    flushRaf();
    recordedTurns.push(api.state.outline.activeTurnNum);
    recordedScrolls.push(outlineList.scrollTop);
    if (t === 80 || t === 50 || t === 25) {
      const rEl = outlineList.querySelector(`.outline-turn-group[data-outline-group-turn="${t}"] .outline-turn-row`);
      const lRect = outlineList.getBoundingClientRect();
      const rRect = rEl.getBoundingClientRect();
      comfortRatios[t] = ((rRect.top + rRect.height * 0.5) - lRect.top) / lRect.height;
    }
  }

  // Descend back to 99
  fakeNow += 50;
  mc.scrollTop = mc.scrollHeight - mc.clientHeight;
  mc.dispatch("scroll");
  flushRaf();

  out.case2MaxScroll = maxScroll2;
  out.case2TurnsUp = recordedTurns;
  out.case2ScrollsUp = recordedScrolls;
  out.case2ComfortRatios = comfortRatios;
  out.case2ReturnBottomTurn = api.state.outline.activeTurnNum;
  out.case2ReturnBottomScroll = outlineList.scrollTop;

  // Long 99th answer in follow_answer_start where turn 98 is still on anchorY and distFromBottom > 16
  const chatLongEnd = mountChatFixture(
    "chatLongEnd",
    makeSimpleTurns(99, { 99: { assistantHeight: 2400 } })
  );
  api.state.activeChatId = "chatLongEnd";
  api.state.activeChat = { id: "chatLongEnd", kind: "chat" };
  api.state.messages = chatLongEnd.messages;
  const u98 = ml.querySelector('.msg-row[data-msg-id="chatLongEnd-u-98"]');
  mc.scrollTop = u98._offsetTop - 80; // anchorY sits on turn 98 while turn 99 starts lower & extends 2400px
  api.refreshChatOutline();
  flushRaf();
  const longCtx = api.computeVisibleOutlineContext();
  out.case2LongAnswerAnchorTurn = longCtx.turnNum;
  out.case2LongAnswerAtBottom = longCtx.atBottomEdge;
}

// ---------------------------------------------------------------------------
// Case 3: Sub-heading (↳) scroll-spy inside a long reply with 6 headings
// ---------------------------------------------------------------------------
{
  const sixHeadings = [1, 2, 3, 4, 5, 6].map((n, idx) => ({
    level: 2,
    text: `Раздел ${n}`,
    offsetY: 160 + idx * 320,
  }));
  const chat3 = mountChatFixture(
    "chatHeadings",
    makeSimpleTurns(12, {
      5: {
        assistantHeight: 2300,
        assistantIntro: "Вводная часть ТЗ перед первым разделом.",
        headings: sixHeadings,
      },
    })
  );
  api.state.activeChatId = "chatHeadings";
  api.state.activeChat = { id: "chatHeadings", kind: "chat" };
  api.state.messages = chat3.messages;
  api.state.outline.pinned = true;

  const aRow5 = ml.querySelector('.msg-row[data-msg-id="chatHeadings-a-5"]');
  // Start at intro paragraph of Turn 5 (above Раздел 1: anchorY+28 = 224 -> 60 + aTop + 160 - scrollTop > 224)
  mc.scrollTop = aRow5._offsetTop - 40;
  api.refreshChatOutline();
  flushRaf();

  const introActiveTurn = api.state.outline.activeTurnNum;
  const introHeadingKey = api.state.outline.activeHeadingKey;
  const group5BtnsAtIntro = outlineList.querySelectorAll('.outline-turn-group[data-outline-group-turn="5"] .outline-heading-btn');

  const activeKeysSeq = [];
  const activeDomKeysSeq = [];
  const headingScrollsSeq = [];

  for (let i = 0; i < 6; i++) {
    fakeNow += 40;
    // Place heading i just above anchorY (anchorY = 196 -> 60 + aTop + offsetY - scrollTop = 180)
    mc.scrollTop = aRow5._offsetTop + sixHeadings[i].offsetY - 120;
    mc.dispatch("scroll");
    flushRaf();
    activeKeysSeq.push(api.state.outline.activeHeadingKey);
    const actBtns = outlineList.querySelectorAll('.outline-turn-group[data-outline-group-turn="5"] .outline-heading-btn.is-active');
    activeDomKeysSeq.push(actBtns.length === 1 ? actBtns[0].dataset.outlineHeadingKey : `COUNT_${actBtns.length}`);
    headingScrollsSeq.push(outlineList.scrollTop);
  }

  out.case3IntroTurn = introActiveTurn;
  out.case3IntroHeadingKey = introHeadingKey;
  out.case3ExpandedHeadingBtnCount = group5BtnsAtIntro.length;
  out.case3ActiveKeysSeq = activeKeysSeq;
  out.case3ActiveDomKeysSeq = activeDomKeysSeq;
  out.case3HeadingScrollsSeq = headingScrollsSeq;
}

// ---------------------------------------------------------------------------
// Case 4: Anti-Conflict Guard + #outlineSyncActiveBtn
// ---------------------------------------------------------------------------
{
  const chat4 = mountChatFixture("chatGuard99", makeSimpleTurns(99));
  api.state.activeChatId = "chatGuard99";
  api.state.activeChat = { id: "chatGuard99", kind: "chat" };
  api.state.messages = chat4.messages;
  api.state.outline.pinned = true;
  mc.scrollTop = mc.scrollHeight - mc.clientHeight;
  api.refreshChatOutline();
  flushRaf();

  const initialBtnHidden = syncBtn.classList.contains("hidden");
  const initialMaxScroll = outlineList.scrollHeight - outlineList.clientHeight;

  // User wheels inside #chatOutlineList up to ~turn 18-20 (scrollTop = 520)
  fakeNow += 100;
  outlineList.dispatch("pointerenter");
  outlineList.dispatch("wheel", { deltaY: -240 });
  outlineList.scrollTop = 520;
  outlineList.dispatch("scroll");
  outlineList.dispatch("pointerleave");

  const btnVisibleAfterManualUp = !syncBtn.classList.contains("hidden");
  const btnLabelAfterManualUp = syncLabel.textContent;
  const listHasChipClass = outlineList.classList.contains("has-sync-chip");

  // Background chat update (streaming delta / preserveOpenScroll + programmatic chat scroll) while hold is active
  fakeNow += 200; // still well within 1800ms manualScrollHoldUntil
  api.refreshChatOutline({ preserveOpenScroll: true });
  api.scrollGuard.programmaticUntil = fakeNow + 250;
  mc.dispatch("scroll");
  flushRaf();

  const scrollPreservedDuringHold = outlineList.scrollTop;
  const btnStillVisibleDuringHold = !syncBtn.classList.contains("hidden");

  // 4A: Clicking #outlineSyncActiveBtn immediately syncs back to turn 99 and hides chip
  fakeNow += 50;
  syncBtn.click();
  flushRaf();

  const scrollAfterChipClick = outlineList.scrollTop;
  const btnHiddenAfterChipClick = syncBtn.classList.contains("hidden");
  const chipClassRemovedAfterClick = !outlineList.classList.contains("has-sync-chip");

  // 4B: Manual wheel up again to 520, then user scrolls #messagesContainer directly (outside popover)
  fakeNow += 100;
  outlineList.dispatch("wheel", { deltaY: -240 });
  outlineList.scrollTop = 520;
  outlineList.dispatch("scroll");
  const btnVisibleBeforeChatScroll = !syncBtn.classList.contains("hidden");

  fakeNow += 100; // still inside 1800ms hold window, but user scrolls chat!
  api.scrollGuard.programmaticUntil = 0;
  const u75 = ml.querySelector('.msg-row[data-msg-id="chatGuard99-u-75"]');
  mc.scrollTop = u75._offsetTop - 90;
  mc.dispatch("wheel", { deltaY: -120, target: mc });
  mc.dispatch("scroll");
  flushRaf();

  const turnAfterUserChatScroll = api.state.outline.activeTurnNum;
  const scrollChangedFrom520 = outlineList.scrollTop !== 520 && outlineList.scrollTop > 1500;
  const btnHiddenAfterUserChatScroll = syncBtn.classList.contains("hidden");

  out.case4InitialBtnHidden = initialBtnHidden;
  out.case4InitialMaxScroll = initialMaxScroll;
  out.case4BtnVisibleAfterManualUp = btnVisibleAfterManualUp;
  out.case4BtnLabelAfterManualUp = btnLabelAfterManualUp;
  out.case4ListHasChipClass = listHasChipClass;
  out.case4ScrollPreservedDuringHold = scrollPreservedDuringHold;
  out.case4BtnStillVisibleDuringHold = btnStillVisibleDuringHold;
  out.case4ScrollAfterChipClick = scrollAfterChipClick;
  out.case4BtnHiddenAfterChipClick = btnHiddenAfterChipClick;
  out.case4ChipClassRemovedAfterClick = chipClassRemovedAfterClick;
  out.case4BtnVisibleBeforeChatScroll = btnVisibleBeforeChatScroll;
  out.case4TurnAfterUserChatScroll = turnAfterUserChatScroll;
  out.case4ScrollChangedFrom520 = scrollChangedFrom520;
  out.case4BtnHiddenAfterUserChatScroll = btnHiddenAfterUserChatScroll;
}

// ---------------------------------------------------------------------------
// Case 5: Surgical O(1) DOM updates without full innerHTML rebuild
// ---------------------------------------------------------------------------
{
  const turn50Headings = [1, 2, 3, 4, 5, 6].map((n, idx) => ({
    level: 2,
    text: `Глава ${n}`,
    offsetY: 140 + idx * 260,
  }));
  const chat5 = mountChatFixture(
    "chatDomId99",
    makeSimpleTurns(99, {
      50: { assistantHeight: 1900, headings: turn50Headings },
    })
  );
  api.state.activeChatId = "chatDomId99";
  api.state.activeChat = { id: "chatDomId99", kind: "chat" };
  api.state.messages = chat5.messages;
  api.state.outline.pinned = true;
  mc.scrollTop = mc.scrollHeight - mc.clientHeight;
  api.refreshChatOutline();
  flushRaf();

  const initialGroups = outlineList.querySelectorAll(".outline-turn-group");
  // While at turn 99, turn 50 is collapsed to 3 headings (+ more button)
  const g50Before = initialGroups[49];
  const g50BtnsCollapsed = g50Before.querySelectorAll(".outline-heading-btn").length;

  // Scroll chat to Turn 98, then to Turn 50 (heading 1), then to Turn 50 (heading 4), then Turn 49
  const u98 = ml.querySelector('.msg-row[data-msg-id="chatDomId99-u-98"]');
  fakeNow += 50;
  mc.scrollTop = u98._offsetTop - 90;
  mc.dispatch("scroll");
  flushRaf();

  const a50 = ml.querySelector('.msg-row[data-msg-id="chatDomId99-a-50"]');
  fakeNow += 50;
  mc.scrollTop = a50._offsetTop + turn50Headings[0].offsetY - 120;
  mc.dispatch("scroll");
  flushRaf();

  const g50BtnsOnEnter = g50Before.querySelectorAll(".outline-heading-btn");
  const g50ExpandedCount = g50BtnsOnEnter.length;

  // Scroll within Turn 50 from Глава 1 to Глава 4 -> heading buttons themselves must keep === identity!
  fakeNow += 50;
  mc.scrollTop = a50._offsetTop + turn50Headings[3].offsetY - 120;
  mc.dispatch("scroll");
  flushRaf();

  const g50BtnsAfterSubScroll = g50Before.querySelectorAll(".outline-heading-btn");
  const headingBtnIdentityPreserved =
    g50BtnsAfterSubScroll.length === 6 &&
    g50BtnsAfterSubScroll.every((btn, idx) => btn === g50BtnsOnEnter[idx]);

  // Scroll up to Turn 49 -> Turn 50 collapses back to 3 headings, while all 99 .outline-turn-group nodes keep ===
  const u49 = ml.querySelector('.msg-row[data-msg-id="chatDomId99-u-49"]');
  fakeNow += 50;
  mc.scrollTop = u49._offsetTop - 90;
  mc.dispatch("scroll");
  flushRaf();

  const afterGroups = outlineList.querySelectorAll(".outline-turn-group");
  const all99GroupsSameIdentity =
    initialGroups.length === 99 &&
    afterGroups.length === 99 &&
    afterGroups.every((g, idx) => g === initialGroups[idx]);
  const g50BtnsReCollapsed = g50Before.querySelectorAll(".outline-heading-btn").length;

  out.case5CollapsedBefore = g50BtnsCollapsed;
  out.case5ExpandedCount = g50ExpandedCount;
  out.case5HeadingBtnIdentityPreserved = headingBtnIdentityPreserved;
  out.case5ReCollapsedAfterLeave = g50BtnsReCollapsed;
  out.case5All99GroupsSameIdentity = all99GroupsSameIdentity;
}

// ---------------------------------------------------------------------------
// Case 6: Bottom-edge heading rule, reopen while wasOpen, search Escape & bookmarks fallback
// ---------------------------------------------------------------------------
{
  const finalHeadings = [1, 2, 3, 4].map((n, idx) => ({
    level: 2,
    text: `Итог ${n}`,
    offsetY: 80 + idx * 140,
  }));
  const chat6 = mountChatFixture(
    "chatExtra40",
    makeSimpleTurns(40, {
      40: { assistantHeight: 620, headings: finalHeadings },
    })
  );
  // Mark turns 5, 18, 35 as bookmarked
  for (const m of chat6.messages) {
    if (m.id === "chatExtra40-a-5" || m.id === "chatExtra40-a-18" || m.id === "chatExtra40-a-35") {
      m.bookmarked = true;
    }
  }
  api.state.activeChatId = "chatExtra40";
  api.state.activeChat = { id: "chatExtra40", kind: "chat" };
  api.state.messages = chat6.messages;
  api.state.outline.pinned = true;

  // At bottom edge: Итог 4 is below anchorY+28 (~224px) at top ~ 60 + aTop + 500 - scrollTop (~600px < bottom-24=756px)
  mc.scrollTop = mc.scrollHeight - mc.clientHeight;
  api.refreshChatOutline();
  flushRaf();

  out.case6BottomTurn = api.state.outline.activeTurnNum;
  out.case6BottomHeadingKey = api.state.outline.activeHeadingKey;

  // Manual scroll up in list while already open -> calling openChatOutline({ scrollActiveIntoView: true }) re-syncs even when wasOpen===true
  fakeNow += 100;
  outlineList.dispatch("wheel", { deltaY: -200 });
  outlineList.scrollTop = 120;
  outlineList.dispatch("scroll");
  const maxBeforeReopen = outlineList.scrollHeight - outlineList.clientHeight;
  api.openChatOutline({ scrollActiveIntoView: true });
  flushRaf();
  const maxAfterReopen = outlineList.scrollHeight - outlineList.clientHeight;
  out.case6ReopenScrollTop = outlineList.scrollTop;
  out.case6ReopenMaxScroll = maxAfterReopen;
  out.case6MaxBeforeReopen = maxBeforeReopen;

  // Search query hides sync chip & pauses auto-sync; Escape clears search and force-syncs
  fakeNow += 100;
  outlineList.scrollTop = 80;
  const searchInput = api.els.chatOutlineSearchInput;
  searchInput.focus();
  searchInput.value = "Вопрос пользователя #1";
  searchInput.dispatch("input");
  out.case6ChipHiddenDuringSearch = syncBtn.classList.contains("hidden");

  searchInput.dispatch("keydown", { key: "Escape" });
  searchInput.blur();
  flushRaf();
  out.case6SearchClearedQuery = api.state.outline.searchQuery;
  out.case6ScrollAfterSearchEsc = outlineList.scrollTop;

  // Bookmarks filter fallback when active turn (25) is not bookmarked -> targets nearest prior bookmark (18)
  const u25 = ml.querySelector('.msg-row[data-msg-id="chatExtra40-u-25"]');
  mc.scrollTop = u25._offsetTop - 90;
  mc.dispatch("scroll");
  flushRaf();
  api.els.outlineFilterBookmarksBtn.click();
  out.case6BookmarksFilterMode = api.state.outline.filterMode;
  api.els.outlineFilterAllBtn.click();
  out.case6RestoredFilterMode = api.state.outline.filterMode;
  out.case6ActiveTurnAt25 = api.state.outline.activeTurnNum;
}

process.stdout.write(JSON.stringify(out));
"""


class TestChatOutlineSync(unittest.TestCase):
    outline_vm_results: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        db_before = (
            (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
            if LIVE_DB_PATH.exists()
            else None
        )
        env = dict(os.environ)
        env["CUDA_VISIBLE_DEVICES"] = ""
        env["ORT_DISABLE_ALL_GPU"] = "1"
        env["AGY_APP_JS_PATH"] = str(APP_JS_PATH)
        with tempfile.TemporaryDirectory() as tmp_dir:
            harness_path = Path(tmp_dir) / "outline_vm_harness.js"
            harness_path.write_text(NODE_OUTLINE_VM_HARNESS, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(harness_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                **subprocess_hidden_kwargs(),
            )
        if proc.returncode != 0:
            raise AssertionError(
                f"Node outline VM harness failed (code={proc.returncode}):\n"
                f"STDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}"
            )
        cls.outline_vm_results = json.loads(proc.stdout.strip())
        db_after = (
            (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
            if LIVE_DB_PATH.exists()
            else None
        )
        if db_before != db_after:
            raise AssertionError("Live data/hub.db was modified during outline VM setup!")

    def setUp(self) -> None:
        self._db_stat_before = (
            (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
            if LIVE_DB_PATH.exists()
            else None
        )

    def tearDown(self) -> None:
        db_stat_after = (
            (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
            if LIVE_DB_PATH.exists()
            else None
        )
        self.assertEqual(
            self._db_stat_before,
            db_stat_after,
            "Live data/hub.db must remain completely untouched!",
        )

    def test_case1_pinned_99_turns_and_chat_switching(self) -> None:
        res = self.outline_vm_results
        self.assertTrue(res["case1DirectIsOpen"])
        self.assertEqual(res["case1DirectActiveTurn"], 99)
        self.assertGreater(res["case1DirectMaxScroll"], 2000)
        self.assertEqual(
            res["case1DirectScrollTop"],
            res["case1DirectMaxScroll"],
            "Pinned outline on 99-turn chat must sync directly to maxScroll (turn 99), not stay at 20-24",
        )

        self.assertEqual(res["case1AsyncActiveTurn"], 99)
        self.assertEqual(
            res["case1AsyncScrollTop"],
            res["case1AsyncMaxScroll"],
            "Uncached async load (empty -> 99 turns) must force-sync pinned outline to turn 99",
        )

        self.assertEqual(res["case1SwitchBActiveTurn"], 55)
        self.assertGreaterEqual(res["case1SwitchBRowCenterRatio"], 0.34)
        self.assertLessEqual(res["case1SwitchBRowCenterRatio"], 0.38)

    def test_case2_synchronous_ascent_descent_and_long_answer_anchor(self) -> None:
        res = self.outline_vm_results
        self.assertEqual(res["case2TurnsUp"], [99, 80, 50, 25, 10, 1])

        scrolls = res["case2ScrollsUp"]
        self.assertEqual(scrolls[0], res["case2MaxScroll"], "Bottom edge must stand at maxScroll")
        self.assertEqual(scrolls[-1], 0, "Top edge (scrollTop = 0) must stand at exact 0")
        for idx in range(len(scrolls) - 1):
            self.assertGreater(
                scrolls[idx],
                scrolls[idx + 1],
                f"Outline scrollTop must decrease monotonically while ascending chat: {scrolls}",
            )

        for t_key in ("80", "50", "25"):
            ratio = res["case2ComfortRatios"][t_key]
            self.assertAlmostEqual(
                ratio,
                0.36,
                delta=0.02,
                msg=f"Active row {t_key} must stay at ~36% comfort line (got {ratio:.3f})",
            )

        self.assertEqual(res["case2ReturnBottomTurn"], 99)
        self.assertEqual(res["case2ReturnBottomScroll"], res["case2MaxScroll"])

        self.assertFalse(res["case2LongAnswerAtBottom"])
        self.assertEqual(
            res["case2LongAnswerAnchorTurn"],
            98,
            "When distFromBottom > 16 on a long final answer, selection must follow anchorY strictly",
        )

    def test_case3_subheading_scroll_spy_inside_long_answer(self) -> None:
        res = self.outline_vm_results
        self.assertEqual(res["case3IntroTurn"], 5)
        self.assertEqual(
            res["case3IntroHeadingKey"],
            "",
            "Reading intro paragraph above first heading must leave activeHeadingKey empty",
        )
        self.assertEqual(
            res["case3ExpandedHeadingBtnCount"],
            6,
            "Active turn with 6 headings must expand all 6 sub-heading buttons",
        )

        expected_keys = [f"chatHeadings-a-5:{i}" for i in range(6)]
        self.assertEqual(res["case3ActiveKeysSeq"], expected_keys)
        self.assertEqual(res["case3ActiveDomKeysSeq"], expected_keys)

        h_scrolls = res["case3HeadingScrollsSeq"]
        for i in range(len(h_scrolls) - 1):
            self.assertLessEqual(h_scrolls[i], h_scrolls[i + 1])
        self.assertGreater(
            h_scrolls[-1],
            h_scrolls[0],
            f"Scrolling across Раздел 1..6 must advance #chatOutlineList.scrollTop: {h_scrolls}",
        )

    def test_case4_anti_conflict_guard_and_sync_active_chip(self) -> None:
        res = self.outline_vm_results
        self.assertTrue(res["case4InitialBtnHidden"])
        self.assertTrue(res["case4BtnVisibleAfterManualUp"])
        self.assertEqual(res["case4BtnLabelAfterManualUp"], "К текущему · 99")
        self.assertTrue(res["case4ListHasChipClass"])

        self.assertEqual(
            res["case4ScrollPreservedDuringHold"],
            520,
            "Background chat updates must NOT reset user's manual scroll in #chatOutlineList",
        )
        self.assertTrue(res["case4BtnStillVisibleDuringHold"])

        self.assertEqual(res["case4ScrollAfterChipClick"], res["case4InitialMaxScroll"])
        self.assertTrue(res["case4BtnHiddenAfterChipClick"])
        self.assertTrue(res["case4ChipClassRemovedAfterClick"])

        self.assertTrue(res["case4BtnVisibleBeforeChatScroll"])
        self.assertEqual(res["case4TurnAfterUserChatScroll"], 75)
        self.assertTrue(res["case4ScrollChangedFrom520"])
        self.assertTrue(res["case4BtnHiddenAfterUserChatScroll"])

    def test_case5_surgical_dom_identity_and_static_invariants(self) -> None:
        res = self.outline_vm_results
        self.assertEqual(res["case5CollapsedBefore"], 3)
        self.assertEqual(res["case5ExpandedCount"], 6)
        self.assertTrue(
            res["case5HeadingBtnIdentityPreserved"],
            "Sub-heading scroll within active turn must toggle .is-active in O(1) without recreating buttons",
        )
        self.assertEqual(res["case5ReCollapsedAfterLeave"], 3)
        self.assertTrue(
            res["case5All99GroupsSameIdentity"],
            "All 99 .outline-turn-group DOM nodes must retain === reference identity during chat scroll",
        )

    def test_case6_bookmarks_search_reopen_and_bottom_heading_rule(self) -> None:
        res = self.outline_vm_results
        self.assertEqual(res["case6BottomTurn"], 40)
        self.assertEqual(
            res["case6BottomHeadingKey"],
            "chatExtra40-a-40:3",
            "At bottom edge, final heading visible above mcRect.bottom - 24 must become active",
        )
        self.assertEqual(
            res["case6ReopenScrollTop"],
            res["case6ReopenMaxScroll"],
            "openChatOutline must sync to active context even when popover was already open",
        )
        self.assertTrue(res["case6ChipHiddenDuringSearch"])
        self.assertEqual(res["case6SearchClearedQuery"], "")
        self.assertEqual(res["case6ScrollAfterSearchEsc"], res["case6ReopenMaxScroll"])
        self.assertEqual(res["case6BookmarksFilterMode"], "bookmarks")
        self.assertEqual(res["case6RestoredFilterMode"], "all")
        self.assertEqual(res["case6ActiveTurnAt25"], 25)

    def test_static_syntax_cache_version_and_css_invariants(self) -> None:

        # Static checks: JS syntax, cache version sync (v>=89), HTML structure, CSS braces & mobile sheet invariant
        for js_file in (APP_JS_PATH, SW_JS_PATH):
            proc = subprocess.run(
                ["node", "--check", str(js_file)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                **subprocess_hidden_kwargs(),
            )
            self.assertEqual(proc.returncode, 0, f"Syntax error in {js_file}: {proc.stderr}")

        sw_text = SW_JS_PATH.read_text(encoding="utf-8")
        html_text = INDEX_HTML_PATH.read_text(encoding="utf-8")
        css_text = STYLES_CSS_PATH.read_text(encoding="utf-8")

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_text)
        self.assertIsNotNone(m_sw, "CACHE_NAME not found in sw.js")
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 89)
        self.assertIn(f'"/static/styles.css?v={ver}"', sw_text)
        self.assertIn(f'"/static/app.js?v={ver}"', sw_text)
        self.assertIn(f'href="/static/styles.css?v={ver}"', html_text)
        self.assertIn(f'src="/static/app.js?v={ver}"', html_text)

        # HTML structure check for #outlineSyncActiveBtn inside #chatOutlinePopover right after #chatOutlineList
        self.assertRegex(
            html_text,
            r'id="chatOutlineList"[^>]*></div>\s*<button[^>]*id="outlineSyncActiveBtn"[^>]*class="outline-sync-active-btn hidden"',
        )
        self.assertIn('id="outlineSyncActiveLabel"', html_text)

        # CSS brace balance & required selectors
        clean_css = strip_css_comments_and_strings(css_text)
        self.assertEqual(
            clean_css.count("{"),
            clean_css.count("}"),
            "Unbalanced CSS curly braces in app/static/styles.css",
        )
        for required_selector in (
            ".outline-turn-row.is-active",
            'html[data-theme="light"] .outline-turn-row.is-active',
            ".outline-heading-btn.is-active",
            ".outline-heading-btn.is-active .outline-heading-arrow",
            ".outline-sync-active-btn",
            ".outline-timeline-list.has-sync-chip",
        ):
            self.assertIn(required_selector, css_text)

        # Mobile Bottom Sheet invariant: main.workspace / .workspace must never define transform, filter, perspective, or container-type
        for rule_match in re.finditer(r"([^{}]+)\{([^{}]*)\}", clean_css):
            selectors_raw = rule_match.group(1)
            body_raw = rule_match.group(2)
            selectors = [s.strip() for s in selectors_raw.split(",")]
            targets_workspace_root = any(
                s in (".workspace", "main.workspace")
                or re.fullmatch(r"(?:main)?\.workspace(?:\.[a-zA-Z0-9_-]+)*", s) is not None
                for s in selectors
            )
            if targets_workspace_root:
                for forbidden_prop in ("transform", "filter", "perspective", "container-type"):
                    self.assertIsNone(
                        re.search(rf"(?<![-\w]){re.escape(forbidden_prop)}\s*:", body_raw),
                        f"Forbidden property '{forbidden_prop}' found on '{selectors_raw.strip()}'!",
                    )


if __name__ == "__main__":
    unittest.main()
