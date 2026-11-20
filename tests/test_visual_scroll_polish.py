"""
Isolated UI tests for Antigravity Hub v=88 (visual & scroll polish), 0% GPU / 0 MB VRAM:

1. Real-browser (headless Edge/Chromium, ``--headless=new --disable-gpu``) streaming simulation of the
   real `app.js` on the real `index.html` + `styles.css`: sendCurrentPrompt -> assistant_start ->
   thinking_update -> tool_step -> 100 x text_delta (with unclosed code-fence height jitter) -> turn_done.
   Verifies the reading controller (`follow_answer_start` / `user_free` / `follow_tail`), the 5 stable
   `.assistant-card` slots, the seamless "Thinking... -> text" collapse and the `#scrollBottomBtn` flow.
2. WCAG 2.1 contrast audit of the expressive `.nav-item.active` style in all 16 palettes (8 x dark/light).
3. Geometry audit (`getBoundingClientRect`) on 5 breakpoints: no overlaps / no collapsed tab title /
   no horizontal overflow.
4. Syntax + regression checks (node --check, CSS braces balance, cache version sync).

The browser tests use their own local 127.0.0.1 static server on a temp copy of `app/static`
and never touch the live `data/hub.db` or the running server. Browser sub-processes are started with
`subprocess_hidden_kwargs()` (0 console windows).
"""

from __future__ import annotations

import html as html_lib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["ORT_DISABLE_ALL_GPU"] = "1"

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

STATIC_DIR = ROOT_DIR / "app" / "static"
APP_JS_PATH = STATIC_DIR / "app.js"
STYLES_CSS_PATH = STATIC_DIR / "styles.css"
INDEX_HTML_PATH = STATIC_DIR / "index.html"
SW_JS_PATH = STATIC_DIR / "sw.js"
LIVE_DB_PATH = ROOT_DIR / "data" / "hub.db"

try:  # pragma: no cover - the helper lives in the app config
    from app.config import subprocess_hidden_kwargs
except Exception:  # pragma: no cover

    def subprocess_hidden_kwargs() -> dict:
        if os.name != "nt":
            return {}
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        return {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            "startupinfo": si,
        }


PALETTES = ["emerald", "gemini", "linear", "claude", "nord", "amethyst", "copper", "amoled"]
MODES = ["dark", "light"]


# ---------------------------------------------------------------------------------------------
# Headless browser harness
# ---------------------------------------------------------------------------------------------
def find_browser() -> str | None:
    env_path = os.environ.get("AGY_TEST_BROWSER")
    if env_path and Path(env_path).exists():
        return env_path
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


EXPORT_TAIL = """globalThis.__AGY_TEST__ = {
    bindEvents, state, els, scrollGuard, findMsg, renderMessages, buildMessageDom,
    applyLiveChatEventToCurrentState, beginTurnScroll, followTurnReadingPosition,
    followBottomIfAllowed, finishTurnScroll, scrollToBottom, scrollToLastAnswerStartIfLong,
    updateSingleAssistantDom, scheduleMessageElementUpdate, sendCurrentPrompt,
    setupWorkspaceLayoutObservers, applyWorkspaceLayoutMetrics, computeWorkspaceWidthClasses,
    syncMessagesStreamingClass, syncComposerState, resizePromptInput,
    updateScrollBottomBtnState, enterUserFreeScroll, enterFollowTailScroll,
    syncMobileHeaderToolsSlot, setProjectAccessBarVisible, ANSWER_READ_TOP_OFFSET,
    buildAssistantInnerHtml, ASSISTANT_SLOT_ORDER,
  };
})();"""

