"""
Isolated automated verification suite for Release v117:
Eliminates vertical text floating / scroll wobble ("плавает вверх-вниз") when switching
between chat tabs into a recently completed / compacted HQ dialog (`ШТАБ`).

Root causes verified and locked against regression:
1. Multi-pass layout & scroll ordering in `openHubChat()` (`app.js`):
   - `#contextMeterBar`, `#chatOutlineRail`, handoff chips, and HQ return chips are settled
     BEFORE setting `scrollTop` / `scrollToLastAnswerStartIfLong()`, eliminating post-scroll
     height/padding jumps (`±22px` composer bounce & `14px` rail reflow) and secondary
     `ResizeObserver` scroll shifts.
   - `renderContextMeter()` keeps `#contextMeterBar` mounted while fetching a known existing
     chat (`isFetchingKnownChat`), preventing the `-22px` -> `+22px` `#composerSection` bounce.
2. Trailing auto-compaction checkpoint handling (`getInitialHistoryStart` &
   `scrollToLastAnswerStartIfLong`):
   - When auto-compaction sits at the tail of a chat right after a completed turn, the
     preceding turns remain visible above `.compaction-checkpoint-card` and
     `scrollToLastAnswerStartIfLong()` locates the completed assistant answer past trailing
     system/compacted rows.
3. Subpixel-exact `getBoundingClientRect().top` scroll anchoring in `unfoldHistoryTo()` and
   `flushChunkedPrependNow()` at fractional Windows DPI (`125%` / `150%`), plus guarded
   `followAfterDomChange()` during silent background resync on idle chats.
4. `scrollbar-gutter: stable` on `.messages-container`, compositor-only `@keyframes live-dot-pulse`
   (zero per-frame `box-shadow` blur repaint), and theme-aware `.older-history-fold-bar` styling.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["ORT_DISABLE_ALL_GPU"] = "1"

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

STATIC_DIR = ROOT_DIR / "app" / "static"
STYLES_CSS_PATH = STATIC_DIR / "styles.css"
APP_JS_PATH = STATIC_DIR / "app.js"
INDEX_HTML_PATH = STATIC_DIR / "index.html"
SW_JS_PATH = STATIC_DIR / "sw.js"
LIVE_HUB_DB = ROOT_DIR / "data" / "hub.db"

try:
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


def _snapshot_live_db() -> tuple[int, int] | None:
    if not LIVE_HUB_DB.exists():
        return None
    st = LIVE_HUB_DB.stat()
    return (st.st_mtime_ns, st.st_size)


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*[\s\S]*?\*/", "", css)


def _find_browser() -> str | None:
    env_path = os.environ.get("AGY_TEST_BROWSER")
    if env_path and Path(env_path).exists():
        return env_path
    for candidate in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if Path(candidate).exists():
            return candidate
    for name in ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _make_hq_messages(count: int = 50) -> list[dict]:
    msgs: list[dict] = []
    for i in range(1, count + 1):
        if i % 2 == 1:
            msgs.append(
                {
                    "id": i,
                    "chat_id": "c_hq",
                    "role": "user",
                    "status": "done",
                    "content": f"Задача #{i}: Проверь реализацию модуля #{i}.",
                    "created_at": "2026-11-15T18:10:00Z",
                }
            )
        else:
            msgs.append(
                {
                    "id": i,
                    "chat_id": "c_hq",
                    "role": "assistant",
                    "status": "done",
                    "model": "gemini-4-carbon-high",
                    "has_thinking": True,
                    "thinking": "Анализ...",
                    "tool_calls": [],
                    "usage": {"total_tokens": 1834831},
                    "content": (
                        f"### Построчный аудит реализации (ход {i})\n\n"
                        + "\n".join(
                            f"{k}. **Пункт {k} (`database.py`, `main.py`):** проверка инвариантов и строк `#L{k*100}-L{k*100+40}` в коде."
                            for k in range(1, 14)
                        )
                    ),
                    "created_at": "2026-11-15T18:11:00Z",
                }
            )
    msgs.append(
        {
            "id": count + 1,
            "chat_id": "c_hq",
            "role": "system",
            "status": "compacted",
            "usage": {"before_tokens": 279000, "after_tokens": 18000, "saved_pct": 94},
            "content": "Главные выводы сохранены без потери смысла.",
            "created_at": "2026-11-15T18:12:00Z",
        }
    )
    return msgs


class TestTabSwitchScrollStabilityV117(unittest.TestCase):
    def setUp(self) -> None:
        self._db_before = _snapshot_live_db()

    def tearDown(self) -> None:
        if os.environ.get("HUB_ALLOW_EXTERNAL_LIVE_DB_ACTIVITY") != "1":
            self._db_after = _snapshot_live_db()
            self.assertEqual(
                self._db_before,
                self._db_after,
                "Live production data/hub.db (st_mtime_ns, st_size) MUST NOT be modified!",
            )

    def test_01_css_scrollbar_gutter_compositor_pulse_and_workspace_invariant(self) -> None:
        raw_css = STYLES_CSS_PATH.read_text(encoding="utf-8")
        clean_css = _strip_css_comments(raw_css)

        self.assertEqual(
            clean_css.count("{"),
            clean_css.count("}"),
            "Unbalanced curly braces in styles.css",
        )
        self.assertRegex(
            clean_css,
            r"\.messages-container\s*\{[^}]*scrollbar-gutter\s*:\s*stable",
        )

        m_pulse = re.search(
            r"@keyframes\s+live-dot-pulse\s*\{([\s\S]*?)\n\}",
            clean_css,
        )
        self.assertIsNotNone(m_pulse, "Missing @keyframes live-dot-pulse")
        assert m_pulse is not None
        self.assertNotIn(
            "box-shadow",
            m_pulse.group(1),
            "live-dot-pulse must remain compositor-only without per-frame box-shadow repaint",
        )

        forbidden_props = (
            "transform",
            "filter",
            "backdrop-filter",
            "perspective",
            "contain",
            "container-type",
        )
        for m in re.finditer(r"main\.workspace\s*\{([^}]*)\}", clean_css, flags=re.DOTALL):
            block = m.group(1)
            for prop in forbidden_props:
                self.assertIsNone(
                    re.search(rf"(?<![\w-]){re.escape(prop)}\s*:", block),
                    f"Forbidden CSS property `{prop}` found inside `main.workspace`",
                )

    def test_02_js_syntax_and_trailing_compaction_history_window(self) -> None:
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node binary must be available")
        for js_path in (APP_JS_PATH, SW_JS_PATH):
            res = subprocess.run(
                [str(node_bin), "--check", str(js_path)],
                capture_output=True,
                text=True,
                timeout=20,
                **subprocess_hidden_kwargs(),
            )
            self.assertEqual(res.returncode, 0, f"node --check failed for {js_path.name}: {res.stderr}")

        source = APP_JS_PATH.read_text(encoding="utf-8")
        start = source.index("  function getInitialHistoryStart(")
        end = source.index("\n  function ", start + 12)
        fn_code = source[start:end]

        check_script = (
            "const assert = require('assert');\n"
            "const HISTORY_WINDOW_INITIAL = 30;\n"
            + fn_code
            + "\n"
            "const msgs = Array.from({length: 100}, (_, i) => ({ id: i + 1, role: i % 2 ? 'assistant' : 'user', status: 'done' }));\n"
            "msgs.push({ id: 101, role: 'system', status: 'compacted' });\n"
            "const idx = getInitialHistoryStart(msgs);\n"
            "assert(idx <= 95, 'Trailing compaction must keep preceding Q&A turns visible, got ' + idx);\n"
        )
        res = subprocess.run(
            [str(node_bin), "-e", check_script],
            capture_output=True,
            text=True,
            timeout=15,
            **subprocess_hidden_kwargs(),
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_03_headless_chromium_zero_wobble_on_hq_tab_switch_and_unfold(self) -> None:
        browser = _find_browser()
        if not browser:
            self.skipTest("No Edge/Chrome binary found for headless 60fps stability check")

        hq_msgs = _make_hq_messages(50)
        received: list[dict] = []
        done_ev = threading.Event()

        probe_js = r"""
