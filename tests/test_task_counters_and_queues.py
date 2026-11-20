"""
Automated tests for Antigravity Hub v=76:
Global task counters (Running / Queued / Done) & end-to-end tab queue indicators.

Covers all 4 specification verification requirements:
1. Empty state (`0` running, `0` queued, `0` unread): `#sidebarToggleRadar` is `.hidden`,
   `#toggleSidebarBtn` has NO `.has-radar-pills`, `#activitySectionBadges` is `.hidden`.
2. With `2` running chats, `+3` queued tasks, and `4` completed tasks: `#toggleSidebarBtn`
   gets `.has-radar-pills` and renders `● 2`, `+3`, `✓ 4` inside `#sidebarToggleRadar`;
   `#sidebarActivitySection` header renders separate badges (`2`, `+3 оч.`, `✓ 4`);
   batch background completion of 3 queued tasks on a single tab yields `✓ Готово · 3` and resets on open.
3. WebSocket `queue_updated` for a background/target tab immediately updates:
   - sidebar `.chat-state-pill` (`● В работе · +3` / `⏸ Очередь · 2`),
   - open header `#activeChatStatePill` (`В работе · +3`),
   - workspace tab `.wtab-queue-tag` (`+3`),
   - project folder header `.proj-count-queued` (`⏳ +3`),
   - Architect HQ chip `.msg-code-handoff-chip` (`Выполняется · +3 в оч.` / `⏸ В очереди: 2`),
   - `🚀 В Код` menu `.stc-queue-pill` (`в очередь (#1)` / `в очередь (уже +3 ждут)`).
4. Collapsible `#composerQueueBar` defaults to 1-line `.is-collapsed` on mobile (`<= 680px`),
   expands on `.queue-bar-header` click (or `#activeChatStatePill` click with `.is-flash-highlight`),
   cold queue hydration (`hydrateColdChatQueues`) works without server restart, and backend
   `api_list_chats` / `api_update_chat` return accurate `queue_count` & `queue_paused` inside
   an isolated `tempfile.TemporaryDirectory()` without touching live `data/hub.db` or `run_server.py`.
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


NODE_VM_HARNESS_JS = r"""
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

class ClassList {
  constructor(el) {
    this._el = el;
    this._set = new Set();
  }
  _syncFromAttr(val) {
    this._set.clear();
    String(val || "")
      .split(/\s+/)
      .filter(Boolean)
      .forEach((c) => this._set.add(c));
  }
  _updateAttr() {
    this._el._attrs.class = Array.from(this._set).join(" ");
  }
  add(...names) {
    names.forEach((n) => {
      if (n) this._set.add(String(n));
    });
    this._updateAttr();
  }
  remove(...names) {
    names.forEach((n) => this._set.delete(String(n)));
    this._updateAttr();
  }
  toggle(name, force) {
    const s = String(name);
    const shouldHave = force !== undefined ? Boolean(force) : !this._set.has(s);
    if (shouldHave) this._set.add(s);
    else this._set.delete(s);
    this._updateAttr();
    return shouldHave;
  }
  contains(name) {
    return this._set.has(String(name));
  }
  toString() {
    return Array.from(this._set).join(" ");
  }
}

function toDash(camel) {
  return camel.replace(/([A-Z])/g, "-$1").toLowerCase();
}