HOST_HTML = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:#000">
<iframe id="f" style="border:0;display:block"></iframe>
<script>
const q = new URLSearchParams(location.search);
const f = document.getElementById("f");
f.style.width = q.get("w") + "px";
f.style.height = q.get("h") + "px";
f.src = "/fixture.html?" + q.toString();
</script>
</body></html>"""

COMMON_JS = r"""
// the scenarios replace window.fetch (to freeze the backend); reporting must keep the real one
const __agyRealFetch = window.fetch.bind(window);
window.__agyCommon = {
  frame: () => new Promise((r) => requestAnimationFrame(() => r())),
  sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
  report(out) {
    const rid = new URLSearchParams(location.search).get("rid") || "0";
    try {
      __agyRealFetch("/__report?rid=" + encodeURIComponent(rid), { method: "POST", body: JSON.stringify(out) });
    } catch (e) { document.title = "report-failed:" + e; }
  },
  progress(msg) {
    const rid = new URLSearchParams(location.search).get("rid") || "0";
    try { __agyRealFetch("/__report?rid=" + encodeURIComponent(rid) + "-progress", { method: "POST", body: String(msg) }); } catch (e) {}
  },
  rect(el) { return el ? el.getBoundingClientRect() : null; },
  overlap(a, b) {
    if (!a || !b) return 0;
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 0.5 && h > 0.5 ? w * h : 0;
  },
};
"""

SCROLL_SCENARIO_JS = r"""
(async function main() {
  const C = window.__agyCommon;
  const out = { errors: [], samples: [] };
  window.addEventListener("error", (e) => out.errors.push(String(e.message || e)));
  window.addEventListener("unhandledrejection", (e) =>
    out.errors.push("rej:" + String((e.reason && e.reason.stack) || e.reason)));
  try {
    const api = window.__AGY_TEST__;
    if (!api) throw new Error("test api missing");
    const frame = C.frame, sleep = C.sleep;
    const q = new URLSearchParams(location.search);
    document.documentElement.setAttribute("data-theme", q.get("theme") || "dark");
    document.documentElement.setAttribute("data-palette", q.get("palette") || "emerald");
    const c = document.getElementById("messagesContainer");
    c.style.flex = "none";
    c.style.height = "700px";
    c.style.minHeight = "700px";

    const CHAT = { id: "c1", kind: "chat", title: "Scroll lab", model: "gemini-4-carbon-high", is_generating: false };
    api.state.activeChatId = "c1";
    api.state.activeChat = Object.assign({}, CHAT);
    api.state.ws = { readyState: 1, OPEN: 1, send() {} };
    api.bindEvents();

    const para = (n) => "Paragraph " + n + ": " + "lorem ipsum dolor sit amet consectetur ".repeat(3);
    const hist = [];
    for (let i = 0; i < 6; i++) {
      hist.push({ id: 10 + i * 2, chat_id: "c1", role: "user", content: "Question " + i + " " + "text ".repeat(20), attachments: [], usage: {} });
      hist.push({ id: 11 + i * 2, chat_id: "c1", role: "assistant", status: "done", model: "gemini-4-carbon-high",
        content: [para(1), para(2), para(3), para(4)].join("\n\n"), thinking: "", has_thinking: false, tool_calls: [] });
    }
    api.state.messages = hist;
    api.renderMessages(true);
    await frame(); await frame();
    out.initialGap = c.scrollHeight - c.clientHeight - c.scrollTop;
    out.initialMode = api.scrollGuard.mode;
    out.slotsInHistory = Boolean(document.querySelector(".assistant-card .assistant-header-slot")) &&
      Boolean(document.querySelector(".assistant-card .assistant-body-slot"));

    // ---- Turn 1: long streaming answer --------------------------------------------------------
    window.fetch = () => new Promise(() => {});
    api.els.promptInput.value = "Please explain the whole architecture in detail. ".repeat(6);
    await Promise.race([api.sendCurrentPrompt(), sleep(300)]);
    await frame();
    out.afterSendMode = api.scrollGuard.mode;
    out.afterSendAnchor = api.scrollGuard.anchorMsgId;
    out.afterSendGap = c.scrollHeight - c.clientHeight - c.scrollTop;

    const MID = 900;
    const cardEl = () => document.querySelector('[data-msg-id="' + MID + '"] .assistant-card');
    const snap = (tag) => {
      const card = cardEl();
      const cr = c.getBoundingClientRect();
      const r = card ? card.getBoundingClientRect() : null;
      const btn = document.getElementById("scrollBottomBtn");
      const s = {
        tag, top: c.scrollTop, sh: c.scrollHeight,
        cardTopInView: r ? r.top - cr.top : null,
        cardTopContent: r ? r.top - cr.top + c.scrollTop : null,
        cardH: r ? r.height : 0,
        mode: api.scrollGuard.mode, locked: api.scrollGuard.lockedAtAnswerStart, auto: api.state.autoScroll,
        btnHidden: btn.classList.contains("hidden"), btnBelow: btn.classList.contains("is-streaming-below"),
        streamingClass: c.classList.contains("is-streaming-turn"),
      };
      out.samples.push(s);
      return s;
    };

    api.applyLiveChatEventToCurrentState({ type: "assistant_start", chat_id: "c1",
      message: { id: MID, chat_id: "c1", role: "assistant", status: "streaming", content: "", thinking: "", has_thinking: false, model: "gemini-4-carbon-high" } });
    await frame();
    snap("start");
    out.anchorAfterStart = api.scrollGuard.anchorMsgId;
    const bodyEl0 = cardEl().querySelector(".markdown-body");
    const headerEl0 = cardEl().querySelector(".assistant-header");

    let thinkText = "";
    for (let i = 1; i <= 14; i++) {
      thinkText += "Reasoning step " + i + ": considering the architecture trade-offs in depth.\n";
      api.applyLiveChatEventToCurrentState({ type: "thinking_update", chat_id: "c1", message_id: MID, seq: i, thinking: thinkText });
      await frame();
      snap("think");
    }
    out.bodyKeptDuringThinking = bodyEl0 === cardEl().querySelector(".markdown-body");
    out.headerKeptDuringThinking = headerEl0 === cardEl().querySelector(".assistant-header");
    out.thinkingOpenBeforeText = Boolean(cardEl().querySelector("details.thinking-box[open]"));
    out.thinkingBoxHeightMax = Math.max(...Array.from(document.querySelectorAll('[data-msg-id="' + MID + '"] .thinking-content')).map((e) => e.getBoundingClientRect().height));

    for (let i = 0; i < 3; i++) {
      api.applyLiveChatEventToCurrentState({ type: "tool_step", chat_id: "c1", message_id: MID, seq: 100 + i,
        step: { step_index: i, name: "read_file", parameters: { Path: "/workspace/app/mod_" + i + ".py" }, state: "DONE", output: "" } });
      await frame();
      snap("tool");
    }
    await sleep(100); await frame(); // A tool batch may still be in the stream render throttle.
    const toolsEl0 = cardEl().querySelector("details.tools-spoiler");
    out.toolsExists = Boolean(toolsEl0);

    const TAG = "\n```js\nconst partial = 1;\n";
    let seq = 200;
    out.toolsReplacedDuringText = 0;
    out.textStartIdx = out.samples.length;
    for (let i = 0; i < 100; i++) {
      const m = api.findMsg(MID);
      if (m.content.endsWith(TAG)) m.content = m.content.slice(0, -TAG.length);
      api.applyLiveChatEventToCurrentState({ type: "text_delta", chat_id: "c1", message_id: MID, seq: seq++,
        delta: "Answer paragraph " + i + ": " + "streamed words flow here ".repeat(4) + "\n\n" });
      if (i % 9 === 4) m.content += TAG;
      await frame();
      snap("text");
      if (cardEl().querySelector("details.tools-spoiler") !== toolsEl0) out.toolsReplacedDuringText += 1;
    }
    out.thinkingOpenAfterText = Boolean(cardEl().querySelector("details.thinking-box[open]"));
    const mid = api.findMsg(MID);
    if (mid.content.endsWith(TAG)) mid.content = mid.content.slice(0, -TAG.length);
    out.topBeforeDone = c.scrollTop;
    out.modeBeforeDone = api.scrollGuard.mode;
    out.lockedBeforeDone = api.scrollGuard.lockedAtAnswerStart;
    api.state.generatingChats.delete("c1");
    api.applyLiveChatEventToCurrentState({ type: "turn_done", chat_id: "c1", chat: Object.assign({}, CHAT),
      message: Object.assign({}, mid, { status: "done", _thinkingActive: false, usage: { total_tokens: 4321 } }) });
    await frame(); await frame();
    snap("done");
    out.topAfterDone = c.scrollTop;
    out.modeAfterDone = api.scrollGuard.mode;
    out.autoAfterDone = api.state.autoScroll;
    out.footerRendered = Boolean(cardEl().querySelector(".assistant-footer-slot .msg-actions"));
    out.cardHeightFinal = cardEl().getBoundingClientRect().height;
    out.btnVisibleAfterDone = !document.getElementById("scrollBottomBtn").classList.contains("hidden");

    // ---- Wheel -> user_free ; button -> follow_tail --------------------------------------------
    c.dispatchEvent(new WheelEvent("wheel", { deltaY: 120, bubbles: true, cancelable: true }));
    out.modeAfterWheelDown = api.scrollGuard.mode;
    c.scrollTop = Math.max(0, c.scrollTop - 200);
    c.dispatchEvent(new WheelEvent("wheel", { deltaY: -120, bubbles: true, cancelable: true }));
    await frame();
    out.modeAfterWheelUp = api.scrollGuard.mode;
    out.autoAfterWheelUp = api.state.autoScroll;
    document.getElementById("scrollBottomBtn").click();
    out.modeAfterBtn = api.scrollGuard.mode;
    out.autoAfterBtn = api.state.autoScroll;
    let stable = 0, lastTop = -1;
    for (let i = 0; i < 60 && stable < 3; i++) {
      await sleep(100);
      stable = c.scrollTop === lastTop ? stable + 1 : 0;
      lastTop = c.scrollTop;
    }
    await frame();
    out.gapAfterBtn = c.scrollHeight - c.clientHeight - c.scrollTop;

    // ---- Turn 2: pinned answer start + wheel while locked --------------------------------------
    api.els.promptInput.value = "Second long question";
    await Promise.race([api.sendCurrentPrompt(), sleep(300)]);
    await frame();
    const MID2 = 901;
    api.applyLiveChatEventToCurrentState({ type: "assistant_start", chat_id: "c1",
      message: { id: MID2, chat_id: "c1", role: "assistant", status: "streaming", content: "", thinking: "", has_thinking: false, model: "gemini-4-carbon-high" } });
    await frame();
    let seq2 = 1000;
    for (let i = 0; i < 40 && !api.scrollGuard.lockedAtAnswerStart; i++) {
      api.applyLiveChatEventToCurrentState({ type: "text_delta", chat_id: "c1", message_id: MID2, seq: seq2++,
        delta: "Locked-turn paragraph " + i + ": " + "words ".repeat(24) + "\n\n" });
      await frame();
    }
    out.turn2Locked = api.scrollGuard.lockedAtAnswerStart;
    out.turn2AutoWhileLocked = api.state.autoScroll;
    const lockTop = c.scrollTop;
    for (let i = 0; i < 6; i++) {
      api.applyLiveChatEventToCurrentState({ type: "text_delta", chat_id: "c1", message_id: MID2, seq: seq2++, delta: "More " + i + " " + "words ".repeat(30) + "\n\n" });
      await frame();
    }
    out.turn2TopStable = c.scrollTop === lockTop;
    out.turn2BtnBelow = document.getElementById("scrollBottomBtn").classList.contains("is-streaming-below");
    out.turn2BtnTitle = document.getElementById("scrollBottomBtn").title;
    c.dispatchEvent(new WheelEvent("wheel", { deltaY: 120, bubbles: true, cancelable: true }));
    out.turn2ModeAfterWheelWhileLocked = api.scrollGuard.mode;
    api.state.generatingChats.delete("c1");
    const m2 = api.findMsg(MID2);
    api.applyLiveChatEventToCurrentState({ type: "turn_done", chat_id: "c1", chat: Object.assign({}, CHAT),
      message: Object.assign({}, m2, { status: "done", _thinkingActive: false }) });
    await frame();

    // ---- Turn 3: short answer is fully visible at turn_done (actions row not clipped) ----------
    api.els.promptInput.value = "Short question";
    await Promise.race([api.sendCurrentPrompt(), sleep(300)]);
    await frame();
    const MID3 = 902;
    api.applyLiveChatEventToCurrentState({ type: "assistant_start", chat_id: "c1",
      message: { id: MID3, chat_id: "c1", role: "assistant", status: "streaming", content: "", thinking: "", has_thinking: false, model: "gemini-4-carbon-high" } });
    await frame();
    api.applyLiveChatEventToCurrentState({ type: "text_delta", chat_id: "c1", message_id: MID3, seq: 2000, delta: "A short answer.\n\nTwo paragraphs only." });
    await frame();
    api.state.generatingChats.delete("c1");
    const m3 = api.findMsg(MID3);
    api.applyLiveChatEventToCurrentState({ type: "turn_done", chat_id: "c1", chat: Object.assign({}, CHAT),
      message: Object.assign({}, m3, { status: "done", _thinkingActive: false, usage: { total_tokens: 99 } }) });
    await frame(); await frame();
    const cr3 = c.getBoundingClientRect();
    const row3 = document.querySelector('[data-msg-id="' + MID3 + '"]');
    const act3 = row3 ? row3.querySelector(".msg-actions") : null;
    out.shortModeAfterDone = api.scrollGuard.mode;
    out.shortActionsVisible = Boolean(act3) && act3.getBoundingClientRect().bottom <= cr3.bottom + 1 && act3.getBoundingClientRect().top >= cr3.top - 1;

    // ---- Opening a completed long answer lands on its beginning --------------------------------
    const longMsg = { id: 950, chat_id: "c1", role: "assistant", status: "done", model: "gemini-4-carbon-high", thinking: "", has_thinking: false, tool_calls: [],
      content: Array.from({ length: 30 }, (_, i) => para(i)).join("\n\n") };
    api.state.messages = hist.concat([longMsg]);
    api.renderMessages(true);
    await frame();
    const handled = api.scrollToLastAnswerStartIfLong();
    await frame();
    const cardLong = document.querySelector('[data-msg-id="950"] .assistant-card');
    const crl = c.getBoundingClientRect();
    out.unreadLongHandled = handled;
    out.unreadLongOffset = cardLong.getBoundingClientRect().top - crl.top;
    out.unreadLongMode = api.scrollGuard.mode;
  } catch (e) {
    out.errors.push("fatal:" + String((e && e.stack) || e));
  }
  C.report(out);
})();
"""

GEOMETRY_SCENARIO_JS = r"""
(async function main() {
  const C = window.__agyCommon;
  const out = { errors: [] };
  window.addEventListener("error", (e) => out.errors.push(String(e.message || e)));
  try {
    const api = window.__AGY_TEST__;
    if (!api) throw new Error("test api missing");
    const frame = C.frame, sleep = C.sleep;
    const q = new URLSearchParams(location.search);
    const $ = (id) => document.getElementById(id);
    document.documentElement.setAttribute("data-theme", q.get("theme") || "dark");
    document.documentElement.setAttribute("data-palette", q.get("palette") || "emerald");
    const sb = Number(q.get("sb") || 0);
    if (sb) document.documentElement.style.setProperty("--sidebar-width", sb + "px");

    const CHAT = { id: "c1", kind: "project", project_name: "Antigravity_Hub_Extremely_Long_Project", title: "T", model: "claude-sonnet-5-5-high", is_generating: true };
    api.state.activeChatId = "c1";
    api.state.activeChat = Object.assign({}, CHAT);
    api.state.ws = { readyState: 1, OPEN: 1, send() {} };

    // ----- header with ALL badges visible at once -----
    const badge = $("chatContextBadge");
    badge.classList.remove("hidden");
    badge.classList.add("is-project-badge");
    badge.innerHTML = '<svg viewBox="0 0 24 24" width="12" height="12"><rect width="20" height="14" x="2" y="5"/></svg><span class="badge-proj-text">Antigravity_Hub_Extremely_Long_Project</span>';
    $("openProjectMemoryBtn").classList.remove("hidden");
    $("chatMetaDot1").classList.remove("hidden");
    $("activeChatTitle").textContent = "Очень длинное название активной вкладки для проверки схлопывания заголовка";
    const pill = $("activeChatStatePill");
    pill.classList.remove("hidden");
    pill.classList.add("is-running");
    pill.innerHTML = '<span class="ocsp-dot"></span><span class="ocsp-full">В работе · +3 в очереди</span><span class="ocsp-short">+3</span>';
    const ob = $("chatMetaOutlineBtn");
    ob.classList.remove("hidden");
    ob.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14"><rect width="18" height="14" x="3" y="5"/></svg><span>12</span><i class="meta-outline-bm-dot"></i>';
    $("chatHeaderActions").classList.remove("hidden");
    $("modelPickerLabel").textContent = "Claude Sonnet 5.5 (High)";
    api.setProjectAccessBarVisible(true);
    api.syncMobileHeaderToolsSlot();
    const mobAccess = $("mobileAccessBar");
    if (mobAccess && api.els.mobileAccessSeg) api.els.mobileAccessSeg.classList.remove("hidden");

    // ----- messages with wide user bubbles + a very long unbroken path -----
    const hist = [];
    for (let i = 0; i < 8; i++) {
      hist.push({ id: 10 + i * 2, chat_id: "c1", role: "user", content: "Question " + i + " " + "very wide user bubble text ".repeat(18), attachments: [], usage: {} });
      hist.push({ id: 11 + i * 2, chat_id: "c1", role: "assistant", status: "done", model: "claude-sonnet-5-5-high", thinking: "", has_thinking: false, tool_calls: [],
        content: "Answer " + i + " " + "words ".repeat(60) + "\n\n`D:\\Internet\\Antigravity\\Antigravity_Hub\\app\\static\\some\\very\\long\\path\\that\\never\\breaks\\app.js`\n\n```js\nconst x = 'a very long line of code ' + 'that should scroll inside pre only ' + 'and not widen the page at all';\n```" });
    }
    api.state.messages = hist;
    api.renderMessages(true);

    // ----- realistic (long) telemetry pill texts -----
    const setTxt = (id, txt) => { const el = $(id); if (el) el.textContent = txt; };
    setTxt("mobQuotaText", "Gem 100% · Cld 100% · 12 480 ток.");
    setTxt("mobChatGptText", "GPT · 5ч 100% · нед 100%");
    setTxt("mobClaudeText", "Claude · 5ч 100% · нед 100%");
    setTxt("mobOrText", "OR · $123.45 · лимит $5/сут");

    // ----- rail ticks -----
    const rail = $("chatOutlineRail");
    rail.classList.remove("hidden");
    rail.innerHTML = Array.from({ length: 12 }, () => '<button type="button" class="outline-rail-tick"><span class="outline-rail-bar"></span></button>').join("");

    // ----- composer: model hint + queue bar + 5-line prompt + floating buttons -----
    const hint = $("composerModelHintBar");
    hint.classList.remove("hidden");
    hint.innerHTML = '<span class="cmh-text">Подсказка советника: для этой задачи лучше подойдёт другая модель — переключиться?</span><button type="button" class="cmh-switch-btn">Переключить модель</button><button type="button" class="cmh-dismiss-btn">×</button>';
    const queue = $("composerQueueBar");
    queue.classList.remove("hidden");
    queue.innerHTML = '<div style="height:84px;display:flex;align-items:center">Очередь задач: 3 в ожидании</div>';
    api.els.promptInput.value = Array.from({ length: 5 }, (_, i) => "Строка запроса номер " + (i + 1)).join("\n");
    api.resizePromptInput();
    $("scrollBottomBtn").classList.remove("hidden");
    const pillMob = $("mobileOutlinePill");
    pillMob.classList.remove("hidden");
    pillMob.classList.add("is-visible");

    api.setupWorkspaceLayoutObservers();
    await frame(); await frame(); await sleep(100); await frame(); await frame();

    const R = C.rect, ov = C.overlap;
    const comp = R($("composerSection"));
    out.vw = window.innerWidth; out.vh = window.innerHeight;
    out.floatingOffsetVar = document.querySelector("main.workspace").style.getPropertyValue("--composer-floating-offset");
    out.composerHeight = comp ? comp.height : 0;
    out.scrollBtnOverlap = ov(R($("scrollBottomBtn")), comp);
    out.outlinePillOverlap = ov(R(pillMob), comp);
    out.scrollBtnBottomGap = comp ? comp.top - R($("scrollBottomBtn")).bottom : null;
    const railRect = R(rail);
    const railVisible = railRect && railRect.width > 0 && getComputedStyle(rail).display !== "none";
    out.railVisible = Boolean(railVisible);
    let bubbleOverlap = 0;
    document.querySelectorAll("#messagesContainer .user-bubble").forEach((b) => { bubbleOverlap += ov(R(b), railRect); });
    out.railBubbleOverlap = railVisible ? bubbleOverlap : 0;
    let codeOverlap = 0;
    document.querySelectorAll("#messagesContainer .msg-action-btn, #messagesContainer .code-block-wrap").forEach((b) => { codeOverlap += ov(R(b), railRect); });
    out.railContentOverlap = railVisible ? codeOverlap : 0;
    const title = R($("activeChatTitle"));
    out.titleWidth = title ? title.width : 0;
    out.titleText = $("activeChatTitle").textContent.length;
    out.wsClasses = document.querySelector("main.workspace").className;
    const hdrIds = ["toggleSidebarBtn", "chatContextBadge", "openProjectMemoryBtn", "activeChatTitle", "activeChatStatePill",
      "topAccessSeg", "modelPickerWrap", "chatMetaOutlineBtn", "chatHeaderActions"];
    const tabRect = R($("workspaceTabbar"));
    out.tabbar = tabRect ? { left: tabRect.left, right: tabRect.right, width: tabRect.width } : null;
    out.header = {};
    hdrIds.forEach((id) => {
      const el = $(id);
      const rr = R(el);
      const cs = el ? getComputedStyle(el) : null;
      out.header[id] = rr ? { left: rr.left, right: rr.right, top: rr.top, bottom: rr.bottom, width: rr.width, display: cs.display } : null;
    });
    const ctx = document.querySelector(".topbar-context");
    out.contextClipped = ctx ? ctx.scrollWidth - ctx.clientWidth : 0;
    const crumb = document.querySelector(".topbar-breadcrumb");
    out.crumbClipped = crumb ? crumb.scrollWidth - crumb.clientWidth : 0;
    const over = (el) => (el ? { sw: el.scrollWidth, cw: el.clientWidth } : null);
    out.overflow = {
      sidebar: over($("sidebar")), telemetry: over($("mobileTelemetryBar")),
      tabbar: over($("workspaceTabbar")), messages: over($("messagesContainer")),
      mobileAccessBar: over($("mobileAccessBar")),
    };
    out.telemetryDisplay = $("mobileTelemetryBar") ? getComputedStyle($("mobileTelemetryBar")).display : null;
    const expandBtn = R($("mobTelemetryExpandBtn"));
    const telemRect = R($("mobileTelemetryBar"));
    out.expandBtnInside = Boolean(expandBtn && telemRect && expandBtn.right <= telemRect.right + 1 && expandBtn.width > 0);
  } catch (e) {
    out.errors.push("fatal:" + String((e && e.stack) || e));
  }
  C.report(out);
})();
"""


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    reports: dict = {}
    report_events: dict = {}

    def log_message(self, *args, **kwargs):  # noqa: D401
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        m = re.search(r"rid=([\w-]+)", self.path)
        if m and self.path.startswith("/__report"):
            rid = m.group(1)
            type(self).reports[rid] = body
            ev = type(self).report_events.get(rid)
            if ev:
                ev.set()
        self.send_response(204)
        self.end_headers()


class _ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class BrowserSite:
    """Temp copy of app/static + fixture shell served by a local 127.0.0.1 server."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agy_visual_scroll_")
        self.root = Path(self.tmp.name)
        self.profile_dir = self.root / "profile"
        static_copy = self.root / "static"
        shutil.copytree(STATIC_DIR, static_copy)

        # app.js -> exported test copy (init() is NOT run, nothing talks to the live backend)
        src = APP_JS_PATH.read_text(encoding="utf-8")
        tail = re.compile(r"init\(\);\s*\}\)\(\);\s*$")
        if not tail.search(src):
            raise RuntimeError("Could not locate IIFE tail init() in app.js")
        (static_copy / "app_test.js").write_text(tail.sub(EXPORT_TAIL, src), encoding="utf-8")
        (static_copy / "common_test.js").write_text(COMMON_JS, encoding="utf-8")
        (static_copy / "scenario_scroll.js").write_text(SCROLL_SCENARIO_JS, encoding="utf-8")
        (static_copy / "scenario_geometry.js").write_text(GEOMETRY_SCENARIO_JS, encoding="utf-8")

        index = INDEX_HTML_PATH.read_text(encoding="utf-8")
        index = re.sub(r"<script\b[^>]*>.*?</script>", "", index, flags=re.S)
        index = re.sub(r'<link[^>]+href="https://fonts\.(?:googleapis|gstatic)\.com[^"]*"[^>]*>', "", index)
        index = index.replace('data-auth="checking"', 'data-auth="authenticated"')
        index = re.sub(r'(href="/static/styles\.css)\?v=\d+"', r'\1"', index)
        scripts = (
            '<script src="/static/vendor/marked.min.js"></script>'
            '<script src="/static/vendor/highlight.min.js"></script>'
            '<script src="/static/common_test.js"></script>'
            '<script src="/static/app_test.js"></script>'
            "<script>(function(){var s=new URLSearchParams(location.search).get('s');"
            "var el=document.createElement('script');el.src='/static/scenario_'+s+'.js';"
            "document.body.appendChild(el);})();</script>"
        )
        index = index.replace("</body>", scripts + "</body>")
        (self.root / "fixture.html").write_text(index, encoding="utf-8")
        (self.root / "host.html").write_text(HOST_HTML, encoding="utf-8")

        handler = partial(_QuietHandler, directory=str(self.root))
        self.server = _ThreadedServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
        finally:
            try:
                self.tmp.cleanup()
            except Exception:
                pass

    def run(self, browser: str, scenario: str, width: int, height: int, **query) -> dict:
        self._seq = getattr(self, "_seq", 0) + 1
        rid = f"r{os.getpid()}x{self._seq}"
        event = threading.Event()
        _QuietHandler.report_events[rid] = event
        qs = "&".join(
            f"{k}={v}" for k, v in {"s": scenario, "w": width, "h": height, "rid": rid, **query}.items()
        )
        url = f"http://127.0.0.1:{self.port}/host.html?{qs}"
        profile = tempfile.mkdtemp(prefix="p_", dir=str(self.root))
        args = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            f"--user-data-dir={profile}",
            f"--window-size={width + 60},{height + 160}",
            url,
        ]
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **subprocess_hidden_kwargs(),
        )
        try:
            got = event.wait(timeout=150)
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=15)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if not got:
            raise AssertionError(f"Browser produced no result for {scenario}@{width}x{height} within 150s")
        return json.loads(_QuietHandler.reports.pop(rid))


