"""
Isolated unit & Node VM tests for Antigravity Hub v=85:
- Reliable chat auto-scroll guard (`scrollGuard` + `scrollToBottom` + `followBottomIfAllowed`),
  preventing false `state.autoScroll = false` resets on long prompts, DOM growth, or smooth scroll.
- User upward-scroll intent detection (`wheel`, inner `.thinking-content` / `.tools-spoiler-body` / `pre`
  scroll protection, `touchstart`/`touchmove`/`touchend`, `keydown` PageUp/Home/ArrowUp, scrollbar `scroll`).
- `ResizeObserver` shrinking `clientHeight` (context meter / queue bar / composer growth / mobile keyboard).
- Instant bottom follow across `sendCurrentPrompt`, `user_message`, `assistant_start` (both branches),
  `turn_done` / `turn_cancelled` / `turn_error`, and `continueInterruptedTurn`.
- `.thinking-content` scroll position preservation & auto-follow inside `updateSingleAssistantDom`.
- Cache version sync (`v>=85`) across `app/static/sw.js` and `app/static/index.html`.
- Strictly 0% GPU / 0 MB VRAM & zero modifications to live `data/hub.db`.
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
    bindEvents,
    scrollGuard,
    scrollToBottom,
    followBottomIfAllowed,
    hasScrollableUpAncestor,
    sendCurrentPrompt,
    applyLiveChatEventToCurrentState,
    continueInterruptedTurn,
    updateSingleAssistantDom,
    scheduleMessageElementUpdate,
    buildAssistantInnerHtml,
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

function makeEmitterEl(id = "", tag = "DIV") {
  const listeners = new Map();
  const children = [];
  const el = {
    id,
    tagName: tag,
    innerHTML: "",
    textContent: "",
    value: "",
    style: { setProperty: () => {}, removeProperty: () => {} },
    dataset: {},
    classList: makeClassList(),
    scrollTop: 0,
    scrollHeight: 600,
    clientHeight: 600,
    clientWidth: 800,
    children,
    childNodes: children,
    addEventListener: (ev, fn) => {
      if (!listeners.has(ev)) listeners.set(ev, []);
      listeners.get(ev).push(fn);
    },
    removeEventListener: (ev, fn) => {
      const arr = listeners.get(ev);
      if (!arr) return;
      const idx = arr.indexOf(fn);
      if (idx >= 0) arr.splice(idx, 1);
    },
    dispatch: (ev, eventObj = {}) => {
      const evt = Object.assign({ type: ev, target: el, preventDefault: () => {}, stopPropagation: () => {} }, eventObj);
      const arr = listeners.get(ev) || [];
      for (const fn of arr) fn(evt);
      return evt;
    },
    scrollTo: (opts) => {
      el._scrollToCalls.push(opts);
      if (opts && opts.behavior === "instant" && typeof opts.top === "number") {
        el.scrollTop = opts.top;
      }
    },
    _scrollToCalls: [],
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild: (child) => { children.push(child); return child; },
    get firstChild() { return children[0] || null; },
    insertBefore(child, reference) {
      const index = children.indexOf(reference);
      children.splice(index < 0 ? children.length : index, 0, child);
      return child;
    },
    replaceChildren: (...nodes) => { children.length = 0; children.push(...nodes); },
    remove: () => {},
    closest: () => null,
    setAttribute: () => {},
    getAttribute: () => null,
    removeAttribute: () => {},
    focus: () => {},
    blur: () => {},
    setSelectionRange: () => {},
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 800, height: 600, bottom: 600, right: 800 }),
  };
  return el;
}

const byId = new Map();
function getElById(id) {
  if (!byId.has(id)) {
    byId.set(id, makeEmitterEl(id, id === "promptInput" ? "TEXTAREA" : "DIV"));
  }
  return byId.get(id);
}

const docListeners = new Map();
const winListeners = new Map();
let activeEl = null;

const docStub = {
  hidden: false,
  documentElement: makeEmitterEl("html", "HTML"),
  body: makeEmitterEl("body", "BODY"),
  get activeElement() { return activeEl; },
  getElementById: (id) => getElById(id),
  querySelector: () => makeEmitterEl("", "DIV"),
  querySelectorAll: () => [],
  createElement: (t) => makeEmitterEl("", String(t || "DIV").toUpperCase()),
  createDocumentFragment: () => makeEmitterEl("", "FRAG"),
  addEventListener: (ev, fn) => {
    if (!docListeners.has(ev)) docListeners.set(ev, []);
    docListeners.get(ev).push(fn);
  },
  dispatch: (ev, eventObj = {}) => {
    const evt = Object.assign({ type: ev, target: docStub.body, preventDefault: () => {}, stopPropagation: () => {} }, eventObj);
    const arr = docListeners.get(ev) || [];
    for (const fn of arr) fn(evt);
  },
};

let fakeNow = 1000;
let resizeObserverCb = null;
const rafQueue = [];

class FakeResizeObserver {
  constructor(cb) {
    this._cb = cb;
    resizeObserverCb = cb;
  }
  observe() {}
  disconnect() {}
}

const sandbox = {
  console,
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
  navigator: { userAgent: "node-test", clipboard: {} },
  location: { protocol: "http:", host: "127.0.0.1:8765", origin: "http://127.0.0.1:8765", pathname: "/" },
  CSS: { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&") },
  matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  requestAnimationFrame: (fn) => {
    rafQueue.push(fn);
    return rafQueue.length;
  },
  cancelAnimationFrame: () => {},
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

const api = sandbox.__AGY_TEST__;
const mc = api.els.messagesContainer;
const ml = api.els.messagesList;
const scrollBottomBtn = api.els.scrollBottomBtn;

const domRows = new Map();
ml.appendChild = (node) => {
  if (node && node.dataset && node.dataset.msgId) {
    domRows.set(String(node.dataset.msgId), node);
  }
  return node;
};
ml.querySelector = (sel) => {
  const m = String(sel).match(/\[data-msg-id="([^"]+)"\]/);
  if (m) return domRows.get(m[1].replace(/\\/g, "")) || null;
  return null;
};

api.bindEvents();

// Case 1: Long prompt send + scroll event right as scrollHeight increases does NOT reset autoScroll
api.state.activeChatId = "chat-scroll-1";
api.state.activeChat = { id: "chat-scroll-1", kind: "chat", model: "gemini-4-carbon-high" };
api.state.messages = [];
mc.clientHeight = 600;
mc.scrollHeight = 600;
mc.scrollTop = 0;

// Simulate inserting a 12-line user prompt
api.els.promptInput.value = Array.from({ length: 12 }, (_, i) => `Line ${i + 1}: important requirement`).join("\n");
// Mock WebSocket
api.state.ws = { readyState: 1, OPEN: 1, send: () => {} };

const origAppend = ml.appendChild;
ml.appendChild = (node) => {
  // User message makes scrollHeight jump +300px
  mc.scrollHeight += 300;
  return origAppend(node);
};

// Trigger sendCurrentPrompt
(async () => {
  await api.sendCurrentPrompt();
  const afterSendImmediateTop = mc.scrollTop;
  const afterSendAutoScroll = api.state.autoScroll;

  // Simulate browser firing a scroll event where scrollHeight just grew another +180px
  // (distFromBottom = 180 >= 72), while scrollTop has NOT moved up
  mc.scrollHeight += 180;
  mc.dispatch("scroll");
  const afterHeightGrowAutoScroll = api.state.autoScroll;

  // Flush queued rAF from sendCurrentPrompt -> snaps back to new scrollHeight
  while (rafQueue.length > 0) {
    const fn = rafQueue.shift();
    fn();
  }
  const afterSendRafTop = mc.scrollTop;

  // Case 2: user_message confirmation + contextMeterBar shrinking clientHeight -> ResizeObserver keeps bottom
  const optMsg = api.state.messages[0];
  const optRow = domRows.get(String(optMsg.id));
  if (optRow) {
    optRow.replaceWith = (newNode) => {
      domRows.delete(String(optMsg.id));
      if (newNode && newNode.dataset && newNode.dataset.msgId) {
        domRows.set(String(newNode.dataset.msgId), newNode);
      }
      mc.scrollHeight += 20;
    };
  }
  api.applyLiveChatEventToCurrentState({
    type: "user_message",
    chat_id: "chat-scroll-1",
    message: {
      id: 101,
      chat_id: "chat-scroll-1",
      role: "user",
      content: optMsg.content,
      attachments: [],
      usage: {},
    },
  });
  const afterUserMsgTop = mc.scrollTop;

  // Context meter opens, shrinking clientHeight by 36px
  mc.clientHeight = 564;
  resizeObserverCb();
  const afterResizeShrinkTop = mc.scrollTop;
  const afterResizeAutoScroll = api.state.autoScroll;

  // Case 3: assistant_start (new card + existing card) & turn_done follow bottom
  api.applyLiveChatEventToCurrentState({
    type: "assistant_start",
    chat_id: "chat-scroll-1",
    message: {
      id: 102,
      chat_id: "chat-scroll-1",
      role: "assistant",
      status: "streaming",
      content: "",
      thinking: "Step 1: analyzing",
      has_thinking: true,
    },
  });
  const afterAssistStartTop = mc.scrollTop;
  const afterAssistStartAutoScroll = api.state.autoScroll;

  // assistant_start (existing card branch) + turn_done
  mc.scrollHeight += 90;
  api.applyLiveChatEventToCurrentState({
    type: "assistant_start",
    chat_id: "chat-scroll-1",
    message: {
      id: 102,
      chat_id: "chat-scroll-1",
      role: "assistant",
      status: "streaming",
      content: "",
      thinking: "Step 2: planning",
      has_thinking: true,
    },
  });
  const afterExistingAssistStartTop = mc.scrollTop;

  mc.scrollHeight += 60;
  api.applyLiveChatEventToCurrentState({
    type: "turn_done",
    chat_id: "chat-scroll-1",
    message: {
      id: 102,
      chat_id: "chat-scroll-1",
      role: "assistant",
      status: "done",
      content: "Done!",
      thinking: "Step 2: planning",
      has_thinking: true,
    },
  });
  const afterTurnDoneTop = mc.scrollTop;

  // Case 4: wheel up inside scrollable .thinking-content (scrollTop > 0) or <pre> inside .tools-spoiler-body does NOT kill main autoScroll
  // (v87: the mock cards are 600px tall > clientHeight, so the previous turn legitimately pinned the answer start;
  //  re-arm the tail-follow mode explicitly so that the nested-scroll guard is what gets tested here)
  api.state.autoScroll = true;
  api.scrollGuard.mode = "follow_tail";
  api.scrollGuard.lockedAtAnswerStart = false;
  const innerThinking = {
    scrollTop: 80,
    scrollHeight: 500,
    clientHeight: 320,
    parentElement: null,
    closest: (sel) => (sel.includes(".thinking-content") ? innerThinking : null),
  };
  const innerTarget = {
    closest: (sel) => (sel.includes(".thinking-content") ? innerThinking : null),
  };
  const innerToolsBody = {
    scrollTop: 65,
    scrollHeight: 420,
    clientHeight: 240,
    parentElement: null,
    closest: (sel) => (sel.includes(".tools-spoiler-body") ? innerToolsBody : null),
  };
  const innerPreInTools = {
    scrollTop: 0,
    scrollHeight: 40,
    clientHeight: 40,
    parentElement: {
      closest: (sel) => (sel.includes(".tools-spoiler-body") ? innerToolsBody : null),
    },
    closest: (sel) => (sel.includes("pre") ? innerPreInTools : null),
  };
  const preTarget = {
    closest: (sel) => (sel.includes("pre") ? innerPreInTools : null),
  };
  mc.scrollTop = mc.scrollHeight - mc.clientHeight;
  fakeNow += 200;
  mc.dispatch("wheel", { deltaY: -120, target: innerTarget });
  const autoScrollAfterInnerWheel = api.state.autoScroll;
  mc.dispatch("wheel", { deltaY: -120, target: preTarget });
  const autoScrollAfterNestedPreWheel = api.state.autoScroll;

  // Case 5: Real user wheel up on main container (past 200px) disables autoScroll & shows scrollBottomBtn
  mc.scrollTop = Math.max(0, mc.scrollHeight - mc.clientHeight - 240);
  mc.dispatch("wheel", { deltaY: -120, target: mc });
  mc.dispatch("scroll");
  const autoScrollAfterOuterWheelUp = api.state.autoScroll;
  const btnVisibleAfterScrollUp = !scrollBottomBtn.classList.contains("hidden");

  // ResizeObserver when autoScroll is FALSE must NOT force-scroll back down
  const savedTopWhileReading = mc.scrollTop;
  mc.clientHeight = 520;
  resizeObserverCb();
  const topAfterResizeWhileReading = mc.scrollTop;

  // Case 6: Clicking scrollBottomBtn enables smooth scroll + programmaticUntil protects autoScroll during frames
  scrollBottomBtn.dispatch("click");
  const autoScrollOnBtnClick = api.state.autoScroll;
  // Simulate mid-smooth-scroll event at distFromBottom = 300 within 550ms
  fakeNow += 100;
  mc.scrollTop = mc.scrollHeight - mc.clientHeight - 300;
  mc.dispatch("scroll");
  const autoScrollMidSmooth = api.state.autoScroll;
  // User interrupts smooth scroll via wheel up -> cancels programmaticUntil and disables autoScroll
  mc.dispatch("wheel", { deltaY: -120, target: mc });
  const autoScrollInterruptedSmooth = api.state.autoScroll;

  // Case 7: Scrolling back down manually into <72px zone re-enables autoScroll
  mc.scrollTop = mc.scrollHeight - mc.clientHeight - 30;
  mc.dispatch("scroll");
  const autoScrollReturnedBottom = api.state.autoScroll;

  // Case 8: Touch drag up disables autoScroll
  mc.dispatch("touchstart", { touches: [{ clientY: 300 }] });
  mc.scrollTop = Math.max(0, mc.scrollHeight - mc.clientHeight - 140);
  mc.dispatch("touchmove", { touches: [{ clientY: 420 }], target: mc });
  mc.dispatch("touchend", {});
  const autoScrollAfterTouchUp = api.state.autoScroll;

  // Case 9: keydown PageUp clears programmaticUntil when focus is outside textarea, ignores inside textarea
  api.scrollToBottom(false); // sets programmaticUntil = now + 550
  activeEl = api.els.promptInput;
  docStub.dispatch("keydown", { key: "ArrowUp" });
  const progUntilInsideTextarea = api.scrollGuard.programmaticUntil;
  activeEl = docStub.body;
  docStub.dispatch("keydown", { key: "PageUp" });
  const progUntilOutsideInput = api.scrollGuard.programmaticUntil;

  // Case 10: continueInterruptedTurn forces autoScroll = true and scrolls to bottom
  api.state.generatingChats.delete("chat-scroll-1");
  api.state.autoScroll = false;
  const errMsg = {
    id: 103,
    chat_id: "chat-scroll-1",
    role: "assistant",
    status: "error",
    content: "Partial\n\n---\n⚠️ **Ошибка:** timeout",
    thinking: "",
    has_thinking: false,
    tool_calls: [],
  };
  api.state.messages.push(errMsg);
  const errRow = makeEmitterEl("", "DIV");
  errRow.dataset.msgId = "103";
  const errCard = makeEmitterEl("", "DIV");
  errRow.querySelector = (sel) => (sel === ".assistant-card" ? errCard : null);
  domRows.set("103", errRow);
  mc.scrollHeight += 120;
  mc.scrollTop = 10;
  api.continueInterruptedTurn(103);
  const afterContinueAutoScroll = api.state.autoScroll;
  const afterContinueAtBottom = mc.scrollTop === mc.scrollHeight;
  const continueMode = api.scrollGuard.mode;
  const continueAnchor = api.scrollGuard.anchorMsgId;

  // Case 11: updateSingleAssistantDom preserves/follows .thinking-content scrollTop
  function runThinkingScrollCase(thinkingActive, manualOpen, prevScrollTop, prevScrollHeight) {
    const msg = {
      id: 200,
      chat_id: "chat-scroll-1",
      role: "assistant",
      status: thinkingActive ? "streaming" : "done",
      content: thinkingActive ? "" : "Final answer",
      thinking: "Line 1\nLine 2\nLine 3\nLine 4",
      has_thinking: true,
      _thinkingActive: thinkingActive,
      tool_calls: [],
    };
    api.state.messages = [msg];
    const prevThinkingEl = {
      scrollTop: prevScrollTop,
      scrollHeight: prevScrollHeight,
      clientHeight: 320,
    };
    const nextThinkingEl = {
      scrollTop: 0,
      scrollHeight: 950,
      clientHeight: 320,
    };
    const existingDetails = {
      open: true,
      dataset: { userOpen: manualOpen ? "1" : "" },
      querySelector: (s) => (s === ".thinking-content" ? prevThinkingEl : null),
    };
    const cardObj = {
      innerHTML: "",
      querySelector: (s) => (s === "details.thinking-box[open] .thinking-content" ? nextThinkingEl : null),
    };
    const rowObj = {
      ...makeEmitterEl(),
      querySelector: (s) => {
        if (s === "details.thinking-box") return existingDetails;
        if (s === ".assistant-card") return cardObj;
        return null;
      },
    };
    domRows.set("200", rowObj);
    api.updateSingleAssistantDom(msg);
    return nextThinkingEl.scrollTop;
  }

  const thinkingAutoFollowTop = runThinkingScrollCase(true, false, 470, 800); // dist = 800 - 470 - 320 = 10 <= 28 -> follow
  const thinkingManualUpTop = runThinkingScrollCase(true, false, 120, 800);   // dist = 360 > 28 -> preserve 120
  const thinkingDoneManualTop = runThinkingScrollCase(false, true, 160, 800); // done + manualOpen -> preserve 160

  console.log(JSON.stringify({
    afterSendImmediateTop,
    afterSendAutoScroll,
    afterHeightGrowAutoScroll,
    afterSendRafTop,
    afterUserMsgTop,
    afterResizeShrinkTop,
    afterResizeAutoScroll,
    afterAssistStartTop,
    afterAssistStartAutoScroll,
    afterExistingAssistStartTop,
    afterTurnDoneTop,
    autoScrollAfterInnerWheel,
    autoScrollAfterNestedPreWheel,
    autoScrollAfterOuterWheelUp,
    btnVisibleAfterScrollUp,
    savedTopWhileReading,
    topAfterResizeWhileReading,
    autoScrollOnBtnClick,
    autoScrollMidSmooth,
    autoScrollInterruptedSmooth,
    autoScrollReturnedBottom,
    autoScrollAfterTouchUp,
    progUntilInsideTextareaGtNow: progUntilInsideTextarea > fakeNow,
    progUntilOutsideInput,
    afterContinueAutoScroll,
    afterContinueAtBottom,
    continueMode,
    continueAnchor,
    thinkingAutoFollowTop,
    thinkingManualUpTop,
    thinkingDoneManualTop,
    finalScrollHeight: mc.scrollHeight,
  }));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""


class TestChatAutoscrollV85(unittest.TestCase):
    def test_cache_version_sync_and_syntax(self) -> None:
        sw_text = SW_JS_PATH.read_text(encoding="utf-8")
        html_text = INDEX_HTML_PATH.read_text(encoding="utf-8")

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_text)
        self.assertIsNotNone(m_sw, "CACHE_NAME must match agy-hub-v<N>")
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 85)
        self.assertIn(f'"/static/styles.css?v={ver}"', sw_text)
        self.assertIn(f'"/static/app.js?v={ver}"', sw_text)
        self.assertIn(f'href="/static/styles.css?v={ver}"', html_text)
        self.assertIn(f'src="/static/app.js?v={ver}"', html_text)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for js_file in (APP_JS_PATH, SW_JS_PATH):
            proc = subprocess.run(
                ["node", "--check", str(js_file)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=creationflags,
            )
            self.assertEqual(proc.returncode, 0, f"Syntax error in {js_file}: {proc.stderr}")

    def test_autoscroll_guard_and_thinking_scroll_behavior(self) -> None:
        db_stat_before = (
            (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
            if LIVE_DB_PATH.exists()
            else None
        )

        env = dict(os.environ)
        env["AGY_APP_JS_PATH"] = str(APP_JS_PATH)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            ["node", "-e", NODE_HARNESS_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            creationflags=creationflags,
        )
        self.assertEqual(proc.returncode, 0, f"Node harness failed:\n{proc.stderr}\n{proc.stdout}")
        res = json.loads(proc.stdout.strip())

        self.assertTrue(res["afterSendAutoScroll"])
        self.assertEqual(res["afterSendImmediateTop"], 900)
        self.assertTrue(res["afterHeightGrowAutoScroll"], "DOM height growth must NOT reset autoScroll")
        self.assertEqual(res["afterSendRafTop"], 1080)
        self.assertEqual(res["afterUserMsgTop"], 1100)
        self.assertEqual(res["afterResizeShrinkTop"], 1100)
        self.assertTrue(res["afterResizeAutoScroll"])
        self.assertTrue(res["afterAssistStartAutoScroll"])
        # v87: a new answer is read from its beginning -> the tail is NOT dragged to the bottom,
        # but the position never jumps backwards either.
        self.assertGreaterEqual(res["afterAssistStartTop"], 1100)
        self.assertGreaterEqual(res["afterExistingAssistStartTop"], res["afterAssistStartTop"])
        self.assertGreaterEqual(res["afterTurnDoneTop"], res["afterExistingAssistStartTop"])

        self.assertTrue(
            res["autoScrollAfterInnerWheel"],
            "Wheel up inside scrollable .thinking-content must NOT break outer autoScroll",
        )
        self.assertTrue(
            res["autoScrollAfterNestedPreWheel"],
            "Wheel up on <pre> inside scrollable .tools-spoiler-body must NOT break outer autoScroll",
        )
        self.assertFalse(res["autoScrollAfterOuterWheelUp"])
        self.assertTrue(res["btnVisibleAfterScrollUp"])
        self.assertEqual(
            res["topAfterResizeWhileReading"],
            res["savedTopWhileReading"],
            "ResizeObserver must NOT scroll down when user scrolled up",
        )

        self.assertTrue(res["autoScrollOnBtnClick"])
        self.assertTrue(res["autoScrollMidSmooth"])
        self.assertFalse(res["autoScrollInterruptedSmooth"])
        self.assertTrue(res["autoScrollReturnedBottom"])
        self.assertFalse(res["autoScrollAfterTouchUp"])
        self.assertTrue(res["progUntilInsideTextareaGtNow"])
        self.assertEqual(res["progUntilOutsideInput"], 0)
        # v87: continuing an interrupted turn re-arms the reading controller on that very message
        self.assertEqual(res["continueMode"], "follow_answer_start")
        self.assertEqual(res["continueAnchor"], "103")

        self.assertEqual(res["thinkingAutoFollowTop"], 950)
        self.assertEqual(res["thinkingManualUpTop"], 120)
        self.assertEqual(res["thinkingDoneManualTop"], 160)

        db_stat_after = (
            (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
            if LIVE_DB_PATH.exists()
            else None
        )
        self.assertEqual(db_stat_before, db_stat_after, "Live data/hub.db must remain untouched!")


if __name__ == "__main__":
    unittest.main()