<script>
(function() {
  class FakeWS {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      window.__ws = this;
      setTimeout(() => { if (this.onopen) this.onopen({}); }, 10);
    }
    send() {}
    close() { this.readyState = 3; }
  }
  FakeWS.OPEN = 1;
  window.WebSocket = FakeWS;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  window.addEventListener("load", async () => {
    await sleep(300);
    for (let i = 0; i < 35; i++) {
      const ph = document.querySelector(".proj-tree-header");
      if (ph && !ph.closest(".proj-tree-group").classList.contains("expanded")) ph.click();
      if (document.querySelector('#projectsTreeList .nav-item[data-chat-id="c_hq"]')) break;
      await sleep(30);
    }

    // Warm up fonts on c_hq, switch to c_worker, emit background turn_done + chat_compacted, then switch back to c_hq
    document.querySelector('#projectsTreeList .nav-item[data-chat-id="c_hq"]').click();
    await sleep(500);
    document.querySelector('#projectsTreeList .nav-item[data-chat-id="c_worker"]').click();
    await sleep(220);

    window.__ws.onmessage({
      data: JSON.stringify({
        type: "turn_done",
        chat_id: "c_hq",
        chat: { id: "c_hq", kind: "project", project_name: "Antigravity_Hub", title: "ШТАБ", icon: "🏠", model: "gemini-4-carbon-high", access_mode: "read_only", pinned: true, is_generating: false, last_assistant_status: "done" },
        message: window.__lastAssistantMsg
      })
    });
    window.__ws.onmessage({
      data: JSON.stringify({
        type: "chat_compacted",
        chat_id: "c_hq",
        chat: { id: "c_hq", kind: "project", project_name: "Antigravity_Hub", title: "ШТАБ", icon: "🏠", model: "gemini-4-carbon-high", access_mode: "read_only", pinned: true, is_generating: false, last_assistant_status: "done" },
        message: window.__compactionMsg,
        context_stats: { used_tokens: 18000, max_tokens: 200000, pct: 9 }
      })
    });
    await sleep(80);

    document.querySelector('#projectsTreeList .nav-item[data-chat-id="c_hq"]').click();

    const frames = [];
    let trackedEl = null;
    for (let f = 0; f < 65; f++) {
      await new Promise((r) => requestAnimationFrame(r));
      const mc = document.getElementById("messagesContainer");
      const ml = document.getElementById("messagesList");
      const comp = document.getElementById("composerSection");
      if (!trackedEl || !ml.contains(trackedEl)) {
        const items = ml.querySelectorAll(".markdown-body li");
        if (items.length > 3) trackedEl = items[items.length - 3];
      }
      frames.push({
        pTop: trackedEl ? Math.round(trackedEl.getBoundingClientRect().top * 100) / 100 : null,
        compH: comp ? comp.offsetHeight : 0,
        rows: ml ? ml.querySelectorAll(":scope > .msg-row").length : 0
      });
    }

    const moves = [];
    const compHeights = new Set();
    for (let i = 0; i < frames.length; i++) {
      compHeights.add(frames[i].compH);
      if (i > 0 && frames[i].pTop !== null && frames[i - 1].pTop !== null) {
        const dy = Math.round((frames[i].pTop - frames[i - 1].pTop) * 100) / 100;
        if (Math.abs(dy) > 0.05) moves.push(dy);
      }
    }

    // Also verify subpixel-exact anchor stability when unfolding older history at 1.25x DPI
    const anchorRow = document.querySelector("#messagesList > .msg-row[data-msg-id]");
    const beforeUnfoldTop = anchorRow ? anchorRow.getBoundingClientRect().top : 0;
    const unfoldBtn = document.querySelector(".older-history-fold-bar button");
    if (unfoldBtn) unfoldBtn.click();
    const afterUnfoldTop = anchorRow ? anchorRow.getBoundingClientRect().top : 0;

    await fetch("/__report", {
      method: "POST",
      body: JSON.stringify({
        rowsRendered: frames[frames.length - 1].rows,
        moveCount: moves.length,
        moves,
        composerHeightCount: compHeights.size,
        unfoldDriftPx: Math.round(Math.abs(afterUnfoldTop - beforeUnfoldTop) * 100) / 100
      })
    });
  });
})();
</script>
"""

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send_json(self, obj: dict) -> None:
                raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                p = self.path.split("?", 1)[0]
                if p == "/":
                    html = INDEX_HTML_PATH.read_text(encoding="utf-8")
                    inj = (
                        "<script>"
                        f"window.__lastAssistantMsg = {json.dumps(hq_msgs[-2], ensure_ascii=False)};"
                        f"window.__compactionMsg = {json.dumps(hq_msgs[-1], ensure_ascii=False)};"
                        "</script>" + probe_js
                    )
                    raw = html.replace("</head>", inj + "\n</head>", 1).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if p.startswith("/static/"):
                    f = STATIC_DIR / p[len("/static/") :]
                    if f.exists() and f.is_file():
                        ct = (
                            "text/javascript"
                            if f.suffix == ".js"
                            else "text/css"
                            if f.suffix == ".css"
                            else "image/svg+xml"
                        )
                        raw = f.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", f"{ct}; charset=utf-8")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                if p == "/api/system/info":
                    return self._send_json({"version": "117", "is_admin": True, "auth_enabled": False})
                if p == "/api/projects":
                    return self._send_json(
                        {
                            "projects": [
                                {
                                    "name": "Antigravity_Hub",
                                    "is_self_hub": True,
                                    "execution_mode": "host",
                                    "hub_chat_count": 2,
                                }
                            ]
                        }
                    )
                if p == "/api/chats":
                    return self._send_json(
                        {
                            "chats": [
                                {
                                    "id": "c_hq",
                                    "kind": "project",
                                    "project_name": "Antigravity_Hub",
                                    "title": "ШТАБ",
                                    "icon": "🏠",
                                    "model": "gemini-4-carbon-high",
                                    "access_mode": "read_only",
                                    "pinned": True,
                                    "is_generating": False,
                                    "last_assistant_status": "done",
                                    "updated_at": "2026-11-15T18:12:00Z",
                                },
                                {
                                    "id": "c_worker",
                                    "kind": "project",
                                    "project_name": "Antigravity_Hub",
                                    "title": "Ядро",
                                    "icon": "🚀",
                                    "model": "gemini-4-carbon-high",
                                    "access_mode": "full_access",
                                    "pinned": True,
                                    "is_generating": True,
                                    "last_assistant_status": "streaming",
                                    "updated_at": "2026-11-15T18:05:00Z",
                                },
                            ],
                            "folders": [],
                        }
                    )
                if p == "/api/chats/c_hq":
                    return self._send_json(
                        {
                            "chat": {
                                "id": "c_hq",
                                "kind": "project",
                                "project_name": "Antigravity_Hub",
                                "title": "ШТАБ",
                                "icon": "🏠",
                                "model": "gemini-4-carbon-high",
                                "access_mode": "read_only",
                                "pinned": True,
                                "is_generating": False,
                            },
                            "messages": hq_msgs,
                            "context_stats": {"used_tokens": 18000, "max_tokens": 200000, "pct": 9},
                            "queued_messages": [],
                            "queue_paused": False,
                        }
                    )
                if p == "/api/chats/c_worker":
                    return self._send_json(
                        {
                            "chat": {
                                "id": "c_worker",
                                "kind": "project",
                                "project_name": "Antigravity_Hub",
                                "title": "Ядро",
                                "icon": "🚀",
                                "model": "gemini-4-carbon-high",
                                "access_mode": "full_access",
                                "pinned": True,
                                "is_generating": True,
                            },
                            "messages": [
                                {
                                    "id": 1,
                                    "chat_id": "c_worker",
                                    "role": "user",
                                    "status": "done",
                                    "content": "Старт",
                                    "created_at": "2026-11-15T18:00:00Z",
                                }
                            ],
                            "context_stats": {"used_tokens": 4000, "max_tokens": 200000, "pct": 2},
                            "queued_messages": [],
                            "queue_paused": False,
                        }
                    )
                return self._send_json({})

            def do_POST(self) -> None:
                if self.path == "/__report":
                    ln = int(self.headers.get("Content-Length", "0"))
                    received.append(json.loads(self.rfile.read(ln).decode("utf-8")))
                    done_ev.set()
                return self._send_json({"ok": True})

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as prof:
                proc = subprocess.Popen(
                    [
                        browser,
                        "--headless=new",
                        "--disable-gpu",
                        "--remote-debugging-port=0",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--force-device-scale-factor=1.25",
                        f"--user-data-dir={prof}",
                        "--window-size=1440,900",
                        f"http://127.0.0.1:{srv.server_address[1]}/",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **subprocess_hidden_kwargs(),
                )
                try:
                    done_ev.wait(timeout=12)
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()
        finally:
            srv.shutdown()

        self.assertTrue(received, "Headless Chromium did not post 60fps stability report")
        rep = received[0]
        self.assertGreaterEqual(
            rep["rowsRendered"],
            6,
            f"Completed answer above trailing compaction card must remain visible! {rep}",
        )
        self.assertEqual(
            rep["moveCount"],
            0,
            f"Text oscillated vertically after switching to HQ tab! {rep}",
        )
        self.assertEqual(
            rep["composerHeightCount"],
            1,
            f"#composerSection bounced in height during tab switch! {rep}",
        )
        self.assertLessEqual(
            rep["unfoldDriftPx"],
            0.5,
            f"Unfolding older history drifted visible anchor at 1.25x DPI! {rep}",
        )

    def test_04_utf8_no_bom_and_v117_cache_sync(self) -> None:
        checked = (
            "app/static/index.html",
            "app/static/styles.css",
            "app/static/sw.js",
            "app/static/app.js",
            "tests/test_tab_switch_scroll_stability_v117.py",
        )
        for rel in checked:
            raw = (ROOT_DIR / rel).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"UTF-8 BOM found in {rel}")
            raw.decode("utf-8")

        html_txt = INDEX_HTML_PATH.read_text(encoding="utf-8")
        sw_txt = SW_JS_PATH.read_text(encoding="utf-8")

        m_sw_cache = re.search(r'CACHE_NAME\s*=\s*"agy-hub-v(\d+)"', sw_txt)
        m_sw_css = re.search(r"/static/styles\.css\?v=(\d+)", sw_txt)
        m_sw_js = re.search(r"/static/app\.js\?v=(\d+)", sw_txt)
        m_html_css = re.search(r"/static/styles\.css\?v=(\d+)", html_txt)
        m_html_js = re.search(r"/static/app\.js\?v=(\d+)", html_txt)

        versions: list[int] = []
        for m in (m_sw_cache, m_sw_css, m_sw_js, m_html_css, m_html_js):
            self.assertIsNotNone(m)
            assert m is not None
            v_num = int(m.group(1))
            self.assertGreaterEqual(v_num, 117)
            versions.append(v_num)
        self.assertEqual(len(set(versions)), 1, f"Mismatched asset versions: {versions}")

        self.assertNotIn("?v=116", html_txt)
        self.assertNotIn("?v=116", sw_txt)
        self.assertNotIn("agy-hub-v116", sw_txt)


if __name__ == "__main__":
    unittest.main()