_SITE: BrowserSite | None = None
_BROWSER: str | None = None


def setUpModule() -> None:  # noqa: N802
    global _SITE, _BROWSER
    _BROWSER = find_browser()
    if _BROWSER:
        _SITE = BrowserSite()


def tearDownModule() -> None:  # noqa: N802
    global _SITE
    if _SITE:
        _SITE.close()
        _SITE = None


# ---------------------------------------------------------------------------------------------
# CSS / colour maths
# ---------------------------------------------------------------------------------------------
def _read_css_without_comments() -> str:
    return re.sub(r"/\*.*?\*/", "", STYLES_CSS_PATH.read_text(encoding="utf-8"), flags=re.S)


def _theme_rules() -> list[tuple[str, int, dict[str, str]]]:
    css = _read_css_without_comments()
    head = css[: css.index("@media")] if "@media" in css else css
    rules: list[tuple[str, int, dict[str, str]]] = []
    for idx, m in enumerate(re.finditer(r"([^{}]+)\{([^{}]*)\}", head)):
        sel = m.group(1).strip()
        if not (sel.startswith(":root") or sel.startswith("html[")):
            continue
        decls = {k: v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(2))}
        if not decls:
            continue
        for part in (s.strip() for s in sel.split(",")):
            rules.append((part, idx, decls))
    return rules