function matchSingleSelector(el, sel) {
  if (!el || el.nodeType !== 1) return false;
  let s = sel.trim();
  if (!s) return false;
  if (s === "*") return true;

  const notRegex = /:not\(([^)]+)\)/g;
  let m;
  while ((m = notRegex.exec(s)) !== null) {
    if (matchSingleSelector(el, m[1])) return false;
  }
  s = s.replace(notRegex, "");

  const attrRegex = /\[([a-zA-Z0-9_-]+)(?:=["']?([^"'\]]*)["']?)?\]/g;
  while ((m = attrRegex.exec(s)) !== null) {
    const attrName = m[1];
    const expected = m[2] !== undefined ? String(m[2]).replace(/\\(.)/g, "$1") : undefined;
    const has = el.hasAttribute(attrName);
    if (!has) return false;
    if (expected !== undefined && String(el.getAttribute(attrName)) !== expected) return false;
  }
  s = s.replace(attrRegex, "");

  if (s.includes(":checked")) {
    if (!el.checked) return false;
    s = s.replace(/:checked/g, "");
  }
  if (s.includes(":disabled")) {
    if (!el.disabled) return false;
    s = s.replace(/:disabled/g, "");
  }

  const idMatch = s.match(/#([a-zA-Z0-9_-]+)/);
  if (idMatch) {
    if (el.id !== idMatch[1]) return false;
    s = s.replace(/#[a-zA-Z0-9_-]+/g, "");
  }

  const classMatches = s.match(/\.([a-zA-Z0-9_-]+)/g);
  if (classMatches) {
    for (const cm of classMatches) {
      if (!el.classList.contains(cm.slice(1))) return false;
    }
    s = s.replace(/\.[a-zA-Z0-9_-]+/g, "");
  }

  const tag = s.trim().toUpperCase();
  if (tag && el.tagName !== tag) return false;
  return true;
}

function matchesSelectorChain(el, selectorGroup) {
  const groups = String(selectorGroup)
    .split(",")
    .map((g) => g.trim())
    .filter(Boolean);
  for (const grp of groups) {
    const parts = grp.split(/\s+/).filter(Boolean);
    let cur = el;
    let ok = matchSingleSelector(cur, parts[parts.length - 1]);
    if (!ok) continue;
    for (let i = parts.length - 2; i >= 0; i--) {
      const part = parts[i];
      if (part === ">") continue;
      let ancestor = cur.parentNode;
      let found = false;
      while (ancestor && ancestor.nodeType === 1) {
        if (matchSingleSelector(ancestor, part)) {
          found = true;
          cur = ancestor;
          break;
        }
        ancestor = ancestor.parentNode;
      }
      if (!found) {
        ok = false;
        break;
      }
    }
    if (ok) return true;
  }
  return false;
}

class MiniNode {
  constructor(nodeType) {
    this.nodeType = nodeType;
    this.parentNode = null;
    this.childNodes = [];
  }
  get parentElement() {
    return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null;
  }
  get firstChild() {
    return this.childNodes[0] || null;
  }
  get lastChild() {
    return this.childNodes[this.childNodes.length - 1] || null;
  }
  get nextSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.childNodes.indexOf(this);
    return idx >= 0 ? this.parentNode.childNodes[idx + 1] || null : null;
  }
  get nextElementSibling() {
    let n = this.nextSibling;
    while (n && n.nodeType !== 1) n = n.nextSibling;
    return n || null;
  }
  get previousElementSibling() {
    if (!this.parentNode) return null;
    let idx = this.parentNode.childNodes.indexOf(this) - 1;
    while (idx >= 0) {
      if (this.parentNode.childNodes[idx].nodeType === 1) return this.parentNode.childNodes[idx];
      idx--;
    }
    return null;
  }
  get children() {
    return this.childNodes.filter((c) => c.nodeType === 1);
  }
  appendChild(child) {
    if (!child) return child;
    if (child.nodeType === 11) {
      const copy = [...child.childNodes];
      child.childNodes = [];
      for (const c of copy) this.appendChild(c);
      return child;
    }
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.childNodes.push(child);
    if (this._ownerDoc) propagateDoc(child, this._ownerDoc);
    return child;
  }
  insertBefore(newNode, refNode) {
    if (!refNode) return this.appendChild(newNode);
    if (newNode.nodeType === 11) {
      const copy = [...newNode.childNodes];
      newNode.childNodes = [];
      for (const c of copy) this.insertBefore(c, refNode);
      return newNode;
    }
    if (newNode.parentNode) newNode.parentNode.removeChild(newNode);
    const idx = this.childNodes.indexOf(refNode);
    newNode.parentNode = this;
    if (idx < 0) this.childNodes.push(newNode);
    else this.childNodes.splice(idx, 0, newNode);
    if (this._ownerDoc) propagateDoc(newNode, this._ownerDoc);
    return newNode;
  }
  replaceChild(newChild, oldChild) {
    this.insertBefore(newChild, oldChild);
    this.removeChild(oldChild);
    return oldChild;
  }
  removeChild(child) {
    const idx = this.childNodes.indexOf(child);
    if (idx >= 0) {
      this.childNodes.splice(idx, 1);
      child.parentNode = null;
    }
    return child;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  replaceChildren(...nodes) {
    for (const c of [...this.childNodes]) this.removeChild(c);
    for (const n of nodes) {
      if (typeof n === "string") this.appendChild(new MiniText(n));
      else if (n) this.appendChild(n);
    }
  }
  contains(other) {
    let cur = other;
    while (cur) {
      if (cur === this) return true;
      cur = cur.parentNode;
    }
    return false;
  }
}

function propagateDoc(node, doc) {
  node._ownerDoc = doc;
  if (node.nodeType === 1 && node.id) {
    doc._idMap.set(node.id, node);
  }
  for (const c of node.childNodes || []) propagateDoc(c, doc);
}

class MiniText extends MiniNode {
  constructor(text) {
    super(3);
    this.nodeValue = String(text ?? "");
  }
  get textContent() {
    return this.nodeValue;
  }
  set textContent(v) {
    this.nodeValue = String(v ?? "");
  }
}

class MiniFragment extends MiniNode {
  constructor(doc) {
    super(11);
    this._ownerDoc = doc;
  }
}

class MiniElement extends MiniNode {
  constructor(tagName, doc) {
    super(1);
    this.tagName = String(tagName || "DIV").toUpperCase();
    this._ownerDoc = doc || null;
    this._attrs = {};
    this._listeners = {};
    this.classList = new ClassList(this);
    this.style = {
      setProperty(k, v) { this[k] = v; },
      removeProperty(k) { delete this[k]; },
      getPropertyValue(k) { return this[k] || ""; },
    };
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.draggable = false;
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this.scrollHeight = 400;
    this.clientHeight = 400;
    this.clientWidth = 360;
    this.offsetHeight = 32;
    this.offsetWidth = 240;

    const self = this;
    this.dataset = new Proxy(
      {},
      {
        get(_, prop) {
          if (typeof prop !== "string") return undefined;
          const attr = "data-" + toDash(prop);
          return Object.prototype.hasOwnProperty.call(self._attrs, attr)
            ? self._attrs[attr]
            : undefined;
        },
        set(_, prop, val) {
          const attr = "data-" + toDash(String(prop));
          self._attrs[attr] = String(val);
          return true;
        },
        deleteProperty(_, prop) {
          const attr = "data-" + toDash(String(prop));
          delete self._attrs[attr];
          return true;
        },
        has(_, prop) {
          if (typeof prop !== "string") return false;
          const attr = "data-" + toDash(prop);
          return Object.prototype.hasOwnProperty.call(self._attrs, attr);
        },
      }
    );
  }
  get id() { return this._attrs.id || ""; }
  set id(v) {
    const s = String(v || "");
    this._attrs.id = s;
    if (this._ownerDoc && s) this._ownerDoc._idMap.set(s, this);
  }
  get className() { return this._attrs.class || ""; }
  set className(v) {
    this._attrs.class = String(v || "");
    this.classList._syncFromAttr(this._attrs.class);
  }
  get title() { return this._attrs.title || ""; }
  set title(v) { this._attrs.title = String(v ?? ""); }
  get type() { return this._attrs.type || ""; }
  set type(v) { this._attrs.type = String(v ?? ""); }
  get options() { return this.children.filter((c) => c.tagName === "OPTION"); }
  get files() { return this._files || []; }
  setAttribute(name, val) {
    const k = String(name).toLowerCase();
    const s = String(val ?? "");
    this._attrs[k] = s;
    if (k === "id" && this._ownerDoc && s) this._ownerDoc._idMap.set(s, this);
    if (k === "class") this.classList._syncFromAttr(s);
    if (k === "value" && !this._valueDirty) this.value = s;
    if (k === "checked") this.checked = true;
    if (k === "disabled") this.disabled = true;
  }
  getAttribute(name) {
    const k = String(name).toLowerCase();
    return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
  }
  hasAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attrs, String(name).toLowerCase());
  }
  removeAttribute(name) {
    const k = String(name).toLowerCase();
    delete this._attrs[k];
    if (k === "class") this.classList._syncFromAttr("");
    if (k === "disabled") this.disabled = false;
  }
  get textContent() {
    let out = "";
    for (const c of this.childNodes) out += c.textContent;
    return out;
  }
  set textContent(v) {
    for (const c of [...this.childNodes]) this.removeChild(c);
    if (v !== "" && v !== null && v !== undefined) {
      this.appendChild(new MiniText(String(v)));
    }
  }
  get innerHTML() {
    return this.childNodes
      .map((c) => {
        if (c.nodeType === 3) return c.nodeValue;
        const attrs = Object.entries(c._attrs)
          .map(([k, v]) => `${k}="${String(v).replace(/"/g, "&quot;")}"`)
          .join(" ");
        const open = attrs ? `<${c.tagName.toLowerCase()} ${attrs}>` : `<${c.tagName.toLowerCase()}>`;
        return `${open}${c.innerHTML}</${c.tagName.toLowerCase()}>`;
      })
      .join("");
  }
  set innerHTML(html) {
    for (const c of [...this.childNodes]) this.removeChild(c);
    if (!html) return;
    parseHtmlInto(String(html), this, this._ownerDoc);
  }
  insertAdjacentHTML(position, html) {
    if (!html) return;
    const frag = new MiniFragment(this._ownerDoc);
    parseHtmlInto(String(html), frag, this._ownerDoc);
    const pos = String(position || "").toLowerCase();
    if (pos === "afterbegin") {
      this.insertBefore(frag, this.firstChild);
    } else if (pos === "beforeend") {
      this.appendChild(frag);
    } else if (pos === "beforebegin") {
      if (this.parentNode) this.parentNode.insertBefore(frag, this);
    } else if (pos === "afterend") {
      if (this.parentNode) this.parentNode.insertBefore(frag, this.nextSibling);
    }
  }
  addEventListener(type, fn) {
    if (!this._listeners[type]) this._listeners[type] = [];
    this._listeners[type].push(fn);
  }
  removeEventListener(type, fn) {
    if (!this._listeners[type]) return;
    this._listeners[type] = this._listeners[type].filter((f) => f !== fn);
  }
  dispatchEvent(evt) {
    evt.target = evt.target || this;
    let cur = this;
    while (cur) {
      evt.currentTarget = cur;
      const list = (cur._listeners && cur._listeners[evt.type]) || [];
      for (const fn of [...list]) {
        fn.call(cur, evt);
        if (evt._immediateStopped) return !evt.defaultPrevented;
      }
      if (evt._stopped || evt.bubbles === false) break;
      cur = cur.parentNode;
    }
    return !evt.defaultPrevented;
  }
  click() {
    this.dispatchEvent(createEvent("click", { bubbles: true }));
  }
  focus() {}
  blur() {}
  select() {}
  scrollTo(opts) {
    if (opts && typeof opts.top === "number") this.scrollTop = opts.top;
  }
  scrollIntoView() {}
  getBoundingClientRect() {
    return { top: 40, left: 10, bottom: 72, right: 250, width: 240, height: 32 };
  }
  matches(sel) { return matchesSelectorChain(this, sel); }
  closest(sel) {
    let cur = this;
    while (cur && cur.nodeType === 1) {
      if (matchesSelectorChain(cur, sel)) return cur;
      cur = cur.parentNode;
    }
    return null;
  }
  querySelector(sel) {
    let found = null;
    const walk = (node) => {
      for (const c of node.childNodes) {
        if (found) return;
        if (c.nodeType === 1) {
          if (matchesSelectorChain(c, sel)) {
            found = c;
            return;
          }
          walk(c);
        }
      }
    };
    walk(this);
    return found;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (node) => {
      for (const c of node.childNodes) {
        if (c.nodeType === 1) {
          if (matchesSelectorChain(c, sel)) out.push(c);
          walk(c);
        }
      }
    };
    walk(this);
    return out;
  }
}

