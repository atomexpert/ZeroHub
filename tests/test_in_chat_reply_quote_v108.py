"""
Automated isolated verification suite for Release v108 (In-Chat Message & Text Selection Reply / Quote):
1. Live `data/hub.db` byte-level immutability guard (`st_mtime_ns`, `st_size`).
2. Static `v=108` cache sync (`index.html`, `sw.js`, `styles.css`, `app.js`), UTF-8 without BOM,
   and DOM/CSS presence of `#pendingReplyPreview`, `#quoteSelectionQuickBtn`, and `#msgSelectionToolbar`.
3. `node --check` syntax verification on `app/static/app.js` and `app/static/sw.js`.
4. Node VM unit tests for `<REPLY_QUOTE>` builder/parser, head+tail context trimming, nested reply
   stripping, forward conversion, `COPY_STRIP_SELECTOR`, and smart outline titles (`extractOutlineTurnTitle`).
5. Read-only compatibility check with `database.extract_outline_title_source_text` (zero edits to backend).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("ORT_DISABLE_ALL_GPU", "1")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

LIVE_HUB_DB = ROOT_DIR / "data" / "hub.db"
_INITIAL_LIVE_DB_SIG: tuple[int, int] | None = None
if LIVE_HUB_DB.exists():
    _st0 = LIVE_HUB_DB.stat()
    _INITIAL_LIVE_DB_SIG = (_st0.st_mtime_ns, _st0.st_size)


class ReleaseV108InChatReplyQuoteTests(unittest.TestCase):
    """Isolated verification of in-chat message reply and highlighted text quote (v=108)."""

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        if _INITIAL_LIVE_DB_SIG is not None:
            assert LIVE_HUB_DB.exists(), "Live data/hub.db disappeared!"
            st_now = LIVE_HUB_DB.stat()
            assert (st_now.st_mtime_ns, st_now.st_size) == _INITIAL_LIVE_DB_SIG, (
                f"Live data/hub.db was modified! before={_INITIAL_LIVE_DB_SIG}, "
                f"after={(st_now.st_mtime_ns, st_now.st_size)}"
            )

    def _assert_live_db_untouched(self) -> None:
        if _INITIAL_LIVE_DB_SIG is not None:
            self.assertTrue(LIVE_HUB_DB.exists())
            st = LIVE_HUB_DB.stat()
            self.assertEqual(
                (st.st_mtime_ns, st.st_size),
                _INITIAL_LIVE_DB_SIG,
                "Live data/hub.db must remain untouched!",
            )

    def test_static_v108_assets_dom_and_utf8_no_bom(self) -> None:
        index_path = ROOT_DIR / "app" / "static" / "index.html"
        sw_path = ROOT_DIR / "app" / "static" / "sw.js"
        css_path = ROOT_DIR / "app" / "static" / "styles.css"
        js_path = ROOT_DIR / "app" / "static" / "app.js"

        for p in (index_path, sw_path, css_path, js_path):
            raw = p.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{p.name} must not have UTF-8 BOM")

        index_html = index_path.read_text(encoding="utf-8")
        sw_js = sw_path.read_text(encoding="utf-8")
        styles_css = css_path.read_text(encoding="utf-8")

        m_sw = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_js)
        self.assertIsNotNone(m_sw, "sw.js CACHE_NAME must match agy-hub-v<N>")
        ver = int(m_sw.group(1))
        self.assertGreaterEqual(ver, 108)

        self.assertIn(f'/static/styles.css?v={ver}"', index_html)
        self.assertIn(f'/static/app.js?v={ver}"', index_html)
        self.assertIn('id="pendingReplyPreview"', index_html)
        self.assertIn('id="quoteSelectionQuickBtn"', index_html)
        self.assertIn('id="msgSelectionToolbar"', index_html)
        self.assertIn('id="selToolbarReplyBtn"', index_html)
        self.assertIn('id="selToolbarCopyBtn"', index_html)

        self.assertIn(f'"/static/styles.css?v={ver}"', sw_js)
        self.assertIn(f'"/static/app.js?v={ver}"', sw_js)

        for selector in (
            ".header-reply-btn",
            ".msg-reply-btn",
            ".ctx-quote-sel-btn",
            ".msg-selection-toolbar",
            ".pending-reply-preview",
            ".user-bubble.has-reply-quote",
            ".user-reply-quote",
        ):
            self.assertIn(selector, styles_css, f"Missing CSS rule for {selector}")

        self._assert_live_db_untouched()

    def test_js_syntax_and_node_vm_reply_quote_suite(self) -> None:
        app_js = ROOT_DIR / "app" / "static" / "app.js"
        sw_js = ROOT_DIR / "app" / "static" / "sw.js"

        for target in (app_js, sw_js):
            chk = subprocess.run(
                ["node", "--check", str(target)],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(chk.returncode, 0, f"node --check failed for {target}: {chk.stderr}")

        node_script = r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const code = fs.readFileSync(process.env.APP_JS_PATH, "utf8");
const noop = () => {};
const makeClassList = () => {
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
};
const makeNode = (tag = "div") => ({
  tagName: String(tag).toUpperCase(),
  id: "",
  className: "",
  textContent: "",
  innerHTML: "",
  value: "",
  disabled: false,
  dataset: {},
  style: { setProperty: noop, removeProperty: noop },
  classList: makeClassList(),
  children: [],
  appendChild: noop,
  insertBefore: noop,
  remove: noop,
  setAttribute: noop,
  getAttribute: () => null,
  removeAttribute: noop,
  addEventListener: noop,
  removeEventListener: noop,
  focus: noop,
  blur: noop,
  closest: () => null,
  contains: () => false,
  getBoundingClientRect: () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }),
  querySelector: () => null,
  querySelectorAll: () => [],
});

const storageMap = new Map();
const fakeStorage = {
  getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
  setItem: (k, v) => storageMap.set(k, String(v)),
  removeItem: (k) => storageMap.delete(k),
};

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: clearTimeout,
  Intl,
  URL,
  URLSearchParams,
  encodeURIComponent,
  decodeURIComponent,
  Math,
  Date,
  JSON,
  Map,
  Set,
  Promise,
  RegExp,
  Array,
  Object,
  String,
  Number,
  Boolean,
  Error,
  Infinity,
  NaN,
  parseInt,
  parseFloat,
  isNaN,
  isFinite,
  performance: { now: () => Date.now() },
  CSS: { escape: (s) => String(s) },
  localStorage: fakeStorage,
  sessionStorage: fakeStorage,
  navigator: { userAgent: "NodeTest", clipboard: { writeText: async () => {} } },
  location: {
    protocol: "http:",
    host: "127.0.0.1:8765",
    hostname: "127.0.0.1",
    port: "8765",
    pathname: "/",
    search: "",
    hash: "",
    origin: "http://127.0.0.1:8765",
    href: "http://127.0.0.1:8765/",
  },
  matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop }),
  getSelection: () => ({ isCollapsed: true, rangeCount: 0, toString: () => "", removeAllRanges: noop }),
  document: {
    getElementById: () => makeNode(),
    querySelector: () => makeNode(),
    querySelectorAll: () => [],
    createElement: (tag) => makeNode(tag),
    createDocumentFragment: () => ({ appendChild: noop }),
    addEventListener: noop,
    documentElement: { setAttribute: noop, getAttribute: () => "", style: { setProperty: noop } },
    body: makeNode("body"),
    hidden: false,
  },
  __AGY_EXPORT_ADVISOR_FOR_TESTS__: true,
  __AGY_SKIP_AUTO_INIT__: true,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "app.js" });

const api = sandbox.__AGY_ADVISOR_API__;
assert.ok(api, "__AGY_ADVISOR_API__ must be exported");

// 1. COPY_STRIP_SELECTOR excludes quote cards and selection toolbar from clean copy
assert.ok(api.COPY_STRIP_SELECTOR.includes(".user-reply-quote"));
assert.ok(api.COPY_STRIP_SELECTOR.includes("#msgSelectionToolbar"));

// 2. Selection reply XML roundtrip + closing tag escaping + attribute sanitization
const selBlock = api.buildReplyQuoteXmlBlock({
  mode: "selection",
  msgId: "402",
  turnNumber: 4,
  role: "assistant",
  authorLabel: 'Claude "Opus"',
  model: "Claude Opus 4.6",
  text: "Кэш check_docker_status() живёт ровно 60 секунд.\nПроверим </REPLY_QUOTE> внутри текста.",
});
assert.ok(selBlock.startsWith('<REPLY_QUOTE mode="selection" msg_id="402" turn="4" role="assistant"'));
assert.ok(!selBlock.slice(0, -14).includes("</REPLY_QUOTE>"), "Inner closing tag must be escaped");
assert.ok(selBlock.includes("[Контекст ответа: пользователь отвечает на выделенный фрагмент"));

const sentSelectionPrompt = `Почему выбран именно такой интервал?\n\n${selBlock}`;
const parsedSel = api.parseReplyQuoteFromContent(sentSelectionPrompt);
assert.ok(parsedSel, "parseReplyQuoteFromContent must parse selection block");
assert.strictEqual(parsedSel.mode, "selection");
assert.strictEqual(parsedSel.msgId, "402");
assert.strictEqual(parsedSel.turnNumber, 4);
assert.strictEqual(parsedSel.role, "assistant");
assert.strictEqual(parsedSel.authorLabel, "Claude Opus");
assert.strictEqual(parsedSel.model, "Claude Opus 4.6");
assert.strictEqual(parsedSel.userText, "Почему выбран именно такой интервал?");
assert.ok(parsedSel.quoteText.includes("Кэш check_docker_status() живёт ровно 60 секунд."));
assert.ok(parsedSel.quoteText.includes("[/REPLY_QUOTE]"));
assert.ok(!parsedSel.quoteText.includes("[Контекст ответа:"), "Header line must be stripped from UI quoteText");

// 3. Full-message reply head+tail trimming (>1800 chars preserves both head and conclusion)
const headPart = "НАЧАЛО_ОТВЕТА: Архитектурное решение по песочнице.\n" + "А".repeat(1200);
const tailPart = "Б".repeat(1200) + "\nВЫВОД_ОТВЕТА: Итоговый чек-лист из 3 пунктов.";
const msgBlock = api.buildReplyQuoteXmlBlock({
  mode: "message",
  msgId: "510",
  turnNumber: 7,
  role: "assistant",
  authorLabel: "Gemini 3.1 Pro",
  model: "Gemini 3.1 Pro",
  text: `${headPart}\n${tailPart}`,
});
const parsedMsg = api.parseReplyQuoteFromMessage({
  role: "user",
  content: `Давай реализуем пункт 2 из вывода\n\n${msgBlock}`,
});
assert.ok(parsedMsg);
assert.strictEqual(parsedMsg.mode, "message");
assert.strictEqual(parsedMsg.turnNumber, 7);
assert.ok(parsedMsg.quoteText.includes("НАЧАЛО_ОТВЕТА"));
assert.ok(parsedMsg.quoteText.includes("ВЫВОД_ОТВЕТА"));
assert.ok(parsedMsg.quoteText.includes("цитата сокращена в карточке"));
assert.ok(parsedMsg.quoteText.includes("полный текст сообщения подгружается сервером при необходимости"));
assert.ok(!parsedMsg.quoteText.includes("полный текст находится выше"));

// 4. Nested reply & forward hygiene: zero raw <REPLY_QUOTE> leakage
const cleanNestedReply = api.extractCleanMessageTextForReply({
  role: "user",
  content: sentSelectionPrompt,
});
assert.strictEqual(cleanNestedReply, "Почему выбран именно такой интервал?");

const cleanForForward = api.extractCleanMessageTextForForward({
  role: "user",
  content: sentSelectionPrompt,
});
assert.ok(!cleanForForward.includes("<REPLY_QUOTE"), "Forwarding must convert <REPLY_QUOTE> to blockquote");
assert.ok(cleanForForward.includes("> [В ответ Claude Opus · Ход #4]:"));
assert.ok(cleanForForward.includes("Почему выбран именно такой интервал?"));

// 5. Markdown insertion helper (`В текст`)
const mdQuote = api.buildReplyQuotedMarkdown({
  mode: "selection",
  turnNumber: 3,
  role: "assistant",
  authorLabel: "Antigravity",
  text: "gpuSupport=false\nmemory=8GB",
});
assert.ok(mdQuote.startsWith("> **Цитата (Antigravity · Ход #3):**"));
assert.ok(mdQuote.includes("> gpuSupport=false\n> memory=8GB"));

// 6. Outline titles & turn body preview: clean user question + smart hint on ultra-short replies
assert.strictEqual(
  api.extractCleanUserTurnBody(sentSelectionPrompt, 400),
  "Почему выбран именно такой интервал?"
);
const specificTitle = api.extractOutlineTurnTitle({
  role: "user",
  content: sentSelectionPrompt,
});
assert.ok(!specificTitle.includes("REPLY_QUOTE"));
assert.ok(specificTitle.includes("интервал"));

const shortReplyBlock = api.buildReplyQuoteXmlBlock({
  mode: "selection",
  msgId: "99",
  turnNumber: 2,
  role: "assistant",
  authorLabel: "Antigravity",
  text: "gpuSupport=false в файле .wslconfig",
});
const enrichedShortTitle = api.extractOutlineTurnTitle({
  role: "user",
  content: `Зачем это?\n\n${shortReplyBlock}`,
});
assert.ok(!enrichedShortTitle.includes("REPLY_QUOTE"));
assert.ok(enrichedShortTitle.includes("Зачем это?"));
assert.ok(
  enrichedShortTitle.includes("gpuSupport=false"),
  `Expected short reply outline title to include quoted hint, got: ${enrichedShortTitle}`
);

console.log("NODE_VM_REPLY_QUOTE_SUITE_OK");
"""
        env = dict(os.environ)
        env["APP_JS_PATH"] = str(app_js)
        res = subprocess.run(
            ["node", "-e", node_script],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(
            res.returncode,
            0,
            f"Node VM suite failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
        )
        self.assertIn("NODE_VM_REPLY_QUOTE_SUITE_OK", res.stdout)
        self._assert_live_db_untouched()

    def test_backend_outline_extractor_compatibility_readonly(self) -> None:
        from app import database

        prompt_with_reply = (
            "Почему здесь используется 60 секунд кэша?\n\n"
            '<REPLY_QUOTE mode="selection" msg_id="402" turn="4" role="assistant" '
            'author="Claude Opus 4.6" model="Claude Opus 4.6">\n'
            "[Контекст ответа: пользователь отвечает на выделенный фрагмент из сообщения "
            "(Claude Opus 4.6 · Ход #4) в текущем диалоге]\n"
            "Кэш check_docker_status() составляет 60 секунд.\n"
            "</REPLY_QUOTE>"
        )
        extracted = database.extract_outline_title_source_text(prompt_with_reply)
        self.assertTrue(extracted.startswith("Почему здесь используется 60 секунд кэша?"))
        self._assert_live_db_untouched()


if __name__ == "__main__":
    unittest.main()