def _specificity(part: str) -> int:
    return 10 if part == ":root" else 10 * part.count("[") + 1


def theme_vars(palette: str, mode: str) -> dict[str, str]:
    applicable = []
    for part, idx, decls in _theme_rules():
        if part in (":root", f'html[data-theme="{mode}"]', f'html[data-palette="{palette}"][data-theme="{mode}"]'):
            applicable.append((_specificity(part), idx, decls))
    applicable.sort(key=lambda t: (t[0], t[1]))
    out: dict[str, str] = {}
    for _, _, d in applicable:
        out.update(d)
    return out


def resolve_var(value: str, vars_: dict[str, str], depth: int = 0) -> str:
    if depth > 8:
        return value

    def rep(m: re.Match) -> str:
        name, fallback = m.group(1), m.group(2)
        if name in vars_:
            return resolve_var(vars_[name], vars_, depth + 1)
        return fallback.strip() if fallback else ""

    return re.sub(r"var\((--[\w-]+)\s*(?:,\s*([^)]*))?\)", rep, value)


def parse_color(s: str) -> tuple[float, float, float]:
    s = s.strip()
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    m = re.match(r"rgba?\(([^)]*)\)", s)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        return tuple(float(p) / 255 for p in parts[:3])  # type: ignore[return-value]
    raise ValueError(f"unsupported colour: {s!r}")