function createEvent(type, init = {}) {
  return {
    type,
    button: 0,
    bubbles: init.bubbles !== undefined ? init.bubbles : true,
    defaultPrevented: false,
    _stopped: false,
    _immediateStopped: false,
    key: init.key || "",
    code: init.code || "",
    ctrlKey: Boolean(init.ctrlKey),
    metaKey: Boolean(init.metaKey),
    shiftKey: Boolean(init.shiftKey),
    altKey: Boolean(init.altKey),
    clientX: init.clientX || 0,
    clientY: init.clientY || 0,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this._stopped = true; },
    stopImmediatePropagation() {
      this._stopped = true;
      this._immediateStopped = true;
    },
  };
}

const VOID_TAGS = new Set([
  "AREA", "BASE", "BR", "COL", "EMBED", "HR", "IMG", "INPUT",
  "LINK", "META", "PARAM", "SOURCE", "TRACK", "WBR", "PATH", "CIRCLE",
  "RECT", "LINE", "POLYLINE", "POLYGON", "USE"
]);

function decodeEntities(s) {
  return s
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

function parseHtmlInto(html, rootParent, doc) {
  const cleaned = html.replace(/<!--[\s\S]*?-->/g, "");
  const stack = [rootParent];
  const tagRegex = /<\/?([a-zA-Z0-9:-]+)([^>]*)>|([^<]+)/g;
  let m;
  while ((m = tagRegex.exec(cleaned)) !== null) {
    if (m[3] !== undefined) {
      const txt = m[3];
      if (txt) stack[stack.length - 1].appendChild(new MiniText(decodeEntities(txt)));
      continue;
    }
    const rawFull = m[0];
    const tag = m[1].toUpperCase();
    const attrStr = m[2] || "";
    const isClosing = rawFull.startsWith("</");
    const selfClosing = rawFull.endsWith("/>") || VOID_TAGS.has(tag);

    if (isClosing) {
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tagName === tag) {
          stack.length = i;
          break;
        }
      }
      continue;
    }

    const el = new MiniElement(tag, doc);
    const attrRe = /([a-zA-Z0-9_:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
    let am;
    while ((am = attrRe.exec(attrStr)) !== null) {
      const k = am[1];
      if (k === "/") continue;
      const v = am[2] !== undefined ? am[2] : am[3] !== undefined ? am[3] : am[4] !== undefined ? am[4] : "";
      el.setAttribute(k, decodeEntities(v));
    }
    stack[stack.length - 1].appendChild(el);
    if (!selfClosing) stack.push(el);
  }
}

class MiniDocument extends MiniElement {
  constructor() {
    super("#DOCUMENT", null);
    this.nodeType = 9;
    this._ownerDoc = this;
    this._idMap = new Map();
    this.documentElement = new MiniElement("HTML", this);
    this.head = new MiniElement("HEAD", this);
    this.body = new MiniElement("BODY", this);
    this.documentElement.appendChild(this.head);
    this.documentElement.appendChild(this.body);
    this.appendChild(this.documentElement);
    this.activeElement = this.body;
    this.hidden = false;
    this.visibilityState = "visible";
    this.title = "Antigravity Hub";
  }
  createElement(tag) { return new MiniElement(tag, this); }
  createElementNS(_ns, tag) { return new MiniElement(tag, this); }
  createTextNode(txt) { return new MiniText(txt); }
  createDocumentFragment() { return new MiniFragment(this); }
  getElementById(id) {
    const fast = this._idMap.get(String(id));
    if (fast && this.documentElement.contains(fast)) return fast;
    return this.documentElement.querySelector(`#${id}`);
  }
}

class MemoryStorage {
  constructor() { this._map = new Map(); }
  getItem(k) { return this._map.has(String(k)) ? this._map.get(String(k)) : null; }
  setItem(k, v) { this._map.set(String(k), String(v)); }
  removeItem(k) { this._map.delete(String(k)); }
  clear() { this._map.clear(); }
  get length() { return this._map.size; }
  key(i) { return Array.from(this._map.keys())[i] || null; }
}

async function main() {
  const rootDir = process.env.HUB_ROOT_DIR;
  const htmlText = fs.readFileSync(path.join(rootDir, "app/static/index.html"), "utf8");
  const appCode = fs.readFileSync(path.join(rootDir, "app/static/app.js"), "utf8");

  const doc = new MiniDocument();
  const bodyMatch = htmlText.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
  assert(bodyMatch, "body found in index.html");
  doc.body.innerHTML = bodyMatch[1].replace(/<script[\s\S]*?<\/script>/gi, "");
  // Provide optional #workspaceTabsList slot so renderWorkspaceTabs() can also be verified directly
  if (!doc.getElementById("workspaceTabsList")) {
    const wtList = doc.createElement("div");
    wtList.id = "workspaceTabsList";
    doc.body.appendChild(wtList);
  }

  const localStorage = new MemoryStorage();
  const sessionStorage = new MemoryStorage();
  localStorage.setItem("agy_hub_token", "tok_alice");

  let currentUser = { id: "u_alice", username: "alice", display_name: "Alice", is_admin: false };
  const projects = [{ name: "HubProj", path: "/ws/HubProj", is_shared: false }];
  const nowIso = new Date().toISOString();

  // Start completely idle (0 running, 0 queued, 0 unread) to test Requirement #1
  const chatsDb = new Map([
    ["c_hq", {
      id: "c_hq",
      title: "Обсуждение (Штаб)",
      kind: "project",
      project_name: "HubProj",
      access_mode: "read_only",
      pinned: 1,
      sort_order: 1,
      is_generating: false,
      updated_at: nowIso,
    }],
    ["c_vis", {
      id: "c_vis",
      title: "Визуал",
      kind: "project",
      project_name: "HubProj",
      access_mode: "write_files",
      pinned: 0,
      sort_order: 2,
      is_generating: false,
      updated_at: nowIso,
    }],
    ["c_back", {
      id: "c_back",
      title: "Бэкенд",
      kind: "project",
      project_name: "HubProj",
      access_mode: "write_files",
      pinned: 0,
      sort_order: 3,
      is_generating: false,
      updated_at: nowIso,
    }],
    ["c_done_a", {
      id: "c_done_a",
      title: "Задача A",
      kind: "project",
      project_name: "HubProj",
      access_mode: "write_files",
      pinned: 0,
      sort_order: 4,
      is_generating: false,
      updated_at: nowIso,
    }],
    ["c_done_b", {
      id: "c_done_b",
      title: "Задача B",
      kind: "project",
      project_name: "HubProj",
      access_mode: "write_files",
      pinned: 0,
      sort_order: 5,
      is_generating: false,
      updated_at: nowIso,
    }],
  ]);

  const queuesByChat = new Map();
  const messagesByChat = new Map([
    ["c_hq", [
      { id: 101, role: "user", content: "Спроектируй фичу", created_at: nowIso },
      {
        id: 102,
        role: "assistant",
        status: "done",
        content: "# ТЗ\nРеализовать глобальные счётчики.",
        created_at: nowIso,
        usage: {
          code_handoffs: [
            {
              target_chat_id: "c_vis",
              target_title: "Визуал",
              project_name: "HubProj",
              sent_at: nowIso,
              send_count: 1,
            },
          ],
        },
      },
    ]],
  ]);

  let lastWs = null;
  class MockWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      lastWs = this;
      setTimeout(() => {
        if (typeof this.onopen === "function") this.onopen({});
      }, 0);
    }
    send(raw) { this._lastSent = JSON.parse(raw); }
    close() { this.readyState = 3; }
    emit(evt) {
      if (typeof this.onmessage === "function") {
        this.onmessage({ data: JSON.stringify(evt) });
      }
    }
  }
  MockWebSocket.OPEN = 1;

  async function mockFetch(urlStr, opts = {}) {
    const u = new URL(urlStr, "http://localhost:8000");
    const p = u.pathname;
    const method = (opts.method || "GET").toUpperCase();
    const jsonResp = (data) => ({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => data,
      text: async () => JSON.stringify(data),
    });

    if (p === "/api/auth/check") {
      return jsonResp({ authenticated: true, auth_enabled: true, user: currentUser });
    }
    if (p === "/api/system/info") {
      return jsonResp({
        user: currentUser,
        models: [{ id: "gemini-2.5-pro", label: "Gemini 2.5 Pro" }],
        account_profiles: [],
      });
    }
    if (p === "/api/models") {
      return jsonResp({ models: [{ id: "gemini-2.5-pro", label: "Gemini 2.5 Pro" }], default: "gemini-2.5-pro" });
    }
    if (p === "/api/projects") return jsonResp({ projects });
    if (p === "/api/chats" && method === "GET") {
      return jsonResp({ chats: Array.from(chatsDb.values()).map((c) => ({ ...c })) });
    }
    const chatMatch = p.match(/^\/api\/chats\/([^/]+)$/);
    if (chatMatch && method === "GET") {
      const id = decodeURIComponent(chatMatch[1]);
      const chat = chatsDb.get(id);
      const q = queuesByChat.get(id) || { items: [], paused: false };
      return jsonResp({
        chat: { ...chat },
        messages: messagesByChat.get(id) || [
          { id: 1, role: "user", content: "Промпт", created_at: nowIso },
          { id: 2, role: "assistant", status: chat.is_generating ? "streaming" : "done", content: "Ответ", created_at: nowIso },
        ],
        queued_messages: q.items,
        queue_paused: Boolean(q.paused),
      });
    }
    if (p === "/api/quotas") return jsonResp({ accounts: [], active_account: null, stats: {} });
    if (p === "/api/user/rules") return jsonResp({ content: "" });
    if (p === "/api/user/project-order") return jsonResp({ order: [] });
    if (p.startsWith("/api/openrouter")) return jsonResp({ configured: false, models: [], logs: [] });
    if (p.startsWith("/api/projects/") && p.endsWith("/memory")) return jsonResp({ exists: false, content: "" });
    if (p.startsWith("/api/projects/") && p.endsWith("/conversations")) return jsonResp({ conversations: [] });
    return jsonResp({});
  }

  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    setInterval: () => 1,
    clearInterval: () => {},
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    cancelAnimationFrame: clearTimeout,
    performance: { now: () => Date.now() },
    Date,
    Math,
    JSON,
    Map,
    Set,
    WeakMap,
    Promise,
    URL,
    URLSearchParams,
    Blob: class Blob {},
    FormData: class FormData {},
    FileReader: class FileReader {},
    AbortController,
    CSS: { escape: (s) => String(s).replace(/([^\w-])/g, "\\$1") },
    localStorage,
    sessionStorage,
    fetch: mockFetch,
    WebSocket: MockWebSocket,
    Notification: { permission: "default", requestPermission: async () => "default" },
    navigator: {
      userAgent: "Mozilla/5.0 Test",
      clipboard: { writeText: async () => {} },
      vibrate: () => {},
    },
    location: {
      protocol: "http:",
      host: "localhost:8000",
      hostname: "localhost",
      origin: "http://localhost:8000",
      pathname: "/",
      search: "",
      hash: "",
    },
    history: { replaceState: () => {}, pushState: () => {} },
    matchMedia: () => ({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
    }),
    ResizeObserver: class ResizeObserver { observe() {} unobserve() {} disconnect() {} },
    IntersectionObserver: class IntersectionObserver { observe() {} unobserve() {} disconnect() {} },
    MutationObserver: class MutationObserver { observe() {} disconnect() {} },
    marked: undefined,
    hljs: undefined,
    confirm: () => true,
    alert: () => {},
    document: doc,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.window.innerWidth = 1440;
  sandbox.window.innerHeight = 900;
  sandbox.window.addEventListener = () => {};
  sandbox.window.removeEventListener = () => {};
  sandbox.window.dispatchEvent = () => true;
  sandbox.window.getComputedStyle = () => ({ getPropertyValue: () => "" });

  vm.createContext(sandbox);
  vm.runInContext(appCode, sandbox, { filename: "app.js" });

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  await sleep(90);

  const toggleBtn = doc.getElementById("toggleSidebarBtn");
  const toggleRadar = doc.getElementById("sidebarToggleRadar");
  const toggleDot = doc.getElementById("sidebarToggleAlertDot");
  const actBadges = doc.getElementById("activitySectionBadges");
  const actSection = doc.getElementById("sidebarActivitySection");
  const projectsTreeList = doc.getElementById("projectsTreeList");

  // =========================================================================
  // CHECK 1: Anti-clutter empty state (0 running, 0 queued, 0 unread)
  // =========================================================================
  assert(!toggleBtn.classList.contains("has-radar-pills"), "Empty state: #toggleSidebarBtn must NOT have .has-radar-pills");
  assert(toggleRadar.classList.contains("hidden"), "Empty state: #sidebarToggleRadar must be .hidden");
  assert.strictEqual(toggleRadar.innerHTML, "", "Empty state: #sidebarToggleRadar innerHTML must be empty");
  assert(toggleDot.classList.contains("hidden"), "Empty state: legacy #sidebarToggleAlertDot must stay .hidden");
  assert(actBadges.classList.contains("hidden"), "Empty state: #activitySectionBadges must be .hidden");
  assert.strictEqual(doc.title, "Antigravity Hub", "Empty state: document.title has no counter prefix");

  // Open Architect HQ chat c_hq so background chats are truly in background
  const projGroup = projectsTreeList.querySelector(".proj-tree-group");
  assert(projGroup, "HubProj group rendered in #projectsTreeList");
  if (!projGroup.classList.contains("expanded")) {
    projGroup.querySelector(".proj-tree-header").click();
    await sleep(30);
  }
  const hqRow = projectsTreeList
    .querySelectorAll(".nav-item")
    .find((el) => el.dataset.chatId === "c_hq");
  assert(hqRow, "Found c_hq in sidebar");
  hqRow.click();
  await sleep(50);

  // Also test cold queue hydration on a running chat without queue_count (simulating live pre-restart server!)
  chatsDb.set("c_cold", {
    id: "c_cold",
    title: "Холодный старт",
    kind: "project",
    project_name: "HubProj",
    access_mode: "write_files",
    pinned: 0,
    sort_order: 6,
    is_generating: true,
    updated_at: nowIso,
    // Notice: NO queue_count field on c_cold!
  });
  queuesByChat.set("c_cold", {
    items: [{ id: "qc1", prompt: "Задача из холодной гидратации" }],
    paused: false,
  });
  lastWs.emit({ type: "chat_updated", chat: chatsDb.get("c_cold") });
  // Open unified Send-to-Code modal to trigger hydrateColdChatQueues(getKnownProjectChats("HubProj"))
  doc.getElementById("messagesList").querySelector("[data-send-to-code]").click();
  await sleep(50);
  const handoffModalCold = doc.getElementById("handoffNewTabModal");
  const stcColdItem = handoffModalCold.querySelector('[data-handoff-target="c_cold"]');
  assert(
    stcColdItem && stcColdItem.textContent.includes("Очередь +1"),
    `Cold queue hydration must populate +1 queue without server restart! Got: ${stcColdItem && stcColdItem.textContent}`
  );
  handoffModalCold.querySelector('[data-close-modal="handoffNewTabModal"]').click();
  // Clean up c_cold back to idle/empty before Check 2 exact counts
  chatsDb.get("c_cold").is_generating = false;
  queuesByChat.set("c_cold", { items: [], paused: false });
  lastWs.emit({ type: "queue_updated", chat_id: "c_cold", queue: [], paused: false });
  lastWs.emit({ type: "turn_done", chat_id: "c_cold" });
  doc.getElementById("clearUnreadDoneBtn").click();
  await sleep(25);

  // =========================================================================
  // CHECK 2 & 3: 2 running chats + 3 queued tasks + 4 completed tasks (incl. batch of 3!)
  //   - c_done_a completes 3 queued turns in background -> runsCount = 3 ("Готово · 3")
  //   - c_done_b completes 1 turn in background -> runsCount = 1
  //   Total unread completed tasks = 3 + 1 = 4!
  //   - c_vis & c_back are running (2 running chats), and c_vis gets +3 tasks in queue via WS queue_updated!
  // =========================================================================
  assert(lastWs, "WebSocket connected");

  // Simulate 3 sequential turns finishing in background on c_done_a (batch run!)
  for (let i = 1; i <= 3; i++) {
    lastWs.emit({ type: "assistant_start", chat_id: "c_done_a" });
    lastWs.emit({ type: "turn_done", chat_id: "c_done_a" });
  }
  // And 1 turn finishing in background on c_done_b
  lastWs.emit({ type: "assistant_start", chat_id: "c_done_b" });
  lastWs.emit({ type: "turn_done", chat_id: "c_done_b" });
  await sleep(30);

  // Verify c_done_a pill shows batch count "Готово · 3"
  const doneAPill = doc.getElementById("sidebarActivityList").querySelector('.nav-item[data-chat-id="c_done_a"] .chat-state-pill');
  assert(doneAPill, "c_done_a pill rendered in Activity");
  assert(
    doneAPill.textContent.includes("Готово · 3"),
    `Expected batch pill 'Готово · 3' on c_done_a, got: ${doneAPill.textContent}`
  );

  // Start 2 running chats: c_vis and c_back
  chatsDb.get("c_vis").is_generating = true;
  chatsDb.get("c_back").is_generating = true;
  lastWs.emit({ type: "assistant_start", chat_id: "c_vis" });
  lastWs.emit({ type: "assistant_start", chat_id: "c_back" });

  // Emit WS queue_updated with 3 queued tasks on c_vis!
  const threeQueueItems = [
    { id: "q1", prompt: "Первая задача: компактный спойлер шагов" },
    { id: "q2", prompt: "Вторая задача: адаптивная шапка" },
    { id: "q3", prompt: "Третья задача: тесты очередей" },
  ];
  queuesByChat.set("c_vis", { items: threeQueueItems, paused: false });
  lastWs.emit({
    type: "queue_updated",
    chat_id: "c_vis",
    queue: threeQueueItems,
    paused: false,
  });
  await sleep(40);

  // Check #toggleSidebarBtn global radar capsule: ● 2, +3, ✓ 4
  assert(toggleBtn.classList.contains("has-radar-pills"), "#toggleSidebarBtn must have .has-radar-pills");
  assert(!toggleRadar.classList.contains("hidden"), "#sidebarToggleRadar must be visible");
  const segRun = toggleRadar.querySelector(".trp-seg.is-run");
  const segDone = toggleRadar.querySelector(".trp-seg.is-done");
  assert(segRun, ".trp-seg.is-run present");
  assert(segRun.textContent.includes("2") && segRun.textContent.includes("+3"), `Expected '2' and '+3' in .trp-seg.is-run, got: ${segRun.textContent}`);
  assert(segDone && segDone.textContent.includes("✓4"), `Expected '✓4' in .trp-seg.is-done, got: ${segDone ? segDone.textContent : "null"}`);
  assert.strictEqual(doc.title, "(● 2 · +3 · ✓ 4) Antigravity Hub", "Browser tab title reflects ● 2 · +3 · ✓ 4");

  // Check #sidebarActivitySection header separate mini-badges: [● 2] [+3 оч.] [✓ 4]
  assert(!actBadges.classList.contains("hidden"), "#activitySectionBadges must be visible");
  assert(actBadges.querySelector(".act-sec-pill.is-run").textContent.includes("2"), "Activity run badge shows 2");
  assert.strictEqual(actBadges.querySelector(".act-sec-pill.is-queue").textContent, "+3 оч.", "Activity queue badge shows '+3 оч.'");
  assert.strictEqual(actBadges.querySelector(".act-sec-pill.is-done").textContent, "✓ 4", "Activity done badge shows '✓ 4'");
  assert(doc.getElementById("activitySectionCount").classList.contains("hidden"), "Grey single #activitySectionCount hides when semantic badges are active");

  // Check sidebar row pill on c_vis: "В работе · +3" + tooltip with #1, #2, #3
  const visSidebarPill = doc.getElementById("sidebarScroll").querySelector('.nav-item[data-chat-id="c_vis"] .chat-state-pill');
  assert(visSidebarPill, "c_vis sidebar pill exists");
  assert(visSidebarPill.textContent.includes("В работе · +3"), `Expected 'В работе · +3', got: ${visSidebarPill.textContent}`);
  assert(
    visSidebarPill.title.includes("#1 Первая задача") &&
    visSidebarPill.title.includes("#2 Вторая задача") &&
    visSidebarPill.title.includes("#3 Третья задача"),
    `Expected #1..#3 tooltip on sidebar pill, got: ${visSidebarPill.title}`
  );

  // Check project folder header counter .proj-count-queued ("⏳ +3")
  const projQueuedEl = projectsTreeList.querySelector(".proj-count-queued");
  assert(projQueuedEl && projQueuedEl.textContent.includes("+3"), "Project header displays '⏳ +3'");

  // Check Architect HQ handoff chip under message 102 in c_hq: "Выполняется · +3 в оч."
  const handoffChip = doc.getElementById("messagesList").querySelector(".msg-code-handoff-chip");
  assert(handoffChip, "Handoff chip rendered under assistant message in c_hq");
  assert(
    handoffChip.textContent.includes("Выполняется · +3 в оч."),
    `Expected handoff chip to show 'Выполняется · +3 в оч.', got: ${handoffChip.textContent}`
  );

  // Open unified "🚀 В Код" modal on message 102 and check queue pills on c_vis (+3) & c_back (#1)
  const sendToCodeBtn = doc.getElementById("messagesList").querySelector("[data-send-to-code]");
  assert(sendToCodeBtn, "Found [data-send-to-code] button");
  sendToCodeBtn.click();
  await sleep(30);

  const stcModal = doc.getElementById("handoffNewTabModal");
  assert(!stcModal.classList.contains("hidden"), "Unified Send-to-Code modal opened");
  const stcVisItem = stcModal.querySelector('[data-handoff-target="c_vis"]');
  const stcBackItem = stcModal.querySelector('[data-handoff-target="c_back"]');
  assert(stcVisItem && stcVisItem.textContent.includes("Очередь +3"), `Expected 'Очередь +3', got: ${stcVisItem && stcVisItem.textContent}`);
  assert(stcVisItem.querySelector(".handoff-pill.is-queued"), "Stacked queue pill has .handoff-pill.is-queued");
  assert(stcBackItem && stcBackItem.textContent.includes("в очередь #1"), `Expected 'в очередь #1', got: ${stcBackItem && stcBackItem.textContent}`);
  stcModal.querySelector('[data-close-modal="handoffNewTabModal"]').click();

  // =========================================================================
  // CHECK 4: Open c_vis on mobile (<= 680px) -> #activeChatStatePill, .wtab-queue-tag,
  //          collapsible 1-line #composerQueueBar, expand via header & via #activeChatStatePill,
  //          and cold queue hydration without server restart!
  // =========================================================================
  sandbox.window.innerWidth = 390; // Smartphone viewport <= 680px
  const visRow = projectsTreeList.querySelector('.nav-item[data-chat-id="c_vis"]');
  visRow.click();
  await sleep(50);

  // Workspace tab tag .wtab-queue-tag "+3"
  const wtabQueueTag = doc.getElementById("workspaceTabsList").querySelector('.wtab .wtab-queue-tag');
  assert(wtabQueueTag && wtabQueueTag.textContent === "+3", `Expected .wtab-queue-tag '+3', got: ${wtabQueueTag && wtabQueueTag.textContent}`);

  // Open chat header pill #activeChatStatePill shows "В работе · +3" / "В раб. +3"
  const headerPill = doc.getElementById("activeChatStatePill");
  assert(!headerPill.classList.contains("hidden"), "#activeChatStatePill visible");
  assert(headerPill.querySelector(".ocsp-full").textContent === "В работе · +3", "Header pill full text is 'В работе · +3'");
  assert(headerPill.querySelector(".ocsp-short").textContent === "В раб. +3", "Header pill short mobile text is 'В раб. +3'");

  // Collapsible #composerQueueBar on mobile <= 680px starts collapsed (.is-collapsed)
  const qBar = doc.getElementById("composerQueueBar");
  assert(!qBar.classList.contains("hidden"), "#composerQueueBar visible");
  assert(qBar.classList.contains("is-collapsed"), "#composerQueueBar must default to .is-collapsed on mobile <= 680px");
  assert(qBar.querySelector(".queue-bar-preview").textContent.includes("#1"), "Collapsed bar shows #1 preview");

  // Click .queue-bar-header -> expands! Click again -> collapses!
  qBar.querySelector(".queue-bar-header").click();
  assert(!qBar.classList.contains("is-collapsed"), "Clicking .queue-bar-header expands #composerQueueBar");
  qBar.querySelector(".queue-bar-header").click();
  assert(qBar.classList.contains("is-collapsed"), "Second click on .queue-bar-header collapses #composerQueueBar");

  // Click #activeChatStatePill when queue > 0 -> expands #composerQueueBar & flashes .is-flash-highlight!
  headerPill.click();
  assert(!qBar.classList.contains("is-collapsed"), "Clicking #activeChatStatePill expands #composerQueueBar");
  assert(qBar.classList.contains("is-flash-highlight"), "Clicking #activeChatStatePill triggers .is-flash-highlight on #composerQueueBar");

  // Verify paused queue state (!isRunning && queue > 0) -> "⏸ Очередь · 3"
  chatsDb.get("c_vis").is_generating = false;
  queuesByChat.set("c_vis", { items: threeQueueItems, paused: true });
  lastWs.emit({ type: "turn_cancelled", chat_id: "c_vis" });
  lastWs.emit({ type: "queue_updated", chat_id: "c_vis", queue: threeQueueItems, paused: true });
  await sleep(30);
  assert(headerPill.classList.contains("is-queued"), "#activeChatStatePill gets .is-queued when queue is paused");
  assert(headerPill.querySelector(".ocsp-full").textContent === "Очередь · 3", "Paused header pill shows 'Очередь · 3'");

  // Verify opening c_done_a resets its batchCompletedRuns so a subsequent single run shows plain "Готово" (not "Готово · 4")
  const doneARow = doc.getElementById("sidebarActivityList").querySelector('.nav-item[data-chat-id="c_done_a"]');
  doneARow.click();
  await sleep(40);
  // Switch back to c_hq and run 1 new turn on c_done_a
  projectsTreeList.querySelector('.nav-item[data-chat-id="c_hq"]').click();
  await sleep(40);
  lastWs.emit({ type: "assistant_start", chat_id: "c_done_a" });
  lastWs.emit({ type: "turn_done", chat_id: "c_done_a" });
  await sleep(30);
  const doneAPillAfterReset = doc.getElementById("sidebarActivityList").querySelector('.nav-item[data-chat-id="c_done_a"] .chat-state-pill');
  assert.strictEqual(doneAPillAfterReset.textContent.trim(), "Готово", "After viewing c_done_a, batch counter reset cleanly to 1 ('Готово')");

  console.log("NODE_VM_UI_TESTS_OK");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""