def rel_luminance(c: tuple[float, float, float]) -> float:
    def f(x: float) -> float:
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    r, g, b = (f(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def wcag_contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = rel_luminance(a), rel_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: tuple[float, float, float], b: tuple[float, float, float], pa: float) -> tuple[float, float, float]:
    """CSS color-mix(in srgb, a pa%, b)."""
    return tuple(a[i] * pa + b[i] * (1 - pa) for i in range(3))  # type: ignore[return-value]


def rect_overlap_area(a: dict | None, b: dict | None) -> float:
    if not a or not b:
        return 0.0
    w = min(a["right"], b["right"]) - max(a["left"], b["left"])
    h = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
    return w * h if w > 0.5 and h > 0.5 else 0.0


def _project_accents() -> list[str]:
    app_js = APP_JS_PATH.read_text(encoding="utf-8")
    m = re.search(r"PROJECT_ACCENT_PALETTE\s*=\s*\[(.*?)\]", app_js, re.S)
    assert m, "PROJECT_ACCENT_PALETTE not found"
    return re.findall(r"#[0-9a-fA-F]{6}", m.group(1))


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
@unittest.skipUnless(find_browser(), "headless Edge/Chrome is not available")
class TestStreamingScrollInRealBrowser(unittest.TestCase):
    """Test №1 — 60fps streaming simulation, slots and answer-start pinning (real DOM + layout)."""

    @classmethod
    def setUpClass(cls) -> None:
        assert _SITE is not None and _BROWSER is not None
        cls.res = _SITE.run(_BROWSER, "scroll", 1100, 1000)

    def test_no_script_errors(self) -> None:
        self.assertEqual(self.res["errors"], [], self.res["errors"])

    def test_slots_exist_and_stay_stable(self) -> None:
        r = self.res
        self.assertTrue(r["slotsInHistory"], "history cards must use the 5-slot structure")
        self.assertTrue(r["toolsExists"])
        self.assertEqual(
            r["toolsReplacedDuringText"],
            0,
            "details.tools-spoiler must be the very same DOM node across all 100 text_delta updates",
        )
        self.assertTrue(r["bodyKeptDuringThinking"], "Markdown body must NOT be re-rendered on thinking_update")
        self.assertTrue(r["headerKeptDuringThinking"], "header slot must not be rebuilt on thinking_update")
        self.assertTrue(r["thinkingOpenBeforeText"])
        self.assertFalse(r["thinkingOpenAfterText"], "auto-opened thinking box collapses with the first text_delta")
        self.assertTrue(r["footerRendered"], "footer slot (msg-actions) is rendered at turn_done")

    def test_prompt_send_enters_follow_answer_start(self) -> None:
        r = self.res
        self.assertEqual(r["initialMode"], "follow_tail")
        self.assertLessEqual(abs(r["initialGap"]), 2)
        self.assertEqual(r["afterSendMode"], "follow_answer_start")
        self.assertEqual(r["anchorAfterStart"], "900")
        self.assertLessEqual(r["afterSendGap"], 24, "user bubble must stay visible at the bottom after sending")

    def test_zero_backward_jumps_and_answer_start_pinned(self) -> None:
        r = self.res
        samples = r["samples"]
        text = [s for s in samples[r["textStartIdx"] :] if s["tag"] == "text"]
        self.assertEqual(len(text), 100)

        jitter = 0.0
        for prev, cur in zip(text, text[1:]):
            if cur["top"] < prev["top"]:
                jitter += prev["top"] - cur["top"]
        self.assertEqual(jitter, 0.0, f"jitter_px must be 0 while the answer grows, got {jitter}")

        # The card never flies above the 18px reading offset (incl. the thinking collapse frame)
        offset = 18
        for s in samples:
            if s["cardTopInView"] is None:
                continue
            self.assertGreaterEqual(
                s["cardTopInView"],
                offset - 1.5,
                f"card top {s['cardTopInView']} above reading offset at {s['tag']}",
            )

        # Once the answer outgrew the viewport the beginning is pinned (NOT dragged to the end)
        locked = [s for s in text if s["locked"]]
        self.assertTrue(locked, "answer start must get pinned after the card outgrew the viewport")
        final = text[-1]
        self.assertGreater(final["cardH"], 700)
        self.assertLessEqual(
            abs(final["top"] - (final["cardTopContent"] - offset)),
            1.5,
            "scrollTop must be exactly answerTop - 18px",
        )
        self.assertLess(final["top"], final["sh"] - 700 - 500, "must not be dragged to the bottom of a long answer")
        first_locked_top = locked[0]["top"]
        for s in locked:
            self.assertEqual(s["top"], first_locked_top, "ZERO px auto-scroll after the lock")
        self.assertFalse(final["auto"], "autoScroll is off while pinned")
        self.assertTrue(final["streamingClass"], "overflow-anchor isolation class is set while streaming")
        self.assertTrue(final["btnBelow"], "pulsing 'generation below' dot on #scrollBottomBtn")

    def test_turn_done_keeps_reading_position(self) -> None:
        r = self.res
        self.assertEqual(r["topAfterDone"], r["topBeforeDone"], "turn_done must NOT scroll to the bottom")
        self.assertEqual(r["modeAfterDone"], "user_free")
        self.assertTrue(r["lockedBeforeDone"])
        self.assertTrue(r["btnVisibleAfterDone"])

    def test_wheel_and_scroll_bottom_button(self) -> None:
        r = self.res
        self.assertEqual(r["modeAfterWheelUp"], "user_free")
        self.assertFalse(r["autoAfterWheelUp"])
        self.assertEqual(r["modeAfterBtn"], "follow_tail")
        self.assertTrue(r["autoAfterBtn"])
        self.assertLessEqual(r["gapAfterBtn"], 2, "click on #scrollBottomBtn scrolls to the very end")

    def test_pinned_turn_wheel_breaks_lock_and_short_answer_fits(self) -> None:
        r = self.res
        self.assertTrue(r["turn2Locked"])
        self.assertFalse(r["turn2AutoWhileLocked"])
        self.assertTrue(r["turn2TopStable"])
        self.assertTrue(r["turn2BtnBelow"])
        self.assertIn("Идёт генерация ниже", r["turn2BtnTitle"])
        self.assertEqual(r["turn2ModeAfterWheelWhileLocked"], "user_free", "wheel DOWN while pinned releases the lock")
        self.assertEqual(r["shortModeAfterDone"], "follow_tail")
        self.assertTrue(r["shortActionsVisible"], "msg-actions of a short answer must not be clipped")

    def test_unread_long_answer_opens_at_its_beginning(self) -> None:
        r = self.res
        self.assertTrue(r["unreadLongHandled"])
        self.assertEqual(r["unreadLongMode"], "user_free")
        self.assertLessEqual(abs(r["unreadLongOffset"] - 18), 1.5)


class TestActiveTabContrast(unittest.TestCase):
    """Test №2 — WCAG 2.1 maths for the expressive `.nav-item.active` in all 16 palettes."""

    def test_sixteen_themes_resolve(self) -> None:
        for palette in PALETTES:
            for mode in MODES:
                v = theme_vars(palette, mode)
                for key in ("--bg-surface-active", "--accent", "--bg-sidebar", "--border-strong", "--text-primary"):
                    self.assertIn(key, v, f"{palette}/{mode} lacks {key}")

    def test_active_row_contrast_distinct_and_readable(self) -> None:
        accents = _project_accents()
        self.assertEqual(len(accents), 10)
        for palette in PALETTES:
            for mode in MODES:
                v = theme_vars(palette, mode)
                col = lambda k: parse_color(resolve_var(v[k], v))  # noqa: E731
                heading = col("--text-heading") if "--text-heading" in v else col("--text-primary")
                theme_accent = col("--accent")
                sidebar = col("--bg-sidebar")
                active = col("--bg-surface-active")
                border_strong = col("--border-strong")

                worst = 99.0
                for acc in [theme_accent] + [parse_color(a) for a in accents]:
                    bg = mix(acc, active, 0.14)
                    worst = min(worst, wcag_contrast(heading, bg))
                self.assertGreaterEqual(worst, 4.5, f"{palette}/{mode}: active title contrast {worst:.2f} < 4.5")

                bg = mix(theme_accent, active, 0.14)
                marker = theme_accent if mode == "dark" else mix(theme_accent, (17 / 255,) * 3, 0.84)
                border = mix(theme_accent, border_strong, 0.42 if mode == "dark" else 0.46)
                expanded = mix(col("--text-primary"), sidebar, 0.022)

                self.assertGreaterEqual(wcag_contrast(marker, bg), 3.0, f"{palette}/{mode}: marker vs row")
                self.assertGreaterEqual(wcag_contrast(marker, sidebar), 3.0, f"{palette}/{mode}: marker vs sidebar")
                self.assertGreaterEqual(wcag_contrast(border, sidebar), 1.8, f"{palette}/{mode}: border vs sidebar")
                dist = sum((bg[i] - expanded[i]) ** 2 for i in range(3)) ** 0.5 * 255
                self.assertGreaterEqual(dist, 25.0, f"{palette}/{mode}: row vs .proj-tree-group.expanded ({dist:.1f})")
                self.assertGreater(wcag_contrast(bg, sidebar), 1.25, f"{palette}/{mode}: row vs sidebar")

    def test_css_wires_the_semantic_tokens(self) -> None:
        css = _read_css_without_comments()
        self.assertRegex(
            css,
            r"\.nav-item\.active,\s*\.nav-item\.is-generating\.active[^{]*\{[^}]*color-mix\(in srgb, var\(--proj-accent, var\(--accent\)\) 14%, var\(--bg-surface-active\)\)",
        )
        self.assertRegex(css, r"\.nav-item\.active::before\s*\{[^}]*width:\s*3\.5px")
        self.assertRegex(css, r"\.nav-item\.active \.nav-item-title\s*\{[^}]*font-weight:\s*600")
        self.assertIn(".proj-tree-group.has-active-chat.expanded > .proj-tree-header", css)
        self.assertIn(".proj-tree-group.has-active-chat.has-active-chat:not(.expanded) > .proj-tree-header", css)
        self.assertIn(".folder-group.has-active-chat > .folder-header-btn", css)

    def test_continue_bar_has_no_hardcoded_palette_colours(self) -> None:
        css = _read_css_without_comments()
        blocks = re.findall(r"([^{}]*\.msg-continue-(?:bar|btn)[^{}]*)\{([^{}]*)\}", css)
        self.assertGreaterEqual(len(blocks), 6)
        for sel, body in blocks:
            low = body.lower()
            self.assertNotIn("#171715", low, f"hard-coded #171715 in {sel.strip()}")
            self.assertNotIn("#8ec0a4", low, f"hard-coded #8ec0a4 in {sel.strip()}")
            self.assertNotIn("142, 192, 164", low, f"hard-coded sage rgba in {sel.strip()}")

    def test_js_marks_active_rows_and_parent_folders(self) -> None:
        js = APP_JS_PATH.read_text(encoding="utf-8")
        self.assertGreaterEqual(js.count('setAttribute("aria-current", "page")'), 4)
        self.assertIn("has-active-chat", js)
        self.assertIn("getActiveProjectLowForHighlight", js)
        self.assertIn("getActiveFolderNameForHighlight", js)


@unittest.skipUnless(find_browser(), "headless Edge/Chrome is not available")
class TestGeometryNoOverlaps(unittest.TestCase):
    """Test №3 — geometry audit at 360x800, 768x1024, 1024x768, 1440x900 (420px sidebar), 1920x1080."""

    BREAKPOINTS = [
        ("mobile", 360, 800, 0),
        ("tablet", 768, 1024, 0),
        ("laptop", 1024, 768, 0),
        ("wide_sidebar", 1440, 900, 420),
        ("fullhd", 1920, 1080, 0),
    ]
    results: dict[str, dict] = {}

    @classmethod
    def setUpClass(cls) -> None:
        assert _SITE is not None and _BROWSER is not None
        for name, w, h, sb in cls.BREAKPOINTS:
            extra = {"sb": sb} if sb else {}
            cls.results[name] = _SITE.run(_BROWSER, "geometry", w, h, **extra)

    def test_no_script_errors(self) -> None:
        for name, r in self.results.items():
            self.assertEqual(r["errors"], [], f"{name}: {r['errors']}")

    def test_floating_buttons_never_overlap_composer(self) -> None:
        for name, r in self.results.items():
            self.assertEqual(r["scrollBtnOverlap"], 0, f"{name}: #scrollBottomBtn overlaps #composerSection")
            self.assertEqual(r["outlinePillOverlap"], 0, f"{name}: #mobileOutlinePill overlaps #composerSection")
            self.assertTrue(r["floatingOffsetVar"].endswith("px"), f"{name}: --composer-floating-offset missing")
            self.assertGreater(r["composerHeight"], 150, f"{name}: hint+queue+5 lines must enlarge the composer")
            self.assertGreaterEqual(r["scrollBtnBottomGap"], 4, f"{name}: ↓ must ride above the composer")

    def test_outline_rail_does_not_touch_messages(self) -> None:
        for name in ("laptop", "wide_sidebar"):
            r = self.results[name]
            self.assertTrue(r["railVisible"], f"{name}: #chatOutlineRail must be visible on desktop")
            self.assertEqual(r["railBubbleOverlap"], 0, f"{name}: rail overlaps .user-bubble")
            self.assertEqual(r["railContentOverlap"], 0, f"{name}: rail overlaps message actions / code blocks")

    def test_active_title_never_collapses(self) -> None:
        for name, r in self.results.items():
            self.assertGreaterEqual(
                r["titleWidth"], 64, f"{name}: #activeChatTitle collapsed to {r['titleWidth']}px (ws={r['wsClasses']})"
            )

    def test_header_blocks_neither_overlap_nor_get_clipped_on_desktop(self) -> None:
        ids = list(self.results["laptop"]["header"].keys())
        for name in ("tablet", "laptop", "wide_sidebar", "fullhd"):
            r = self.results[name]
            self.assertEqual(r["contextClipped"], 0, f"{name}: left header zone is clipped")
            self.assertEqual(r["crumbClipped"], 0, f"{name}: breadcrumb content is clipped")
            tab = r["tabbar"]
            rects = {k: v for k, v in r["header"].items() if v and v["width"] > 0 and v["display"] != "none"}
            for k, v in rects.items():
                self.assertGreaterEqual(v["left"], tab["left"] - 0.5, f"{name}: {k} leaves the tabbar on the left")
                self.assertLessEqual(v["right"], tab["right"] + 0.5, f"{name}: {k} leaves the tabbar on the right")
            for i, a in enumerate(ids):
                for b in ids[i + 1 :]:
                    if a in rects and b in rects:
                        self.assertEqual(
                            rect_overlap_area(
                                rects[a],
                                rects[b],
                            ),
                            0.0,
                            f"{name}: header blocks {a} and {b} intersect",
                        )

    def test_workspace_width_classes(self) -> None:
        self.assertIn("is-ws-compact", self.results["laptop"]["wsClasses"])
        self.assertIn("is-ws-medium", self.results["laptop"]["wsClasses"])
        self.assertIn("is-ws-medium", self.results["wide_sidebar"]["wsClasses"])
        self.assertNotIn("is-ws-compact", self.results["wide_sidebar"]["wsClasses"])
        self.assertNotIn("is-ws-medium", self.results["fullhd"]["wsClasses"])
        # mobile widths are handled by the mobile layout, not by the desktop width classes
        self.assertNotIn("is-ws-medium", self.results["mobile"]["wsClasses"])

    def test_no_horizontal_overflow_on_smartphone(self) -> None:
        o = self.results["mobile"]["overflow"]
        for key in ("sidebar", "telemetry", "tabbar", "messages"):
            self.assertIsNotNone(o[key], f"{key} missing in fixture")
            self.assertLessEqual(o[key]["sw"], o[key]["cw"] + 1, f"{key}: scrollWidth {o[key]['sw']} > clientWidth {o[key]['cw']}")
        self.assertEqual(self.results["mobile"]["telemetryDisplay"], "grid")
        self.assertTrue(self.results["mobile"]["expandBtnInside"], "▴ button must stay inside the telemetry row")


class TestCssJsStaticGuards(unittest.TestCase):
    """Static guards for the exact numbers of the ТЗ (cheap, no browser)."""

    def test_floating_buttons_use_composer_offset_variable(self) -> None:
        css = _read_css_without_comments()
        self.assertIn("bottom: calc(var(--composer-floating-offset, 108px) + 14px)", css)
        self.assertIn("bottom: calc(var(--composer-floating-offset, 108px) + 56px)", css)
        self.assertRegex(css, r"\.scroll-bottom-btn\.is-streaming-below::after\s*\{[^}]*width:\s*6px")

    def test_streaming_isolation_and_slots_css(self) -> None:
        css = _read_css_without_comments()
        self.assertRegex(css, r"\.messages-container\.is-streaming-turn\s*\{\s*overflow-anchor:\s*none;?\s*\}")
        for slot in ("header", "thinking", "tools", "body", "footer"):
            self.assertIn(f".assistant-{slot}-slot", css)
        self.assertRegex(css, r"\.assistant-card,\s*\.assistant-body-slot,\s*\.assistant-card \.markdown-body\s*\{[^}]*min-width:\s*0;[^}]*max-width:\s*100%")

    def test_no_forbidden_workspace_props(self) -> None:
        css = _read_css_without_comments()
        for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css):
            sel, body = m.group(1).strip(), m.group(2)
            if re.fullmatch(r"(main\.workspace|\.workspace|main\.workspace\.island)", sel):
                for prop in ("container-type", "transform", "filter"):
                    self.assertNotRegex(body, rf"(^|[;\s]){prop}\s*:", f"{prop} is forbidden on {sel}")

    def test_hover_only_more_button_and_outline_rail_rules(self) -> None:
        css = _read_css_without_comments()
        self.assertIn("#projectsTreeList .nav-item:not(:hover):not(:focus-within):not(.menu-open) .nav-item-more-btn", css)
        self.assertIn("#treeRegularChatsSection .nav-item:not(:hover):not(:focus-within):not(.menu-open) .nav-item-more-btn", css)
        self.assertIn("padding-right: 42px", css)
        self.assertIn("padding-right: 336px", css)
        self.assertRegex(css, r"\.chat-outline-rail\s*\{\s*right:\s*14px")
        self.assertRegex(css, r"\.nav-item-main\s*\{\s*min-width:\s*52px")
        self.assertIn("max-height: calc(100dvh - 16px)", css)

    def test_js_controller_contract(self) -> None:
        js = APP_JS_PATH.read_text(encoding="utf-8")
        for token in (
            "const ANSWER_READ_TOP_OFFSET = 18;",
            'mode: "follow_tail"',
            "lockedAtAnswerStart",
            "maxAutoScrollTopInTurn",
            "anchorMsgId",
            "function followTurnReadingPosition()",
            "function finishTurnScroll()",
            "const ASSISTANT_SLOT_ORDER",
            "assistant-footer-slot",
            "--composer-floating-offset",
            "is-ws-medium",
            "is-ws-compact",
            "is-streaming-turn",
            "is-streaming-below",
            "Идёт генерация ниже — перейти в конец",
        ):
            self.assertIn(token, js, f"app.js lacks {token!r}")
        m = re.search(r"function finishTurnScroll\(\) \{.*?\n  \}\n", js, re.S)
        self.assertTrue(m)
        self.assertNotIn("scrollToBottom(", m.group(0), "turn_done must never call an unconditional scrollToBottom")


class TestSyntaxAndRegression(unittest.TestCase):
    """Test №4 — node --check, CSS braces, cache version sync, live DB untouched."""

    def test_node_check(self) -> None:
        for js_file in (APP_JS_PATH, SW_JS_PATH):
            proc = subprocess.run(
                ["node", "--check", str(js_file)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                **subprocess_hidden_kwargs(),
            )
            self.assertEqual(proc.returncode, 0, f"Syntax error in {js_file}: {proc.stderr}")

    def test_css_braces_balanced(self) -> None:
        css = _read_css_without_comments()
        css = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', css)
        depth = 0
        for ch in css:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            self.assertGreaterEqual(depth, 0, "closing brace without opening one")
        self.assertEqual(depth, 0, "unbalanced braces in styles.css")

    def test_cache_version_sync(self) -> None:
        sw_text = SW_JS_PATH.read_text(encoding="utf-8")
        html_text = INDEX_HTML_PATH.read_text(encoding="utf-8")
        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_text)
        self.assertIsNotNone(m_sw)
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 88)
        self.assertIn(f'"/static/styles.css?v={ver}"', sw_text)
        self.assertIn(f'"/static/app.js?v={ver}"', sw_text)
        self.assertIn(f'href="/static/styles.css?v={ver}"', html_text)
        self.assertIn(f'src="/static/app.js?v={ver}"', html_text)

    def test_live_database_is_untouched(self) -> None:
        if not LIVE_DB_PATH.exists():
            self.skipTest("no live DB")
        before = (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
        # a harmless read of the sibling frontend tests' inputs only — nothing here opens the DB
        _ = APP_JS_PATH.read_text(encoding="utf-8")
        after = (LIVE_DB_PATH.stat().st_mtime_ns, LIVE_DB_PATH.stat().st_size)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