class TestTaskCountersAndQueuesV76(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        self._live_db = config.DB_PATH
        self._live_stat_before = (
            (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            if self._live_db.exists()
            else None
        )

        self._tmp = tempfile.TemporaryDirectory(
            prefix="agy_test_v76_", ignore_cleanup_errors=True
        )
        self.tmp_root = Path(self._tmp.name).resolve()

        self._orig = {
            "cfg_db": config.DB_PATH,
            "db_db": database.DB_PATH,
            "cfg_cli": config.CLI_HOMES_DIR,
            "cfg_agy": config.AGY_CLI_DATA_DIR,
            "eng_agy": agy_engine.AGY_CLI_DATA_DIR,
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

        tmp_db = self.tmp_root / "test_v76.db"
        tmp_cli = self.tmp_root / "cli_homes"
        tmp_agy = self.tmp_root / "agy_cli_data"
        tmp_ws = self.tmp_root / "workspaces"
        tmp_sh = self.tmp_root / "shared_projects"
        tmp_up = self.tmp_root / "uploads"
        tmp_sbx = self.tmp_root / "chat_sandbox"
        for d in (tmp_cli, tmp_agy, tmp_ws, tmp_sh, tmp_up, tmp_sbx):
            d.mkdir(parents=True, exist_ok=True)

        config.DB_PATH = tmp_db
        database.DB_PATH = tmp_db
        config.CLI_HOMES_DIR = tmp_cli
        config.AGY_CLI_DATA_DIR = tmp_agy
        agy_engine.AGY_CLI_DATA_DIR = tmp_agy
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

    async def asyncTearDown(self) -> None:
        import app.agy_engine as agy_engine
        import app.brain_reader as brain_reader
        import app.config as config
        import app.database as database

        config.DB_PATH = self._orig["cfg_db"]
        database.DB_PATH = self._orig["db_db"]
        config.CLI_HOMES_DIR = self._orig["cfg_cli"]
        config.AGY_CLI_DATA_DIR = self._orig["cfg_agy"]
        agy_engine.AGY_CLI_DATA_DIR = self._orig["eng_agy"]
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

        if self._live_stat_before is not None:
            live_after = (self._live_db.stat().st_mtime_ns, self._live_db.stat().st_size)
            self.assertEqual(
                self._live_stat_before,
                live_after,
                "Live data/hub.db must NEVER be modified by tests!",
            )

    def test_static_cache_bumped_to_v76_and_anchors_present(self) -> None:
        sw_text = (ROOT_DIR / "app" / "static" / "sw.js").read_text(encoding="utf-8")
        html_text = (ROOT_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        css_text = (ROOT_DIR / "app" / "static" / "styles.css").read_text(encoding="utf-8")

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_text)
        self.assertIsNotNone(m_sw)
        sw_ver = int(m_sw.group(1))
        self.assertGreaterEqual(sw_ver, 76)
        self.assertIn(f'"/static/styles.css?v={sw_ver}"', sw_text)
        self.assertIn(f'"/static/app.js?v={sw_ver}"', sw_text)
        self.assertIn(f'href="/static/styles.css?v={sw_ver}"', html_text)
        self.assertIn(f'src="/static/app.js?v={sw_ver}"', html_text)

        self.assertIn('id="sidebarToggleRadar"', html_text)
        self.assertIn('id="activitySectionBadges"', html_text)
        for cls_selector in (
            ".toggle-sidebar-btn.has-radar-pills",
            ".toggle-radar-pills",
            ".trp-seg",
            ".activity-sec-badges",
            ".proj-count-queued",
            ".chat-state-pill.is-queued",
            ".wtab-queue-tag",
            ".composer-queue-bar.is-collapsed",
        ):
            self.assertIn(cls_selector, css_text)

    async def test_backend_api_list_and_update_chat_expose_queue_count_and_paused(self) -> None:
        import app.database as database
        import app.main as main_mod

        await database.init_db()
        user = await database.create_user(
            username="queue_tester",
            password="secret",
            display_name="Queue Tester",
            role="user",
            max_access_mode="write_files",
        )
        uid = user["id"]

        c1 = await database.create_chat(
            kind="project", project_name="DemoProj", title="Tab 1", owner_id=uid
        )
        c2 = await database.create_chat(
            kind="project", project_name="DemoProj", title="Tab 2", owner_id=uid
        )

        class DummyReq:
            def __init__(self, usr: dict):
                self.state = type("S", (), {"user": usr})()
                self.headers = {}
                self.cookies = {}

        req = DummyReq(user)

        try:
            main_mod.CHAT_TURN_QUEUES[c1["id"]] = [
                {"id": "q1", "prompt": "Task 1"},
                {"id": "q2", "prompt": "Task 2"},
                {"id": "q3", "prompt": "Task 3"},
            ]
            main_mod.CHAT_QUEUE_PAUSED.add(c2["id"])
            main_mod.CHAT_TURN_QUEUES[c2["id"]] = [{"id": "q_paused", "prompt": "Paused task"}]

            listed = await main_mod.api_list_chats(
                kind=None,
                project_name=None,
                query=None,
                recent=False,
                limit=None,
                current_user=user,
            )
            by_id = {c["id"]: c for c in listed["chats"]}

            self.assertEqual(by_id[c1["id"]]["queue_count"], 3)
            self.assertFalse(by_id[c1["id"]]["queue_paused"])
            self.assertEqual(by_id[c2["id"]]["queue_count"], 1)
            self.assertTrue(by_id[c2["id"]]["queue_paused"])

            updated = await main_mod.api_update_chat(
                c1["id"],
                main_mod.UpdateChatRequest(title="Tab 1 Renamed"),
                current_user=user,
            )
            self.assertEqual(updated["chat"]["queue_count"], 3)
            self.assertFalse(updated["chat"]["queue_paused"])
        finally:
            main_mod.CHAT_TURN_QUEUES.pop(c1["id"], None)
            main_mod.CHAT_TURN_QUEUES.pop(c2["id"], None)
            main_mod.CHAT_QUEUE_PAUSED.discard(c1["id"])
            main_mod.CHAT_QUEUE_PAUSED.discard(c2["id"])

    def test_frontend_global_counters_ws_queues_and_collapsible_bar_end_to_end(self) -> None:
        script_path = self.tmp_root / "run_ui_checks.js"
        script_path.write_text(NODE_VM_HARNESS_JS, encoding="utf-8")

        env = os.environ.copy()
        env["HUB_ROOT_DIR"] = str(ROOT_DIR)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        proc = subprocess.run(
            ["node", str(script_path)],
            cwd=str(ROOT_DIR),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            creationflags=creationflags,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Frontend VM test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("NODE_VM_UI_TESTS_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
